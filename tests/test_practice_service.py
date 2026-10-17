"""song grouping and practice lifecycle, with no musical-accuracy claims."""

import json
import time
from threading import Event
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from test_server import SNAPSHOT_KEYS, FakeEngine, await_phase, upload, wav_bytes

from cover_engine.models import Candidate
from cover_engine.persistence import write_json
from cover_engine.server import create_app


class FakeAssessor:
    VERSION = "practice-test"

    def __init__(self):
        self.calls = []
        self.failure = False
        self.entered = Event()
        self.release = None

    def assess(self, work_dir, candidate_id):
        self.calls.append((work_dir, candidate_id))
        self.entered.set()
        if self.release:
            assert self.release.wait(3)
        if self.failure:
            raise ValueError("raw evidence is missing")
        return {
            "status": "ready",
            "version": self.VERSION,
            "candidate_id": candidate_id,
            "summary": "Measured raw recording",
            "metrics": [{"id": "timing", "available": True, "value": 80}],
            "evidence": {"exact_anchors": 8},
        }


def client_for(tmp_path, assessor, engine=None):
    return TestClient(
        create_app(tmp_path, engine=engine or FakeEngine(), assessor=assessor),
        base_url="http://127.0.0.1:8891",
    )


def create_take(client, song=None, name=""):
    song = song or upload(client, "same title.wav")
    vocal = upload(client, "my verse.wav")
    response = client.post(
        "/jobs",
        json={
            "song_id": song["id"],
            "vocal_id": vocal["id"],
            "title": "My song",
            "take_name": name,
        },
    )
    assert response.status_code == 202, response.text
    assert set(response.json()) == SNAPSHOT_KEYS
    return song, vocal, response.json()["id"]


def await_assessment(client, job_id, status="ready"):
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        response = client.get(f"/jobs/{job_id}/assessment")
        assert response.status_code == 200, response.text
        result = response.json()
        if result["status"] == status:
            return result
        time.sleep(0.01)
    pytest.fail(f"assessment never reached {status}: {result}")


def test_grouping_same_original_only_and_add_take_survive_restart(tmp_path):
    assessor = FakeAssessor()
    with client_for(tmp_path, assessor) as client:
        song, _, first = create_take(client, name="First pass")
        await_phase(client, first, "ready")
        other, _, distinct = create_take(client)
        await_phase(client, distinct, "ready")
        new_vocal = upload(client, "another pass.wav")
        response = client.post(
            f"/songs/{song['id']}/takes", json={"vocal_id": new_vocal["id"], "name": "Second pass"}
        )
        assert response.status_code == 202, response.text
        second = response.json()["id"]
        await_phase(client, second, "ready")
        assert len(client.get("/jobs").json()["covers"]) == 3
        songs = client.get("/songs").json()["songs"]
        assert {row["id"]: row["take_count"] for row in songs} == {song["id"]: 2, other["id"]: 1}
        assert songs[0]["latest_take_id"] == second
        assert songs[0]["title"] == "My song"
        session = client.get(f"/jobs/{second}/session").json()
        assert session["song_id"] == song["id"]
        assert session["vocal_id"] == new_vocal["id"]
        assert session["take_name"] == "Second pass"
        assert client.get(f"/songs/{song['id']}/takes?selected={distinct}").status_code == 404
    state_file = tmp_path / "jobs" / first / "job.json"
    legacy = json.loads(state_file.read_text())
    legacy.pop("take_name")
    state_file.write_text(json.dumps(legacy))
    with client_for(tmp_path, FakeAssessor()) as client:
        takes = client.get(f"/songs/{song['id']}/takes").json()["takes"]
        assert {row["id"]: row["name"] for row in takes} == {
            second: "Second pass",
            first: "my verse.wav",
        }
        assert len(client.get("/songs").json()["songs"]) == 2


