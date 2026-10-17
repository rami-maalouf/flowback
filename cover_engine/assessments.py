import hashlib
import importlib
import json
import logging
import math
import queue
import threading
from copy import deepcopy
from tempfile import NamedTemporaryFile

import soundfile as sf
from fastapi import HTTPException

from .persistence import write_json

LOGGER = logging.getLogger(__name__)


def envelope(version, candidate_id, status="not_started", error=""):
    return {
        "status": status,
        "version": version,
        "candidate_id": candidate_id,
        "summary": "",
        "error": error,
        "metrics": [],
        "notes": [],
        "reference_text": "",
        "performance_text": "",
        "words": [],
        "pitch": {"points": [], "unit": "semitones from median"},
    }


class AssessmentStore:
    def __init__(self, store, assessor=None):
        self.store = store
        self.assessor = assessor or importlib.import_module("cover_engine.assessment")
        self.version = self.assessor.VERSION
        self.root = store.root / "assessments"
        self.root.mkdir(exist_ok=True)
        self.lock = threading.RLock()
        self.pending = queue.Queue()
        self.stopping = threading.Event()
        self.records = {}
        self.fingerprints = {}
        self.fingerprint_lock = threading.Lock()
        for path in self.root.glob("*.json"):
            try:
                record = json.loads(path.read_text())
                key = self._key(record["identity"])
                if path.stem != key or record["identity"]["version"] != self.version:
                    continue
                result = record["result"]
                if result["status"] in {"queued", "running"}:
                    record["result"] = envelope(
                        self.version,
                        record["identity"]["candidate_id"],
                        "failed",
                        "Analysis was interrupted. Retry analysis; your cover is still available.",
                    )
                    self._save(key, record)
                self.records[key] = record
            except (OSError, ValueError, KeyError, TypeError):
                LOGGER.warning("could not restore assessment: %s", path)
        self.worker = threading.Thread(target=self._work, name="cover-assessment", daemon=True)

    @staticmethod
    def _key(identity):
        return hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()

    def _identity(self, job_id, *, require_ready=False):
        with self.store.lock:
            snapshot = self.store.get(job_id)
            if require_ready and (snapshot["phase"] != "ready" or not snapshot["selected_id"]):
                raise HTTPException(409, "Choose a section and finish the cover before analysis.")
            job = self.store.jobs[job_id]
            identity = {
                "song_id": job["song_id"],
                "vocal_id": job["vocal_id"],
                "candidate_id": snapshot["selected_id"],
                "version": self.version,
            }
        identity["evidence"] = self._evidence(self.store.work_dir(job_id))
        return identity

    def _evidence(self, directory):
        return {
            name: self._artifact_digest(directory / name)
            for name in ("analysis.json", "reference.wav", "performance.wav")
        }

    def _artifact_digest(self, path):
        for _ in range(2):
            try:
                before = path.stat()
            except FileNotFoundError:
                return "missing"
            stamp = (
                before.st_dev,
                before.st_ino,
                before.st_size,
                before.st_mtime_ns,
                before.st_ctime_ns,
            )
            with self.fingerprint_lock:
                cached = self.fingerprints.get(path)
                if cached and cached[0] == stamp:
                    return cached[1]
            with path.open("rb") as audio:
                digest = hashlib.file_digest(audio, "sha256").hexdigest()
            after = path.stat()
            if stamp == (
                after.st_dev,
                after.st_ino,
                after.st_size,
                after.st_mtime_ns,
                after.st_ctime_ns,
            ):
                with self.fingerprint_lock:
                    self.fingerprints[path] = (stamp, digest)
                return digest
        raise HTTPException(409, "The raw analysis files are changing. Wait and retry analysis.")

    def _save(self, key, record):
        write_json(self.root / f"{key}.json", record)

    def get(self, job_id):
        identity = self._identity(job_id)
        with self.lock:
            record = self.records.get(self._key(identity))
            return deepcopy(
                record["result"] if record else envelope(self.version, identity["candidate_id"])
            )

    def submit(self, job_id):
        identity = self._identity(job_id, require_ready=True)
        key = self._key(identity)
        with self.lock:
            existing = self.records.get(key)
            if existing and existing["result"]["status"] in {"queued", "running", "ready"}:
                return deepcopy(existing["result"])
            record = {
                "identity": identity,
                "result": envelope(self.version, identity["candidate_id"], "queued"),
            }
            self._save(key, record)
            self.records[key] = record
            self.pending.put((key, self.store.work_dir(job_id)))
            return deepcopy(record["result"])

    def _work(self):
        while not self.stopping.is_set():
            item = self.pending.get()
            if item is None or self.stopping.is_set():
                return
            key, work_dir = item
            with self.lock:
                record = deepcopy(self.records[key])
            candidate_id = record["identity"]["candidate_id"]
            try:
                record["result"] = envelope(self.version, candidate_id, "running")
                with self.lock:
                    self._save(key, record)
                    self.records[key] = deepcopy(record)
                if self._evidence(work_dir) != record["identity"]["evidence"]:
                    raise ValueError(
                        "The raw analysis files changed. Retry analysis for this take."
                    )
                measured = self.assessor.assess(work_dir, candidate_id)
                if self._evidence(work_dir) != record["identity"]["evidence"]:
                    raise ValueError(
                        "The raw analysis files changed. Retry analysis for this take."
                    )
                if measured.get("status") != "ready":
                    raise ValueError(
                        "The assessment did not produce a complete result. Retry analysis."
                    )
                record["result"] = {
                    **envelope(self.version, candidate_id),
                    **measured,
                    "status": "ready",
                    "version": self.version,
                    "candidate_id": candidate_id,
                    "error": "",
                }
                with self.lock:
                    if self.stopping.is_set():
                        continue
                    self._save(key, record)
                    self.records[key] = record
            except Exception as error:
                LOGGER.exception("practice assessment failed for %s", key)
                with self.lock:
                    if self.stopping.is_set():
                        continue
                    record["result"] = envelope(
                        self.version,
                        candidate_id,
                        "failed",
                        str(error) or "Could not analyze this take. Your cover is still available.",
                    )
                    self.records[key] = record
                    try:
                        self._save(key, record)
                    except OSError:
                        LOGGER.exception("could not save assessment failure for %s", key)

    def audio(self, job_id, kind):
        if kind not in {"performance", "reference"}:
            raise HTTPException(404, "This practice audio was not found.")
        identity = self._identity(job_id, require_ready=True)
        key = self._key(identity)
        with self.lock:
            record = deepcopy(self.records.get(key))
        if not record or record["result"]["status"] != "ready":
            raise HTTPException(
                409, "Finish this take's analysis before listening to raw excerpts."
            )
        result = record["result"]
        start, end = result.get(kind + "_start"), result.get(kind + "_end")
        if not all(
            isinstance(value, (int, float)) and math.isfinite(value) for value in (start, end)
        ):
            raise HTTPException(409, "This analysis has no reliable raw audio excerpt bounds.")
        if start < 0 or end <= start:
            raise HTTPException(409, "This analysis has no reliable raw audio excerpt bounds.")
        directory = self.root / key
        directory.mkdir(exist_ok=True)
        destination = directory / f"{kind}.wav"
        if not destination.is_file():
            source = self.store.work_dir(job_id) / f"{kind}.wav"
            try:
                with sf.SoundFile(source) as audio:
                    first = round(start * audio.samplerate)
                    last = min(len(audio), round(end * audio.samplerate))
                    if first >= last:
                        raise ValueError("excerpt bounds exceed the original recording")
                    with NamedTemporaryFile(
                        suffix=".wav", dir=directory, delete=False
                    ) as temporary:
                        temporary_path = type(destination)(temporary.name)
                    try:
                        with sf.SoundFile(
                            temporary_path,
                            mode="w",
                            samplerate=audio.samplerate,
                            channels=audio.channels,
                            format="WAV",
                            subtype="FLOAT",
                        ) as output:
                            audio.seek(first)
                            remaining = last - first
                            while remaining:
                                block = audio.read(
                                    min(remaining, 65536), dtype="float32", always_2d=True
                                )
                                if not len(block):
                                    raise ValueError("incomplete raw recording")
                                output.write(block)
                                remaining -= len(block)
                        temporary_path.replace(destination)
                    finally:
                        temporary_path.unlink(missing_ok=True)
            except (OSError, ValueError, sf.LibsndfileError) as error:
                raise HTTPException(
                    409, "The raw recording is unavailable. Reimport this take."
                ) from error
        if self._identity(job_id, require_ready=True) != identity:
            raise HTTPException(409, "The selected section changed. Load its analysis again.")
        return destination

    def close(self):
        self.stopping.set()
        self.pending.put(None)
        if self.worker.is_alive():
            self.worker.join(timeout=1)
