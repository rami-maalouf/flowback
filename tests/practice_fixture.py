"""run an isolated real api with synthetic music and known fixture transcripts.

uv run --extra models python tests/practice_fixture.py --port 8898

the default root is .exact/practice-fixture. restarting preserves takes, levels and
assessments. use another --root for a fresh fixture. state.json contains dynamic ids
and absolute upload paths for browser/native drivers. no user audio is read or changed.
"""

import argparse
import json
import sys
import time
from dataclasses import asdict, replace
from pathlib import Path

import numpy as np
import soundfile as sf
import uvicorn
from fastapi.testclient import TestClient

RATE = 44100
TEXT = ["we", "find", "a", "steady", "rhythm", "through", "this", "city"]
NOTES = np.array([50, 52, 53, 55, 57, 55, 53, 52], dtype=float)
MARKER = ".synthetic-practice-fixture"
TITLE = "Practice fixture"
OTHER_TITLE = "Other fixture"


def words(offset, step):
    return [
        {
            "text": text,
            "start": offset + i * step,
            "end": offset + (i + 0.76) * step,
            "probability": 0.95,
        }
        for i, text in enumerate(TEXT)
    ]


def voice(transcript, duration, *, shift=0, noise=False):
    samples = np.zeros(round(duration * RATE), dtype=np.float32)
    rng = np.random.default_rng(517)
    for word, note in zip(transcript, NOTES + shift, strict=True):
        start, stop = round(word["start"] * RATE), round(word["end"] * RATE)
        times = np.arange(stop - start) / RATE
        frequency = 440 * 2 ** ((note - 69) / 12)
        signal = (
            rng.normal(size=len(times))
            if noise
            else np.sin(2 * np.pi * frequency * times)
            + 0.35 * np.sin(4 * np.pi * frequency * times)
            + 0.1 * np.sin(6 * np.pi * frequency * times)
        )
        envelope = np.minimum(1, np.minimum(times / 0.02, (times[-1] - times) / 0.02))
        samples[start:stop] = signal * envelope * 0.15
    return np.column_stack((samples, samples))


def backing(duration):
    times = np.arange(round(duration * RATE)) / RATE
    pulse = np.exp(-((times % 0.45) * 16))
    signal = 0.035 * np.sin(2 * np.pi * 110 * times) * (0.3 + 0.7 * pulse)
    signal += 0.015 * np.sin(2 * np.pi * 165 * times)
    return np.column_stack((signal, signal)).astype(np.float32)


def create_inputs(root):
    inputs = root / "inputs"
    inputs.mkdir(exist_ok=True)
    paths = {}
    song = voice(words(2, 0.45), 6.2) + backing(6.2)
    for name, scale in (("song", 1), ("other_song", 0.85)):
        path = inputs / f"{name}.wav"
        sf.write(path, song * scale, RATE, subtype="FLOAT")
        paths[name] = str(path)
    for name, step, shift, noise in (
        ("steady", 0.45, 0, False),
        ("slower", 0.56, 3, False),
        ("noisy", 0.45, 0, True),
        ("new_take", 0.48, 5, False),
    ):
        transcript = words(0.4, step)
        duration = transcript[-1]["end"] + 0.35
        path = inputs / f"{name}.wav"
        sf.write(path, voice(transcript, duration, shift=shift, noise=noise), RATE, subtype="FLOAT")
        paths[name] = str(path)
    return paths


