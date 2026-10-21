// Per-tab draft: public navigation metadata in sessionStorage, selected files in IndexedDB.
// Never copy model profiles, form fields, API keys or the page's CSRF token.
import { state, update, subscribe } from './state.js';
import { fileSelectionError, fileKey } from '../features/files/files.js';

const SESSION_SUFFIX = '.autofill.draft.v1';
const DATABASE_SUFFIX = '-autofill-drafts';
const SESSION_KEY = `document-questionnaire${SESSION_SUFFIX}`;
const DATABASE_NAME = `document-questionnaire${DATABASE_SUFFIX}`;
const views = new Set(['setup', 'settings', 'files', 'run', 'result']);
const databases = new Map();
let draftId, storageKey = SESSION_KEY, databaseName = DATABASE_NAME;
let fileWrite = Promise.resolve(), previousFiles, previousMetadata = '', lastView = 'files';
let storageWarning = false;

export function normalizedSubmission(value) {
  if (!value || typeof value !== 'object') return null;
  const { request_id, config_id, questionnaire, sources, decision_config_id } = value;
  const answer_mode = value.answer_mode ?? 'grounded';
  if (![request_id, config_id, questionnaire].every(item => typeof item === 'string' && item.length > 0 && item.length <= 200)
      || !['grounded', 'autonomous'].includes(answer_mode)
      || !Array.isArray(sources) || sources.length > 20 || (answer_mode === 'grounded' ? !sources.length : sources.length !== 0)
      || !sources.every(item => typeof item === 'string' && item.length > 0 && item.length <= 200)) return null;
  if (decision_config_id !== undefined && (answer_mode !== 'autonomous' || typeof decision_config_id !== 'string' || !decision_config_id.length || decision_config_id.length > 200)) return null;
  return { request_id, config_id, questionnaire, sources: [...sources], answer_mode,
    ...(decision_config_id ? { decision_config_id } : {}) };
}
function warnStorage() {
  if (storageWarning) return;
  storageWarning = true;
  if (!state.notice) update({ notice: '浏览器暂时无法保存选择，刷新后可能需要重新选择文件。' });
}
function readMetadata() {
  try {
    let key = SESSION_KEY, raw = sessionStorage.getItem(key);
    // Find a draft saved under the previous product name without keeping that name in the app.
    if (raw === null && typeof sessionStorage.key === 'function') {
      for (let index = 0; index < sessionStorage.length; index++) {
        const candidate = sessionStorage.key(index);
        if (!candidate?.endsWith(SESSION_SUFFIX) || candidate === SESSION_KEY) continue;
        const prefix = candidate.slice(0, -SESSION_SUFFIX.length);
        if (!/^[a-z][a-z0-9_-]{0,63}$/i.test(prefix)) continue;
        raw = sessionStorage.getItem(candidate);
        if (raw !== null) { key = candidate; break; }
      }
    }
    const saved = JSON.parse(raw);
    if (saved?.version !== 1 || typeof saved.id !== 'string' || saved.id.length > 80) return null;
    return {
      legacyStorageKey: key === SESSION_KEY ? null : key,
      id: saved.id, configId: typeof saved.configId === 'string' ? saved.configId : null,
      answerMode: saved.answerMode === 'autonomous' ? 'autonomous' : 'grounded',
      decisionConfigId: typeof saved.decisionConfigId === 'string' ? saved.decisionConfigId : null,
      decisionChoiceSet: saved.decisionChoiceSet === true,
      view: views.has(saved.view) ? saved.view : 'files',
      selected: typeof saved.selected === 'string' ? saved.selected : null,
      settingsReturn: saved.settingsReturn && ['files', 'run', 'result'].includes(saved.settingsReturn.view)
        ? { view: saved.settingsReturn.view, selected: typeof saved.settingsReturn.selected === 'string' ? saved.settingsReturn.selected : null } : null,
      pendingAttempt: normalizedSubmission(saved.pendingAttempt),
      fileKeys: saved.fileKeys && Array.isArray(saved.fileKeys.source) && saved.fileKeys.source.length <= 20
        && saved.fileKeys.source.every(key => typeof key === 'string' && key.length <= 2000)
        && (saved.fileKeys.questionnaire === null || typeof saved.fileKeys.questionnaire === 'string') ? saved.fileKeys : null,
    };
  } catch { return null; }
}
export function saveDraftMetadata() {
  if (!draftId) return;
  if (views.has(state.view)) lastView = state.view;
  const value = JSON.stringify({ version: 1, id: draftId, configId: state.configId,
    answerMode: state.answerMode, decisionConfigId: state.decisionConfigId, decisionChoiceSet: state.decisionChoiceSet,
    view: lastView, selected: state.selected, settingsReturn: state.settingsReturn, pendingAttempt: normalizedSubmission(state.pendingAttempt),
    fileKeys: { source: (state.files.source || []).map(fileKey), questionnaire: state.files.questionnaire ? fileKey(state.files.questionnaire) : null } });
  if (value === previousMetadata) return;
  try { sessionStorage.setItem(storageKey, value); previousMetadata = value; }
  catch { warnStorage(); }
}
function openDatabase(name = databaseName) {
  if (!databases.has(name)) databases.set(name, new Promise((resolve, reject) => {
    const request = indexedDB.open(name, 1);
    const timeout = setTimeout(() => reject(new Error('DRAFT_STORAGE_TIMEOUT')), 3000);
    request.onupgradeneeded = () => request.result.createObjectStore('files');
    request.onsuccess = () => { clearTimeout(timeout); resolve(request.result); };
    request.onerror = () => { clearTimeout(timeout); reject(request.error); };
    request.onblocked = () => { clearTimeout(timeout); reject(new Error('DRAFT_STORAGE_BLOCKED')); };
  }).catch(error => { databases.delete(name); throw error; }));
  return databases.get(name);
}
async function fileTransaction(mode, operation, name = databaseName) {
  const db = await openDatabase(name);
  return new Promise((resolve, reject) => {
    const transaction = db.transaction('files', mode);
    const request = operation(transaction.objectStore('files'));
    transaction.oncomplete = () => resolve(request.result);
    transaction.onerror = () => reject(transaction.error);
    transaction.onabort = () => reject(transaction.error);
  });
}
const storedFile = file => file ? { blob: file, name: file.name, type: file.type, lastModified: file.lastModified } : null;
function restoredFile(item) {
  if (!item || !(item.blob instanceof Blob) || typeof item.name !== 'string') return null;
  return new File([item.blob], item.name, { type: item.type || '', lastModified: item.lastModified });
}
function saveFiles() {
  if (state.files === previousFiles) return;
  previousFiles = state.files;
  const value = { source: (state.files.source || []).map(storedFile), questionnaire: storedFile(state.files.questionnaire) };
  // Serialize writes so a slow earlier selection cannot resurrect a removed file.
  fileWrite = fileWrite.then(() => fileTransaction('readwrite', store => !value.source.length && !value.questionnaire ? store.delete(draftId) : store.put(value, draftId)))
    .catch(() => { warnStorage(); });
}
export async function flushDraft() {
  saveDraftMetadata();
  await fileWrite;
}
export async function restoreDraft() {
  const saved = readMetadata();
  storageKey = SESSION_KEY;
  databaseName = DATABASE_NAME;
  draftId = saved?.id || crypto.randomUUID();
  lastView = saved?.view || 'files';
  if (saved) {
    if (saved.legacyStorageKey) {
      const prefix = saved.legacyStorageKey.slice(0, -SESSION_SUFFIX.length);
      const legacyDatabaseName = `${prefix}${DATABASE_SUFFIX}`;
      try {
        const files = await fileTransaction('readonly', store => store.get(draftId), legacyDatabaseName);
        if (files) await fileTransaction('readwrite', store => store.put(files, draftId), DATABASE_NAME);
      } catch {
        // Keep using the old draft if the new browser store is unavailable.
        storageKey = saved.legacyStorageKey;
        databaseName = legacyDatabaseName;
      }
    }
    update({ configId: saved.pendingAttempt?.config_id || saved.configId,
      answerMode: saved.pendingAttempt?.answer_mode || saved.answerMode,
      decisionConfigId: saved.pendingAttempt ? saved.pendingAttempt.decision_config_id || null : saved.decisionConfigId,
      decisionChoiceSet: saved.pendingAttempt ? true : saved.decisionChoiceSet,
      selected: saved.selected, settingsReturn: saved.settingsReturn, pendingAttempt: saved.pendingAttempt });
    try {
      const files = await fileTransaction('readonly', store => store.get(draftId));
      if (files) {
        let sources = (Array.isArray(files.source) ? files.source : []).map(restoredFile).filter(Boolean);
        let questionnaire = restoredFile(files.questionnaire), missing = false;
        if (saved.fileKeys) {
          const available = new Map(sources.map(file => [fileKey(file), file]));
          sources = saved.fileKeys.source.map(key => available.get(key)).filter(Boolean);
          if (!questionnaire || fileKey(questionnaire) !== saved.fileKeys.questionnaire) questionnaire = null;
          missing = sources.length !== saved.fileKeys.source.length || (!!saved.fileKeys.questionnaire && !questionnaire);
        }
        const sourceError = fileSelectionError('source', sources), questionnaireError = fileSelectionError('questionnaire', questionnaire ? [questionnaire] : []);
        update({ files: { source: !sourceError && sources.length ? sources : null, questionnaire: questionnaireError ? null : questionnaire } });
        if (missing || sourceError || questionnaireError) update({ notice: '部分文件选择未能恢复，请重新选择缺失的文件。' });
      } else if (saved.fileKeys?.source.length || saved.fileKeys?.questionnaire) update({ notice: '文件选择未能恢复，请重新选择文件。' });
    } catch { warnStorage(); }
  }
  // Rewrite the restored selection so deleted/stale blobs are also removed from the cache.
  previousFiles = saved ? null : state.files;
  saveDraftMetadata();
  subscribe(() => { saveDraftMetadata(); saveFiles(); });
  saveFiles();
  return saved;
}
