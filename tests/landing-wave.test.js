import { afterEach, expect, test } from 'bun:test';
import { readFileSync } from 'node:fs';
import { Window } from 'happy-dom';
import { follow, snap } from '../landing/wave.js';

const windows = [];
afterEach(() => { for (const window of windows.splice(0)) window.happyDOM.abort(); });

test('the played part always ends in the gap after a bar', () => {
  expect(snap(0)).toBeCloseTo(6 / 444);
  expect(snap(.62)).toBeCloseTo(278 / 444);
  expect(snap(1)).toBe(1);
  expect(snap(-.5)).toBeCloseTo(6 / 444);
  expect(snap(2)).toBe(1);
  let last = 0;
  for (let x = 0; x <= 444; x++) {
    const at = snap(x / 444);
    expect(at).toBeGreaterThanOrEqual(last);
    if (at < 1) expect((at * 444) % 8).toBeCloseTo(6);
    last = at;
  }
});

test('the resting position in the stylesheet is a snapped one', () => {
  const rest = Number(readFileSync(new URL('../landing/landing.css', import.meta.url), 'utf8').match(/--at: ([\d.]+);/)[1]);
  expect(rest).toBeCloseTo(snap(rest), 4);
});

test('the waveform follows the pointer while it is over it and lets go when it leaves', () => {
  const window = new Window();
  windows.push(window);
  window.document.body.innerHTML = '<div class="wave"></div>';
  const wave = window.document.querySelector('.wave');
  wave.getBoundingClientRect = () => ({left: 100, width: 222});
  follow(wave);
  const pointer = (type, clientX) => wave.dispatchEvent(new (window.PointerEvent ?? window.MouseEvent)(type, {clientX}));

  pointer('pointerenter', 100 + 111);
  expect(wave.classList.contains('scrubbing')).toBe(true);
  expect(wave.style.getPropertyValue('--at')).toBe(snap(.5).toFixed(4));
  pointer('pointermove', 100 + 20);
  expect(wave.style.getPropertyValue('--at')).toBe(snap(20 / 222).toFixed(4));
  pointer('pointermove', 400);
  expect(wave.style.getPropertyValue('--at')).toBe('1.0000');
  pointer('pointerleave', 400);
  expect(wave.classList.contains('scrubbing')).toBe(false);
  // the intro must not run again: removing `animation: none` would restart it from the start.
  expect(wave.classList.contains('touched')).toBe(true);
  expect(wave.style.getPropertyValue('--at')).toBe('');
});