def test_assessment_is_persisted_independent_of_mix_and_reused_by_input_identity(tmp_path):
    assessor = FakeAssessor()
    with client_for(tmp_path, assessor) as client:
        song, vocal, job_id = create_take(client)
        await_phase(client, job_id, "ready")
        measured = await_assessment(client, job_id)
        assert measured["evidence"] == {"exact_anchors": 8}
        assert client.post(f"/jobs/{job_id}/assessment").status_code == 202
        assert client.post(f"/jobs/{job_id}/mix", json={"vocal_volume": 50}).status_code == 202
        assert await_phase(client, job_id, "ready")["revision"] == 2
        assert await_assessment(client, job_id) == measured
        duplicate = client.post(
            f"/songs/{song['id']}/takes", json={"vocal_id": vocal["id"]}
        ).json()["id"]
        await_phase(client, duplicate, "ready")
        assert await_assessment(client, duplicate) == measured
        assert len(assessor.calls) == 1
    with client_for(tmp_path, FakeAssessor()) as client:
        assert client.get(f"/jobs/{job_id}/assessment").json() == measured
        assert client.get(f"/jobs/{job_id}/mix.wav").content == wav_bytes()


def test_failure_and_interrupted_assessment_can_retry_without_hiding_audio(tmp_path):
    assessor = FakeAssessor()
    assessor.failure = True
    with client_for(tmp_path, assessor) as client:
        _, _, job_id = create_take(client)
        ready = await_phase(client, job_id, "ready")
        failed = await_assessment(client, job_id, "failed")
        assert "raw evidence" in failed["error"]
        assert client.get(f"/jobs/{job_id}").json() == ready
        assert client.get(f"/jobs/{job_id}/mix.wav").status_code == 200
        assessor.failure = False
        assert client.post(f"/jobs/{job_id}/assessment").status_code == 202
        await_assessment(client, job_id)
    path = next((tmp_path / "assessments").glob("*.json"))
    saved = json.loads(path.read_text())
    saved["result"]["status"] = "running"
    path.write_text(json.dumps(saved))
    with client_for(tmp_path, FakeAssessor()) as client:
        interrupted = client.get(f"/jobs/{job_id}/assessment").json()
        assert interrupted["status"] == "failed"
        assert "interrupted" in interrupted["error"].lower()
        assert client.get(f"/jobs/{job_id}/mix.wav").status_code == 200
        assert client.post(f"/jobs/{job_id}/assessment").status_code == 202
        await_assessment(client, job_id)


def test_candidate_change_cannot_publish_old_assessment_as_current(tmp_path):
    assessor = FakeAssessor()
    assessor.release = Event()
    engine = FakeEngine(
        [
            Candidate("one", "Verse 1", 10, 20, 0.9, 0.9, "repeated"),
            Candidate("two", "Verse 2", 30, 40, 0.9, 0.9, "repeated"),
        ]
    )
    with client_for(tmp_path, assessor, engine) as client:
        _, _, job_id = create_take(client)
        await_phase(client, job_id, "choose")
        assert client.post(f"/jobs/{job_id}/assessment").status_code == 409
        client.post(f"/jobs/{job_id}/choose", json={"candidate_id": "one"})
        await_phase(client, job_id, "ready")
        assert assessor.entered.wait(1)
        client.post(f"/jobs/{job_id}/choose", json={"candidate_id": "two"})
        await_phase(client, job_id, "ready")
        current = client.get(f"/jobs/{job_id}/assessment").json()
        assert current["candidate_id"] == "two"
        assert current["status"] in {"queued", "not_started"}
        assessor.release.set()
        assert await_assessment(client, job_id)["candidate_id"] == "two"


def test_existing_ready_covers_analyze_on_demand_and_old_method_is_not_ranked(tmp_path):
    assessor = FakeAssessor()
    with client_for(tmp_path, assessor) as client:
        song, _, job_id = create_take(client)
        await_phase(client, job_id, "ready")
        await_assessment(client, job_id)
    upgraded = FakeAssessor()
    upgraded.VERSION = "practice-next"
    with client_for(tmp_path, upgraded) as client:
        result = client.get(f"/jobs/{job_id}/assessment").json()
        assert result["status"] == "not_started"
        assert result["metrics"] == []
        assert upgraded.calls == []
        take = client.get(f"/songs/{song['id']}/takes?sort=timing").json()["takes"][0]
        assert take["assessment"]["version"] == upgraded.VERSION
        assert take["assessment"]["metrics"] == []
        assert client.post(f"/jobs/{job_id}/assessment").status_code == 202
        assert await_assessment(client, job_id)["version"] == upgraded.VERSION


