import { readFileSync, statSync } from 'node:fs';
import { join, resolve, sep } from 'node:path';

// the engine's routes by first path segment. requests for them go to the engine, so the
// app and its studio pages share one origin and one password.
export const ENGINE_PATHS = ['health', 'uploads', 'imports', 'extractions', 'jobs', 'songs', 'studio'];
const UNSAFE_HEADERS = ['host', 'origin', 'referer', 'authorization', 'cookie', 'accept-encoding', 'connection',
  'keep-alive', 'transfer-encoding', 'upgrade', 'forwarded', 'x-forwarded-for', 'x-forwarded-host',
  'x-forwarded-port', 'x-forwarded-proto', 'x-forwarded-server', 'x-real-ip'];
const FAILURE_LIMIT = 10;
const FAILURE_WINDOW = 10 * 60 * 1000;
const REJECTED_FOR = 60 * 1000;

// FLOWBACK_ORIGIN is the public origin; the users file holds `name:hash` lines from Bun.password.hash.
// a public origin without a password is refused, so the site never opens unprotected.
export function config(env) {
  const origin = env.FLOWBACK_ORIGIN ?? '';
  const path = env.CREDENTIALS_DIRECTORY ? join(env.CREDENTIALS_DIRECTORY, 'users') : env.FLOWBACK_USERS;
  const users = new Map();
  for (const line of path ? readFileSync(path, 'utf8').split('\n') : []) {
    const colon = line.indexOf(':');
    if (colon < 1) continue;
    const hash = line.slice(colon + 1).trim();
    if (!/^\$(argon2id?|2[aby])\$/.test(hash)) throw new Error(`The password of ${line.slice(0, colon)} is not an argon2 or bcrypt hash.`);
    users.set(line.slice(0, colon), hash);
  }
  if (origin && !users.size) throw new Error('A public origin needs a password: write name:hash lines to /etc/flowback/web.users.');
  return {origin, users};
}

// serves a release build (bun exact.mjs web-build --out dist) by exact's static host rule:
// the file at the path; else shell.html, with 200 for a route in routes.json and 404 otherwise.
export function webHandler(root, {engine = 'http://127.0.0.1:8891', origin = '', users = new Map(), now = Date.now} = {}) {
  root = resolve(root);
  const shell = join(root, 'shell.html');
  const routes = JSON.parse(readFileSync(join(root, 'routes.json'), 'utf8')).routes
    .filter(route => !route.notfound).map(route => segments(route.pattern));
  const clash = routes.find(pattern => ENGINE_PATHS.includes(pattern[0]));
  if (clash) throw new Error(`The web route /${clash.join('/')} would go to the engine.`);
  const matches = (path) => routes.some(pattern => pattern.length === path.length
    && pattern.every((part, i) => part.startsWith(':') ? path[i] !== '' : part === path[i]));
  const admit = users.size ? passwordCheck(users, now) : async () => null;
  const secure = origin.startsWith('https:');

  return async (request) => {
    const response = await admit(request) ?? await answer(request);
    response.headers.set('x-content-type-options', 'nosniff');
    response.headers.set('x-frame-options', 'SAMEORIGIN');
    response.headers.set('cross-origin-resource-policy', 'same-origin');
    if (secure) response.headers.set('strict-transport-security', 'max-age=31536000');
    return response;
  };

  async function answer(request) {
    const url = new URL(request.url);
    if (ENGINE_PATHS.includes(url.pathname.split('/')[1])) return forward(request, url);
    if (request.method !== 'GET' && request.method !== 'HEAD') return new Response(null, {status: 405});
    let path;
    try { path = decodeURIComponent(url.pathname); } catch { return new Response(null, {status: 400}); }
    const file = resolve(root, `.${path}`);
    if (file !== root && !file.startsWith(root + sep)) return new Response(null, {status: 404});
    if (isFile(file)) return send(request, file, 200);
    return send(request, shell, path === '/' || !path.endsWith('/') && matches(segments(path)) ? 200 : 404);
  }

  // the engine accepts only loopback hosts and origins, so the request goes on as the proxy's own.
  // a browser sends the password to any site that asks it to post here: only this site may.
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

// http basic auth. a verified header is remembered, since the app polls every few seconds and
// each hash check costs tens of milliseconds. a rejected one is remembered for a while too, so a
// browser polling with an old password costs one check; each client gets FAILURE_LIMIT new wrong
// tries per window.
function passwordCheck(users, now) {
  const verified = new Set();
  const rejected = new Map();
  const checking = new Map();
  const failures = new Map();
  const unknown = Bun.password.hash(crypto.randomUUID());
  // an argon2 check takes 64 MiB: one at a time keeps memory bounded however many requests arrive.
  let turn = Promise.resolve();
  const verify = (password, hash) => (turn = turn.then(async () => Bun.password.verify(password, hash ?? await unknown)).catch(() => false));
  const ask = () => new Response('Flowback needs your password.', {status: 401, headers: {
    'www-authenticate': 'Basic realm="Flowback", charset="UTF-8"', 'cache-control': 'no-store',
  }});

  return async (request) => {
    const header = request.headers.get('authorization') ?? '';
    if (verified.has(header)) return null;
    const [scheme, encoded] = header.split(' ');
    if (scheme?.toLowerCase() !== 'basic' || !encoded) return ask();
    if (now() - (rejected.get(header) ?? -Infinity) < REJECTED_FOR) return ask();
    let check = checking.get(header);
    if (!check) {
      // traefik appends the address it saw, so the last entry is the one a client cannot write.
      const client = (request.headers.get('x-forwarded-for') ?? '').split(',').pop().trim() || 'direct';
      const recent = (failures.get(client) ?? []).filter(time => now() - time < FAILURE_WINDOW);
      if (recent.length >= FAILURE_LIMIT) {
        return new Response('Too many wrong passwords. Try again later.', {status: 429, headers: {'retry-after': `${FAILURE_WINDOW / 1000}`}});
      }
      const decoded = Buffer.from(encoded, 'base64').toString('utf8');
      const colon = decoded.indexOf(':');
      const hash = colon > 0 ? users.get(decoded.slice(0, colon)) : undefined;
      check = verify(decoded.slice(colon + 1), hash).then(valid => {
        checking.delete(header);
        if (valid && hash !== undefined) {
          if (verified.size >= 64) verified.clear();
          verified.add(header);
          failures.delete(client);
          return true;
        }
        if (rejected.size >= 256) rejected.clear();
        rejected.set(header, now());
        failures.set(client, [...recent, now()]);
        if (failures.size > 1000) {
          for (const [key, times] of failures) if (now() - times.at(-1) >= FAILURE_WINDOW) failures.delete(key);
        }
        return false;
      });
      checking.set(header, check);
    }
    return await check ? null : ask();
  };
}

function segments(path) {
  return path.replace(/^\/+/, '').split('/');
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
  const {origin, users} = config(process.env);
  const handler = webHandler(root, {origin, users});
  // long engine requests (an upload, preparing a cover's tracks) stay open past bun's 10 s idle default.
  const server = Bun.serve({hostname: '127.0.0.1', port: Number(port), idleTimeout: 255, fetch: (request, bun) => {
    bun.timeout(request, 0);
    return handler(request);
  }});
  console.log(`Flowback web app at ${server.url}${origin ? ` for ${origin}` : ''}${users.size ? ', with a password' : ''}`);
}
