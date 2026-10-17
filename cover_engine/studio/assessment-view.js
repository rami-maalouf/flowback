import { metricText, pitchChart } from './practice.js';

const svgNamespace = 'http://www.w3.org/2000/svg';
const time = seconds => `${Math.floor(seconds / 60)}:${String(Math.floor(seconds % 60)).padStart(2, '0')}`;
function element(tag, text, className) {
  const node = document.createElement(tag);
  if (text) node.textContent = text;
  if (className) node.className = className;
  return node;
}
function svgElement(tag, attributes, text) {
  const node = document.createElementNS(svgNamespace, tag);
  for (const [key, value] of Object.entries(attributes)) node.setAttribute(key, String(value));
  if (text) node.textContent = text;
  return node;
}
function pitchPlot(pitch) {
  const chart = pitchChart(pitch?.points || []);
  if (!chart) return null;
  const section = element('section', '', 'pitch-evidence');
  const heading = element('h3', 'Pitch movement');
  const caption = element('p', 'Word-level pitch medians, centered on each voice. Gaps mean pitch could not be measured reliably.');
  const legend = element('div', '', 'pitch-legend');
  legend.append(element('span', 'Your recording', 'performance'), element('span', 'Original vocals', 'reference'));
  const svg = svgElement('svg', {viewBox: '0 0 600 190', role: 'img', 'aria-label': `Pitch movement from ${time(chart.start)} to ${time(chart.end)} in the original song, in semitones from each voice’s median.`});
  for (const [y, label] of [[26, `+${chart.limit}`], [90, '0'], [154, `-${chart.limit}`]]) {
    svg.append(svgElement('line', {x1: 36, x2: 584, y1: y, y2: y, class: 'pitch-grid'}));
    svg.append(svgElement('text', {x: 28, y: y + 4, 'text-anchor': 'end'}, label));
  }
  for (const name of ['reference', 'performance']) {
    for (const segment of chart[name]) {
      if (segment.length > 1) svg.append(svgElement('polyline', {points: segment.map(point => `${point.x},${point.y}`).join(' '), class: name}));
      for (const point of segment) svg.append(svgElement('circle', {cx: point.x, cy: point.y, r: 2.6, class: name}));
    }
  }
  svg.append(svgElement('text', {x: 36, y: 180}, time(chart.start)));
  svg.append(svgElement('text', {x: 584, y: 180, 'text-anchor': 'end'}, time(chart.end)));
  section.append(heading, caption, legend, svg);
  return section;
}
function evidence(assessment, {job, pauseMix, players}) {
  const details = element('details', '', 'assessment-evidence');
  details.append(element('summary', 'Inspect transcripts & measurement details'));
  const content = element('div', '', 'evidence-content');
  content.append(element('p', 'Speech recognition can mishear either recording. Lyric agreement compares the recognized words; it is not a pronunciation or articulation grade.'));
  content.append(element('p', 'Listen to the raw excerpts to check the words yourself. These recordings keep their own timing and may have different lengths.'));
  for (const [kind, label, text] of [['performance', 'Your original recording', assessment.performance_text], ['reference', 'Original vocal (separated)', assessment.reference_text]]) {
    const block = element('section', '', 'transcript');
    const audio = element('audio');
    audio.controls = true;
    audio.preload = 'none';
    audio.src = `/jobs/${encodeURIComponent(job)}/practice-audio/${kind}.wav`;
    audio.setAttribute('aria-label', label);
    audio.addEventListener('play', () => {
      pauseMix();
      for (const other of players) if (other !== audio) other.pause();
    });
    players.push(audio);
    const audioError = element('p', 'This excerpt could not be loaded. Your mixed cover is still available.');
    audioError.hidden = true;
    audio.addEventListener('error', () => { audioError.hidden = false; });
    const transcript = element('p', text || 'No reliable transcript available.', 'transcript-text');
    block.append(element('h3', label), audio, audioError, transcript);
    content.append(block);
  }
  if (assessment.words?.length) {
    const words = element('details');
    words.append(element('summary', `Matched word timing (${assessment.words.length} anchors)`));
    words.append(element('p', 'Timing differences remove one constant start offset. Positive values mean your word started later; negative values mean earlier. Neither recording is stretched for this measurement.'));
    const wrap = element('div', '', 'word-table-wrap');
    const table = element('table');
    const head = element('tr');
    for (const label of ['Word', 'Original time', 'Your time', 'Timing difference']) head.append(element('th', label));
    const thead = element('thead');
    thead.append(head);
    const body = element('tbody');
    for (const word of assessment.words) {
      const row = element('tr');
      for (const value of [word.reference || word.reference_text || '', time(word.reference_start), time(word.performance_start), `${Math.round(word.delta_ms)} ms`]) row.append(element('td', value));
      body.append(row);
    }
    table.append(thead, body);
    wrap.append(table);
    words.append(wrap);
    content.append(words);
  }
  const notes = element('ul');
  for (const note of assessment.notes || []) notes.append(element('li', note));
  if (notes.childNodes.length) content.append(notes);
  content.append(element('p', `Method ${assessment.version || 'unknown'} · Measurements apply to this excerpt of your original recording, before timing correction. Vocal range here means the observed pitch spread, not your full physiological range.`));
  details.append(content);
  details.addEventListener('toggle', () => { if (!details.open) for (const audio of players) audio.pause(); });
  return details;
}

