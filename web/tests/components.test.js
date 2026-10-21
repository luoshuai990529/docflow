import { afterEach, describe, expect, it, vi } from 'vitest';
import { createApp, nextTick } from 'vue';
import { pinia, state, update } from '../src/shared/state.js';
import FileUpload from '../src/components/FileUpload.vue';
import FilePanel from '../src/components/FilePanel.vue';
import ConfigPanel from '../src/components/ConfigPanel.vue';
import RunPanel from '../src/components/RunPanel.vue';
import ResultPanel from '../src/components/ResultPanel.vue';
import App from '../src/App.vue';

const mounted = [];
function mount(Component, props = {}) {
  const root = document.createElement('div');
  document.body.append(root);
  const app = createApp(Component, props).use(pinia);
  app.mount(root);
  mounted.push(() => { app.unmount(); root.remove(); });
  return root;
}
afterEach(() => {
  while (mounted.length) mounted.pop()();
  update({ view: 'loading', viewEpoch: 0, configs: [], configsStatus: 'loading', configError: '',
    configId: null, jobs: [], jobsStatus: 'loading', selected: null,
    answerMode: 'grounded', decisionConfigId: null, decisionChoiceSet: false,
    files: { source: null, questionnaire: null }, submitting: false,
    pendingAttempt: null, notice: '', connectionError: '', settingsReturn: null, upload: null });
});

