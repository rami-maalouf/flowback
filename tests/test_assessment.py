import json
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf

from cover_engine.assessment import VERSION, assess

TEXT = ["we", "chase", "the", "sunlight", "through", "this", "quiet", "city"]
NOTES = np.array([50, 52, 53, 55, 57, 55, 53, 52], dtype=float)
RATE = 16000


def make_words(text=TEXT, *, offset=0.4, step=0.45, confidence=True):
    return [
        {
            "text": word,
            "start": offset + i * step,
            "end": offset + i * step + step * 0.76,
            **({"probability": 0.95} if confidence else {}),
        }
        for i, word in enumerate(text)
    ]


def audio(words, notes=NOTES, *, gain=0.2, noise=False, silent=False, phase=0):
    samples = np.zeros(int((words[-1]["end"] + 0.3) * RATE), dtype=np.float32)
    rng = np.random.default_rng(17)
    for word, note in zip(words, notes, strict=True):
        start, end = int(word["start"] * RATE), int(word["end"] * RATE)
        time = np.arange(end - start) / RATE
        hz = 440 * 2 ** ((note - 69) / 12)
        wave = (
            rng.normal(size=len(time))
            if noise
            else np.sin(2 * np.pi * hz * time + phase)
            + 0.35 * np.sin(4 * np.pi * hz * time + 2 * phase)
            + 0.1 * np.sin(6 * np.pi * hz * time + 3 * phase)
        )
        fade = np.minimum(1, np.minimum(time / 0.02, (time[-1] - time) / 0.02))
        samples[start:end] = 0 if silent else gain * wave * fade
    return samples


def fixture(
    path: Path,
    *,
    reference=None,
    performance=None,
    notes=NOTES,
    gain=0.2,
    noise=False,
    silent=False,
    phase=0,
):
    path.mkdir(parents=True, exist_ok=True)
    reference = reference or make_words()
    performance = performance or make_words()
    data = {
        "reference": reference,
        "performance": performance,
        "matches": [
            {
                "candidate": {
                    "id": "section-test",
                    "start": reference[0]["start"],
                    "end": reference[-1]["end"],
                },
                "pairs": [[i, i] for i in range(min(len(reference), len(performance)))],
            }
        ],
    }
    (path / "analysis.json").write_text(json.dumps(data))
    sf.write(path / "reference.wav", audio(reference), RATE, subtype="FLOAT")
    sf.write(
        path / "performance.wav",
        audio(performance, notes, gain=gain, noise=noise, silent=silent, phase=phase),
        RATE,
        subtype="FLOAT",
    )
    return data


def metrics(result):
    return {metric["id"]: metric for metric in result["metrics"]}


@pytest.fixture(scope="module")
def identity(tmp_path_factory):
    path = tmp_path_factory.mktemp("identity")
    fixture(path)
    return assess(path, "section-test")


def test_identity_has_transparent_measurements_and_json_finite(identity):
    values = metrics(identity)
    assert identity["status"] == "ready"
    assert identity["version"] == VERSION == "practice-v2"
    assert set(values) == {"lyric_agreement", "timing", "pace", "pitch_shape", "pitch_range"}
    assert all(value["available"] for value in values.values())
    assert values["lyric_agreement"]["value"] == 100
    assert values["timing"]["value"] == pytest.approx(0)
    assert values["pace"]["value"] == pytest.approx(100)
    assert values["pitch_shape"]["value"] < 0.15
    assert values["pitch_range"]["value"] == pytest.approx(values["pitch_range"]["reference"])
    assert identity["reference_text"] == identity["performance_text"] == " ".join(TEXT)
    assert identity["evidence"]["timing_anchors"] == 8
    assert len(identity["words"]) == 8
    assert identity["pitch"]["unit"] == "semitones from median"
    assert identity["pitch"]["points"][0]["time"] == pytest.approx(0.571)
    assert "pronunciation" in values["lyric_agreement"]["detail"].lower()
    json.dumps(identity, allow_nan=False)


