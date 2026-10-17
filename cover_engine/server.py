import asyncio
import importlib
import importlib.util
import json
import logging
import math
import os
import queue
import re
import shutil
import subprocess
import threading
import time
from contextlib import asynccontextmanager
from copy import deepcopy
from dataclasses import asdict
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import unquote, urlsplit
from uuid import uuid4

import soundfile as sf
from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from . import youtube
from .assessments import AssessmentStore, envelope
from .live import STUDIO, LiveMixer
from .persistence import write_json as _write_json

MAX_UPLOAD_BYTES = 64 * 1024 * 1024
ACTIVE_PHASES = {"queued", "separating", "transcribing", "matching", "rendering"}
PROGRESS_PHASES = {"separating", "transcribing", "matching", "rendering"}
AUDIO_FORMATS = "wav,mp3,flac,ogg,mov,matroska,webm,aac,aiff,asf,ape,wv"
LOGGER = logging.getLogger(__name__)


class JobInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    song_id: str = Field(min_length=1, max_length=64)
    vocal_id: str = Field(min_length=1, max_length=64)
    title: str = Field(default="", max_length=160)
    take_name: str = Field(default="", max_length=160)


class TakeInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    vocal_id: str = Field(min_length=1, max_length=64)
    name: str = Field(default="", max_length=160)


class YoutubeInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    url: str = Field(min_length=1, max_length=2048)


class ChoiceInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    candidate_id: str = Field(min_length=1, max_length=256)


class MixInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    vocal_volume: float = Field(default=100, ge=0, le=200, allow_inf_nan=False, strict=True)
    backing_volume: float = Field(default=100, ge=0, le=200, allow_inf_nan=False, strict=True)


class LevelsInput(MixInput):
    revision: int = Field(ge=1, strict=True)
    client_id: str = Field(pattern=r"^[a-f0-9-]{36}$")
    sequence: int = Field(ge=1, strict=True)


class ExportInput(MixInput):
    revision: int = Field(ge=1, strict=True)


def _snapshot(job_id):
    return {
        "id": job_id,
        "phase": "queued",
        "message": "Waiting to analyze your performance",
        "error": "",
        "candidates": [],
        "selected_id": "",
        "start": 0,
        "end": 0,
        "duration": 0,
        "warning": "",
        "vocal_volume": 100,
        "backing_volume": 100,
        "revision": 0,
    }


