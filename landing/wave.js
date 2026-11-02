// the landing page's waveform follows the pointer, as the app's player does when you scrub, a
// little behind it: each frame closes a share of the distance, so it eases in wherever it goes.
// it comes to rest in the gap after the bar under the pointer, and glides back when the pointer leaves.
const WIDTH = 444;
const PITCH = 8;
const BAR = 4;
const BARS = (WIDTH - BAR) / PITCH + 1;
// seconds for the playhead to close 63% of the distance: following the pointer, then going back to rest.
const FOLLOW = 0.14;
const RETURN = 0.24;

// the played fraction for a pointer at `fraction` of the strip: up to the gap after the bar under it.
export function snap(fraction) {
  const bar = Math.min(BARS - 1, Math.max(0, Math.floor(fraction * WIDTH / PITCH)));
  return Math.min(1, (bar * PITCH + BAR + (PITCH - BAR) / 2) / WIDTH);
}

// where the playhead is `seconds` later, chasing `target` with time constant `lag`: the same path at any frame rate.
export function chase(position, target, seconds, lag) {
  return target + (position - target) * Math.exp(-seconds / lag);
}

export const REST = snap(0.62);

export function follow(wave, {
  frame = (callback) => requestAnimationFrame(callback),
  reduced = () => matchMedia('(prefers-reduced-motion: reduce)').matches,
} = {}) {
  const head = wave.querySelector('.head');
  let position = REST;
  let target = REST;
  let lag = FOLLOW;
  let last = null;
  let scheduled = false;

  const draw = () => wave.style.setProperty('--at', position.toFixed(4));
  const schedule = () => {
    if (scheduled) return;
    scheduled = true;
    frame(tick);
  };
  const tick = (time) => {
    scheduled = false;
    // a frame after a long pause (a background tab) moves at most a tenth of a second's worth.
    position = chase(position, target, last === null ? 1 / 60 : Math.min(0.1, (time - last) / 1000), lag);
    last = time;
    // under half a pixel away, it lands on the gap.
    if (Math.abs(position - target) < 0.001) position = target;
    draw();
    if (position === target) last = null;
    else schedule();
  };
  const aim = (to, withLag) => {
    target = to;
    lag = withLag;
    if (reduced()) {
      position = target;
      draw();
    } else {
      schedule();
    }
  };
  const pointer = (event) => {
    const box = wave.getBoundingClientRect();
    return snap((event.clientX - box.left) / box.width);
  };

  wave.addEventListener('pointerenter', (event) => {
    // the first touch ends the opening sweep where it is, so the playhead never jumps.
    if (!wave.classList.contains('touched') && head) {
      const box = wave.getBoundingClientRect();
      const at = head.getBoundingClientRect();
      position = Math.min(1, Math.max(0, (at.left + at.width / 2 - box.left) / box.width));
      wave.classList.add('touched');
      draw();
    }
    aim(pointer(event), FOLLOW);
  });
  wave.addEventListener('pointermove', (event) => aim(pointer(event), FOLLOW));
  wave.addEventListener('pointerleave', () => aim(REST, RETURN));
}

if (typeof document !== 'undefined') {
  const wave = document.querySelector('.wave');
  if (wave) follow(wave);
}
