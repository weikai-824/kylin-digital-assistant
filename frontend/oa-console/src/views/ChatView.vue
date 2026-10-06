<script setup>
import { computed, nextTick, onMounted, onUnmounted, ref, watch } from 'vue'
import { kbChatStream, kbDisplayName, kbShortName, optimizePrompt } from '../api/kb'
import OaIcon from '../components/OaIcon.vue'
import { useSessions } from '../composables/useSessions'

/**
 * AI 助手对话主页（真实业务版）。
 * 数据全部来自后端：
 *  - 知识库由 App 层从 GET /knowledge_base/list_knowledge_bases 真实列表选中后传入
 *  - 对话走 POST /chat/kb_chat（SSE 流式），带当前用户 user_id/dept
 *  - 多会话：消息快照与 sessionId 由 useSessions store（localStorage）承载，
 *    侧栏切换会话 → store.activeId 变化 → 本组件 watch 同步装载对应消息
 */
const props = defineProps({
  kb: { type: Object, default: null }, // { kb_name, kb_info, managed }
  user: {
    type: Object,
    default: () => ({ userid: 'dev_local', username: '本地开发者', dept: '' }),
  },
  sidebarCollapsed: { type: Boolean, default: true },
})
const emit = defineEmits(['toggle-sidebar'])

const store = useSessions()

// 示例问题（与当前 oa_ma 知识库真实覆盖的章节对应，逐个实测可答且回答稳定；
// "目前支持单点登录的系统有哪些"因解析归属错位导致清单不在其父块内、回答不稳定，已换下）
const SUGGESTIONS = [
  '如何发起一个审批流程？',
  '如何使用微信快捷审批报销单？',
  '出差申请怎么提交？',
]

// 思考过程（虚化思维链）。
// 内容来自后端 thought 事件（结构化中间产物：改写后的检索意图 / 检索到的文档 / 组织答案），
// 前端按"多行虚化"呈现 —— opacity 低、灰色、随到随推，让用户看到 AI 真实在做什么，
// 避免首 token 前的长等待被误认为"卡死"。首 token 到达后收起为"已完成思考"。
const STAGE_LABEL = {
  planning: '理解',
  retrieving: '检索',
  generating: '生成',
}

// 阶段占位文案（改动3/4 同源表）：progress 先于真实 thought 到达，
// 用来在等待期给用户"正在做什么"的体感。label 用于思考面板标题（header 动态化），
// text 用于占位行正文 —— 措辞与左侧阶段标签语义互补，避免"理解｜正在理解问题…"式叠词。
// 后端 status_text 仅是兜底（text 缺失时用），文案最终归属前端展示层，便于随时打磨。
const STAGE_HINT = {
  planning: { label: '理解中', text: '理解你的问题，规划如何检索…' },
  retrieving: { label: '检索中', text: '在知识库中查找相关资料…' },
  generating: { label: '生成中', text: '正在组织答案…' },
}

const messages = ref([])
const input = ref('')
const sending = ref(false)
// 主链路中断句柄：发送时创建，点「停止」/ 组件卸载时 abort，供"停止生成"按钮使用
const streamCtrl = ref(null)
// 提示词改写旁路状态：optimizing 控制按钮 loading；optimizeCtrl 供 Esc 中断
const optimizing = ref(false)
const optimizeCtrl = ref(null)
const optimizeToast = ref({ visible: false, msg: '' })
let optimizeToastTimer = null
// 按钮内联反馈：no_rewrite 时按钮短暂变绿色「✓ 已识别为清晰」，不弹孤儿 toast
const optimizeAck = ref(false)
let optimizeAckTimer = null
// 改写建议卡：rewrite 结果先经用户确认（原 vs 改 + 应用/保留），绝不未经同意替换输入
const suggestion = ref(null) // { original: string, optimized: string }
const sessionId = ref(null)
const messageListRef = ref(null)
const textareaRef = ref(null)
// 当前视图已装载的 store 会话 id（null = 草稿态/新对话）
const loadedId = ref(null)

