import { Mixer } from './mixer.js';

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
const abort = new AbortController();

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
    try { localStorage.setItem(key, JSON.stringify([vocal, backing])); } catch {}
  }
}
function compare(value) {
  original = value;
  $('cover').setAttribute('aria-pressed', String(!value));
  $('original').setAttribute('aria-pressed', String(value));
  updateLevels(false);
}
function tick() {
  if (!mixer || disposed) return;
  if (mixer.playing && mixer.position >= mixer.duration) mixer.pause();
  $('play').textContent = mixer.playing ? 'Pause' : 'Play';
  $('play').setAttribute('aria-label', mixer.playing ? 'Pause' : 'Play');
  $('seek').value = mixer.position;
  $('elapsed').textContent = time(mixer.position);
  frame = requestAnimationFrame(tick);
}
function error(error) {
  $('status').textContent = error.message || 'Playback failed. Try again.';
}
async function load() {
  $('retry').hidden = true;
  $('status').textContent = 'Loading aligned tracks…';
  notify('loading');
  let context;
  try {
    const data = await (await request(`/jobs/${encodeURIComponent(job)}/preview?revision=${revision}`)).json();
    if (disposed) return;
    context = new AudioContext();
    const buffers = await Promise.all([data.vocal_url, data.backing_url, data.original_url].map(async url =>
      context.decodeAudioData(await (await request(url)).arrayBuffer())));
    if (disposed) { await context.close(); return; }
    mixer = new Mixer(context, buffers, [data.vocal_peak, data.backing_peak]);
    let levels = [data.vocal_volume, data.backing_volume];
    try {
      const saved = JSON.parse(localStorage.getItem(key));
      if (Array.isArray(saved) && saved.length === 2 && saved.every(v => Number.isFinite(v) && v >= 0 && v <= 200)) levels = saved;
    } catch {}
    $('vocal').value = levels[0];
    $('backing').value = levels[1];
    updateLevels(false);
    $('seek').max = mixer.duration;
    $('duration').textContent = time(mixer.duration);
    document.querySelectorAll('input, button').forEach(node => { node.disabled = false; });
    $('status').textContent = 'Adjust while listening. These levels are used for export.';
    notify('ready');
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
$('cover').addEventListener('click', () => compare(false));
$('original').addEventListener('click', () => compare(true));
for (const name of ['vocal', 'backing']) $(name).addEventListener('input', () => { compare(false); updateLevels(); });
$('retry').addEventListener('click', load);
addEventListener('pagehide', () => {
  disposed = true;
  abort.abort();
  cancelAnimationFrame(frame);
  mixer?.close();
});
load();
