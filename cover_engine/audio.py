import math
import shutil
import subprocess
from itertools import pairwise
from pathlib import Path

import numpy as np
import soundfile as sf

from .alignment import Phrase
from .models import RenderResult

SAMPLE_RATE = 44100
# bump when rendering changes how a take sounds; saved mixes from older versions are redone.
# 2: stretched vocal segments are crossfaded instead of hard-cut.
# 3: wsola replaces the rubber band phase vocoder, which made voices sound robotic.
MIX_VERSION = 3
# seconds per wsola window, and how far it may shift a window to keep the waveform continuous.
STRETCH_WINDOW = 0.025
STRETCH_TOLERANCE = 0.006
# seconds of crossfade on each side of a join between stretched segments.
SEAM = 0.01
# seconds of neighboring audio the stretcher sees on each side of a segment.
CONTEXT = 0.1


def decode(source: Path, destination: Path) -> None:
    if source.resolve() == destination.resolve():
        raise ValueError("Decoded audio must have a separate output path.")
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("Install ffmpeg (brew install ffmpeg), then retry this job.")
    result = subprocess.run(
        [
            ffmpeg,
            "-nostdin",
            "-v",
            "error",
            "-y",
            "-i",
            str(source),
            "-vn",
            "-ar",
            str(SAMPLE_RATE),
            "-ac",
            "2",
            "-c:a",
            "pcm_f32le",
            str(destination),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode:
        raise ValueError(
            "Could not decode the audio. Supply a playable WAV, MP3, M4A or FLAC file. "
            + result.stderr[-500:].strip()
        )


def stretch_phrase(audio: np.ndarray, rate: int, duration: float) -> np.ndarray:
    frames = round(duration * rate)
    if not len(audio) or frames <= 0:
        raise ValueError("The detected phrase has empty audio or invalid timing.")
    speed = len(audio) / frames
    if not 0.5 <= speed <= 2:
        raise ValueError(
            "The performance timing differs too much from this section. "
            "Choose another section or supply a take closer to the original flow."
        )
    if frames == len(audio):
        return audio.copy()
    return _wsola(audio, frames, rate)


def _wsola(audio, frames, rate):
    """time-scale audio to frames by overlap-adding pieces of the real waveform (wsola).

    a phase vocoder rebuilds every sample from its spectrum, which makes a voice sound robotic
    even for a 1% change. wsola copies short windows of the take and repeats or skips a few
    milliseconds where the waveforms line up, so the voice keeps its own sound.
    """
    size = round(STRETCH_WINDOW * rate) // 2 * 2
    hop = size // 2
    tolerance = round(STRETCH_TOLERANCE * rate)
    window = (0.5 - 0.5 * np.cos(2 * np.pi * np.arange(size) / size)).astype(np.float32)
    step = hop * len(audio) / frames
    pad = size + tolerance
    padded = np.pad(audio, ((pad, pad + 3 * size + 2 * tolerance), (0, 0)))
    mono = padded.mean(axis=1)
    count = math.ceil(frames / hop) + 2
    output = np.zeros(((count + 1) * hop + size, audio.shape[1]), dtype=np.float32)
    weight = np.zeros(len(output), dtype=np.float32)
    fft = 1 << (size + 2 * tolerance - 1).bit_length()
    delta = 0
    for index in range(count):
        # window centers sit at their nominal input position, so output time t maps to t * speed.
        position = pad + round(index * step) - hop + delta
        output[index * hop : index * hop + size] += (
            padded[position : position + size] * window[:, None]
        )
        weight[index * hop : index * hop + size] += window
        # the next window is the candidate that best continues the waveform just placed.
        natural = mono[position + hop : position + hop + size]
        nominal = pad + round((index + 1) * step) - hop
        region = mono[nominal - tolerance : nominal + tolerance + size]
        if len(region) < size + 2 * tolerance or not np.any(natural):
            delta = 0
            continue
        delta = (
            int(
                np.argmax(
                    np.fft.irfft(
                        np.fft.rfft(region, fft) * np.conj(np.fft.rfft(natural, fft)), fft
                    )[: 2 * tolerance + 1]
                )
            )
            - tolerance
        )
    weight[weight < 1e-3] = 1
    return (output / weight[:, None])[hop : hop + frames].astype(audio.dtype)


def _segment(vocal, rate, a, b, frames, seam_in, seam_out):
    """stretch vocal[a:b] to frames, with seam_in/seam_out extra frames weighted by crossfade ramps.

    the stretcher runs on surrounding context too, so its start-up and tail artifacts land
    outside the kept audio, and neighbors overlap on real audio instead of a hard cut.
    """
    speed = (b - a) / frames
    context = round(CONTEXT * rate)
    before = min(context, a)
    after = min(context, len(vocal) - b)
    total = before + (b - a) + after
    stretched = stretch_phrase(vocal[a - before : b + after], rate, total / speed / rate)
    start = round(before / speed) - seam_in
    piece = np.zeros((seam_in + frames + seam_out, vocal.shape[1]), dtype=vocal.dtype)
    lo, hi = max(0, start), min(len(stretched), start + len(piece))
    if hi > lo:
        piece[lo - start : hi - start] = stretched[lo:hi]
    if seam_in:
        piece[: 2 * seam_in] *= _ramp(2 * seam_in)
    if seam_out:
        piece[-2 * seam_out :] *= _ramp(2 * seam_out)[::-1]
    return piece


def _ramp(frames):
    return ((np.arange(frames, dtype=np.float32) + 0.5) / frames)[:, None]


def _fade(audio: np.ndarray, frames: int) -> None:
    frames = min(frames, len(audio) // 2)
    if frames:
        ramp = np.linspace(0, 1, frames, dtype=np.float32)[:, None]
        audio[:frames] *= ramp
        audio[-frames:] *= ramp[::-1]


def mix_section(
    song: np.ndarray,
    backing: np.ndarray,
    vocal: np.ndarray,
    rate: int,
    phrases: list[Phrase],
    directory: Path,
    *,
    vocal_volume: float = 100,
    backing_volume: float = 100,
) -> RenderResult:
    if not all(math.isfinite(v) and 0 <= v <= 200 for v in (vocal_volume, backing_volume)):
        raise ValueError("Volume must be between 0% and 200%.")
    if not phrases:
        raise ValueError("No phrases were aligned. Analyze the files again.")
    channels = song.shape[1]
    if backing.shape[1] != channels or vocal.shape[1] != channels:
        raise ValueError("Audio channel counts differ. Analyze the files again.")
    start_frame = max(0, round((phrases[0].target_start - 0.25) * rate))
    end_frame = min(len(song), len(backing), round((phrases[-1].target_end + 0.5) * rate))
    if end_frame <= start_frame:
        raise ValueError("The matched section falls outside the original song.")
    instrumental = backing[start_frame:end_frame].copy()
    aligned = np.zeros_like(instrumental)
    for phrase in phrases:
        a, b = round(phrase.source_start * rate), round(phrase.source_end * rate)
        if a < 0 or b > len(vocal) or b <= a:
            raise ValueError("Word timestamps exceed the supplied vocal. Analyze the files again.")
    leading = min(
        round(0.25 * rate),
        round(phrases[0].source_start * rate),
        round(phrases[0].target_start * rate) - start_frame,
    )
    trailing = min(
        round(0.5 * rate),
        len(vocal) - round(phrases[-1].source_end * rate),
        end_frame - round(phrases[-1].target_end * rate),
    )
    boundaries = [
        (
            round(phrases[0].source_start * rate) - max(0, leading),
            round(phrases[0].target_start * rate) - max(0, leading),
        )
    ]
    boundaries.extend(
        (
            round((left.source_end + right.source_start) * rate / 2),
            round((left.target_end + right.target_start) * rate / 2),
        )
        for left, right in pairwise(phrases)
    )
    boundaries.append(
        (
            round(phrases[-1].source_end * rate) + max(0, trailing),
            round(phrases[-1].target_end * rate) + max(0, trailing),
        )
    )
    segments = []
    for phrase, (left, right) in zip(phrases, pairwise(boundaries)):
        anchors = [left]
        for point in (
            (round(phrase.source_start * rate), round(phrase.target_start * rate)),
            (round(phrase.source_end * rate), round(phrase.target_end * rate)),
            right,
        ):
            if point != anchors[-1]:
                anchors.append(point)
        feasible = all(
            b > a
            and target_end > target_start
            and 0.5 <= (b - a) / (target_end - target_start) <= 2
            for (a, target_start), (b, target_end) in pairwise(anchors)
        )
        # preserve audio across impossible or extreme pauses using the continuous span.
        segments.extend(pairwise(anchors) if feasible else [(left, right)])
    for (a, target_start), (b, target_end) in segments:
        if b <= a or target_end <= target_start:
            raise ValueError("Phrase timing is out of order. Analyze the files again.")
    # each seam is crossfaded over the same frames by both neighbors, so the weights sum to one.
    lengths = [target_end - target_start for (_, target_start), (_, target_end) in segments]
    seams = [
        0,
        *(min(round(SEAM * rate), left // 2, right // 2) for left, right in pairwise(lengths)),
        0,
    ]
    changed = False
    for ((a, target_start), (b, target_end)), seam_in, seam_out in zip(segments, seams, seams[1:]):
        duration = (target_end - target_start) / rate
        changed |= abs(duration - (b - a) / rate) > 0.05
        piece = _segment(vocal, rate, a, b, target_end - target_start, seam_in, seam_out)
        offset = target_start - seam_in - start_frame
        source_offset = max(0, -offset)
        target_offset = max(0, offset)
        count = min(len(piece) - source_offset, len(aligned) - target_offset)
        if count > 0:
            aligned[target_offset : target_offset + count] += piece[
                source_offset : source_offset + count
            ]
    active = np.max(np.abs(aligned), axis=1) > 0.001
    if not np.any(active):
        raise ValueError("The aligned vocal is silent. Supply an audible vocal performance.")
    vocal_rms = float(np.sqrt(np.mean(aligned[active] ** 2)))
    music_rms = float(np.sqrt(np.mean(instrumental[active] ** 2)))
    gain = min(8.0, max(0.15, max(0.06, music_rms * 0.9) / vocal_rms))
    aligned *= gain
    _fade(aligned, round(rate * 0.01))
    _fade(instrumental, round(rate * 0.01))
    sf.write(directory / "preview-vocal.wav", aligned, rate, subtype="FLOAT")
    sf.write(directory / "preview-backing.wav", instrumental, rate, subtype="FLOAT")
    mixed = mix_stems(aligned, instrumental, vocal_volume, backing_volume)
    sf.write(directory / "mix.wav", mixed, rate, subtype="PCM_24")
    sf.write(directory / "original.wav", song[start_frame:end_frame], rate, subtype="PCM_24")
    warning = "Separation can leave traces of the original vocal."
    if changed:
        warning += " Phrase timing was adjusted; listen for stretching artifacts."
    return RenderResult(
        start_frame / rate, end_frame / rate, (end_frame - start_frame) / rate, warning
    )


def mix_stems(vocal, backing, vocal_volume, backing_volume):
    if not all(math.isfinite(v) and 0 <= v <= 200 for v in (vocal_volume, backing_volume)):
        raise ValueError("Volume must be between 0% and 200%.")
    if vocal.shape != backing.shape or not vocal.size:
        raise ValueError("The aligned tracks must have the same length and channels.")
    voice = vocal_volume / 100
    music = backing_volume / 100
    bound = float(np.max(np.abs(vocal))) * voice + float(np.max(np.abs(backing))) * music
    scale = min(1, 0.98 / bound) if bound else 1
    return vocal * (voice * scale) + backing * (music * scale)
