"""immutable aligned tracks for a shared-clock player and matching exports."""

import importlib
import json
import re
import shutil
import threading
from pathlib import Path
from uuid import uuid4

import numpy as np
import soundfile as sf
from fastapi import HTTPException

from .audio import mix_stems


class LiveMixer:
    def __init__(self, store):
        self.store = store
        self.lock = threading.Lock()

    def current(self, job_id, revision):
        snapshot = self.store.get(job_id)
        if snapshot["phase"] != "ready" or snapshot["revision"] != revision:
            raise HTTPException(409, "This cover changed. Reload the player and try again.")
        return snapshot

    def directory(self, job_id, revision):
        self.store.get(job_id)
        return self.store.root / "jobs" / job_id / "previews" / str(revision)

    def prepare(self, job_id, revision):
        with self.lock:
            snapshot = self.current(job_id, revision)
            directory = self.directory(job_id, revision)
            manifest_path = directory / "preview.json"
            if not manifest_path.is_file():
                temporary = directory.with_name(uuid4().hex)
                temporary.mkdir(parents=True)
                try:
                    with self.store.lock:
                        self.current(job_id, revision)
                        published = self.store.output_dir(job_id)
                    names = ("preview-vocal.wav", "preview-backing.wav", "original.wav")
                    if all((published / name).is_file() for name in names):
                        for name in names:
                            shutil.copyfile(published / name, temporary / name)
                    else:
                        engine = self.store.engine or importlib.import_module(
                            "cover_engine.pipeline"
                        )
                        engine.render(
                            self.store.work_dir(job_id),
                            snapshot["selected_id"],
                            lambda *_: None,
                            output_dir=temporary,
                        )
                    tracks = [
                        sf.read(temporary / name, dtype="float32", always_2d=True) for name in names
                    ]
                    vocal, rate = tracks[0]
                    if not vocal.size or any(
                        audio.shape != vocal.shape
                        or sample_rate != rate
                        or not np.isfinite(audio).all()
                        for audio, sample_rate in tracks
                    ):
                        raise ValueError("The preview tracks do not match. Create the cover again.")
                    manifest = {
                        "duration": len(vocal) / rate,
                        "vocal_peak": float(np.max(np.abs(vocal))),
                        "backing_peak": float(np.max(np.abs(tracks[1][0]))),
                        "vocal_volume": snapshot["vocal_volume"],
                        "backing_volume": snapshot["backing_volume"],
                        **{
                            name.removeprefix("preview-")[:-4]
                            + "_url": f"/jobs/{job_id}/preview/{revision}/{name}"
                            for name in names
                        },
                    }
                    (temporary / "preview.json").write_text(json.dumps(manifest))
                    self.current(job_id, revision)
                    temporary.rename(directory)
                finally:
                    if temporary.exists():
                        shutil.rmtree(temporary)
            self.current(job_id, revision)
            return json.loads(manifest_path.read_text())

    def track(self, job_id, revision, name):
        if name not in {"preview-vocal.wav", "preview-backing.wav", "original.wav"}:
            raise HTTPException(404, "Track not found.")
        path = self.directory(job_id, revision) / name
        if not path.is_file():
            raise HTTPException(404, "Load the player before requesting its tracks.")
        return path

    def export(self, job_id, revision, vocal_volume, backing_volume):
        self.prepare(job_id, revision)
        directory = self.directory(job_id, revision)
        vocal, rate = sf.read(directory / "preview-vocal.wav", dtype="float32", always_2d=True)
        backing, _ = sf.read(directory / "preview-backing.wav", dtype="float32", always_2d=True)
        mixed = mix_stems(vocal, backing, vocal_volume, backing_volume)
        download_id = uuid4().hex
        path = self.store.root / "jobs" / job_id / "downloads" / f"{download_id}.wav"
        path.parent.mkdir(exist_ok=True)
        sf.write(path, mixed, rate, subtype="PCM_24")
        try:
            self.current(job_id, revision)
        except Exception:
            path.unlink(missing_ok=True)
            raise
        return {"download_id": download_id}

    def download(self, job_id, download_id):
        self.store.get(job_id)
        if not re.fullmatch(r"[a-f0-9]{32}", download_id):
            raise HTTPException(404, "Export not found.")
        path = self.store.root / "jobs" / job_id / "downloads" / f"{download_id}.wav"
        if not path.is_file():
            raise HTTPException(404, "Export not found.")
        return path


STUDIO = Path(__file__).parent / "studio"
