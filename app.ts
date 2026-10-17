import type { Answer, Result, Sources, Storage } from './app.contract.d.ts';

export const appId = 'com.rami.exact-cover';
export const grants = 'net.fetch http://127.0.0.1:8891\nfs.read app:/tmp/picked\nfs.read app:/data\nfs.write app:/data\nfs.read app:/tmp/output\nfs.write app:/tmp/output';

const origin = 'http://127.0.0.1:8891';
const sessionPath = 'app:/data/session.json';
const draftPath = 'app:/data/draft.json';
const emptyJob: Result<'loadSession'>['job'] = {
  id: '', phase: 'idle', message: '', error: '', candidates: [], selected_id: '',
  start: 0, end: 0, duration: 0, warning: '', vocal_volume: 100, backing_volume: 100, revision: 0,
};
type Saved = {
  title: string;
  take_name: string;
  song_id: string; song_name: string; song_duration: number;
  vocal_id: string; vocal_name: string; vocal_duration: number; job_id: string;
};
const empty: Saved = {
  title: '', take_name: '',
  song_id: '', song_name: '', song_duration: 0,
  vocal_id: '', vocal_name: '', vocal_duration: 0, job_id: '',
};
const ok = { ok: true, error: '' };
const message = (error: unknown) => error instanceof Error ? error.message : String(error);

async function read(storage: Storage, path = sessionPath): Promise<Saved> {
  try {
    return { ...empty, ...JSON.parse(new TextDecoder().decode(await storage.fs.readFile(path))) };
  } catch (error) {
    if ((error as { code?: string }).code === 'ENOENT') return { ...empty };
    throw error;
  }
}

async function save(storage: Storage, session: Saved, path = sessionPath) {
  await storage.fs.atomicWriteFile(path, new TextEncoder().encode(JSON.stringify(session)));
}

async function request(path: string, options?: RequestInit) {
  let response: Response;
  try { response = await fetch(`${origin}${path}`, options); }
  catch { throw new Error('Could not reach the local audio engine. Check that it is running, then try again.'); }
  if (!response.ok) {
    let detail = `The audio engine returned an error (${response.status}).`;
    try { detail = String((await response.json()).detail || detail); } catch {}
    throw new Error(detail);
  }
  return response;
}

const post = (value: object): RequestInit => ({
  method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(value),
});

async function attempt(run: () => Promise<void>): Promise<Result<'createCover'>> {
  try { await run(); return ok; }
  catch (error) { return { ok: false, error: message(error) }; }
}

function displayTitle(title: string, file: string) {
  if (title && title !== file) return title;
  return (file || 'Untitled song').replace(/\.(wav|mp3|m4a|flac|ogg|aac|aiff|webm)$/i, '')
    .split(/[-_ ]+/).map(word => word.charAt(0).toUpperCase() + word.slice(1)).join(' ');
}

const takeDraftPath = (songId: string) => `app:/data/take-draft-${encodeURIComponent(songId)}.json`;
const phaseLabels: Record<string, string> = {ready: 'Ready to play', choose: 'Choose a section', failed: 'Needs attention'};
const analysisLabels: Record<string, string> = {ready: 'Analysis ready', queued: 'Analysis queued', running: 'Analyzing', failed: 'Analysis needs attention'};
const phaseLabel = (phase: string) => phaseLabels[phase] || 'Processing';
const savedAt = (createdAt: number) => new Date(createdAt * 1000).toLocaleString(undefined, {month: 'short', day: 'numeric'});
type Measurement = {id: string; available: boolean; value: number | null; unit: string};
function measurementText(metric?: Measurement) {
  if (!metric?.available || typeof metric.value !== 'number' || !Number.isFinite(metric.value)) return 'Unavailable';
  const value = metric.unit === 'ms' ? Math.round(metric.value) : Math.round(metric.value * 10) / 10;
  const unit = metric.unit === 'semitones' ? 'st' : metric.unit;
  return `${value}${unit === '%' ? '' : ' '}${unit}`;
}

