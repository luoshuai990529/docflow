<script setup>
import { computed, nextTick, reactive, ref, watch } from 'vue';
import { api } from '../shared/api.js';
import { state, configured, navigate, profileProtocol, returnFromSettings, update } from '../shared/state.js';
import { endpoints, jevProvider, loadConfigs } from '../features/configuration/configuration.js';

const editing = ref(null);
const editorReady = ref(false);
const busy = ref(false);
const replacingKey = ref(false);
const errorMessage = ref('');
const nameInput = ref(null);
const keyInput = ref(null);
const changeKeyButton = ref(null);
const form = reactive({ name: '', provider: 'deepseek', model: 'deepseek-v4-pro', baseUrl: endpoints.deepseek, outputMode: 'json_object', apiKey: '' });
const profile = computed(() => state.configs.find(item => item.id === editing.value));
const jev = computed(() => jevProvider(form.provider));
const addressChanged = computed(() => !!profile.value?.has_key && form.baseUrl.trim() !== profile.value.base_url);
const showKeyEntry = computed(() => !profile.value?.has_key || replacingKey.value || addressChanged.value);
const isSetup = computed(() => state.view === 'setup');

function officialBaseUrl(provider, savedUrl) {
  const canonical = endpoints[provider];
  if (typeof savedUrl !== 'string') return canonical;
  const normalized = savedUrl.replace(/\/+$/, '');
  return normalized === canonical || (provider === 'deepseek' && normalized === `${canonical}/v1`)
    ? savedUrl : canonical;
}

function editConfig(id, provider = 'deepseek') {
  if (busy.value) return;
  const selected = state.configs.find(item => item.id === id);
  editing.value = id; editorReady.value = true; errorMessage.value = '';
  form.name = selected?.name || '';
  form.provider = selected?.provider || provider;
  form.model = selected?.model || (jevProvider(form.provider) ? 'jev-latest' : 'deepseek-v4-pro');
  form.baseUrl = officialBaseUrl(form.provider, selected?.base_url);
  form.outputMode = selected?.output_mode || 'json_object';
  form.apiKey = ''; replacingKey.value = false;
}
function newConfig() { editConfig(null); nextTick(() => nameInput.value?.focus()); }
function providerChanged() {
  form.baseUrl = endpoints[form.provider];
  form.model = jev.value ? 'jev-latest' : 'deepseek-v4-pro';
  form.apiKey = '';
}
function changeKey() { replacingKey.value = true; nextTick(() => keyInput.value?.focus()); }
function cancelKeyChange() { replacingKey.value = false; form.apiKey = ''; nextTick(() => changeKeyButton.value?.focus()); }
function back() { form.apiKey = ''; returnFromSettings(); }
async function retryConfigs() {
  try {
    await loadConfigs(); editConfig(state.configId);
    if (configured() && state.view === 'setup') navigate('files', null);
  } catch { /* Keep the inline error actionable. */ }
}
async function saveConfig() {
  if (busy.value) return;
  const epoch = state.viewEpoch;
  const body = {
    name: form.name.trim(), provider: form.provider, model: form.model.trim(), base_url: officialBaseUrl(form.provider, form.baseUrl),
    output_mode: jev.value ? 'json_object' : form.outputMode,
    protocol: jev.value ? 'typesafe_systemone' : 'openai_chat', api_key: form.apiKey,
  };
  form.apiKey = ''; errorMessage.value = ''; busy.value = true;
  let savedId;
  try {
    const path = editing.value ? '/configs/' + encodeURIComponent(editing.value) : '/configs';
    const promise = api(path, editing.value ? 'PUT' : 'POST', body);
    body.api_key = '';
    const result = await promise; savedId = result.config.id;
    await loadConfigs(savedId);
    if (epoch === state.viewEpoch) {
      if (configured() && state.configs.find(item => item.id === savedId)?.has_key) returnFromSettings();
      else if (!configured()) { navigate('setup'); update({ notice: 'Jev 用于选项决策，还需配置一个 LLM 来识别题目和填写文本。' }); }
    }
  } catch (error) { errorMessage.value = error.message; }
  finally {
    body.api_key = ''; busy.value = false;
    if (savedId && state.configsStatus === 'ready') editConfig(savedId);
  }
}
async function deleteConfig() {
  if (!editing.value || busy.value) return;
  if (!window.confirm('删除此模型配置及保存的 Key？历史任务和结果会保留。')) return;
  const id = editing.value, epoch = state.viewEpoch;
  busy.value = true; errorMessage.value = '';
  try {
    await api('/configs/' + encodeURIComponent(id), 'DELETE'); await loadConfigs();
    if (!configured() && epoch === state.viewEpoch) navigate('setup');
    busy.value = false; editConfig(state.configId);
  } catch (error) { busy.value = false; errorMessage.value = error.message; }
}
watch(() => [state.view, state.configsStatus], ([view, status], previous) => {
  if (previous && ['setup', 'settings'].includes(previous[0]) && !['setup', 'settings'].includes(view)) form.apiKey = '';
  if (['setup', 'settings'].includes(view) && status === 'ready' && (!editorReady.value || (previous && !['setup', 'settings'].includes(previous[0])))) editConfig(state.configId);
}, { immediate: true });
defineExpose({ editConfig });
</script>

