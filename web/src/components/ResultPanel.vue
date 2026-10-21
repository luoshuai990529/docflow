<script setup>
import { computed } from 'vue';
import { apiUrl } from '../shared/api.js';
import { configured, navigate, readyFiles } from '../shared/state.js';
import { jobKind, modeLabel } from '../shared/ui.js';

const props = defineProps({ job: { type: Object, required: true } });
const files = computed(() => props.job.files || []);
const documentFile = computed(() => files.value.find(name => /\.(pdf|docx?|xlsx?)$/i.test(name)));
const hasReport = computed(() => files.value.includes('report.json'));
const otherFiles = computed(() => files.value.filter(name => name !== documentFile.value && !(hasReport.value && /^report\.(json|md|html)$/.test(name))));
const title = computed(() => props.job.status === 'cancelled' ? '填写已取消'
  : props.job.summary?.title || (props.job.status === 'done' ? '填写已结束' : props.job.status === 'running' ? '正在填写' : '本次填写未完成'));
const message = computed(() => props.job.status === 'cancelled' ? '任务副本已清理，当前文件选择仍可用于重新填写。' : props.job.summary?.message || props.job.error?.message);
function downloadUrl(name) { return apiUrl(`/jobs/${encodeURIComponent(props.job.id)}/download/${encodeURIComponent(name)}`); }
function reportUrl(format) { return apiUrl(`/jobs/${encodeURIComponent(props.job.id)}/report.${format}`); }
function retry() { navigate(configured() ? 'files' : 'setup', null); }
</script>

<template>
  <section id="result-panel" aria-label="填写结果"><div id="result"><p class="document-name">{{ job.questionnaire_name || '历史任务' }}</p>
    <div class="result-outcome" :class="jobKind(job)"><h2>{{ title }}</h2><p v-if="message">{{ message }}</p><p v-if="modeLabel(job)" class="hint">{{ modeLabel(job) }}</p></div>
    <div v-if="job.summary" class="result-counts"><div v-for="[count, label] in [[job.summary.counts.complete, '完整填写'], [job.summary.counts.partial, '部分填写'], [job.summary.counts.blank, '未填写']].filter(([count, label]) => label !== '部分填写' || count)" :key="label" class="result-count"><strong>{{ count }}</strong><span>{{ label }}</span></div></div>
    <div class="result-actions"><a v-if="documentFile" :href="downloadUrl(documentFile)" :download="documentFile" class="button primary">{{ documentFile.endsWith('.pdf') ? '下载填写 PDF' : '下载填写文件' }}<svg viewBox="0 0 24 24" class="ui-icon" aria-hidden="true"><path d="M12 3v12M7 10l5 5 5-5M5 17v4h14v-4"/></svg></a><p v-else-if="!['running', 'cancelled'].includes(job.status)" class="hint">未生成通过校验的填写文件。</p><a v-if="hasReport" :href="reportUrl('html')" target="_blank" rel="noopener" class="text-link">查看报告</a><a v-for="name in otherFiles" :key="name" :href="downloadUrl(name)" :download="name" class="text-link">{{ { 'report.md': '下载报告', 'failure.json': '下载诊断记录' }[name] || name }}</a></div>
    <section v-if="job.summary?.pending_total" class="pending-section"><h3>未完成的题目</h3><ol class="pending-list"><li v-for="row in job.summary.pending.slice(0, 5)" :key="row.number"><p class="pending-question">第 {{ row.number }} 题 · {{ row.question }}</p><p class="pending-reason">{{ row.reason }}</p></li></ol><p v-if="job.summary.pending_total > 5" class="hint">其余 {{ job.summary.pending_total - 5 }} 题见报告。</p></section>
    <p v-if="job.summary && job.summary.layout_check !== 'passed' && job.summary.delivered" class="hint">{{ job.summary.layout_note }}</p>
    <details v-if="job.error?.code && job.status !== 'cancelled'" class="result-details"><summary>诊断信息</summary><p class="hint">{{ job.error.code }}</p></details>
    <div v-if="job.status !== 'running'" class="result-footer"><button type="button" class="quiet" @click="navigate('run', job.id)">查看处理记录</button><button type="button" :class="job.status === 'done' ? 'quiet' : 'primary'" @click="retry">{{ readyFiles() ? '重新填写' : '选择文件重新填写' }}</button></div>
  </div></section>
</template>
