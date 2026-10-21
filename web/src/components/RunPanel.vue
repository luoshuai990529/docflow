<script setup>
import { computed, onMounted, onUnmounted, ref } from 'vue';
import { navigate } from '../shared/state.js';
import { cancelTask, elapsedText, nodeLabel, workflowNodeProgressText, workflowNodesForJob } from '../features/jobs/jobs.js';
import { jobKind, jobLabel, modeLabel, statusNames, time } from '../shared/ui.js';

const props = defineProps({ job: { type: Object, required: true } });
const nodes = computed(() => workflowNodesForJob(props.job));
const now = ref(Date.now());
const elapsed = computed(() => elapsedText(props.job, now.value));
let ticker;
onMounted(() => { ticker = setInterval(() => { now.value = Date.now(); }, 1000); });
onUnmounted(() => clearInterval(ticker));
function timing(node) {
  return node.node_id
    ? node.occurred_at ? `${time(node.occurred_at)} · ${statusNames[node.status] || node.status}` : statusNames[node.status] || node.status
    : node.started_at ? `${time(node.started_at)} → ${node.finished_at ? time(node.finished_at) : '进行中'}` : '未开始';
}
</script>

<template>
  <section id="run" aria-label="填写进度">
    <div class="run-summary"><div><p id="run-document" class="document-name">{{ job.questionnaire_name || '历史任务' }}</p><span id="run-model" class="hint">{{ job.config?.name || '历史配置' }}</span></div><span id="run-status" class="status" :class="jobKind(job)">{{ jobLabel(job) }}</span></div>
    <ol id="nodes" class="nodes" aria-live="polite"><li v-for="(node, index) in nodes" :key="node.node_id || node.stage || index" class="node" :class="node.status"><span class="node-number" aria-hidden="true"><svg v-if="node.status === 'success'" viewBox="0 0 24 24" class="ui-icon"><path d="M5 12l4 4L19 6"/></svg></span><div class="node-copy"><div class="node-heading"><h3>{{ nodeLabel(node, job) }}</h3><span class="status" :class="node.status">{{ statusNames[node.status] || node.status }}</span></div><p v-if="workflowNodeProgressText(node, job)" class="batch">{{ workflowNodeProgressText(node, job) }}</p><p v-if="node.error_code" class="diagnostic-error">错误代码：{{ node.error_code }}</p></div></li></ol>
    <p v-if="!nodes.length" id="legacy-note" class="hint">历史任务只保留最终报告，没有逐节点记录。</p>
    <p id="run-elapsed" class="hint">{{ elapsed }}</p>
    <div class="run-footer"><button v-if="job.status === 'running' && job.version === 2" id="cancel-task" type="button" class="quiet" :disabled="!!job.cancelling" @click="cancelTask">{{ job.cancelling ? '正在取消…' : '取消任务' }}</button><p id="run-note" class="hint">{{ job.cancelling ? '正在等待当前处理结束并清理副本…' : job.status === 'running' ? '完成后自动显示结果。' : job.status === 'done' ? '' : '已停止，不会自动重试。' }}</p><button v-if="job.status !== 'running'" id="to-result" type="button" class="primary" @click="navigate('result')">查看结果</button></div>
    <details id="run-details" :key="job.id" class="run-details"><summary>处理详情</summary><div id="run-detail-body">
      <dl class="detail-list"><div class="detail-row"><dt>模型</dt><dd>{{ job.config?.model || '—' }}</dd></div><div class="detail-row"><dt>开始</dt><dd>{{ time(job.created_at) }}</dd></div><div class="detail-row"><dt>结束</dt><dd>{{ job.finished_at ? time(job.finished_at) : '尚未结束' }}</dd></div><div v-if="modeLabel(job)" class="detail-row"><dt>作答方式</dt><dd>{{ modeLabel(job) }}</dd></div><div v-if="job.decision_config?.model" class="detail-row"><dt>选项决策</dt><dd>{{ job.decision_config.model }}</dd></div><div v-for="node in nodes.filter(item => (item.stage === 'matching' || item.node_id === 'answer') && (item.node_id ? item.counts?.batch : item.batch))" :key="'batch-' + (node.node_id || node.stage)" class="detail-row"><dt>匹配批次</dt><dd>{{ node.node_id ? node.counts?.batch : node.batch }} / {{ (node.node_id ? node.counts?.batches : node.batches) || (node.node_id ? node.counts?.batch : node.batch) }}</dd></div></dl>
      <template v-for="(node, index) in nodes" :key="'detail-' + index"><div class="node-timing"><span>{{ nodeLabel(node, job) }}</span><span>{{ timing(node) }}</span></div><p v-if="node.error_code" class="diagnostic-error">{{ nodeLabel(node, job) }} · {{ node.error_code }}</p></template>
      <p v-if="job.error" class="diagnostic-error">{{ job.error.code || '' }} · {{ job.error.message || '' }}</p>
    </div></details>
  </section>
</template>
