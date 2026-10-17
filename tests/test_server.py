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
    "vocal_volume",
    "backing_volume",
    "revision",
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
        self.render_volumes = []
        self.analyzed = 0

    def analyze(self, song, vocal, work_dir, progress):
        self.analyzed += 1
        assert song.is_file() and vocal.is_file()
        progress("matching", "Finding your section")
        if self.failure:
            raise RuntimeError(self.failure)
        return self.candidates

    def render(
        self,
        work_dir,
        candidate_id,
        progress,
        *,
        vocal_volume=100,
        backing_volume=100,
        output_dir=None,
    ):
        work_dir = output_dir if output_dir is not None else work_dir
        self.rendered.append(candidate_id)
        self.render_volumes.append((vocal_volume, backing_volume))
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
        assert (snapshot["vocal_volume"], snapshot["backing_volume"], snapshot["revision"]) == (
            100,
            100,
            1,
        )
        for output in ("mix", "original"):
            response = client.get(f"/jobs/{job_id}/{output}.wav")
            assert response.status_code == 200
            assert response.content == wav_bytes()
            assert response.headers["cache-control"] == "no-store"
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
        def render(self, work_dir, candidate_id, progress, **volumes):
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


def test_remix_reuses_analysis_propagates_gains_and_survives_restart(tmp_path):
    engine = FakeEngine()
    with client_for(tmp_path, engine) as client:
        job_id = start_job(client)
        await_phase(client, job_id, "ready")
        response = client.post(
            f"/jobs/{job_id}/mix", json={"vocal_volume": 175, "backing_volume": 40}
        )
        assert response.status_code == 202, response.text
        assert response.json()["phase"] == "queued"
        assert response.json()["revision"] == 1
        snapshot = await_phase(client, job_id, "ready")
        assert snapshot["revision"] == 2
        assert engine.analyzed == 1
        assert engine.render_volumes == [(100, 100), (175, 40)]
    restored_engine = FakeEngine()
    with client_for(tmp_path, restored_engine) as client:
        assert client.get(f"/jobs/{job_id}").json() == snapshot
        response = client.post(f"/jobs/{job_id}/choose", json={"candidate_id": "first"})
        assert response.status_code == 202
        assert await_phase(client, job_id, "ready")["revision"] == 3
        assert restored_engine.analyzed == 0
        assert restored_engine.render_volumes == [(175, 40)]


@pytest.mark.parametrize("missing_export", [False, True])
def test_legacy_jobs_acquire_persisted_volume_defaults(tmp_path, missing_export):
    with client_for(tmp_path, FakeEngine()) as client:
        audio = upload(client)
        job_id = client.post(
            "/jobs", json={"song_id": audio["id"], "vocal_id": audio["id"]}
        ).json()["id"]
        await_phase(client, job_id, "ready")
    state_file = tmp_path / "jobs" / job_id / "job.json"
    state = json.loads(state_file.read_text())
    export_dir = state_file.parent / "exports" / state.pop("output_id")
    for name in ("mix.wav", "original.wav"):
        (export_dir / name).replace(state_file.parent / "work" / name)
    for field in ("vocal_volume", "backing_volume", "revision"):
        state["snapshot"].pop(field, None)
    state_file.write_text(json.dumps(state))
    if missing_export:
        (state_file.parent / "work" / "mix.wav").unlink()
    with client_for(tmp_path, FakeEngine()) as client:
        response = client.get(f"/jobs/{job_id}")
        assert response.status_code == 200
        snapshot = response.json()
        assert snapshot["vocal_volume"] == snapshot["backing_volume"] == 100
        assert snapshot["revision"] == 0
        assert snapshot["phase"] == ("failed" if missing_export else "ready")
        assert json.loads(state_file.read_text())["snapshot"] == snapshot


@pytest.mark.parametrize("value", [-1, 201, "NaN", "Infinity", "-Infinity", None, True, "loud"])
@pytest.mark.parametrize("field", ["vocal_volume", "backing_volume"])
def test_rejects_invalid_mix_volumes(tmp_path, value, field):
    with client_for(tmp_path, FakeEngine()) as client:
        response = client.post(
            "/jobs/missing/mix", json={"vocal_volume": 100, "backing_volume": 100, field: value}
        )
        assert response.status_code == 422
        assert isinstance(response.json()["detail"], str)


