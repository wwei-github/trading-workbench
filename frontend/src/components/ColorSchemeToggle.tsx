/**
 * 涨跌配色全局切换（红涨绿跌 / 绿涨红跌）。
 * 放在页面顶部工具栏，对所有面板生效：K线实体与仓位线、涨跌/盈亏文本、
 * 胜负与多空标签（见 utils/scheme.ts 的消费方）。
 */
import { useScanStore } from '../stores/scanStore'
import type { ColorScheme } from '../stores/scanStore'

const OPTIONS: { value: ColorScheme; label: string; tint: string; dots: [string, string] }[] = [
  { value: 'red-up', label: '红涨绿跌', tint: 'rgba(239, 83, 80, 0.30)', dots: ['#ef5350', '#26a69a'] },
  { value: 'green-up', label: '绿涨红跌', tint: 'rgba(38, 166, 154, 0.30)', dots: ['#26a69a', '#ef5350'] },
]

export default function ColorSchemeToggle() {
  const colorScheme = useScanStore((s) => s.colorScheme)
  const setColorScheme = useScanStore((s) => s.setColorScheme)

  return (
    // 药丸形分段控件：选中项带涨跌语义色底色，圆点直观示意红绿顺序
    <div
      style={{
        display: 'flex',
        alignItems: 'center',
        background: '#f5f5f5',
        border: '1px solid #e5e5e5',
        borderRadius: 999,
        padding: 2,
      }}
    >
      {OPTIONS.map((opt) => {
        const active = colorScheme === opt.value
        return (
          <span
            key={opt.value}
            onClick={() => setColorScheme(opt.value)}
            style={{
              display: 'inline-flex',
              alignItems: 'center',
              gap: 4,
              fontSize: 12,
              lineHeight: '18px',
              padding: '2px 10px',
              borderRadius: 999,
              cursor: 'pointer',
              userSelect: 'none',
              transition: 'all 0.2s',
              color: active ? '#333' : '#8c8c8c',
              background: active ? opt.tint : 'transparent',
            }}
          >
            <span style={{ width: 6, height: 6, borderRadius: '50%', background: opt.dots[0] }} />
            {opt.label}
            <span style={{ width: 6, height: 6, borderRadius: '50%', background: opt.dots[1] }} />
          </span>
        )
      })}
    </div>
  )
}