class SyntheticEngine:
    """replace expensive separation/asr only; use the production renderer and raw audio."""

    def analyze(self, song, vocal, work_dir, progress):
        from cover_engine.alignment import phrase_plan
        from cover_engine.audio import decode
        from cover_engine.models import Candidate, Word

        progress("transcribing", "Using known synthetic fixture words; no speech recognition")
        decode(song, work_dir / "song.wav")
        decode(vocal, work_dir / "performance.wav")
        song_duration = sf.info(work_dir / "song.wav").duration
        vocal_duration = sf.info(work_dir / "performance.wav").duration
        if song_duration < 4 or vocal_duration < 3:
            raise ValueError(
                "Use one of this fixture's generated audio files (at least 3 seconds)."
            )
        reference_step = min(0.45, (song_duration - 2.5) / 7.76)
        performance_step = (vocal_duration - 0.75) / 7.76
        reference = words(2, reference_step)
        performance = words(0.4, performance_step)
        sf.write(work_dir / "reference.wav", voice(reference, song_duration), RATE, subtype="FLOAT")
        sf.write(work_dir / "backing.wav", backing(song_duration), RATE, subtype="FLOAT")
        candidate = Candidate(
            "fixture-verse",
            "Synthetic verse",
            reference[0]["start"],
            reference[-1]["end"],
            1,
            1,
            " ".join(TEXT),
        )
        pairs = [(i, i) for i in range(len(TEXT))]
        phrases = phrase_plan(
            [Word(**word) for word in reference],
            [Word(**word) for word in performance],
            pairs,
        )
        data = {
            "fixture": True,
            "reference": reference,
            "performance": performance,
            "matches": [
                {
                    "candidate": asdict(candidate),
                    "pairs": pairs,
                    "phrases": [asdict(phrase) for phrase in phrases],
                }
            ],
        }
        (work_dir / "analysis.json").write_text(json.dumps(data, indent=2, allow_nan=False))
        return [candidate]

    def render(self, work_dir, candidate_id, progress, **options):
        from cover_engine.pipeline import render

        result = render(work_dir, candidate_id, progress, **options)
        return replace(
            result, warning="Synthetic fixture audio and known words for interface testing."
        )


class SyntheticAssessor:
    """run the actual measurements but explicitly label synthetic known-word evidence."""

    def __init__(self):
        from cover_engine import assessment

        self.assessment = assessment
        self.VERSION = assessment.VERSION

    def assess(self, work_dir, candidate_id):
        result = self.assessment.assess(work_dir, candidate_id)
        result["summary"] = "Synthetic fixture: " + result["summary"]
        result["notes"].insert(
            0,
            "Synthetic tones/noise and known fixture words, not evidence of human singing accuracy.",
        )
        return result


def checked(response, status=200):
    if response.status_code != status:
        raise RuntimeError(f"Fixture API returned {response.status_code}: {response.text}")
    return response.json()


def upload(client, path):
    path = Path(path)
    return checked(
        client.post("/uploads", content=path.read_bytes(), headers={"X-File-Name": path.name}), 201
    )["id"]


def wait_ready(client, job_id, timeout=90):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job = checked(client.get(f"/jobs/{job_id}"))
        result = checked(client.get(f"/jobs/{job_id}/assessment"))
        if job["phase"] == "failed" or result["status"] == "failed":
            raise RuntimeError(job["error"] or result["error"])
        if job["phase"] == "ready" and result["status"] == "ready":
            return job
        if job["phase"] == "ready" and result["status"] == "not_started":
            checked(client.post(f"/jobs/{job_id}/assessment"), 202)
        time.sleep(0.05)
    raise TimeoutError(f"Synthetic fixture take {job_id} did not finish in {timeout} seconds.")


def ensure_song(client, title, original_path, take_specs):
    songs = checked(client.get("/songs"))["songs"]
    existing_song = next((song for song in songs if song["title"] == title), None)
    song_id = existing_song["id"] if existing_song else None
    take_ids = {}
    for key, name, path in take_specs:
        existing_takes = checked(client.get(f"/songs/{song_id}/takes"))["takes"] if song_id else []
        existing = next((take for take in existing_takes if take["name"] == name), None)
        if existing:
            take_ids[key] = existing["id"]
        else:
            vocal_id = upload(client, path)
            if song_id:
                job = checked(
                    client.post(
                        f"/songs/{song_id}/takes", json={"vocal_id": vocal_id, "name": name}
                    ),
                    202,
                )
            else:
                song_id = upload(client, original_path)
                job = checked(
                    client.post(
                        "/jobs",
                        json={
                            "song_id": song_id,
                            "vocal_id": vocal_id,
                            "title": title,
                            "take_name": name,
                        },
                    ),
                    202,
                )
            take_ids[key] = job["id"]
        wait_ready(client, take_ids[key])
    return song_id, take_ids


