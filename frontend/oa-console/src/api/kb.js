// Kylin 后端 API 封装 —— 本地走 Vite 代理，生产走 Gateway 统一 API 前缀。
// 真实后端路由见 kylin/adapters/inbound/http/chat.py：POST /chat/kb_chat（SSE 流式）
// 知识库列表：GET /knowledge_base/list_knowledge_bases

/**
 * 优化提示词（发送前改写，不触发检索、不写会话）。
 * 对应 POST /chat/optimize_prompt —— 一次 LLM 改写后回填输入框。
 * 后端保证 HTTP 200：正常改写 notes 为空；回退原文时 notes 以 empty/exception 开头。
 * @param {string} text 用户原始输入（调用方需先 trim 校验非空）
 * @param {object}  [opts]        { signal?: AbortSignal } 供调用方 Esc 中断
 * @returns {Promise<{optimized: string, kind: string, notes: string}>}
 *   kind: 'rewrite'（有改写，建议卡确认）| 'no_rewrite'（无需改写/需上下文指代，保留原文）
 *         | 'fallback'（服务不可用回退原文，notes 说明原因）
 */
export async function optimizePrompt(text, opts = {}) {
  const ctrl = new AbortController()
  const timer = setTimeout(() => ctrl.abort(), 30000)
  // 外部 signal 触发时同样中止（AbortSignal.any 兼容性差，手动桥接）
  const onOuterAbort = () => ctrl.abort()
  const outerSignal = opts.signal || null
  if (outerSignal) {
    if (outerSignal.aborted) ctrl.abort()
    else outerSignal.addEventListener('abort', onOuterAbort, { once: true })
  }
  try {
    const resp = await request('/chat/optimize_prompt', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
      body: JSON.stringify({ query: text }),
      signal: ctrl.signal,
    })
    const json = await resp.json().catch(() => null)
    if (!resp.ok || !json || json.code !== 200) {
      throw new Error((json && json.msg) || `后端返回 HTTP ${resp.status}`)
    }
    const data = (json.data && typeof json.data === 'object') ? json.data : {}
    return {
      optimized: typeof data.optimized === 'string' ? data.optimized : text,
      kind: data.kind === 'no_rewrite' || data.kind === 'rewrite' || data.kind === 'fallback'
        ? data.kind
        : (data.optimized === text ? 'no_rewrite' : 'rewrite'),
      notes: typeof data.notes === 'string' ? data.notes : '',
    }
  } catch (e) {
    // 用户 Esc 主动取消：原样抛 AbortError，视图据此静默处理
    if (e.name === 'AbortError' && outerSignal && outerSignal.aborted) throw e
    if (e.name === 'AbortError') throw new Error('改写请求超时，请重试')
    throw e
  } finally {
    clearTimeout(timer)
    if (outerSignal) outerSignal.removeEventListener('abort', onOuterAbort)
  }
}

export const DEFAULT_KB = 'oa_ma' // 项目真实知识库名（oa_manuals 仅为代码默认误用）
const defaultApiBase = import.meta.env.PROD
  ? '/prod-api/app-api/ai/digital-human'
  : ''
export const API_BASE = (import.meta.env.VITE_API_BASE ?? defaultApiBase).replace(/\/$/, '')
export const SSO_ENABLED = import.meta.env.VITE_SSO_ENABLED
  ? import.meta.env.VITE_SSO_ENABLED === 'true'
  : import.meta.env.PROD

function accessToken() {
  return window.localStorage.getItem('ACCESS_TOKEN') || ''
}

export function clearAuthTokens() {
  window.localStorage.removeItem('ACCESS_TOKEN')
  window.localStorage.removeItem('REFRESH_TOKEN')
}

async function request(path, options = {}) {
  const headers = new Headers(options.headers || {})
  const token = accessToken()
  if (token) headers.set('Authorization', `Bearer ${token}`)
  if (!token && SSO_ENABLED) throw new Error('当前未登录')
  const response = await fetch(`${API_BASE}${path}`, {
    credentials: 'include',
    ...options,
    headers,
  })
  if (SSO_ENABLED && response.status === 401) {
    clearAuthTokens()
    window.dispatchEvent(new CustomEvent('kylin-auth-expired'))
  }
  return response
}