// 触屏设备（手机/平板）：占位提示不提 Enter 发送（移动端无物理键盘语义）
const isTouch = computed(
  () => typeof window !== 'undefined' && window.matchMedia && window.matchMedia('(pointer: coarse)').matches
)
const inputPlaceholder = computed(() =>
  kbName() ? (isTouch.value ? '请输入你的问题' : '请输入你的问题，Enter 发送，Shift+Enter 换行') : '请先选择知识库'
)

// ===== 移动端软键盘守卫 =====
// webview 键盘弹起若为"覆盖式"（不压缩布局视口），底部输入框会被键盘挡住。
// 用 visualViewport 监听：innerHeight(布局视口) 与 visualViewport.height(可视视口)
// 差值 >120px 判定键盘弹起 → 把 #app 高度钉到可视高度，输入区随之顶到键盘上方。
// 桌面端 Ctrl+/- 缩放时两个视口同步变化、差值极小，不会触发，PC 行为不变。
const KEYBOARD_THRESHOLD = 120
function onViewportResize() {
  const vv = window.visualViewport
  if (!vv) return
  const appEl = document.getElementById('app')
  if (!appEl) return
  const keyboardOpen = window.innerHeight - vv.height > KEYBOARD_THRESHOLD
  if (keyboardOpen) {
    appEl.style.height = vv.height + 'px'
    // 键盘弹起瞬间确保输入框仍在可视范围内
    const el = document.activeElement
    if (el && el.tagName === 'TEXTAREA') el.scrollIntoView({ block: 'nearest' })
  } else {
    appEl.style.height = '' // 键盘收起，恢复 CSS 控制的高度（100dvh）
  }
}
onMounted(() => {
  window.visualViewport?.addEventListener('resize', onViewportResize)
})
onUnmounted(() => {
  window.visualViewport?.removeEventListener('resize', onViewportResize)
  cancelOptimize()
  stopStreaming() // 兜底：卸载时中断仍在跑的问答流
  clearTimeout(optimizeToastTimer)
  clearTimeout(optimizeAckTimer)
  // 兜底：卸载时清掉仍在跑的思考计时器（正常流在 finishThinking 已清）
  for (const m of messages.value) {
    if (m.thinking && m.thinking.timer) {
      clearInterval(m.thinking.timer)
      m.thinking.timer = null
    }
  }
})

// 还没开始正式提问 → 在输入栏上方持续展示示例问题 chips
const hasUserMessage = () => messages.value.some((m) => m.role === 'user')

const kbName = () => (props.kb ? props.kb.kb_name : '')

function scrollToBottom() {
  nextTick(() => {
    const el = messageListRef.value
    if (el) el.scrollTop = el.scrollHeight
  })
}

// ===== store <-> 视图 同步 =====
// 把激活会话的消息装载进视图（草稿态 = 清空进入欢迎页）
function syncFromStore() {
  const s = store.active(kbName())
  loadedId.value = s ? s.id : null
  sessionId.value = s ? s.sessionId || null : null
  messages.value = s
    ? s.messages.map((m) => ({ ...m, refs: m.refs || [], refused: !!m.refused, ticketId: m.ticketId ?? null }))
    : []
  scrollToBottom()
}

// 知识库就绪 / 变化时装载
watch(
  () => props.kb && props.kb.kb_name,
  (name) => {
    if (name) syncFromStore()
  },
  { immediate: true }
)

// 侧栏切换会话 / 新对话 / 删除当前会话 → store.activeId 变化 → 装载
// 流式进行中禁止切换：回弹 activeId，避免流式回调写到已被替换的 messages 数组
watch(
  () => {
    const kb = kbName()
    const s = kb ? store.state.byKb[kb] : null
    return s ? s.activeId : undefined
  },
  (newId) => {
    if (newId === loadedId.value) return
    if (sending.value) {
      if (kbName() && loadedId.value) store.setActive(kbName(), loadedId.value)
      else if (kbName()) store.startDraft(kbName())
      return
    }
    syncFromStore()
  }
)

