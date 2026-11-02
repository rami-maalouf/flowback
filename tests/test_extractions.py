"""standalone extraction lifecycle through the same local http API as the app."""

import json
import time

import pytest
from test_server import FakeEngine, await_phase, client_for, upload, wav_bytes

from cover_engine import pipeline


class ExtractEngine(FakeEngine):
    def __init__(self):
        super().__init__()
        self.extracted = 0
        self.extract_failure = ""

    def extract(self, song, work_dir, progress):
        self.extracted += 1
        assert song.is_file()
        progress("separating", "Separating your song")
        if self.extract_failure:
            raise RuntimeError(self.extract_failure)
        for name in ("instrumental", "vocals"):
            (work_dir / f"{name}.wav").write_bytes(wav_bytes())


def wait_extraction(client, extraction_id, phase="ready"):
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        response = client.get(f"/extractions/{extraction_id}")
        assert response.status_code == 200, response.text
        item = response.json()
        if item["phase"] == phase:
            return item
        if item["phase"] == "failed":
            pytest.fail(item["error"])
        time.sleep(0.01)
    pytest.fail(f"extraction never reached {phase}: {item}")


def start_extraction(client, song):
    response = client.post("/extractions", json={"song_id": song["id"], "title": "My song"})
    assert response.status_code == 202, response.text
    return response.json()["id"]


def test_extract_only_play_download_restart_and_optionally_make_cover(tmp_path):
    engine = ExtractEngine()
    with client_for(tmp_path, engine) as client:
        song = upload(client, "original.wav")
        extraction_id = start_extraction(client, song)
        item = wait_extraction(client, extraction_id)
        assert item["song_id"] == song["id"]
        assert item["song_name"] == "original.wav"
        assert item["duration"] == pytest.approx(0.1)
        assert item["title"] == "My song"
        assert client.get("/extractions").json() == {"extractions": [item]}
        assert client.get("/songs").json() == {"songs": []}
        assert engine.analyzed == 0
        assert start_extraction(client, song) == extraction_id
        for kind in ("instrumental", "vocals"):
            path = f"/extractions/{extraction_id}/audio/{kind}.wav"
            response = client.get(path)
            assert response.status_code == 200
            assert response.content == wav_bytes()
            chunk = client.get(path, headers={"Range": "bytes=100-199"})
            assert chunk.status_code == 206
            assert chunk.content == wav_bytes()[100:200]
        assert client.get(f"/extractions/{extraction_id}/audio/unknown.wav").status_code == 404
        assert client.post(f"/extractions/{extraction_id}/retry").status_code == 409
    with client_for(tmp_path, engine) as client:
        assert client.get(f"/extractions/{extraction_id}").json() == item
        assert engine.extracted == 1
        voice = upload(client)
        response = client.post(
            f"/songs/{song['id']}/takes", json={"vocal_id": voice["id"], "name": "First take"}
        )
        assert response.status_code == 202, response.text
        job = response.json()
        await_phase(client, job["id"], "ready")
        assert client.get(f"/jobs/{job['id']}/session").json()["title"] == "My song"
        assert client.get(f"/extractions/{extraction_id}").json() == item


def test_failed_extraction_retries_without_new_library_entry(tmp_path):
    engine = ExtractEngine()
    engine.extract_failure = "Model unavailable"
    with client_for(tmp_path, engine) as client:
        extraction_id = start_extraction(client, upload(client))
        assert wait_extraction(client, extraction_id, "failed")["error"] == "Model unavailable"
        assert client.get(f"/extractions/{extraction_id}/audio/vocals.wav").status_code == 409
        engine.extract_failure = ""
        assert client.post(f"/extractions/{extraction_id}/retry").status_code == 202
        wait_extraction(client, extraction_id)
        assert len(client.get("/extractions").json()["extractions"]) == 1
        assert engine.extracted == 2
        assert client.post("/extractions", json={"song_id": "../missing"}).status_code == 404
        assert client.get("/extractions/missing").status_code == 404


@pytest.mark.parametrize("damage", ["interrupted", "missing", "invalid"])
def test_restart_extraction_recovery_is_retryable(tmp_path, damage):
    engine = ExtractEngine()
    with client_for(tmp_path, engine) as client:
        extraction_id = start_extraction(client, upload(client))
        wait_extraction(client, extraction_id)
    directory = tmp_path / "extractions" / extraction_id
    if damage == "interrupted":
        path = directory / "extraction.json"
        item = json.loads(path.read_text())
        item["phase"] = "separating"
        path.write_text(json.dumps(item))
    elif damage == "missing":
        (directory / "work" / "vocals.wav").unlink()
    else:
        (directory / "work" / "vocals.wav").write_bytes(b"broken")
    with client_for(tmp_path, engine) as client:
        item = client.get(f"/extractions/{extraction_id}").json()
        assert item["phase"] == "failed"
        assert client.post(f"/extractions/{extraction_id}/retry").status_code == 202
        wait_extraction(client, extraction_id)


