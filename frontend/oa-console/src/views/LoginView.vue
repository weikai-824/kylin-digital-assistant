<script setup>
import { onMounted, ref } from 'vue'
import { loginWithJava } from '../api/kb'

const emit = defineEmits(['success'])

const username = ref('')
const password = ref('')
const rememberMe = ref(false)
const loading = ref(false)
const error = ref('')

const rememberedUser = window.localStorage.getItem('DIGITAL_HUMAN_USERNAME')
if (rememberedUser) {
  username.value = rememberedUser
  rememberMe.value = true
}

async function submitLogin() {
  if (!username.value.trim() || !password.value) {
    error.value = '请输入账号和密码'
    return
  }
  loading.value = true
  error.value = ''
  try {
    await loginWithJava(username.value.trim(), password.value)
    if (rememberMe.value) window.localStorage.setItem('DIGITAL_HUMAN_USERNAME', username.value.trim())
    else window.localStorage.removeItem('DIGITAL_HUMAN_USERNAME')
    password.value = ''
    emit('success')
  } catch (e) {
    error.value = e.message || '登录失败，请重试'
  } finally {
    loading.value = false
  }
}

onMounted(() => {
  document.title = '麒麟数字人 - 智能问答'
})
</script>

<template>
  <main class="dh-login">
    <div class="dh-login__visual" aria-hidden="true">
      <div class="dh-login__grid"></div>
      <div class="dh-login__mark">AI</div>
      <p class="dh-login__visual-title">麒麟数字人</p>
      <p class="dh-login__visual-copy">让知识，随时可问</p>
    </div>
    <section class="dh-login__panel" aria-labelledby="login-title">
      <div class="dh-login__eyebrow">DIGITAL HUMAN</div>
      <h1 id="login-title">智能问答</h1>
      <p class="dh-login__hint">使用统一账号登录，访问企业知识库</p>
      <form class="dh-login__form" @submit.prevent="submitLogin">
        <label class="dh-login__field">
          <span>账号</span>
          <input v-model="username" autocomplete="username" placeholder="请输入账号" :disabled="loading" />
        </label>
        <label class="dh-login__field">
          <span>密码</span>
          <input v-model="password" type="password" autocomplete="current-password" placeholder="请输入密码" :disabled="loading" />
        </label>
        <label class="dh-login__remember">
          <input v-model="rememberMe" type="checkbox" />
          <span>记住账号</span>
        </label>
        <p v-if="error" class="dh-login__error" role="alert">{{ error }}</p>
        <button class="dh-login__submit" type="submit" :disabled="loading">
          <span v-if="loading" class="dh-login__spinner"></span>
          {{ loading ? '登录中...' : '登录智能问答' }}
        </button>
      </form>
      <p class="dh-login__footer">登录后将按统一权限展示可访问的知识库</p>
    </section>
  </main>
</template>
