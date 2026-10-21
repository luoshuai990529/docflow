import assert from 'node:assert/strict';
import { test } from 'node:test';
import { createServer } from 'node:http';
import { state, update, configured, workflowStep, returnFromSettings } from '../src/shared/state.js';
import { APIError, api, MAX_WEB_UPLOAD_BYTES, uploadFile } from '../src/shared/api.js';
import { canSubmit, submitJob, workflowNodesForJob, workflowNodeProgressText } from '../src/features/jobs/jobs.js';
import { loadConfigs } from '../src/features/configuration/configuration.js';
import { fileSelectionError } from '../src/features/files/files.js';
import { normalizedSubmission, restoreDraft, flushDraft } from '../src/shared/draft.js';

const llm = { id: 'llm', name: 'Local fake LLM', has_key: true };
const jev = { id: 'jev', name: 'Local fake Jev', has_key: true, protocol: 'typesafe_systemone' };
const questionnaire = new File(['synthetic questionnaire'], 'questionnaire.docx', { lastModified: 1 });
const source = new File(['synthetic source'], 'source.md', { lastModified: 2 });

test('processing history does not rewind a completed task', () => {
  assert.equal(workflowStep('run', { status: 'done', outcome: 'partial' }), 4);
  assert.equal(workflowStep('run', { status: 'cancelled' }), 4);
  assert.equal(workflowStep('run', { status: 'running' }), 3);
  assert.equal(workflowStep('files', null), 2);
});

test('new jobs use seven workflow nodes and historical jobs fall back to five legacy stages', () => {
  const workflowNodes = ['preflight', 'extract', 'understand', 'answer', 'validate', 'write', 'verify']
    .map(node_id => ({ node_id, status: 'success' }));
  const legacyNodes = ['checking', 'reading', 'matching', 'writing', 'verifying']
    .map(stage => ({ stage, status: 'success' }));
  const current = { workflow_nodes: workflowNodes, nodes: legacyNodes };
  assert.strictEqual(workflowNodesForJob(current), workflowNodes);
  assert.strictEqual(workflowNodesForJob({ nodes: legacyNodes }), legacyNodes);
  assert.deepEqual(workflowNodesForJob({}), []);
});

test('active workflow nodes render their forwarded phase, counts and retry attempt', () => {
  const node = { node_id: 'answer', status: 'running', phase: 'deciding', counts: {
    completed: 2, total: 4, attempt: 2, batch: 1, batches: 2,
  } };
  assert.equal(workflowNodeProgressText(node, { answer_mode: 'autonomous' }),
    'Jev 选项决策 · 已完成 2 / 4 题 · 第 2 次');
  assert.equal(workflowNodeProgressText({ node_id: 'write', status: 'running', phase: 'answer_compaction',
    counts: { completed: 1, total: 2, attempt: 1 } }, { answer_mode: 'autonomous' }),
  '精简答案 · 已完成 1 / 2 题');
});

test('model settings return to the task, including one that finished meanwhile', () => {
  update({ settingsReturn: { view: 'run', selected: 'job' }, jobs: [{ id: 'job', status: 'done' }] });
  returnFromSettings();
  assert.equal(state.view, 'result'); assert.equal(state.selected, 'job');
  update({ settingsReturn: { view: 'files', selected: null } });
  returnFromSettings();
  assert.equal(state.view, 'files'); assert.equal(state.selected, null);
});

test('autonomous mode needs only a questionnaire, while Jev never replaces the main LLM', () => {
  update({ configsStatus: 'ready', configs: [llm, jev], configId: 'llm', jobsStatus: 'ready', jobs: [],
    answerMode: 'grounded', files: { questionnaire, source: null }, submitting: false, pendingAttempt: null });
  assert.equal(canSubmit(), false);
  update({ answerMode: 'autonomous' }); assert.equal(canSubmit(), true);
  update({ configs: [jev], configId: 'jev' });
  assert.equal(configured(), false); assert.equal(canSubmit(), false);
  update({ configs: [llm, jev], configId: 'llm', answerMode: 'grounded', files: { questionnaire, source: [source] } });
  assert.equal(canSubmit(), true);
});

