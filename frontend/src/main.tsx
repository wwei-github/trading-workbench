import React from 'react'
import ReactDOM from 'react-dom/client'
import { ConfigProvider } from 'antd'
import zhCN from 'antd/locale/zh_CN'
import App from './App'
import 'antd/dist/reset.css'
import './index.css'

// 发版后旧页面懒加载旧 hash 的 chunk 会 404（Vite 动态 import 走 __vitePreload）。
// 浏览器已打开的页面不会自动换入口，这里拦截 Vite 的 preloadError 并刷新一次，
// 拿到新 index.html 后 hash 对齐即恢复；10s 内已重试过则放弃，避免部署未完成时无限重载。
const PRELOAD_RELOAD_KEY = 'vite-preload-reload-at'
window.addEventListener('vite:preloadError', (event) => {
  const last = Number(sessionStorage.getItem(PRELOAD_RELOAD_KEY) || 0)
  if (Date.now() - last < 10_000) return
  event.preventDefault()
  sessionStorage.setItem(PRELOAD_RELOAD_KEY, String(Date.now()))
  window.location.reload()
})

ReactDOM.createRoot(document.getElementById('root')!).render(
  <React.StrictMode>
    <ConfigProvider locale={zhCN} theme={{ token: { colorPrimary: '#1677ff' } }}>
      <App />
    </ConfigProvider>
  </React.StrictMode>,
)
