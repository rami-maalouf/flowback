const extraction = new URLSearchParams(location.search).get('extraction') || '';
const valid = /^[a-f0-9]{32}$/.test(extraction);
const tracks = ['instrumental', 'vocals'].map(kind => ({
  kind, audio: document.getElementById(kind),
  status: document.getElementById(`${kind}-status`),
  retry: document.getElementById(`${kind}-retry`),
}));
let closed = false;
let lastHeight = 0;
const reportHeight = () => {
  if (closed || !valid) return;
  const height = Math.ceil(document.querySelector('main').getBoundingClientRect().height) + 8;
  if (height !== lastHeight) {
    lastHeight = height;
    parent.postMessage(`flowback-stems:${extraction}:height:${height}`, '*');
  }
};
const observer = new ResizeObserver(reportHeight);
observer.observe(document.querySelector('main'));

for (const track of tracks) {
  const {audio, status, retry} = track;
  const feedback = (text, failed = false) => {
    if (closed) return;
    status.textContent = text;
    retry.hidden = !failed;
    audio.closest('section').dataset.error = String(failed);
    reportHeight();
  };
  if (!valid) {
    feedback('Open this song from your Instrumentals library.');
    audio.removeAttribute('controls');
    continue;
  }
  audio.addEventListener('play', () => {
    for (const other of tracks) if (other !== track) other.audio.pause();
    feedback('Playing');
  });
  audio.addEventListener('pause', () => { if (!audio.error) feedback('Paused'); });
  audio.addEventListener('ended', () => feedback('Ready to play'));
  audio.addEventListener('loadedmetadata', () => feedback('Ready to play'));
  audio.addEventListener('canplay', () => feedback(audio.paused ? 'Ready to play' : 'Playing'));
  audio.addEventListener('waiting', () => feedback('Buffering audio…'));
  audio.addEventListener('error', () => feedback('Could not load this track. Check the audio engine and retry.', true));
  retry.addEventListener('click', () => {
    feedback('Loading audio…');
    audio.load();
  });
  audio.src = `/extractions/${extraction}/audio/${track.kind}.wav`;
}

addEventListener('pagehide', () => {
  closed = true;
  observer.disconnect();
  for (const {audio} of tracks) audio.pause();
}, {once: true});