def test_gain_leading_offset_and_register_do_not_reduce_contour_similarity(tmp_path, identity):
    fixture(
        tmp_path,
        performance=make_words(offset=1.7),
        notes=NOTES + 7,
        gain=0.012,
    )
    result = assess(tmp_path, "section-test")
    values = metrics(result)
    assert values["timing"]["value"] == pytest.approx(0, abs=0.01)
    assert values["pace"]["value"] == pytest.approx(100)
    assert values["pitch_shape"]["value"] < 0.3
    assert values["pitch_range"]["value"] == pytest.approx(
        metrics(identity)["pitch_range"]["value"], abs=0.3
    )
    assert result["pitch"]["register_shift_semitones"] == pytest.approx(7, abs=0.3)
    assert result["evidence"]["removed_start_offset_ms"] == pytest.approx(1300)
    assert result["pitch"]["points"][0]["time"] == pytest.approx(0.571)


def test_slow_take_reports_pace_and_drift_without_time_warping(tmp_path):
    fixture(tmp_path, performance=make_words(offset=1, step=0.6))
    result = assess(tmp_path, "section-test")
    values = metrics(result)
    assert values["pace"]["value"] == pytest.approx(75)
    assert values["timing"]["value"] == pytest.approx(300)
    assert result["words"][0]["delta_ms"] == pytest.approx(-525)
    assert result["words"][-1]["delta_ms"] == pytest.approx(525)


def test_narrower_delivery_has_smaller_observed_range(tmp_path, identity):
    fixture(tmp_path, notes=NOTES[0] + (NOTES - NOTES[0]) * 0.5)
    result = metrics(assess(tmp_path, "section-test"))
    assert result["pitch_range"]["value"] == pytest.approx(
        metrics(identity)["pitch_range"]["value"] / 2, abs=0.4
    )
    assert result["pitch_shape"]["value"] > 0.4


def test_fuzzy_words_do_not_count_as_exact_asr_agreement(tmp_path):
    text = TEXT.copy()
    text[6] = "quite"
    fixture(tmp_path, performance=make_words(text))
    result = assess(tmp_path, "section-test")
    values = metrics(result)
    assert values["lyric_agreement"]["value"] == 87.5
    assert result["evidence"]["timing_anchors"] == 7
    assert "quiet" not in [word["reference"] for word in result["words"]]
    assert "recognized" in values["lyric_agreement"]["label"].lower()
    assert "pronunciation" in values["lyric_agreement"]["detail"].lower()


def test_low_confidence_words_do_not_manufacture_reliable_timing(tmp_path):
    performance = make_words()
    for word in performance[:3]:
        word["probability"] = 0.1
    fixture(tmp_path, performance=performance)
    result = assess(tmp_path, "section-test")
    values = metrics(result)
    assert result["evidence"]["timing_anchors"] == 5
    assert not values["timing"]["available"]
    assert values["timing"]["value"] is None
    assert not values["pace"]["available"]
    assert not values["pitch_shape"]["available"]
    assert values["timing"]["reason"]


def test_legacy_asr_timestamps_are_supported_with_an_explicit_caveat(tmp_path):
    fixture(
        tmp_path, reference=make_words(confidence=False), performance=make_words(confidence=False)
    )
    result = assess(tmp_path, "section-test")
    assert metrics(result)["timing"]["available"]
    assert result["evidence"]["unknown_probability_words"] == 16
    assert any(
        "confidence" in note.lower() and "legacy" in note.lower() for note in result["notes"]
    )


@pytest.mark.parametrize("kind", ["silence", "noise"])
def test_unvoiced_audio_abstains_from_pitch(tmp_path, kind):
    fixture(tmp_path, silent=kind == "silence", noise=kind == "noise")
    result = assess(tmp_path, "section-test")
    for key in ("pitch_shape", "pitch_range"):
        value = metrics(result)[key]
        assert not value["available"]
        assert value["value"] is None
        assert value["reason"]
    json.dumps(result, allow_nan=False)


