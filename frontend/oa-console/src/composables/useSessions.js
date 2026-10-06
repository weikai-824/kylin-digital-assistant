import { reactive } from 'vue'

/**
 * 多会话 store（纯前端 localStorage，按知识库维度）。
 *  - 数据结构：state.byKb[kb] = { items: [session], activeId }
 *    session = { id, title, sessionId(后端会话id), createdAt, updatedAt, messages }
 *  - 续聊原理：每个会话各自持有后端 kb_chat 发的 sessionId，切回即恢复上下文
 *  - 迁移：旧版单会话 key `kylin-console-chat:<kb>` 首次访问时自动导入为第一条并删除
 *  - 容量护栏：会话数 ≤ MAX_SESSIONS，单会话消息 ≤ MAX_MESSAGES（保留最新）
 *  - 后端零改动：本 store 只管前端元数据与消息快照
 */
const MAX_SESSIONS = 100
const MAX_MESSAGES = 50

const state = reactive({ byKb: {} })
const loadedKbs = new Set()

function genId() {
  return Date.now().toString(36) + Math.random().toString(36).slice(2, 8)
}

function truncate(text, n = 20) {
  const t = (text || '').replace(/\s+/g, ' ').trim()
  return t.length > n ? t.slice(0, n) + '…' : t
}

function storageKey(kb) {
  return `kylin-console-sessions:${kb}`
}

function persist(kb) {
  const s = state.byKb[kb]
  if (!s) return
  try {
    localStorage.setItem(storageKey(kb), JSON.stringify({ items: s.items, activeId: s.activeId }))
  } catch { /* 存储满等异常：忽略，不影响当次对话 */ }
}

// 首次访问某知识库时加载 + 旧数据迁移
function loadKb(kb) {
  if (!kb) return null
  if (!loadedKbs.has(kb)) {
    loadedKbs.add(kb)
    let items = []
    let activeId = null
    try {
      const raw = localStorage.getItem(storageKey(kb))
      if (raw) {
        const parsed = JSON.parse(raw)
        if (parsed && Array.isArray(parsed.items)) {
          items = parsed.items
          activeId = parsed.activeId || null
        }
      }
    } catch { /* 缓存损坏则从空开始 */ }
    // 旧版单会话迁移：有历史消息才导入为一条会话
    if (!items.length) {
      try {
        const oldRaw = localStorage.getItem(`kylin-console-chat:${kb}`)
        if (oldRaw) {
          const old = JSON.parse(oldRaw)
          if (old && Array.isArray(old.messages) && old.messages.length) {
            const firstUser = old.messages.find((m) => m.role === 'user')
            items = [{
              id: genId(),
              title: firstUser ? truncate(firstUser.content) : '历史会话',
              sessionId: old.sessionId || null,
              createdAt: Date.now(),
              updatedAt: Date.now(),
              messages: old.messages,
            }]
            activeId = items[0].id
          }
          localStorage.removeItem(`kylin-console-chat:${kb}`)
        }
      } catch { /* 迁移失败不阻塞 */ }
    }
    state.byKb[kb] = { items, activeId }
  }
  return state.byKb[kb]
}

function moveToFront(items, id) {
  const idx = items.findIndex((i) => i.id === id)
  if (idx > 0) {
    const [item] = items.splice(idx, 1)
    items.unshift(item)
  }
}

export function useSessions() {
  return {
    state,
    /** 会话列表（已按最近更新排序：更新时移到队首） */
    list(kb) {
      return loadKb(kb) ? state.byKb[kb].items : []
    },
    /** 当前激活会话（草稿态返回 null） */
    active(kb) {
      const s = loadKb(kb)
      if (!s || !s.activeId) return null
      return s.items.find((i) => i.id === s.activeId) || null
    },
    /** 回到草稿态（新对话）：不创建条目，首条消息发出时才落会话 */
    startDraft(kb) {
      const s = loadKb(kb)
      if (!s) return
      s.activeId = null
      persist(kb)
    },
    /** 首条消息发出时创建会话，标题取首问 */
    beginSession(kb, firstQuestion) {
      const s = loadKb(kb)
      if (!s) return null
      const item = {
        id: genId(),
        title: truncate(firstQuestion),
        sessionId: null,
        createdAt: Date.now(),
        updatedAt: Date.now(),
        messages: [],
      }
      s.items.unshift(item)
      if (s.items.length > MAX_SESSIONS) s.items.length = MAX_SESSIONS
      s.activeId = item.id
      persist(kb)
      return item
    },
    /** 对话过程中回写消息快照 / 后端 sessionId */
    syncSession(kb, id, { messages, sessionId }) {
      const s = loadKb(kb)
      if (!s || !id) return
      const item = s.items.find((i) => i.id === id)
      if (!item) return
      if (Array.isArray(messages)) {
        // 深拷贝快照，切断与 ChatView 响应式对象的引用
        item.messages = JSON.parse(JSON.stringify(messages)).slice(-MAX_MESSAGES)
      }
      if (sessionId) item.sessionId = sessionId
      item.updatedAt = Date.now()
      moveToFront(s.items, id)
      persist(kb)
    },
    /** 切换激活会话，返回该会话（ChatView 负责把 messages 装载进视图） */
    setActive(kb, id) {
      const s = loadKb(kb)
      if (!s) return null
      s.activeId = id
      persist(kb)
      return s.items.find((i) => i.id === id) || null
    },
    remove(kb, id) {
      const s = loadKb(kb)
      if (!s) return
      s.items = s.items.filter((i) => i.id !== id)
      if (s.activeId === id) s.activeId = null
      persist(kb)
    },
  }
}
