import { Mixer } from './mixer.js';
import { Autosave, restoredLevels } from './autosave.js';
import { drawWaveform, waveform } from './waveform.js';

const $ = id => document.getElementById(id);
const params = new URLSearchParams(location.search);
const extraction = params.get('extraction') || '';
const valid = /^[a-f0-9]{32}$/.test(extraction);
const key = `flowback-stems:${extraction}`;
// the level each track returns to when it is unmuted; the server only stores what plays.
const unmutedKey = `flowback-stems-unmuted:${extraction}`;
const names = ['vocal', 'backing'];
const muted = {vocal: false, backing: false};
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
const silent = name => muted[name] || Number($(name).value) === 0;
const audible = name => silent(name) ? 0 : Number($(name).value);
function remembered() {
  try {
    const value = JSON.parse(localStorage.getItem(unmutedKey));
    return names.map(name => Number.isFinite(value?.[name]) && value[name] > 0 && value[name] <= 200 ? value[name] : 100);
  } catch { return [100, 100]; }
}
function remember() {
  const values = remembered();
  names.forEach((name, i) => { if (Number($(name).value) > 0) values[i] = Number($(name).value); });
  try { localStorage.setItem(unmutedKey, JSON.stringify({vocal: values[0], backing: values[1]})); } catch {}
}
function updateLevels(save = true) {
  for (const name of names) {
    const value = Number($(name).value);
    const off = silent(name);
    $(`${name}-value`).textContent = off ? 'Muted' : `${value}%`;
    $(`${name}-strip`).dataset.muted = String(off);
    $(`${name}-strip`).style.setProperty('--p', String(value / 200));
    const button = $(`${name}-mute`);
    button.setAttribute('aria-pressed', String(off));
    const label = name === 'vocal' ? 'vocals' : 'instrumental';
    button.setAttribute('aria-label', `${off ? 'Unmute' : 'Mute'} ${label}`);
    button.title = `${off ? 'Unmute' : 'Mute'} ${label} (${name === 'vocal' ? 'V' : 'I'})`;
  }
  const [vocal, backing] = names.map(audible);
  mixer?.levels(vocal, backing);
  if (save) {
    remember();
    saver.set(vocal, backing);
  }
}
function toggleMute(name) {
  if ($(name).disabled) return;
  if (silent(name)) {
    muted[name] = false;
    if (Number($(name).value) === 0) $(name).value = remembered()[names.indexOf(name)];
  } else {
    muted[name] = true;
  }
  updateLevels();
}
function setLevel(name, value) {
  muted[name] = false;
  $(name).value = value;
  updateLevels();
}
// a slim live meter per track, measured after its gain so mute and level both show.
let meters = [];
function meter(context) {
  if (typeof context.createAnalyser !== 'function') return [];
  return mixer.nodes.map(node => {
    const analyser = context.createAnalyser();
    analyser.fftSize = 1024;
    node.connect(analyser);
    return {analyser, data: new Float32Array(analyser.fftSize), level: 0};
  });
}
function drawMeters() {
  meters.forEach((entry, i) => {
    let target = 0;
    if (mixer.playing) {
      entry.analyser.getFloatTimeDomainData(entry.data);
      let energy = 0;
      for (const sample of entry.data) energy += sample * sample;
      const rms = Math.sqrt(energy / entry.data.length);
      // map -48 dB..0 dB to the meter width.
      target = rms > 0 ? Math.max(0, Math.min(1, 1 + 20 * Math.log10(rms) / 48)) : 0;
    }
    entry.level = target > entry.level ? target : entry.level * .9 + target * .1;
    $(`${names[i]}-meter`).style.clipPath = `inset(0 ${(100 - entry.level * 100).toFixed(1)}% 0 0 round 2px)`;
  });
}
function tick() {
  if (!mixer || disposed) return;
  if (mixer.playing && mixer.position >= mixer.duration) mixer.pause();
  $('play').dataset.playing = String(mixer.playing);
  $('play').setAttribute('aria-label', mixer.playing ? 'Pause' : 'Play');
  $('played-width').setAttribute('width', String(600 * mixer.position / mixer.duration));
  $('seek').value = mixer.position;
  $('elapsed').textContent = time(mixer.position);
  drawMeters();
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
    meters = meter(context);
    let cached = null;
    try { cached = JSON.parse(localStorage.getItem(key)); } catch {}
    const restored = restoredLevels(data, cached);
    const back = remembered();
    // a silent track keeps its slider at the level it returns to, shown as muted.
    names.forEach((name, i) => {
      muted[name] = restored.values[i] === 0;
      $(name).value = muted[name] ? back[i] : restored.values[i];
    });
    updateLevels(false);
    $('seek').max = mixer.duration;
    $('duration').textContent = time(mixer.duration);
    document.querySelectorAll('#play, #seek, #vocal, #backing, #vocal-mute, #backing-mute').forEach(node => { node.disabled = false; });
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
let dragging = false;
for (const name of names) {
  const slider = $(name);
  slider.addEventListener('pointerdown', () => { dragging = true; });
  slider.addEventListener('input', () => {
    // a soft detent at 100% while dragging; arrow keys still step through it.
    const value = Number(slider.value);
    if (dragging && value !== 100 && Math.abs(value - 100) <= 4) slider.value = 100;
    muted[name] = false;
    updateLevels();
  });
  slider.addEventListener('dblclick', () => { if (!slider.disabled) setLevel(name, 100); });
  $(`${name}-mute`).addEventListener('click', () => toggleMute(name));
}
addEventListener('pointerup', () => { dragging = false; });
addEventListener('pointercancel', () => { dragging = false; });
if (typeof matchMedia === 'function' && matchMedia('(pointer: coarse)').matches) $('mix-hint').textContent = 'Tap a speaker to mute a track';
const shortcuts = {v: 'vocal', i: 'backing'};
const shortcut = event => {
  const name = shortcuts[event.key?.toLowerCase()];
  if (!name || event.metaKey || event.ctrlKey || event.altKey || event.repeat) return;
  if (event.target?.closest?.('input:not([type="range"]), textarea, select, [contenteditable]')) return;
  event.preventDefault();
  toggleMute(name);
};
addEventListener('keydown', shortcut);
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
  removeEventListener('keydown', shortcut);
  resize.disconnect();
  saver.close();
  abort.abort();
  cancelAnimationFrame(frame);
  mixer?.close();
}, {once: true});
if (valid) load();
else $('status').textContent = 'Open this song from your Instrumentals library.';
