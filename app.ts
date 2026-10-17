import type { Answer, Result, Sources, Storage } from './app.contract.d.ts';

export const appId = 'com.rami.exact-cover';
export const grants = 'net.fetch http://127.0.0.1:8891\nfs.read app:/tmp/picked\nfs.read app:/data\nfs.write app:/data\nfs.read app:/tmp/output\nfs.write app:/tmp/output';

const origin = 'http://127.0.0.1:8891';
const sessionPath = 'app:/data/session.json';
const emptyJob: Result<'loadSession'>['job'] = {
  id: '', phase: 'idle', message: '', error: '', candidates: [], selected_id: '',
  start: 0, end: 0, duration: 0, warning: '', vocal_volume: 100, backing_volume: 100, revision: 0,
};
type Saved = {
  song_id: string; song_name: string; song_duration: number;
  vocal_id: string; vocal_name: string; vocal_duration: number; job_id: string;
};
const empty: Saved = {
  song_id: '', song_name: '', song_duration: 0,
  vocal_id: '', vocal_name: '', vocal_duration: 0, job_id: '',
};
const ok = { ok: true, error: '' };
const message = (error: unknown) => error instanceof Error ? error.message : String(error);

async function read(storage: Storage): Promise<Saved> {
  try {
    return { ...empty, ...JSON.parse(new TextDecoder().decode(await storage.fs.readFile(sessionPath))) };
  } catch (error) {
    if ((error as { code?: string }).code === 'ENOENT') return { ...empty };
    throw error;
  }
}

async function save(storage: Storage, session: Saved) {
  await storage.fs.atomicWriteFile(sessionPath, new TextEncoder().encode(JSON.stringify(session)));
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

const sources: Sources = {
  loadSession: async (_args, _store, storage): Promise<Result<'loadSession'>> => {
    const { job_id: ignored, ...defaults } = empty;
    let saved: Saved;
    try { saved = await read(storage); }
    catch (error) {
      return { ...defaults, job: emptyJob, online: false, models_available: false, error: message(error) };
    }
    const { job_id, ...files } = saved;
    const result: Result<'loadSession'> = { ...files, job: emptyJob, online: false, models_available: false, error: '' };
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
    const session = await read(storage);
    const next = role === 'song'
      ? { ...session, song_id: uploaded.id, song_name: uploaded.name, song_duration: uploaded.duration }
      : { ...session, vocal_id: uploaded.id, vocal_name: uploaded.name, vocal_duration: uploaded.duration };
    await save(storage, { ...next, job_id: '' });
  }),
  createCover: (_args, _store, storage) => attempt(async () => {
    const session = await read(storage);
    const job = await (await request('/jobs', post({ song_id: session.song_id, vocal_id: session.vocal_id }))).json();
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
