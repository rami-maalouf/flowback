import { afterAll, expect, test } from 'bun:test';
import { mkdirSync, mkdtempSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { webHandler } from '../deploy/serve-web.mjs';

const root = mkdtempSync(join(tmpdir(), 'flowback-web-'));
afterAll(() => rmSync(root, {recursive: true, force: true}));
mkdirSync(join(root, 'assets'));
writeFileSync(join(root, 'shell.html'), '<main>shell</main>');
writeFileSync(join(root, 'app.js'), 'export {};');
writeFileSync(join(root, 'assets', 'icon.txt'), 'icon');
writeFileSync(join(root, 'routes.json'), JSON.stringify({routes: [
  {name: 'home', pattern: '/', notfound: false},
  {name: 'cover', pattern: '/cover', notfound: false},
  {name: 'take', pattern: '/cover/:take', notfound: false},
  {name: 'notfound', pattern: '', notfound: true},
]}));
const serve = webHandler(root);
const get = (path, headers = {}) => serve(new Request(`http://127.0.0.1${path}`, {headers}));

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

test('an unchanged file revalidates with its etag', async () => {
  const first = await get('/app.js');
  expect(first.headers.get('cache-control')).toBe('no-cache');
  const again = await get('/app.js', {'if-none-match': first.headers.get('etag')});
  expect(again.status).toBe(304);
});
