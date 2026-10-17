export function gains(vocal, backing, vocalPeak, backingPeak) {
  const bound = vocalPeak * vocal / 100 + backingPeak * backing / 100;
  const scale = bound ? Math.min(1, .98 / bound) : 1;
  return [vocal / 100 * scale, backing / 100 * scale];
}

export class Mixer {
  constructor(context, buffers, peaks) {
    this.context = context;
    this.buffers = buffers;
    this.peaks = peaks;
    this.duration = buffers[0].duration;
    this.nodes = buffers.map(() => {
      const node = context.createGain();
      node.connect(context.destination);
      return node;
    });
    this.sources = [];
    this.offset = 0;
    this.started = 0;
    this.playing = false;
  }
  levels(vocal, backing, original = false) {
    const values = [...gains(vocal, backing, ...this.peaks), original ? 1 : 0];
    if (original) values[0] = values[1] = 0;
    this.nodes.forEach((node, i) => {
      const now = this.context.currentTime;
      node.gain.cancelScheduledValues(now);
      node.gain.setTargetAtTime(values[i], now, .005);
    });
  }
  get position() {
    return Math.min(this.duration, this.offset + (this.playing
      ? Math.max(0, this.context.currentTime - this.started) : 0));
  }
  async play() {
    await this.context.resume();
    if (this.playing) return;
    if (this.offset >= this.duration) this.offset = 0;
    this.started = this.context.currentTime + .03;
    this.sources = this.buffers.map((buffer, i) => {
      const source = this.context.createBufferSource();
      source.buffer = buffer;
      source.connect(this.nodes[i]);
      source.start(this.started, this.offset);
      return source;
    });
    this.playing = true;
  }
  pause() {
    this.offset = this.position;
    this.playing = false;
    this.sources.forEach(source => { source.stop(); source.disconnect(); });
    this.sources = [];
  }
  async seek(seconds) {
    const resume = this.playing;
    this.pause();
    this.offset = Math.max(0, Math.min(this.duration, seconds));
    if (resume) await this.play();
  }
  close() {
    this.pause();
    this.nodes.forEach(node => node.disconnect());
    return this.context.close();
  }
}
