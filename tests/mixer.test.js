import { expect, test } from 'bun:test';
import { Mixer, gains } from '../cover_engine/studio/mixer.js';

function fixture() {
  const sources = [];
  const context = {
    currentTime: 10, destination: {}, resume: async () => {}, close: async () => {},
    createGain: () => ({ gain: { cancelScheduledValues() {}, setTargetAtTime(value) { this.value = value; } }, connect() {}, disconnect() {} }),
    createBufferSource() {
      const source = { connect() {}, disconnect() {}, start(...args) { this.started = args; }, stop() { this.stopped = true; } };
      sources.push(source);
      return source;
    },
  };
  return { context, sources, mixer: new Mixer(context, [{duration: 40}, {duration: 40}, {duration: 40}], [.8, .6]) };
}

test('volume changes and comparison do not restart or desynchronize playing tracks', async () => {
  const { mixer, context, sources } = fixture();
  await mixer.play();
  expect(sources.map(s => s.started)).toEqual([[10.03, 0], [10.03, 0], [10.03, 0]]);
  context.currentTime = 15;
  mixer.levels(0, 80);
  expect(mixer.nodes.map(n => n.gain.value)).toEqual([0, .8, 0]);
  mixer.levels(100, 80, true);
  expect(mixer.nodes.map(n => n.gain.value)).toEqual([0, 0, 1]);
  expect(sources).toHaveLength(3);
  expect(sources.every(s => !s.stopped)).toBe(true);
  expect(mixer.position).toBeCloseTo(4.97);
  await mixer.seek(20);
  expect(sources.slice(3).map(s => s.started)).toEqual([[15.03, 20], [15.03, 20], [15.03, 20]]);
  expect(sources.slice(0, 3).every(s => s.stopped)).toBe(true);
  mixer.pause();
  context.currentTime = 30;
  expect(mixer.position).toBe(20);
  await mixer.play();
  expect(sources.slice(6).map(s => s.started)).toEqual([[30.03, 20], [30.03, 20], [30.03, 20]]);
});

test('gain protection preserves balance and exact mute at boosted levels', () => {
  const [voice, backing] = gains(200, 100, .8, .6);
  expect(voice / backing).toBe(2);
  expect(voice * .8 + backing * .6).toBeCloseTo(.98);
  expect(gains(0, 0, .8, .6)).toEqual([0, 0]);
});

test('waveform follows the supplied audio and keeps silent sections silent', async () => {
  const { waveform } = await import('../cover_engine/studio/waveform.js');
  const audio = samples => ({numberOfChannels: 1, getChannelData: () => new Float32Array(samples)});
  const shape = waveform(audio([0, 0, .5, .5]), audio([0, 0, 0, 0]), 2);
  expect(shape).toEqual([0, 1]);
  expect(waveform(audio([0, 0]), audio([0, 0]), 2)).toEqual([0, 0]);
});
