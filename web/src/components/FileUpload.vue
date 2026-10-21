<script setup>
import { computed, ref } from 'vue';
import { state, update } from '../shared/state.js';
import { asFiles, chooseFile, lockedFiles, removeSource, sizeLabel } from '../features/files/files.js';

const props = defineProps({ kind: { type: String, required: true } });
const input = ref(null);
const dragging = ref(false);
const files = computed(() => asFiles(state.files[props.kind]));
const total = computed(() => files.value.reduce((sum, file) => sum + file.size, 0));
const label = computed(() => files.value.length ? (props.kind === 'source' ? `已选择 ${files.value.length} 份资料` : files.value[0].name) : (props.kind === 'source' ? '拖入资料，支持多选' : '拖入文件，或选择文件'));
const hint = computed(() => props.kind === 'source'
  ? files.value.length ? `${sizeLabel(total.value)} / 200 MiB · 最多 20 份` : '最多 20 份 · 每份 10 MiB · 合计 200 MiB'
  : files.value.length ? sizeLabel(total.value) : '每份最多 10 MiB');
const locked = computed(lockedFiles);
function inputChanged(event) {
  chooseFile(props.kind, props.kind === 'source' ? [...event.target.files] : event.target.files[0]);
  event.target.value = '';
}
function dragOver(event) { event.preventDefault(); if (!locked.value) dragging.value = true; }
function dragLeave(event) { if (!event.currentTarget.contains(event.relatedTarget)) dragging.value = false; }
function drop(event) {
  event.preventDefault(); dragging.value = false;
  if (locked.value || !event.dataTransfer.files.length) return;
  if (props.kind !== 'source' && event.dataTransfer.files.length !== 1) { update({ notice: '每个区域请选择一份文件。' }); return; }
  chooseFile(props.kind, props.kind === 'source' ? [...event.dataTransfer.files] : event.dataTransfer.files[0]);
}
</script>

<template>
  <div class="upload" :class="{ drag: dragging, 'has-file': files.length, locked }" :data-file="kind" @dragover="dragOver" @dragleave="dragLeave" @drop="drop">
    <div class="upload-heading"><h2>{{ kind === 'source' ? '答案资料' : '待填写问卷' }}</h2><span>{{ kind === 'source' ? '文档 / 表格 / 文本 / 图片' : 'PDF / Word / Excel' }}</span></div>
    <div class="file-icon" aria-hidden="true"><svg viewBox="0 0 32 36" fill="none"><path d="M7 2h12l7 7v24H7z"/><path d="M19 2v8h7M12 17h9M12 22h9M12 27h6"/></svg></div>
    <input :id="kind" ref="input" class="file-input" type="file" :accept="kind === 'source' ? '.pdf,.doc,.docx,.xls,.xlsx,.csv,.txt,.md,.markdown,.png,.jpg,.jpeg' : '.pdf,.doc,.docx,.xls,.xlsx'" :multiple="kind === 'source'" :aria-label="kind === 'source' ? '选择答案资料' : '选择待填写问卷'" tabindex="-1" :disabled="locked" @change="inputChanged">
    <div :id="kind + '-meta'" class="file-meta" :aria-live="kind === 'source' ? 'polite' : undefined"><strong>{{ label }}</strong><span class="hint">{{ hint }}</span></div>
    <ul v-if="kind === 'source' && files.length" id="source-list" class="source-list" aria-label="已选择的答案资料">
      <li v-for="(file, index) in files" :key="file.name + ':' + file.size + ':' + file.lastModified"><div class="source-file-info"><span class="source-file-name">{{ file.name }}</span><span class="hint">{{ sizeLabel(file.size) }}</span></div><button type="button" class="text-button" :disabled="locked" :aria-label="`移除 ${file.name}`" @click="removeSource(index)">移除</button></li>
    </ul>
    <div class="file-actions"><button type="button" :data-pick="kind" :disabled="locked" @click="input?.click()">{{ files.length ? kind === 'source' ? '添加资料' : '更换文件' : '选择文件' }}</button><button v-if="files.length" type="button" class="quiet" :data-remove="kind" :disabled="locked" @click="chooseFile(kind, null)">{{ kind === 'source' ? '清空' : '移除' }}</button></div>
  </div>
</template>
