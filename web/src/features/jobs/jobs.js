import { api, uploadFile } from '../../shared/api.js';
import { state, update, configured, selectedConfig, selectedDecisionConfig, readyFiles, currentJob, activeJob, navigate } from '../../shared/state.js';
import { stageNames } from '../../shared/ui.js';
import { flushDraft, saveDraftMetadata } from '../../shared/draft.js';

let refreshSequence = 0, timer, uploadController, polling = false;
export function canSubmit() {
  if (state.submitting || state.jobsStatus !== 'ready') return false;
  if (state.pendingAttempt) return true;
  if (!configured()) return false;
  return readyFiles() && !!selectedConfig()?.has_key && !activeJob();
}
export function selectJob(id) {
  const job = state.jobs.find(item => item.id === id); if (!job) return;
  navigate(job.status === 'running' ? 'run' : 'result', id);
}
const workflowNodeNames = {
  preflight: '输入与任务校验', extract: '提取问卷和资料', understand: '识别题目和填写位置',
  answer: '生成答案', validate: '校验答案与填写约束', write: '写入输出副本', verify: '验证交付结果',
};
export function workflowNodesForJob(job) {
  return Array.isArray(job?.workflow_nodes) && job.workflow_nodes.length
    ? job.workflow_nodes : Array.isArray(job?.nodes) ? job.nodes : [];
}
export function nodeLabel(node, job) {
  if (node.node_id) return workflowNodeNames[node.node_id] || node.node_id;
  return job.answer_mode === 'autonomous'
    ? ({ reading: '整理问卷', matching: '生成答案' }[node.stage] || stageNames[node.stage] || node.stage)
    : stageNames[node.stage] || node.stage;
}
export function workflowNodeProgressText(node, job) {
  if (node.status !== 'running') return '';
  const phases = { questionnaire: '读取问卷', baseline: '准备原稿版式', sources: '读取资料', inventory: '识别题目', extracting: '提取答案', integrating: '整合多份资料', reviewing: '核对答案', revising: '修正答案', correcting: '纠正返回格式', answer_compaction: '精简答案', fallback: job.answer_mode === 'autonomous' ? '改用 LLM 作答' : '补查全部资料', autonomous: '模型自主作答', generating: '模型自主作答', deciding: 'Jev 选项决策', jev: 'Jev 选项决策' };
  const label = phases[node.phase] || '';
  const counts = { ...node, ...(node.counts || {}) };
  const unit = ['questionnaire','baseline','sources'].includes(node.phase) ? '份' : ['integrating', 'reviewing', 'revising', 'autonomous', 'generating', 'deciding', 'jev', 'answer_compaction'].includes(node.phase) ? '题' : '批';
  const count = Number.isInteger(counts.total) ? ` · 已完成 ${counts.completed || 0} / ${counts.total} ${unit}` : counts.batch ? ` · 第 ${counts.batch} / ${counts.batches} 批` : '';
  const attempt = counts.attempt;
  return label || count ? label + count + (attempt > 1 ? ` · 第 ${attempt} 次` : '') : '';
}
export function elapsedText(job, now = Date.now()) {
  const seconds = Math.max(0, Math.floor(((job.finished_at ? Date.parse(job.finished_at) : now) - Date.parse(job.created_at)) / 1000));
  return `用时 ${Math.floor(seconds / 60)} 分 ${seconds % 60} 秒`;
}
export async function refreshJobs() {
  const sequence = ++refreshSequence;
  try {
    const { jobs } = await api('/jobs'); if (sequence !== refreshSequence) return;
    const wasRunning = currentJob()?.status === 'running';
    const values = { jobs, jobsStatus: 'ready', connectionError: '' };
    if (wasRunning && state.view === 'run' && jobs.find(job => job.id === state.selected)?.status !== 'running') values.view = 'result';
    update(values);
  } catch (error) {
    if (sequence !== refreshSequence) return;
    update({ jobsStatus: 'error', connectionError: error.message + ' 正在重新连接。' });
    throw error;
  }
}
export function startPolling() {
  polling = true;
  clearTimeout(timer);
  timer = setTimeout(async () => { try { await refreshJobs(); } catch {} finally { if (polling) startPolling(); } }, 2000);
}
export function stopPolling() { polling = false; clearTimeout(timer); }
export function cancelUpload() { uploadController?.abort(); }
export async function cancelTask() {
  const job = currentJob(); if (!job) return;
  try {
    await api(`/jobs/${encodeURIComponent(job.id)}/cancel`, 'POST', {});
    update({ jobs: state.jobs.map(item => item.id === job.id ? { ...item, cancelling: true } : item), notice: '' });
  } catch (error) { update({ notice: error.message }); }
}
export async function submitJob() {
  if (!canSubmit()) return;
  const epoch = state.viewEpoch; update({ submitting: true, notice: '' });
  let attempt = state.pendingAttempt, sent = false;
  try {
    await flushDraft();
    if (!attempt) {
      // Capture mode and selected profiles once; uncertain POST retries reuse this exact body.
      const answerMode = state.answerMode, configId = state.configId, decisionId = answerMode === 'autonomous' ? selectedDecisionConfig()?.id : null;
      const tokens = [], files = [[state.files.questionnaire, 'questionnaire'], ...(answerMode === 'grounded' ? state.files.source : []).map(file => [file, 'source'])];
      uploadController = new AbortController();
      try {
        for (const [index, [file, role]] of files.entries()) {
          update({ upload: { current: index + 1, total: files.length, name: file.name } });
          tokens.push(await uploadFile(file, role, uploadController.signal));
        }
        if (uploadController.signal.aborted) throw new Error('已取消上传，文件选择已保留。');
        const [questionnaire, ...sources] = tokens;
        update({ upload: null });
        attempt = { request_id: crypto.randomUUID(), config_id: configId, questionnaire, sources, answer_mode: answerMode,
          ...(decisionId ? { decision_config_id: decisionId } : {}) };
      } catch (error) { await Promise.allSettled(tokens.map(t => api('/uploads/' + encodeURIComponent(t), 'DELETE'))); throw error; }
      update({ pendingAttempt: attempt });
    }
    // Persist the original idempotency body before POST; a reload never creates a new request automatically.
    saveDraftMetadata();
    sent = true;
    const response = await api('/jobs', 'POST', attempt);
    const job = await api('/jobs/' + encodeURIComponent(response.id));
    refreshSequence++;
    const values = { pendingAttempt: null, jobs: [job, ...state.jobs.filter(item => item.id !== job.id)], jobsStatus: 'ready', connectionError: '' };
    if (epoch === state.viewEpoch) { values.selected = job.id; values.view = job.status === 'running' ? 'run' : 'result'; }
    update(values);
  } catch (error) {
    const uncertain = sent && error.uncertain !== false;
    if (!uncertain && attempt) await Promise.allSettled([attempt.questionnaire, ...attempt.sources].map(t => api('/uploads/' + encodeURIComponent(t), 'DELETE')));
    if (!uncertain && !configured() && state.configsStatus === 'ready') navigate('setup', null);
    update({
      pendingAttempt: uncertain ? attempt : null,
      notice: error.message + (uncertain ? ' 点击“确认提交状态”会使用同一提交标识，不会新建重复任务。' : ''),
    });
  } finally { uploadController = null; update({ submitting: false, upload: null }); }
}
