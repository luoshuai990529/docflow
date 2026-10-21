import { createPinia, defineStore, setActivePinia } from 'pinia';

// The store contains public metadata and selected local File objects. Keys stay in the form.
export const pinia = createPinia();
setActivePinia(pinia);
export const useWorkflowStore = defineStore('workflow', {
  state: () => ({
    view: 'loading', viewEpoch: 0, configs: [], configsStatus: 'loading', configError: '',
    configId: null, jobs: [], jobsStatus: 'loading', selected: null,
    answerMode: 'grounded', decisionConfigId: null, decisionChoiceSet: false,
    files: { source: null, questionnaire: null }, submitting: false,
    pendingAttempt: null, notice: '', connectionError: '', settingsReturn: null, upload: null,
  }),
});
export const state = useWorkflowStore(pinia).$state;
const listeners = new Set();
export function subscribe(listener) { listeners.add(listener); return () => listeners.delete(listener); }
export function update(values = {}) {
  // Preserve the former top-level assignment contract. Pinia's object patch
  // recursively merges `files`, hiding reference changes from draft persistence.
  useWorkflowStore(pinia).$patch(current => { Object.assign(current, values); });
  for (const listener of listeners) listener(state);
}
export const profileProtocol = profile => profile.protocol || 'openai_chat';
export const languageProfiles = () => state.configs.filter(profile => profileProtocol(profile) === 'openai_chat');
export const decisionProfiles = () => state.configs.filter(profile => profileProtocol(profile) === 'typesafe_systemone');
export function configured() { return state.configsStatus === 'ready' && languageProfiles().some(profile => profile.has_key === true); }
export function selectedConfig() { return languageProfiles().find(profile => profile.id === state.configId); }
export function selectedDecisionConfig() { return decisionProfiles().find(profile => profile.id === state.decisionConfigId && profile.has_key); }
export function readyFiles() { return !!state.files.questionnaire && (state.answerMode === 'autonomous' || !!state.files.source?.length); }
export function currentJob() { return state.jobs.find(job => job.id === state.selected); }
export function activeJob() { return state.jobs.find(job => job.status === 'running'); }
export function navigate(view, selected = state.selected) { update({ view, selected, viewEpoch: state.viewEpoch + 1, notice: '' }); }
export function workflowStep(view = state.view, job = currentJob()) {
  if (['run', 'result'].includes(view) && job?.status !== 'running') return 4;
  return { setup: 1, settings: 1, files: 2, run: 3, result: 4 }[view];
}
export function returnFromSettings() {
  const context = state.settingsReturn;
  const job = state.jobs.find(item => item.id === context?.selected);
  update({ settingsReturn: null });
  navigate(context?.view === 'run' && job?.status !== 'running' ? 'result' : context?.view || 'files', job?.id || null);
}
