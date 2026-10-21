<script setup>
import { computed, onMounted, onUnmounted } from 'vue';
import { state, update, languageProfiles, decisionProfiles, activeJob, readyFiles } from '../shared/state.js';
import { lockedFiles, selectAnswerMode } from '../features/files/files.js';
import { canSubmit, cancelUpload, submitJob } from '../features/jobs/jobs.js';
import FileUpload from './FileUpload.vue';

const emit = defineEmits(['manage-settings', 'manage-decision']);
const llms = computed(languageProfiles), jevs = computed(decisionProfiles);
const locked = computed(lockedFiles);
const settingsLocked = computed(() => state.submitting || !!state.pendingAttempt);
const submitHint = computed(() => state.submitting ? '' : state.pendingAttempt ? '请先确认上次提交' : state.jobsStatus !== 'ready' ? '正在连接服务…' : activeJob() ? '请等待当前任务完成' : !readyFiles() ? (state.answerMode === 'autonomous' ? '请选择问卷' : '请选择问卷和答案资料') : '');
const submitLabel = computed(() => state.submitting ? (state.upload ? '正在上传…' : '正在提交…') : state.pendingAttempt ? '确认提交状态' : '开始填写');
const preventDrop = event => event.preventDefault();
onMounted(() => { document.addEventListener('dragover', preventDrop); document.addEventListener('drop', preventDrop); });
onUnmounted(() => { document.removeEventListener('dragover', preventDrop); document.removeEventListener('drop', preventDrop); });
</script>

<template>
  <section id="files" aria-label="上传文件">
    <fieldset class="answer-modes" aria-describedby="autonomous-note"><legend>作答方式</legend><div class="mode-options"><label><input type="radio" name="answer-mode" value="grounded" :checked="state.answerMode === 'grounded'" :disabled="locked" @change="selectAnswerMode('grounded')"><span>依据资料填写</span></label><label><input type="radio" name="answer-mode" value="autonomous" :checked="state.answerMode === 'autonomous'" :disabled="locked" @change="selectAnswerMode('autonomous')"><span>模型自主作答</span></label></div></fieldset>
    <p v-if="state.answerMode === 'autonomous'" id="autonomous-note" class="mode-note">无需答案资料。模型自行拟定答案，必要假设记录在报告。</p>
    <div id="upload-grid" class="upload-grid" :class="{ autonomous: state.answerMode === 'autonomous' }"><FileUpload v-show="state.answerMode !== 'autonomous'" kind="source"/><FileUpload kind="questionnaire"/></div>
    <details class="support-details"><summary>支持范围与限制</summary><p>PDF（含扫描件）、DOC / DOCX、XLS / XLSX。最多 500 页、2,000 题。网页上传的答案资料最多 20 份、合计 200 MiB；支持上述格式及 CSV、TXT、Markdown、PNG / JPG。网页单文件最多 10 MiB。</p><p>需有可识别的表单、空白单元格或填写区域。<span v-if="state.answerMode !== 'autonomous'" id="grounded-limit">引用无法核对的答案不写入；答案冲突时留空。</span>位置不明时留空，并在结果中说明。PDF 放不下的答案留空。加密、宏、电子签名及部分复杂控件暂不支持。</p><p>旧 DOC / XLS 转为 DOCX / XLSX，转换丢失内容时停止处理。扫描件需文字清晰；超大图片、复杂版式或过多文字可能触发处理上限。任务结束即清理服务端上传副本；当前选择保存在本浏览器，刷新后可继续使用，移除或清空即可删除对应选择。</p></details>
    <div class="model-row"><label for="config-select">本次模型</label><div class="select-control"><select id="config-select" :value="state.configId || ''" :disabled="locked || state.configsStatus !== 'ready'" @change="update({ configId: $event.target.value })"><option v-for="profile in llms" :key="profile.id" :value="profile.id" :disabled="!profile.has_key">{{ profile.name }}{{ profile.has_key ? '' : ' · 待配置' }}</option></select><svg viewBox="0 0 24 24" class="ui-icon" aria-hidden="true"><path d="m7 10 5 5 5-5"/></svg></div></div>
    <div v-if="state.answerMode === 'autonomous'" id="decision-model-row" class="model-row decision-model-row"><label for="decision-config-select">选项决策</label><div class="select-control"><select id="decision-config-select" :value="state.decisionConfigId || ''" :disabled="locked || state.configsStatus !== 'ready'" @change="update({ decisionConfigId: $event.target.value || null, decisionChoiceSet: true })"><option value="">使用当前 LLM</option><option v-for="profile in jevs" :key="profile.id" :value="profile.id" :disabled="!profile.has_key">{{ profile.name }}{{ profile.has_key ? '' : ' · 待配置' }}</option></select><svg viewBox="0 0 24 24" class="ui-icon" aria-hidden="true"><path d="m7 10 5 5 5-5"/></svg></div><button id="manage-decision" type="button" class="text-button" :disabled="settingsLocked" @click="emit('manage-decision')">配置 Jev</button></div>
    <p class="processing-note">文档文本将发送至所选模型并按其规则计费，请确认资料已获授权。</p>
    <div v-if="state.upload" id="upload-status" class="upload-status"><p id="upload-message" role="status">上传文件 {{ state.upload.current }} / {{ state.upload.total }} · {{ state.upload.name }}</p><button id="cancel-upload" type="button" class="quiet" @click="cancelUpload">取消上传</button></div>
    <div class="submit-row"><button id="manage-main" type="button" class="quiet" :disabled="settingsLocked" @click="emit('manage-settings')">模型设置</button><div class="submit-next"><span id="submit-hint" class="hint" :class="{ attention: state.pendingAttempt }" aria-live="polite">{{ submitHint }}</span><button id="submit" type="button" class="primary" :disabled="!canSubmit()" @click="submitJob">{{ submitLabel }}</button></div></div>
  </section>
</template>
