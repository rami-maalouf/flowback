import numpy as np
import pytest
import soundfile as sf

from cover_engine import pipeline
from cover_engine.alignment import Phrase
from cover_engine.audio import mix_section, stretch_phrase
from cover_engine.models import Word


def tone(frequency, seconds, rate=24000):
    t = np.arange(round(seconds * rate)) / rate
    return (0.15 * np.sin(2 * np.pi * frequency * t)).astype(np.float32)[:, None]


def test_stretch_changes_duration_without_pitch_shift():
    stretched = stretch_phrase(tone(440, 2), 24000, 3)
    assert stretched.shape == (72000, 1)
    middle = stretched[12000:-12000, 0]
    frequencies = np.fft.rfftfreq(len(middle), 1 / 24000)
    peak = frequencies[np.argmax(abs(np.fft.rfft(middle)))]
    assert peak == pytest.approx(440, abs=2)


def test_mix_exports_only_matched_section_with_equal_comparison_length(tmp_path):
    rate = 24000
    song = tone(110, 12)
    backing = tone(220, 12)
    vocal = tone(440, 2)
    result = mix_section(song, backing, vocal, rate, [Phrase(0, 2, 5, 8)], tmp_path)
    mixed, mixed_rate = sf.read(tmp_path / "mix.wav", always_2d=True)
    original, _ = sf.read(tmp_path / "original.wav", always_2d=True)
    assert mixed_rate == rate
    assert result.start == 4.75
    assert result.end == 8.5
    assert result.duration == 3.75
    assert mixed.shape == original.shape == (90000, 1)
    assert np.max(abs(mixed)) <= 0.981
    np.testing.assert_allclose(original, song[114000:204000], atol=1e-6)
    assert np.max(abs(mixed[12000:72000] - original[12000:72000])) > 0.05


def test_unusable_stretch_is_refused():
    with pytest.raises(ValueError, match="timing"):
        stretch_phrase(tone(440, 1), 24000, 8)


def test_independent_levels_control_exported_audio_and_allow_muting(tmp_path):
    rate = 24000
    backing = tone(220, 5)
    vocal = tone(440, 2)
    phrases = [Phrase(0, 2, 1, 3)]

    def render(vocal_volume, backing_volume):
        mix_section(
            backing,
            backing,
            vocal,
            rate,
            phrases,
            tmp_path,
            vocal_volume=vocal_volume,
            backing_volume=backing_volume,
        )
        return sf.read(tmp_path / "mix.wav")[0]

    voice = render(100, 0)
    music = render(0, 100)
    assert np.max(abs(voice)) > 0.01
    assert np.max(abs(music)) > 0.01
    np.testing.assert_allclose(render(50, 100), voice * 0.5 + music, atol=4e-7)
    np.testing.assert_allclose(render(100, 25), voice + music * 0.25, atol=4e-7)
    assert not np.any(render(0, 0))
    assert np.max(abs(render(200, 200))) <= 0.981


def test_equal_timing_keeps_vocal_between_phrases_and_at_seams(tmp_path):
    rate = 24000
    vocal = tone(437, 3.4)
    backing = np.zeros((rate * 7, 1), dtype=np.float32)
    phrases = [Phrase(0.2, 1.2, 2.2, 3.2), Phrase(1.6, 3.1, 3.6, 5.1)]
    result = mix_section(backing, backing, vocal, rate, phrases, tmp_path)
    mixed, _ = sf.read(tmp_path / "mix.wav", always_2d=True)
    offset = round((2 - result.start) * rate)
    actual = mixed[offset : offset + len(vocal)]
    gain = np.sum(actual * vocal) / np.sum(vocal**2)
    gap = actual[round(1.2 * rate) : round(1.6 * rate)]
    assert np.sqrt(np.mean(gap**2)) > 0.02
    np.testing.assert_allclose(actual, vocal * gain, atol=2e-6)


def test_stretched_vocal_keeps_audio_in_untranscribed_gap(tmp_path):
    rate = 24000
    vocal = tone(437, 3)
    backing = np.zeros((rate * 7, 1), dtype=np.float32)
    phrases = [Phrase(0, 1, 2, 3.2), Phrase(1.5, 3, 3.8, 5)]
    result = mix_section(backing, backing, vocal, rate, phrases, tmp_path)
    mixed, _ = sf.read(tmp_path / "mix.wav", always_2d=True)
    gap = mixed[round((3.3 - result.start) * rate) : round((3.7 - result.start) * rate)]
    assert np.sqrt(np.mean(gap**2)) > 0.02


def test_analysis_persists_alignment_and_renders_selected_repeat(tmp_path, monkeypatch):
    song = tmp_path / "input-song.wav"
    vocal = tmp_path / "input-vocal.wav"
    sf.write(song, tone(110, 20), 24000)
    sf.write(vocal, tone(440, 4), 24000)
    original_inputs = (song.read_bytes(), vocal.read_bytes())
    lyrics = ["we", "chase", "the", "sunlight", "through", "the", "city", "tonight"]
    performance = [Word(word, i * 0.5, i * 0.5 + 0.4) for i, word in enumerate(lyrics)]
    reference = [
        Word(w.text, w.start + offset, w.end + offset) for offset in (5, 12) for w in performance
    ]
    monkeypatch.setattr(pipeline, "_separate", lambda audio, directory: (audio, audio))
    monkeypatch.setattr(pipeline, "_model_directory", lambda: tmp_path)
    monkeypatch.setattr(pipeline, "_whisper", lambda *args: None)
    monkeypatch.setattr(
        pipeline,
        "_transcribe",
        lambda model, audio: reference if audio.name == "reference.wav" else performance,
    )
    work_dir = tmp_path / "job"
    progress = []
    candidates = pipeline.analyze(song, vocal, work_dir, lambda p, m: progress.append(p))
    assert [c.start for c in candidates] == [5, 12]
    result = pipeline.render(work_dir, candidates[1].id, lambda p, m: progress.append(p))
    assert result.start == 11.75
    assert result.end == pytest.approx(16.4)
    assert progress == ["separating", "transcribing", "matching", "rendering"]
    assert sf.info(work_dir / "mix.wav").duration == pytest.approx(result.duration)
    pipeline.render(
        work_dir, candidates[1].id, lambda *_: None, vocal_volume=0, backing_volume=0
    )
    assert not np.any(sf.read(work_dir / "mix.wav")[0])
    assert (song.read_bytes(), vocal.read_bytes()) == original_inputs
