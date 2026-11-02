// the landing page's waveform follows the pointer, as the app's player does when you scrub:
// the played part ends at the bar under it, and glides back to rest when the pointer leaves.
const WIDTH = 444;
const PITCH = 8;
const BAR = 4;
const BARS = (WIDTH - BAR) / PITCH + 1;

// the played fraction for a pointer at `fraction` of the strip: up to the gap after the bar under it.
export function snap(fraction) {
  const bar = Math.min(BARS - 1, Math.max(0, Math.floor(fraction * WIDTH / PITCH)));
  return Math.min(1, (bar * PITCH + BAR + (PITCH - BAR) / 2) / WIDTH);
}

export function follow(wave) {
  const move = (event) => {
    const box = wave.getBoundingClientRect();
    wave.style.setProperty('--at', snap((event.clientX - box.left) / box.width).toFixed(4));
  };
  wave.addEventListener('pointerenter', (event) => {
    wave.classList.add('touched', 'scrubbing');
    move(event);
  });
  wave.addEventListener('pointermove', move);
  wave.addEventListener('pointerleave', () => {
    wave.classList.remove('scrubbing');
    wave.style.removeProperty('--at');
  });
}

if (typeof document !== 'undefined') {
  const wave = document.querySelector('.wave');
  if (wave) follow(wave);
}
