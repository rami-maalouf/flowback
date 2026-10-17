export function playbackStart(at, start, duration) {
  if (at === null || at === '' || !Number.isFinite(Number(at))) return {position: 0, moved: false};
  const offset = Number(at) - start;
  return offset >= 0 && offset < duration
    ? {position: offset, moved: false}
    : {position: 0, moved: true};
}

export function metricText(metric) {
  if (!metric?.available || !Number.isFinite(metric.value)) return 'Unavailable';
  const unit = metric.unit === 'semitones' ? 'st' : metric.unit;
  const value = metric.unit === 'ms' ? Math.round(metric.value) : Math.round(metric.value * 10) / 10;
  return `${value}${unit === '%' ? '' : ' '}${unit || ''}`.trim();
}

export function pitchChart(points) {
  const valid = points.filter(point => Number.isFinite(point.time));
  const values = valid.flatMap(point => [point.reference, point.performance]).filter(Number.isFinite);
  if (!valid.length || !values.length) return null;
  const start = Math.min(...valid.map(point => point.time));
  const end = Math.max(...valid.map(point => point.time));
  const limit = Math.max(2, Math.ceil(Math.max(...values.map(Math.abs))));
  const y = value => 90 - value / limit * 64;
  const series = name => {
    const segments = [];
    let segment = [];
    for (const point of valid) {
      if (Number.isFinite(point[name])) {
        segment.push({x: 36 + (point.time - start) / (end - start || 1) * 548, y: y(point[name])});
      } else if (segment.length) {
        segments.push(segment);
        segment = [];
      }
    }
    if (segment.length) segments.push(segment);
    return segments;
  };
  return {start, end, limit, reference: series('reference'), performance: series('performance')};
}
