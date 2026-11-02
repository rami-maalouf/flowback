import { readFileSync, statSync } from 'node:fs';
import { join, resolve, sep } from 'node:path';

// serves a release build (bun exact.mjs web-build --out dist) by exact's static host rule:
// the file at the path; else shell.html, with 200 for a route in routes.json and 404 otherwise.
export function webHandler(root) {
  root = resolve(root);
  const shell = join(root, 'shell.html');
  const routes = JSON.parse(readFileSync(join(root, 'routes.json'), 'utf8')).routes
    .filter(route => !route.notfound).map(route => segments(route.pattern));
  const matches = (path) => routes.some(pattern => pattern.length === path.length
    && pattern.every((part, i) => part.startsWith(':') ? path[i] !== '' : part === path[i]));

  return (request) => {
    if (request.method !== 'GET' && request.method !== 'HEAD') return new Response(null, {status: 405});
    let path;
    try { path = decodeURIComponent(new URL(request.url).pathname); } catch { return new Response(null, {status: 400}); }
    const file = resolve(root, `.${path}`);
    if (file !== root && !file.startsWith(root + sep)) return new Response(null, {status: 404});
    if (isFile(file)) return send(request, file, 200);
    return send(request, shell, path === '/' || !path.endsWith('/') && matches(segments(path)) ? 200 : 404);
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
  const headers = {'cache-control': 'no-cache', etag, 'x-content-type-options': 'nosniff'};
  if (status === 200 && request.headers.get('if-none-match') === etag) return new Response(null, {status: 304, headers});
  return new Response(Bun.file(path), {status, headers});
}

if (import.meta.main) {
  const [root, port] = process.argv.slice(2);
  if (!root || !port) throw new Error('Use bun deploy/serve-web.mjs <dist> <port>.');
  const server = Bun.serve({hostname: '127.0.0.1', port: Number(port), fetch: webHandler(root)});
  console.log(`Flowback web app at ${server.url}`);
}
