import { createHash, createHmac, timingSafeEqual } from 'node:crypto';
import { appendFileSync, existsSync, readFileSync, statSync } from 'node:fs';
import { extname, join, resolve, sep } from 'node:path';

// the engine's routes by first path segment. requests for them go to the engine, so the
// app and its studio pages share one origin and one sign-in.
export const ENGINE_PATHS = ['health', 'uploads', 'imports', 'extractions', 'jobs', 'songs', 'studio'];
// what this server answers itself: the sign-in pages, the waitlist and the landing page's files.
const OWN_PATHS = ['login', 'logout', 'waitlist', 'landing'];
const LANDING = resolve(import.meta.dir, '../landing');
const LANDING_FILES = ['.css', '.js', '.png'];
const UNSAFE_HEADERS = ['host', 'origin', 'referer', 'authorization', 'cookie', 'accept-encoding', 'connection',
  'keep-alive', 'transfer-encoding', 'upgrade', 'forwarded', 'x-forwarded-for', 'x-forwarded-host',
  'x-forwarded-port', 'x-forwarded-proto', 'x-forwarded-server', 'x-real-ip'];
const COOKIE = 'flowback_session';
// browsers keep a cookie 400 days at most (RFC 6265bis). a day of use renews it, so a session lasts while it is used.
const SESSION_FOR = 400 * 24 * 60 * 60 * 1000;
const RENEW_AFTER = 24 * 60 * 60 * 1000;
const FAILURE_LIMIT = 10;
const FAILURE_WINDOW = 10 * 60 * 1000;
// joins one address may send per window, and the most entries the list keeps, so it cannot fill the disk.
const JOIN_LIMIT = 5;
const WAITLIST_MAX = 10_000;
const PAGE_POLICY = "default-src 'none'; style-src 'self'; img-src 'self'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'";
// the landing page runs its waveform's script; the sign-in page runs none.
const LANDING_POLICY = `${PAGE_POLICY}; script-src 'self'`;

// FLOWBACK_ORIGIN is the public origin. the users file holds `name:hash` lines from Bun.password.hash,
// and the secret signs session cookies. a public origin without an account is refused, so the
// site never opens unprotected.
export function config(env) {
  const origin = env.FLOWBACK_ORIGIN ?? '';
  const credential = (name, path) => (env.CREDENTIALS_DIRECTORY ? join(env.CREDENTIALS_DIRECTORY, name) : path);
  const usersPath = credential('users', env.FLOWBACK_USERS);
  const users = new Map();
  for (const line of usersPath ? readFileSync(usersPath, 'utf8').split('\n') : []) {
    const colon = line.indexOf(':');
    if (colon < 1) continue;
    const hash = line.slice(colon + 1).trim();
    if (!/^\$(argon2id?|2[aby])\$/.test(hash)) throw new Error(`The password of ${line.slice(0, colon)} is not an argon2 or bcrypt hash.`);
    users.set(line.slice(0, colon), hash);
  }
  if (origin && !users.size) throw new Error('A public origin needs an account: write name:hash lines to /etc/flowback/web.users.');
  const secretPath = credential('secret', env.FLOWBACK_SECRET);
  const secret = users.size && secretPath ? readFileSync(secretPath, 'utf8').trim() : '';
  if (users.size && secret.length < 32) throw new Error('Accounts need a session secret of 32 or more characters in /etc/flowback/web.secret.');
  // systemd's StateDirectory= gives the service a place to keep the waitlist.
  const waitlist = env.FLOWBACK_WAITLIST ?? (env.STATE_DIRECTORY ? join(env.STATE_DIRECTORY, 'waitlist.jsonl') : '');
  return {origin, users, secret, waitlist};
}