function newThinking() {
  return {
    active: true,
    done: false,
    collapsed: false,
    startedAt: Date.now(),
    elapsed: '0.0',
    timer: null, // 计时器句柄，send() 里 push 后由 startThinkingTicker 挂载
    currentStage: null, // 最近一次 progress 的阶段键，驱动 header 动态文案（改动4）
    lines: [], // [{ stage: 'planning'|'retrieving'|'generating', text: '...', isHint?: true }]
  }
}

// 每 100ms 刷新思考耗时时长（elapsed），让"正在思考 X.Xs"真正走动起来。
// thinking 必须是已 push 进 messages 的 proxy 引用（getAi().thinking），写字段才触发响应式。
function startThinkingTicker(thinking) {
  if (!thinking || thinking.timer) return
  thinking.timer = setInterval(() => {
    if (!thinking.active) return
    thinking.elapsed = ((Date.now() - thinking.startedAt) / 1000).toFixed(1)
  }, 100)
}

// 阶段进度占位（progress 事件）：真实 thought 到达前先亮一行"理解中/检索中/生成中"文案，
// 避免首条 thought 前整段空白被误判为卡死。isHint=true 标记，同阶段真实 thought 到达时替换。
// 同时把 thinking.currentStage 推为最新，供 header 动态文案（改动4）取用。
function pushStageHint(thinking, stage, text) {
  if (!thinking || !stage || !text) return
  thinking.currentStage = stage
  const lines = thinking.lines
  const last = lines[lines.length - 1]
  if (last && last.stage === stage && last.isHint) {
    last.text = text // 同阶段重复 progress：仅刷新文案，不叠行
    return
  }
  lines.push({ stage, text, isHint: true })
}

// header 动态文案（改动4）：思考进行中按当前阶段显示"理解中/检索中/生成中"，
// progress 尚未到达（currentStage 为空）时回退为"正在思考"。
function thinkActiveLabel(thinking) {
  if (!thinking || !thinking.currentStage) return '正在思考'
  const hint = STAGE_HINT[thinking.currentStage]
  return (hint && hint.label) || '正在思考'
}

// 后端 thought 事件 -> 追加/合并到思考面板的虚化行（同阶段合并、异阶段起新行）。
// 若该阶段先落过进度占位行（isHint），真实 thought 直接替换占位文本，不留"占位+正文"拼接。
function appendThinkLine(thinking, stage, delta) {
  if (!thinking || !delta) return
  const lines = thinking.lines
  const last = lines[lines.length - 1]
  if (last && last.stage === stage) {
    if (last.isHint) {
      last.text = delta
      delete last.isHint
    } else {
      last.text += delta
    }
  } else {
    lines.push({ stage: stage || '', text: delta })
  }
}

// 首个 token 到达 / 出错 / 流结束 -> 收尾：停表、elapsed 定格、默认折叠
function finishThinking(thinking) {
  if (!thinking) return
  thinking.done = true
  thinking.active = false
  thinking.collapsed = true // 默认收起：完成后再展开看过程，事后不干扰阅读
  if (thinking.timer) {
    clearInterval(thinking.timer)
    thinking.timer = null
  }
  thinking.elapsed = ((Date.now() - thinking.startedAt) / 1000).toFixed(1)
}

function pushMessage(role, content) {
  messages.value.push({ role, content: content || '', refs: [] })
}

// 消息快照回写 store（只留可序列化的字段，thinking/streaming 状态不落盘）
function persist() {
  if (!kbName() || !loadedId.value) return
  const snapshot = messages.value.map((m) => ({
    role: m.role,
    content: m.content,
    refs: m.refs || [],
    refused: !!m.refused,
    ticketId: m.ticketId ?? null,
  }))
  store.syncSession(kbName(), loadedId.value, { messages: snapshot, sessionId: sessionId.value })
}