const JAVA_API_BASE = (import.meta.env.VITE_JAVA_API_BASE ?? '/prod-api/admin-api').replace(/\/$/, '')
const TENANT_ID = import.meta.env.VITE_TENANT_ID || '1'

async function javaRequest(path, options = {}) {
  const headers = new Headers(options.headers || {})
  headers.set('Content-Type', 'application/json')
  headers.set('tenant-id', TENANT_ID)
  return fetch(`${JAVA_API_BASE}${path}`, {
    credentials: 'include',
    ...options,
    headers,
  })
}

async function javaJson(path, options = {}) {
  const response = await javaRequest(path, options)
  const json = await response.json().catch(() => null)
  if (!response.ok) {
    throw new Error((json && (json.msg || json.repMsg)) || `认证服务返回 HTTP ${response.status}`)
  }
  return json
}

/** 使用现有 Java 账号体系登录数字人平台。 */
export async function loginWithJava(username, password) {
  const json = await javaJson('/system/auth/digital-human-login', {
    method: 'POST',
    body: JSON.stringify({ username, password }),
  })
  const ok = json && (json.code === 0 || json.code === 200)
  const data = json && json.data
  if (!ok || !data || !data.accessToken) {
    throw new Error((json && json.msg) || '账号或密码错误')
  }
  window.localStorage.setItem('ACCESS_TOKEN', data.accessToken)
  if (data.refreshToken) window.localStorage.setItem('REFRESH_TOKEN', data.refreshToken)
  return data
}

export async function getCurrentUser() {
  const resp = await request('/auth/me', {
    headers: { Accept: 'application/json' },
  })
  if (!resp.ok) {
    throw new Error(resp.status === 401 ? '统一登录已失效，请重新登录' : `获取当前用户失败（HTTP ${resp.status}）`)
  }
  const json = await resp.json().catch(() => null)
  if (!json || json.code !== 200 || !json.data) {
    throw new Error((json && json.msg) || '获取当前用户失败')
  }
  return json.data
}

// 知识库显示名映射：内部 kb_name 不变（API/存储仍用），仅前端展示层替换
const KB_DISPLAY_NAMES = {
  oa_ma: '麒麟数字人-智能问答',
}

/** 知识库显示名：有映射用映射，否则回退原始 kb_name */
export function kbDisplayName(kb) {
  if (!kb) return ''
  return KB_DISPLAY_NAMES[kb.kb_name] || kb.kb_name
}

/** 消息署名等短场景：去掉"-智能问答"这类后缀（麒麟数字人-智能问答 → 麒麟数字人） */
export function kbShortName(kb) {
  return kbDisplayName(kb).replace(/-[^-]*$/, '')
}

/**
 * 拉取真实知识库列表。
 * 对应 GET /knowledge_base/list_knowledge_bases
 * @returns {Promise<Array<{kb_name:string,kb_info:string,vs_type:string,embed_model:string,managed:boolean}>>}
 */
export async function listKnowledgeBases() {
  const resp = await request('/knowledge_base/list_knowledge_bases', {
    method: 'GET',
    headers: { Accept: 'application/json' },
  })
  if (!resp.ok) throw new Error(`后端返回 HTTP ${resp.status}`)
  const json = await resp.json().catch(() => null)
  if (!json || json.code !== 200) {
    throw new Error((json && json.msg) || '获取知识库列表失败')
  }
  return Array.isArray(json.data) ? json.data : []
}

/**
 * 流式问答的三层超时（毫秒）。
 * 1) firstResponse：发起请求到收到第一个 SSE 事件的上限。要盖住 Ollama 空闲卸载后
 *    重载 bge-m3 的冷启动（实测约 18s）+ 检索耗时，太短会把正常慢请求误杀。
 * 2) idle：连接已建立但中途停发 chunk（网络半开 / 后端卡住）的上限，
 *    每收到一个 SSE 事件即重置，专治"流到一半假死"。
 * 3) total：整轮兜底，防止超长回答无限占用（后端 stream_runtime 是 300s，前端要更早放手）。
 */
