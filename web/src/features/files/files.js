import { MAX_WEB_UPLOAD_BYTES } from '../../shared/api.js';
import { state, update, configured } from '../../shared/state.js';

export const asFiles = value => Array.isArray(value) ? value : value ? [value] : [];
export const fileKey = file => JSON.stringify([file.name, file.size, file.lastModified]);
export const sizeLabel = size => size >= 1024 * 1024 ? `${(size / (1024 * 1024)).toFixed(1)} MiB` : `${(size / 1024).toFixed(1)} KiB`;
export const lockedFiles = () => state.submitting || !!state.pendingAttempt || !configured();

export function selectAnswerMode(mode) {
  if (lockedFiles() || !['grounded', 'autonomous'].includes(mode)) return;
  update({ answerMode: mode, notice: '' });
}

export function fileSelectionError(id, files) {
  if (id === 'source' && (files.length > 20 || files.reduce((n, f) => n + f.size, 0) > 200 * 1024 * 1024)) return '最多 20 份资料，合计 200 MiB。';
  if (id === 'questionnaire' && files.length > 1) return '请选择一份问卷。';
  for (const file of files) {
    const extension = file.name.split('.').pop().toLowerCase();
    const allowed = id === 'source' ? ['pdf', 'doc', 'docx', 'xls', 'xlsx', 'csv', 'txt', 'md', 'markdown', 'png', 'jpg', 'jpeg'] : ['pdf', 'doc', 'docx', 'xls', 'xlsx'];
    if (!allowed.includes(extension)) return '文件格式不支持，请选择标注的文档格式。';
    if (!file.size || file.size > MAX_WEB_UPLOAD_BYTES) return '文件为空或超过 10 MiB，请重新选择。';
  }
  return '';
}
export function chooseFile(id, file) {
  if (lockedFiles()) return;
  const incoming = asFiles(file);
  // Cancelling a picker or dropping non-file data preserves the current selection.
  if (file !== null && !incoming.length) return;
  const files = id === 'source' && file !== null
    ? [...new Map([...asFiles(state.files.source), ...incoming].map(item => [fileKey(item), item])).values()]
    : incoming;
  const error = fileSelectionError(id, files);
  if (error) { update({ notice: error }); return; }
  update({ files: { ...state.files, [id]: files.length ? (id === 'source' ? files : files[0]) : null }, notice: '' });
}
export function removeSource(index) {
  if (lockedFiles()) return;
  const files = asFiles(state.files.source).filter((_, i) => i !== index);
  update({ files: { ...state.files, source: files.length ? files : null }, notice: '' });
}
