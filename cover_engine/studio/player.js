import { Mixer } from './mixer.js';
import { Autosave, restoredLevels } from './autosave.js';
import { waveform } from './waveform.js';
import { playbackStart } from './practice.js';
import { mountAssessment } from './assessment-view.js';

const $ = id => document.getElementById(id);
const params = new URLSearchParams(location.search);
const job = params.get('job');
const revision = Number(params.get('revision'));
const key = `flowback-live:${job}:${revision}`;
const notify = value => parent.postMessage(`${key}:${value}`, '*');
const time = seconds => `${Math.floor(seconds / 60)}:${String(Math.floor(seconds % 60)).padStart(2, '0')}`;
let mixer;
let original = false;
let disposed = false;
let frame;
let originalStart = 0;
let lastPosition = -1;
const abort = new AbortController();
const saver = new Autosave(async (payload, keepalive) => {
  const response = await fetch(`/jobs/${encodeURIComponent(job)}/levels`, {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({revision, ...payload}), keepalive,
  });
  if (!response.ok) throw new Error('Could not save the levels.');
}, {
  pending(payload) {
    try { localStorage.setItem(key, JSON.stringify({...payload, pending: true})); } catch {}
    $('status').textContent = 'Saving mix…';
    document.body.dataset.save = 'saving';
    $('retry-save').hidden = true;
    notify('saving');
  },
  saved() {
    try { localStorage.removeItem(key); } catch {}
    $('status').textContent = 'Mix saved on this Mac';
    document.body.dataset.save = 'saved';
    $('retry-save').hidden = true;
    notify('saved');
  },
  error() {
    $('status').textContent = 'Mix not saved. Please retry.';
    document.body.dataset.save = 'error';
    $('retry-save').hidden = false;
    notify('save-error');
  },
}, crypto.randomUUID());

async function request(url) {
  const response = await fetch(url, { signal: abort.signal });
  if (!response.ok) {
    let detail = 'Could not load the tracks. Check the audio engine and retry.';
    try { detail = (await response.json()).detail || detail; } catch {}
    throw new Error(detail);
  }
  return response;
}
function updateLevels(save = true) {
  const vocal = Number($('vocal').value);
  const backing = Number($('backing').value);
  $('vocal-value').textContent = vocal ? `${vocal}%` : 'Muted';
  $('backing-value').textContent = backing ? `${backing}%` : 'Muted';
  mixer?.levels(vocal, backing, original);
  notify(`vocal:${vocal}`);
  notify(`backing:${backing}`);
  if (save) {
    saver.set(vocal, backing);
  }
}
function compare(value) {
  assessment.pause();
  original = value;
  $('mix-hint').textContent = value ? 'Listening to original' : 'Adjust while listening';
  $('cover').setAttribute('aria-pressed', String(!value));
  $('original').setAttribute('aria-pressed', String(value));
  updateLevels(false);
}
function tick() {
  if (!mixer || disposed) return;
  if (mixer.playing && mixer.position >= mixer.duration) mixer.pause();
  $('play').dataset.playing = String(mixer.playing);
  $('played-width').setAttribute('width', String(600 * mixer.position / mixer.duration));
  $('play').setAttribute('aria-label', mixer.playing ? 'Pause' : 'Play');
  $('seek').value = mixer.position;
  $('elapsed').textContent = time(mixer.position);
  const position = originalStart + mixer.position;
  if (Math.abs(position - lastPosition) >= .25) {
    lastPosition = position;
    notify(`position:${position}`);
  }
  frame = requestAnimationFrame(tick);
}
function error(error) {
  document.body.dataset.save = 'error';
  $('status').textContent = error.message || 'Playback failed. Try again.';
}
async function load() {
  $('retry').hidden = true;
  $('status').textContent = 'Loading audio…';
  document.body.dataset.save = 'loading';
  notify('loading');
  let context;
  try {
    const [data, snapshot] = await Promise.all([
      request(`/jobs/${encodeURIComponent(job)}/preview?revision=${revision}`).then(response => response.json()),
      request(`/jobs/${encodeURIComponent(job)}`).then(response => response.json()),
    ]);
    if (disposed) return;
    context = new AudioContext();
    const buffers = await Promise.all([data.vocal_url, data.backing_url, data.original_url].map(async url =>
      context.decodeAudioData(await (await request(url)).arrayBuffer())));
    if (disposed) { await context.close(); return; }
    const bars = waveform(buffers[0], buffers[1]);
    for (const id of ['wave-bars', 'played-bars']) {
      $(id).replaceChildren(...bars.map((amplitude, index) => {
        const bar = document.createElementNS('http://www.w3.org/2000/svg', 'rect');
        const height = Math.max(2, amplitude * 48);
        for (const [name, value] of Object.entries({x: index * 5, y: (56 - height) / 2, width: 3, height, rx: 1.5})) bar.setAttribute(name, String(value));
        return bar;
      }));
    }
    mixer = new Mixer(context, buffers, [data.vocal_peak, data.backing_peak]);
    originalStart = snapshot.start;
    const requestedAt = params.get('at');
    const resume = playbackStart(Number(requestedAt) < 0 ? null : requestedAt, originalStart, mixer.duration);
    await mixer.seek(resume.position);
    $('position-note').hidden = !resume.moved;
    let cached = null;
    try { cached = JSON.parse(localStorage.getItem(key)); } catch {}
    const restored = restoredLevels(data, cached);
    $('vocal').value = restored.values[0];
    $('backing').value = restored.values[1];
    updateLevels(false);
    $('seek').max = mixer.duration;
    $('duration').textContent = time(mixer.duration);
    document.querySelectorAll('#play, #seek, #cover, #original, #vocal, #backing').forEach(node => { node.disabled = false; });
    $('status').textContent = 'Mix saved on this Mac';
    document.body.dataset.save = 'saved';
    if (restored.save) saver.set(...restored.values);
    notify('ready');
    tick();
  } catch (failure) {
    if (context && context.state !== 'closed') await context.close();
    if (!disposed) { error(failure); $('retry').hidden = false; }
  }
}
$('play').addEventListener('click', async () => {
  assessment.pause();
  try { if (mixer.playing) mixer.pause(); else await mixer.play(); } catch (failure) { error(failure); }
});
$('seek').addEventListener('input', () => mixer.seek(Number($('seek').value)).catch(error));
$('cover').addEventListener('click', () => compare(false));
$('original').addEventListener('click', () => compare(true));
for (const name of ['vocal', 'backing']) $(name).addEventListener('input', () => { compare(false); updateLevels(); });
$('retry').addEventListener('click', load);
$('retry-save').addEventListener('click', () => saver.flush());
addEventListener('online', () => saver.flush());
addEventListener('pagehide', () => {
  disposed = true;
  resize.disconnect();
  saver.close();
  abort.abort();
  cancelAnimationFrame(frame);
  mixer?.close();
  assessment.close();
});
const assessment = mountAssessment($('assessment'), {
  job, signal: abort.signal, changed: () => notify('assessment'), pauseMix: () => mixer?.pause(),
});
const resize = new ResizeObserver(() => notify(`height:${Math.ceil(document.querySelector('main').getBoundingClientRect().height + 8)}`));
resize.observe(document.querySelector('main'));
load();
