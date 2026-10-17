import { afterEach, expect, test } from 'bun:test';
import { Window } from 'happy-dom';
import { mountAssessment } from '../cover_engine/studio/assessment-view.js';

const restore = [];
function globals(values) {
  for (const [name, value] of Object.entries(values)) {
    const descriptor = Object.getOwnPropertyDescriptor(globalThis, name);
    Object.defineProperty(globalThis, name, {configurable: true, writable: true, value});
    restore.push(() => descriptor ? Object.defineProperty(globalThis, name, descriptor) : delete globalThis[name]);
  }
}
afterEach(() => { for (const reset of restore.splice(0).reverse()) reset(); });
async function until(check) {
  const deadline = Date.now() + 4000;
  while (!check()) {
    if (Date.now() > deadline) throw new Error('expected player state did not arrive');
    await Bun.sleep(5);
  }
}
const ready = {
  status: 'ready', version: 'practice-test', performance_text: 'our words', reference_text: 'their words',
  metrics: [{id: 'timing', label: 'Timing difference', available: true, value: 40, unit: 'ms', detail: 'Raw timing'}],
};
function dom() {
  const window = new Window({url: 'http://localhost/studio/?job=test&revision=1'});
  const observers = [];
  class Observer {
    constructor() { observers.push(this); }
    observe() {}
    disconnect() { this.disconnected = true; }
  }
  globals({document: window.document, ResizeObserver: Observer});
  restore.push(() => window.happyDOM.abort());
  return {window, observers};
}

test('queued analysis polls to ready, raw players exclude each other, and closing stops audio and observers', async () => {
  const {window, observers} = dom();
  let calls = 0;
  let pauses = 0;
  globals({fetch: async () => Response.json(++calls === 1 ? {status: 'queued'} : ready)});
  const root = document.createElement('section');
  document.body.append(root);
  const view = mountAssessment(root, {job: 'test', signal: new AbortController().signal, pauseMix: () => pauses++});
  await until(() => root.textContent.includes('Analyzing'));
  await until(() => root.querySelectorAll('audio').length === 2);
  expect(calls).toBe(2);
  const players = [...root.querySelectorAll('audio')];
  for (const audio of players) audio.pause = () => { audio.stopped = true; };
  players[0].dispatchEvent(new window.Event('play'));
  expect(pauses).toBe(1);
  expect(players[1].stopped).toBe(true);
  players[0].stopped = false;
  players[1].dispatchEvent(new window.Event('play'));
  expect(players[0].stopped).toBe(true);
  players.forEach(audio => { audio.stopped = false; });
  const details = root.querySelector('details');
  details.open = false;
  details.dispatchEvent(new window.Event('toggle'));
  expect(players.every(audio => audio.stopped)).toBe(true);
  players.forEach(audio => { audio.stopped = false; });
  view.close();
  expect(players.every(audio => audio.stopped)).toBe(true);
  expect(observers.every(observer => observer.disconnected)).toBe(true);
});

test('a ready response arriving after the analysis view closes cannot render', async () => {
  dom();
  let respond;
  globals({fetch: () => new Promise(resolve => { respond = resolve; })});
  const root = document.createElement('section');
  const view = mountAssessment(root, {job: 'test', signal: new AbortController().signal});
  view.close();
  respond(Response.json(ready));
  await Bun.sleep(20);
  expect(root.querySelectorAll('audio')).toHaveLength(0);
  expect(root.textContent).not.toContain('Raw timing');
});

test('the actual player keeps listening through analysis retry and cleans up mix and raw audio on navigation', async () => {
  const {window, observers} = dom();
  const html = await Bun.file(new URL('../cover_engine/studio/index.html', import.meta.url)).text();
  document.body.innerHTML = html.match(/<body>([\s\S]*)<\/body>/)[1];
  const sources = [];
  const contexts = [];
  const signals = [];
  let assessmentReady = false;
  const requests = [];
  class Context {
    constructor() { this.currentTime = 0; this.destination = {}; contexts.push(this); }
    async resume() {}
    async close() { this.closed = true; }
    async decodeAudioData() { return {duration: 4, numberOfChannels: 1, getChannelData: () => new Float32Array([.1, .2])}; }
    createGain() { return {gain: {cancelScheduledValues() {}, setTargetAtTime() {}}, connect() {}, disconnect() {}}; }
    createBufferSource() {
      const source = {connect() {}, disconnect() {}, start() { this.playing = true; }, stop() { this.playing = false; }};
      sources.push(source);
      return source;
    }
  }
  globals({
    location: window.location, localStorage: window.localStorage,
    parent: {postMessage() {}}, AudioContext: Context,
    addEventListener: window.addEventListener.bind(window),
    requestAnimationFrame: () => 1, cancelAnimationFrame() {},
    fetch: async (url, options = {}) => {
      if (options.signal) signals.push(options.signal);
      if (url.endsWith('/assessment')) {
        requests.push(options.method);
        if (options.method === 'POST') assessmentReady = true;
        return Response.json(assessmentReady ? ready : {status: 'failed', error: 'Try analysis again'});
      }
      if (url.includes('/preview?')) return Response.json({vocal_url: '/v.wav', backing_url: '/b.wav', original_url: '/r.wav', vocal_peak: .2, backing_peak: .2, vocal_volume: 100, backing_volume: 100});
      if (url === '/jobs/test') return Response.json({start: 10});
      if (url.endsWith('.wav')) return new Response(new ArrayBuffer(8));
      throw new Error(`unexpected player request ${url}`);
    },
  });
  await import('../cover_engine/studio/player.js?lifecycle-test');
  await until(() => !document.getElementById('play').disabled);
  expect(document.getElementById('assessment').textContent).toContain('Try analysis again');
  document.getElementById('play').click();
  await until(() => sources.filter(source => source.playing).length === 3);
  document.querySelector('#assessment button').click();
  await until(() => document.querySelectorAll('audio').length === 2);
  expect(requests).toEqual(['GET', 'POST', 'GET']);
  expect(sources.filter(source => source.playing)).toHaveLength(3);
  const raw = [...document.querySelectorAll('audio')];
  raw.forEach(audio => { audio.pause = () => { audio.stopped = true; }; });
  raw[0].dispatchEvent(new window.Event('play'));
  expect(sources.filter(source => source.playing)).toHaveLength(0);
  document.getElementById('play').click();
  await until(() => sources.filter(source => source.playing).length === 3);
  expect(raw.every(audio => audio.stopped)).toBe(true);
  raw.forEach(audio => { audio.stopped = false; });
  window.dispatchEvent(new window.Event('pagehide'));
  expect(sources.filter(source => source.playing)).toHaveLength(0);
  expect(contexts.every(context => context.closed)).toBe(true);
  expect(raw.every(audio => audio.stopped)).toBe(true);
  expect(signals.every(signal => signal.aborted)).toBe(true);
  expect(observers.every(observer => observer.disconnected)).toBe(true);
});
