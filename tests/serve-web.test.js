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
    origin: request.headers.get('origin'), authorization: request.headers.get('authorization'), cookie: request.headers.get('cookie'),
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
      origin: 'http://flowback.example', authorization: 'Basic abc', cookie: 'a=b', 'x-forwarded-for': '203.0.113.9',
    },
  }));
  expect(reply.status).toBe(200);
  expect(await reply.json()).toEqual({
    method: 'POST', path: '/uploads?x=1', host: `127.0.0.1:${engine.port}`, origin: null, authorization: null, cookie: null,
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

// accounts: a signed-out visitor sees the landing page; the app needs a session.
const secret = 'a session secret of more than thirty-two characters';
const rami = await Bun.password.hash('right horse');
const accounts = (options = {}) => webHandler(root, {engine: engineOrigin, users: new Map([['rami', rami]]), secret, ...options});
const signIn = (handler, fields, headers = {}) => handler(new Request('http://127.0.0.1/login', {
  method: 'POST', body: new URLSearchParams(fields), headers: {origin: 'http://127.0.0.1', ...headers},
}));
const session = (reply) => reply.headers.get('set-cookie').split(';')[0];

test('signed out, a visitor gets the landing page and the sign-in page, and nothing else', async () => {
  const handler = accounts();
  const landing = await get('/', {}, handler);
  expect(landing.status).toBe(200);
  expect(await landing.text()).toContain('<a class="primary rise d5" href="/login">Log in</a>');
  expect(landing.headers.get('content-security-policy')).toContain("default-src 'none'");
  expect((await get('/landing/landing.css', {}, handler)).status).toBe(200);
  expect((await get('/landing/flowback-icon.png', {}, handler)).status).toBe(200);
  for (const path of ['/landing/index.html', '/landing/login.html', '/landing/..%2f..%2fdeploy%2fserve-web.mjs']) {
    expect([path, (await get(path, {}, handler)).status]).toEqual([path, 404]);
  }
  const page = await get('/cover?take=2', {}, handler);
  expect([page.status, page.headers.get('location')]).toEqual([303, '/login?next=%2Fcover%3Ftake%3D2']);
  expect((await get('/app.js', {}, handler)).headers.get('location')).toBe('/login?next=%2Fapp.js');
  const engineReply = await get('/songs', {}, handler);
  expect([engineReply.status, (await engineReply.json()).detail]).toEqual([401, 'You are signed out. Reload the page to log in again.']);
  expect((await handler(new Request('http://127.0.0.1/jobs', {method: 'POST', body: '{}'}))).status).toBe(401);

  const form = await get('/login?next=%2Fcover%3Ftake%3D2', {}, handler);
  const html = await form.text();
  expect(html).toContain('<input type="hidden" name="next" value="/cover?take=2">');
  expect(html).not.toContain('{{');
  expect(form.headers.get('cache-control')).toBe('no-store');
  for (const next of ['//evil.example/x', '/\\evil.example', 'https://evil.example', '/logout', '/"><script>']) {
    const value = (await (await get(`/login?next=${encodeURIComponent(next)}`, {}, handler)).text()).match(/name="next" value="([^"]*)"/)[1];
    expect([next, value]).toEqual([next, next === '/"><script>' ? '/%22%3E%3Cscript%3E' : '/']);
  }
});

test('signing in opens the app and the engine; a wrong password and other sites are turned away', async () => {
  const handler = accounts();
  const wrong = await signIn(handler, {name: 'rami', password: 'old horse', next: '/cover'});
  expect([wrong.status, wrong.headers.get('location'), wrong.headers.get('set-cookie')]).toEqual([303, '/login?error=wrong&next=%2Fcover', null]);
  const again = await (await get('/login?error=wrong&next=%2Fcover', {}, handler)).text();
  expect(again).toContain('<p class="error" role="alert">That name and password do not match.</p>');
  expect((await signIn(handler, {name: 'rami', password: 'right horse'}, {origin: 'https://evil.example'})).status).toBe(403);

  const reply = await signIn(handler, {name: 'rami', password: 'right horse', next: '/cover'});
  expect([reply.status, reply.headers.get('location')]).toEqual([303, '/cover']);
  expect(reply.headers.get('set-cookie')).toMatch(/^flowback_session=[\w-]+\.[\w-]+; Path=\/; Max-Age=2592000; HttpOnly; SameSite=Lax$/);
  const cookie = {cookie: session(reply)};
  expect(await (await get('/', cookie, handler)).text()).toBe('<main>shell</main>');
  expect(await (await get('/songs', cookie, handler)).json()).toMatchObject({path: '/songs', cookie: null});
  expect((await get('/login', cookie, handler)).headers.get('location')).toBe('/');

  const out = await get('/logout', cookie, handler);
  expect([out.status, out.headers.get('location')]).toEqual([303, '/']);
  expect(out.headers.get('set-cookie')).toContain('flowback_session=; Path=/; Max-Age=0');

  const secure = accounts({origin: 'https://flowback.example'});
  const signed = await signIn(secure, {name: 'rami', password: 'right horse'}, {origin: 'https://flowback.example'});
  expect(signed.headers.get('set-cookie')).toEndWith('; Secure');
});

test('a session ends when it expires, is forged, or its account changes', async () => {
  let clock = 0;
  const cookie = {cookie: session(await signIn(accounts({now: () => clock}), {name: 'rami', password: 'right horse'}))};
  const app = async (handler) => (await (await get('/', cookie, handler)).text()) === '<main>shell</main>';
  expect(await app(accounts({now: () => clock}))).toBe(true);
  const [body, signature] = cookie.cookie.split('.');
  const forged = Buffer.from(JSON.stringify({...JSON.parse(Buffer.from(body.split('=')[1], 'base64url')), u: 'someone'})).toString('base64url');
  expect(await (await get('/', {cookie: `flowback_session=${forged}.${signature}`}, accounts())).text()).toContain('href="/login"');
  expect(await app(accounts({secret: `${secret}, changed`}))).toBe(false);
  expect(await app(accounts({users: new Map([['rami', await Bun.password.hash('new horse')]])}))).toBe(false);
  expect(await app(accounts({users: new Map([['someone', rami]])}))).toBe(false);
  clock += 30 * 24 * 60 * 60 * 1000;
  expect(await app(accounts({now: () => clock}))).toBe(false);
});

test('wrong passwords are limited per client, and checks run one at a time', async () => {
  let clock = 0;
  const handler = accounts({now: () => clock});
  const from = (client, password) => signIn(handler, {name: 'rami', password}, {'x-forwarded-for': `1.2.3.4, ${client}`});
  for (let i = 0; i < 10; i++) expect((await from('198.51.100.1', `wrong ${i}`)).headers.get('location')).toBe('/login?error=wrong');
  expect((await from('198.51.100.1', 'right horse')).headers.get('location')).toBe('/login?error=limit');
  expect(await (await get('/login?error=limit', {}, handler)).text()).toContain('Too many tries.');
  expect((await from('198.51.100.2', 'right horse')).headers.get('location')).toBe('/');
  clock += 10 * 60 * 1000;
  expect((await from('198.51.100.1', 'right horse')).headers.get('location')).toBe('/');

  const verify = Bun.password.verify;
  let running = 0, most = 0;
  Bun.password.verify = async (...args) => {
    most = Math.max(most, ++running);
    try { return await verify(...args); } finally { running--; }
  };
  try {
    const replies = await Promise.all(['a', 'b', 'c'].map(word => from(`198.51.100.${word.charCodeAt(0)}`, `wrong ${word}`)));
    expect(replies.map(reply => reply.headers.get('location'))).toEqual(Array(3).fill('/login?error=wrong'));
    expect(most).toBe(1);
  } finally {
    Bun.password.verify = verify;
  }
});

test('accounts need hashes and a session secret, and no web route may shadow a path the server answers', async () => {
  const users = join(root, 'users');
  const secretFile = join(root, 'secret');
  expect(() => config({FLOWBACK_ORIGIN: 'https://flowback.example'})).toThrow('A public origin needs an account');
  writeFileSync(users, 'rami:plain-text\n');
  expect(() => config({FLOWBACK_USERS: users})).toThrow('not an argon2 or bcrypt hash');
  writeFileSync(users, `rami:${rami}\n\n`);
  expect(() => config({FLOWBACK_USERS: users})).toThrow('Accounts need a session secret');
  writeFileSync(secretFile, 'short\n');
  expect(() => config({FLOWBACK_USERS: users, FLOWBACK_SECRET: secretFile})).toThrow('Accounts need a session secret');
  writeFileSync(secretFile, `${secret}\n`);
  const loaded = config({FLOWBACK_ORIGIN: 'https://flowback.example', FLOWBACK_USERS: users, FLOWBACK_SECRET: secretFile});
  expect([loaded.users.has('rami'), loaded.secret]).toEqual([true, secret]);
  expect(config({})).toEqual({origin: '', users: new Map(), secret: ''});

  for (const pattern of ['/songs', '/login', '/landing']) {
    const clashing = mkdtempSync(join(tmpdir(), 'flowback-clash-'));
    writeFileSync(join(clashing, 'routes.json'), routes([{name: 'x', pattern}]));
    expect(() => webHandler(clashing)).toThrow(`The web route ${pattern} is a path this server answers itself.`);
    rmSync(clashing, {recursive: true, force: true});
  }
});
