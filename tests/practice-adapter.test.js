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

test.each(['song/one?', 'song-two'])('removing a song clears only its draft and any matching selected session (%s)', async selectedSong => {
  const {call, files} = storageFixture();
  const oldFetch = globalThis.fetch;
  const selected = new TextEncoder().encode(JSON.stringify({song_id: selectedSong, job_id: 'selected-take'}));
  const newSong = new TextEncoder().encode(JSON.stringify({vocal_id: 'new-song-recording'}));
  files.set('app:/data/session.json', selected);
  files.set('app:/data/draft.json', newSong);
  const requests = [];
  globalThis.fetch = async (url, options) => {
    requests.push({url, method: options.method, body: JSON.parse(options.body)});
    return Response.json({ok: true});
  };
  try {
    await call('saveTakeName', ['song/one?', 'Remove this draft']);
    await call('saveTakeName', ['song-two', 'Keep this draft']);
    expect(await call('removeSong', ['song/one?'])).toEqual({ok: true, error: ''});
    expect(requests).toEqual([{url: 'http://127.0.0.1:8891/songs/song%2Fone%3F/remove', method: 'POST', body: {}}]);
    expect((await call('loadTakeDraft', ['song/one?'])).name).toBe('');
    expect((await call('loadTakeDraft', ['song-two'])).name).toBe('Keep this draft');
    expect(files.get('app:/data/draft.json')).toEqual(newSong);
    if (selectedSong === 'song/one?') {
      expect(JSON.parse(new TextDecoder().decode(files.get('app:/data/session.json'))).job_id).toBe('');
    } else {
      expect(files.get('app:/data/session.json')).toEqual(selected);
    }
  } finally { globalThis.fetch = oldFetch; }
});

test('a rejected removal leaves the selected song and every draft intact', async () => {
  const {call, files} = storageFixture();
  const oldFetch = globalThis.fetch;
  files.set('app:/data/session.json', new TextEncoder().encode(JSON.stringify({song_id: 'song-one', job_id: 'keep-me'})));
  files.set('app:/data/draft.json', new TextEncoder().encode(JSON.stringify({song_id: 'unfinished-song'})));
  await call('saveTakeName', ['song-one', 'Saved work']);
  await call('saveTakeName', ['song-two', 'Other work']);
  const before = new Map(files);
  globalThis.fetch = async () => Response.json({detail: 'Removal could not be saved'}, {status: 503});
  try {
    expect(await call('removeSong', ['song-one'])).toEqual({ok: false, error: 'Removal could not be saved'});
    expect(files).toEqual(before);
  } finally { globalThis.fetch = oldFetch; }
});

test.each(['session.json', 'take-draft-song-one.json'])('retry finishes removal after %s cleanup fails', async failedFile => {
  const {call, files, storage} = storageFixture();
  const oldFetch = globalThis.fetch;
  files.set('app:/data/session.json', new TextEncoder().encode(JSON.stringify({song_id: 'song-one', job_id: 'selected-take'})));
  await call('saveTakeName', ['song-one', 'Remove this draft']);
  await call('saveTakeName', ['song-two', 'Keep this draft']);
  let failOnce = true;
  const write = storage.fs.atomicWriteFile;
  storage.fs.atomicWriteFile = async (path, value) => {
    if (path === `app:/data/${failedFile}` && failOnce) {
      failOnce = false;
      throw new Error('Local storage unavailable');
    }
    await write(path, value);
  };
  const removedSongs = new Set();
  let requests = 0;
  globalThis.fetch = async url => {
    expect(url).toBe('http://127.0.0.1:8891/songs/song-one/remove');
    requests++;
    removedSongs.add('song-one');
    return Response.json({ok: true});
  };
  try {
    expect(await call('removeSong', ['song-one'])).toEqual({ok: false, error: 'Local storage unavailable'});
    expect(removedSongs.size).toBe(1);
    expect((await call('loadTakeDraft', ['song-one'])).name).toBe('Remove this draft');
    expect(await call('removeSong', ['song-one'])).toEqual({ok: true, error: ''});
    expect(requests).toBe(2);
    expect(removedSongs.size).toBe(1);
    expect(JSON.parse(new TextDecoder().decode(files.get('app:/data/session.json'))).job_id).toBe('');
    expect((await call('loadTakeDraft', ['song-one'])).name).toBe('');
    expect((await call('loadTakeDraft', ['song-two'])).name).toBe('Keep this draft');
  } finally { globalThis.fetch = oldFetch; }
});

test('removed songs use the same library presentation and report unavailable storage separately', async () => {
  const {call} = storageFixture();
  const oldFetch = globalThis.fetch;
  const song = {id: 'song-one', title: '', song_name: 'first-song.wav', vocal_name: 'take.wav',
    created_at: 1700000000, phase: 'ready', duration: 12, start: 1, end: 13, take_count: 2, latest_take_id: 'take-two'};
  const requests = [];
  globalThis.fetch = async url => {
    requests.push(url);
    return Response.json({songs: [song]});
  };
  try {
    const active = await call('loadLibrary');
    const removed = await call('loadRemovedLibrary');
    expect(requests).toEqual(['http://127.0.0.1:8891/songs', 'http://127.0.0.1:8891/songs/removed']);
    expect(removed).toEqual(active);
    expect(removed.covers[0]).toMatchObject({id: 'song-one', title: 'First Song', status: 'Ready to play', take_count: 2, latest_take_id: 'take-two'});
    globalThis.fetch = async () => { throw new TypeError('offline'); };
    expect(await call('loadRemovedLibrary')).toEqual({covers: [], error: 'Removed songs are unavailable while the audio engine is disconnected. They are still kept on this Mac.'});
  } finally { globalThis.fetch = oldFetch; }
});

test.each([200, 503])('restoring a song encodes its id and preserves local work (response %s)', async status => {
  const {call, files} = storageFixture();
  const oldFetch = globalThis.fetch;
  files.set('app:/data/session.json', new TextEncoder().encode(JSON.stringify({song_id: 'other-song', job_id: 'selected-take'})));
  files.set('app:/data/draft.json', new TextEncoder().encode(JSON.stringify({song_id: 'unfinished-song'})));
  await call('saveTakeName', ['song/one?', 'Keep this draft']);
  const before = new Map(files);
  globalThis.fetch = async (url, options) => {
    expect(url).toBe('http://127.0.0.1:8891/songs/song%2Fone%3F/restore');
    expect(options.method).toBe('POST');
    return Response.json(status === 200 ? {ok: true} : {detail: 'Restore unavailable'}, {status});
  };
  try {
    expect(await call('restoreSong', ['song/one?'])).toEqual(status === 200 ? {ok: true, error: ''} : {ok: false, error: 'Restore unavailable'});
    expect(files).toEqual(before);
  } finally { globalThis.fetch = oldFetch; }
});