// serves a release build (bun exact.mjs web-build --out dist) by exact's static host rule:
// the file at the path; else shell.html, with 200 for a route in routes.json and 404 otherwise.
// with accounts, a signed-out visitor gets only the landing page and the sign-in page.
export function webHandler(root, {engine = 'http://127.0.0.1:8891', origin = '', users = new Map(), secret = '', waitlist = '', landing = LANDING, now = Date.now} = {}) {
  root = resolve(root);
  landing = resolve(landing);
  const shell = join(root, 'shell.html');
  const routes = JSON.parse(readFileSync(join(root, 'routes.json'), 'utf8')).routes
    .filter(route => !route.notfound).map(route => segments(route.pattern));
  const clash = routes.find(pattern => [...ENGINE_PATHS, ...OWN_PATHS].includes(pattern[0]));
  if (clash) throw new Error(`The web route /${clash.join('/')} is a path this server answers itself.`);
  const matches = (path) => routes.some(pattern => pattern.length === path.length
    && pattern.every((part, i) => part.startsWith(':') ? path[i] !== '' : part === path[i]));
  const sessions = users.size ? sessionGate({users, secret, origin, landing, now}) : null;
  const list = sessions && waitlist ? waitlistDesk({file: waitlist, origin, landing, now}) : null;
  const secure = origin.startsWith('https:');

  return async (request) => {
    const response = await route(request);
    response.headers.set('x-content-type-options', 'nosniff');
    response.headers.set('x-frame-options', 'SAMEORIGIN');
    response.headers.set('cross-origin-resource-policy', 'same-origin');
    if (secure) response.headers.set('strict-transport-security', 'max-age=31536000');
    return response;
  };

  async function route(request) {
    const url = new URL(request.url);
    if (!sessions) return answer(request, url);
    const first = url.pathname.split('/')[1];
    const reading = request.method === 'GET' || request.method === 'HEAD';
    if (first === 'landing') {
      const file = within(landing, url.pathname.slice('/landing'.length));
      return reading && file && LANDING_FILES.includes(extname(file)) && isFile(file) ? send(request, file, 200) : new Response(null, {status: 404});
    }
    if (url.pathname === '/waitlist') return list ? list.answer(request, url) : new Response(null, {status: 404});
    const session = sessions.user(request);
    if (url.pathname === '/login') return sessions.login(request, url, session);
    if (url.pathname === '/logout') return sessions.logout();
    if (session) {
      const response = await answer(request, url);
      if (session.renew) response.headers.append('set-cookie', sessions.issue(session.name));
      return response;
    }
    if (url.pathname === '/' && reading) return page(send(request, join(landing, 'index.html'), 200), LANDING_POLICY);
    if (ENGINE_PATHS.includes(first)) {
      return Response.json({detail: 'You are signed out. Reload the page to log in again.'}, {status: 401});
    }
    return reading ? redirect(`/login?next=${encodeURIComponent(url.pathname + url.search)}`) : new Response(null, {status: 401});
  }

  async function answer(request, url) {
    if (ENGINE_PATHS.includes(url.pathname.split('/')[1])) return forward(request, url);
    if (request.method !== 'GET' && request.method !== 'HEAD') return new Response(null, {status: 405});
    let path;
    try { path = decodeURIComponent(url.pathname); } catch { return new Response(null, {status: 400}); }
    const file = within(root, path);
    if (!file) return new Response(null, {status: 404});
    if (isFile(file)) return send(request, file, 200);
    return send(request, shell, path === '/' || !path.endsWith('/') && matches(segments(path)) ? 200 : 404);
  }

  // the engine accepts only loopback hosts and origins, so the request goes on as the proxy's own.
  // the session cookie is SameSite=Lax, and the origin check also turns away posts from other sites.
  async function forward(request, url) {
    const from = request.headers.get('origin');
    if (from && from !== origin && from !== url.origin) {
      return Response.json({detail: 'Flowback only accepts requests from its own pages.'}, {status: 403});
    }
    const headers = new Headers(request.headers);
    for (const name of UNSAFE_HEADERS) headers.delete(name);
    const body = request.method === 'GET' || request.method === 'HEAD' ? undefined : request.body;
    try {
      const reply = await fetch(new URL(url.pathname + url.search, engine), {
        method: request.method, headers, body, duplex: 'half', redirect: 'manual', signal: request.signal,
      });
      return new Response(reply.body, {status: reply.status, statusText: reply.statusText, headers: reply.headers});
    } catch {
      return Response.json({detail: 'The audio engine is not running.'}, {status: 502});
    }
  }
}