def verify(client, state):
    for job_id in [*state["take_ids"].values(), state["other_take_id"]]:
        job = checked(client.get(f"/jobs/{job_id}"))
        preview = checked(client.get(f"/jobs/{job_id}/preview?revision={job['revision']}"))
        for key in ("vocal_url", "backing_url", "original_url"):
            response = client.get(preview[key])
            if response.status_code != 200 or not response.content.startswith(b"RIFF"):
                raise RuntimeError(f"Missing playable preview: {preview[key]}")
        for kind in ("reference", "performance"):
            response = client.get(f"/jobs/{job_id}/practice-audio/{kind}.wav")
            if response.status_code != 200 or not response.content.startswith(b"RIFF"):
                raise RuntimeError(f"Missing raw fixture excerpt: {job_id}/{kind}")
    selected = state["take_ids"]["steady"]
    ranked = checked(client.get(f"/songs/{state['song_id']}/takes?selected={selected}&sort=pitch"))[
        "takes"
    ]
    noisy = next(take for take in ranked if take["id"] == state["take_ids"]["noisy"])
    if any(
        metric["available"]
        for metric in noisy["assessment"]["metrics"]
        if metric["id"].startswith("pitch_")
    ):
        raise RuntimeError("The noisy fixture must abstain from pitch measurements.")
    if ranked[-1]["id"] != noisy["id"]:
        raise RuntimeError("Unavailable pitch did not sort after measurable fixture takes.")
    job = checked(client.get(f"/jobs/{selected}"))
    exported = checked(
        client.post(
            f"/jobs/{selected}/export",
            json={"revision": job["revision"], "vocal_volume": 85.0, "backing_volume": 60.0},
        )
    )
    response = client.get(f"/jobs/{selected}/downloads/{exported['download_id']}.wav")
    if response.status_code != 200 or not response.content.startswith(b"RIFF"):
        raise RuntimeError("The synthetic fixture export was not playable.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8898)
    parser.add_argument("--root", type=Path, default=Path(".exact/practice-fixture"))
    parser.add_argument(
        "--check-only", action="store_true", help="seed and verify the API, then exit"
    )
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("--port must be between 1 and 65535")
    root = args.root.resolve()
    if root.is_dir() and any(root.iterdir()) and not (root / MARKER).is_file():
        parser.error("--root must be empty or already marked as this synthetic fixture")
    root.mkdir(parents=True, exist_ok=True)
    (root / MARKER).write_text("synthetic fixture only; contains no user recordings\n")
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from cover_engine.server import create_app

    paths = create_inputs(root)
    app = create_app(root, engine=SyntheticEngine(), assessor=SyntheticAssessor())
    origin = f"http://127.0.0.1:{args.port}"
    with TestClient(app, base_url=origin) as client:
        song_id, take_ids = ensure_song(
            client,
            TITLE,
            paths["song"],
            [
                ("steady", "Take 1 - steady", paths["steady"]),
                ("slower", "Take 2 - slower", paths["slower"]),
                ("noisy", "Take 3 - noisy", paths["noisy"]),
            ],
        )
        other_song_id, other_ids = ensure_song(
            client,
            OTHER_TITLE,
            paths["other_song"],
            [
                ("other", "Other take", paths["steady"]),
            ],
        )
        state = {
            "synthetic": True,
            "fixture_version": 1,
            "service_url": origin,
            "root": str(root),
            "state_file": str(root / "state.json"),
            "song_id": song_id,
            "song_title": TITLE,
            "take_ids": take_ids,
            "other_song_id": other_song_id,
            "other_take_id": other_ids["other"],
            "vocal_files": {key: paths[key] for key in ("steady", "slower", "noisy", "new_take")},
            "song_files": {"song": paths["song"], "other_song": paths["other_song"]},
        }
        verify(client, state)
        (root / "state.json").write_text(json.dumps(state, indent=2))
    print(json.dumps(state), flush=True)
    if not args.check_only:
        uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