// 中断当前问答流（「停止生成」按钮 / 组件卸载兜底）。
// 只发 abort 信号，具体收尾（停表、落盘、提示语）由 kbChatStream 的 onError 与 send 的 finally 完成。
function stopStreaming() {
  const ctrl = streamCtrl.value
  if (ctrl && !ctrl.signal.aborted) ctrl.abort()
}

async function send(raw) {
  const text = (raw ?? input.value).trim()
  if (!text || sending.value || optimizing.value) return
  if (!kbName()) return
  // 有未决改写建议时点发送 = 放弃建议，按输入框原文发送
  if (suggestion.value) suggestion.value = null
  input.value = ''
  resizeTextarea()

  // 首条消息落地为新会话（标题取首问），后续消息写回当前会话
  if (!loadedId.value) {
    const item = store.beginSession(kbName(), text)
    if (!item) return
    loadedId.value = item.id
  }

  pushMessage('user', text)
  const ai = {
    role: 'assistant',
    content: '',
    refs: [],
    refused: false,
    ticketId: null,
    streaming: true,
    thinking: newThinking(),
  }
  messages.value.push(ai)
  // 记录 ai 在 reactive 数组中的索引：Vue 3 对 ref 数组 push 进去的对象做 deep reactive
  // 代理，闭包持有的原始 `ai` 引用写入属性/数组不会触发响应式。因此后续所有回调都通过
  // messages.value[aiIdx] 重新取 proxy 引用，保证写入路径与模板读取路径是同一个响应式对象。
  const aiIdx = messages.value.length - 1
  // 统一从 reactive 数组取当前 ai 的 proxy 引用，规避"闭包持有原始对象"的响应式陷阱
  const getAi = () => messages.value[aiIdx]
  // 计时器：从这里开始走秒（thinking 已是 proxy，写 elapsed 能触发渲染）
  startThinkingTicker(getAi().thinking)
  scrollToBottom()

  const history = messages.value
    .filter((m) => m !== ai && !m.streaming)
    .map((m) => ({ role: m.role, content: m.content }))
    .slice(-10)

  sending.value = true
  // 本轮流的中断句柄：点「停止」时 abort，kb.js 收到后抛 name=AbortError 的错
  const ctrl = new AbortController()
  streamCtrl.value = ctrl
  try {
    await kbChatStream(
      {
        query: text,
        kbName: kbName(),
        history,
        sessionId: sessionId.value,
        userId: props.user.userid || 'dev_local',
        dept: props.user.dept || null,
        signal: ctrl.signal,
      },
      {
        onStart: () => {
          getAi().streaming = true
          scrollToBottom()
        },
        onProgress: (stage, statusText) => {
          // 阶段进度事件：真实 thought 前先落一条占位行（文案取本地 STAGE_HINT，
          // 后端 status_text 兜底），按阶段点亮思考面板与 header，让规划/检索期不是空白等待
          const cur = getAi()
          if (cur && cur.thinking) {
            const hint = (STAGE_HINT[stage] && STAGE_HINT[stage].text) || statusText || ''
            pushStageHint(cur.thinking, stage, hint)
          }
          scrollToBottom()
        },
        onThink: ({ stage, delta }) => {
          // 后端 thought 事件：虚化思维链的多行内容，随到随推
          appendThinkLine(getAi().thinking, stage, delta)
          scrollToBottom()
        },
        onToken: (chunk) => {
          const cur = getAi()
          cur.streaming = true
          cur.content += chunk
          finishThinking(cur.thinking)
          scrollToBottom()
        },
        onDone: (result) => {
          const cur = getAi()
          sessionId.value = result.sessionId || sessionId.value
          const refusal =
            Array.isArray(result.docs) &&
            result.docs.some((d) => typeof d === 'string' && /未找到相关文档/.test(d))
          cur.refused = refusal
          cur.refs = refusal ? [] : Array.isArray(result.docs) ? result.docs : []
          cur.ticketId = result.ticketId
          cur.streaming = false
          finishThinking(cur.thinking)
          if (!cur.content) cur.content = '未检索到相关资料，请换个问法再试'
          persist()
          scrollToBottom()
        },
        onError: (e) => {
          const cur = getAi()
          cur.streaming = false
          finishThinking(cur.thinking)
          const msg = (e && e.message) || e || '未知错误'
          // AbortError = 用户点「停止」；其余超时/网络错走"请求失败"文案
          const stopped = (e && e.name) === 'AbortError'
          if (!cur.content) {
            cur.content = stopped ? msg : `请求失败：${msg}`
          } else {
            cur.content += `\n\n[${stopped ? '已停止生成' : `流中断：${msg}`}]`
          }
          persist()
        },
      }
    )
  } catch (e) {
    const cur = getAi()
    const stopped = (e && e.name) === 'AbortError'
    if (!cur.content) cur.content = stopped ? '已停止生成' : `请求失败：${(e && e.message) || e}`
    cur.streaming = false
    finishThinking(cur.thinking)
  } finally {
    sending.value = false
    streamCtrl.value = null
    finishThinking(getAi().thinking)
    persist()
    scrollToBottom()
  }
}

