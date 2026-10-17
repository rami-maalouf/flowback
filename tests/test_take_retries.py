"""take creation retries use durable per-song request identities."""

import json
from copy import deepcopy

import pytest
from fastapi.testclient import TestClient
from test_practice_service import FakeAssessor
from test_server import SNAPSHOT_KEYS, FakeEngine, await_phase, upload

from cover_engine.server import create_app


def client_for(tmp_path, engine=None):
    return TestClient(
        create_app(tmp_path, engine=engine or FakeEngine(), assessor=FakeAssessor()),
        base_url="http://127.0.0.1:8891",
    )


def saved_song(client):
    song = upload(client, "original.wav")
    vocal = upload(client, "first.wav")
    response = client.post("/jobs", json={"song_id": song["id"], "vocal_id": vocal["id"]})
    assert response.status_code == 202, response.text
    await_phase(client, response.json()["id"], "ready")
    return song["id"]


def test_take_retry_reuses_snapshot_and_initial_metadata_after_restart(tmp_path, monkeypatch):
    engine = FakeEngine()
    with client_for(tmp_path, engine) as client:
        song_id = saved_song(client)
        vocal_id = upload(client, "second.wav")["id"]
        path = f"/songs/{song_id}/takes"
        body = {"vocal_id": vocal_id, "name": "Second pass", "request_id": vocal_id}
        store = client.app.state.store
        original_save = store._save
        saved_jobs = []

        def capture_save(job_id):
            saved_jobs.append(deepcopy(store.jobs[job_id]))
            original_save(job_id)

        monkeypatch.setattr(store, "_save", capture_save)
        response = client.post(path, json=body)
        assert response.status_code == 202, response.text
        job_id = response.json()["id"]
        assert set(response.json()) == SNAPSHOT_KEYS
        assert saved_jobs[0]["request_id"] == vocal_id
        ready = await_phase(client, job_id, "ready")
        retry = client.post(path, json={**body, "name": "Changed before retry"})
        assert retry.status_code == 202, retry.text
        assert retry.json() == ready
        assert client.get(f"/jobs/{job_id}/session").json()["take_name"] == "Second pass"
        assert len(client.get(path).json()["takes"]) == 2
        assert engine.analyzed == 2
        metadata = json.loads((tmp_path / "jobs" / job_id / "job.json").read_text())
        assert metadata["request_id"] == vocal_id

    restarted_engine = FakeEngine()
    with client_for(tmp_path, restarted_engine) as client:
        retry = client.post(path, json=body)
        assert retry.status_code == 202, retry.text
        assert retry.json() == ready
        assert len(client.get(path).json()["takes"]) == 2
        assert restarted_engine.analyzed == 0


def test_take_request_id_is_scoped_to_song_and_rejects_another_recording(tmp_path):
    with client_for(tmp_path) as client:
        song_id = saved_song(client)
        other_song_id = saved_song(client)
        vocal_id = upload(client, "second.wav")["id"]
        other_vocal_id = upload(client, "third.wav")["id"]
        body = {"vocal_id": vocal_id, "request_id": "one-request"}
        path = f"/songs/{song_id}/takes"
        accepted = client.post(path, json=body)
        assert accepted.status_code == 202, accepted.text
        ready = await_phase(client, accepted.json()["id"], "ready")

        conflict = client.post(path, json={**body, "vocal_id": other_vocal_id})
        assert conflict.status_code == 409, conflict.text
        assert len(client.get(path).json()["takes"]) == 2
        assert client.get(f"/jobs/{ready['id']}").json() == ready

        separate_song = client.post(f"/songs/{other_song_id}/takes", json=body)
        assert separate_song.status_code == 202, separate_song.text
        assert separate_song.json()["id"] != ready["id"]
        await_phase(client, separate_song.json()["id"], "ready")


def test_omitting_request_id_keeps_intentional_duplicate_takes(tmp_path):
    with client_for(tmp_path) as client:
        song_id = saved_song(client)
        vocal_id = upload(client, "second.wav")["id"]
        path = f"/songs/{song_id}/takes"
        first = client.post(path, json={"vocal_id": vocal_id})
        assert first.status_code == 202, first.text
        await_phase(client, first.json()["id"], "ready")
        second = client.post(path, json={"vocal_id": vocal_id})
        assert second.status_code == 202, second.text
        assert second.json()["id"] != first.json()["id"]
        await_phase(client, second.json()["id"], "ready")
        assert len(client.get(path).json()["takes"]) == 3


@pytest.mark.parametrize("keyed", [False, True])
def test_failed_initial_save_rolls_back_before_retry(tmp_path, monkeypatch, keyed):
    with client_for(tmp_path) as client:
        song_id = saved_song(client)
        vocal_id = upload(client, "second.wav")["id"]
        store = client.app.state.store
        existing_ids = set(store.jobs)
        original_save = store._save
        attempted_ids = []

        def fail_first_save(job_id):
            attempted_ids.append(job_id)
            if len(attempted_ids) == 1:
                raise OSError(28, "No space left on device")
            original_save(job_id)

        monkeypatch.setattr(store, "_save", fail_first_save)
        if keyed:
            path = f"/songs/{song_id}/takes"
            body = {"vocal_id": vocal_id, "request_id": vocal_id}
        else:
            path = "/jobs"
            body = {"song_id": song_id, "vocal_id": vocal_id}
        with pytest.raises(OSError, match="No space left"):
            client.post(path, json=body)
        assert set(store.jobs) == existing_ids
        assert store.pending.empty()
        assert not (tmp_path / "jobs" / attempted_ids[0] / "job.json").exists()

        retry = client.post(path, json=body)
        assert retry.status_code == 202, retry.text
        job_id = retry.json()["id"]
        assert job_id != attempted_ids[0]
        await_phase(client, job_id, "ready")
        assert set(store.jobs) == existing_ids | {job_id}
        assert (tmp_path / "jobs" / job_id / "job.json").is_file()