describe('Vue workflow views', () => {
  it('shows local file selection and removes one source without dropping the others', async () => {
    const first = new File(['a'], 'first.md');
    const second = new File(['b'], 'second.md');
    update({ configsStatus: 'ready', configs: [{ id: 'llm', protocol: 'openai_chat', has_key: true }],
      configId: 'llm', files: { source: [first, second], questionnaire: null } });
    const root = mount(FileUpload, { kind: 'source' });
    expect(root.querySelectorAll('#source-list li')).toHaveLength(2);
    expect(root.textContent).toContain('已选择 2 份资料');
    root.querySelector('button[aria-label="移除 first.md"]').click();
    await nextTick();
    expect(root.querySelectorAll('#source-list li')).toHaveLength(1);
    expect(root.textContent).toContain('second.md');
    expect(root.textContent).not.toContain('first.md');
  });

  it('keeps autonomous mode questionnaire-only and exposes the same submission action', async () => {
    const questionnaire = new File(['q'], 'form.docx');
    update({ view: 'files', configsStatus: 'ready', configs: [{ id: 'llm', protocol: 'openai_chat', has_key: true }],
      configId: 'llm', jobsStatus: 'ready', jobs: [], files: { source: null, questionnaire } });
    const root = mount(FilePanel);
    expect(root.querySelector('#submit').disabled).toBe(true);
    root.querySelector('input[value="autonomous"]').click();
    await nextTick();
    expect(root.querySelector('#submit').disabled).toBe(false);
    expect(root.querySelector('[data-file="source"]').style.display).toBe('none');
    expect(root.querySelector('#decision-config-select')).toBeTruthy();
  });

  it('requires a new key when an existing profile uses a nonofficial service address', async () => {
    const profile = { id: 'llm', name: 'Synthetic', provider: 'deepseek', protocol: 'openai_chat',
      model: 'mock', base_url: 'https://api.deepseek.com/v1', has_key: true };
    update({ view: 'settings', configsStatus: 'ready', configs: [profile], configId: 'llm' });
    const root = mount(ConfigPanel);
    await nextTick();
    const address = root.querySelector('#base_url');
    expect(address.readOnly).toBe(true);
    expect(address.value).toBe('https://api.deepseek.com/v1');
    expect(root.querySelector('#key-saved')).toBeTruthy();
    expect(root.querySelector('#key')).toBeFalsy();
    update({ configs: [{ ...profile, base_url: 'https://example.invalid' }] });
    await nextTick();
    root.querySelector('.config-tab').click();
    await nextTick();
    expect(address.value).toBe('https://api.deepseek.com');
    expect(root.querySelector('#key-saved')).toBeFalsy();
    expect(root.querySelector('#key')).toBeTruthy();
    expect(root.querySelector('#key').required).toBe(true);
    expect(root.querySelector('#key-state').textContent).toContain('服务地址已更改');
  });

  it('submits a replaced key once and keeps it out of shared state', async () => {
    const originalFetch = globalThis.fetch;
    const requests = [];
    globalThis.fetch = async (url, options) => {
      requests.push({ url: String(url), method: options.method, body: options.body });
      const payload = String(url).endsWith('/session') ? { csrf_token: 'FAKE-SESSION-TOKEN' }
        : options.method === 'PUT' ? { config: { id: 'llm' } }
          : { configs: [{ id: 'llm', name: 'Synthetic', provider: 'deepseek', model: 'mock',
            protocol: 'openai_chat', base_url: 'https://api.deepseek.com/v1', has_key: true }] };
      return { ok: true, status: 200, json: async () => payload };
    };
    try {
      update({ view: 'settings', configsStatus: 'ready', configs: [{ id: 'llm', name: 'Synthetic', provider: 'deepseek',
        protocol: 'openai_chat', model: 'mock', base_url: 'https://api.deepseek.com/v1', has_key: true }], configId: 'llm' });
      const root = mount(ConfigPanel);
      await nextTick();
      root.querySelector('#change-key').click();
      await nextTick();
      const key = root.querySelector('#key');
      key.value = 'FAKE-REPLACEMENT-KEY';
      key.dispatchEvent(new Event('input', { bubbles: true }));
      root.querySelector('#config-form').dispatchEvent(new Event('submit', { bubbles: true, cancelable: true }));
      await vi.waitFor(() => expect(requests.some(item => item.method === 'PUT')).toBe(true));
      const submitted = JSON.parse(requests.find(item => item.method === 'PUT').body);
      expect(submitted.api_key).toBe('FAKE-REPLACEMENT-KEY');
      expect(submitted.base_url).toBe('https://api.deepseek.com/v1');
      expect(submitted).not.toHaveProperty('max_tokens');
      expect(JSON.stringify(state)).not.toContain('FAKE-REPLACEMENT-KEY');
    } finally { globalThis.fetch = originalFetch; }
  });

  it('renders node progress and verified result links from server data', async () => {
    const job = { id: 'job 1', version: 2, status: 'running', created_at: new Date().toISOString(),
      questionnaire_name: 'form.docx', config: { name: 'Synthetic', model: 'fake' },
      workflow_nodes: [{ node_id: 'answer', status: 'running', phase: 'deciding', counts: { completed: 2, total: 4 } }] };
    const run = mount(RunPanel, { job });
    expect(run.textContent).toContain('生成答案');
    expect(run.textContent).toContain('Jev 选项决策 · 已完成 2 / 4 题');
    expect(run.querySelector('#cancel-task')).toBeTruthy();
    const result = mount(ResultPanel, { job: { ...job, status: 'done', outcome: 'complete', files: ['filled.docx', 'report.json'],
      summary: { title: '填写完成', counts: { complete: 3, partial: 0, blank: 0 }, pending_total: 0 } } });
    await nextTick();
    expect(result.querySelector('a[download="filled.docx"]').getAttribute('href')).toBe('/api/v1/jobs/job%201/download/filled.docx');
    expect(result.querySelector('a[target="_blank"]').getAttribute('rel')).toBe('noopener');
  });

  it('boots the workflow against local fake API responses', async () => {
    const originalFetch = globalThis.fetch;
    const requests = [];
    globalThis.fetch = async url => {
      requests.push(String(url));
      const payload = String(url).endsWith('/session') ? { csrf_token: 'FAKE-SESSION-TOKEN' }
        : String(url).endsWith('/configs') ? { configs: [{ id: 'llm', name: 'Synthetic', model: 'mock', protocol: 'openai_chat', provider: 'deepseek', has_key: true }] }
          : { jobs: [] };
      return { ok: true, status: 200, json: async () => payload };
    };
    try {
      const root = mount(App);
      await vi.waitFor(() => expect(root.querySelector('#page-title')?.textContent).toBe('上传文件'));
      expect(root.querySelector('#submit').disabled).toBe(true);
      expect(requests).toEqual(expect.arrayContaining(['/api/v1/configs', '/api/v1/jobs']));
      expect(root.querySelector('#open-history')).toBeTruthy();
    } finally { globalThis.fetch = originalFetch; }
  });
});