@pytest.mark.parametrize(
    ("sort", "metric_id", "base", "better", "reference"),
    [
        ("lyrics", "lyric_agreement", 80, 95, None),
        ("timing", "timing", 80, 20, None),
        ("pace", "pace", 105, 99, 100),
        ("pitch", "pitch_shape", 2, 0.5, None),
        ("range", "pitch_range", 3, 7, 6),
    ],
)
def test_metric_sort_only_ranks_comparable_usable_values(
    tmp_path, sort, metric_id, base, better, reference
):
    assessor = FakeAssessor()
    app = create_app(tmp_path, engine=FakeEngine(), assessor=assessor)
    with TestClient(app, base_url="http://127.0.0.1:8891") as client:
        song, _, selected = create_take(client)
        await_phase(client, selected, "ready")
        await_assessment(client, selected)
        identifiers = [selected]
        for _ in range(4):
            _, _, job_id = create_take(client, song)
            await_phase(client, job_id, "ready")
            await_assessment(client, job_id)
            identifiers.append(job_id)
        improved, missing, different, unknown = identifiers[1:]
        store = app.state.store
        for job_id, value in zip(identifiers, [base, better, None, better, better], strict=True):
            identity = store.assessments._identity(job_id)
            result = store.assessments.records[store.assessments._key(identity)]["result"]
            result["metrics"] = [
                {
                    "id": metric_id,
                    "available": value is not None,
                    "value": value,
                    "reference": reference,
                }
            ]
        with store.lock:
            store.jobs[improved]["snapshot"]["candidates"][0].update(start=12, end=22)
            store.jobs[different]["snapshot"]["candidates"][0].update(start=30, end=40)
            store.jobs[unknown]["snapshot"]["candidates"] = []
        response = client.get(f"/songs/{song['id']}/takes?selected={selected}&sort={sort}")
        assert response.status_code == 200
        takes = response.json()["takes"]
        assert [take["id"] for take in takes[:3]] == [improved, selected, missing]
        assert all(take["comparable"] for take in takes[:3])
        assert {take["id"] for take in takes[3:]} == {different, unknown}
        assert all(not take["comparable"] for take in takes[3:])
        newest = client.get(f"/songs/{song['id']}/takes?sort=newest").json()["takes"]
        assert [take["id"] for take in newest] == identifiers[::-1]
        assert client.get(f"/songs/{song['id']}/takes?sort=talent").status_code == 422


@pytest.mark.parametrize(
    "reference_start,reference_end,expected_status",
    [(0.02, 0.06, 200), (0.02, 0.5, 200), (0.2, 0.5, 409)],
)
def test_raw_audio_endpoints_crop_uncorrected_evidence_and_allow_ranges(
    tmp_path, reference_start, reference_end, expected_status
):
    import io

    import numpy as np
    import soundfile as sf

    class RawEngine(FakeEngine):
        def analyze(self, song, vocal, work_dir, progress):
            sf.write(work_dir / "reference.wav", np.arange(800) / 8000, 8000, subtype="FLOAT")
            sf.write(work_dir / "performance.wav", -np.arange(800) / 8000, 8000, subtype="FLOAT")
            return super().analyze(song, vocal, work_dir, progress)

    class RawAssessor(FakeAssessor):
        def assess(self, *args):
            return {
                **super().assess(*args),
                "reference_start": reference_start,
                "reference_end": reference_end,
                "performance_start": 0.01,
                "performance_end": 0.04,
            }

    assessor = RawAssessor()
    with client_for(tmp_path, assessor, RawEngine()) as client:
        _, _, job_id = create_take(client)
        await_phase(client, job_id, "ready")
        await_assessment(client, job_id)
        for kind, start, end, sign in [
            ("reference", round(reference_start * 8000), min(800, round(reference_end * 8000)), 1),
            ("performance", 80, 320, -1),
        ]:
            response = client.get(f"/jobs/{job_id}/practice-audio/{kind}.wav")
            if kind == "reference" and expected_status != 200:
                assert response.status_code == expected_status
                continue
            assert response.status_code == 200, response.text
            samples, rate = sf.read(io.BytesIO(response.content))
            assert rate == 8000
            np.testing.assert_allclose(samples, sign * np.arange(start, end) / 8000, atol=1e-7)
            ranged = client.get(
                f"/jobs/{job_id}/practice-audio/{kind}.wav", headers={"Range": "bytes=100-199"}
            )
            assert ranged.status_code == 206
            assert ranged.content == response.content[100:200]
        assert client.get(f"/jobs/{job_id}/practice-audio/mix.wav").status_code == 404
        assert client.get(f"/jobs/{job_id}/mix.wav").content == wav_bytes()