test('web uploads enforce the API limit and API mutations bootstrap an in-memory CSRF token', async () => {
  assert.match(fileSelectionError('questionnaire', [{ name: 'large.pdf', size: MAX_WEB_UPLOAD_BYTES + 1 }]), /10 MiB/);
  const requests = [];
  const server = createServer(async (req, res) => {
    let body = ''; for await (const chunk of req) body += chunk;
    requests.push({ method: req.method, url: req.url, csrf: req.headers['x-csrf-token'], body });
    res.setHeader('Content-Type', 'application/json');
    if (req.url === '/api/v1/session') res.end(JSON.stringify({ csrf_token: 'FAKE-SESSION-TOKEN' }));
    else if (req.url.startsWith('/api/v1/configs')) res.end(JSON.stringify({ saved: true }));
    else if (req.url === '/api/v1/jobs' && req.method === 'POST') { res.statusCode = 202; res.end('not-json'); }
    else { res.statusCode = 503; res.end(JSON.stringify({ error: { code: 'FAKE_UNAVAILABLE', message: 'Local fake is unavailable.' } })); }
  });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const originalFetch = globalThis.fetch;
  globalThis.fetch = (url, options) => originalFetch(new URL(url, `http://127.0.0.1:${server.address().port}`), options);
  try {
    const profileBody = { name: 'Synthetic profile', api_key: 'FAKE-API-KEY' };
    const save = api('/configs', 'POST', profileBody);
    profileBody.api_key = '';
    await save;
    await api('/configs/profile-1', 'PUT', { name: 'Updated profile' });
    assert.equal(requests[0].url, '/api/v1/session');
    assert.equal(requests[1].url, '/api/v1/configs');
    assert.equal(requests[1].csrf, 'FAKE-SESSION-TOKEN');
    assert.equal(JSON.parse(requests[1].body).api_key, 'FAKE-API-KEY');
    assert.equal(profileBody.api_key, '');
    assert.equal(requests[2].method, 'PUT');
    assert.equal(requests[2].url, '/api/v1/configs/profile-1');
    await assert.rejects(api('/jobs', 'GET'), error => {
      assert.ok(error instanceof APIError);
      assert.equal(error.code, 'FAKE_UNAVAILABLE');
      assert.equal(error.uncertain, true);
      return true;
    });
    await assert.rejects(api('/jobs', 'POST', {}), error => {
      assert.ok(error instanceof APIError);
      assert.equal(error.uncertain, true);
      return true;
    });
  } finally {
    globalThis.fetch = originalFetch; server.closeAllConnections();
    await new Promise(resolve => server.close(resolve));
  }
});

test('an uncertain autonomous submission retries its exact mode and model choices without uploading sources', async () => {
  const posts = [], uploads = [];
  const server = createServer(async (req, res) => {
    let body = ''; for await (const chunk of req) body += chunk;
    res.setHeader('Content-Type', 'application/json');
    if (req.url === '/api/v1/session') res.end(JSON.stringify({ csrf_token: 'FAKE-SESSION-TOKEN' }));
    else if (req.url.startsWith('/api/v1/uploads?')) { uploads.push(req.url); res.end(JSON.stringify({ token: 'questionnaire-token' })); }
    else if (req.method === 'POST') {
      posts.push(JSON.parse(body));
      if (posts.length === 1) { res.statusCode = 503; res.end(JSON.stringify({ error: { message: 'FAKE uncertain response' } })); }
      else res.end(JSON.stringify({ id: 'same-job' }));
    } else if (req.url === '/api/v1/configs') res.end(JSON.stringify({ configs: [{ ...llm, id: 'first-llm' }, llm, jev] }));
    else res.end(JSON.stringify({ id: 'same-job', status: 'done' }));
  });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const originalFetch = globalThis.fetch;
  globalThis.window = {};
  globalThis.fetch = (url, options) => originalFetch(new URL(url, `http://127.0.0.1:${server.address().port}`), options);
  try {
    update({ configs: [llm, jev], configId: 'llm', decisionConfigId: 'jev', answerMode: 'autonomous',
      files: { questionnaire, source: [source] }, jobs: [], jobsStatus: 'ready', pendingAttempt: null });
    await loadConfigs('jev'); // Saving a decision model must not change the independently selected LLM.
    assert.equal(state.configId, 'llm'); assert.equal(state.decisionConfigId, 'jev');
    await submitJob();
    assert.equal(uploads.length, 1); assert.match(uploads[0], /^\/api\/v1\/uploads\?/); assert.match(uploads[0], /role=questionnaire/);
    assert.equal(posts[0].answer_mode, 'autonomous'); assert.equal(posts[0].decision_config_id, 'jev');
    assert.deepEqual(posts[0].sources, []);
    assert.deepEqual(normalizedSubmission(state.pendingAttempt), posts[0]);
    update({ answerMode: 'grounded', configId: 'changed-after-send', decisionConfigId: null });
    await submitJob();
    assert.deepEqual(posts[1], posts[0]); assert.equal(uploads.length, 1);
    assert.equal(state.pendingAttempt, null);
  } finally {
    globalThis.fetch = originalFetch; server.closeAllConnections();
    await new Promise(resolve => server.close(resolve));
  }
});

