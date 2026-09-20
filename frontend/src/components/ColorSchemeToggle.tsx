/**
 * 涨跌配色全局切换（红涨绿跌 / 绿涨红跌）。
 * 放在页面顶部工具栏，对所有面板生效：K线实体与仓位线、涨跌/盈亏文本、
 * 胜负与多空标签（见 utils/scheme.ts 的消费方）。
 * 紧凑药丸样式（2026-09-20）：只显示当前方案（涨/跌两个色点 + 短文案），
 * 点击即切换到另一方案，悬停 tooltip 预览切换后的效果。
 */
import { Tooltip } from 'antd'
import { useScanStore } from '../stores/scanStore'
import type { ColorScheme } from '../stores/scanStore'

const SCHEMES: Record<ColorScheme, { label: string; short: string; up: string; down: string; tint: string }> = {
  'red-up': { label: '红涨绿跌', short: '红涨', up: '#ef5350', down: '#26a69a', tint: 'rgba(239, 83, 80, 0.28)' },
  'green-up': { label: '绿涨红跌', short: '绿涨', up: '#26a69a', down: '#ef5350', tint: 'rgba(38, 166, 154, 0.28)' },
}

export default function ColorSchemeToggle() {
  const colorScheme = useScanStore((s) => s.colorScheme)
  const setColorScheme = useScanStore((s) => s.setColorScheme)

  const cur = SCHEMES[colorScheme]
  const next = SCHEMES[colorScheme === 'red-up' ? 'green-up' : 'red-up']

  return (
    <Tooltip title={`当前：${cur.label}（涨${cur.short === '红涨' ? '红' : '绿'}跌${cur.short === '红涨' ? '绿' : '红'}）。点击切换为「${next.label}」`}>
      <span
        onClick={() => setColorScheme(colorScheme === 'red-up' ? 'green-up' : 'red-up')}
        style={{
          display: 'inline-flex',
          alignItems: 'center',
          gap: 6,
          fontSize: 12,
          lineHeight: '22px',
          padding: '2px 10px',
          borderRadius: 999,
          border: '1px solid #e5e5e5',
          background: cur.tint,
          cursor: 'pointer',
          userSelect: 'none',
          transition: 'all 0.2s',
          color: '#333',
        }}
      >
        {/* 左点=涨色，右点=跌色，随方案互换 */}
        <span style={{ width: 8, height: 8, borderRadius: '50%', background: cur.up }} />
        {cur.short}
        <span style={{ width: 8, height: 8, borderRadius: '50%', background: cur.down }} />
      </span>
    </Tooltip>
  )
}
