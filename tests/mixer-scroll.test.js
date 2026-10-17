import { afterEach, expect, test } from 'bun:test';
import { Window } from 'happy-dom';
import { destroy, element, elementEnded } from '../modules/web/index.js';

const cleanup = [];
function globals(values) {
  for (const [name, value] of Object.entries(values)) {
    const previous = Object.getOwnPropertyDescriptor(globalThis, name);
    Object.defineProperty(globalThis, name, {configurable: true, writable: true, value});
    cleanup.push(() => previous ? Object.defineProperty(globalThis, name, previous) : delete globalThis[name]);
  }
}
afterEach(() => {
  destroy();
  for (const close of cleanup.splice(0).reverse()) close();
});

function page() {
  const window = new Window({url: 'https://flowback.test/', settings: {disableIframePageLoading: true}});
  cleanup.push(() => window.happyDOM.abort());
  globals({
    window, document: window.document,
    getComputedStyle: window.getComputedStyle.bind(window),
    addEventListener: window.addEventListener.bind(window),
    removeEventListener: window.removeEventListener.bind(window),
  });
  return window;
}

function host() {
  const window = page();
  document.body.innerHTML = '<div id="cover-content" style="overflow-y:auto"><div style="overflow:hidden"><iframe></iframe></div></div>';
  const outer = document.getElementById('cover-content');
  const frame = document.querySelector('iframe');
  frame.src = 'https://engine.test/studio/?job=take-1&revision=3&embedded=1';
  Object.defineProperty(outer, 'clientHeight', {value: 800});
  const moves = [];
  outer.scrollBy = options => moves.push(options);
  const handle = {hatch: 'mixer-scroll', element: frame};
  const send = (data = 'flowback-live:take-1:3:scroll:0:20', overrides = {}) => {
    window.dispatchEvent(new window.MessageEvent('message', {
      data, origin: 'https://engine.test', source: frame.contentWindow, ...overrides,
    }));
  };
  element(handle);
  return {window, outer, frame, handle, moves, send};
}

test('the mixer scrolls its outer container with pixel, line and page wheel units', () => {
  const {moves, send, handle} = host();
  element(handle);
  send('flowback-live:take-1:3:scroll:0:20.5');
  send('flowback-live:take-1:3:scroll:1:-2');
  send('flowback-live:take-1:3:scroll:2:1');
  expect(moves).toEqual([
    {top: 20.5, behavior: 'instant'},
    {top: -32, behavior: 'instant'},
    {top: 800, behavior: 'instant'},
  ]);
});

test('only the current embedded iframe source, origin and take can scroll the page', () => {
  const {window, frame, moves, send} = host();
  send(undefined, {source: window});
  send(undefined, {origin: 'https://other.test'});
  for (const payload of [
    'flowback-live:take-2:3:scroll:0:20', 'flowback-live:take-1:2:scroll:0:20',
    'flowback-live:take-1:3:scroll:3:20', 'flowback-live:take-1:3:scroll:0:Infinity',
    'flowback-live:take-1:3:scroll:0:NaN', 'flowback-live:take-1:3:scroll:0:',
    'flowback-live:take-1:3:scroll:0:20:extra', {deltaY: 20},
  ]) send(payload);
  frame.src = 'https://engine.test/studio/?job=take-1&revision=4&embedded=1';
  send();
  frame.src = 'https://engine.test/studio/?job=take-1&revision=3';
  send();
  frame.remove();
  send();
  expect(moves).toEqual([]);
});

test('ending a hatch, destroying the module and hiding the page remove its listeners', () => {
  const {window, handle, moves, send} = host();
  elementEnded(handle);
  send();
  expect(moves).toEqual([]);
  element(handle);
  send();
  expect(moves).toHaveLength(1);
  destroy();
  send();
  expect(moves).toHaveLength(1);
  element(handle);
  window.dispatchEvent(new window.Event('pagehide'));
  send();
  expect(moves).toHaveLength(1);
});

test('a mixer without a scroll container never scrolls the window', () => {
  const {window, outer, moves, send} = host();
  let windowMoves = 0;
  window.scrollBy = () => windowMoves++;
  outer.style.overflowY = 'visible';
  send();
  expect(moves).toEqual([]);
  expect(windowMoves).toBe(0);
});

test('the embedded player relays cancelable vertical wheels and preserves zoom and horizontal input', async () => {
  const window = page();
  window.location.href = 'https://engine.test/studio/?job=take-1&revision=3&embedded=1';
  const html = await Bun.file(new URL('../cover_engine/studio/index.html', import.meta.url)).text();
  document.body.innerHTML = html.match(/<body>([\s\S]*)<\/body>/)[1];
  const messages = [];
  globals({
    location: window.location, localStorage: window.localStorage,
    parent: {postMessage: value => messages.push(value)},
    ResizeObserver: class { observe() {} disconnect() {} },
    requestAnimationFrame: () => 1, cancelAnimationFrame() {},
    fetch: async () => Response.json({error: 'offline'}, {status: 503}),
  });
  await import('../cover_engine/studio/player.js?mixer-scroll-test');
  const wheel = options => {
    const event = new window.WheelEvent('wheel', {cancelable: true, deltaY: 24, ...options});
    // happy-dom's wheel event omits the mouse event modifier fields.
    Object.defineProperty(event, 'ctrlKey', {value: options.ctrlKey ?? false});
    window.dispatchEvent(event);
    return event;
  };
  expect(wheel({deltaMode: 1, deltaY: -2}).defaultPrevented).toBe(true);
  expect(messages.filter(value => value.includes(':scroll:'))).toEqual(['flowback-live:take-1:3:scroll:1:-2']);
  expect(wheel({ctrlKey: true}).defaultPrevented).toBe(false);
  expect(wheel({deltaX: 50}).defaultPrevented).toBe(false);
  expect(wheel({cancelable: false}).defaultPrevented).toBe(false);
  expect(messages.filter(value => value.includes(':scroll:'))).toHaveLength(1);
  window.dispatchEvent(new window.Event('pagehide'));
  expect(wheel({}).defaultPrevented).toBe(false);
  expect(messages.filter(value => value.includes(':scroll:'))).toHaveLength(1);
});
