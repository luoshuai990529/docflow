export function time(value) {
  return value ? new Date(value).toLocaleString('zh-CN', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' }) : '—';
}
export const stageNames = { checking: '识别问卷', reading: '读取资料', matching: '匹配答案', writing: '填写副本', verifying: '检查结果' };
export const statusNames = { cancelled: '已取消', pending: '等待', running: '进行中', success: '完成', review: '部分完成', failed: '失败', error: '失败', interrupted: '已中断', skipped: '未执行' };
export function jobLabel(job) { if (job.cancelling && job.status === 'running') return '正在取消'; return job.status === 'done' ? (job.summary?.title || (job.outcome === 'complete' ? '已完成' : '部分完成')) : statusNames[job.status] || '等待'; }
export function jobKind(job) { return job.status === 'done' ? (job.outcome === 'complete' ? 'success' : 'review') : job.status; }
export const modeLabel = job => job.answer_mode === 'autonomous' || job.summary?.answer_mode === 'autonomous' ? '模型自主作答' : '';
