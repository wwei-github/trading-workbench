import { useState } from 'react'

/**
 * Tab 记忆：刷新后保留刷新前的聚焦 tab（localStorage 持久化）。
 *
 * 返回 [当前 key, 切换函数]，直接接 Tabs 的 activeKey / onChange。
 * 存储 key 不在 keys 里（tab 被移除等）时回退到第一个。
 */
export function useTabMemory(storageKey: string, keys: readonly string[]) {
  const [active, setActive] = useState(() => {
    try {
      const saved = localStorage.getItem(storageKey)
      if (saved && keys.includes(saved)) return saved
    } catch { /* 隐私模式等 localStorage 不可用场景 */ }
    return keys[0]
  })
  const switchTo = (key: string) => {
    setActive(key)
    try {
      localStorage.setItem(storageKey, key)
    } catch { /* 同上，忽略持久化失败 */ }
  }
  return [active, switchTo] as const
}
