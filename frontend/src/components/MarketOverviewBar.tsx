/** Header 大盘环境/市场情绪条：BTC/ETH EMA 形态 + 24h 涨跌幅 + 恐贪指数。
 *
 * - 数据源 GET /api/market/overview（后端聚合，TTL 缓存 5/30min）；60s 轮询
 * - EMA 形态与涨跌色跟随全局配色方案（红涨绿跌/绿涨红跌自动翻转）；
 *   恐贪为固定语义色（恐惧红 → 中性灰 → 贪婪绿），不随方案翻转
 * - 全部数据不可用 → 灰色「大盘 --」，绝不阻塞/撑破 Header 布局
 */
import { useEffect, useState } from 'react'
import { Tooltip } from 'antd'
import { marketApi } from '../api/market'
import type { BreadthItem, MarketOverview } from '../types'
import { useScanStore } from '../stores/scanStore'
import type { ColorScheme } from '../stores/scanStore'
import { SCHEME_UP_DOWN, schemeValueColor } from '../utils/scheme'

// alternative.me 英文分档 → 中文
const FG_LABELS: Record<string, string> = {
  'Extreme Fear': '极度恐惧',
  Fear: '恐惧',
  Neutral: '中性',
  Greed: '贪婪',
  'Extreme Greed': '极度贪婪',
}

// 恐贪分档色（固定语义色）：极度恐惧 → 极度贪婪
function fgColor(v: number): string {
  if (v <= 25) return '#cf1322'
  if (v <= 45) return '#fa8c16'
  if (v <= 55) return '#8c8c8c'
  if (v <= 75) return '#52c41a'
  return '#237804'
}

// EMA 形态 → 文字色（随配色方案翻转：多头=涨色；纠缠/未知=中性白）
function emaStateColor(state: string | null, scheme: ColorScheme): string {
  if (!state) return 'rgba(255,255,255,0.85)'
  if (state.startsWith('bullish') || state === 'turning_up') return SCHEME_UP_DOWN[scheme].up
  if (state.startsWith('bearish') || state === 'turning_down') return SCHEME_UP_DOWN[scheme].down
  return 'rgba(255,255,255,0.85)'
}

function SymbolChip({ sym, b }: { sym: string; b: BreadthItem }) {
  const colorScheme = useScanStore((s) => s.colorScheme)
  const chg = b.change_24h_pct
  return (
    <span style={{ display: 'inline-flex', alignItems: 'center', gap: 6, whiteSpace: 'nowrap' }}>
      <span style={{ color: 'rgba(255,255,255,0.65)', fontSize: 13 }}>{sym}</span>
      {b.ema_label && (
        <span style={{ color: emaStateColor(b.ema_state, colorScheme), fontSize: 13 }}>{b.ema_label}</span>
      )}
      <span style={{ color: schemeValueColor(chg, colorScheme), fontSize: 13, fontWeight: 600 }}>
        {chg > 0 ? '+' : ''}{chg.toFixed(2)}%
      </span>
    </span>
  )
}

export default function MarketOverviewBar() {
  const [data, setData] = useState<MarketOverview | null>(null)
  const [failed, setFailed] = useState(false)

  useEffect(() => {
    let alive = true
    const load = () => {
      marketApi
        .overview()
        .then((d) => {
          if (alive) {
            setData(d)
            setFailed(false)
          }
        })
        .catch(() => {
          if (alive) setFailed(true)
        })
    }
    load()
    const timer = setInterval(load, 60000)
    return () => {
      alive = false
      clearInterval(timer)
    }
  }, [])

  const breadth = data?.breadth
  const fg = data?.fear_greed ?? null
  const chips = breadth
    ? (['BTCUSDT', 'ETHUSDT'] as const)
        .map((sym) => (breadth[sym] ? <SymbolChip key={sym} sym={sym.replace('USDT', '')} b={breadth[sym]} /> : null))
        .filter(Boolean)
    : []

  // 全部不可用 → 降级占位（避免 Header 抖动）
  if (failed || (!chips.length && !fg)) {
    return (
      <Tooltip title={failed ? '大盘数据获取失败，将自动重试' : '大盘数据暂不可用'}>
        <span style={{ color: 'rgba(255,255,255,0.35)', fontSize: 13, whiteSpace: 'nowrap' }}>
          大盘 --
        </span>
      </Tooltip>
    )
  }

  const pct = (v: number) => `${v > 0 ? '+' : ''}${v.toFixed(2)}%`
  const tooltip = (
    <div>
      {breadth?.BTCUSDT && <div>BTC：EMA {breadth.BTCUSDT.ema_label ?? '--'}，24h {pct(breadth.BTCUSDT.change_24h_pct)}</div>}
      {breadth?.ETHUSDT && <div>ETH：EMA {breadth.ETHUSDT.ema_label ?? '--'}，24h {pct(breadth.ETHUSDT.change_24h_pct)}</div>}
      {fg && <div>恐惧贪婪指数：{fg.value}（{FG_LABELS[fg.label] ?? fg.label}）—— 0 极度恐惧 / 100 极度贪婪</div>}
    </div>
  )

  return (
    <Tooltip title={tooltip}>
      <span style={{ display: 'inline-flex', alignItems: 'center', gap: 18, whiteSpace: 'nowrap' }}>
        {chips}
        {fg && (
          <span style={{ display: 'inline-flex', alignItems: 'center', gap: 6, whiteSpace: 'nowrap' }}>
            <span style={{ color: 'rgba(255,255,255,0.65)', fontSize: 13 }}>恐贪</span>
            <span style={{ color: fgColor(fg.value), fontSize: 13, fontWeight: 700 }}>{fg.value}</span>
            <span style={{ color: '#fff', fontSize: 13 }}>{FG_LABELS[fg.label] ?? fg.label}</span>
          </span>
        )}
      </span>
    </Tooltip>
  )
}
