"""practice measurements from raw audio, without scoring voice identity or talent."""

import json
import math
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import soundfile as sf

from .alignment import token

VERSION = "practice-v2"

# these are engineering defaults, not calibrated probabilities of correct pronunciation or pitch.
MIN_ASR_PROBABILITY = 0.5
MIN_ANCHORS = 6
MIN_ANCHOR_SPAN = 2.0
PITCH_RATE = 16000
PITCH_FRAME = 1024
PITCH_HOP = 320
PITCH_MIN_HZ = 50.0
PITCH_MAX_HZ = 2006.0
PITCH_BATCH_SIZE = 128
MIN_PERIODICITY = 0.6
MIN_VOICED_COVERAGE = 0.2
MIN_VOICED_SECONDS = 1.0
PITCH_CHUNK_SECONDS = 20


def _number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _valid_word(word):
    return (
        isinstance(word, dict)
        and isinstance(word.get("text"), str)
        and bool(token(word["text"]))
        and _number(word.get("start"))
        and _number(word.get("end"))
        and 0 <= word["start"] < word["end"]
    )


def _reliable(word):
    probability = word.get("probability")
    return probability is None or (_number(probability) and MIN_ASR_PROBABILITY <= probability <= 1)


def _exact_matches(reference, performance):
    # longest common subsequence counts ordered exact tokens, never fuzzy matching scores.
    row = [0] * (len(reference) + 1)
    for performed in performance:
        previous = row
        row = [0]
        for i, expected in enumerate(reference, 1):
            row.append(previous[i - 1] + 1 if performed == expected else max(previous[i], row[-1]))
    return row[-1]


def _metric(identifier, label, value, reference, unit, lower_is_better, detail, reason=""):
    return {
        "id": identifier,
        "label": label,
        "available": value is not None,
        "value": None if value is None else float(value),
        "reference": None if reference is None else float(reference),
        "unit": unit,
        "lower_is_better": lower_is_better,
        "detail": detail,
        "reason": reason if value is None else "",
    }


@dataclass
class _Pitch:
    times: np.ndarray
    semitones: np.ndarray
    coverage: float = 0
    voiced_seconds: float = 0
    boundary_fraction: float = 0
    octave_jumps: int = 0
    reason: str = ""

    @property
    def span(self):
        if self.reason:
            return None
        low, high = np.percentile(self.semitones[np.isfinite(self.semitones)], [5, 95])
        return float(high - low)

    def word_median(self, word):
        mask = (self.times >= word["start"]) & (self.times < word["end"])
        values = self.semitones[mask]
        valid = np.isfinite(values)
        if self.reason or valid.sum() < 3 or valid.mean() < MIN_VOICED_COVERAGE:
            return None
        return float(np.median(values[valid]))

    def evidence(self):
        return {
            "usable_frames": int(np.isfinite(self.semitones).sum()),
            "total_frames": len(self.times),
            "voiced_coverage": float(self.coverage),
            "voiced_seconds": float(self.voiced_seconds),
            "boundary_fraction": float(self.boundary_fraction),
            "possible_octave_jumps": int(self.octave_jumps),
            "reason": self.reason,
        }


def _decode_pitch(activations):
    """use torchcrepe's local weighted-argmax rule without randomized bin weights."""
    import torch

    bins = activations.argmax(dim=1)
    indices = torch.arange(activations.shape[1], device=activations.device)[None, :, None]
    window = torch.abs(indices - bins[:, None, :]) <= 4
    weights = torch.sigmoid(activations).masked_fill(~window, 0)
    centers = 20 * indices.to(torch.float64) + 1997.3794084376191
    cents = (weights * centers).sum(dim=1) / weights.sum(dim=1)
    return bins, 10 * torch.pow(2, cents / 1200)


