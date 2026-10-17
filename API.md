# engine and client boundary

The app imports an original song and a performance of its original lyrics. There is no recording, voice synthesis, or new-lyrics mode. It exports only the matched section. An ambiguous match requires a choice before rendering.

The local development service binds to 127.0.0.1:8891. Browser origins on localhost or 127.0.0.1 are permitted; other origins and hostnames are refused. No public deployment is configured. Files stay on this machine; model weights may download on first use.

## http interface

- `GET /health`: `{ "ok": true, "models_available": bool, "message": string }`.
- `POST /uploads`: raw audio bytes, `X-File-Name` with percent-encoded original filename. Returns `{ "id": string, "name": string, "duration": number }`. Validate by decoding with ffmpeg; accept at most 64 MiB to match Exact's file transport. Store under a generated id, never an input filename.
- `POST /jobs`: `{ "song_id": string, "vocal_id": string }`. Returns a job snapshot immediately. A serial background worker calls analysis. If the top candidate has coverage >= 0.75 and score exceeds the second candidate by >= 0.12, render it automatically. Otherwise wait for selection. Analysis may reject insufficient evidence instead of manufacturing a match.
- `GET /jobs/{id}`: job snapshot.
- `POST /jobs/{id}/choose`: `{ "candidate_id": string }`. Valid only in `choose` or `ready`; queue rendering for a candidate belonging to this job.
- `GET /jobs/{id}/mix.wav`: rendered cover, only when ready.
- `GET /jobs/{id}/original.wav`: same section of original song for comparison, only when ready.

Audio endpoints support HTTP byte ranges and expose `Content-Range` to local browser clients. Export downloads in 256 KiB chunks to stay within Exact's response and native execution limits. Once prepared, a separate user press opens the save dialog with browser activation intact.

A job snapshot has exactly these fields: `id`, `phase`, `message`, `error`, `candidates`, `selected_id`, `start`, `end`, `duration`, `warning`. Phases: `queued`, `separating`, `transcribing`, `matching`, `choose`, `rendering`, `ready`, `failed`. Numeric fields default to zero, strings to empty, candidates to an empty list. Candidates have the fields in `cover_engine.models.Candidate`. Scores are matching heuristics, not calibrated probabilities. Errors use `{ "detail": string }` with an appropriate non-2xx status. Preserve completed jobs and uploads on restart; mark interrupted jobs failed with a retry instruction.

## audio engine interface

`cover_engine.pipeline.analyze(song: Path, vocal: Path, work_dir: Path, progress: Progress) -> list[Candidate]`

Writes reusable stems, normalized audio, word timestamps and candidate alignment information to its private work directory. Uses pretrained separation and transcription models. Model libraries are optional until a real job is requested; missing dependencies produce an actionable error. All durations and alignment data refer to seconds.

`cover_engine.pipeline.render(work_dir: Path, candidate_id: str, progress: Progress) -> RenderResult`

Uses saved analysis and the chosen match to create `mix.wav` and `original.wav` in that directory. Preserve vocal pitch and phrasing; do not stretch individual words independently. Include brief lead-in and tail without returning the whole source song. Never overwrite input files.

## verification

Engine tests use timestamped lyrics to check partial matches, repeated choruses, transcription errors, rejected unrelated text, and timing. Synthetic audio verifies export boundaries and pitch preservation without requiring model downloads. API tests may use an injected engine to exercise HTTP/job lifecycle; these do not prove model quality. Real separation and transcription must also be exercised when dependencies are installed. Listening to user-supplied rap/singing remains required before claiming musical accuracy.
