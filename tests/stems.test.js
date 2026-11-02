import {afterEach, expect, test} from 'bun:test';
import {Window} from 'happy-dom';

const resets = [];
let serial = 0;
afterEach(() => { for (const reset of resets.splice(0).reverse()) reset(); });
function globals(values) {
  for (const [name, value] of Object.entries(values)) {
    const previous = Object.getOwnPropertyDescriptor(globalThis, name);
    Object.defineProperty(globalThis, name, {configurable: true, writable: true, value});
    resets.push(() => previous ? Object.defineProperty(globalThis, name, previous) : delete globalThis[name]);
  }
}
const id = 'a'.repeat(32);
const settled = () => new Promise(resolve => setTimeout(resolve, 0));
async function until(check) {
  for (let attempt = 0; attempt < 100 && !check(); attempt++) await new Promise(resolve => setTimeout(resolve, 10));
  expect(check()).toBe(true);
}

class Context {
  static made = [];
  constructor() {
    this.currentTime = 0;
    this.destination = {};
    this.state = 'running';
    this.sources = [];
    Context.made.push(this);
  }
  async resume() {}
  async close() { this.state = 'closed'; }
  async decodeAudioData() {
    const data = Float32Array.from({length: 1000}, (_, i) => Math.sin(i / 10) * .5);
    return {duration: 30, numberOfChannels: 1, getChannelData: () => data};
  }
  createGain() {
    return {gain: {cancelScheduledValues() {}, setTargetAtTime(value) { this.value = value; }}, connect() {}, disconnect() {}};
  }
  createBufferSource() {
    const source = {connect() {}, disconnect() {}, start(...args) { this.started = args; }, stop() { this.stopped = true; }};
    this.sources.push(source);
    return source;
  }
}

async function player(extraction = id, answer = {}) {
  const window = new Window({url: `http://localhost/studio/stems.html?extraction=${extraction}`});
  const html = await Bun.file(new URL('../cover_engine/studio/stems.html', import.meta.url)).text();
  window.document.body.innerHTML = html.match(/<body>([\s\S]*)<\/body>/)[1];
  const messages = [];
  const requests = [];
  const observers = [];
  Context.made = [];
  const responses = {
    player: () => Response.json({
      duration: 30, vocals_url: `/extractions/${extraction}/player/vocals.flac`,
      instrumental_url: `/extractions/${extraction}/player/instrumental.flac`,
      vocal_volume: 40, backing_volume: 0, levels_saved: true,
    }),
    ...answer,
  };
  globals({window, document: window.document, location: window.location, localStorage: window.localStorage,
    parent: {postMessage: value => messages.push(value)},
    addEventListener: window.addEventListener.bind(window),
    removeEventListener: window.removeEventListener.bind(window),
    requestAnimationFrame: () => 1, cancelAnimationFrame() {},
    AudioContext: Context,
    ResizeObserver: class {
      constructor(callback) { this.callback = callback; observers.push(this); }
      observe() { this.callback(); }
      disconnect() { this.disconnected = true; }
    },
    fetch: async (url, options = {}) => {
      requests.push({url, ...options});
      if (url.endsWith('/player')) return responses.player();
      if (url.endsWith('/levels')) return Response.json({ok: true});
      return new Response(new ArrayBuffer(8));
    },
  });
  resets.push(() => window.happyDOM.abort());
  await import(`../cover_engine/studio/stems.js?test=${++serial}`);
  return {window, messages, requests, observers, responses};
}
const $ = name => document.getElementById(name);

test('plays both tracks in one mixer with saved levels that change while playing', async () => {
  const {window, messages, requests, observers} = await player();
  await until(() => $('status').textContent === 'Mix saved');
  expect(requests.map(request => request.url)).toEqual([
    `/extractions/${id}/player`, `/extractions/${id}/player/vocals.flac`, `/extractions/${id}/player/instrumental.flac`,
  ]);
  expect([$('vocal').value, $('backing').value]).toEqual(['40', '0']);
  expect([$('vocal-value').textContent, $('backing-value').textContent]).toEqual(['40%', 'Muted']);
  expect(['play', 'seek', 'vocal', 'backing'].every(name => !$(name).disabled)).toBe(true);
  expect($('wave-bars').children).toHaveLength(120);
  expect($('duration').textContent).toBe('0:30');
  expect(messages.some(value => value.startsWith(`flowback-stems:${id}:height:`))).toBe(true);

  $('play').click();
  await settled();
  const [context] = Context.made;
  expect(context.sources.map(source => source.started)).toEqual([[.03, 0], [.03, 0]]);
  $('vocal').value = '150';
  $('vocal').dispatchEvent(new window.Event('input'));
  expect($('vocal-value').textContent).toBe('150%');
  expect($('status').textContent).toBe('Saving mix…');
  expect(context.sources.every(source => !source.stopped)).toBe(true);
  await until(() => $('status').textContent === 'Mix saved');
  const save = requests.find(request => request.url.endsWith('/levels'));
  expect(save.method).toBe('POST');
  expect(JSON.parse(save.body)).toMatchObject({vocal_volume: 150, backing_volume: 0, sequence: 1});

  window.dispatchEvent(new window.Event('pagehide'));
  expect(context.state).toBe('closed');
  expect(observers.every(observer => observer.disconnected)).toBe(true);
});

test('a failed load offers a retry that loads the mixer', async () => {
  let fail = true;
  await player(id, {player: () => fail
    ? Response.json({detail: 'Wait until this song finishes extracting.'}, {status: 409})
    : Response.json({duration: 30, vocals_url: 'v', instrumental_url: 'i', vocal_volume: 100, backing_volume: 100, levels_saved: false})});
  await until(() => !$('retry').hidden);
  expect($('status').textContent).toBe('Wait until this song finishes extracting.');
  expect($('play').disabled).toBe(true);
  fail = false;
  $('retry').click();
  await until(() => $('status').textContent === 'Mix saved');
  expect($('retry').hidden).toBe(true);
  expect($('play').disabled).toBe(false);
});

test('invalid extraction links never request audio', async () => {
  const {requests} = await player('invalid');
  await settled();
  expect(requests).toEqual([]);
  expect($('status').textContent).toContain('Open this song');
  expect($('play').disabled).toBe(true);
});