class JobStore:
    def __init__(self, root: Path, engine, assessor=None):
        self.root = root
        self.engine = engine
        self.lock = threading.RLock()
        self.pending = queue.Queue()
        self.stopping = threading.Event()
        self.uploads = {}
        self.jobs = {}
        (root / "uploads").mkdir(parents=True, exist_ok=True)
        (root / "jobs").mkdir(parents=True, exist_ok=True)
        self._reload()
        self.assessments = AssessmentStore(self, assessor)
        self.worker = threading.Thread(target=self._work, name="cover-engine", daemon=True)

    def _reload(self):
        for path in (self.root / "uploads").glob("*.json"):
            try:
                value = json.loads(path.read_text())
                if not re.fullmatch(r"[a-f0-9]{32}", path.stem) or value["id"] != path.stem:
                    continue
                if path.with_suffix(".audio").is_file():
                    self.uploads[path.stem] = value
            except (ValueError, KeyError, OSError):
                LOGGER.warning("could not restore upload metadata: %s", path)
        for path in (self.root / "jobs").glob("*/job.json"):
            try:
                value = json.loads(path.read_text())
                job_id = path.parent.name
                snapshot = value["snapshot"]
                if not re.fullmatch(r"[a-f0-9]{32}", job_id) or snapshot["id"] != job_id:
                    continue
                defaults = _snapshot(job_id)
                migration_fields = ("vocal_volume", "backing_volume", "revision")
                migrated = any(field not in snapshot for field in migration_fields)
                if "created_at" not in value:
                    value["created_at"] = path.stat().st_mtime
                    migrated = True
                for field in migration_fields:
                    snapshot.setdefault(field, defaults[field])
                if set(snapshot) != set(defaults):
                    continue
                self.jobs[job_id] = value
                if snapshot["phase"] in ACTIVE_PHASES and value.get("previous_ready"):
                    self._restore_ready(
                        job_id,
                        "The mix update was interrupted. Your previous mix is still available. Try again.",
                    )
                    snapshot = self.jobs[job_id]["snapshot"]
                    self._save(job_id)
                elif snapshot["phase"] in ACTIVE_PHASES:
                    snapshot.update(
                        phase="failed",
                        message="Processing was interrupted",
                        error="The service stopped during processing. Create the cover again to retry.",
                    )
                    self._save(job_id)
                elif snapshot["phase"] == "ready" and not all(
                    (self.output_dir(job_id) / name).is_file()
                    for name in ("mix.wav", "original.wav")
                ):
                    snapshot.update(
                        phase="failed",
                        message="Export files are missing",
                        error="The saved audio is missing. Create the cover again to retry.",
                    )
                    self._save(job_id)
                elif migrated:
                    self._save(job_id)
            except (ValueError, KeyError, OSError, TypeError):
                LOGGER.warning("could not restore job: %s", path)

    def _save(self, job_id):
        _write_json(self.root / "jobs" / job_id / "job.json", self.jobs[job_id])

    def work_dir(self, job_id):
        return self.root / "jobs" / job_id / "work"

    def output_dir(self, job_id):
        output_id = self.jobs[job_id].get("output_id")
        if output_id is None:
            return self.work_dir(job_id)
        if not re.fullmatch(r"[a-f0-9]{32}", output_id):
            raise ValueError("Invalid saved export directory")
        return self.root / "jobs" / job_id / "exports" / output_id

    def _queue_render(self, job_id, snapshot, candidate_id):
        previous = deepcopy(self.jobs[job_id])
        if previous["snapshot"]["phase"] == "ready":
            self.jobs[job_id]["previous_ready"] = deepcopy(previous["snapshot"])
        self.jobs[job_id]["snapshot"] = snapshot
        try:
            self._save(job_id)
        except Exception:
            self.jobs[job_id] = previous
            raise
        self.pending.put((job_id, candidate_id))
        return deepcopy(snapshot)

    def _restore_ready(self, job_id, error):
        job = self.jobs[job_id]
        job["snapshot"] = job.pop("previous_ready")
        job["snapshot"].update(error=error)

    def get(self, job_id):
        with self.lock:
            if job_id not in self.jobs:
                raise HTTPException(404, "This cover was not found.")
            return deepcopy(self.jobs[job_id]["snapshot"])

    def saved_session(self, job_id):
        with self.lock:
            snapshot = self.get(job_id)
            job = self.jobs[job_id]
            session = {"job_id": job_id}
            for role in ("song", "vocal"):
                upload = self.uploads.get(job[role + "_id"], {})
                session.update(
                    {
                        role + "_id": job[role + "_id"],
                        role + "_name": upload.get(
                            "name", "Original song" if role == "song" else "Your vocals"
                        ),
                        role + "_duration": upload.get("duration", 0),
                    }
                )
            return {
                **session,
                "title": job.get("title") or session["song_name"],
                "take_name": job.get("take_name") or session["vocal_name"],
                "job": snapshot,
            }

    def library(self):
        with self.lock:
            covers = []
            for job_id, job in sorted(
                self.jobs.items(), key=lambda item: item[1]["created_at"], reverse=True
            ):
                session = self.saved_session(job_id)
                snapshot = session["job"]
                covers.append(
                    {
                        "id": job_id,
                        "title": session["title"],
                        "song_name": session["song_name"],
                        "vocal_name": session["vocal_name"],
                        "created_at": job["created_at"],
                        "phase": snapshot["phase"],
                        "duration": snapshot["duration"],
                        "start": snapshot["start"],
                        "end": snapshot["end"],
                    }
                )
            return {"covers": covers}

    def songs(self):
        with self.lock:
            groups = {}
            for job_id, job in sorted(
                self.jobs.items(), key=lambda item: item[1]["created_at"], reverse=True
            ):
                song_id = job["song_id"]
                if song_id in groups:
                    groups[song_id]["take_count"] += 1
                    continue
                song_name = self.uploads.get(song_id, {}).get("name", "Original song")
                snapshot = job["snapshot"]
                groups[song_id] = {
                    "title": job.get("title") or song_name,
                    "song_name": song_name,
                    "vocal_name": self.uploads.get(job["vocal_id"], {}).get("name", "Your vocals"),
                    "created_at": job["created_at"],
                    "phase": snapshot["phase"],
                    "duration": snapshot["duration"],
                    "start": snapshot["start"],
                    "end": snapshot["end"],
                    "id": song_id,
                    "take_count": 1,
                    "latest_take_id": job_id,
                }
            return {"songs": list(groups.values())}

    def takes(self, song_id, selected="", sort="newest"):
        metrics = {
            "lyrics": "lyric_agreement",
            "timing": "timing",
            "pace": "pace",
            "pitch": "pitch_shape",
            "range": "pitch_range",
        }
        if sort != "newest" and sort not in metrics:
            raise HTTPException(
                422, "Choose newest, lyrics, timing, pace, pitch, or range sorting."
            )
        with self.lock:
            jobs = sorted(
                ((job_id, job) for job_id, job in self.jobs.items() if job["song_id"] == song_id),
                key=lambda item: item[1]["created_at"],
                reverse=True,
            )
            if not jobs:
                raise HTTPException(404, "This song was not found.")
            selected = selected or jobs[0][0]
            if not any(job_id == selected for job_id, _ in jobs):
                raise HTTPException(404, "The selected take does not belong to this song.")
            selected_bounds = self._section(self.jobs[selected]["snapshot"])
            takes = []
            for job_id, job in jobs:
                session = self.saved_session(job_id)
                snapshot = session["job"]
                takes.append(
                    {
                        "id": job_id,
                        "name": session["take_name"],
                        "vocal_name": session["vocal_name"],
                        "created_at": job["created_at"],
                        "phase": snapshot["phase"],
                        "duration": snapshot["duration"],
                        "start": snapshot["start"],
                        "end": snapshot["end"],
                        "selected_id": snapshot["selected_id"],
                        "comparable": self._comparable(selected_bounds, self._section(snapshot)),
                    }
                )
            title = self.saved_session(jobs[0][0])["title"]
        for take in takes:
            measured = self.assessments.get(take["id"])
            take["assessment"] = (
                measured
                if measured["candidate_id"] == take["selected_id"]
                else envelope(self.assessments.version, take["selected_id"])
            )
        if sort != "newest":

            def ranking(take):
                assessment = take["assessment"]
                metric = next(
                    (item for item in assessment["metrics"] if item["id"] == metrics[sort]), {}
                )
                value = metric.get("value")
                eligible = (
                    take["comparable"]
                    and assessment["status"] == "ready"
                    and assessment["version"] == self.assessments.version
                    and metric.get("available") is True
                    and isinstance(value, (int, float))
                    and not isinstance(value, bool)
                    and math.isfinite(value)
                )
                if eligible and sort == "range":
                    reference = metric.get("reference")
                    eligible = isinstance(reference, (int, float)) and math.isfinite(reference)
                    value = abs(value - reference) if eligible else 0
                elif eligible and sort == "pace":
                    value = abs(value - 100)
                elif eligible and sort == "lyrics":
                    value = -value
                return (
                    not take["comparable"],
                    not eligible,
                    value if eligible else 0,
                    -take["created_at"],
                )

            takes.sort(key=ranking)
        return {"song_id": song_id, "title": title, "takes": takes}

    @staticmethod
    def _section(snapshot):
        candidate = next(
            (item for item in snapshot["candidates"] if item["id"] == snapshot["selected_id"]), None
        )
        if candidate is None:
            return None
        start, end = candidate.get("start"), candidate.get("end")
        if (
            not isinstance(start, (int, float))
            or not isinstance(end, (int, float))
            or not math.isfinite(start)
            or not math.isfinite(end)
            or start < 0
            or end <= start
        ):
            return None
        return start, end

    @staticmethod
    def _comparable(first, second):
        if first is None or second is None:
            return False
        overlap = max(0, min(first[1], second[1]) - max(first[0], second[0]))
        return overlap / (first[1] - first[0]) >= 0.8 and overlap / (second[1] - second[0]) >= 0.8

    def add_take(self, song_id, vocal_id, name=""):
        with self.lock:
            latest = max(
                (job for job in self.jobs.values() if job["song_id"] == song_id),
                key=lambda job: job["created_at"],
                default=None,
            )
            if latest is None:
                raise HTTPException(404, "This song was not found.")
            title = latest.get("title") or self.uploads.get(song_id, {}).get(
                "name", "Original song"
            )
            return self.create(song_id, vocal_id, title, name)

    def create(self, song_id, vocal_id, title="", take_name=""):
        with self.lock:
            if song_id not in self.uploads or vocal_id not in self.uploads:
                raise HTTPException(404, "An uploaded audio file was not found. Import it again.")
            job_id = uuid4().hex
            self.work_dir(job_id).mkdir(parents=True)
            snapshot = _snapshot(job_id)
            self.jobs[job_id] = {
                "song_id": song_id,
                "vocal_id": vocal_id,
                "snapshot": snapshot,
                "created_at": time.time(),
                "title": title.strip(),
                "take_name": take_name.strip(),
            }
            self._save(job_id)
            self.pending.put((job_id, None))
            return deepcopy(snapshot)

    def choose(self, job_id, candidate_id):
        with self.lock:
            snapshot = self.get(job_id)
            if snapshot["phase"] not in {"choose", "ready"}:
                raise HTTPException(409, "Wait until this cover is ready for a section choice.")
            if not any(candidate["id"] == candidate_id for candidate in snapshot["candidates"]):
                raise HTTPException(422, "Choose one of this cover's suggested sections.")
            snapshot.update(
                phase="queued",
                message="Waiting to render your chosen section",
                selected_id=candidate_id,
                error="",
                start=0,
                end=0,
                duration=0,
                warning="",
            )
            return self._queue_render(job_id, snapshot, candidate_id)

    def update(self, job_id, **changes):
        with self.lock:
            if not self.stopping.is_set():
                self.jobs[job_id]["snapshot"].update(changes)
                self._save(job_id)

    def remix(self, job_id, vocal_volume, backing_volume):
        with self.lock:
            snapshot = self.get(job_id)
            if snapshot["phase"] != "ready":
                raise HTTPException(409, "Wait until this cover is ready before changing the mix.")
            snapshot.update(
                phase="queued",
                message="Waiting to update your mix",
                vocal_volume=vocal_volume,
                backing_volume=backing_volume,
                error="",
            )
            return self._queue_render(job_id, snapshot, snapshot["selected_id"])

    def _work(self):
        while not self.stopping.is_set():
            item = self.pending.get()
            if item is None or self.stopping.is_set():
                return
            job_id, candidate_id = item
            try:
                engine = self.engine or importlib.import_module("cover_engine.pipeline")

                def progress(phase, message, current_job=job_id):
                    if phase in PROGRESS_PHASES:
                        self.update(current_job, phase=phase, message=message)

                if candidate_id is None:
                    self.update(job_id, phase="separating", message="Preparing the original song")
                    job = self.jobs[job_id]
                    options = (
                        {"reference_cache": self.root / "references"} if self.engine is None else {}
                    )
                    candidates = engine.analyze(
                        self.root / "uploads" / f"{job['song_id']}.audio",
                        self.root / "uploads" / f"{job['vocal_id']}.audio",
                        self.work_dir(job_id),
                        progress,
                        **options,
                    )
                    if self.stopping.is_set():
                        continue
                    if not candidates:
                        raise ValueError(
                            "No reliable lyrical match was found. Try a clearer or longer performance."
                        )
                    candidates = sorted(
                        candidates, key=lambda candidate: candidate.score, reverse=True
                    )
                    self.update(job_id, candidates=[asdict(candidate) for candidate in candidates])
                    best = candidates[0]
                    gap = best.score - candidates[1].score if len(candidates) > 1 else math.inf
                    if best.coverage < 0.75 or gap < 0.12 - 1e-9:
                        self.update(
                            job_id,
                            phase="choose",
                            message="Choose the section that matches your performance",
                        )
                        continue
                    candidate_id = best.id
                if self.stopping.is_set():
                    continue
                self.update(
                    job_id,
                    phase="rendering",
                    selected_id=candidate_id,
                    message="Aligning and mixing your performance",
                )
                snapshot = self.get(job_id)
                output_id = uuid4().hex
                output_dir = self.root / "jobs" / job_id / "exports" / output_id
                output_dir.mkdir(parents=True)
                result = engine.render(
                    self.work_dir(job_id),
                    candidate_id,
                    progress,
                    vocal_volume=snapshot["vocal_volume"],
                    backing_volume=snapshot["backing_volume"],
                    output_dir=output_dir,
                )
                if not all((output_dir / name).is_file() for name in ("mix.wav", "original.wav")):
                    raise ValueError(
                        "The audio engine did not produce both exports. Try creating the cover again."
                    )
                for name in ("mix.wav", "original.wav"):
                    info = sf.info(output_dir / name)
                    if info.format != "WAV" or info.frames <= 0:
                        raise ValueError("The audio engine produced an invalid export.")
                with self.lock:
                    if self.stopping.is_set():
                        continue
                    previous = deepcopy(self.jobs[job_id])
                    self.jobs[job_id]["output_id"] = output_id
                    self.jobs[job_id].pop("previous_ready", None)
                    try:
                        self.update(
                            job_id,
                            phase="ready",
                            message="Your cover is ready",
                            revision=snapshot["revision"] + 1,
                            **asdict(result),
                        )
                    except Exception:
                        self.jobs[job_id] = previous
                        raise
                try:
                    self.assessments.submit(job_id)
                except Exception:
                    LOGGER.exception("could not queue practice analysis for %s", job_id)
            except Exception as error:
                LOGGER.exception("cover processing failed for %s", job_id)
                try:
                    with self.lock:
                        if self.jobs[job_id].get("previous_ready"):
                            self._restore_ready(
                                job_id,
                                "Could not update your mix. Your previous mix is still available. Try again.",
                            )
                            self._save(job_id)
                        else:
                            self.update(
                                job_id,
                                phase="failed",
                                message="Could not finish this cover",
                                error=str(error)
                                or "The audio engine failed. Try creating the cover again.",
                            )
                except Exception:
                    LOGGER.exception("could not persist failed status for %s", job_id)

    def close(self):
        with self.lock:
            self.stopping.set()
        self.pending.put(None)
        if self.worker.is_alive():
            self.worker.join(timeout=1)
        self.assessments.close()