def test_changed_raw_evidence_invalidates_analysis_without_mix_revision_change(tmp_path):
    import hashlib
    from unittest.mock import patch

    class EvidenceEngine(FakeEngine):
        def analyze(self, song, vocal, work_dir, progress):
            (work_dir / "analysis.json").write_text('{"method": "first"}')
            (work_dir / "reference.wav").write_bytes(wav_bytes())
            (work_dir / "performance.wav").write_bytes(wav_bytes())
            return super().analyze(song, vocal, work_dir, progress)

    assessor = FakeAssessor()
    with client_for(tmp_path, assessor, EvidenceEngine()) as client:
        _, _, job_id = create_take(client)
        before = await_phase(client, job_id, "ready")
        await_assessment(client, job_id)
        work = tmp_path / "jobs" / job_id / "work"
        with patch("hashlib.file_digest", wraps=hashlib.file_digest) as digest:
            for _ in range(3):
                assert client.get(f"/jobs/{job_id}/assessment").json()["status"] == "ready"
            assert digest.call_count == 0
        (work / "analysis.json").write_text('{"method": "changed transcription"}')
        assert client.get(f"/jobs/{job_id}/assessment").json()["status"] == "not_started"
        assert client.post(f"/jobs/{job_id}/assessment").status_code == 202
        await_assessment(client, job_id)
        (work / "reference.wav").write_bytes(wav_bytes()[:-2] + b"\x01\x00")
        assert client.get(f"/jobs/{job_id}/assessment").json()["status"] == "not_started"
        assert client.post(f"/jobs/{job_id}/assessment").status_code == 202
        await_assessment(client, job_id)
        assert len(assessor.calls) == 3
        assert client.get(f"/jobs/{job_id}").json() == before


def test_delete_and_restore_song_keep_every_take_and_unrelated_song_across_restart(tmp_path):
    with client_for(tmp_path, FakeAssessor()) as client:
        song, vocal, first = create_take(client, name="First pass")
        await_phase(client, first, "ready")
        await_assessment(client, first)
        second = client.post(
            f"/songs/{song['id']}/takes",
            json={"vocal_id": vocal["id"], "name": "Second pass", "request_id": "second"},
        ).json()["id"]
        await_phase(client, second, "ready")
        await_assessment(client, second)
        other, _, unrelated = create_take(client)
        await_phase(client, unrelated, "ready")
        await_assessment(client, unrelated)
        levels = client.post(
            f"/jobs/{first}/levels",
            json={
                "revision": 1,
                "vocal_volume": 125,
                "backing_volume": 70,
                "client_id": "00000000-0000-0000-0000-000000000001",
                "sequence": 1,
            },
        )
        assert levels.status_code == 200
        songs_before = client.get("/songs").json()
        takes_before = client.get(f"/songs/{song['id']}/takes").json()
        library_before = client.get("/jobs").json()
        files_before = {
            path.relative_to(tmp_path): path.read_bytes()
            for path in tmp_path.rglob("*")
            if path.is_file()
        }
        response = client.post(f"/songs/{song['id']}/remove")
        assert response.status_code == 200
        assert response.json() == {"song_id": song["id"], "removed": True}
        with patch("cover_engine.server._write_json", wraps=write_json) as persist:
            assert client.post(f"/songs/{song['id']}/remove").json() == response.json()
            persist.assert_not_called()
        assert [row["id"] for row in client.get("/songs").json()["songs"]] == [other["id"]]
        assert [row["id"] for row in client.get("/jobs").json()["covers"]] == [unrelated]
        removed = client.get("/songs/removed").json()["songs"]
        assert removed == [row for row in songs_before["songs"] if row["id"] == song["id"]]
        assert removed[0]["take_count"] == 2
        assert client.get(f"/songs/{song['id']}/takes").status_code == 410
        assert client.get(f"/jobs/{first}/session").status_code == 410
        assert client.get(f"/jobs/{second}/session").status_code == 410
        assert client.post(
            f"/songs/{song['id']}/takes",
            json={"vocal_id": vocal["id"], "request_id": "second"},
        ).status_code == 410
        assert client.post(
            "/jobs", json={"song_id": song["id"], "vocal_id": vocal["id"]}
        ).status_code == 410
        assert all((tmp_path / path).read_bytes() == data for path, data in files_before.items())
    with client_for(tmp_path, FakeAssessor()) as client:
        assert [row["id"] for row in client.get("/songs").json()["songs"]] == [other["id"]]
        assert client.get("/songs/removed").json()["songs"] == removed
        response = client.post(f"/songs/{song['id']}/restore")
        assert response.status_code == 200
        assert response.json() == {"song_id": song["id"], "removed": False}
        with patch("cover_engine.server._write_json", wraps=write_json) as persist:
            assert client.post(f"/songs/{song['id']}/restore").json() == response.json()
            persist.assert_not_called()
        assert client.get("/songs").json() == songs_before
        assert client.get("/jobs").json() == library_before
        assert client.get(f"/songs/{song['id']}/takes").json() == takes_before
        assert client.get("/songs/removed").json() == {"songs": []}
        assert all((tmp_path / path).read_bytes() == data for path, data in files_before.items())
    with client_for(tmp_path, FakeAssessor()) as client:
        assert client.get("/songs").json() == songs_before
        assert client.get("/songs/removed").json() == {"songs": []}


