import {afterAll, beforeAll, expect, test} from 'bun:test';
import {mkdtempSync, mkdirSync, writeFileSync, rmSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {join, resolve} from 'node:path';

const macTest = process.platform === 'darwin' ? test : test.skip;
let root, executable, fixture;
const children = new Set();
const run = async (args) => {
  const child = Bun.spawn(args, {stdout: 'pipe', stderr: 'pipe'});
  const error = await new Response(child.stderr).text();
  if (await child.exited !== 0) throw new Error(error);
};

beforeAll(async () => {
  if (process.platform !== 'darwin') return;
  root = mkdtempSync(join(tmpdir(), 'flowback-supervisor-'));
  executable = join(root, 'supervisor');
  mkdirSync(join(root, 'python/bin'), {recursive: true});
  const python = resolve('.venv/bin/python');
  fixture = join(root, 'python/bin/python3');
  writeFileSync(fixture, `#!${python}
import json, os, sys, threading
from http.server import BaseHTTPRequestHandler, HTTPServer
class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(json.dumps({"ok": True, "models_available": True, "pid": os.getpid()}).encode())
    def log_message(self, *args): pass
server = HTTPServer(("127.0.0.1", int(os.environ["TEST_PORT"])), Handler)
def watch():
    sys.stdin.buffer.read()
    server.shutdown()
threading.Thread(target=watch, daemon=True).start()
server.serve_forever()
`, {mode: 0o755});
  const main = join(root, 'main.swift');
  writeFileSync(main, `import Foundation
let supervisor = EngineSupervisor(runtime: URL(fileURLWithPath: CommandLine.arguments[1]), log: URL(fileURLWithPath: CommandLine.arguments[1] + "/engine.log"), port: Int(CommandLine.arguments[2])!)
supervisor.start()
DispatchQueue.global().async {
    while readLine() != nil {}
    DispatchQueue.main.async { supervisor.stop(); exit(0) }
}
RunLoop.main.run()
`);
  await run(['swiftc', resolve('modules/apple/EngineSupervisor.swift'), main, '-o', executable]);
}, 30000);

afterAll(() => {
  for (const child of children) child.kill();
  if (root) rmSync(root, {recursive: true, force: true});
});

function unusedPort() {
  const server = Bun.serve({port: 0, fetch: () => new Response('')});
  const port = server.port;
  server.stop(true);
  return port;
}
function spawn(args, port) {
  const child = Bun.spawn(args, {env: {...process.env, TEST_PORT: String(port)}, stdin: 'pipe', stdout: 'ignore', stderr: 'pipe'});
  children.add(child);
  return child;
}
async function health(port) {
  try { return await (await fetch(`http://127.0.0.1:${port}/health`, {signal: AbortSignal.timeout(500)})).json(); }
  catch { return null; }
}
async function until(check) {
  const deadline = Date.now() + 12000;
  while (Date.now() < deadline) {
    const value = await check();
    if (value) return value;
    await Bun.sleep(100);
  }
  throw new Error('engine lifecycle condition timed out');
}

macTest('normal launch starts an engine, recovers a crash, and closes its lifetime pipe on quit', async () => {
  const port = unusedPort();
  const app = spawn([executable, root, String(port)], port);
  const first = await until(() => health(port));
  process.kill(first.pid, 'SIGKILL');
  const restarted = await until(async () => {
    const value = await health(port);
    return value && value.pid !== first.pid ? value : null;
  });
  expect(restarted.pid).not.toBe(first.pid);
  app.stdin.end();
  expect(await app.exited).toBe(0);
  await until(async () => !await health(port));
}, 30000);

macTest('an already running engine is reused and survives app quit', async () => {
  const port = unusedPort();
  const external = spawn([fixture], port);
  const first = await until(() => health(port));
  const app = spawn([executable, root, String(port)], port);
  await Bun.sleep(2500);
  expect((await health(port)).pid).toBe(first.pid);
  app.stdin.end();
  await app.exited;
  expect((await health(port)).pid).toBe(first.pid);
  external.stdin.end();
  expect(await external.exited).toBe(0);
}, 15000);
