import io

import numpy as np
import pytest
import soundfile as sf
from test_server import FakeEngine, await_phase, client_for, start_job


class StemEngine(FakeEngine):
    def render(self, work_dir, candidate_id, progress, **kwargs):
        result = super().render(work_dir, candidate_id, progress, **kwargs)
        directory = kwargs.get("output_dir") or work_dir
        t = np.arange(800) / 8000
        sf.write(
            directory / "preview-vocal.wav",
            0.8 * np.sin(440 * t * 2 * np.pi),
            8000,
            subtype="FLOAT",
        )
        sf.write(
            directory / "preview-backing.wav",
            0.6 * np.sin(220 * t * 2 * np.pi),
            8000,
            subtype="FLOAT",
        )
        return result


def test_live_export_reuses_stems_preserves_job_and_matches_player_gains(tmp_path):
    engine = StemEngine()
    with client_for(tmp_path, engine) as client:
        job_id = start_job(client)
        snapshot = await_phase(client, job_id, "ready")
        preview = client.get(f"/jobs/{job_id}/preview?revision=1")
        assert preview.status_code == 200
        manifest = preview.json()
        assert manifest["duration"] == 0.1
        vocal, rate = sf.read(io.BytesIO(client.get(manifest["vocal_url"]).content))
        backing, _ = sf.read(io.BytesIO(client.get(manifest["backing_url"]).content))
        original = client.get(f"/jobs/{job_id}/mix.wav").content
        downloads = []
        for voice, music in [(110, 45), (0, 200), (200, 0), (0, 0), (200, 200)]:
            response = client.post(
                f"/jobs/{job_id}/export",
                json={
                    "revision": 1,
                    "vocal_volume": voice,
                    "backing_volume": music,
                },
            )
            assert response.status_code == 200, response.text
            url = f"/jobs/{job_id}/downloads/{response.json()['download_id']}.wav"
            data = client.get(url).content
            actual, sample_rate = sf.read(io.BytesIO(data))
            bound = manifest["vocal_peak"] * voice / 100 + manifest["backing_peak"] * music / 100
            scale = min(1, 0.98 / bound) if bound else 1
            expected = (vocal * voice / 100 + backing * music / 100) * scale
            np.testing.assert_allclose(actual, expected, atol=3e-7)
            assert sample_rate == rate
            assert np.max(np.abs(actual)) <= 0.980001
            downloads.append((url, data))
        assert len(engine.rendered) == 1
        assert engine.analyzed == 1
        assert client.get(f"/jobs/{job_id}").json() == snapshot
        assert client.get(f"/jobs/{job_id}/mix.wav").content == original
        for url, data in downloads:
            assert client.get(url).content == data
            assert client.get(url, headers={"Range": "bytes=0-255"}).content == data[:256]
        assert client.get(f"/jobs/{job_id}/preview/1/analysis.json").status_code == 404
        assert client.get(f"/jobs/{job_id}/downloads/not-a-token.wav").status_code == 404
        assert client.get(f"/jobs/{job_id}/preview?revision=2").status_code == 409
        assert client.post(f"/jobs/{job_id}/export", json={"revision": 2}).status_code == 409


@pytest.mark.parametrize("volume", [-1, 201, "100", None, True])
def test_live_export_rejects_invalid_levels(tmp_path, volume):
    with client_for(tmp_path, StemEngine()) as client:
        response = client.post("/jobs/unknown/export", json={"revision": 1, "vocal_volume": volume})
        assert response.status_code == 422


def test_old_covers_prepare_stems_once_without_replacing_their_published_mix(tmp_path):
    engine = StemEngine()
    with client_for(tmp_path, engine) as client:
        job_id = start_job(client)
        snapshot = await_phase(client, job_id, "ready")
        directory = client.app.state.store.output_dir(job_id)
        (directory / "preview-vocal.wav").unlink()
        (directory / "preview-backing.wav").unlink()
        original = (directory / "mix.wav").read_bytes()
        for _ in range(2):
            assert client.get(f"/jobs/{job_id}/preview?revision=1").status_code == 200
        assert len(engine.rendered) == 2
        assert engine.analyzed == 1
        assert (directory / "mix.wav").read_bytes() == original
        assert client.get(f"/jobs/{job_id}").json() == snapshot


def test_preview_failure_is_retryable_and_preserves_ready_cover(tmp_path):
    engine = FakeEngine()
    with client_for(tmp_path, engine) as client:
        job_id = start_job(client)
        snapshot = await_phase(client, job_id, "ready")
        assert client.get(f"/jobs/{job_id}/preview?revision=1").status_code == 422
        assert client.get(f"/jobs/{job_id}").json() == snapshot
        assert list((tmp_path / "jobs" / job_id / "previews").iterdir()) == []
        client.app.state.store.engine = StemEngine()
        assert client.get(f"/jobs/{job_id}/preview?revision=1").status_code == 200
