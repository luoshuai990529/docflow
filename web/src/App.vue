<script setup>
import { computed, nextTick, onMounted, onUnmounted, ref, watch } from 'vue';
import { state, update, configured, currentJob, activeJob, navigate, workflowStep } from './shared/state.js';
import { bootstrapApiSession } from './shared/api.js';
import { restoreDraft } from './shared/draft.js';
import { loadConfigs } from './features/configuration/configuration.js';
import { refreshJobs, selectJob, startPolling, stopPolling } from './features/jobs/jobs.js';
import ConfigPanel from './components/ConfigPanel.vue';
import FilePanel from './components/FilePanel.vue';
import HistoryDrawer from './components/HistoryDrawer.vue';
import RunPanel from './components/RunPanel.vue';
import ResultPanel from './components/ResultPanel.vue';

const historyOpen = ref(false), configPanel = ref(null), pageTitle = ref(null);
const job = computed(currentJob), active = computed(activeJob);
const title = computed(() => ({ loading: '文档填写', setup: '配置模型', settings: '配置模型', files: '上传文件', run: job.value?.status === 'running' ? '正在填写' : '处理记录', result: '填写结果' })[state.view]);
const currentStep = computed(() => workflowStep(state.view, job.value));
const steps = ['模型配置', '上传文件', '自动填写', '查看结果'];
function stepState(step) {
  const stopped = step === 3 && !!job.value && ['error', 'interrupted'].includes(job.value.status);
  const cancelled = step === 3 && job.value?.status === 'cancelled' && ['run', 'result'].includes(state.view);
  return { current: step === currentStep.value, stopped, cancelled, complete: step < currentStep.value && !stopped && !cancelled };
}
function stepLabel(step) {
  const value = stepState(step);
  return `${step}，${steps[step - 1]}${value.cancelled ? '，已取消' : value.stopped ? '，未完成' : value.complete ? '，已完成' : value.current ? '，当前步骤' : '，待进行'}`;
}
function openSettings(provider = null) {
  if (state.submitting || state.pendingAttempt) return;
  if (!['settings', 'setup'].includes(state.view)) update({ settingsReturn: { view: state.view, selected: state.selected } });
  historyOpen.value = false;
  navigate(configured() ? 'settings' : 'setup');
  nextTick(() => configPanel.value?.editConfig(provider ? state.decisionConfigId : state.configId, provider || 'deepseek'));
}
function newRun() {
  historyOpen.value = false;
  navigate(configured() ? 'files' : 'setup', null);
  if (!configured() && state.configsStatus === 'ready') nextTick(() => configPanel.value?.editConfig(state.configId));
}
function selectHistoryJob(id) { historyOpen.value = false; selectJob(id); }
async function retryJobs() { try { await refreshJobs(); } catch { /* Status notice remains visible. */ } }
watch(() => [state.view, state.viewEpoch], async ([view], previous) => {
  if (previous && view !== 'loading') { await nextTick(); pageTitle.value?.focus({ preventScroll: true }); }
});
onMounted(async () => {
  const initialEpoch = state.viewEpoch;
  const draft = await restoreDraft();
  try { await bootstrapApiSession(); }
  catch (error) { update({ connectionError: error.message + ' 正在重新连接。' }); }
  await Promise.allSettled([loadConfigs(), refreshJobs()]);
  if (state.viewEpoch === initialEpoch) {
    const running = activeJob();
    const restoredJob = state.jobs.find(item => item.id === draft?.selected);
    let view = configured() ? (running ? 'run' : 'files') : 'setup', selected = configured() && running ? running.id : null;
    if (state.pendingAttempt) { view = 'files'; selected = null; }
    else if (restoredJob && ['run', 'result'].includes(draft.view)) {
      view = restoredJob.status === 'running' ? 'run' : draft.view; selected = restoredJob.id;
    } else if (configured() && ['files', 'settings'].includes(draft?.view)) { view = draft.view; selected = draft.settingsReturn?.selected || null; }
    update({ view, selected });
  }
  startPolling();
});
onUnmounted(stopPolling);
</script>

<template>
  <a class="skip-link" href="#main">跳到工作区</a>
  <HistoryDrawer :open="historyOpen" @close="historyOpen = false" @new-run="newRun" @select-job="selectHistoryJob" @manage-settings="openSettings()"/>
  <div class="app"><main id="main" class="main" :inert="historyOpen"><header class="topbar"><div class="brand"><img src="/static/document-questionnaire.svg" width="44" height="44" alt=""><span>文档填写</span></div><div class="topbar-actions"><button v-if="active && !(state.view === 'run' && job?.status === 'running')" id="active-run" type="button" class="quiet active-link" @click="selectJob(active.id)">进行中的任务</button><button id="open-history" type="button" class="quiet" aria-label="打开任务历史" @click="historyOpen = true">历史记录</button></div></header>
    <div class="workspace"><nav v-if="state.view !== 'loading'" id="steps" class="steps" aria-label="填写步骤"><ol><li v-for="(label, index) in steps" :key="label" :data-step="index + 1" :class="stepState(index + 1)" :aria-current="stepState(index + 1).current ? 'step' : undefined" :aria-label="stepLabel(index + 1)"><span class="step-number" aria-hidden="true"><svg v-if="stepState(index + 1).complete" viewBox="0 0 24 24" class="ui-icon"><path d="M5 12l4 4L19 6"/></svg><template v-else>{{ index + 1 }}</template></span><span>{{ label }}</span></li></ol></nav>
      <div class="page-heading"><h1 id="page-title" ref="pageTitle" tabindex="-1">{{ title }}</h1><p v-if="state.view === 'loading'" id="page-description">正在读取配置…</p></div>
      <div v-if="state.notice" id="notice" class="notice" role="alert"><span id="notice-message">{{ state.notice }}</span><button id="dismiss-notice" class="icon-button" aria-label="关闭提示" @click="update({ notice: '' })"><svg class="ui-icon" viewBox="0 0 24 24" aria-hidden="true"><path d="M6 6l12 12M18 6L6 18"/></svg></button></div>
      <div v-if="state.connectionError" id="connection-notice" class="notice connection-notice" role="status"><span id="connection-message">{{ state.connectionError }}</span><button id="retry-jobs" class="text-button" @click="retryJobs">重试</button></div>
      <section v-if="state.view === 'loading'" id="loading" class="loading-panel" aria-live="polite"><span class="loading-dot" aria-hidden="true"></span><p>正在读取配置…</p></section>
      <ConfigPanel v-show="state.view === 'setup' || state.view === 'settings'" ref="configPanel"/>
      <FilePanel v-show="state.view === 'files'" @manage-settings="openSettings()" @manage-decision="openSettings('typesafe')"/>
      <RunPanel v-if="state.view === 'run' && job" :job="job"/>
      <ResultPanel v-if="state.view === 'result' && job" :job="job"/>
    </div>
  </main></div>
</template>