test('refresh restores the mode, decision preference and selected questionnaire; legacy drafts stay grounded', async () => {
  const saved = { version: 1, id: 'fake-draft', answerMode: 'autonomous', configId: 'llm', decisionConfigId: 'jev', decisionChoiceSet: true, view: 'files' };
  let metadata = JSON.stringify(saved);
  globalThis.sessionStorage = { getItem: () => metadata, setItem: (_key, value) => { metadata = value; } };
  const record = { source: [{ blob: source, name: source.name, lastModified: source.lastModified }],
    questionnaire: { blob: questionnaire, name: questionnaire.name, lastModified: questionnaire.lastModified } };
  globalThis.indexedDB = { open() {
    const request = {};
    queueMicrotask(() => { request.result = { transaction() {
      const transaction = { objectStore: () => ({ get: () => ({ result: record }), put: () => ({}), delete: () => ({}) }) };
      setTimeout(() => transaction.oncomplete(), 0); return transaction;
    } }; request.onsuccess(); }); return request;
  } };
  update({ answerMode: 'grounded', decisionConfigId: null, pendingAttempt: null, files: { questionnaire: null, source: null } });
  await restoreDraft();
  assert.equal(state.answerMode, 'autonomous'); assert.equal(state.decisionConfigId, 'jev');
  assert.equal(state.files.questionnaire.name, questionnaire.name); assert.equal(state.files.source[0].name, source.name);
  await flushDraft();
  assert.equal(JSON.parse(metadata).answerMode, 'autonomous');
  assert.doesNotMatch(metadata, /FAKE-SESSION-TOKEN|api_key|csrf_token|FAKE-API-KEY/);
  const legacy = normalizedSubmission({ request_id: 'old', config_id: 'llm', questionnaire: 'q', sources: ['s'] });
  assert.equal(legacy.answer_mode, 'grounded');
  assert.equal(normalizedSubmission({ ...legacy, sources: [] }), null);
  assert.equal(normalizedSubmission({ ...legacy, answer_mode: 'autonomous' }), null);
});

test('uploads can be cancelled and malformed server responses remain readable', async () => {
  const server = createServer((req, res) => {
    if (req.url.includes('bad.txt')) { req.resume(); res.end('not json'); }
    else req.resume(); // A deliberately slow local upload endpoint.
  });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const originalFetch = globalThis.fetch;
  globalThis.window = {};
  globalThis.fetch = (url, options) => originalFetch(new URL(url, `http://127.0.0.1:${server.address().port}`), options);
  try {
    const controller = new AbortController();
    const pending = uploadFile(new File(['sample'], 'slow.txt'), 'source', controller.signal);
    controller.abort();
    await assert.rejects(pending, /已取消上传/);
    await assert.rejects(uploadFile(new File(['sample'], 'bad.txt'), 'source'), /上传响应异常/);
  } finally {
    globalThis.fetch = originalFetch; server.closeAllConnections();
    await new Promise(resolve => server.close(resolve));
  }
});