@pytest.mark.parametrize("value", ["NaN", "Infinity", "-Infinity"])
def test_rejects_nonfinite_json_mix_volumes(tmp_path, value):
    with client_for(tmp_path, FakeEngine()) as client:
        response = client.post(
            "/jobs/missing/mix",
            content='{"vocal_volume":' + value + ',"backing_volume":100}',
            headers={"Content-Type": "application/json"},
        )
        assert response.status_code == 422
        assert isinstance(response.json()["detail"], str)


def test_remix_rejects_pending_choice_and_active_render(tmp_path):
    entered, release = Event(), Event()

    class BlockingRenderEngine(FakeEngine):
        def render(self, *args, **kwargs):
            if self.rendered:
                entered.set()
                assert release.wait(3)
            return super().render(*args, **kwargs)

    engine = BlockingRenderEngine(
        [Candidate("first", "Verse", 10, 20, 0.7, 0.9, "some matching words")]
    )
    with client_for(tmp_path, engine) as client:
        job_id = start_job(client)
        await_phase(client, job_id, "choose")
        url = f"/jobs/{job_id}/mix"
        assert client.post(url, json={}).status_code == 409
        assert (
            client.post(f"/jobs/{job_id}/choose", json={"candidate_id": "first"}).status_code == 202
        )
        await_phase(client, job_id, "ready")
        try:
            response = client.post(url, json={"vocal_volume": 0, "backing_volume": 200})
            assert response.status_code == 202
            assert entered.wait(1)
            assert client.post(url, json={}).status_code == 409
            assert client.get(f"/jobs/{job_id}/mix.wav").status_code == 409
        finally:
            release.set()
        snapshot = await_phase(client, job_id, "ready")
        assert (snapshot["vocal_volume"], snapshot["backing_volume"]) == (0, 200)
        assert snapshot["revision"] == 2
        assert client.post(url, json={}).status_code == 202
        snapshot = await_phase(client, job_id, "ready")
        assert (snapshot["vocal_volume"], snapshot["backing_volume"]) == (100, 100)
        assert snapshot["revision"] == 3


def test_failed_remix_queue_save_preserves_ready_export_and_retry(tmp_path, monkeypatch):
    app = create_app(tmp_path, engine=FakeEngine())
    with TestClient(app, base_url="http://127.0.0.1:8891", raise_server_exceptions=False) as client:
        job_id = start_job(client)
        before = await_phase(client, job_id, "ready")
        store = app.state.store
        save = store._save

        def fail_save(job_id):
            raise OSError("disk full")

        monkeypatch.setattr(store, "_save", fail_save)
        assert client.post(f"/jobs/{job_id}/mix", json={"vocal_volume": 50}).status_code == 500
        assert client.get(f"/jobs/{job_id}").json() == before
        assert client.get(f"/jobs/{job_id}/mix.wav").content == wav_bytes()
        assert store.pending.empty()
        monkeypatch.setattr(store, "_save", save)
        assert client.post(f"/jobs/{job_id}/mix", json={"vocal_volume": 50}).status_code == 202
        assert await_phase(client, job_id, "ready")["revision"] == 2


@pytest.mark.parametrize("failure_mode", ["exception", "invalid_export"])
def test_failed_partial_remix_preserves_audio_settings_and_restart(tmp_path, failure_mode):
    class PartialEngine(FakeEngine):
        def render(self, work_dir, candidate_id, progress, **options):
            if self.rendered:
                directory = options.get("output_dir", work_dir)
                (directory / "mix.wav").write_bytes(b"partial audio")
                if failure_mode == "exception":
                    raise OSError("interrupted write")
                (directory / "original.wav").write_bytes(wav_bytes())
                return RenderResult(9, 21, 12, "")
            return super().render(work_dir, candidate_id, progress, **options)

    with client_for(tmp_path, PartialEngine()) as client:
        job_id = start_job(client)
        before = await_phase(client, job_id, "ready")
        assert client.post(f"/jobs/{job_id}/mix", json={"vocal_volume": 25}).status_code == 202
        after = await_phase(client, job_id, "ready")
        assert "previous mix is still available" in after["error"]
        for field in ("revision", "vocal_volume", "backing_volume", "selected_id", "duration"):
            assert after[field] == before[field]
        assert client.get(f"/jobs/{job_id}/mix.wav").content == wav_bytes()
        assert client.get(f"/jobs/{job_id}/original.wav").content == wav_bytes()
    with client_for(tmp_path, FakeEngine()) as client:
        assert client.get(f"/jobs/{job_id}").json() == after
        assert client.get(f"/jobs/{job_id}/mix.wav").content == wav_bytes()


