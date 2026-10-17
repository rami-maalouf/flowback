import json
import subprocess

import pytest
from fastapi import HTTPException
from test_server import FakeEngine, await_phase, client_for, upload, wav_bytes

from cover_engine import youtube


@pytest.mark.parametrize(
    "url",
    [
        "https://youtu.be/rH7eIjeILIE?t=10",
        "https://www.youtube.com/watch?v=rH7eIjeILIE&list=ignored",
        "https://music.youtube.com/watch?v=rH7eIjeILIE",
        "https://www.youtube.com/shorts/rH7eIjeILIE",
    ],
)
def test_video_urls_are_canonicalized_to_one_video(url):
    assert youtube.video_url(url) == "https://www.youtube.com/watch?v=rH7eIjeILIE"


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "https://127.0.0.1/watch?v=rH7eIjeILIE",
        "https://youtube.com.evil.test/watch?v=rH7eIjeILIE",
        "https://evil.test@youtube.com/watch?v=rH7eIjeILIE",
        "https://youtube.com:8891/watch?v=rH7eIjeILIE",
        "https://youtube.com/playlist?list=123",
        "https://youtu.be/../../file",
    ],
)
def test_non_video_and_untrusted_urls_are_rejected(url):
    with pytest.raises(HTTPException) as error:
        youtube.video_url(url)
    assert error.value.status_code == 422


def test_download_is_bounded_and_does_not_load_user_configuration(tmp_path, monkeypatch):
    def run(command, **options):
        assert command[command.index("--max-filesize") + 1] == "4096"
        assert "--ignore-config" in command and "--no-playlist" in command
        assert options["timeout"] == 120
        assert command[-2:] == ["--", "https://www.youtube.com/watch?v=rH7eIjeILIE"]
        (tmp_path / "source.m4a").write_bytes(wav_bytes())
        (tmp_path / "source.info.json").write_text(json.dumps({"title": "Dots & Lines"}))

    monkeypatch.setattr(youtube.subprocess, "run", run)
    path, title, _url = youtube.download("https://youtu.be/rH7eIjeILIE", tmp_path, 4096)
    assert path.name == "source.m4a" and title == "Dots & Lines"


def test_youtube_import_creates_reusable_audio_and_cleans_temporary_files(tmp_path, monkeypatch):
    def download(url, directory, limit):
        path = directory / "source.wav"
        path.write_bytes(wav_bytes())
        return path, "Dots & Lines", youtube.video_url(url)

    monkeypatch.setattr(youtube, "download", download)
    with client_for(tmp_path, FakeEngine()) as client:
        response = client.post("/imports/youtube", json={"url": "https://youtu.be/rH7eIjeILIE"})
        assert response.status_code == 201
        song = response.json()
        assert song["name"] == "Dots & Lines" and song["duration"] == pytest.approx(0.1)
        assert not list((tmp_path / "uploads").glob("youtube-*"))
        vocal = upload(client)
        job = client.post("/jobs", json={"song_id": song["id"], "vocal_id": vocal["id"]}).json()
        await_phase(client, job["id"], "ready")
    with client_for(tmp_path, FakeEngine()) as client:
        assert client.get("/jobs").json()["covers"][0]["title"] == "Dots & Lines"


def test_timeout_and_invalid_audio_leave_no_uploads(tmp_path, monkeypatch):
    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired("yt-dlp", 120)

    monkeypatch.setattr(youtube.subprocess, "run", timeout)
    with pytest.raises(HTTPException) as error:
        youtube.download("https://youtu.be/rH7eIjeILIE", tmp_path, 4096)
    assert error.value.status_code == 504

    def invalid(url, directory, limit):
        path = directory / "source.wav"
        path.write_text("not audio")
        return path, "Invalid", url

    monkeypatch.setattr(youtube, "download", invalid)
    with client_for(tmp_path, FakeEngine()) as client:
        response = client.post("/imports/youtube", json={"url": "https://youtu.be/rH7eIjeILIE"})
        assert response.status_code == 422
        assert list((tmp_path / "uploads").iterdir()) == []
        assert not client.app.state.store.uploads
