import { expect, test } from 'bun:test';
import { Autosave, restoredLevels } from '../cover_engine/studio/autosave.js';

const events = () => ({ pending() {}, saved() {}, error() {} });

test('edits during a save are serialized and the latest values are saved', async () => {
  let finish;
  const sent = [];
  let acknowledged = 0;
  const saver = new Autosave(async payload => {
    sent.push(payload);
    if (sent.length === 1) await new Promise(resolve => { finish = resolve; });
  }, {...events(), saved() { acknowledged++; }}, 'test-editor');
  saver.set(100, 50);
  const saving = saver.flush();
  saver.set(110, 40);
  saver.set(120, 30);
  finish();
  await saving;
  await Promise.resolve();
  expect(sent.map(p => [p.sequence, p.vocal_volume, p.backing_volume])).toEqual([[1,100,50],[3,120,30]]);
  expect(acknowledged).toBe(1);
  expect(saver.latest).toBeNull();
  saver.close();
});

test('failure retains the draft and retry saves it', async () => {
  let fail = true;
  let errors = 0;
  const saver = new Autosave(async () => { if (fail) throw new Error('offline'); },
    {...events(), error() { errors++; }}, 'test-editor');
  saver.set(137, 28);
  await saver.flush();
  expect(errors).toBe(1);
  expect(saver.latest.vocal_volume).toBe(137);
  fail = false;
  await saver.flush();
  expect(saver.latest).toBeNull();
  saver.close();
});

test('closing sends the latest pending draft with keepalive even with a request in flight', async () => {
  let finish;
  const sent = [];
  let acknowledged = 0;
  const saver = new Autosave(async (payload, keepalive) => {
    sent.push({payload, keepalive});
    if (!keepalive) await new Promise(resolve => { finish = resolve; });
  }, {...events(), saved() { acknowledged++; }}, 'test-editor');
  saver.set(100, 50);
  const saving = saver.flush();
  saver.set(140, 20);
  saver.close();
  finish();
  await saving;
  expect(sent).toHaveLength(2);
  expect(sent[1].keepalive).toBe(true);
  expect(sent[1].payload.sequence).toBe(2);
  expect(sent[1].payload.vocal_volume).toBe(140);
  expect(acknowledged).toBe(0);
});

test('shared saved levels win over old browser defaults, while unsaved edits recover', () => {
  const server = {vocal_volume:137, backing_volume:28, levels_saved:true};
  expect(restoredLevels(server, [100,100])).toEqual({values:[137,28], save:false});
  expect(restoredLevels(server, {pending:true, vocal_volume:140, backing_volume:0}))
    .toEqual({values:[140,0], save:true});
  expect(restoredLevels({...server, levels_saved:false}, [80,60])).toEqual({values:[80,60], save:true});
  expect(restoredLevels(server, {pending:true, vocal_volume:-1, backing_volume:0})).toEqual({values:[137,28],save:false});
});