function onKeydown(ev) {
  if (ev.key === 'Escape') {
    if (sending.value) {
      // 生成中：Esc 中断本轮回答（等价于点「停止」）
      ev.preventDefault()
      stopStreaming()
      return
    }
    if (optimizing.value) {
      // 改写进行中：Esc 中断（静默，不改输入、不提示）
      ev.preventDefault()
      cancelOptimize()
      return
    }
    if (suggestion.value) {
      // 建议卡展示中：Esc = 保留原文
      ev.preventDefault()
      dismissSuggestion()
      return
    }
  }
  if (ev.key === 'Enter' && !ev.shiftKey) {
    ev.preventDefault()
    send()
  }
}

function resizeTextarea() {
  const el = textareaRef.value
  if (!el) return
  el.style.height = 'auto'
  el.style.height = Math.min(el.scrollHeight, 140) + 'px'
}

// ===== 提示词改写旁路（发送前优化，独立于 kb_chat 主链路） =====
// 触发点：工具栏「优化提示词」按钮。一次 LLM 改写后回填输入框，供用户审阅后再发送。
// 语义：不检索、不写会话、不碰主链路；失败（后端回退原文 / 网络错）时保留输入并 toast。
const canOptimize = computed(
  () => kbName() && !sending.value && !optimizing.value && !suggestion.value && !!input.value.trim()
)

function showOptimizeToast(msg) {
  optimizeToast.value = { visible: true, msg }
  clearTimeout(optimizeToastTimer)
  optimizeToastTimer = setTimeout(() => {
    optimizeToast.value.visible = false
  }, 3000)
}

// 按钮内联反馈：no_rewrite 时按钮短暂变绿色「✓ 已识别为清晰」，1.5s 后还原
function flashOptimizeAck() {
  optimizeAck.value = true
  clearTimeout(optimizeAckTimer)
  optimizeAckTimer = setTimeout(() => {
    optimizeAck.value = false
  }, 1500)
}

function moveCursorToEnd(el) {
  const len = el.value ? el.value.length : 0
  if (typeof el.setSelectionRange === 'function') {
    el.setSelectionRange(len, len)
  }
  el.focus()
}

