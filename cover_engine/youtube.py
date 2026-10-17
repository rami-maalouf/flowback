"""bounded imports of single public youtube videos."""

import json
import re
import subprocess
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from fastapi import HTTPException


def video_url(value: str) -> str:
    try:
        parsed = urlsplit(value.strip())
        if (
            parsed.scheme not in {"http", "https"}
            or parsed.username is not None
            or parsed.password is not None
            or parsed.port not in {None, 80, 443}
        ):
            raise ValueError
        if parsed.hostname == "youtu.be":
            video_id = parsed.path.removeprefix("/")
        elif parsed.hostname in {
            "youtube.com",
            "www.youtube.com",
            "m.youtube.com",
            "music.youtube.com",
        }:
            if parsed.path == "/watch":
                video_id = parse_qs(parsed.query).get("v", [""])[0]
            elif parsed.path.startswith(("/shorts/", "/embed/")):
                video_id = parsed.path.split("/")[-1]
            else:
                raise ValueError
        else:
            raise ValueError
        if not re.fullmatch(r"[A-Za-z0-9_-]{11}", video_id):
            raise ValueError
    except ValueError as error:
        raise HTTPException(
            422, "Paste a link to a single YouTube video, or choose an audio file."
        ) from error
    return f"https://www.youtube.com/watch?v={video_id}"


def download(value: str, directory: Path, limit: int) -> tuple[Path, str, str]:
    url = video_url(value)
    try:
        subprocess.run(
            [
                sys.executable,
                "-m",
                "yt_dlp",
                "--ignore-config",
                "--no-playlist",
                "--no-progress",
                "--no-warnings",
                "--js-runtimes",
                "bun",
                "--socket-timeout",
                "15",
                "--retries",
                "1",
                "--fragment-retries",
                "1",
                "--max-filesize",
                str(limit),
                "--match-filter",
                "duration <= 1200 & !is_live",
                "--format",
                "bestaudio",
                "--write-info-json",
                "--output",
                str(directory / "source.%(ext)s"),
                "--",
                url,
            ],
            check=True,
            capture_output=True,
            timeout=120,
        )
        metadata = json.loads((directory / "source.info.json").read_text())
        files = [
            path
            for path in directory.glob("source.*")
            if path.suffix not in {".json", ".part", ".ytdl"}
        ]
        if len(files) != 1 or not 0 < files[0].stat().st_size <= limit:
            raise ValueError("no complete audio within the size limit")
        title = "".join(c for c in str(metadata.get("title") or "YouTube song") if c.isprintable())[
            :160
        ]
        return files[0], title, url
    except subprocess.TimeoutExpired as error:
        raise HTTPException(
            504, "YouTube took too long to respond. Try again or choose an audio file."
        ) from error
    except (subprocess.CalledProcessError, OSError, ValueError) as error:
        raise HTTPException(
            422,
            "Could not import this YouTube video. It may be unavailable or restricted. "
            "Use a public video under 20 minutes and 64 MB, or choose an audio file.",
        ) from error
