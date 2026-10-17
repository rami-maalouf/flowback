import type { Answer, Result, Sources } from './app.contract.d.ts';

// app.json’s app.id: change the two together.
export const appId = 'com.example.exact-cover-d9cc69';
export const grants = '';

const sources: Sources = {
  greeting: ([name]): Result<'greeting'> => ({ text: `Hello from ${name}.` }),
};
export const answer: Answer = (source, args, store, storage, native) =>
  sources[source](args, store, storage, native);
