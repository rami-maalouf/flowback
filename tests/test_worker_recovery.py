import io
import time
import wave
from threading import Event

from fastapi.testclient import TestClient

from cover_engine.models import Candidate, RenderResult
from cover_engine.server import create_app


def test_persistence_failure_does_not_strand_jobs_after_storage_recovers(tmp_path, monkeypatch):
    entered, release, failed_status_write = Event(), Event(), Event()

    class Engine:
        calls = 0

        def analyze(self, song, vocal, work_dir, progress):
            self.calls += 1
            if self.calls == 1:
                entered.set()
                assert release.wait(3)
            progress("matching", "Finding your section")
            return [Candidate("verse", "Verse", 0, 1, 1, 1, "three distinct words")]

        def render(self, work_dir, candidate_id, progress, *, output_dir=None, **volumes):
            work_dir = output_dir if output_dir is not None else work_dir
            (work_dir / "mix.wav").write_bytes(audio)
            (work_dir / "original.wav").write_bytes(audio)
            return RenderResult(0, 1, 1, "")

    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(8000)
        output.writeframes(b"\x00\x00" * 800)
    audio = buffer.getvalue()
    app = create_app(tmp_path, engine=Engine())
    with TestClient(app, base_url="http://127.0.0.1:8891") as client:
        uploaded = client.post("/uploads", content=audio)
        assert uploaded.status_code == 201
        upload_id = uploaded.json()["id"]
        body = {"song_id": upload_id, "vocal_id": upload_id}
        first = client.post("/jobs", json=body)
        assert first.status_code == 202
        first_id = first.json()["id"]
        assert entered.wait(1)
        store = app.state.store
        save = store._save

        def full_disk(job_id):
            if store.get(job_id)["phase"] == "failed":
                failed_status_write.set()
            raise OSError(28, "No space left on device")

        monkeypatch.setattr(store, "_save", full_disk)
        release.set()
        assert failed_status_write.wait(1)
        monkeypatch.setattr(store, "_save", save)
        assert client.get(f"/jobs/{first_id}").json()["phase"] == "failed"
        second = client.post("/jobs", json=body)
        assert second.status_code == 202
        second_id = second.json()["id"]
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            snapshot = client.get(f"/jobs/{second_id}").json()
            if snapshot["phase"] == "ready":
                break
            time.sleep(0.01)
        assert snapshot["phase"] == "ready"
        assert store.worker.is_alive()
        assert client.get(f"/jobs/{second_id}/mix.wav").content == audio
