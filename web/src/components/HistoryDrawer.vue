<script setup>
import { computed, nextTick, onUnmounted, ref, watch } from 'vue';
import { state, configured } from '../shared/state.js';
import { modelStateText } from '../features/configuration/configuration.js';
import { jobKind, jobLabel, modeLabel, time } from '../shared/ui.js';

const props = defineProps({ open: Boolean });
const emit = defineEmits(['close', 'new-run', 'select-job', 'manage-settings']);
const closeButton = ref(null), sidebar = ref(null);
let returnFocus, shouldRestore = true;
const emptyText = computed(() => state.jobsStatus === 'loading' ? '正在读取任务…' : state.jobsStatus === 'error' ? '暂时无法读取历史' : '暂无填写记录');
function close(restore = true) { shouldRestore = restore; emit('close'); }
function select(id) { shouldRestore = false; emit('select-job', id); }
function newRun() { shouldRestore = false; emit('new-run'); }
function manage() { shouldRestore = false; emit('manage-settings'); }
function keydown(event) {
  if (!props.open) return;
  if (event.key === 'Escape') { event.preventDefault(); close(); return; }
  if (event.key !== 'Tab') return;
  const items = [...sidebar.value.querySelectorAll('button:not(:disabled), a[href]')].filter(node => node.offsetParent !== null);
  const first = items[0], last = items[items.length - 1];
  if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
  else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
}
watch(() => props.open, async value => {
  document.body.style.overflow = value ? 'hidden' : '';
  if (value) { returnFocus = document.activeElement; shouldRestore = true; document.addEventListener('keydown', keydown); await nextTick(); closeButton.value?.focus(); }
  else { document.removeEventListener('keydown', keydown); if (shouldRestore) returnFocus?.focus(); }
});
onUnmounted(() => { document.body.style.overflow = ''; document.removeEventListener('keydown', keydown); });
</script>

<template>
  <div v-show="open" id="history-backdrop" class="history-backdrop" @click="close()"></div>
  <aside id="sidebar" ref="sidebar" class="sidebar" :class="{ open }" aria-label="任务历史" :role="open ? 'dialog' : undefined" :aria-modal="open ? 'true' : undefined">
    <div class="drawer-heading"><h2>填写记录</h2><button id="close-history" ref="closeButton" class="icon-button" aria-label="关闭任务历史" @click="close()"><svg class="ui-icon" viewBox="0 0 24 24" aria-hidden="true"><path d="M6 6l12 12M18 6L6 18"/></svg></button></div>
    <button id="new-run" class="new-run" @click="newRun"><svg class="ui-icon" viewBox="0 0 24 24" aria-hidden="true"><path d="M12 5v14M5 12h14"/></svg> 新建填写</button>
    <div class="history-heading"><span>最近任务</span><span id="history-count">{{ state.jobs.length }}</span></div>
    <div id="history" class="history" aria-label="填写记录">
      <p v-if="!state.jobs.length" class="history-empty">{{ emptyText }}</p>
      <button v-for="job in state.jobs" :key="job.id" class="history-item" :class="{ selected: job.id === state.selected }" :data-job="job.id" :aria-current="job.id === state.selected ? 'true' : undefined" @click="select(job.id)">
        <span class="history-title">{{ job.questionnaire_name || '填写任务' }}</span>
        <span class="history-meta"><span class="history-status" :class="jobKind(job)">{{ jobLabel(job) }}</span><time>{{ time(job.created_at) }}</time></span>
        <span class="history-context">{{ [job.summary?.counts ? `${job.summary.counts.complete} / ${job.summary.counts.total} 完整${job.summary.counts.partial ? ` · ${job.summary.counts.partial} 部分` : ''}` : '', modeLabel(job), job.config?.model].filter(Boolean).join(' · ') }}</span>
      </button>
    </div>
    <div class="sidebar-footer"><button id="manage-side" class="settings-button" :disabled="state.submitting || !!state.pendingAttempt" @click="manage">模型设置 <svg class="ui-icon" viewBox="0 0 24 24" aria-hidden="true"><path d="M7 17L17 7M7 7h10v10"/></svg></button><p><span id="model-dot" class="model-dot" :class="{ ready: configured() }" aria-hidden="true"></span><span id="model-state">{{ modelStateText() }}</span></p></div>
  </aside>
</template>