def _validate_audio(path: Path):
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        raise HTTPException(
            503, "Install ffmpeg (including ffprobe), then import your audio again."
        )
    common = ["-v", "error", "-protocol_whitelist", "file,pipe", "-format_whitelist", AUDIO_FORMATS]
    try:
        probe = subprocess.run(
            [
                "ffprobe",
                *common,
                "-show_entries",
                "format=duration:stream=codec_type,duration",
                "-of",
                "json",
                str(path),
            ],
            capture_output=True,
            timeout=60,
            check=True,
        )
        metadata = json.loads(probe.stdout)
        audio_streams = [
            stream for stream in metadata.get("streams", []) if stream.get("codec_type") == "audio"
        ]
        if not audio_streams:
            raise ValueError("no audio stream")
        duration = float(
            audio_streams[0].get("duration", metadata.get("format", {}).get("duration", 0))
        )
        if not math.isfinite(duration) or duration <= 0:
            raise ValueError("invalid duration")
        subprocess.run(
            [
                "ffmpeg",
                "-nostdin",
                *common,
                "-xerror",
                "-i",
                str(path),
                "-map",
                "0:a:0",
                "-f",
                "null",
                "-",
            ],
            capture_output=True,
            timeout=60,
            check=True,
        )
        return duration
    except subprocess.TimeoutExpired as error:
        raise HTTPException(
            422, "Audio validation took too long. Try a shorter audio file."
        ) from error
    except (subprocess.CalledProcessError, ValueError, TypeError, OSError) as error:
        raise HTTPException(
            422, "This file could not be decoded as audio. Try WAV, MP3, M4A, or FLAC."
        ) from error


