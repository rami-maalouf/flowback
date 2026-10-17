# vocal practice comparison

## goal and decisions

One original song owns multiple uploaded vocal takes. Preserve the redesigned native/web library, focused waveform player, existing job IDs, levels and exports. Add sortable take comparisons and evidence-based practice measurements of the raw recording, never the corrected mix. No aggregate talent score, voice imitation score, or unsupported phoneme/pronunciation grade. This is an engineering measurement tool, not validated educational or clinical assessment.

The original upload ID is the stable song identity. Group legacy jobs only when their original upload IDs match, never by filename. Existing job snapshots remain unchanged. A song projection is enough until song-level editing exists; keep metadata in jobs and uploads. Existing GET /jobs stays compatible.

## service contract

- GET /songs -> {songs:[{id: original upload id, title, song_name, created_at, take_count, latest_take_id, phase, duration, start, end, vocal_name}]}.
- GET /songs/{song_id}/takes?selected={job_id}&sort={newest|lyrics|timing|pace|pitch|range} -> {song_id,title,takes:[{id,name,vocal_name,created_at,phase,duration,start,end,selected_id,comparable,assessment}]}.
- POST /songs/{song_id}/takes {vocal_id,name?:string} -> existing job snapshot. Reuse the original and inherit the song title; name is take-specific (filename fallback). POST /jobs accepts optional take_name too.
- GET /jobs/{id}/assessment -> assessment envelope, current candidate only.
- POST /jobs/{id}/assessment -> queue/retry current candidate assessment; does not change playable job state. New ready covers automatically queue assessment; old ones can analyze on demand. Poll GET. Cache by original source, raw take, candidate, and method version; volume/render-only changes do not invalidate it.
- Assessment envelope always contains status (not_started/queued/running/ready/failed), version, candidate_id, summary, error, metrics, notes, reference_text, performance_text, words, pitch. Unavailable data is explicit, never a zero score.
- Sort missing/unavailable metrics last. Only comparable selected-reference sections and the current method version may participate in metric ranking. Comparable means both overlap fractions >=0.8; report different sections separately. Newest sorting includes all takes. Pace ranks by distance from 100%; range by absolute difference from reference; lyric agreement descending, timing/pitch difference ascending.

## measurement module contract

`cover_engine/assessment.py`: `VERSION = "practice-v1"`, `assess(work_dir: Path, candidate_id: str) -> dict`.

Reads work/analysis.json, performance.wav and reference.wav only. Never reads rendered/preview audio. Returns a ready envelope including:

- metrics: [{id,label,available,value:number|null,reference:number|null,unit,lower_is_better,detail,reason}]. Stable IDs: lyric_agreement, timing, pace, pitch_shape, pitch_range.
- reference_start/end, performance_start/end: raw measurement section bounds.
- evidence: counts/voiced coverage and timestamp provenance; unknown legacy ASR probabilities are not invented.
- reference_text/performance_text: ASR text for inspection.
- words: compact matched-word timing evidence, with reference/performance text, reference_start/performance_start and delta_ms. Prefer exact canonical token matches; never treat fuzzy candidate matching score as performance accuracy.
- pitch: {points:[{time,reference:number|null,performance:number|null}], unit:"semitones from median"}; points are word-level medians, not a claim of precise phonetic alignment. Additional useful pitch metadata is allowed.
- notes and summary: factual limitations and what is measurable, not ungrounded coaching.

Definitions: lyric_agreement = exact ordered token matches / longer transcript length *100 within matched section, explicitly ASR agreement, not articulation. Timing = median absolute difference of exact matched word starts after removing only constant start offset, in ms. Pace = reference anchor span / performance anchor span *100. Use at least six reliable exact word anchors spanning >=2 seconds; reject low-probability anchors when probabilities exist. Preserve probability optionally in new transcription words; legacy inputs work with an explicit caveat. Pitch uses local librosa pYIN with energy, voiced and probability gates; range is P95-P5 of usable semitone estimates in this excerpt, not physiological range. Pitch shape is median absolute difference between paired word-level pitch medians after removing constant register offset. Abstain for insufficient voiced words/coverage; documented thresholds are engineering defaults, not calibrated probabilities. No timbre/MFCC/speaker identity similarity.

## playback and interface contract

Library rows represent songs and show take counts. Song detail has Add take, a compact sortable list, selected take player, and readable practice analysis. Separate add-take draft storage from new-song draft. Preserve the existing cover/original switch and live volume/export behavior. Analysis shows metric units/meaning, unavailable reasons, transcript evidence and pitch movement plot below listening controls. Show that this measures the original performance before timing correction. Rhyme quality and medical/physiological range are not inferred.

Changing takes maps playback via original-song coordinates, not equal local seconds. Parent may capture position and add `at` query parameter (absolute original seconds) to new player URL; player fetches preview plus job start/end. Resume at overlapping original time; otherwise start the new section and say so. Do not autoplay a new AudioContext without user activation. One-click selection followed by Play is acceptable when browser policy requires it. No simultaneous lingering audio. Comparison to original within a take stays immediate.

## implementation units and ownership

1. Measurement module: assessment.py + test_assessment.py, focused fixtures proving shift invariance, slower timing, transposition invariance, range differences, silence/noise abstention, ASR mismatch labeling. No dependencies/config edits.
2. Service/song model: server.py, assessments.py, pipeline.py, models.py and dedicated service/cache tests. Preserve snapshot boundary, independent metric worker/failure, restart persistence, old jobs, per-take levels and original source. Reuse completed reference artifacts for later takes with configuration-aware cache identity; never trust partial cached artifacts. No UI or assessment.py edits.
3. Interface: app.contract, app.ts, app.test.contract, cover_engine/studio files and UI JS tests. Match the current design. Format unavailable metrics honestly and expose sorting and inspection. No service or measurement edits.
4. Integration/research documentation: parent-owned final tests, actual supplied recording analysis, inspect Mac/web screenshots (narrow/desktop, active/empty/unavailable), independent code review and new PR against main.

## verification and done

Use meaningful synthetic raw audio with known perturbations to prove measurement behavior, plus real supplied performance to establish execution and inspect limitations. Synthetic tests do not validate human skill scoring. Verify two takes of a single song, independent saved settings/assessments, section-sensitive sorting, switching, persistence/restart, analysis failure preserving playback, new and legacy songs, upload and export. Run full Python/Bun suites, lint/typecheck, Exact web verification and native Mac tests. No recordings, transcripts or generated audio committed. Push completed units incrementally. Research report cites primary sources and records why stronger claims were not made. New PR contains only this feature relative to the merged redesign.
