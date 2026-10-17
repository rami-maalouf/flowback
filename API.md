# engine and client boundary

The app imports an original song and a performance of its original lyrics. There is no recording, voice synthesis, or new-lyrics mode. It exports only the matched section. An ambiguous match requires a choice before rendering.

The local development service binds to 127.0.0.1:8891. Browser origins on localhost or 127.0.0.1 are permitted; other origins and hostnames are refused. No public deployment is configured. Files stay on this machine; model weights may download on first use.

## http interface

- `GET /health`: `{ "ok": true, "models_available": bool, "message": string }`.
- `POST /uploads`: raw audio bytes, `X-File-Name` with percent-encoded original filename. Returns `{ "id": string, "name": string, "duration": number }`. Validate by decoding with ffmpeg; accept at most 64 MiB to match Exact's file transport. Store under a generated id, never an input filename.
- `POST /imports/youtube`: `{ "url": string }`. Accept only single YouTube video URLs, canonicalized before download. Return upload metadata plus `source_url`. Download audio with yt-dlp, ignore external configuration, refuse playlists/live streams/videos over 20 minutes, cap files at 64 MiB and the downloader at 120 seconds. Validate with ffmpeg before publishing. Temporary files are cleaned on success or failure.
- `POST /jobs`: `{ "song_id": string, "vocal_id": string, "title"?: string }`. An optional title is trimmed and limited to 160 characters; older or unnamed covers use the original filename. Returns a job snapshot immediately. A serial background worker calls analysis. If the top candidate has coverage >= 0.75 and score exceeds the second candidate by >= 0.12, render it automatically. Otherwise wait for selection. Analysis may reject insufficient evidence instead of manufacturing a match.
- `GET /jobs`: saved covers, newest first, with title, source names, status, section times, and creation timestamp. Older jobs are migrated automatically.
- `GET /jobs/{id}/session`: saved title, source identifiers and metadata plus the job snapshot, for reopening an existing cover without processing it again.
- `POST /jobs/{id}/levels`: `{ "revision": number, "vocal_volume": number, "backing_volume": number, "client_id": uuid, "sequence": number }`. Atomically persist the live mix levels without changing the rendered audio or job revision. Ignore older or repeated sequences from the same editor; reject stale cover revisions. A failed disk write preserves the previous settings.
- `GET /jobs/{id}`: job snapshot.
- `POST /jobs/{id}/choose`: `{ "candidate_id": string }`. Valid only in `choose` or `ready`; queue rendering for a candidate belonging to this job.
- `POST /jobs/{id}/mix`: `{ "vocal_volume": number, "backing_volume": number }`. Percentages from 0 to 200, default 100; zero mutes a track. Valid only when ready. Queue rendering with the saved analysis and selected section, without separation or transcription. Return the queued job snapshot.
- `GET /jobs/{id}/preview?revision=N`: prepare/cache aligned tracks once and return `duration`, `vocal_peak`, `backing_peak`, saved volumes, a `levels_saved` flag, and `vocal_url`, `backing_url`, `original_url`. Reject stale revisions. Old covers reuse saved alignment without transcription or separation.
- `GET /jobs/{id}/preview/{revision}/{name}`: immutable, allowlisted preview WAVs.
- `POST /jobs/{id}/export`: `{ "revision": number, "vocal_volume": number, "backing_volume": number }`. Mix cached aligned tracks at the supplied levels and return a unique `download_id`. Leaves the ready job and preview playing; never reruns alignment.
- `GET /jobs/{id}/downloads/{download_id}.wav`: immutable WAV export, including byte-range downloads.
- `GET /studio/index.html?job={id}&revision=N`: shared Web Audio player for web and the Mac web view. Each buffer starts on the same AudioContext clock. Gain changes are local with a 5 ms smoothing time constant. Playback gain changes immediately. Level settings are saved separately after a short debounce, with sequential writes and a final keepalive request on close. Unacknowledged edits remain in local storage for recovery. Saved levels belong to the job revision and are shared by Mac and web on the same local engine.
- `GET /jobs/{id}/mix.wav`: rendered cover, only when ready.
- `GET /jobs/{id}/original.wav`: same section of original song for comparison, only when ready.

Audio endpoints support HTTP byte ranges and expose `Content-Range` to local browser clients. Export downloads in 256 KiB chunks to stay within Exact's response and native execution limits. Once prepared, a separate user press opens the save dialog with browser activation intact.

A job snapshot has exactly these fields: `id`, `phase`, `message`, `error`, `candidates`, `selected_id`, `start`, `end`, `duration`, `warning`, `vocal_volume`, `backing_volume`, `revision`. Volume fields default to 100, revision to zero and increment after each successful render. Older jobs acquire these defaults when restored. Other numeric fields default to zero, strings to empty, candidates to an empty list. Phases: `queued`, `separating`, `transcribing`, `matching`, `choose`, `rendering`, `ready`, `failed`. Candidates have the fields in `cover_engine.models.Candidate`. Scores are matching heuristics, not calibrated probabilities. Errors use `{ "detail": string }` with an appropriate non-2xx status. Preserve completed jobs and uploads on restart; mark interrupted jobs failed with a retry instruction. Clients use revision in preview URLs to reload changed audio and invalidate prepared exports when requesting another mix.

## audio engine interface

`cover_engine.pipeline.analyze(song: Path, vocal: Path, work_dir: Path, progress: Progress) -> list[Candidate]`

Writes reusable stems, normalized audio, word timestamps and candidate alignment information to its private work directory. Uses pretrained separation and transcription models. Model libraries are optional until a real job is requested; missing dependencies produce an actionable error. All durations and alignment data refer to seconds.

`cover_engine.pipeline.render(work_dir: Path, candidate_id: str, progress: Progress, *, vocal_volume: float = 100, backing_volume: float = 100) -> RenderResult`

Uses saved analysis and the chosen match to create `mix.wav`, `original.wav`, `preview-vocal.wav`, and `preview-backing.wav`. An optional `output_dir` receives the files; otherwise they go into `work_dir`. Preview stems use float WAV to preserve their full dynamic range. Preserve vocal pitch and continuous audio between phrases, including sounds without transcribed words. Include brief lead-in and tail without returning the whole source song. Never overwrite input files. Volumes scale each track relative to the automatic balance; peak protection can reduce the master output to prevent clipping without changing the requested balance.

## verification

Engine tests use timestamped lyrics to check partial matches, repeated choruses, transcription errors, rejected unrelated text, and timing. Synthetic audio verifies export boundaries and pitch preservation without requiring model downloads. API tests may use an injected engine to exercise HTTP/job lifecycle; these do not prove model quality. Real separation and transcription must also be exercised when dependencies are installed. Listening to user-supplied rap/singing remains required before claiming musical accuracy.

Live playback and export share the same gain rule: multiply each volume percentage by `min(1, 0.98 / (vocal_peak * vocal_volume / 100 + backing_peak * backing_volume / 100))`, with a scale of 1 when both contributions are zero. This conservative peak bound prevents clipping and preserves the requested balance. The render applies boundary fades to the stems before either path uses them.
