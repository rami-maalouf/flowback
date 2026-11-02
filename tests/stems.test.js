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
async function player(id = 'a'.repeat(32)) {
  const window = new Window({url: `http://localhost/studio/stems.html?extraction=${id}`});
  const html = await Bun.file(new URL('../cover_engine/studio/stems.html', import.meta.url)).text();
  window.document.body.innerHTML = html.match(/<body>([\s\S]*)<\/body>/)[1];
  const messages = [];
  const observers = [];
  globals({document: window.document, location: window.location,
    parent: {postMessage: value => messages.push(value)},
    addEventListener: window.addEventListener.bind(window),
    ResizeObserver: class {
      constructor(callback) { this.callback = callback; observers.push(this); }
      observe() { this.callback(); }
      disconnect() { this.disconnected = true; }
    },
  });
  const audio = [...document.querySelectorAll('audio')];
  audio.forEach(item => {
    item.pause = () => { item.pauses = (item.pauses || 0) + 1; };
    item.load = () => { item.loads = (item.loads || 0) + 1; };
  });
  resets.push(() => window.happyDOM.abort());
  await import(`../cover_engine/studio/stems.js?test=${++serial}`);
  return {window, audio, messages, observers};
}

test('streams the full stems, plays one at a time, and stops on navigation', async () => {
  const {window, audio, messages, observers} = await player();
  expect(audio[0].src).toEndWith(`/extractions/${'a'.repeat(32)}/audio/instrumental.wav`);
  expect(audio[1].src).toEndWith(`/extractions/${'a'.repeat(32)}/audio/vocals.wav`);
  expect(audio.every(item => !item.autoplay && item.preload === 'metadata')).toBe(true);
  audio[0].dispatchEvent(new window.Event('loadedmetadata'));
  expect(document.getElementById('instrumental-status').textContent).toBe('Ready to play');
  audio[0].dispatchEvent(new window.Event('play'));
  expect(audio[1].pauses).toBe(1);
  audio[1].dispatchEvent(new window.Event('play'));
  expect(audio[0].pauses).toBe(1);
  expect(messages.some(value => value.startsWith(`flowback-stems:${'a'.repeat(32)}:height:`))).toBe(true);
  window.dispatchEvent(new window.Event('pagehide'));
  expect(audio.every(item => item.pauses === 2)).toBe(true);
  expect(observers.every(observer => observer.disconnected)).toBe(true);
});

test('failed audio offers a retry without disturbing the other track', async () => {
  const {window, audio} = await player();
  audio[0].dispatchEvent(new window.Event('error'));
  expect(document.getElementById('instrumental-status').textContent).toContain('Could not load');
  expect(document.getElementById('instrumental-retry').hidden).toBe(false);
  document.getElementById('instrumental-retry').click();
  expect(audio[0].loads).toBe(1);
  expect(audio[1].loads).toBeUndefined();
  expect(document.getElementById('instrumental-retry').hidden).toBe(true);
  expect(document.getElementById('instrumental-status').textContent).toBe('Loading audio…');
  audio[0].dispatchEvent(new window.Event('canplay'));
  expect(document.getElementById('instrumental-status').textContent).toBe('Ready to play');
});

test('invalid extraction links never request audio', async () => {
  const {audio} = await player('invalid');
  expect(audio.every(item => !item.getAttribute('src'))).toBe(true);
  expect(document.getElementById('instrumental-status').textContent).toContain('Open this song');
  expect(document.getElementById('instrumental-retry').hidden).toBe(true);
});