<template>
  <section id="configuration" aria-label="模型配置">
    <div v-if="state.configsStatus === 'error'" id="config-fetch-error" class="empty-state">
      <h2>暂时无法读取模型配置</h2><p id="config-fetch-message">{{ state.configError }}</p>
      <button id="retry-configs" class="primary" @click="retryConfigs">重新读取</button>
    </div>
    <div v-if="state.configsStatus === 'ready'" id="config-body">
      <div id="config-list" class="config-list" aria-label="已保存的配置">
        <button v-for="item in state.configs" :key="item.id" type="button" class="config-tab" :aria-pressed="editing === item.id" :disabled="busy" @click="editConfig(item.id)">
          <span>{{ item.name }}</span><span class="config-tab-state">{{ profileProtocol(item) === 'typesafe_systemone' ? 'Jev · ' : '' }}{{ item.has_key ? '已配置' : '待配置' }}</span>
        </button>
        <button type="button" class="config-tab config-add" :aria-pressed="editing === null" :disabled="busy" @click="newConfig">+ 新建</button>
      </div>
      <form id="config-form" class="config-form" @submit.prevent="saveConfig">
        <div v-if="errorMessage" id="config-error" class="notice" role="alert">{{ errorMessage }}</div>
        <fieldset :disabled="busy" class="form-fieldset">
          <div class="field"><label for="config-name">配置名称</label><input id="config-name" ref="nameInput" v-model="form.name" maxlength="80" required placeholder="例如：日常填写"></div>
          <div class="form-grid">
            <div class="field"><label for="provider">服务商</label><div class="select-control"><select id="provider" v-model="form.provider" @change="providerChanged"><option value="deepseek">DeepSeek 官方</option><option value="typesafe">Jev 官方 · 选项决策</option></select><svg viewBox="0 0 24 24" class="ui-icon" aria-hidden="true"><path d="m7 10 5 5 5-5"/></svg></div></div>
            <div class="field"><label for="model">模型名称</label><input id="model" v-model="form.model" required maxlength="200" spellcheck="false"></div>
          </div>
          <div class="field"><label for="base_url">API 基础地址</label><input id="base_url" v-model="form.baseUrl" type="url" readonly></div>
          <div class="field"><label id="key-label" :for="showKeyEntry ? 'key' : 'change-key'">API Key</label>
            <div v-if="!showKeyEntry" id="key-saved" class="key-saved"><span>已设置</span><button id="change-key" ref="changeKeyButton" type="button" class="text-button" @click="changeKey">更换 Key</button></div>
            <div v-else id="key-entry"><input id="key" ref="keyInput" v-model="form.apiKey" type="password" autocomplete="new-password" maxlength="512" placeholder="粘贴 API Key" required aria-describedby="key-state"><button v-if="profile?.has_key && !addressChanged" id="cancel-key-change" type="button" class="text-button" @click="cancelKeyChange">取消更换</button></div>
            <p v-if="addressChanged" id="key-state" class="field-hint" role="status">服务地址已更改，请填写对应的 Key。</p>
          </div>
          <p v-if="jev" id="decision-profile-note" class="field-hint">可选，用于自主作答的选项决策；调用失败时自动使用本次 LLM。</p>
          <details v-if="!jev" id="llm-advanced" class="advanced"><summary>高级设置</summary>
            <div class="field"><label for="output_mode">结构化输出</label><div class="select-control"><select id="output_mode" v-model="form.outputMode" aria-describedby="output-mode-help"><option value="json_object">JSON（默认）</option><option value="json_schema">严格 Schema</option><option value="prompt_only">兼容模式</option></select><svg viewBox="0 0 24 24" class="ui-icon" aria-hidden="true"><path d="m7 10 5 5 5-5"/></svg></div><p id="output-mode-help" class="field-hint">模型支持时可选严格 Schema；DeepSeek 官方使用 JSON。兼容模式仅靠提示约束格式。</p></div>
            <p class="field-hint">Key 加密保存在本机。备份需包含数据和主密钥。</p>
          </details>
          <div class="form-footer"><div><button v-if="!isSetup" id="config-back" type="button" class="quiet" @click="back">取消修改</button><button v-if="!isSetup && editing" id="delete-config" type="button" class="quiet danger" @click="deleteConfig">删除配置</button></div><button id="save-config" type="submit" class="primary">{{ busy ? '正在保存…' : isSetup ? '保存并继续' : '保存并返回' }}</button></div>
        </fieldset>
      </form>
    </div>
  </section>
</template>