def _pitch(path, start, end):
    empty = np.array([], dtype=float)
    info = sf.info(path)
    first, last = (
        max(0, int(start * info.samplerate)),
        min(info.frames, math.ceil(end * info.samplerate)),
    )
    if last <= first:
        return _Pitch(empty, empty, reason="No audio overlaps the transcribed section.")
    samples, rate = sf.read(path, start=first, stop=last, dtype="float32", always_2d=True)
    if not np.isfinite(samples).all():
        return _Pitch(empty, empty, reason="The audio contains invalid samples.")
    # choose the strongest channel so phase-inverted stereo cannot cancel the voice.
    samples = samples[:, np.argmax(np.mean(samples.astype(float) ** 2, axis=0))]
    if not len(samples) or np.max(np.abs(samples)) < 1e-6:
        return _Pitch(empty, empty, reason="No audible voiced signal in this section.")
    try:
        import librosa
        import torch
        import torchcrepe
    except ImportError:
        return _Pitch(empty, empty, reason="Pitch analysis requires the local model dependencies.")
    if rate != PITCH_RATE:
        samples = librosa.resample(samples, orig_sr=rate, target_sr=PITCH_RATE)
    # bounded chunks and inference batches limit memory; padding supplies context across chunk edges.
    chunk_size = PITCH_CHUNK_SECONDS * PITCH_RATE
    context = PITCH_HOP * math.ceil(PITCH_FRAME * 2 / PITCH_HOP)
    tracks, grids, energies, periodicities, stable_periodicities = [], [], [], [], []
    for cursor in range(0, len(samples), chunk_size):
        stop = min(len(samples), cursor + chunk_size)
        left, right = max(0, cursor - context), min(len(samples), stop + context)
        segment = samples[left:right]
        # the full supported frequency band avoids assuming a speaking or singing register.
        # framewise decoding also avoids a viterbi endpoint bias seen on the real reference.
        with torch.inference_mode():
            tensor = torch.from_numpy(np.ascontiguousarray(segment))[None]
            activations = [
                torchcrepe.infer(frames, model="full", device="cpu").T[None]
                for frames in torchcrepe.preprocess(
                    tensor, PITCH_RATE, PITCH_HOP, batch_size=PITCH_BATCH_SIZE, device="cpu"
                )
            ]
            frequencies, periodicity = torchcrepe.postprocess(
                torch.cat(activations, dim=2),
                fmin=PITCH_MIN_HZ,
                fmax=PITCH_MAX_HZ,
                decoder=_decode_pitch,
                return_periodicity=True,
            )
            stable_periodicity = torchcrepe.filter.median(periodicity, 3)[0].numpy()
            periodicity = periodicity[0].numpy()
            f0 = frequencies[0].numpy()
        positions = left + np.arange(len(f0)) * PITCH_HOP
        selected = (positions >= cursor) & (positions < stop)
        rms = librosa.feature.rms(y=segment, frame_length=PITCH_FRAME, hop_length=PITCH_HOP)[0]
        tracks.append(f0[selected])
        grids.append(positions[selected] / PITCH_RATE + first / rate)
        energies.append(rms[selected])
        periodicities.append(periodicity[selected])
        stable_periodicities.append(stable_periodicity[selected])
    frequencies = np.concatenate(tracks)
    times = np.concatenate(grids)
    energy = np.concatenate(energies)
    # a relative threshold preserves ordinary gain changes; the absolute floor rejects numerical silence.
    energy_floor = max(1e-6, float(np.percentile(energy, 95)) * 10 ** (-35 / 20))
    usable = (
        (np.concatenate(periodicities) >= MIN_PERIODICITY)
        & (np.concatenate(stable_periodicities) >= MIN_PERIODICITY)
        & (energy >= energy_floor)
        & np.isfinite(frequencies)
        & (frequencies > 0)
    )
    semitones = np.full(len(frequencies), np.nan)
    semitones[usable] = 69 + 12 * np.log2(frequencies[usable] / 440)
    coverage = float(usable.mean()) if len(usable) else 0
    seconds = float(usable.sum() * PITCH_HOP / PITCH_RATE)
    floor_note = 69 + 12 * math.log2(PITCH_MIN_HZ / 440)
    ceiling_note = 69 + 12 * math.log2(PITCH_MAX_HZ / 440)
    near_boundary = usable & ((semitones < floor_note + 1) | (semitones > ceiling_note - 1))
    boundary = float(near_boundary.sum() / usable.sum()) if usable.any() else 0
    # an octave-sized jump can be real singing or tracking error; report it without silently correcting it.
    delta = np.abs(np.diff(semitones[usable]))
    gaps = np.diff(times[usable])
    jump_count = int(np.count_nonzero((delta >= 10) & (delta <= 14) & (gaps <= 0.2)))
    reason = ""
    if seconds < MIN_VOICED_SECONDS or coverage < MIN_VOICED_COVERAGE:
        reason = "Not enough stable voiced audio: need 1 second and 20% usable pitch coverage."
    elif boundary >= 0.1:
        reason = "Pitch estimates crowd the tracking limits; the observed range may be clipped."
    return _Pitch(times, semitones, coverage, seconds, boundary, jump_count, reason)