def test_delete_and_restore_unknown_or_unstarted_song_do_not_change_library(tmp_path):
    with client_for(tmp_path, FakeAssessor()) as client:
        song = upload(client)
        for song_id in ("missing", song["id"]):
            for action in ("remove", "restore"):
                assert client.post(f"/songs/{song_id}/{action}").status_code == 404
        assert client.get("/songs").json() == {"songs": []}
        assert client.get("/songs/removed").json() == {"songs": []}
        assert not (tmp_path / "removed-songs.json").exists()


@pytest.mark.parametrize("action", ["remove", "restore"])
def test_deletion_write_failure_keeps_visible_and_durable_state_unchanged(tmp_path, action):
    with client_for(tmp_path, FakeAssessor()) as client:
        song, _, job_id = create_take(client)
        await_phase(client, job_id, "ready")
        await_assessment(client, job_id)
        if action == "restore":
            assert client.post(f"/songs/{song['id']}/remove").status_code == 200
        songs_before = client.get("/songs").json()
        removed_before = client.get("/songs/removed").json()
        with patch("cover_engine.server._write_json", side_effect=OSError("disk full")):
            response = client.post(f"/songs/{song['id']}/{action}")
            assert response.status_code == 503
            assert "Try again" in response.json()["detail"]
        assert client.get("/songs").json() == songs_before
        assert client.get("/songs/removed").json() == removed_before
    with client_for(tmp_path, FakeAssessor()) as client:
        assert client.get("/songs").json() == songs_before
        assert client.get("/songs/removed").json() == removed_before
        assert client.post(f"/songs/{song['id']}/{action}").status_code == 200


@pytest.mark.parametrize("stage", ["render", "assessment"])
def test_background_completion_does_not_restore_deleted_song(tmp_path, stage):
    entered, release = Event(), Event()

    class PausedEngine(FakeEngine):
        def render(self, *args, **kwargs):
            if stage == "render":
                entered.set()
                assert release.wait(3)
            return super().render(*args, **kwargs)

    assessor = FakeAssessor()
    if stage == "assessment":
        assessor.entered, assessor.release = entered, release
    with client_for(tmp_path, assessor, PausedEngine()) as client:
        song, _, job_id = create_take(client)
        try:
            assert entered.wait(1)
            assert client.post(f"/songs/{song['id']}/remove").status_code == 200
            assert client.get("/songs").json() == {"songs": []}
        finally:
            release.set()
        await_phase(client, job_id, "ready")
        await_assessment(client, job_id)
        assert client.get("/songs").json() == {"songs": []}
        assert client.get("/jobs").json() == {"covers": []}
        removed = client.get("/songs/removed").json()["songs"]
        assert removed[0]["phase"] == "ready"
    with client_for(tmp_path, FakeAssessor()) as client:
        assert client.get("/songs").json() == {"songs": []}
        assert client.get("/songs/removed").json()["songs"] == removed
        assert client.post(f"/songs/{song['id']}/restore").status_code == 200
        assert client.get(f"/jobs/{job_id}/session").json()["job"]["phase"] == "ready"
        assert client.get(f"/jobs/{job_id}/mix.wav").content == wav_bytes()


def test_invalid_deletion_record_does_not_silently_restore_deleted_songs(tmp_path):
    tmp_path.joinpath("removed-songs.json").write_text('{"song_ids": "invalid"}')
    with (
        pytest.raises(ValueError, match="deleted-song list is invalid"),
        client_for(tmp_path, FakeAssessor()),
    ):
        pass
