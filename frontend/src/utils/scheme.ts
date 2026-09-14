/**
 * 涨跌配色方案工具：红涨绿跌（国内习惯，默认）/ 绿涨红跌（国际习惯）
 *
 * 全局方案存于 scanStore.colorScheme（localStorage 持久化），此处提供：
 * - SCHEME_UP_DOWN：方案 → 涨/跌色值（K线、涨跌文本、盈亏文本）
 * - schemeTag：方案 → antd Tag 色名（胜/负、多/空等语义标签用）
 * 全局切换入口在页面顶部（ScanResult 头部），对所有面板生效。
 */
import type { ColorScheme } from '../stores/scanStore'

export const SCHEME_UP_DOWN: Record<ColorScheme, { up: string; down: string }> = {
  'red-up': { up: '#ef5350', down: '#26a69a' },
  'green-up': { up: '#26a69a', down: '#ef5350' },
}

// antd Tag 色名版（保持既有浅色 Tag 风格，仅随方案翻转红绿）
export function schemeTag(scheme: ColorScheme): { up: 'red' | 'green'; down: 'red' | 'green' } {
  return scheme === 'red-up' ? { up: 'red', down: 'green' } : { up: 'green', down: 'red' }
}

// 盈亏/涨跌值 → 色值：正=涨色、负=跌色、零=灰
export function schemeValueColor(v: number, scheme: ColorScheme): string {
  if (v > 0) return SCHEME_UP_DOWN[scheme].up
  if (v < 0) return SCHEME_UP_DOWN[scheme].down
  return '#595959'
}
