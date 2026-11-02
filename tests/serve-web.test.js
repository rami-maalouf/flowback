import { afterAll, expect, test } from 'bun:test';
import { mkdirSync, mkdtempSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { config, webHandler } from '../deploy/serve-web.mjs';

const root = mkdtempSync(join(tmpdir(), 'flowback-web-'));
const routes = (list) => JSON.stringify({routes: [...list, {name: 'notfound', pattern: '', notfound: true}]});
mkdirSync(join(root, 'assets'));
writeFileSync(join(root, 'shell.html'), '<main>shell</main>');
writeFileSync(join(root, 'app.js'), 'export {};');
writeFileSync(join(root, 'assets', 'icon.txt'), 'icon');
writeFileSync(join(root, 'routes.json'), routes([
  {name: 'home', pattern: '/'}, {name: 'cover', pattern: '/cover'}, {name: 'take', pattern: '/cover/:take'},
]));

// a stand-in engine that says what reached it.
const engine = Bun.serve({hostname: '127.0.0.1', port: 0, fetch: async (request) => {
  const url = new URL(request.url);
  if (url.pathname === '/jobs/one/mix.wav') {
    return new Response('ange', {status: 206, headers: {'content-range': 'bytes 1-4/9', 'accept-ranges': 'bytes'}});
  }
  return Response.json({
    method: request.method, path: url.pathname + url.search, host: request.headers.get('host'),
    origin: request.headers.get('origin'), authorization: request.headers.get('authorization'),
    range: request.headers.get('range'), body: request.body ? (await request.arrayBuffer()).byteLength : 0,
  });
}});
const engineOrigin = `http://127.0.0.1:${engine.port}`;
afterAll(() => { engine.stop(true); rmSync(root, {recursive: true, force: true}); });

const serve = webHandler(root, {engine: engineOrigin});
const get = (path, headers = {}, handler = serve) => handler(new Request(`http://127.0.0.1${path}`, {headers}));
const basic = (name, password) => `Basic ${Buffer.from(`${name}:${password}`).toString('base64')}`;

test('files are served as they are and routes get the shell', async () => {
  const file = await get('/assets/icon.txt');
  expect(file.status).toBe(200);
  expect(await file.text()).toBe('icon');
  for (const path of ['/', '/cover', '/cover/first']) {
    const page = await get(path);
    expect([path, page.status]).toEqual([path, 200]);
    expect(await page.text()).toBe('<main>shell</main>');
  }
});

test('an unknown page is a 404 with the shell, so the client draws its notfound route', async () => {
  for (const path of ['/missing', '/cover/', '/cover/first/extra']) {
    const page = await get(path);
    expect([path, page.status]).toEqual([path, 404]);
    expect(await page.text()).toBe('<main>shell</main>');
  }
});

test('paths cannot leave the build and only reads are allowed', async () => {
  expect((await get('/../../etc/passwd')).status).not.toBe(200);
  expect((await get('/%2e%2e/%2e%2e/etc/passwd')).status).not.toBe(200);
  expect((await get('/..%2f..%2fetc%2fpasswd')).status).toBe(404);
  expect((await get('/%E0%A4%A')).status).toBe(400);
  expect((await serve(new Request('http://127.0.0.1/app.js', {method: 'POST'}))).status).toBe(405);
});

test('an unchanged file revalidates with its etag, and every answer carries the security headers', async () => {
  const first = await get('/app.js');
  expect(first.headers.get('cache-control')).toBe('no-cache');
  expect(first.headers.get('x-frame-options')).toBe('SAMEORIGIN');
  expect(first.headers.get('cross-origin-resource-policy')).toBe('same-origin');
  expect(first.headers.get('strict-transport-security')).toBeNull();
  const again = await get('/app.js', {'if-none-match': first.headers.get('etag')});
  expect(again.status).toBe(304);
});

test('engine paths reach the engine as loopback requests, with the body, query and range', async () => {
  const reply = await serve(new Request('http://flowback.example/uploads?x=1', {
    method: 'POST', body: new Uint8Array(70_000), headers: {
      origin: 'http://flowback.example', authorization: 'Basic abc', 'x-forwarded-for': '203.0.113.9',
    },
  }));
  expect(reply.status).toBe(200);
  expect(await reply.json()).toEqual({
    method: 'POST', path: '/uploads?x=1', host: `127.0.0.1:${engine.port}`, origin: null, authorization: null,
    range: null, body: 70_000,
  });
  const ranged = await get('/jobs/one/mix.wav', {range: 'bytes=1-4'});
  expect([ranged.status, ranged.headers.get('content-range'), await ranged.text()]).toEqual([206, 'bytes 1-4/9', 'ange']);
});

test('only the site itself may post to the engine', async () => {
  const handler = webHandler(root, {engine: engineOrigin, origin: 'https://flowback.example'});
  const post = (from) => handler(new Request('http://flowback.example/jobs', {method: 'POST', body: '{}', headers: {origin: from}}));
  expect((await post('https://evil.example')).status).toBe(403);
  expect((await post('https://flowback.example')).status).toBe(200);
  expect((await post('http://flowback.example')).status).toBe(200);
  expect((await get('/app.js', {}, handler)).headers.get('strict-transport-security')).toBe('max-age=31536000');
});

test('a stopped engine is a 502 the app can show', async () => {
  const handler = webHandler(root, {engine: 'http://127.0.0.1:9'});
  const reply = await get('/health', {}, handler);
  expect(reply.status).toBe(502);
  expect((await reply.json()).detail).toBe('The audio engine is not running.');
});

test('with users, every request needs the password, and wrong ones are limited per client', async () => {
  let clock = 0;
  const users = new Map([['rami', await Bun.password.hash('right horse')]]);
  const handler = webHandler(root, {engine: engineOrigin, users, now: () => clock});
  const asked = await get('/', {}, handler);
  expect(asked.status).toBe(401);
  expect(asked.headers.get('www-authenticate')).toBe('Basic realm="Flowback", charset="UTF-8"');
  expect((await get('/health', {}, handler)).status).toBe(401);
  expect((await get('/', {authorization: basic('rami', 'right horse')}, handler)).status).toBe(200);
  expect((await get('/health', {authorization: basic('rami', 'right horse')}, handler)).status).toBe(200);
  expect((await get('/', {authorization: basic('someone', 'right horse')}, handler)).status).toBe(401);

  const from = (client, password) => get('/', {authorization: basic('rami', password), 'x-forwarded-for': `1.2.3.4, ${client}`}, handler);
  for (let i = 0; i < 10; i++) expect((await from('198.51.100.1', `wrong ${i}`)).status).toBe(401);
  expect((await from('198.51.100.1', 'wrong again')).status).toBe(429);
  expect((await from('198.51.100.2', 'right horse')).status).toBe(200);
  clock += 10 * 60 * 1000;
  expect((await from('198.51.100.1', 'right horse')).status).toBe(200);
});

test('a browser polling with an old password costs one check, and checks run one at a time', async () => {
  const users = new Map([['rami', await Bun.password.hash('right horse')]]);
  const verify = Bun.password.verify;
  let running = 0, most = 0, calls = 0;
  Bun.password.verify = async (...args) => {
    calls++; most = Math.max(most, ++running);
    try { return await verify(...args); } finally { running--; }
  };
  try {
    const handler = webHandler(root, {engine: engineOrigin, users});
    const stale = {authorization: basic('rami', 'old horse'), 'x-forwarded-for': '198.51.100.7'};
    const polls = await Promise.all(Array.from({length: 6}, () => get('/health', stale, handler)));
    expect(polls.map(reply => reply.status)).toEqual([401, 401, 401, 401, 401, 401]);
    for (let i = 0; i < 20; i++) expect((await get('/health', stale, handler)).status).toBe(401);
    expect(calls).toBe(1);
    const fresh = await Promise.all(['a', 'b', 'c'].map(word => get('/', {authorization: basic('rami', `wrong ${word}`)}, handler)));
    expect(fresh.map(reply => reply.status)).toEqual([401, 401, 401]);
    expect(most).toBe(1);
    expect((await get('/', {authorization: basic('rami', 'right horse'), 'x-forwarded-for': '198.51.100.7'}, handler)).status).toBe(200);
  } finally {
    Bun.password.verify = verify;
  }
});

test('a public origin needs a password, a users file needs hashes, and no web route may shadow the engine', async () => {
  const users = join(root, 'users');
  expect(() => config({FLOWBACK_ORIGIN: 'https://flowback.example'})).toThrow('A public origin needs a password');
  writeFileSync(users, 'rami:plain-text\n');
  expect(() => config({FLOWBACK_USERS: users})).toThrow('not an argon2 or bcrypt hash');
  writeFileSync(users, `rami:${await Bun.password.hash('x')}\n\n`);
  expect(config({FLOWBACK_ORIGIN: 'https://flowback.example', FLOWBACK_USERS: users}).users.has('rami')).toBe(true);
  expect(config({}).users.size).toBe(0);

  const clashing = mkdtempSync(join(tmpdir(), 'flowback-clash-'));
  writeFileSync(join(clashing, 'routes.json'), routes([{name: 'songs', pattern: '/songs'}]));
  expect(() => webHandler(clashing)).toThrow('The web route /songs would go to the engine.');
  rmSync(clashing, {recursive: true, force: true});
});
