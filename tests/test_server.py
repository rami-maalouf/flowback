"""http lifecycle tests with a fake engine, not evidence of musical accuracy."""

import io
import json
import time
import wave
from threading import Event

import pytest
from fastapi.testclient import TestClient

from cover_engine.models import Candidate, RenderResult
from cover_engine.server import create_app

SNAPSHOT_KEYS = {
    "id",
    "phase",
    "message",
    "error",
    "candidates",
    "selected_id",
    "start",
    "end",
    "duration",
    "warning",
}


def wav_bytes():
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(8000)
        audio.writeframes(b"\x00\x00" * 800)
    return buffer.getvalue()


class FakeEngine:
    def __init__(self, candidates=None, failure=None):
        self.candidates = (
            candidates
            if candidates is not None
            else [Candidate("first", "Verse", 10, 20, 0.95, 0.9, "some matching words")]
        )
        self.failure = failure
        self.rendered = []

    def analyze(self, song, vocal, work_dir, progress):
        assert song.is_file() and vocal.is_file()
        progress("matching", "Finding your section")
        if self.failure:
            raise RuntimeError(self.failure)
        return self.candidates

    def render(self, work_dir, candidate_id, progress):
        self.rendered.append(candidate_id)
        (work_dir / "mix.wav").write_bytes(wav_bytes())
        (work_dir / "original.wav").write_bytes(wav_bytes())
        return RenderResult(9, 21, 12, "Timing adjusted")


def client_for(tmp_path, engine):
    return TestClient(create_app(tmp_path, engine=engine), base_url="http://127.0.0.1:8891")


def upload(client, name="voice.wav"):
    response = client.post("/uploads", content=wav_bytes(), headers={"X-File-Name": name})
    assert response.status_code == 201, response.text
    return response.json()


def start_job(client):
    song, vocal = upload(client), upload(client)
    response = client.post("/jobs", json={"song_id": song["id"], "vocal_id": vocal["id"]})
    assert response.status_code == 202, response.text
    assert set(response.json()) == SNAPSHOT_KEYS
    return response.json()["id"]


def await_phase(client, job_id, phase):
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        response = client.get(f"/jobs/{job_id}")
        assert response.status_code == 200
        snapshot = response.json()
        if snapshot["phase"] == phase:
            return snapshot
        if snapshot["phase"] == "failed":
            pytest.fail(snapshot["error"])
        time.sleep(0.01)
    pytest.fail(f"job never reached {phase}: {snapshot}")


def test_upload_auto_render_and_completed_job_survive_restart(tmp_path):
    engine = FakeEngine()
    with client_for(tmp_path, engine) as client:
        audio = upload(client, "my%20voice.wav")
        assert audio["name"] == "my voice.wav"
        assert audio["duration"] == pytest.approx(0.1)
        job_id = start_job(client)
        snapshot = await_phase(client, job_id, "ready")
        assert snapshot["selected_id"] == "first"
        assert snapshot["duration"] == 12
        assert snapshot["warning"] == "Timing adjusted"
        for output in ("mix", "original"):
            response = client.get(f"/jobs/{job_id}/{output}.wav")
            assert response.status_code == 200
            assert response.content == wav_bytes()
    with client_for(tmp_path, FakeEngine()) as client:
        assert client.get(f"/jobs/{job_id}").json() == snapshot
        assert client.get(f"/jobs/{job_id}/mix.wav").status_code == 200
        response = client.post("/jobs", json={"song_id": audio["id"], "vocal_id": audio["id"]})
        assert response.status_code == 202


def test_ambiguous_match_requires_valid_choice_and_allows_reselection(tmp_path):
    engine = FakeEngine(
        [
            Candidate("one", "Chorus 1", 10, 20, 0.9, 0.91, "repeat words"),
            Candidate("two", "Chorus 2", 30, 40, 0.9, 0.90, "repeat words"),
        ]
    )
    with client_for(tmp_path, engine) as client:
        job_id = start_job(client)
        await_phase(client, job_id, "choose")
        assert engine.rendered == []
        assert client.get(f"/jobs/{job_id}/mix.wav").status_code == 409
        assert (
            client.post(f"/jobs/{job_id}/choose", json={"candidate_id": "bad"}).status_code == 422
        )
        assert (
            client.post(f"/jobs/{job_id}/choose", json={"candidate_id": "two"}).status_code == 202
        )
        assert await_phase(client, job_id, "ready")["selected_id"] == "two"
        assert (
            client.post(f"/jobs/{job_id}/choose", json={"candidate_id": "one"}).status_code == 202
        )
        assert await_phase(client, job_id, "ready")["selected_id"] == "one"
    assert engine.rendered == ["two", "one"]


def test_low_coverage_never_auto_renders(tmp_path):
    engine = FakeEngine([Candidate("one", "Verse", 0, 10, 0.7, 0.99, "few words")])
    with client_for(tmp_path, engine) as client:
        await_phase(client, start_job(client), "choose")
    assert engine.rendered == []


def test_export_ranges_are_readable_by_browser(tmp_path):
    with client_for(tmp_path, FakeEngine()) as client:
        job_id = start_job(client)
        await_phase(client, job_id, "ready")
        response = client.get(
            f"/jobs/{job_id}/mix.wav",
            headers={"Range": "bytes=100-199", "Origin": "http://localhost:8893"},
        )
        assert response.status_code == 206
        assert response.content == wav_bytes()[100:200]
        assert response.headers["content-range"] == f"bytes 100-199/{len(wav_bytes())}"
        assert "Content-Range" in response.headers["access-control-expose-headers"]


