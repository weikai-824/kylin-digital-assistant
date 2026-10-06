<script setup>
import { computed } from 'vue'
import OaIcon from './OaIcon.vue'
import { useSessions } from '../composables/useSessions'

/**
 * DeepSeek 式左侧会话栏。
 *  - 新对话（回草稿态）+ 历史会话列表（按 今天 / 7 天内 / 更早 分组）
 *  - 点击切会话、hover 出删除；展开/收起由父层控制（collapsed 时宽度收到 0）
 *  - 数据直接读 useSessions store，选中/删除等操作也走 store，
 *    ChatView 通过 watch activeId 感知切换，组件间无需传事件
 */
const props = defineProps({
  kbName: { type: String, default: '' },
  collapsed: { type: Boolean, default: false },
})
const emit = defineEmits(['toggle'])

const store = useSessions()

// 分组渲染：[{ label, items }]
const groups = computed(() => {
  const items = props.kbName ? store.list(props.kbName) : []
  const now = new Date()
  const startOfToday = new Date(now.getFullYear(), now.getMonth(), now.getDate()).getTime()
  const weekAgo = startOfToday - 6 * 24 * 3600 * 1000
  const buckets = { today: [], week: [], earlier: [] }
  for (const it of items) {
    if (it.updatedAt >= startOfToday) buckets.today.push(it)
    else if (it.updatedAt >= weekAgo) buckets.week.push(it)
    else buckets.earlier.push(it)
  }
  return [
    { label: '今天', items: buckets.today },
    { label: '7 天内', items: buckets.week },
    { label: '更早', items: buckets.earlier },
  ].filter((g) => g.items.length)
})

function newChat() {
  if (props.kbName) store.startDraft(props.kbName)
}

function select(id) {
  if (!props.kbName) return
  const cur = store.active(props.kbName)
  if (cur && cur.id === id) return
  store.setActive(props.kbName, id)
}

function remove(id) {
  if (!props.kbName) return
  if (window.confirm('删除该会话？删除后不可恢复。')) store.remove(props.kbName, id)
}
</script>

<template>
  <aside class="oa-sessions" :class="{ 'oa-sessions--collapsed': collapsed }">
    <div class="oa-sessions__inner">
      <div class="oa-sessions__top">
        <span class="oa-sessions__brand" title="麒麟数字人-智能问答">
          <OaIcon name="chat" :size="15" />
        </span>
        <button class="oa-sessions__iconbtn" title="收起会话栏" @click="emit('toggle')">
          <OaIcon name="panel" :size="16" />
        </button>
      </div>

      <button class="oa-sessions__new" @click="newChat">
        <OaIcon name="plus" :size="14" /> 开启新对话
      </button>

      <div class="oa-sessions__list">
        <div v-if="!groups.length" class="oa-sessions__empty">暂无历史会话</div>
        <template v-for="g in groups" :key="g.label">
          <div class="oa-sessions__group">{{ g.label }}</div>
          <div
            v-for="s in g.items"
            :key="s.id"
            class="oa-sessions__item"
            :class="{ 'oa-sessions__item--active': kbName && store.active(kbName) && store.active(kbName).id === s.id }"
            @click="select(s.id)"
          >
            <span class="oa-sessions__title" :title="s.title">{{ s.title }}</span>
            <button class="oa-sessions__del" title="删除会话" @click.stop="remove(s.id)">
              <OaIcon name="trash" :size="13" />
            </button>
          </div>
        </template>
      </div>
    </div>
  </aside>
</template>