async function onOptimize() {
  const text = input.value.trim()
  if (!canOptimize.value || !text) return
  optimizing.value = true
  const ctrl = new AbortController()
  optimizeCtrl.value = ctrl
  try {
    const { optimized, kind } = await optimizePrompt(text, { signal: ctrl.signal })
    if (ctrl.signal.aborted) return // Esc 取消：不改输入、不提示
    if (kind === 'no_rewrite') {
      // 原句已清晰 / 需上下文指代：保留原文，交由主链路（规划器带历史）处理
      // 用按钮内联反馈（绿色「✓ 已识别为清晰」），不弹孤儿 toast
      flashOptimizeAck()
      return
    }
    if (kind === 'fallback') {
      // 后端回退原文（LLM 不可用/空输出/异常）：输入保持不变，仅提示
      showOptimizeToast('改写服务暂不可用，已保留原文，可直接发送')
      return
    }
    // rewrite：弹建议卡让用户确认，绝不未经同意替换输入
    if (optimized && optimized !== text) {
      suggestion.value = { original: text, optimized }
    }
  } catch (e) {
    if (e && e.name === 'AbortError') return // Esc 主动取消，静默
    showOptimizeToast('改写失败，请稍后重试')
  } finally {
    optimizing.value = false
    optimizeCtrl.value = null
  }
}

// 应用改写建议：替换输入框并聚焦末尾
async function applySuggestion() {
  const sug = suggestion.value
  if (!sug || !sug.optimized) return
  suggestion.value = null
  input.value = sug.optimized
  await nextTick()
  resizeTextarea()
  moveCursorToEnd(textareaRef.value)
}

// 保留原文：关闭建议卡，输入不变
function dismissSuggestion() {
  suggestion.value = null
  const el = textareaRef.value
  if (el) el.focus()
}

function cancelOptimize() {
  const ctrl = optimizeCtrl.value
  if (ctrl && !ctrl.signal.aborted) ctrl.abort()
}

function newTopic() {
  if (sending.value) return
  // 回到草稿态：清空视图 + store.activeId 置空（侧栏回到"未选中"）
  messages.value = []
  sessionId.value = null
  loadedId.value = null
  if (kbName()) store.startDraft(kbName())
  scrollToBottom()
}

function refTitle(r) {
  if (typeof r === 'string') return r
  return r.doc_title || r.file_name || r.source || r.section_title || '参考资料'
}

function refMeta(r) {
  if (typeof r === 'string') return ''
  const parts = []
  if (r.file_name && r.file_name !== refTitle(r)) parts.push(r.file_name)
  if (r.section_title) parts.push(r.section_title)
  if (typeof r.score === 'number') parts.push(`相关度 ${r.score.toFixed(3)}`)
  return parts.join(' · ')
}

onMounted(scrollToBottom)
</script>

