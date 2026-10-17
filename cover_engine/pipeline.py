import json
import logging
import os
from dataclasses import asdict
from functools import lru_cache
from pathlib import Path

import soundfile as sf

from .alignment import Phrase, match_lyrics, phrase_plan, token
from .audio import SAMPLE_RATE, decode, mix_section
from .models import Candidate, Progress, RenderResult, Word


def _model_directory() -> Path:
    directory = Path(os.environ.get("EXACT_COVER_MODEL_DIR", "~/.cache/exact-cover/models"))
    directory = directory.expanduser()
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def _separate(song: Path, work_dir: Path) -> tuple[Path, Path]:
    try:
        from audio_separator.separator import Separator
    except ImportError as error:
        raise RuntimeError(
            "Separation dependencies are missing. Run uv sync --extra models "
            "in exact-cover, restart the service, and retry."
        ) from error
    separator = Separator(
        output_dir=str(work_dir),
        output_format="WAV",
        model_file_dir=str(_model_directory() / "separator"),
        sample_rate=SAMPLE_RATE,
        log_level=logging.WARNING,
    )
    model_name = os.environ.get("EXACT_COVER_SEPARATOR_MODEL", "UVR_MDXNET_KARA_2.onnx")
    separator.load_model(model_filename=model_name)
    separator.separate(
        str(song), {"Vocals": "separated-vocals", "Instrumental": "separated-instrumental"}
    )
    vocals = work_dir / "separated-vocals.wav"
    instrumental = work_dir / "separated-instrumental.wav"
    if not vocals.is_file() or not instrumental.is_file():
        raise RuntimeError(
            "The separation model did not produce Vocals and Instrumental stems. "
            "Set EXACT_COVER_SEPARATOR_MODEL=UVR_MDXNET_KARA_2.onnx and retry."
        )
    return vocals, instrumental


@lru_cache(maxsize=1)
def _whisper(model_name: str, device: str, compute: str, directory: str):
    try:
        from faster_whisper import WhisperModel
    except ImportError as error:
        raise RuntimeError(
            "Transcription dependencies are missing. Run uv sync --extra models "
            "in exact-cover, restart the service, and retry."
        ) from error
    return WhisperModel(model_name, device=device, compute_type=compute, download_root=directory)


def _transcribe(model, audio: Path) -> list[Word]:
    duration = sf.info(audio).duration
    segments, _ = model.transcribe(
        str(audio),
        word_timestamps=True,
        beam_size=5,
        condition_on_previous_text=False,
        vad_filter=False,
    )
    words = []
    for segment in segments:
        for word in segment.words or []:
            start = max(0.0, float(word.start))
            end = min(duration, float(word.end))
            text = word.word.strip()
            if token(text) and end > start and (not words or start >= words[-1].start):
                words.append(Word(text, start, end))
    return words


def analyze(song: Path, vocal: Path, work_dir: Path, progress: Progress) -> list[Candidate]:
    work_dir = work_dir.resolve()
    work_dir.mkdir(parents=True, exist_ok=True)
    if song.resolve().parent == work_dir or vocal.resolve().parent == work_dir:
        raise ValueError("Input files must be outside the job's private work directory.")
    phase = "separating"
    try:
        progress(
            phase,
            "Preparing audio and separating the original vocal. "
            "First use may download model weights.",
        )
        decode(song, work_dir / "song.wav")
        decode(vocal, work_dir / "performance.wav")
        original_vocal, instrumental = _separate(work_dir / "song.wav", work_dir)
        decode(original_vocal, work_dir / "reference.wav")
        decode(instrumental, work_dir / "backing.wav")

        phase = "transcribing"
        progress(
            phase,
            "Reading the original lyrics and your performance. "
            "First use may download transcription weights.",
        )
        model = _whisper(
            os.environ.get("EXACT_COVER_WHISPER_MODEL", "small"),
            os.environ.get("EXACT_COVER_WHISPER_DEVICE", "cpu"),
            os.environ.get("EXACT_COVER_WHISPER_COMPUTE", "int8"),
            str(_model_directory() / "whisper"),
        )
        reference = _transcribe(model, work_dir / "reference.wav")
        performance = _transcribe(model, work_dir / "performance.wav")
        phase = "matching"
        progress(phase, "Finding the matching section and checking for repeated lyrics.")
        matches = match_lyrics(reference, performance)
        data = {
            "reference": [asdict(word) for word in reference],
            "performance": [asdict(word) for word in performance],
            "matches": [
                {
                    "candidate": asdict(match.candidate),
                    "pairs": match.pairs,
                    "phrases": [
                        asdict(phrase)
                        for phrase in phrase_plan(reference, performance, match.pairs)
                    ],
                }
                for match in matches
            ],
        }
        (work_dir / "analysis.json").write_text(json.dumps(data, indent=2), encoding="utf-8")
        return [match.candidate for match in matches]
    except Exception as error:
        hint = (
            " Check the model setting and internet connection for first-use downloads."
            if phase in {"separating", "transcribing"}
            else ""
        )
        raise RuntimeError(f"{phase.capitalize()} failed: {error}.{hint}") from error


def render(work_dir: Path, candidate_id: str, progress: Progress) -> RenderResult:
    progress("rendering", "Aligning phrases without changing pitch and mixing your section.")
    try:
        data = json.loads((work_dir / "analysis.json").read_text(encoding="utf-8"))
        match = next((m for m in data["matches"] if m["candidate"]["id"] == candidate_id), None)
        if match is None:
            raise ValueError(
                "That section does not belong to this analysis. Choose a listed section."
            )
        audio = []
        for name in ("song.wav", "backing.wav", "performance.wav"):
            samples, rate = sf.read(work_dir / name, dtype="float32", always_2d=True)
            if rate != SAMPLE_RATE:
                raise ValueError("Saved audio sample rates differ. Analyze the files again.")
            audio.append(samples)
        phrases = [Phrase(**phrase) for phrase in match["phrases"]]
        return mix_section(*audio, SAMPLE_RATE, phrases, work_dir)
    except Exception as error:
        raise RuntimeError(f"Rendering failed: {error}") from error
