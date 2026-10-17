import { expect, test } from 'bun:test';
import { answer } from '../app.ts';

function storageFixture() {
  const files = new Map();
  const storage = {fs: {
    async readFile(path) {
      if (!files.has(path)) throw Object.assign(new Error('missing'), {code: 'ENOENT'});
      return files.get(path);
    },
    async atomicWriteFile(path, value) { files.set(path, value); },
  }};
  return {files, storage, call: (source, args = []) => answer(source, args, {}, storage, {})};
}

test('drafts keep each song’s take name and recording separate from an unfinished new song', async () => {
  const {call} = storageFixture();
  const oldFetch = globalThis.fetch;
  let upload = 0;
  globalThis.fetch = async (_url, options) => Response.json({id: `vocal-${++upload}`, name: decodeURIComponent(options.headers['X-File-Name']), duration: 12});
  try {
    expect(await call('importAudio', ['vocal', 'picked:new', 'new-song.wav', 100])).toEqual({ok: true, error: ''});
    expect(await call('saveTakeName', ['song-one', 'Clearer consonants'])).toEqual({ok: true, error: ''});
    await call('importTakeAudio', ['song-one', 'picked:one', 'first.wav', 100]);
    await call('saveTakeName', ['song-two', 'Slower pass']);
    await call('importTakeAudio', ['song-two', 'picked:two', 'second.wav', 100]);
    expect(await call('loadTakeDraft', ['song-one'])).toEqual({name: 'Clearer consonants', vocal_id: 'vocal-2', vocal_name: 'first.wav', vocal_duration: 12, error: ''});
    expect((await call('loadTakeDraft', ['song-two'])).name).toBe('Slower pass');
    expect((await call('loadDraft')).vocal_name).toBe('new-song.wav');
  } finally { globalThis.fetch = oldFetch; }
});

test('creating a take clears only its draft after the saved session has been opened', async () => {
  const {call, files} = storageFixture();
  const oldFetch = globalThis.fetch;
  const writes = [];
  globalThis.fetch = async (url, options) => {
    if (url.endsWith('/uploads')) return Response.json({id: 'voice', name: 'take.wav', duration: 8});
    if (url.endsWith('/songs/song-one/takes')) {
      writes.push(JSON.parse(options.body));
      return Response.json({id: 'new-take'});
    }
    if (url.endsWith('/jobs/new-take/session')) return Response.json({job_id: 'new-take', song_id: 'song-one', title: 'Song one', take_name: 'Second pass', vocal_id: 'voice', vocal_name: 'take.wav', vocal_duration: 8, job: {id: 'new-take'}});
    throw new Error(`unexpected request ${url}`);
  };
  try {
    await call('saveTakeName', ['song-one', 'Second pass']);
    await call('saveTakeName', ['song-two', 'Keep this draft']);
    await call('importTakeAudio', ['song-one', 'picked:one', 'take.wav', 100]);
    expect(await call('createTake', ['song-one'])).toEqual({ok: true, error: ''});
    expect(writes).toEqual([{vocal_id: 'voice', name: 'Second pass', request_id: 'voice'}]);
    expect(JSON.parse(new TextDecoder().decode(files.get('app:/data/session.json'))).job_id).toBe('new-take');
    expect((await call('loadTakeDraft', ['song-one'])).vocal_id).toBe('');
    expect((await call('loadTakeDraft', ['song-two'])).name).toBe('Keep this draft');
  } finally { globalThis.fetch = oldFetch; }
});

test('a failed take creation keeps the draft and selected saved song intact', async () => {
  const {call, files} = storageFixture();
  const oldFetch = globalThis.fetch;
  files.set('app:/data/session.json', new TextEncoder().encode(JSON.stringify({job_id: 'keep-me'})));
  globalThis.fetch = async () => Response.json({detail: 'Engine unavailable'}, {status: 503});
  try {
    await call('saveTakeName', ['song-one', 'Saved work']);
    expect(await call('createTake', ['song-one'])).toEqual({ok: false, error: 'Engine unavailable'});
    expect((await call('loadTakeDraft', ['song-one'])).name).toBe('Saved work');
    expect(JSON.parse(new TextDecoder().decode(files.get('app:/data/session.json'))).job_id).toBe('keep-me');
  } finally { globalThis.fetch = oldFetch; }
});

test.each(['session request', 'creation response', 'session storage'])('retrying after a failed %s reuses the accepted take until a new recording is uploaded', async failure => {
  const {call, files, storage} = storageFixture();
  const oldFetch = globalThis.fetch;
  const write = storage.fs.atomicWriteFile;
  const jobs = new Map();
  const requests = [];
  let uploads = 0;
  let failOnce = true;
  files.set('app:/data/session.json', new TextEncoder().encode(JSON.stringify({job_id: 'previous-take'})));
  storage.fs.atomicWriteFile = async (path, value) => {
    if (failure === 'session storage' && path === 'app:/data/session.json' && failOnce) {
      failOnce = false;
      throw new Error('session storage unavailable');
    }
    await write(path, value);
  };
  globalThis.fetch = async (url, options) => {
    if (url.endsWith('/uploads')) return Response.json({id: `voice-${++uploads}`, name: 'take.wav', duration: 8});
    if (url.endsWith('/songs/song-one/takes')) {
      const body = JSON.parse(options.body);
      requests.push(body);
      const key = body.request_id || `unkeyed-${requests.length}`;
      if (!jobs.has(key)) jobs.set(key, {id: `take-${jobs.size + 1}`, vocal_id: body.vocal_id});
      if (failure === 'creation response' && failOnce) {
        failOnce = false;
        throw new TypeError('connection lost after accepted creation');
      }
      return Response.json({id: jobs.get(key).id});
    }
    const job = [...jobs.values()].find(job => url.endsWith(`/jobs/${job.id}/session`));
    if (job) {
      if (failure === 'session request' && failOnce) {
        failOnce = false;
        throw new TypeError('session request disconnected');
      }
      return Response.json({job_id: job.id, song_id: 'song-one', vocal_id: job.vocal_id, job: {id: job.id}});
    }
    throw new Error(`unexpected request ${url}`);
  };
  try {
    await call('saveTakeName', ['song-one', 'Second pass']);
    await call('importTakeAudio', ['song-one', 'picked:one', 'take.wav', 100]);
    expect((await call('createTake', ['song-one'])).ok).toBe(false);
    expect(jobs.size).toBe(1);
    expect((await call('loadTakeDraft', ['song-one'])).vocal_id).toBe('voice-1');
    expect(JSON.parse(new TextDecoder().decode(files.get('app:/data/session.json'))).job_id).toBe('previous-take');

    await call('saveTakeName', ['song-one', 'Renamed before retry']);
    expect(await call('createTake', ['song-one'])).toEqual({ok: true, error: ''});
    expect(jobs.size).toBe(1);
    expect(requests.map(body => body.request_id)).toEqual(['voice-1', 'voice-1']);
    expect(JSON.parse(new TextDecoder().decode(files.get('app:/data/session.json'))).job_id).toBe('take-1');
    expect((await call('loadTakeDraft', ['song-one'])).vocal_id).toBe('');

    await call('importTakeAudio', ['song-one', 'picked:two', 'take.wav', 100]);
    expect(await call('createTake', ['song-one'])).toEqual({ok: true, error: ''});
    expect(jobs.size).toBe(2);
    expect(requests[2].request_id).toBe('voice-2');
    expect(JSON.parse(new TextDecoder().decode(files.get('app:/data/session.json'))).job_id).toBe('take-2');
  } finally { globalThis.fetch = oldFetch; }
});
