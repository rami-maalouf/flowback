export class Autosave {
  constructor(send, events, clientId) {
    this.send = send;
    this.events = events;
    this.clientId = clientId;
    this.sequence = 0;
    this.latest = null;
    this.busy = false;
    this.closed = false;
  }
  set(vocal, backing) {
    this.latest = { vocal_volume: vocal, backing_volume: backing,
      client_id: this.clientId, sequence: ++this.sequence };
    this.events.pending(this.latest);
    clearTimeout(this.timer);
    this.timer = setTimeout(() => this.flush(), 200);
  }
  async flush() {
    clearTimeout(this.timer);
    if (this.busy || !this.latest || this.closed) return;
    const payload = this.latest;
    this.busy = true;
    try {
      await this.send(payload, false);
      if (this.latest.sequence === payload.sequence) {
        this.latest = null;
        if (!this.closed) this.events.saved();
      }
    } catch (error) {
      if (!this.closed) this.events.error(error);
    } finally {
      this.busy = false;
      if (this.latest && this.latest.sequence > payload.sequence && !this.closed) this.flush();
    }
  }
  close() {
    this.closed = true;
    clearTimeout(this.timer);
    if (this.latest) this.send(this.latest, true).catch(() => {});
  }
}

export function restoredLevels(server, cached) {
  if (cached?.pending && [cached.vocal_volume, cached.backing_volume].every(validVolume)) {
    return { values: [cached.vocal_volume, cached.backing_volume], save: true };
  }
  if (!server.levels_saved && Array.isArray(cached) && cached.length === 2 && cached.every(validVolume)) {
    return { values: cached, save: true };
  }
  return { values: [server.vocal_volume, server.backing_volume], save: false };
}
const validVolume = value => Number.isFinite(value) && value >= 0 && value <= 200;
