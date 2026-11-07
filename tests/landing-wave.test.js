import { afterEach, expect, test } from 'bun:test';
import { readFileSync } from 'node:fs';
import { Window } from 'happy-dom';
import { REST, chase, follow, snap } from '../landing/wave.js';

const windows = [];
afterEach(() => { for (const window of windows.splice(0)) window.happyDOM.abort(); });

test('the played part comes to rest in the gap after a bar', () => {
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

test('the stylesheet rests where the script does', () => {
  const css = readFileSync(new URL('../landing/landing.css', import.meta.url), 'utf8');
  expect(Number(css.match(/--at: ([\d.]+);/)[1])).toBe(Number(REST.toFixed(4)));
});

test('the chase eases in, never overshoots, and takes the same path at any frame rate', () => {
  const at60 = chase(chase(0, 1, 1 / 60, .14), 1, 1 / 60, .14);
  const at30 = chase(0, 1, 2 / 60, .14);
  expect(at60).toBeCloseTo(at30, 12);
  let position = 0;
  let step = Infinity;
  for (let frame = 0; frame < 60; frame++) {
    const next = chase(position, 1, 1 / 60, .14);
    expect(next).toBeGreaterThan(position);
    expect(next).toBeLessThanOrEqual(1);
    expect(next - position).toBeLessThan(step);
    step = next - position;
    position = next;
  }
});

function strip({reduced = false, headAt = REST} = {}) {
  const window = new Window();
  windows.push(window);
  window.document.body.innerHTML = '<div class="wave"><span class="head"></span></div>';
  const wave = window.document.querySelector('.wave');
  wave.getBoundingClientRect = () => ({left: 100, width: 222});
  wave.querySelector('.head').getBoundingClientRect = () => ({left: 100 + headAt * 222 - 1.5, width: 3});
  const frames = [];
  let time = 0;
  follow(wave, {frame: (callback) => frames.push(callback), reduced: () => reduced});
  return {
    at: () => Number(wave.style.getPropertyValue('--at')),
    wave,
    frames,
    pointer: (type, clientX) => wave.dispatchEvent(new (window.PointerEvent ?? window.MouseEvent)(type, {clientX})),
    // runs 60 Hz frames for `ms` milliseconds and returns where the playhead was after each.
    run(ms) {
      const seen = [];
      for (let elapsed = 0; elapsed < ms; elapsed += 1000 / 60) {
        time += 1000 / 60;
        for (const callback of frames.splice(0)) callback(time);
        seen.push(Number(wave.style.getPropertyValue('--at')));
      }
      return seen;
    },
  };
}

test('the playhead follows the pointer a little behind it, comes to rest on a gap, and glides back', () => {
  const {at, wave, frames, pointer, run} = strip();
  pointer('pointerenter', 100 + .2 * 222);
  expect(wave.classList.contains('touched')).toBe(true);
  const towards = run(60);
  expect(towards[0]).toBeLessThan(REST);
  expect(towards[0]).toBeGreaterThan(snap(.2) + (REST - snap(.2)) / 2);
  for (let i = 1; i < towards.length; i++) expect(towards[i]).toBeLessThan(towards[i - 1]);
  run(1000);
  expect(at()).toBe(Number(snap(.2).toFixed(4)));
  expect(frames.length).toBe(0);

  pointer('pointermove', 100 + .9 * 222);
  pointer('pointermove', 100 + .95 * 222);
  expect(frames.length).toBe(1);
  run(1500);
  expect(at()).toBe(Number(snap(.95).toFixed(4)));

  pointer('pointerleave', 0);
  const back = run(1500);
  for (let i = 1; i < back.length; i++) expect(back[i]).toBeLessThanOrEqual(back[i - 1]);
  expect(at()).toBe(Number(REST.toFixed(4)));
  expect(frames.length).toBe(0);
});

test('the first touch starts from wherever the opening sweep is', () => {
  const {at, pointer, run} = strip({headAt: .3});
  pointer('pointerenter', 100 + .8 * 222);
  expect(at()).toBe(.3);
  expect(run(20)[0]).toBeGreaterThan(.3);
});

test('with reduced motion the playhead goes straight to the pointer', () => {
  const {at, frames, pointer} = strip({reduced: true});
  pointer('pointerenter', 100 + .2 * 222);
  expect(at()).toBe(Number(snap(.2).toFixed(4)));
  pointer('pointerleave', 0);
  expect(at()).toBe(Number(REST.toFixed(4)));
  expect(frames.length).toBe(0);
});
