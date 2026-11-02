"""persistent standalone stems, scheduled on the cover engine's serial worker."""

import importlib
import json
import logging
import re
import time
from copy import deepcopy
from uuid import uuid4

import soundfile as sf
from fastapi import HTTPException

from .persistence import write_json

LOGGER = logging.getLogger(__name__)
KINDS = ("instrumental", "vocals")
QUEUE_KIND = object()


class ExtractionStore:
    def __init__(self, store):
        self.store = store
        self.root = store.root / "extractions"
        self.root.mkdir(parents=True, exist_ok=True)
        self.items = {}
        for path in self.root.glob("*/extraction.json"):
            try:
                item = json.loads(path.read_text())
                if not re.fullmatch(r"[a-f0-9]{32}", path.parent.name):
                    continue
                if item["id"] != path.parent.name or item["song_id"] not in store.uploads:
                    continue
                self.items[item["id"]] = item
                if item["phase"] in {"queued", "separating"}:
                    self.update(
                        item["id"],
                        phase="failed",
                        message="Extraction was interrupted",
                        error="The service stopped during extraction. Try again to finish it.",
                    )
                elif item["phase"] == "ready":
                    try:
                        self.validate(item["id"])
                    except (ValueError, OSError, sf.LibsndfileError):
                        self.update(
                            item["id"],
                            phase="failed",
                            message="Extracted audio is missing or damaged",
                            error="The saved tracks could not be opened. Try extracting again.",
                        )
            except (ValueError, KeyError, OSError, TypeError):
                LOGGER.warning("could not restore extraction: %s", path)

    def work_dir(self, extraction_id):
        return self.root / extraction_id / "work"

    def get(self, extraction_id):
        with self.store.lock:
            if extraction_id not in self.items:
                raise HTTPException(404, "This extracted song was not found.")
            return deepcopy(self.items[extraction_id])

    def library(self):
        with self.store.lock:
            return {
                "extractions": deepcopy(
                    sorted(self.items.values(), key=lambda item: item["created_at"], reverse=True)
                )
            }

    def update(self, extraction_id, **changes):
        with self.store.lock:
            if self.store.stopping.is_set():
                return
            item = {**self.items[extraction_id], **changes}
            write_json(self.root / extraction_id / "extraction.json", item)
            self.items[extraction_id] = item

    def create(self, song_id, title):
        with self.store.lock:
            if song_id not in self.store.uploads:
                raise HTTPException(404, "The original song was not found. Import it again.")
            for item in self.items.values():
                if item["song_id"] == song_id:
                    return deepcopy(item)
            upload = self.store.uploads[song_id]
            extraction_id = uuid4().hex
            item = {
                "id": extraction_id,
                "song_id": song_id,
                "title": title.strip() or upload["name"],
                "song_name": upload["name"],
                "duration": upload["duration"],
                "created_at": time.time(),
                "phase": "queued",
                "message": "Waiting to extract your song",
                "error": "",
            }
            self.work_dir(extraction_id).mkdir(parents=True)
            write_json(self.root / extraction_id / "extraction.json", item)
            self.items[extraction_id] = item
            self.store.pending.put((extraction_id, QUEUE_KIND))
            return deepcopy(item)

    def retry(self, extraction_id):
        with self.store.lock:
            if self.get(extraction_id)["phase"] != "failed":
                raise HTTPException(409, "Only a failed extraction needs to be retried.")
            self.update(
                extraction_id, phase="queued", message="Waiting to extract your song", error=""
            )
            self.store.pending.put((extraction_id, QUEUE_KIND))
            return self.get(extraction_id)

    def validate(self, extraction_id):
        tracks = [sf.info(self.work_dir(extraction_id) / f"{kind}.wav") for kind in KINDS]
        if any(info.format != "WAV" or info.frames <= 0 for info in tracks):
            raise ValueError("The separator did not produce both valid audio tracks.")
        if abs(tracks[0].duration - tracks[1].duration) > 0.1:
            raise ValueError("The extracted tracks have different durations. Try again.")
        return tracks[0].duration

    def audio(self, extraction_id, kind):
        with self.store.lock:
            if kind not in KINDS:
                raise HTTPException(404, "This audio track was not found.")
            if self.get(extraction_id)["phase"] != "ready":
                raise HTTPException(409, "Wait until this song finishes extracting.")
            path = self.work_dir(extraction_id) / f"{kind}.wav"
            if not path.is_file():
                raise HTTPException(404, "The extracted track is missing. Extract the song again.")
            return path

    def process(self, extraction_id):
        try:
            item = self.get(extraction_id)
            engine = self.store.engine or importlib.import_module("cover_engine.pipeline")
            self.update(extraction_id, phase="separating", message="Separating vocals and music")

            def progress(phase, message):
                if phase == "separating":
                    self.update(extraction_id, message=message)

            options = {"stem_cache": self.store.root / "stems"} if self.store.engine is None else {}
            engine.extract(
                self.store.root / "uploads" / f"{item['song_id']}.audio",
                self.work_dir(extraction_id),
                progress,
                **options,
            )
            duration = self.validate(extraction_id)
            self.update(
                extraction_id,
                phase="ready",
                message="Your instrumental and vocals are ready",
                error="",
                duration=duration,
            )
        except Exception as error:
            LOGGER.exception("extraction failed for %s", extraction_id)
            try:
                self.update(
                    extraction_id,
                    phase="failed",
                    message="Could not extract this song",
                    error=str(error) or "The separator failed. Try extracting again.",
                )
            except Exception:
                LOGGER.exception("could not save extraction failure for %s", extraction_id)
