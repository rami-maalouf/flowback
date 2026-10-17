import { expect, test } from 'bun:test';
import { metricText, playbackStart, pitchChart } from '../cover_engine/studio/practice.js';

test('take switching preserves original-song time only inside the new section', () => {
  expect(playbackStart('245.5', 240, 30)).toEqual({ position: 5.5, moved: false });
  expect(playbackStart('245.5', 250, 12)).toEqual({ position: 0, moved: true });
  expect(playbackStart('270', 240, 30)).toEqual({ position: 0, moved: true });
  expect(playbackStart(null, 240, 30)).toEqual({ position: 0, moved: false });
  expect(playbackStart('garbage', 240, 30)).toEqual({ position: 0, moved: false });
});

test('unavailable measurements never appear as zero or an accuracy grade', () => {
  expect(metricText({ available: false, value: null, unit: '%' })).toBe('Unavailable');
  expect(metricText({ available: true, value: null, unit: '%' })).toBe('Unavailable');
  expect(metricText({ available: true, value: NaN, unit: 'ms' })).toBe('Unavailable');
  expect(metricText({ available: true, value: 0, unit: 'ms' })).toBe('0 ms');
  expect(metricText({ available: true, value: 91.456, unit: '%' })).toBe('91.5%');
  expect(metricText({ available: true, value: 2.456, unit: 'semitones' })).toBe('2.5 st');
});

test('pitch evidence uses original time and leaves missing observations disconnected', () => {
  const chart = pitchChart([
    {time: 100, reference: -2, performance: -1},
    {time: 102, reference: 0, performance: null},
    {time: 104, reference: 2, performance: 1},
  ]);
  expect(chart.start).toBe(100);
  expect(chart.end).toBe(104);
  expect(chart.reference).toHaveLength(1);
  expect(chart.performance).toHaveLength(2);
  expect(chart.performance[0]).toHaveLength(1);
  expect(chart.performance[1]).toHaveLength(1);
  expect(chart.reference[0].map(point => point.x)).toEqual([36, 310, 584]);
  expect(pitchChart([])).toBeNull();
});
