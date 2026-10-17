# Flowback

Flowback is the public project behind the Exact Cover Mac and web prototype.

Import an original song and a recording of yourself performing its lyrics. The app separates the backing track, matches your performance to the original words, adjusts phrase timing while preserving pitch, and exports only the section you sang.

This is a local prototype with an Exact interface for macOS and web. No recording controls, voice cloning, synthesized performance, or new-lyrics mode. iOS scaffolding exists, but an iPhone cannot use this version's loopback-only processing service.

## Run

Requires the sibling `../exact2` checkout, Bun, uv, and ffmpeg. Native builds also need Exact's Xcode/Rust prerequisites. The launcher installs locked Python dependencies and starts the audio service if needed.

```sh
bun run dev:web
bun run dev:mac
```

The web app is served at `http://127.0.0.1:8893`. Keep the launcher terminal open while using either app. Closing a window does not stop the processing service. Ctrl+C stops a service started by that launcher. A separately started service is reused and left running.

Import WAV, MP3, M4A, or FLAC files up to 64 MiB each. Provide the full original song, including its artist's vocals, and a clear recording of your own performance. Choose **Create cover**. If the words match multiple sections, select the intended section. Preview your cover or the corresponding original section, then choose **Prepare WAV**, followed by **Save WAV**. Output downloads use chunks, so uncompressed exports can exceed the upload limit.

Your audio stays on this Mac. The first processing job downloads pretrained model weights from their upstream hosts. Imports, intermediate audio, analysis, and finished exports persist under `~/.local/share/exact-cover`; model weights persist under `~/.cache/exact-cover/models`. Source files are never modified. There is no automatic cleanup yet.

## Architecture

- `app.contract`: responsive import, processing, match-selection, playback, and export interface.
- `app.ts`: Exact file transport, local session persistence, and service requests.
- `cover_engine/server.py`: local HTTP API and persistent serial job queue.
- `cover_engine/pipeline.py`: model orchestration and saved analysis.
- `cover_engine/alignment.py`: subsequence lyric matching and phrase timing.
- `cover_engine/audio.py`: decoding, pitch-preserving timing changes, mixing, and section export.

The shared boundary is documented in [API.md](API.md). The engine does not depend on Exact. A future hosted service can serve web and iOS clients, with authentication, upload policy, and deployment designed separately.

## Models and dependencies

The initial separator is `UVR_MDXNET_KARA_2.onnx` through [Audio Separator](https://github.com/nomadkaraoke/python-audio-separator). Transcription uses the multilingual `small` model through [Faster Whisper](https://github.com/SYSTRAN/faster-whisper). Pitch-preserving timing uses [Pedalboard](https://github.com/spotify/pedalboard), which includes Rubber Band. The Python lock pins a compatible dependency set; `librosa<1` and `av<17` avoid incompatibilities observed during real model integration.

Optional environment settings:

| Variable | Default |
| --- | --- |
| `EXACT_COVER_DATA_DIR` | `~/.local/share/exact-cover` |
| `EXACT_COVER_MODEL_DIR` | `~/.cache/exact-cover/models` |
| `EXACT_COVER_SEPARATOR_MODEL` | `UVR_MDXNET_KARA_2.onnx` |
| `EXACT_COVER_WHISPER_MODEL` | `small` |
| `EXACT_COVER_WHISPER_DEVICE` | `cpu` |
| `EXACT_COVER_WHISPER_COMPUTE` | `int8` |

These dependencies and model weights have their own licenses. Pedalboard is GPL-licensed; distributing a proprietary app with this engine needs a compatible licensing strategy. This prototype does not grant rights to redistribute source songs or recordings.

## Verification and current limits

```sh
uv run --extra models pytest -q
uv run --extra models ruff check cover_engine tests
uv run --extra models ruff format --check cover_engine tests
bun run typecheck
bun exact.mjs test web
bun exact.mjs test macos
```

Set `CHROME` to an installed Chromium executable if Exact cannot find Chrome. Tests use isolated temporary data. The HTTP tests inject an engine and do not measure model accuracy. The audio tests measure pitch retention, section boundaries, repeated-section selection, and preservation of input files.

Word timestamps from transcription are estimates, particularly on fast rap or sustained singing. Matching requires at least three distinct words and substantial lyrical overlap. Repeated lyrics may need your choice. Phrase stretching outside a 0.5-2 speed ratio is refused. Separation can leave traces of the original singer. The initial mix balances vocal and backing levels; it does not provide mastering, pitch correction, or manual timing editing.

A successful synthetic integration run does not establish musical quality. Validation with the user's real song and vocal recording, including listening, is still required.

The real separator and transcription models were exercised with synthetic speech over a backing track, including a partial performance and repeated sections. Both Mac and web import, processing, preview controls, and WAV export were exercised. Layout checks cover narrow and desktop sizes, light/dark appearance, and large text. These checks establish the file-processing workflow, not production readiness or musical accuracy.