const sources: Sources = {
  loadDraft: async (_args, _store, storage) => {
    const { title: ignoredTitle, job_id: ignoredJob, take_name: ignoredTake, ...files } = empty;
    try {
      const { title, job_id, take_name, ...draft } = await read(storage, draftPath);
      return { ...draft, error: '' };
    } catch (error) { return { ...files, error: message(error) }; }
  },
  loadLibrary: async (): Promise<Result<'loadLibrary'>> => {
    try {
      const { songs } = await (await request('/songs')).json();
      return { error: '', covers: songs.map((cover: {
        id: string; title: string; song_name: string; vocal_name: string; created_at: number;
        phase: string; duration: number; start: number; end: number; take_count: number; latest_take_id: string;
      }) => ({
        take_count: cover.take_count, latest_take_id: cover.latest_take_id,
        phase: cover.phase, id: cover.id, title: displayTitle(cover.title, cover.song_name), song_name: cover.song_name, vocal_name: cover.vocal_name,
        saved_at: savedAt(cover.created_at),
        status: phaseLabel(cover.phase),
        duration: cover.duration, start: cover.start, end: cover.end,
      })) };
    } catch {
      return { covers: [], error: 'Saved covers are unavailable while the audio engine is disconnected. They are still kept on this Mac.' };
    }
  },
  loadTakes: async ([songId, selected, sort]) => {
    if (!songId) return {takes: [], error: ''};
    try {
      const {takes} = await (await request(`/songs/${encodeURIComponent(songId)}/takes?selected=${encodeURIComponent(selected)}&sort=${encodeURIComponent(sort)}`)).json();
      return {error: '', takes: takes.map((take: {
        id: string; name: string; vocal_name: string; created_at: number; phase: string;
        duration: number; start: number; end: number; comparable: boolean;
        assessment: {status: string; metrics: Measurement[]};
      }) => {
        const metrics = take.assessment.metrics || [];
        return {id: take.id, name: take.name || take.vocal_name, saved_at: savedAt(take.created_at),
          phase: take.phase, status: phaseLabel(take.phase), duration: take.duration, start: take.start, end: take.end,
          comparable: take.comparable,
          assessment_status: take.assessment.status,
          analysis_status: analysisLabels[take.assessment.status] || 'Not analyzed',
          lyrics: measurementText(metrics.find(metric => metric.id === 'lyric_agreement')),
          timing: measurementText(metrics.find(metric => metric.id === 'timing')),
          pace: measurementText(metrics.find(metric => metric.id === 'pace')),
          pitch: measurementText(metrics.find(metric => metric.id === 'pitch_shape')),
          range: measurementText(metrics.find(metric => metric.id === 'pitch_range'))};
      })};
    } catch (error) { return {takes: [], error: message(error)}; }
  },
  loadTakeDraft: async ([songId], _store, storage) => {
    try {
      const draft = songId ? await read(storage, takeDraftPath(songId)) : empty;
      return {name: draft.take_name, vocal_id: draft.vocal_id, vocal_name: draft.vocal_name, vocal_duration: draft.vocal_duration, error: ''};
    } catch (error) { return {name: '', vocal_id: '', vocal_name: '', vocal_duration: 0, error: message(error)}; }
  },
  saveTakeName: ([songId, name], _store, storage) => attempt(async () => {
    const path = takeDraftPath(songId);
    await save(storage, {...await read(storage, path), take_name: name.slice(0, 160)}, path);
  }),
  importTakeAudio: ([songId, path, name, size], _store, storage) => attempt(async () => {
    if (size > 64 * 1024 * 1024) throw new Error('Choose an audio file smaller than 64 MB.');
    const uploaded = await (await request('/uploads', {method: 'POST', headers: {'X-File-Name': encodeURIComponent(name)}, exactBodyFrom: path} as RequestInit)).json();
    const draftFile = takeDraftPath(songId);
    const draft = await read(storage, draftFile);
    await save(storage, {...draft, vocal_id: uploaded.id, vocal_name: uploaded.name, vocal_duration: uploaded.duration}, draftFile);
  }),
  createTake: ([songId], _store, storage) => attempt(async () => {
    const path = takeDraftPath(songId);
    const draft = await read(storage, path);
    const job = await (await request(`/songs/${encodeURIComponent(songId)}/takes`, post({vocal_id: draft.vocal_id, name: draft.take_name.trim()}))).json();
    const {job: ignored, ...session} = await (await request(`/jobs/${encodeURIComponent(job.id)}/session`)).json();
    await save(storage, {...empty, ...session});
    await save(storage, empty, path);
  }),
  openCover: ([jobId], _store, storage) => attempt(async () => {
    const { job: ignored, ...session } = await (await request(`/jobs/${encodeURIComponent(jobId)}/session`)).json();
    await save(storage, { ...empty, ...session, title: session.title || session.song_name });
  }),
  loadSession: async (_args, _store, storage): Promise<Result<'loadSession'>> => {
    const { job_id: ignored, ...defaults } = empty;
    let saved: Saved;
    try { saved = await read(storage); }
    catch (error) {
      return { ...defaults, studio_origin: origin, job: emptyJob, online: false, models_available: false, error: message(error) };
    }
    const { job_id, ...files } = saved;
    const result: Result<'loadSession'> = { ...files, studio_origin: origin, title: displayTitle(files.title, files.song_name), job: emptyJob, online: false, models_available: false, error: '' };
    try {
      const health = await (await request('/health')).json();
      result.online = health.ok === true;
      result.models_available = health.models_available === true;
    } catch { return result; }
    if (job_id) {
      try { result.job = await (await request(`/jobs/${encodeURIComponent(job_id)}`)).json(); }
      catch (error) { result.error = message(error); }
    }
    return result;
  },
  importAudio: ([role, path, name, size], _store, storage) => attempt(async () => {
    if (size > 64 * 1024 * 1024) throw new Error('Choose an audio file smaller than 64 MB. A compressed M4A or MP3 keeps longer songs within this limit.');
    if (role !== 'song' && role !== 'vocal') throw new Error('Choose a song or vocal file.');
    const options = { method: 'POST', headers: { 'X-File-Name': encodeURIComponent(name) }, exactBodyFrom: path };
    const uploaded = await (await request('/uploads', options)).json();
    const session = await read(storage, draftPath);
    const next = role === 'song'
      ? { ...session, song_id: uploaded.id, song_name: uploaded.name, song_duration: uploaded.duration }
      : { ...session, vocal_id: uploaded.id, vocal_name: uploaded.name, vocal_duration: uploaded.duration };
    await save(storage, { ...next, job_id: '' }, draftPath);
  }),
  importYoutubeAudio: ([url], _store, storage) => attempt(async () => {
    const uploaded = await (await request('/imports/youtube', post({ url }))).json();
    const draft = await read(storage, draftPath);
    await save(storage, { ...draft, song_id: uploaded.id, song_name: uploaded.name,
      song_duration: uploaded.duration }, draftPath);
  }),
  createCover: ([title], _store, storage) => attempt(async () => {
    const session = await read(storage, draftPath);
    const name = title.trim() || session.song_name.slice(0, 160);
    if (name.length > 160) throw new Error('Keep the song name within 160 characters.');
    const job = await (await request('/jobs', post({ song_id: session.song_id, vocal_id: session.vocal_id, title: name }))).json();
    await save(storage, { ...session, title: name, job_id: job.id });
    await save(storage, empty, draftPath);
  }),
  retryCover: (_args, _store, storage) => attempt(async () => {
    const session = await read(storage);
    const job = await (await request('/jobs', post({ song_id: session.song_id,
      vocal_id: session.vocal_id, title: session.title.slice(0, 160), take_name: session.take_name }))).json();
    await save(storage, { ...session, job_id: job.id });
  }),
  chooseMatch: ([candidate_id], _store, storage) => attempt(async () => {
    const session = await read(storage);
    await request(`/jobs/${encodeURIComponent(session.job_id)}/choose`, post({ candidate_id }));
  }),
  prepareExport: ([jobId, revision, vocal_volume, backing_volume], _store, storage) => attempt(async () => {
    const session = await read(storage);
    if (session.job_id !== jobId) throw new Error('The cover changed. Prepare the current mix again.');
    const checkMix = async () => {
      const job = await (await request(`/jobs/${encodeURIComponent(jobId)}`)).json();
      if (job.phase !== 'ready' || job.revision !== revision) {
        throw new Error('The mix changed. Wait for it to finish, then prepare the WAV again.');
      }
    };
    await checkMix();
    const { download_id } = await (await request(`/jobs/${encodeURIComponent(jobId)}/export`,
      post({ revision, vocal_volume, backing_volume }))).json();
    if (!/^[a-f0-9]{32}$/.test(download_id)) throw new Error('The export was invalid. Try again.');
    await storage.fs.mkdir('app:/tmp/output');
    const path = 'app:/tmp/output/cover.wav';
    let offset = 0;
    let total = 0;
    do {
      const response = await request(`/jobs/${encodeURIComponent(jobId)}/downloads/${download_id}.wav`, {
        headers: { Range: `bytes=${offset}-${offset + 256 * 1024 - 1}` },
      });
      const range = /^bytes (\d+)-(\d+)\/(\d+)$/.exec(response.headers.get('Content-Range') || '');
      if (response.status !== 206 || !range || Number(range[1]) !== offset
        || Number(range[2]) < offset || Number(range[3]) <= Number(range[2])
        || (total && total !== Number(range[3]))) {
        throw new Error('The audio download was interrupted. Try exporting again.');
      }
      const bytes = new Uint8Array(await response.arrayBuffer());
      if (bytes.length !== Number(range[2]) - offset + 1) {
        throw new Error('The audio download was incomplete. Try exporting again.');
      }
      if (offset === 0) await storage.fs.atomicWriteFile(path, bytes);
      else await storage.fs.appendFile(path, bytes);
      offset += bytes.length;
      total = Number(range[3]);
    } while (offset < total);
    await checkMix();
  }),
};

export const answer: Answer = (source, args, store, storage, native) =>
  sources[source](args, store, storage, native);
