import { createApp } from 'vue'
import ArcoVue from '@arco-design/web-vue'
import '@arco-design/web-vue/dist/arco.css'
import './styles/oa.css'
import App from './App.vue'

const canonicalUrl = import.meta.env.VITE_CANONICAL_URL
  || ''
if (canonicalUrl && window.location.origin !== new URL(canonicalUrl).origin) {
  window.location.replace(canonicalUrl)
} else {
  createApp(App).use(ArcoVue).mount('#app')
}
