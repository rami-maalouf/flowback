import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const root = dirname(fileURLToPath(import.meta.url));
const target = process.argv[2] ?? 'web';
if (!['web', 'mac'].includes(target)) throw new Error('Use bun dev.mjs web or bun dev.mjs mac.');

async function healthy() {
  try {
    const response = await fetch('http://127.0.0.1:8891/health', { signal: AbortSignal.timeout(1000) });
    const data = await response.json();
    return response.ok && data.ok === true && typeof data.models_available === 'boolean';
  } catch { return false; }
}

let engine;
let client;
const stop = () => { client?.kill(); engine?.kill(); process.exit(0); };
process.on('SIGINT', stop);
process.on('SIGTERM', stop);

if (!await healthy()) {
  console.log('Starting the local audio engine. First launch installs its dependencies.');
  engine = Bun.spawn(['uv', 'run', '--frozen', '--extra', 'models', 'python', '-m', 'cover_engine.server'], {
    cwd: root, stdout: 'inherit', stderr: 'inherit',
  });
  for (let attempt = 0; !await healthy(); attempt++) {
    if (engine.exitCode !== null || attempt >= 180) {
      engine.kill();
      throw new Error('The audio engine could not start. Check the output above.');
    }
    await Bun.sleep(1000);
  }
}

console.log('Audio engine ready at http://127.0.0.1:8891. Keep this terminal open while using the app.');
client = Bun.spawn([process.execPath, join(root, 'exact.mjs'), target, ...(target === 'mac' ? ['--run'] : ['--port', '8893'])], {
  cwd: root, stdout: 'inherit', stderr: 'inherit',
});
const code = await client.exited;
if (code !== 0 || target === 'web') {
  engine?.kill();
  process.exit(code);
}
console.log('Exact Cover is open. Press Ctrl+C here when you are finished.');
await new Promise(() => {});
