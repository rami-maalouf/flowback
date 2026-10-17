"""prove original preparation reuse while preserving raw per-take evidence."""

import json
from types import SimpleNamespace

import numpy as np
import pytest
import soundfile as sf

from cover_engine import pipeline
from cover_engine.models import Word


@pytest.fixture
def prepared(tmp_path, monkeypatch):
    song, first, second = (tmp_path / name for name in ("song.wav", "one.wav", "two.wav"))
    for index, audio in enumerate((song, first, second)):
        sf.write(audio, np.ones((44100 * 4, 2), dtype=np.float32) * (index + 1) * 0.03, 44100)
    calls = {"separate": 0, "reference": 0, "performance": 0}
    words = [
        Word(word, i * 0.5, i * 0.5 + 0.4)
        for i, word in enumerate(["we", "find", "the", "light", "and", "keep", "moving", "forward"])
    ]

    def separate(audio, directory):
        calls["separate"] += 1
        return audio, audio

    def transcribe(model, audio):
        calls["reference" if audio.name == "reference.wav" else "performance"] += 1
        return words

    monkeypatch.setattr(pipeline, "_separate", separate)
    monkeypatch.setattr(pipeline, "_whisper", lambda *args: None)
    monkeypatch.setattr(pipeline, "_model_directory", lambda: tmp_path)
    monkeypatch.setattr(pipeline, "_transcribe", transcribe)
    return song, first, second, calls


def test_complete_reference_cache_reuses_original_but_transcribes_each_raw_take(tmp_path, prepared):
    song, first, second, calls = prepared
    cache = tmp_path / "reference-cache"
    pipeline.analyze(song, first, tmp_path / "work-one", lambda *_: None, reference_cache=cache)
    pipeline.analyze(song, second, tmp_path / "work-two", lambda *_: None, reference_cache=cache)
    assert calls == {"separate": 1, "reference": 1, "performance": 2}
    assert (tmp_path / "work-one" / "reference.wav").read_bytes() == (
        tmp_path / "work-two" / "reference.wav"
    ).read_bytes()
    assert (tmp_path / "work-one" / "performance.wav").read_bytes() != (
        tmp_path / "work-two" / "performance.wav"
    ).read_bytes()


@pytest.mark.parametrize("damage", ["missing", "corrupt", "transcript", "config", "source"])
def test_partial_stale_or_corrupt_cache_cannot_supply_reference(
    tmp_path, prepared, monkeypatch, damage
):
    song, first, second, calls = prepared
    cache = tmp_path / "reference-cache"
    pipeline.analyze(song, first, tmp_path / "work-one", lambda *_: None, reference_cache=cache)
    directory = next(path for path in cache.iterdir() if path.is_dir())
    if damage == "missing":
        (directory / "backing.wav").unlink()
    elif damage == "corrupt":
        (directory / "reference.wav").write_bytes(b"not a wave")
    elif damage == "transcript":
        (directory / "reference.json").write_text("[]")
    elif damage == "config":
        monkeypatch.setenv("EXACT_COVER_WHISPER_MODEL", "different-model")
    else:
        sf.write(song, np.ones((44100 * 4, 2), dtype=np.float32) * 0.2, 44100)
    pipeline.analyze(song, second, tmp_path / "work-two", lambda *_: None, reference_cache=cache)
    assert calls == {"separate": 2, "reference": 2, "performance": 2}


def test_word_probability_is_optional_and_transcription_preserves_real_values(tmp_path):
    assert Word(text="old", start=0, end=0.5).probability is None
    audio = tmp_path / "audio.wav"
    sf.write(audio, np.zeros(44100), 44100)
    words = [
        SimpleNamespace(word="new", start=0, end=0.2, probability=0.91),
        SimpleNamespace(word="unknown", start=0.3, end=0.5),
        SimpleNamespace(word="invalid", start=0.6, end=0.8, probability=float("nan")),
    ]
    model = SimpleNamespace(transcribe=lambda *a, **k: ([SimpleNamespace(words=words)], None))
    from dataclasses import asdict

    result = pipeline._transcribe(model, audio)
    assert [word.probability for word in result] == [0.91, None, None]
    assert (
        json.loads(json.dumps([asdict(word) for word in result], allow_nan=False))[0]["probability"]
        == 0.91
    )
