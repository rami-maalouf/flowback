import {chmodSync, cpSync, existsSync, lstatSync, mkdirSync, readFileSync, readdirSync, realpathSync, rmSync} from 'node:fs';
import {basename, dirname, join, relative, resolve} from 'node:path';
import {fileURLToPath} from 'node:url';
import {spawnSync} from 'node:child_process';

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const output = join(root, 'engine-runtime');
function run(command, args, options = {}) {
  const result = spawnSync(command, args, {cwd: root, encoding: 'utf8', ...options});
  if (result.status !== 0) throw new Error(`${command} failed: ${result.stderr || result.error || result.status}`);
  return result.stdout;
}
if (process.platform !== 'darwin') throw new Error('Build the Mac engine on macOS.');
run('uv', ['sync', '--frozen', '--extra', 'models', '--no-dev'], {
  env: {...process.env, UV_PROJECT_ENVIRONMENT: join(root, '.exact/engine-env')}, stdio: 'inherit',
});
const interpreter = join(root, '.exact/engine-env/bin/python');
const info = JSON.parse(run(interpreter, ['-c', 'import json,sys,sysconfig; print(json.dumps({"prefix":sys.base_prefix,"site":sysconfig.get_path("purelib"),"version":f"{sys.version_info.major}.{sys.version_info.minor}"}))']));
// this tree is a build product; no user library or model weights belong in it.
rmSync(output, {recursive: true, force: true});
mkdirSync(output, {recursive: true});
const copy = (from, to) => cpSync(from, to, {
  recursive: true, verbatimSymlinks: true,
  filter: path => !path.split('/').includes('__pycache__') && !path.endsWith('.pyc'),
});
console.log('Bundling Python and locked processing dependencies...');
copy(realpathSync(info.prefix), join(output, 'python'));
const site = join(output, 'python/lib', `python${info.version}`, 'site-packages');
copy(info.site, site);
copy(join(root, 'cover_engine'), join(site, 'cover_engine'));
mkdirSync(join(output, 'bin'));
for (const name of ['ffmpeg', 'ffprobe', 'bun']) {
  const source = Bun.which(name);
  if (!source) throw new Error(`Install ${name} before packaging the Mac app.`);
  copy(realpathSync(source), join(output, 'bin', name));
}

function files(path) {
  return readdirSync(path).flatMap(name => {
    const item = join(path, name), stat = lstatSync(item);
    return stat.isSymbolicLink() ? [] : stat.isDirectory() ? files(item) : [item];
  });
}
// the engine runs python3.12 directly; its console scripts and their links are unused.
const pythonBin = join(output, 'python/bin');
for (const name of readdirSync(pythonBin)) if (!['python3', 'python3.12'].includes(name)) rmSync(join(pythonBin, name));
const magic = new Set(['cffaedfe', 'cefaedfe', 'cafebabe', 'bebafeca', 'cafebabf', 'bfbafeca']);
const native = files(output).filter(path => {
  const head = readFileSync(path).subarray(0, 4);
  const executable = head.length === 4 && magic.has(head.toString('hex'));
  // only native executables get their own signature; python imports the rest as data.
  // the tree ships under Resources/ because codesign rejects plain data files in Helpers/.
  if (!executable) chmodSync(path, lstatSync(path).mode & ~0o111);
  return executable;
});
const copied = new Map();
const names = new Map();
mkdirSync(join(output, 'lib'));
for (let index = 0; index < native.length; index++) {
  const binary = native[index];
  const identities = new Set(run('otool', ['-D', binary]).split('\n').slice(1).map(line => line.trim()));
  const dependencies = run('otool', ['-L', binary]).split('\n').slice(1)
    .map(line => line.trim().split(' (compatibility')[0]).filter(Boolean);
  for (const dependency of dependencies) {
    if (identities.has(dependency) || !dependency.startsWith('/') || dependency.startsWith('/usr/lib/') || dependency.startsWith('/System/')) continue;
    const source = realpathSync(dependency);
    let destination;
    const local = relative(realpathSync(info.prefix), source);
    if (!local.startsWith('../') && existsSync(join(output, 'python', local))) {
      destination = join(output, 'python', local);
    } else {
      destination = copied.get(source);
      if (!destination) {
        const name = basename(source);
        if (names.has(name) && names.get(name) !== source) throw new Error(`Conflicting native library: ${name}`);
        names.set(name, source);
        destination = join(output, 'lib', name);
        copy(source, destination);
        copied.set(source, destination);
        native.push(destination);
      }
    }
    if (destination !== binary) run('install_name_tool', ['-change', dependency, `@loader_path/${relative(dirname(binary), destination)}`, binary]);
  }
}
// relocation changes Mach-O signatures, including the decoder's transitive libraries.
for (const binary of native) run('codesign', ['--force', '--sign', '-', '--timestamp=none', binary]);
run(join(output, 'python/bin/python3'), ['-I', '-B', '-c', 'import cover_engine.managed, audio_separator, faster_whisper, torchcrepe; print("bundled engine imports passed")'], {
  env: {...process.env, PATH: `${join(output, 'bin')}:/usr/bin:/bin`}, stdio: 'inherit',
});
for (const name of ['ffmpeg', 'ffprobe', 'bun']) run(join(output, 'bin', name), [name === 'bun' ? '--version' : '-version']);
console.log(`Engine ready: ${output}`);