export const STREAM_TIMEOUTS = {
  // 60s：实测热态首事件约 5~8s，服务重启后的冷启动（嵌入模型重载 + 检索）实测到 35s，
  // 取约 1.7 倍余量，避免并发排队时误杀；同时远早于原来的"干等 120s 才报错"。
  firstResponse: 60000,
  // 30s：实测检索阶段两事件间隔最大约 10.5s（热态），冷启动 + 大库检索会更慢，
  // 取 3 倍余量，只拦"流到一半真卡死"，不拦正常思考间隙。
  idle: 30000,
  total: 120000,
}

// 各类超时的用户可读文案（首响应/空闲/总时长）
const TIMEOUT_MSG = {
  first: `等待响应超时（${STREAM_TIMEOUTS.firstResponse / 1000}s 内未收到任何数据），请重试`,
  idle: `响应中断（${STREAM_TIMEOUTS.idle / 1000}s 未收到新数据），请重试`,
  total: `请求超时（${STREAM_TIMEOUTS.total / 1000}s），请重试`,
}

/**
 * 发起知识库对话（SSE 流式）。
 * @param {object}  opts
 * @param {string}  opts.query       用户输入
 * @param {string}  [opts.kbName]    知识库名，默认 oa_ma
 * @param {Array}   [opts.history]   [{role, content}] 上下文（不含本轮）
 * @param {string}  [opts.sessionId] 续聊会话 ID
 * @param {string}  [opts.userId]    当前用户 ID（identity_mode=fixed_dev 时为 dev_local）
 * @param {string}  [opts.dept]      当前用户部门（空=匿名，只看全开放文档）
 * @param {AbortSignal} [opts.signal] 外部中断信号（"停止生成"按钮用；中断后抛 name=AbortError 的错）
 * @param {object}  handlers         { onStart, onToken, onProgress, onThink, onDone, onError }
 * @returns {Promise<{sessionId: string, content: string, docs: Array, ticketId: string|null}>}
 */
