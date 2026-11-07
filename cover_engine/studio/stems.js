import { Mixer } from './mixer.js';
import { Autosave, restoredLevels } from './autosave.js';
import { drawWaveform, waveform } from './waveform.js';

const $ = id => document.getElementById(id);
const params = new URLSearchParams(location.search);
const extraction = params.get('extraction') || '';
const valid = /^[a-f0-9]{32}$/.test(extraction);
const key = `flowback-stems:${extraction}`;
const notify = value => parent.postMessage(`${key}:${value}`, '*');
const embedded = params.get('embedded') === '1' && parent !== window;
const relayWheel = event => {
  if (event.ctrlKey || event.defaultPrevented || !event.cancelable || !event.deltaY
      || Math.abs(event.deltaX) > Math.abs(event.deltaY)) return;
  event.preventDefault();
  notify(`scroll:${event.deltaMode}:${event.deltaY}`);
};
if (embedded) addEventListener('wheel', relayWheel, {passive: false});
const time = seconds => `${Math.floor(seconds / 60)}:${String(Math.floor(seconds % 60)).padStart(2, '0')}`;
let mixer;
let disposed = false;
let frame;
let lastHeight = 0;
const abort = new AbortController();
const saver = new Autosave(async (payload, keepalive) => {
  const response = await fetch(`/extractions/${extraction}/levels`, {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(payload), keepalive,
  });
  if (!response.ok) throw new Error('Could not save the levels.');
}, {
  pending(payload) {
    try { localStorage.setItem(key, JSON.stringify({...payload, pending: true})); } catch {}
    $('status').textContent = 'Saving mix…';
    document.body.dataset.save = 'saving';
    $('retry-save').hidden = true;
  },
  saved() {
    try { localStorage.removeItem(key); } catch {}
    $('status').textContent = 'Mix saved';
    document.body.dataset.save = 'saved';
    $('retry-save').hidden = true;
  },
  error() {
    $('status').textContent = 'Mix not saved. Please retry.';
    document.body.dataset.save = 'error';
    $('retry-save').hidden = false;
  },
}, crypto.randomUUID());

async function request(url) {
  const response = await fetch(url, {signal: abort.signal});
  if (!response.ok) {
    let detail = 'Could not load the tracks. Check the audio engine and retry.';
    try { detail = (await response.json()).detail || detail; } catch {}
    throw new Error(detail);
  }
  return response;
}
function peak(buffer) {
  let value = 0;
  for (let channel = 0; channel < buffer.numberOfChannels; channel++) {
    const data = buffer.getChannelData(channel);
    for (let i = 0; i < data.length; i++) value = Math.max(value, Math.abs(data[i]));
  }
  return value;
}
function updateLevels(save = true) {
  const vocal = Number($('vocal').value);
  const backing = Number($('backing').value);
  $('vocal-value').textContent = vocal ? `${vocal}%` : 'Muted';
  $('backing-value').textContent = backing ? `${backing}%` : 'Muted';
  mixer?.levels(vocal, backing);
  if (save) saver.set(vocal, backing);
}
function tick() {
  if (!mixer || disposed) return;
  if (mixer.playing && mixer.position >= mixer.duration) mixer.pause();
  $('play').dataset.playing = String(mixer.playing);
  $('play').setAttribute('aria-label', mixer.playing ? 'Pause' : 'Play');
  $('played-width').setAttribute('width', String(600 * mixer.position / mixer.duration));
  $('seek').value = mixer.position;
  $('elapsed').textContent = time(mixer.position);
  frame = requestAnimationFrame(tick);
}
function error(failure) {
  document.body.dataset.save = 'error';
  $('status').textContent = failure.message || 'Playback failed. Try again.';
}
async function load() {
  $('retry').hidden = true;
  $('status').textContent = 'Loading audio…';
  document.body.dataset.save = 'loading';
  let context;
  try {
    const data = await (await request(`/extractions/${extraction}/player`)).json();
    if (disposed) return;
    context = new AudioContext();
    // both tracks are decoded whole before playback, so they share one sample clock.
    const buffers = await Promise.all([data.vocals_url, data.instrumental_url].map(async url =>
      context.decodeAudioData(await (await request(url)).arrayBuffer())));
    if (disposed) { await context.close(); return; }
    drawWaveform(waveform(buffers[0], buffers[1]));
    mixer = new Mixer(context, buffers, buffers.map(peak));
    let cached = null;
    try { cached = JSON.parse(localStorage.getItem(key)); } catch {}
    const restored = restoredLevels(data, cached);
    $('vocal').value = restored.values[0];
    $('backing').value = restored.values[1];
    updateLevels(false);
    $('seek').max = mixer.duration;
    $('duration').textContent = time(mixer.duration);
    document.querySelectorAll('#play, #seek, #vocal, #backing').forEach(node => { node.disabled = false; });
    $('status').textContent = 'Mix saved';
    document.body.dataset.save = 'saved';
    if (restored.save) saver.set(...restored.values);
    tick();
  } catch (failure) {
    if (context && context.state !== 'closed') await context.close();
    if (!disposed) { error(failure); $('retry').hidden = false; }
  }
}
$('play').addEventListener('click', async () => {
  try { if (mixer.playing) mixer.pause(); else await mixer.play(); } catch (failure) { error(failure); }
});
$('seek').addEventListener('input', () => mixer.seek(Number($('seek').value)).catch(error));
for (const name of ['vocal', 'backing']) $(name).addEventListener('input', () => updateLevels());
$('retry').addEventListener('click', load);
$('retry-save').addEventListener('click', () => saver.flush());
addEventListener('online', () => saver.flush());
const resize = new ResizeObserver(() => {
  const height = Math.ceil(document.querySelector('main').getBoundingClientRect().height + 8);
  if (height !== lastHeight) {
    lastHeight = height;
    notify(`height:${height}`);
  }
});
resize.observe(document.querySelector('main'));
addEventListener('pagehide', () => {
  disposed = true;
  if (embedded) removeEventListener('wheel', relayWheel);
  resize.disconnect();
  saver.close();
  abort.abort();
  cancelAnimationFrame(frame);
  mixer?.close();
}, {once: true});
if (valid) load();
else $('status').textContent = 'Open this song from your Instrumentals library.';