def assess(work_dir: Path, candidate_id: str) -> dict:
    """return deterministic practice-v2 measurements from the selected raw source excerpt.

    timing removes a single median start offset, never tempo drift. pitch shape centers
    each set of paired word medians; range measures p95 minus p5 in the full excerpt.
    unavailable measurements have null values and a displayable reason. timestamps are
    asr estimates, and unknown legacy confidence is explicitly retained as unknown.
    """
    work_dir = Path(work_dir)
    data = json.loads((work_dir / "analysis.json").read_text(encoding="utf-8"))
    match = next((m for m in data["matches"] if m["candidate"]["id"] == candidate_id), None)
    if match is None:
        raise ValueError("That section does not belong to this analysis.")
    candidate = match["candidate"]
    reference_start, reference_end = candidate["start"], candidate["end"]
    if not (
        _number(reference_start) and _number(reference_end) and 0 <= reference_start < reference_end
    ):
        raise ValueError("That section has invalid audio bounds.")
    reference, performance = data["reference"], data["performance"]
    reference_indices = {
        i
        for i, word in enumerate(reference)
        if _valid_word(word)
        and reference_start <= (word["start"] + word["end"]) / 2 < reference_end
    }
    selected_reference = [reference[i] for i in sorted(reference_indices)]
    selected_performance = [word for word in performance if _valid_word(word)]
    if not selected_reference or not selected_performance:
        raise ValueError("That section does not contain usable transcript timestamps.")
    performance_start = min(word["start"] for word in selected_performance)
    performance_end = max(word["end"] for word in selected_performance)
    all_words = selected_reference + selected_performance
    unknown = sum(word.get("probability") is None for word in all_words)
    rejected = sum(not _reliable(word) for word in all_words)
    exact_count = _exact_matches(
        [token(word["text"]) for word in selected_reference],
        [token(word["text"]) for word in selected_performance],
    )
    pairs = []
    previous_i, previous_j = -1, -1
    for pair in match["pairs"]:
        i, j = pair
        if not (
            isinstance(i, int)
            and isinstance(j, int)
            and 0 <= i < len(performance)
            and j in reference_indices
        ):
            continue
        p, r = performance[i], reference[j]
        if (
            i > previous_i
            and j > previous_j
            and _valid_word(p)
            and _reliable(p)
            and _reliable(r)
            and token(p["text"]) == token(r["text"])
            and (
                not pairs
                or (p["start"] > pairs[-1][0]["start"] and r["start"] > pairs[-1][1]["start"])
            )
        ):
            pairs.append((p, r))
            previous_i, previous_j = i, j
    offsets = np.array([p["start"] - r["start"] for p, r in pairs])
    offset = float(np.median(offsets)) if len(offsets) else 0.0
    residuals = (offsets - offset) * 1000
    reference_span = pairs[-1][1]["start"] - pairs[0][1]["start"] if len(pairs) > 1 else 0
    performance_span = pairs[-1][0]["start"] - pairs[0][0]["start"] if len(pairs) > 1 else 0
    enough_anchors = (
        len(pairs) >= MIN_ANCHORS and min(reference_span, performance_span) >= MIN_ANCHOR_SPAN
    )
    timing_reason = (
        "Need at least six reliable exact word anchors spanning 2 seconds in both recordings."
    )
    reference_pitch = _pitch(work_dir / "reference.wav", reference_start, reference_end)
    performance_pitch = _pitch(work_dir / "performance.wav", performance_start, performance_end)
    word_pitches = [
        (r, p, reference_pitch.word_median(r), performance_pitch.word_median(p)) for p, r in pairs
    ]
    voiced_pairs = [pair for pair in word_pitches if pair[2] is not None and pair[3] is not None]
    shape, reference_center, performance_center = None, 0.0, 0.0
    range_reason = (
        f"Original vocal: {reference_pitch.reason}"
        if reference_pitch.reason
        else f"Your recording: {performance_pitch.reason}"
        if performance_pitch.reason
        else ""
    )
    shape_reason = range_reason
    if not shape_reason and len(voiced_pairs) >= MIN_ANCHORS:
        span = min(
            voiced_pairs[-1][0]["start"] - voiced_pairs[0][0]["start"],
            voiced_pairs[-1][1]["start"] - voiced_pairs[0][1]["start"],
        )
        if span >= MIN_ANCHOR_SPAN:
            reference_values = np.array([pair[2] for pair in voiced_pairs])
            performance_values = np.array([pair[3] for pair in voiced_pairs])
            reference_center = float(np.median(reference_values))
            performance_center = float(np.median(performance_values))
            shape = float(
                np.median(
                    np.abs(
                        (performance_values - performance_center)
                        - (reference_values - reference_center)
                    )
                )
            )
    if shape is None and not shape_reason:
        shape_reason = (
            "Need six jointly voiced exact words spanning 2 seconds for a pitch contour comparison."
        )
    result_metrics = [
        _metric(
            "lyric_agreement",
            "Recognized word agreement",
            exact_count / max(len(selected_reference), len(selected_performance)) * 100,
            100,
            "%",
            False,
            "Exact ordered speech-recognition transcript matches divided by the longer transcript. This agreement is not pronunciation or articulation accuracy.",
        ),
        _metric(
            "timing",
            "Word timing difference",
            float(np.median(np.abs(residuals))) if enough_anchors else None,
            0,
            "ms",
            True,
            "Median absolute word-start difference after removing only a constant start offset. Tempo drift remains visible; timestamps are ASR estimates.",
            timing_reason,
        ),
        _metric(
            "pace",
            "Relative pace",
            reference_span / performance_span * 100 if enough_anchors else None,
            100,
            "%",
            False,
            "Reference word-anchor span divided by your span. 100% is the same pace; lower values mean a slower performance.",
            timing_reason,
        ),
        _metric(
            "pitch_shape",
            "Pitch movement difference",
            shape,
            0,
            "semitones",
            True,
            "Median difference between paired word-level pitch medians after centering each contour. A constant register shift is removed; this does not measure voice similarity.",
            shape_reason,
        ),
        _metric(
            "pitch_range",
            "Observed pitch span",
            performance_pitch.span if not range_reason else None,
            reference_pitch.span,
            "semitones",
            False,
            "P95 minus P5 of usable pitch estimates in this excerpt. This is observed vocal movement, not physiological range or a higher-is-better score.",
            range_reason,
        ),
    ]
    notes = [
        "Measurements use your original recording before timing correction or mix levels.",
        "Transcription and word times can be wrong, especially in rap, backing vocals or separated audio. Check the transcript evidence.",
        "CREPE periodicity gates are engineering defaults, not probabilities of correct pitch. Separation artifacts and octave errors can distort estimates; word medians do not provide phonetic alignment.",
    ]
    if unknown:
        notes.append(
            "Legacy ASR words have no stored confidence; their timing reliability cannot be checked retrospectively."
        )
    if rejected:
        notes.append(
            f"{rejected} low or invalid confidence transcript words were excluded from timing and paired pitch anchors."
        )
    for label, track in (("Original", reference_pitch), ("Your recording", performance_pitch)):
        if track.octave_jumps:
            notes.append(
                f"{label}: abrupt octave-sized changes may be musical jumps or tracking errors. Inspect the contour."
            )
        if track.boundary_fraction > 0:
            notes.append(
                f"{label}: some pitch estimates are near the tracking limits of 50 to 2006 Hz."
            )
    available = sum(metric["available"] for metric in result_metrics)
    return {
        "status": "ready",
        "version": VERSION,
        "candidate_id": candidate_id,
        "summary": f"{available} of 5 practice measurements available. These compare this excerpt, not overall vocal ability.",
        "error": "",
        "metrics": result_metrics,
        "notes": notes,
        "reference_start": float(reference_start),
        "reference_end": float(reference_end),
        "performance_start": float(performance_start),
        "performance_end": float(performance_end),
        "reference_text": " ".join(word["text"].strip() for word in selected_reference),
        "performance_text": " ".join(word["text"].strip() for word in selected_performance),
        "words": [
            {
                "reference": r["text"],
                "performance": p["text"],
                "reference_start": float(r["start"]),
                "performance_start": float(p["start"]),
                "delta_ms": float(delta),
            }
            for (p, r), delta in zip(pairs, residuals, strict=True)
        ],
        "pitch": {
            "unit": "semitones from median",
            "register_shift_semitones": performance_center - reference_center
            if shape is not None
            else None,
            "points": [
                {
                    "time": (r["start"] + r["end"]) / 2,
                    "reference": rv - reference_center if rv is not None else None,
                    "performance": pv - performance_center if pv is not None else None,
                }
                for r, _p, rv, pv in word_pitches
            ]
            if shape is not None
            else [],
        },
        "evidence": {
            "audio_sources": ["reference.wav", "performance.wav"],
            "timestamp_provenance": "ASR word timestamps, not forced phoneme alignment",
            "pitch_method": "CREPE full, deterministic local weighted argmax",
            "pitch_sample_rate": PITCH_RATE,
            "pitch_hop_seconds": PITCH_HOP / PITCH_RATE,
            "pitch_frequency_limits_hz": [PITCH_MIN_HZ, PITCH_MAX_HZ],
            "reference_words": len(selected_reference),
            "performance_words": len(selected_performance),
            "exact_ordered_matches": exact_count,
            "timing_anchors": len(pairs),
            "unknown_probability_words": unknown,
            "rejected_probability_words": rejected,
            "removed_start_offset_ms": offset * 1000 if pairs else None,
            "reference_anchor_span": float(reference_span),
            "performance_anchor_span": float(performance_span),
            "jointly_voiced_words": len(voiced_pairs),
            "reference_pitch": reference_pitch.evidence(),
            "performance_pitch": performance_pitch.evidence(),
            "thresholds": {
                "asr_probability": MIN_ASR_PROBABILITY,
                "periodicity": MIN_PERIODICITY,
                "periodicity_median_frames": 3,
                "octave_warning_max_gap_seconds": 0.2,
                "minimum_anchors": MIN_ANCHORS,
                "minimum_anchor_span_seconds": MIN_ANCHOR_SPAN,
                "minimum_voiced_coverage": MIN_VOICED_COVERAGE,
                "minimum_voiced_seconds": MIN_VOICED_SECONDS,
            },
        },
    }