def test_remix_keeps_previous_download_path_immutable(tmp_path, monkeypatch):
    from cover_engine import server

    paths = []
    response_class = server.FileResponse

    def capture_response(path, **options):
        paths.append(path)
        return response_class(path, **options)

    class ChangingEngine(FakeEngine):
        def render(self, work_dir, candidate_id, progress, **options):
            result = super().render(work_dir, candidate_id, progress, **options)
            if len(self.rendered) > 1:
                directory = options.get("output_dir", work_dir)
                (directory / "mix.wav").write_bytes(wav_bytes()[:-2] + b"\x01\x00")
            return result

    monkeypatch.setattr(server, "FileResponse", capture_response)
    with client_for(tmp_path, ChangingEngine()) as client:
        job_id = start_job(client)
        await_phase(client, job_id, "ready")
        assert client.get(f"/jobs/{job_id}/mix.wav").content == wav_bytes()
        old_path = paths[-1]
        with old_path.open("rb") as in_flight_download:
            assert in_flight_download.read(100) == wav_bytes()[:100]
            assert client.post(f"/jobs/{job_id}/mix", json={"vocal_volume": 50}).status_code == 202
            assert await_phase(client, job_id, "ready")["revision"] == 2
            assert client.get(f"/jobs/{job_id}/mix.wav").content != wav_bytes()
            assert paths[-1] != old_path
            assert in_flight_download.read() == wav_bytes()[100:]
            assert old_path.read_bytes() == wav_bytes()


def test_interrupted_remix_restores_last_ready_revision_on_restart(tmp_path):
    with client_for(tmp_path, FakeEngine()) as client:
        job_id = start_job(client)
        before = await_phase(client, job_id, "ready")
    state_file = tmp_path / "jobs" / job_id / "job.json"
    state = json.loads(state_file.read_text())
    state["previous_ready"] = before
    state["snapshot"] = {**before, "phase": "rendering", "vocal_volume": 20}
    state_file.write_text(json.dumps(state))
    with client_for(tmp_path, FakeEngine()) as client:
        restored = client.get(f"/jobs/{job_id}").json()
        assert restored["phase"] == "ready"
        assert restored["revision"] == before["revision"]
        assert restored["vocal_volume"] == before["vocal_volume"]
        assert "interrupted" in restored["error"]
        assert client.get(f"/jobs/{job_id}/mix.wav").content == wav_bytes()
        assert "previous_ready" not in json.loads(state_file.read_text())
        assert client.post(f"/jobs/{job_id}/mix", json={}).status_code == 202
        assert await_phase(client, job_id, "ready")["revision"] == 2


def test_failed_remix_publication_keeps_previous_revision(tmp_path, monkeypatch):
    app = create_app(tmp_path, engine=FakeEngine())
    with TestClient(app, base_url="http://127.0.0.1:8891") as client:
        job_id = start_job(client)
        before = await_phase(client, job_id, "ready")
        store = app.state.store
        old_directory = store.output_dir(job_id)
        save = store._save

        def fail_publish(job_id):
            if store.get(job_id)["revision"] == 2:
                raise OSError("publication failed")
            save(job_id)

        monkeypatch.setattr(store, "_save", fail_publish)
        assert client.post(f"/jobs/{job_id}/mix", json={"vocal_volume": 50}).status_code == 202
        after = await_phase(client, job_id, "ready")
        assert after["revision"] == before["revision"]
        assert after["vocal_volume"] == before["vocal_volume"]
        assert after["error"]
        assert store.output_dir(job_id) == old_directory
        assert client.get(f"/jobs/{job_id}/mix.wav").content == wav_bytes()
    with client_for(tmp_path, FakeEngine()) as client:
        assert client.get(f"/jobs/{job_id}").json() == after