// sessions are signed cookies, `<base64url json>.<hmac>`, holding the name, a fingerprint of the
// account's hash and an expiry. removing an account or changing its password ends its sessions.
function sessionGate({users, secret, origin, landing, now}) {
  const secure = origin.startsWith('https:');
  const template = readFileSync(join(landing, 'login.html'), 'utf8');
  const failures = new Map();
  const unknown = Bun.password.hash(crypto.randomUUID());
  // an argon2 check takes 64 MiB: one at a time keeps memory bounded however many tries arrive.
  let turn = Promise.resolve();
  const verify = (password, hash) => (turn = turn.then(async () => Bun.password.verify(password, hash ?? await unknown)).catch(() => false));
  const sign = (body) => createHmac('sha256', secret).update(body).digest('base64url');
  const fingerprint = (hash) => createHash('sha256').update(hash).digest('base64url').slice(0, 16);
  const cookie = (value, seconds) => `${COOKIE}=${value}; Path=/; Max-Age=${seconds}; HttpOnly; SameSite=Lax${secure ? '; Secure' : ''}`;
  const messages = {
    wrong: 'That name and password do not match.',
    limit: 'Too many tries. Wait 10 minutes, then try again.',
  };

  function user(request) {
    const token = cookieValue(request.headers.get('cookie'), COOKIE);
    const [body, signature] = token?.split('.') ?? [];
    if (!body || !signature) return null;
    const expected = Buffer.from(sign(body));
    const given = Buffer.from(signature);
    if (given.length !== expected.length || !timingSafeEqual(given, expected)) return null;
    let session;
    try { session = JSON.parse(Buffer.from(body, 'base64url').toString('utf8')); } catch { return null; }
    const hash = users.get(session?.u);
    if (!hash || session.e <= now() || session.f !== fingerprint(hash)) return null;
    return {name: session.u, renew: session.e - now() < SESSION_FOR - RENEW_AFTER};
  }

  function issue(name) {
    const body = Buffer.from(JSON.stringify({u: name, f: fingerprint(users.get(name)), e: now() + SESSION_FOR})).toString('base64url');
    return cookie(`${body}.${sign(body)}`, SESSION_FOR / 1000);
  }

  async function login(request, url, signedIn) {
    if (request.method === 'GET' || request.method === 'HEAD') {
      if (signedIn) return redirect(safeNext(url.searchParams.get('next')));
      const message = messages[url.searchParams.get('error')];
      return page(new Response(template
        .replace('{{message}}', message ? `<p class="error" role="alert">${message}</p>` : '')
        .replace('{{next}}', escape(safeNext(url.searchParams.get('next')))), {
        headers: {'content-type': 'text/html; charset=utf-8', 'cache-control': 'no-store'},
      }));
    }
    if (request.method !== 'POST') return new Response(null, {status: 405});
    const from = request.headers.get('origin');
    if (from && from !== origin && from !== url.origin) return new Response(null, {status: 403});
    if (Number(request.headers.get('content-length') ?? 0) > 4096) return new Response(null, {status: 413});
    const form = await request.formData().catch(() => null);
    const field = (key) => (typeof form?.get(key) === 'string' ? form.get(key) : '');
    const next = safeNext(field('next'));
    const again = (error) => redirect(`/login?error=${error}${next === '/' ? '' : `&next=${encodeURIComponent(next)}`}`);
    const client = clientOf(request);
    const recent = (failures.get(client) ?? []).filter(time => now() - time < FAILURE_WINDOW);
    if (recent.length >= FAILURE_LIMIT) return again('limit');
    const name = field('name');
    const hash = users.get(name);
    if (!(await verify(field('password'), hash)) || hash === undefined) {
      failures.set(client, [...recent, now()]);
      if (failures.size > 1000) {
        for (const [key, times] of failures) if (now() - times.at(-1) >= FAILURE_WINDOW) failures.delete(key);
      }
      return again('wrong');
    }
    failures.delete(client);
    return redirect(next, {'set-cookie': issue(name)});
  }

  return {user, issue, login, logout: () => redirect('/', {'set-cookie': cookie('', 0)})};
}