export function mountAssessment(root, {job, signal, changed = () => {}, pauseMix = () => {}}) {
  let timer;
  let closed = false;
  let busy = false;
  const players = [];
  const pause = () => { for (const audio of players) audio.pause(); };
  const endpoint = `/jobs/${encodeURIComponent(job)}/assessment`;
  const title = element('h2', 'Practice analysis');
  const intro = element('p', 'Measurements of your original recording, before the mix adjusts its timing.');
  const content = element('div', '', 'assessment-content');
  root.className = 'assessment';
  root.append(title, intro, content);
  async function request(method = 'GET') {
    const response = await fetch(endpoint, {method, signal});
    if (!response.ok) {
      let message = 'Analysis is unavailable. Your audio is still ready to play.';
      try { message = (await response.json()).detail || message; } catch {}
      throw new Error(message);
    }
    return response.json();
  }
  function action(text, run) {
    const button = element('button', text);
    button.addEventListener('click', run);
    return button;
  }
  function failure(error) {
    if (closed || signal.aborted) return;
    const message = element('p', error.message || 'Could not load analysis.');
    message.setAttribute('role', 'status');
    content.replaceChildren(message, action('Retry analysis', () => load(true)));
  }
  function render(assessment) {
    if (closed) return;
    content.replaceChildren();
    if (assessment.status !== 'ready') {
      const pending = ['queued', 'running'].includes(assessment.status);
      const message = assessment.status === 'failed' ? assessment.error || 'This take could not be analyzed.'
        : pending ? 'Analyzing your recording. You can keep listening.' : 'Compare your words, timing, pace and measurable pitch with the original vocals.';
      const status = element('p', message);
      status.setAttribute('role', 'status');
      content.append(status);
      if (pending) timer = setTimeout(() => load(), 2000);
      else content.append(action(assessment.status === 'failed' ? 'Retry analysis' : 'Analyze take', () => load(true)));
      return;
    }
    if (assessment.summary) content.append(element('p', assessment.summary));
    const metrics = element('dl', '', 'metrics');
    for (const metric of assessment.metrics || []) {
      const row = element('div', '', 'metric');
      const heading = element('div', '', 'metric-heading');
      heading.append(element('dt', metric.label), element('dd', metricText(metric), metric.available ? 'metric-value' : 'metric-unavailable'));
      row.append(heading);
      if (metric.available && Number.isFinite(metric.reference)) row.append(element('p', `Original: ${metricText({...metric, value: metric.reference})}`));
      row.append(element('p', metric.available ? metric.detail : metric.reason || 'Not enough reliable evidence in this excerpt.'));
      metrics.append(row);
    }
    content.append(metrics);
    const plot = pitchPlot(assessment.pitch);
    if (plot) content.append(plot);
    content.append(evidence(assessment, {job, pauseMix, players}));
  }
  async function load(start = false) {
    if (closed || busy) return;
    busy = true;
    clearTimeout(timer);
    try {
      if (start) {
        content.replaceChildren(element('p', 'Starting analysis…'));
        await request('POST');
      }
      render(await request());
      changed();
    } catch (error) { failure(error); }
    finally { busy = false; }
  }
  load();
  return {pause, close() {
    closed = true;
    clearTimeout(timer);
    pause();
  }};
}