def test_rendered_and_preview_audio_cannot_improve_raw_measurements(tmp_path, monkeypatch):
    fixture(tmp_path, performance=make_words(step=0.6))
    for name in ("cover.wav", "aligned-performance.wav", "preview-vocal.wav"):
        sf.write(tmp_path / name, audio(make_words()), RATE)
    actual_read = sf.read
    paths = []

    def tracked_read(path, *args, **kwargs):
        paths.append(Path(path).name)
        return actual_read(path, *args, **kwargs)

    monkeypatch.setattr(sf, "read", tracked_read)
    result = assess(tmp_path, "section-test")
    assert metrics(result)["pace"]["value"] == pytest.approx(75)
    assert set(paths) == {"performance.wav", "reference.wav"}


def test_unknown_candidate_is_not_silently_assessed(tmp_path):
    fixture(tmp_path)
    with pytest.raises(ValueError, match="section"):
        assess(tmp_path, "different-section")


def test_transcripts_are_scoped_to_candidate_and_unmatched_words_count(tmp_path):
    data = fixture(tmp_path)
    data["reference"].insert(0, {"text": "outside", "start": 0.01, "end": 0.1})
    data["matches"][0]["pairs"] = [[i, i + 1] for i in range(8)]
    last_end = data["performance"][-1]["end"]
    data["performance"].append({"text": "extra", "start": last_end + 0.05, "end": last_end + 0.15})
    (tmp_path / "analysis.json").write_text(json.dumps(data))
    result = assess(tmp_path, "section-test")
    assert result["reference_text"] == " ".join(TEXT)
    assert result["performance_text"].endswith(" extra")
    assert metrics(result)["lyric_agreement"]["value"] == pytest.approx(8 / 9 * 100)
    assert result["evidence"]["timing_anchors"] == 8


def test_a_short_burst_does_not_have_a_reliable_pace_score(tmp_path):
    fixture(tmp_path, reference=make_words(step=0.15), performance=make_words(step=0.15))
    result = metrics(assess(tmp_path, "section-test"))
    assert result["timing"]["value"] is None
    assert result["pace"]["value"] is None
    assert "2 seconds" in result["pace"]["reason"]


def test_pitch_crowding_tracking_limit_is_explicitly_unavailable(tmp_path):
    fixture(tmp_path, notes=np.full(8, 94.75))
    result = assess(tmp_path, "section-test")
    assert result["evidence"]["performance_pitch"]["boundary_fraction"] > 0.1
    assert metrics(result)["pitch_range"]["value"] is None
    assert "limits" in metrics(result)["pitch_range"]["reason"]


def test_assessment_is_deterministic(tmp_path):
    fixture(tmp_path)
    first = assess(tmp_path, "section-test")
    assert first == assess(tmp_path, "section-test")


def test_waveform_phase_does_not_change_pitch_movement(tmp_path, identity):
    fixture(tmp_path, phase=np.pi / 2)
    result = metrics(assess(tmp_path, "section-test"))
    assert result["pitch_shape"]["value"] < 0.3
    assert result["pitch_range"]["value"] == pytest.approx(
        metrics(identity)["pitch_range"]["value"], abs=0.3
    )


def test_octave_transitions_are_disclosed_without_silently_correcting_them(tmp_path):
    fixture(tmp_path, notes=np.tile([50.0, 62.0], 4))
    result = assess(tmp_path, "section-test")
    assert result["evidence"]["performance_pitch"]["possible_octave_jumps"] > 0
    assert metrics(result)["pitch_range"]["value"] == pytest.approx(12, abs=0.4)
    assert any("musical jumps or tracking errors" in note for note in result["notes"])
