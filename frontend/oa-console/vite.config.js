import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

const appBase = process.env.VITE_APP_BASE || '/'

// 本地开发服务：OA 前端跑 :8502，/chat /knowledge_base /server 代理到后端 :7861，
// 规避跨域与 api_token 鉴权（与 Streamlit WebUI 同机 loopback 免鉴权一致）。
// 代理目标可用环境变量 VITE_PROXY_TARGET 覆盖（容器内注入 http://app:7861）。
const proxyTarget = process.env.VITE_PROXY_TARGET || 'http://localhost:7861'
const gatewayProxyTarget = process.env.VITE_GATEWAY_PROXY_TARGET || 'http://127.0.0.1:48080'

export default defineConfig({
  plugins: [vue()],
  base: appBase,
  server: {
    host: '0.0.0.0',
    port: 8502,
    strictPort: true,
    proxy: {
      // 让直接访问 :8502 测试时也能调用 Java Gateway 的统一认证和业务 API。
      '/prod-api': {
        target: gatewayProxyTarget,
        changeOrigin: true,
      },
      '/chat': {
        target: proxyTarget,
        changeOrigin: true,
      },
      '/knowledge_base': {
        target: proxyTarget,
        changeOrigin: true,
      },
      '/server': {
        target: proxyTarget,
        changeOrigin: true,
      },
      '/auth': {
        target: proxyTarget,
        changeOrigin: true,
      },
    },
  },
})