def _local_address(value: str, *, origin=False):
    try:
        parsed = urlsplit(value if origin else f"//{value}")
        return (
            parsed.hostname in {"localhost", "127.0.0.1"}
            and parsed.username is None
            and parsed.password is None
            and not parsed.path
            and not parsed.query
            and not parsed.fragment
            and (not origin or parsed.scheme in {"http", "https"})
            and (parsed.port is None or 0 < parsed.port < 65536)
        )
    except ValueError:
        return False


def create_app(storage_dir: Path | str | None = None, *, engine=None, assessor=None):
    root = Path(
        storage_dir
        or os.environ.get("EXACT_COVER_DATA_DIR", Path.home() / ".local/share/exact-cover")
    )

    @asynccontextmanager
    async def lifespan(app):
        store = JobStore(root, engine, assessor)
        app.state.store = store
        app.state.live = LiveMixer(store)
        store.assessments.worker.start()
        store.worker.start()
        try:
            yield
        finally:
            await asyncio.to_thread(store.close)

    app = FastAPI(title="Exact Cover local engine", lifespan=lifespan)
    app.add_middleware(
        CORSMiddleware,
        allow_origin_regex=r"https?://(?:localhost|127\.0\.0\.1)(?::[0-9]+)?",
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["Content-Type", "X-File-Name", "Range"],
        expose_headers=["Content-Range"],
    )

    @app.middleware("http")
    async def local_requests_only(request: Request, call_next):
        origins = request.headers.getlist("origin")
        hosts = request.headers.getlist("host")
        if len(hosts) != 1 or not _local_address(hosts[0]):
            return JSONResponse(
                {"detail": "This service only accepts localhost requests."}, status_code=403
            )
        if len(origins) > 1 or (origins and not _local_address(origins[0], origin=True)):
            return JSONResponse(
                {"detail": "This service only accepts local browser origins."}, status_code=403
            )
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request, error):
        return JSONResponse(
            {
                "detail": "Invalid request. Check file and section identifiers and use volumes from 0 to 200."
            },
            status_code=422,
        )

    @app.get("/health")
    async def health():
        models_available = engine is not None or all(
            importlib.util.find_spec(name) is not None
            for name in ("audio_separator", "faster_whisper")
        )
        has_decoder = bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))
        message = "The local audio engine is ready."
        if not has_decoder:
            message = "Install ffmpeg (including ffprobe) to import and process audio."
        elif not models_available:
            message = (
                "Install model dependencies with uv sync --extra models before creating a cover."
            )
        return {"ok": True, "models_available": models_available, "message": message}

    @app.post("/uploads", status_code=201)
    async def upload(request: Request):
        store = app.state.store
        content_length = request.headers.get("content-length")
        if content_length:
            try:
                length = int(content_length)
            except ValueError as error:
                raise HTTPException(400, "Invalid upload length.") from error
            if length < 0:
                raise HTTPException(400, "Invalid upload length.")
            if length > MAX_UPLOAD_BYTES:
                raise HTTPException(413, "Choose an audio file no larger than 64 MiB.")
        upload_id = uuid4().hex
        path = root / "uploads" / f"{upload_id}.audio"
        name = unquote(request.headers.get("x-file-name", "audio"))
        name = name.replace("\\", "/").rsplit("/", 1)[-1]
        name = "".join(character for character in name if character.isprintable())[:255] or "audio"
        complete = False
        try:
            size = 0
            with path.open("xb") as file:
                async for chunk in request.stream():
                    size += len(chunk)
                    if size > MAX_UPLOAD_BYTES:
                        raise HTTPException(413, "Choose an audio file no larger than 64 MiB.")
                    file.write(chunk)
            if size == 0:
                raise HTTPException(422, "The audio file is empty.")
            duration = await asyncio.to_thread(_validate_audio, path)
            metadata = {"id": upload_id, "name": name, "duration": duration}
            with store.lock:
                _write_json(path.with_suffix(".json"), metadata)
                store.uploads[upload_id] = metadata
            complete = True
            return metadata
        finally:
            if not complete:
                path.unlink(missing_ok=True)

    @app.post("/imports/youtube", status_code=201)
    async def import_youtube(body: YoutubeInput):
        youtube.video_url(body.url)

        def run():
            with TemporaryDirectory(prefix="youtube-", dir=root / "uploads") as temporary:
                audio, title, url = youtube.download(body.url, Path(temporary), MAX_UPLOAD_BYTES)
                duration = _validate_audio(audio)
                upload_id = uuid4().hex
                path = root / "uploads" / f"{upload_id}.audio"
                metadata = {"id": upload_id, "name": title, "duration": duration, "source_url": url}
                audio.replace(path)
                try:
                    with app.state.store.lock:
                        _write_json(path.with_suffix(".json"), metadata)
                        app.state.store.uploads[upload_id] = metadata
                except Exception:
                    path.unlink(missing_ok=True)
                    raise
                return metadata

        return await asyncio.to_thread(run)

    @app.post("/jobs", status_code=202)
    async def create_job(body: JobInput):
        return app.state.store.create(body.song_id, body.vocal_id, body.title, body.take_name)

    @app.get("/songs")
    def songs():
        return app.state.store.songs()

    @app.get("/songs/{song_id}/takes")
    def takes(song_id: str, selected: str = "", sort: str = "newest"):
        return app.state.store.takes(song_id, selected, sort)

    @app.post("/songs/{song_id}/takes", status_code=202)
    def add_take(song_id: str, body: TakeInput):
        return app.state.store.add_take(song_id, body.vocal_id, body.name)

    @app.get("/jobs/{job_id}/assessment")
    def assessment(job_id: str):
        return app.state.store.assessments.get(job_id)

    @app.post("/jobs/{job_id}/assessment", status_code=202)
    def analyze_take(job_id: str):
        try:
            return app.state.store.assessments.submit(job_id)
        except OSError as error:
            raise HTTPException(
                503, "Could not save the analysis request. Retry analysis."
            ) from error

    @app.get("/jobs/{job_id}/practice-audio/{kind}.wav")
    async def practice_audio(job_id: str, kind: str):
        path = await asyncio.to_thread(app.state.store.assessments.audio, job_id, kind)
        return FileResponse(path, media_type="audio/wav", filename=f"{kind}-excerpt.wav")

    @app.get("/jobs")
    async def saved_covers():
        return app.state.store.library()

    @app.get("/jobs/{job_id}/session")
    async def saved_session(job_id: str):
        return app.state.store.saved_session(job_id)

    @app.post("/jobs/{job_id}/levels")
    async def save_levels(job_id: str, body: LevelsInput):
        try:
            return await asyncio.to_thread(app.state.live.save_levels, job_id, **body.model_dump())
        except OSError as error:
            raise HTTPException(503, "Could not save the mix levels. Retry saving.") from error

    @app.get("/jobs/{job_id}")
    async def get_job(job_id: str):
        return app.state.store.get(job_id)

    @app.post("/jobs/{job_id}/choose", status_code=202)
    async def choose(job_id: str, body: ChoiceInput):
        return app.state.store.choose(job_id, body.candidate_id)

    @app.post("/jobs/{job_id}/mix", status_code=202)
    async def remix(job_id: str, body: MixInput):
        return app.state.store.remix(job_id, body.vocal_volume, body.backing_volume)

    def output(job_id, name):
        store = app.state.store
        with store.lock:
            if store.get(job_id)["phase"] != "ready":
                raise HTTPException(409, "This cover's audio is not ready yet.")
            path = store.output_dir(job_id) / name
            if not path.is_file():
                raise HTTPException(404, "The exported audio is missing. Create the cover again.")
            return FileResponse(path, media_type="audio/wav", filename=f"cover-{job_id}-{name}")

    @app.get("/jobs/{job_id}/mix.wav")
    async def mix(job_id: str):
        return output(job_id, "mix.wav")

    @app.get("/jobs/{job_id}/original.wav")
    async def original(job_id: str):
        return output(job_id, "original.wav")

    @app.get("/studio/{name}")
    async def studio(name: str):
        if name not in {
            "index.html",
            "player.js",
            "player.css",
            "mixer.js",
            "autosave.js",
            "waveform.js",
            "practice.js",
            "assessment-view.js",
            "assessment.css",
        }:
            raise HTTPException(404, "Page not found.")
        return FileResponse(STUDIO / name)

    @app.get("/jobs/{job_id}/preview")
    async def preview(job_id: str, revision: int):
        try:
            return await asyncio.to_thread(app.state.live.prepare, job_id, revision)
        except (ValueError, OSError, RuntimeError) as error:
            LOGGER.exception("could not prepare live preview")
            raise HTTPException(
                422, "Could not load the tracks. Retry or create the cover again."
            ) from error

    @app.get("/jobs/{job_id}/preview/{revision}/{name}")
    async def preview_track(job_id: str, revision: int, name: str):
        return FileResponse(app.state.live.track(job_id, revision, name), media_type="audio/wav")

    @app.post("/jobs/{job_id}/export")
    async def live_export(job_id: str, body: ExportInput):
        return await asyncio.to_thread(
            app.state.live.export, job_id, body.revision, body.vocal_volume, body.backing_volume
        )

    @app.get("/jobs/{job_id}/downloads/{download_id}.wav")
    async def download(job_id: str, download_id: str):
        return FileResponse(
            app.state.live.download(job_id, download_id),
            media_type="audio/wav",
            filename="my-cover.wav",
        )

    return app


app = create_app()


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8891)
