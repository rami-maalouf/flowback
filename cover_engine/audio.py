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
    from pedalboard import time_stretch

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
    stretched = time_stretch(
        np.ascontiguousarray(audio.T),
        rate,
        stretch_factor=speed,
        pitch_shift_in_semitones=0,
        preserve_formants=True,
    ).T
    if stretched.ndim == 1:
        stretched = stretched[:, None]
    if len(stretched) < frames:
        stretched = np.pad(stretched, ((0, frames - len(stretched)), (0, 0)))
    return stretched[:frames]


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
    changed = False
    for (a, target_start), (b, target_end) in segments:
        if b <= a or target_end <= target_start:
            raise ValueError("Phrase timing is out of order. Analyze the files again.")
        duration = (target_end - target_start) / rate
        changed |= abs(duration - (b - a) / rate) > 0.05
        stretched = stretch_phrase(vocal[a:b], rate, duration)
        offset = target_start - start_frame
        source_offset = max(0, -offset)
        target_offset = max(0, offset)
        count = min(len(stretched) - source_offset, len(aligned) - target_offset)
        if count > 0:
            aligned[target_offset : target_offset + count] += stretched[
                source_offset : source_offset + count
            ]
    active = np.max(np.abs(aligned), axis=1) > 0.001
    if not np.any(active):
        raise ValueError("The aligned vocal is silent. Supply an audible vocal performance.")
    vocal_rms = float(np.sqrt(np.mean(aligned[active] ** 2)))
    music_rms = float(np.sqrt(np.mean(instrumental[active] ** 2)))
    gain = min(8.0, max(0.15, max(0.06, music_rms * 0.9) / vocal_rms))
    mixed = instrumental * (backing_volume / 100) + aligned * gain * (vocal_volume / 100)
    peak = float(np.max(np.abs(mixed)))
    if peak > 0.98:
        mixed *= 0.98 / peak
    _fade(mixed, round(rate * 0.01))
    sf.write(directory / "mix.wav", mixed, rate, subtype="PCM_24")
    sf.write(directory / "original.wav", song[start_frame:end_frame], rate, subtype="PCM_24")
    warning = "Separation can leave traces of the original vocal."
    if changed:
        warning += " Phrase timing was adjusted; listen for stretching artifacts."
    return RenderResult(
        start_frame / rate, end_frame / rate, (end_frame - start_frame) / rate, warning
    )