export async function kbChatStream(opts, handlers = {}) {
  const {
    query,
    kbName = DEFAULT_KB,
    history = [],
    sessionId = null,
    userId = null,
    dept = null,
    signal: outerSignal = null,
  } = opts
  const {
    onStart = () => {},
    onToken = () => {},
    onProgress = () => {},
    onThink = () => {},
    onDone = () => {},
    onError = () => {},
  } = handlers

  const ctrl = new AbortController()
  const timers = { first: null, idle: null, total: null }
  // abort 原因：'first' | 'idle' | 'total' | 'user'，catch 里据此给出可区分的提示
  let abortReason = null

  const clearTimers = () => {
    clearTimeout(timers.first)
    clearTimeout(timers.idle)
    clearTimeout(timers.total)
    timers.first = timers.idle = timers.total = null
  }

  // 收到任意 SSE 事件即视为"还活着"：清掉首响应超时，并把空闲超时重置。
  // progress / thought / token 都算活跃——后端只要还在推东西就不该被判死。
  const bumpLiveness = () => {
    if (timers.first) {
      clearTimeout(timers.first)
      timers.first = null
    }
    clearTimeout(timers.idle)
    timers.idle = setTimeout(() => {
      abortReason = 'idle'
      ctrl.abort()
    }, STREAM_TIMEOUTS.idle)
  }

  // 外部中断（"停止生成"）：桥接到内部 ctrl，标记为 user 以便视图静默处理
  const onOuterAbort = () => {
    abortReason = 'user'
    ctrl.abort()
  }
  if (outerSignal) {
    if (outerSignal.aborted) onOuterAbort()
    else outerSignal.addEventListener('abort', onOuterAbort, { once: true })
  }

  const body = {
    query,
    kb_name: kbName,
    top_k: 5,
    history,
    session_id: sessionId,
    user_id: userId,
    dept,
    stream: true,
    model: null,
    temperature: null,
    prompt_name: 'default',
    return_direct: false,
  }

  try {
    // 首响应与总时长自发起请求起算（HTTP 响应头迟迟不回，同样算首响应超时）
    timers.first = setTimeout(() => {
      abortReason = 'first'
      ctrl.abort()
    }, STREAM_TIMEOUTS.firstResponse)
    timers.total = setTimeout(() => {
      abortReason = 'total'
      ctrl.abort()
    }, STREAM_TIMEOUTS.total)

    const resp = await request('/chat/kb_chat', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', Accept: 'text/event-stream' },
      body: JSON.stringify(body),
      signal: ctrl.signal,
    })
    if (!resp.ok) {
      const text = await resp.text().catch(() => '')
      throw new Error(`后端返回 HTTP ${resp.status}${text ? `：${text.slice(0, 200)}` : ''}`)
    }

    const reader = resp.body.getReader()
    const decoder = new TextDecoder('utf-8')
    let buffer = ''
    let result = {
      sessionId: sessionId || null,
      content: '',
      docs: [],
      rawDocs: [],
      ticketId: null,
      finishReason: null,
    }
    let sawStart = false

    const dispatch = (json) => {
      bumpLiveness()
      const obj = json.object === 'chat.completion' ? 'complete' : json.object === 'chat.completion.chunk' ? 'chunk' : 'other'
      if (json.session_id) result.sessionId = json.session_id
      if (obj === 'chunk' && json.progress) {
        // progress 事件：json.progress 是阶段键（planning/retrieving/generating），
        // json.status_text 是后端人读文案（"正在理解问题…"）。两者都透传，
        // 视图层才能按阶段渲染占位反馈（此前只传文案、丢了 stage 键）。
        onProgress(json.progress, json.status_text || json.progress)
      } else if (obj === 'chunk' && json.event === 'thought') {
        // 结构化思考中间产物：透传给 onThink 渲染虚化思维链
        onThink({ stage: json.stage, delta: json.delta || '' })
      } else if (obj === 'chunk' && !sawStart) {
        sawStart = true
        onStart({ sessionId: result.sessionId, docs: json.docs || [] })
      }
      if (json.content) {
        result.content += json.content
        onToken(json.content)
      }
      if (obj === 'complete' || json.finish_reason) {
        result.finishReason = json.finish_reason || 'stop'
        if (json.docs) result.docs = json.docs
        if (json.raw_docs) result.rawDocs = json.raw_docs
        if (json.ticket_id != null) result.ticketId = json.ticket_id
      }
    }

    for (;;) {
      const { done, value } = await reader.read()
      if (done) break
      buffer += decoder.decode(value, { stream: true })
      // SSE 事件间以空行分隔；兼容单行 data: 情况
      const lines = buffer.split(/\r?\n/)
      buffer = lines.pop() // 可能半行，留到下一轮
      for (const line of lines) {
        const trimmed = line.trim()
        if (!trimmed.startsWith('data:')) continue
        const payload = trimmed.slice(5).trim()
        if (!payload) continue
        try {
          const json = JSON.parse(payload)
          // 后端 sse_error 的 error 是字符串（{"error": "..."}），按对象取 msg 会退化成"服务端错误"
          if (json.error) {
            const detail =
              typeof json.error === 'string'
                ? json.error
                : json.error.msg || json.error.message || '服务端错误'
            throw new Error(detail)
          }
          dispatch(json)
        } catch (e) {
          if (e.name === 'AbortError' || /JSON/i.test(e.message)) continue
          onError(e)
        }
      }
    }
    // 缓冲区剩余
    if (buffer.trim()) {
      const line = buffer.trim()
      if (line.startsWith('data:')) {
        try {
          const json = JSON.parse(line.slice(5).trim())
          dispatch(json)
        } catch { /* 忽略尾部不完整事件 */ }
      }
    }

    clearTimers()
    onDone(result)
    return result
  } catch (e) {
    clearTimers()
    if (e.name === 'AbortError') {
      const byUser = abortReason === 'user' || (outerSignal && outerSignal.aborted)
      // 用户主动停止沿用 AbortError 这个名字，视图据此显示"已停止生成"而不是"请求失败"
      const err = new Error(byUser ? '已停止生成' : TIMEOUT_MSG[abortReason] || '请求超时，请重试')
      err.name = 'AbortError'
      onError(err)
      throw err
    }
    onError(e)
    throw e
  } finally {
    clearTimers()
    if (outerSignal) outerSignal.removeEventListener('abort', onOuterAbort)
  }
}
