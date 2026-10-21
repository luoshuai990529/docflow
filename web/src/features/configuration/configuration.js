import { api } from '../../shared/api.js';
import { state, update, configured, navigate, languageProfiles, profileProtocol } from '../../shared/state.js';

export const endpoints = {
  deepseek: 'https://api.deepseek.com',
  typesafe: 'https://api.typesafe.ai/v1',
};
export const jevProvider = provider => provider === 'typesafe';

export async function loadConfigs(preferred) {
  update({ configsStatus: 'loading', configError: '' });
  try {
    const { configs } = await api('/configs');
    const llms = configs.filter(profile => profileProtocol(profile) === 'openai_chat');
    const preferredId = llms.some(profile => profile.id === preferred) ? preferred : state.configId;
    const selected = llms.find(profile => profile.id === preferredId && profile.has_key)
      || llms.find(profile => profile.has_key) || llms.find(profile => profile.id === preferredId) || llms[0];
    const jevs = configs.filter(profile => profileProtocol(profile) === 'typesafe_systemone' && profile.has_key);
    const decision = jevs.find(profile => profile.id === preferred)
      || jevs.find(profile => profile.id === state.decisionConfigId)
      || (!state.decisionChoiceSet && jevs.length === 1 ? jevs[0] : null);
    update({ configs, configId: selected?.id || null, decisionConfigId: decision?.id || null,
      decisionChoiceSet: state.decisionChoiceSet || !!decision, configsStatus: 'ready' });
    if (!configured() && state.view === 'files') navigate('setup');
    return configs;
  } catch (error) {
    update({ configsStatus: 'error', configError: error.message });
    throw error;
  }
}

export function modelStateText() {
  const count = languageProfiles().filter(profile => profile.has_key).length;
  return state.configsStatus === 'loading' ? '正在读取配置'
    : state.configsStatus === 'error' ? '配置读取失败'
      : count ? `已配置 ${count} 个模型` : '需要配置模型';
}