<template>
  <div class="oa-chat">
    <div class="oa-chat__header">
      <button
        class="oa-chat__sbtoggle"
        :title="sidebarCollapsed ? '展开会话栏' : '收起会话栏'"
        @click="emit('toggle-sidebar')"
      >
        <OaIcon name="panel" :size="16" />
      </button>
      <div class="oa-chat__title">
        <OaIcon name="chat" :size="17" style="color: var(--oa-primary)" />
        <template v-if="kb">{{ kbDisplayName(kb) }}</template>
        <template v-else>AI 助手</template>
      </div>
      <div class="oa-chat__meta">
        <span v-if="kb && kb.managed" class="oa-chat__tag">系统知识库</span>
        <span class="oa-chat__kb">{{ kb ? kb.kb_name : '未选择知识库' }}</span>
      </div>
    </div>

    <div ref="messageListRef" class="oa-chat__messages">
      <template v-if="!kb">
        <div class="oa-welcome">
          <div class="oa-welcome__card">
            <div class="oa-welcome__logo">AI</div>
            <div class="oa-welcome__title">请选择知识库</div>
            <div class="oa-welcome__desc">知识库尚未就绪，加载完成后即可开始问答。</div>
          </div>
        </div>
      </template>

      <template v-else-if="messages.length === 0">
        <!-- DeepSeek 式空态：去卡片去大图标，轻量欢迎区（左对齐，与对话栏同宽） -->
        <div class="oa-welcome">
          <div class="oa-welcome__hero">
            <OaIcon name="spark" :size="28" class="oa-welcome__spark" />
            <div class="oa-welcome__greet">你好，我是{{ kbDisplayName(kb) }}</div>
          </div>
        </div>
      </template>

      <template v-else>
        <div
          v-for="(m, i) in messages"
          :key="i"
          class="oa-msg"
          :class="m.role === 'user' ? 'oa-msg--user' : 'oa-msg--assistant'"
        >
          <div class="oa-msg__avatar">
            <template v-if="m.role === 'user'">
              <OaIcon name="user" :size="18" />
            </template>
            <template v-else>
              <OaIcon name="spark" :size="18" />
            </template>
          </div>
          <div class="oa-msg__body">
            <div class="oa-msg__name">
              {{ m.role === 'user' ? user.username : `${kbShortName(kb)} 助手` }}
            </div>

            <!-- 思考过程面板（虚化思维链） -->
            <!-- 等待中：opacity 半透明 + 顶部 shimmer 扫光进度条，"在做事但不剧透步骤" -->
            <!-- 完成后：默认收起，只留"✓ 已完成思考（Xs）"一行，点开才看到过程 -->
            <div
              v-if="m.thinking"
              class="oa-think"
              :class="{ 'oa-think--done': m.thinking.done }"
            >
              <div
                class="oa-think__header"
                @click="m.thinking.done && (m.thinking.collapsed = !m.thinking.collapsed)"
              >
                <template v-if="m.thinking.active">
                  <span class="oa-think__spinner"></span>
                  <span class="oa-think__label">{{ thinkActiveLabel(m.thinking) }}</span>
                  <span class="oa-think__elapsed">{{ m.thinking.elapsed }}s</span>
                </template>
                <template v-else>
                  <span class="oa-think__check">✓</span>
                  <span class="oa-think__label">已完成思考（{{ m.thinking.elapsed }}s）</span>
                  <span class="oa-think__toggle">{{ m.thinking.collapsed ? '展开' : '收起' }}</span>
                </template>
              </div>
              <!-- 虚化思维链：后端 thought 事件的多行内容，随到随推，opacity 低 -->
              <div
                v-if="(m.thinking.active || !m.thinking.collapsed) && m.thinking.lines.length"
                class="oa-think__lines"
              >
                <div
                  v-for="(line, li) in m.thinking.lines"
                  :key="li"
                  class="oa-think__line"
                  :class="`oa-think__line--${line.stage}`"
                >
                  <span v-if="line.stage" class="oa-think__stage">{{ STAGE_LABEL[line.stage] || line.stage }}</span>
                  <span class="oa-think__text">{{ line.text }}</span>
                </div>
              </div>
            </div>

            <div class="oa-msg__bubble">
              <span style="white-space: pre-wrap">{{ m.content }}</span>
              <span v-if="m.streaming" class="oa-cursor"></span>
            </div>

            <div v-if="m.refs && m.refs.length" class="oa-msg__refs">
              <details>
                <summary>参考资料（{{ m.refs.length }}）</summary>
                <ul>
                  <li v-for="(r, ri) in m.refs.slice(0, 10)" :key="ri">
                    <div class="oa-ref__title">{{ refTitle(r) }}</div>
                    <div v-if="refMeta(r)" class="oa-ref__meta">{{ refMeta(r) }}</div>
                  </li>
                </ul>
              </details>
            </div>

            <!-- 知识缺口工单提示：硬拒答（无文档）与软拒答（有文档但判据不足）都会带 ticketId -->
            <div v-if="m.ticketId" class="oa-msg__ticket">
              ⚠️ 本题暂缺可靠依据，已自动登记为知识缺口工单 #{{ m.ticketId }}；人工补充答案反哺知识库后即可正常回答。
            </div>
          </div>
        </div>
      </template>
    </div>

    <div v-if="kb && !hasUserMessage()" class="oa-chat__chips">
      <div class="oa-chat__chips-title">试试这样问</div>
      <div class="oa-welcome__suggests">
        <button
          v-for="q in SUGGESTIONS"
          :key="q"
          class="oa-welcome__suggest"
          :disabled="sending"
          @click="send(q)"
        >
          {{ q }}
        </button>
      </div>
    </div>

    <div class="oa-chat__inputbar">
      <div class="oa-chat__inputwrap">
        <textarea
          ref="textareaRef"
          v-model="input"
          class="oa-chat__textarea"
          rows="1"
          :placeholder="inputPlaceholder"
          :disabled="sending || !kb"
          @keydown="onKeydown"
          @input="resizeTextarea"
        ></textarea>
        <div class="oa-chat__toolbar">
          <div class="oa-chat__tools">
            <button
              class="oa-chat__optimize"
              :class="{
                'oa-chat__optimize--busy': optimizing,
                'oa-chat__optimize--ack': optimizeAck
              }"
              :disabled="!canOptimize"
              title="把输入改写为更清晰完整的问题，改写后可继续编辑或发送"
              @click="onOptimize"
            >
              <svg
                v-if="!optimizing && !optimizeAck"
                class="oa-chat__optimize-icon"
                viewBox="0 0 16 16"
                width="12"
                height="12"
                aria-hidden="true"
              >
                <path
                  d="M8 1.6l1.5 4.3 4.3 1.5-4.3 1.5L8 13.2 6.5 8.9 2.2 7.4l4.3-1.5z"
                  fill="currentColor"
                />
              </svg>
              <span v-else-if="optimizeAck" class="oa-chat__optimize-check" aria-hidden="true">✓</span>
              <span v-else class="oa-chat__optimize-spin" aria-hidden="true"></span>
              <span>{{ optimizing ? '优化中…' : optimizeAck ? '已识别为清晰' : '优化提示词' }}</span>
            </button>
          </div>
          <button
            v-if="sending"
            class="oa-chat__send oa-chat__send--stop"
            title="中断本轮回答"
            @click="stopStreaming"
          >
            停止
          </button>
          <button
            v-else
            class="oa-chat__send"
            :disabled="optimizing || !input.trim() || !kb"
            @click="send()"
          >
            发送
          </button>
        </div>
        <transition name="oa-suggest-fade">
          <div v-if="suggestion" class="oa-chat__suggest" role="dialog" aria-label="改写建议">
            <div class="oa-chat__suggest-title">改写建议</div>
            <div class="oa-chat__suggest-row">
              <span class="oa-chat__suggest-tag">原</span>
              <span class="oa-chat__suggest-text oa-chat__suggest-text--orig">{{ suggestion.original }}</span>
            </div>
            <div class="oa-chat__suggest-row">
              <span class="oa-chat__suggest-tag oa-chat__suggest-tag--new">改</span>
              <span class="oa-chat__suggest-text oa-chat__suggest-text--new">{{ suggestion.optimized }}</span>
            </div>
            <div class="oa-chat__suggest-actions">
              <button class="oa-chat__suggest-btn oa-chat__suggest-btn--apply" @click="applySuggestion">
                应用改写
              </button>
              <button class="oa-chat__suggest-btn oa-chat__suggest-btn--keep" @click="dismissSuggestion">
                保留原文
              </button>
            </div>
          </div>
        </transition>
        <transition name="oa-toast-fade">
          <div v-if="optimizeToast.visible" class="oa-chat__toast" role="status">
            {{ optimizeToast.msg }}
          </div>
        </transition>
      </div>
      <div class="oa-chat__hint">AI 生成内容仅供参考，请以 OA 系统实际功能与内部制度为准</div>
    </div>
  </div>
</template>
