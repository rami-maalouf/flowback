// show the aligned audio's envelope, using both tracks so quiet vocals retain context.
export function waveform(vocal, backing, count = 120) {
  const values = Array.from({length: count}, () => 0);
  for (const buffer of [vocal, backing]) {
    for (let channel = 0; channel < buffer.numberOfChannels; channel++) {
      const data = buffer.getChannelData(channel);
      for (let bin = 0; bin < count; bin++) {
        const start = Math.floor(bin * data.length / count);
        const end = Math.floor((bin + 1) * data.length / count);
        let energy = 0;
        const step = Math.max(1, Math.floor((end - start) / 128));
        let samples = 0;
        for (let i = start; i < end; i += step) { energy += data[i] * data[i]; samples++; }
        values[bin] = Math.max(values[bin], samples ? Math.sqrt(energy / samples) : 0);
      }
    }
  }
  const peak = Math.max(...values);
  return values.map(value => peak ? Math.sqrt(value / peak) : 0);
}