def test_restart_separates_tracks_from_an_older_separator_again(tmp_path, monkeypatch):
    engine = ExtractEngine()
    with client_for(tmp_path, engine) as client:
        extraction_id = start_extraction(client, upload(client))
        item = wait_extraction(client, extraction_id)
        # the app's data module answers an exact shape, so a new field would break its reads.
        assert set(item) == {
            "id",
            "song_id",
            "title",
            "song_name",
            "duration",
            "created_at",
            "phase",
            "message",
            "error",
        }
        assert client.get("/extractions").json() == {"extractions": [item]}
    path = tmp_path / "extractions" / extraction_id / "extraction.json"

    def saved():
        return json.loads(path.read_text())

    assert saved()["separator"] == pipeline.SEPARATOR_MODEL
    legacy = saved()
    del legacy["separator"]
    path.write_text(json.dumps(legacy))
    with client_for(tmp_path, engine) as client:
        wait_extraction(client, extraction_id)
        assert saved()["separator"] == pipeline.SEPARATOR_MODEL
        assert engine.extracted == 2
    monkeypatch.setenv("EXACT_COVER_SEPARATOR_MODEL", "another-model.ckpt")
    with client_for(tmp_path, engine) as client:
        wait_extraction(client, extraction_id)
        assert saved()["separator"] == "another-model.ckpt"
        assert engine.extracted == 3
    with client_for(tmp_path, engine) as client:
        wait_extraction(client, extraction_id)
        assert engine.extracted == 3


def test_extraction_does_not_transcribe_and_cover_reuses_separated_audio(tmp_path, monkeypatch):
    import numpy as np
    import soundfile as sf

    from cover_engine.models import Word

    song = tmp_path / "song.wav"
    vocal = tmp_path / "vocal.wav"
    sf.write(song, np.zeros((44100 * 4, 2)), 44100)
    sf.write(vocal, np.zeros((44100 * 4, 2)), 44100)
    separated = []
    transcribed = []

    def separate(audio, directory):
        separated.append(audio)
        outputs = directory / "separated-vocals.wav", directory / "separated-instrumental.wav"
        for output in outputs:
            output.write_bytes(audio.read_bytes())
        return outputs

    def transcribe(model, audio):
        transcribed.append(audio)
        return [
            Word(word, i * 0.5, i * 0.5 + 0.4)
            for i, word in enumerate(
                ["we", "find", "the", "light", "and", "keep", "moving", "forward"]
            )
        ]

    monkeypatch.setattr(pipeline, "_separate", separate)
    monkeypatch.setattr(pipeline, "_whisper", lambda *args: None)
    monkeypatch.setattr(pipeline, "_model_directory", lambda: tmp_path)
    monkeypatch.setattr(pipeline, "_transcribe", transcribe)
    cache = tmp_path / "stems"
    pipeline.extract(song, tmp_path / "extract", lambda *_: None, stem_cache=cache)
    assert len(separated) == 1
    assert transcribed == []
    pipeline.analyze(song, vocal, tmp_path / "cover", lambda *_: None, stem_cache=cache)
    assert len(separated) == 1
    assert len(transcribed) == 2
    pipeline.extract(song, tmp_path / "cached", lambda *_: None, stem_cache=cache)
    assert len(separated) == 1
    for name in ("extract", "cached"):
        tracks = sorted(path.name for path in (tmp_path / name).iterdir())
        assert tracks == ["instrumental.wav", "vocals.wav"]
        assert sf.info(tmp_path / name / "vocals.wav").frames == 44100 * 4


def test_invalid_separator_output_never_becomes_playable(tmp_path):
    class InvalidEngine(ExtractEngine):
        def extract(self, song, work_dir, progress):
            (work_dir / "instrumental.wav").write_bytes(wav_bytes())
            (work_dir / "vocals.wav").write_bytes(b"not audio")

    with client_for(tmp_path, InvalidEngine()) as client:
        extraction_id = start_extraction(client, upload(client))
        wait_extraction(client, extraction_id, "failed")
        assert client.get(f"/extractions/{extraction_id}/audio/instrumental.wav").status_code == 409


def test_extensible_wav_tracks_are_valid(tmp_path):
    # ffmpeg 6 writes 32-bit float as WAVE_FORMAT_EXTENSIBLE, which soundfile names WAVEX.
    import numpy as np
    import soundfile as sf

    class ExtensibleEngine(ExtractEngine):
        def extract(self, song, work_dir, progress):
            for name in ("instrumental", "vocals"):
                path = work_dir / f"{name}.wav"
                sf.write(path, np.zeros((800, 2)), 8000, subtype="FLOAT", format="WAVEX")
                assert sf.info(path).format == "WAVEX"

    with client_for(tmp_path, ExtensibleEngine()) as client:
        extraction_id = start_extraction(client, upload(client))
        assert wait_extraction(client, extraction_id)["duration"] == pytest.approx(0.1)
        assert client.get(f"/extractions/{extraction_id}/audio/vocals.wav").status_code == 200


def test_extraction_health_does_not_require_transcription(tmp_path, monkeypatch):
    from cover_engine import server

    monkeypatch.setattr(
        server.importlib.util,
        "find_spec",
        lambda name: None if name == "faster_whisper" else object(),
    )
    with client_for(tmp_path, None) as client:
        health = client.get("/health").json()
        assert health["separation_available"] is True
        assert health["models_available"] is False