// the waitlist is one json line per email, `{"email":…,"at":…}`, appended in the order people join.
// a repeated email gets the same answer as a new one, so the page never tells who is on the list.
function waitlistDesk({file, origin, landing, now}) {
  const form = readFileSync(join(landing, 'waitlist.html'), 'utf8');
  const joined = readFileSync(join(landing, 'joined.html'), 'utf8');
  const emails = new Set();
  if (existsSync(file)) {
    for (const line of readFileSync(file, 'utf8').split('\n')) {
      try { emails.add(JSON.parse(line).email); } catch {}
    }
  }
  const tries = new Map();
  const messages = {
    invalid: 'Enter an email address, such as you@example.com.',
    limit: 'Too many tries. Wait 10 minutes, then try again.',
    full: 'The waitlist is full for now. Try again later.',
  };
  const html = (text) => page(new Response(text, {headers: {'content-type': 'text/html; charset=utf-8', 'cache-control': 'no-store'}}));

  async function answer(request, url) {
    if (request.method === 'GET' || request.method === 'HEAD') {
      if (url.searchParams.has('joined')) return html(joined);
      const message = messages[url.searchParams.get('error')];
      return html(form.replace('{{message}}', message ? `<p class="error" role="alert">${message}</p>` : ''));
    }
    if (request.method !== 'POST') return new Response(null, {status: 405});
    const from = request.headers.get('origin');
    if (from && from !== origin && from !== url.origin) return new Response(null, {status: 403});
    if (Number(request.headers.get('content-length') ?? 0) > 4096) return new Response(null, {status: 413});
    const client = clientOf(request);
    const recent = (tries.get(client) ?? []).filter(time => now() - time < FAILURE_WINDOW);
    if (recent.length >= JOIN_LIMIT) return redirect('/waitlist?error=limit');
    tries.set(client, [...recent, now()]);
    if (tries.size > 1000) {
      for (const [key, times] of tries) if (now() - times.at(-1) >= FAILURE_WINDOW) tries.delete(key);
    }
    const fields = await request.formData().catch(() => null);
    const field = (key) => (typeof fields?.get(key) === 'string' ? fields.get(key) : '');
    // a person never sees the trap field; a bot that fills it is thanked and forgotten.
    if (field('website')) return redirect('/waitlist?joined');
    const email = field('email').trim().toLowerCase();
    if (email.length > 254 || !/^[^\s@]+@[^\s@.]+(\.[^\s@.]+)+$/.test(email)) return redirect('/waitlist?error=invalid');
    if (!emails.has(email)) {
      if (emails.size >= WAITLIST_MAX) return redirect('/waitlist?error=full');
      appendFileSync(file, `${JSON.stringify({email, at: new Date(now()).toISOString()})}\n`, {mode: 0o600});
      emails.add(email);
    }
    return redirect('/waitlist?joined');
  }

  return {answer};
}

// traefik appends the address it saw, so the last entry is the one a client cannot write.
function clientOf(request) {
  return (request.headers.get('x-forwarded-for') ?? '').split(',').pop().trim() || 'direct';
}

// a page this server writes: nothing from elsewhere, and scripts only where the policy names them.
function page(response, policy = PAGE_POLICY) {
  response.headers.set('content-security-policy', policy);
  return response;
}

function redirect(location, headers = {}) {
  return new Response(null, {status: 303, headers: {location, 'cache-control': 'no-store', ...headers}});
}

// only a path on this site, and never back into signing in or out.
function safeNext(next) {
  if (typeof next !== 'string' || !next.startsWith('/') || next.startsWith('//') || next.startsWith('/\\')) return '/';
  const url = new URL(next, 'http://flowback.invalid');
  if (url.origin !== 'http://flowback.invalid' || url.pathname === '/login' || url.pathname === '/logout') return '/';
  return url.pathname + url.search;
}

function cookieValue(header, name) {
  for (const part of (header ?? '').split(';')) {
    const at = part.indexOf('=');
    if (at > 0 && part.slice(0, at).trim() === name) return part.slice(at + 1).trim();
  }
  return null;
}

function escape(text) {
  return text.replace(/[&<>"']/g, (c) => `&#${c.charCodeAt(0)};`);
}

function segments(path) {
  return path.replace(/^\/+/, '').split('/');
}

function within(dir, path) {
  const file = resolve(dir, `.${path}`);
  return file === dir || file.startsWith(dir + sep) ? file : null;
}

function isFile(path) {
  try { return statSync(path).isFile(); } catch { return false; }
}

// a deploy replaces files in place, so the browser revalidates every file against its etag.
function send(request, path, status) {
  const stat = statSync(path);
  const etag = `"${stat.size.toString(16)}-${stat.mtimeMs.toString(16)}"`;
  const headers = {'cache-control': 'no-cache', etag};
  if (status === 200 && request.headers.get('if-none-match') === etag) return new Response(null, {status: 304, headers});
  return new Response(Bun.file(path), {status, headers});
}

if (import.meta.main) {
  const [root, port] = process.argv.slice(2);
  if (!root || !port) throw new Error('Use bun deploy/serve-web.mjs <dist> <port>.');
  const {origin, users, secret, waitlist} = config(process.env);
  const handler = webHandler(root, {origin, users, secret, waitlist});
  // long engine requests (an upload, preparing a cover's tracks) stay open past bun's 10 s idle default.
  const server = Bun.serve({hostname: '127.0.0.1', port: Number(port), idleTimeout: 255, fetch: (request, bun) => {
    bun.timeout(request, 0);
    return handler(request);
  }});
  console.log(`Flowback web app at ${server.url}${origin ? ` for ${origin}` : ''}${users.size ? `, ${users.size} account(s)` : ''}${waitlist ? `, waitlist in ${waitlist}` : ''}`);
}
