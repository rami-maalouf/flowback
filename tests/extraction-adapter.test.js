import {expect, test} from 'bun:test';
import {answer} from '../app.ts';

function fixture() {
  const files = new Map();
  const storage = {fs: {
    async readFile(path) {
      if (!files.has(path)) throw Object.assign(new Error('missing'), {code: 'ENOENT'});
      return files.get(path);
    },
    async mkdir() {},
    async atomicWriteFile(path, bytes) { files.set(path, bytes); },
    async appendFile(path, bytes) { files.set(path, new Uint8Array([...files.get(path), ...bytes])); },
  }};
  return {files, call: (source, args = []) => answer(source, args, {}, storage, {})};
}

test('extraction imports and creation preserve unfinished covers and clear only their own draft', async () => {
  const {files, call} = fixture();
  const cover = new TextEncoder().encode(JSON.stringify({song_id: 'unfinished', vocal_id: 'voice'}));
  files.set('app:/data/draft.json', cover);
  const oldFetch = globalThis.fetch;
  globalThis.fetch = async (url, options) => {
    if (url.endsWith('/uploads')) return Response.json({id: 'song', name: 'original.wav', duration: 30});
    expect(url.endsWith('/extractions')).toBe(true);
    expect(JSON.parse(options.body)).toEqual({song_id: 'song', title: 'Original'});
    return Response.json({id: 'extraction'});
  };
  try {
    expect(await call('importExtractionAudio', ['picked:song', 'original.wav', 100])).toEqual({ok: true, error: ''});
    expect((await call('loadExtractionDraft')).song_id).toBe('song');
    expect(await call('createExtraction', ['Original'])).toEqual({ok: true, error: '', id: 'extraction'});
    expect((await call('loadExtractionDraft')).song_id).toBe('');
    expect(files.get('app:/data/draft.json')).toEqual(cover);
  } finally { globalThis.fetch = oldFetch; }
});

test('failed extraction creation retains the imported original', async () => {
  const {call} = fixture();
  const oldFetch = globalThis.fetch;
  globalThis.fetch = async url => url.endsWith('/uploads')
    ? Response.json({id: 'song', name: 'original.wav', duration: 30})
    : Response.json({detail: 'Try again later'}, {status: 503});
  try {
    await call('importExtractionAudio', ['picked:song', 'original.wav', 100]);
    expect(await call('createExtraction', ['Original'])).toEqual({ok: false, error: 'Try again later', id: ''});
    expect((await call('loadExtractionDraft')).song_id).toBe('song');
  } finally { globalThis.fetch = oldFetch; }
});

test('adding vocals to an extraction uses its original without changing cover drafts', async () => {
  const {files, call} = fixture();
  const oldFetch = globalThis.fetch;
  globalThis.fetch = async () => Response.json({id: 'extract', phase: 'ready', song_id: 'song', title: 'Saved song', song_name: 'original.wav', duration: 30});
  try {
    expect(await call('openExtractionCover', ['extract'])).toEqual({ok: true, error: ''});
    const session = JSON.parse(new TextDecoder().decode(files.get('app:/data/session.json')));
    expect(session).toMatchObject({song_id: 'song', title: 'Saved song', song_duration: 30, job_id: '', vocal_id: ''});
    expect(files.has('app:/data/draft.json')).toBe(false);
  } finally { globalThis.fetch = oldFetch; }
});

test.each(['instrumental', 'vocals'])('stem downloads assemble validated ranges for %s', async kind => {
  const {files, call} = fixture();
  const oldFetch = globalThis.fetch;
  const bytes = new Uint8Array(300000).map((_, i) => i % 251);
  globalThis.fetch = async (url, options) => {
    expect(url).toBe(`http://127.0.0.1:8891/extractions/extract/audio/${kind}.wav`);
    const [, from, to] = /bytes=(\d+)-(\d+)/.exec(options.headers.Range);
    const end = Math.min(Number(to), bytes.length - 1);
    return new Response(bytes.slice(Number(from), end + 1), {status: 206, headers: {'Content-Range': `bytes ${from}-${end}/${bytes.length}`}});
  };
  try {
    expect(await call('prepareStemExport', ['extract', kind])).toEqual({ok: true, error: ''});
    expect(files.get(`app:/tmp/output/${kind}.wav`)).toEqual(bytes);
  } finally { globalThis.fetch = oldFetch; }
});

test('an incomplete stem download never reports success', async () => {
  const {call} = fixture();
  const oldFetch = globalThis.fetch;
  globalThis.fetch = async () => new Response(new Uint8Array(3), {status: 206, headers: {'Content-Range': 'bytes 0-9/10'}});
  try { expect((await call('prepareStemExport', ['extract', 'instrumental'])).ok).toBe(false); }
  finally { globalThis.fetch = oldFetch; }
});
