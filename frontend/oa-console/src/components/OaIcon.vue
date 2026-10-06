<script setup>
// 极简内联 SVG 图标集（OA 观感，避免引入额外 icon 依赖）
const props = defineProps({
  name: { type: String, required: true },
  size: { type: [Number, String], default: 16 },
})
const paths = {
  home: 'M3 10.5 12 3l9 7.5M5 9.5V21h5v-6h4v6h5V9.5',
  process: 'M4 6h10M18 6h2M4 12h4M12 12h8M4 18h7M15 18h5M20 4v4M14 10v4M7 16v4',
  file: 'M6 2h8l6 6v14H6zM14 2v6h6',
  robot: 'M12 8V4M8 4h8M4 11h3v8a2 2 0 0 0 2 2h6a2 2 0 0 0 2-2v-8h3M7 14h2M15 14h2M9 17h6',
  chat: 'M21 11.5a8.5 8.5 0 0 1-12.4 7.5L3 21l2-5.6A8.5 8.5 0 1 1 21 11.5zM8.5 11.5h.01M12.5 11.5h.01M16.5 11.5h.01',
  grid: 'M4 4h6v6H4zM14 4h6v6h-6zM4 14h6v6H4zM14 14h6v6h-6z',
  gear: 'M12 15a3 3 0 1 0 0-6 3 3 0 0 0 0 6zm7.4-3a7.4 7.4 0 0 0-.1-1.2l2-1.6-2-3.4-2.4 1a7.6 7.6 0 0 0-2-1.2L14.5 3h-4l-.4 2.6a7.6 7.6 0 0 0-2 1.2l-2.4-1-2 3.4 2 1.6a7.4 7.4 0 0 0 0 2.4l-2 1.6 2 3.4 2.4-1a7.6 7.6 0 0 0 2 1.2l.4 2.6h4l.4-2.6a7.6 7.6 0 0 0 2-1.2l2.4 1 2-3.4-2-1.6c.06-.4.1-.8.1-1.2z',
  apps: 'M4 4h4v4H4zM10 4h4v4h-4zM16 4h4v4h-4zM4 10h4v4H4zM10 10h4v4h-4zM16 10h4v4h-4zM4 16h4v4H4zM10 16h4v4h-4zM16 16h4v4h-4z',
  search: 'M10.5 18a7.5 7.5 0 1 1 5.3-2.2L20 20m-9.5-14a5.5 5.5 0 0 1 5.5 5.5',
  bell: 'M18 8a6 6 0 1 0-12 0c0 7-3 9-3 9h18s-3-2-3-9M10.3 21a2 2 0 0 0 3.4 0',
  plus: 'M12 5v14M5 12h14',
  send: 'M22 2 11 13M22 2l-7 20-4-9-9-4z',
  refresh: 'M21 12a9 9 0 1 1-2.6-6.3M21 3v5h-5',
  spark: 'M12 3l1.9 5.1L19 10l-5.1 1.9L12 17l-1.9-5.1L5 10l5.1-1.9zM19 15l.9 2.1L22 18l-2.1.9L19 21l-.9-2.1L16 18l2.1-.9z',
  check: 'M20 6 9 17l-5-5',
  doc: 'M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8zM14 2v6h6M9 13h6M9 17h6',
  translate: 'M4 5h7M7.5 3c.5 2.5.5 5-.5 7M7 10c-1.5-1-2.8-3-3-5M15 21l-4.5-11h2l4 9 4-9h2L18 21M13 18h5',
  proof: 'M12 21s-7-4.6-7-11a5 5 0 0 1 10 0c0 6.4-3 11-3 11zM12 9a2 2 0 1 0 0-4 2 2 0 0 0 0 4z',
  write: 'M12 20h9M16.5 3.5a2.1 2.1 0 0 1 3 3L7 19l-4 1 1-4z',
  user: 'M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2M12 11a4 4 0 1 0 0-8 4 4 0 0 0 0 8z',
  panel: 'M3 4h18v16H3zM9 4v16',
  trash: 'M3 6h18M8 6V4h8v2M6 6l1 14h10l1-14M10 11v6M14 11v6',
}
</script>

<template>
  <svg
    :width="size" :height="size" viewBox="0 0 24 24" fill="none"
    stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"
    aria-hidden="true"
  >
    <path :d="paths[name] || ''" />
  </svg>
</template>
