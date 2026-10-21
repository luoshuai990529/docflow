import assert from 'node:assert/strict';
import { test } from 'node:test';
import { state, update } from '../src/shared/state.js';
import { flushDraft, restoreDraft } from '../src/shared/draft.js';

function fakeIndexedDB(records) {
  const db = {
    transaction() {
      const transaction = {
        objectStore() {
          return {
            get(key) {
              const request = { result: structuredClone(records.get(key)) };
              queueMicrotask(() => transaction.oncomplete?.());
              return request;
            },
            put(value, key) {
              records.set(key, structuredClone(value));
              queueMicrotask(() => transaction.oncomplete?.());
              return { result: key };
            },
            delete(key) {
              records.delete(key);
              queueMicrotask(() => transaction.oncomplete?.());
              return { result: undefined };
            },
          };
        },
      };
      return transaction;
    },
  };
  return {
    open() {
      const request = {};
      queueMicrotask(() => { request.result = db; request.onsuccess?.(); });
      return request;
    },
  };
}

test('Pinia updates replace selected files so the draft survives selection, removal and reload', async () => {
  const records = new Map();
  let metadata = null;
  globalThis.indexedDB = fakeIndexedDB(records);
  globalThis.sessionStorage = {
    getItem: () => metadata,
    setItem: (_key, value) => { metadata = value; },
  };
  const source = new File(['synthetic source'], 'source.md', { lastModified: 1 });
  const questionnaire = new File(['synthetic questionnaire'], 'form.docx', { lastModified: 2 });
  update({ files: { source: null, questionnaire: null }, view: 'files' });
  await restoreDraft();

  update({ settingsReturn: { view: 'run', selected: 'old-job' } });
  update({ settingsReturn: { view: 'files' } });
  assert.deepEqual(state.settingsReturn, { view: 'files' });
  update({ settingsReturn: null });

  const emptySelection = state.files;
  update({ files: { source: [source], questionnaire } });
  assert.notStrictEqual(state.files, emptySelection);
  await flushDraft();
  const draftId = JSON.parse(metadata).id;
  assert.equal(records.get(draftId)?.source?.[0]?.name, 'source.md');
  assert.equal(records.get(draftId)?.questionnaire?.name, 'form.docx');

  // A new page starts with empty in-memory state while IndexedDB and session metadata remain.
  state.files = { source: null, questionnaire: null };
  await restoreDraft();
  assert.equal(state.files.source?.[0]?.name, 'source.md');
  assert.equal(state.files.questionnaire?.name, 'form.docx');

  const selectedFiles = state.files;
  update({ files: { source: null, questionnaire: state.files.questionnaire } });
  assert.notStrictEqual(state.files, selectedFiles);
  await flushDraft();
  assert.deepEqual(records.get(draftId)?.source, []);
  assert.equal(records.get(draftId)?.questionnaire?.name, 'form.docx');

  state.files = { source: null, questionnaire: null };
  await restoreDraft();
  assert.equal(state.files.source, null);
  assert.equal(state.files.questionnaire?.name, 'form.docx');
});
