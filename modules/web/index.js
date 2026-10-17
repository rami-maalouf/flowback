const mixers = new Map();

function scrollParent(frame) {
  for (let node = frame.parentElement; node && node !== document.documentElement; node = node.parentElement) {
    if (/^(auto|scroll)$/.test(getComputedStyle(node).overflowY)) return node;
  }
}

export function element(handle) {
  const frame = handle.element;
  if (handle.hatch !== 'mixer-scroll' || frame.localName !== 'iframe' || mixers.has(frame)) return;
  const receive = event => {
    if (!frame.isConnected || event.source !== frame.contentWindow || typeof event.data !== 'string') return;
    const url = new URL(frame.src, document.baseURI);
    if (event.origin !== url.origin || url.searchParams.get('embedded') !== '1') return;
    const prefix = `flowback-live:${url.searchParams.get('job')}:${Number(url.searchParams.get('revision'))}:scroll:`;
    if (!event.data.startsWith(prefix)) return;
    const [mode, amount, ...extra] = event.data.slice(prefix.length).split(':');
    if (extra.length || !['0', '1', '2'].includes(mode) || !amount?.trim()) return;
    const delta = Number(amount);
    if (!Number.isFinite(delta)) return;
    const outer = scrollParent(frame);
    if (!outer) return;
    const pixels = delta * (mode === '1' ? 16 : mode === '2' ? outer.clientHeight : 1);
    if (Number.isFinite(pixels)) outer.scrollBy({top: pixels, behavior: 'instant'});
  };
  mixers.set(frame, receive);
  addEventListener('message', receive);
  addEventListener('pagehide', destroy);
}

export function elementEnded(handle) {
  const receive = mixers.get(handle.element);
  if (receive) removeEventListener('message', receive);
  mixers.delete(handle.element);
  if (!mixers.size) removeEventListener('pagehide', destroy);
}

export function destroy() {
  for (const receive of mixers.values()) removeEventListener('message', receive);
  mixers.clear();
  removeEventListener('pagehide', destroy);
}