@pytest.mark.parametrize("failure", ["Install the model dependencies", None])
def test_engine_errors_and_empty_evidence_become_failed_jobs(tmp_path, failure):
    with client_for(tmp_path, FakeEngine([], failure)) as client:
        job_id = start_job(client)
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            snapshot = client.get(f"/jobs/{job_id}").json()
            if snapshot["phase"] == "failed":
                break
            time.sleep(0.01)
        assert snapshot["phase"] == "failed"
        assert snapshot["error"]
        assert client.get(f"/jobs/{job_id}/mix.wav").status_code == 409


def test_rejects_bad_audio_unknown_ids_and_oversized_uploads(tmp_path, monkeypatch):
    with client_for(tmp_path, FakeEngine()) as client:
        assert client.post("/uploads", content=b"not audio").status_code == 422
        assert client.post("/uploads", content=b"").status_code == 422
        assert client.get("/jobs/missing").status_code == 404
        assert (
            client.post("/jobs", json={"song_id": "missing", "vocal_id": "missing"}).status_code
            == 404
        )
        monkeypatch.setattr("cover_engine.server.MAX_UPLOAD_BYTES", 8)
        assert client.post("/uploads", content=b"123456789").status_code == 413
        assert client.post("/uploads", content=iter([b"1234", b"56789"])).status_code == 413
    assert list((tmp_path / "uploads").glob("*.audio")) == []


def test_blocks_remote_hosts_origins_and_allows_local_preflight(tmp_path):
    with client_for(tmp_path, FakeEngine()) as client:
        assert client.get("/health").json()["ok"] is True
        for host in (
            "attacker.example",
            "localhost.attacker.example",
            "127.0.0.1.attacker.example",
        ):
            assert client.get("/health", headers={"Host": host}).status_code == 403
        for origin in (
            "https://attacker.example",
            "null",
            "http://localhost.attacker.example",
            "file://",
        ):
            assert (
                client.post("/uploads", content=wav_bytes(), headers={"Origin": origin}).status_code
                == 403
            )
        response = client.options(
            "/uploads",
            headers={
                "Origin": "http://localhost:4400",
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "x-file-name,content-type",
            },
        )
        assert response.status_code == 200
        assert response.headers["access-control-allow-origin"] == "http://localhost:4400"
        response = client.get("/health", headers={"Origin": "http://127.0.0.1:4400"})
        assert response.headers["access-control-allow-origin"] == "http://127.0.0.1:4400"


def test_worker_is_serial_and_rejects_early_selection(tmp_path):
    entered, release = Event(), Event()

    class BlockingEngine(FakeEngine):
        def analyze(self, *args):
            entered.set()
            assert release.wait(3)
            return super().analyze(*args)

    engine = BlockingEngine()
    with client_for(tmp_path, engine) as client:
        first = start_job(client)
        assert entered.wait(1)
        second = start_job(client)
        assert client.get(f"/jobs/{second}").json()["phase"] == "queued"
        assert (
            client.post(f"/jobs/{first}/choose", json={"candidate_id": "first"}).status_code == 409
        )
        release.set()
        await_phase(client, first, "ready")
        await_phase(client, second, "ready")


def test_restart_marks_interrupted_job_failed(tmp_path):
    with client_for(tmp_path, FakeEngine()) as client:
        job_id = start_job(client)
        await_phase(client, job_id, "ready")
    state_file = tmp_path / "jobs" / job_id / "job.json"
    state = json.loads(state_file.read_text())
    state["snapshot"]["phase"] = "rendering"
    state_file.write_text(json.dumps(state))
    with client_for(tmp_path, FakeEngine()) as client:
        snapshot = client.get(f"/jobs/{job_id}").json()
        assert snapshot["phase"] == "failed"
        assert "again" in snapshot["error"].lower() or "retry" in snapshot["error"].lower()
        assert client.get(f"/jobs/{job_id}/mix.wav").status_code == 409


def test_pending_choice_survives_restart(tmp_path):
    engine = FakeEngine(
        [
            Candidate("one", "Chorus 1", 10, 20, 0.9, 0.9, "repeat words"),
            Candidate("two", "Chorus 2", 30, 40, 0.9, 0.9, "repeat words"),
        ]
    )
    with client_for(tmp_path, engine) as client:
        job_id = start_job(client)
        before = await_phase(client, job_id, "choose")
    restored_engine = FakeEngine()
    with client_for(tmp_path, restored_engine) as client:
        assert client.get(f"/jobs/{job_id}").json() == before
        response = client.post(f"/jobs/{job_id}/choose", json={"candidate_id": "two"})
        assert response.status_code == 202
        await_phase(client, job_id, "ready")
    assert restored_engine.rendered == ["two"]


def test_missing_exports_fail_instead_of_claiming_ready(tmp_path):
    class IncompleteEngine(FakeEngine):
        def render(self, work_dir, candidate_id, progress):
            return RenderResult(0, 1, 1, "")

    with client_for(tmp_path, IncompleteEngine()) as client:
        job_id = start_job(client)
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            snapshot = client.get(f"/jobs/{job_id}").json()
            if snapshot["phase"] == "failed":
                break
            time.sleep(0.01)
        assert snapshot["phase"] == "failed"
        assert "exports" in snapshot["error"]


def test_validation_errors_follow_the_error_contract(tmp_path):
    with client_for(tmp_path, FakeEngine()) as client:
        response = client.post("/jobs", json={})
        assert response.status_code == 422
        assert isinstance(response.json()["detail"], str)
        response = client.post("/uploads", content=b"#EXTM3U\nhttp://example.com/audio.mp3")
        assert response.status_code == 422
