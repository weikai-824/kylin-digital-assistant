<script setup>
import { computed, onMounted, ref } from 'vue'
import { SSO_ENABLED, clearAuthTokens, getCurrentUser, listKnowledgeBases } from './api/kb'
import ChatView from './views/ChatView.vue'
import LoginView from './views/LoginView.vue'
import SessionSidebar from './components/SessionSidebar.vue'

/**
 * 纯对话页外壳 + 左侧会话栏。
 *  - 左侧：SessionSidebar（历史会话列表，默认收起，状态记 localStorage）
 *  - 右侧：ChatView 对话区（知识库自动选中 oa_ma）
 *  - 会话数据由 useSessions store 承载，侧栏与 ChatView 各自直连 store，
 *    选中/删除/新建都不需要经 App 转发事件
 * 身份：统一从 Gateway 转发的 /auth/me 获取；本地开发由后端返回 dev_local。
 */
const user = ref(null)
const authLoading = ref(true)
const authError = ref('')

const kbs = ref([])
const kbLoading = ref(true)
const kbError = ref('')
const selectedKb = ref(null)

// 会话栏开合：默认收起；用户手动展开后记住（存"open"即展开）
const SIDEBAR_KEY = 'kylin-console-sidebar'
const sidebarCollapsed = ref(localStorage.getItem(SIDEBAR_KEY) !== 'open')
function toggleSidebar() {
  sidebarCollapsed.value = !sidebarCollapsed.value
  localStorage.setItem(SIDEBAR_KEY, sidebarCollapsed.value ? 'collapsed' : 'open')
}

async function loadKbs() {
  kbLoading.value = true
  kbError.value = ''
  try {
    const list = await listKnowledgeBases()
    kbs.value = list
    // 默认选中 oa_ma（系统默认问答库）；不在列表中则选第一条
    const preferred = list.find((k) => k.kb_name === 'oa_ma') || list[0] || null
    selectedKb.value = preferred
  } catch (e) {
    kbError.value = (e && e.message) || '知识库列表加载失败'
    kbs.value = []
  } finally {
    kbLoading.value = false
  }
}

async function initializeApp() {
  authLoading.value = true
  authError.value = ''
  if (SSO_ENABLED && !localStorage.getItem('ACCESS_TOKEN')) {
    user.value = null
    authLoading.value = false
    return
  }
  try {
    user.value = await getCurrentUser()
    await loadKbs()
  } catch (e) {
    user.value = null
    authError.value = (e && e.message) || '统一登录状态获取失败'
  } finally {
    authLoading.value = false
  }
}

async function handleLoginSuccess() {
  await initializeApp()
}

function handleAuthExpired() {
  clearAuthTokens()
  user.value = null
  kbs.value = []
  selectedKb.value = null
  authError.value = '登录状态已失效，请重新登录'
}

// 知识库未就绪时的居中状态卡文案
const appState = computed(() => {
  if (authLoading.value) return { title: '正在验证登录状态…', desc: '' }
  if (authError.value) return { title: '登录状态获取失败', desc: authError.value }
  if (kbLoading.value) return { title: '正在加载知识库…', desc: '' }
  if (kbError.value) return { title: '知识库加载失败', desc: kbError.value }
  return { title: '暂无可用知识库', desc: '请先在后台上传文档后再试。' }
})

onMounted(() => {
  window.addEventListener('kylin-auth-expired', handleAuthExpired)
  initializeApp()
})
</script>

<template>
  <div v-if="authLoading" class="oa-welcome">
    <div class="oa-welcome__card">
      <div class="oa-welcome__logo">AI</div>
      <div class="oa-welcome__title">正在验证登录状态...</div>
    </div>
  </div>
  <LoginView v-else-if="!user" @success="handleLoginSuccess" />
  <div v-else class="oa-app">
    <SessionSidebar
      :kb-name="selectedKb ? selectedKb.kb_name : ''"
      :collapsed="sidebarCollapsed"
      @toggle="toggleSidebar"
    />
    <div class="oa-body">
      <ChatView
        v-if="user && selectedKb"
        :key="selectedKb.kb_name"
        :kb="selectedKb"
        :user="user"
        :sidebar-collapsed="sidebarCollapsed"
        @toggle-sidebar="toggleSidebar"
      />
      <div v-if="!selectedKb" class="oa-welcome">
        <div class="oa-welcome__card">
          <div class="oa-welcome__logo">AI</div>
          <div class="oa-welcome__title">{{ appState.title }}</div>
          <div v-if="appState.desc" class="oa-welcome__desc">{{ appState.desc }}</div>
          <button v-if="authError" class="oa-welcome__suggest" @click="handleAuthExpired">重新登录</button>
          <button v-else-if="kbError" class="oa-welcome__suggest" @click="loadKbs">重新加载</button>
        </div>
      </div>
    </div>
  </div>
</template>
