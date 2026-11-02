"""persistent standalone stems, scheduled on the cover engine's serial worker."""

import importlib
import json
import logging
import re
import shutil
import threading
import time
from copy import deepcopy
from uuid import uuid4

import numpy as np
import soundfile as sf
from fastapi import HTTPException

from .persistence import write_json
from .pipeline import separator_model

LOGGER = logging.getLogger(__name__)
KINDS = ("instrumental", "vocals")
QUEUE_KIND = object()
# extractions saved before the separator was recorded all used this karaoke model.
LEGACY_SEPARATOR = "UVR_MDXNET_KARA_2.onnx"
# ffmpeg before 8 writes 32-bit float with the extensible header, which soundfile names WAVEX.
WAV_FORMATS = frozenset({"WAV", "WAVEX"})
INTERNAL = frozenset({"separator", "levels", "level_writes"})
# the newest writers whose late, out-of-order level saves are still ignored.
LEVEL_WRITERS = 32


def _public(item):
    # clients read an exact shape: an extra field fails the whole response there.
    return {key: deepcopy(value) for key, value in item.items() if key not in INTERNAL}


class ExtractionStore:
    def __init__(self, store):
        self.store = store
        self.root = store.root / "extractions"
        self.root.mkdir(parents=True, exist_ok=True)
        self.items = {}
        self.player_lock = threading.Lock()
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
                    else:
                        if item.get("separator", LEGACY_SEPARATOR) != separator_model():
                            self.update(
                                item["id"],
                                phase="queued",
                                message="Waiting to separate your song again with a better model",
                            )
                            store.pending.put((item["id"], QUEUE_KIND))
            except (ValueError, KeyError, OSError, TypeError):
                LOGGER.warning("could not restore extraction: %s", path)

    def work_dir(self, extraction_id):
        return self.root / extraction_id / "work"

    def get(self, extraction_id):
        with self.store.lock:
            if extraction_id not in self.items:
                raise HTTPException(404, "This extracted song was not found.")
            return _public(self.items[extraction_id])

    def library(self):
        with self.store.lock:
            return {
                "extractions": [
                    _public(item)
                    for item in sorted(
                        self.items.values(), key=lambda item: item["created_at"], reverse=True
                    )
                ]
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
                    return _public(item)
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
            return _public(item)

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
        if any(info.format not in WAV_FORMATS or info.frames <= 0 for info in tracks):
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

    def player(self, extraction_id):
        with self.player_lock:
            if self.get(extraction_id)["phase"] != "ready":
                raise HTTPException(409, "Wait until this song finishes extracting.")
            directory = self.work_dir(extraction_id) / "player"
            directory.mkdir(exist_ok=True)
            for kind in KINDS:
                target = directory / f"{kind}.flac"
                if target.is_file():
                    continue
                audio, rate = sf.read(
                    self.work_dir(extraction_id) / f"{kind}.wav", dtype="float32", always_2d=True
                )
                temporary = directory / f".{uuid4().hex}.flac"
                try:
                    # a player loads whole tracks before it plays; 16-bit flac is a quarter
                    # of the float wav, so a full song arrives quickly from a server.
                    sf.write(
                        temporary, np.clip(audio, -1, 1), rate, format="FLAC", subtype="PCM_16"
                    )
                    temporary.replace(target)
                finally:
                    temporary.unlink(missing_ok=True)
            duration = sf.info(directory / "vocals.flac").duration
        with self.store.lock:
            levels = self.items[extraction_id].get("levels")
            return {
                "duration": duration,
                **{
                    f"{kind}_url": f"/extractions/{extraction_id}/player/{kind}.flac"
                    for kind in KINDS
                },
                "vocal_volume": levels["vocal_volume"] if levels else 100,
                "backing_volume": levels["backing_volume"] if levels else 100,
                "levels_saved": levels is not None,
            }

    def player_track(self, extraction_id, kind):
        with self.store.lock:
            if kind not in KINDS:
                raise HTTPException(404, "This audio track was not found.")
            if self.get(extraction_id)["phase"] != "ready":
                raise HTTPException(409, "Wait until this song finishes extracting.")
            path = self.work_dir(extraction_id) / "player" / f"{kind}.flac"
            if not path.is_file():
                raise HTTPException(404, "Load the player before requesting its tracks.")
            return path

    def save_levels(self, extraction_id, vocal_volume, backing_volume, client_id, sequence):
        with self.store.lock:
            self.get(extraction_id)
            writes = self.items[extraction_id].get("level_writes", {})
            if sequence <= writes.get(client_id, 0):
                return {"ok": True}
            writes = {name: count for name, count in writes.items() if name != client_id}
            writes[client_id] = sequence
            self.update(
                extraction_id,
                levels={"vocal_volume": vocal_volume, "backing_volume": backing_volume},
                level_writes=dict(list(writes.items())[-LEVEL_WRITERS:]),
            )
            return {"ok": True}

    def process(self, extraction_id):
        try:
            item = self.get(extraction_id)
            engine = self.store.engine or importlib.import_module("cover_engine.pipeline")
            separator = separator_model()
            self.update(extraction_id, phase="separating", message="Separating vocals and music")
            with self.player_lock:
                shutil.rmtree(self.work_dir(extraction_id) / "player", ignore_errors=True)

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
                separator=separator,
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
