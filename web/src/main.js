import { createApp } from 'vue';
import { pinia } from './shared/state.js';
import App from './App.vue';
import './styles.css';

createApp(App).use(pinia).mount('#app');
