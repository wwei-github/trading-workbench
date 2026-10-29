/** Header 大盘环境/市场情绪条：BTC/ETH 结构/EMA 形态 + 24h 涨跌幅 + 恐贪指数。
 *
 * - 数据源 GET /api/market/overview（后端聚合，TTL 缓存 5/30min）；60s 轮询
 * - 2026-10-13：chip 主 token 改为每小时结构检测结果（上涨结构/破位⚠…）——
 *   快照缺失或 >2h 过期（任务停摆保护）回落 EMA 形态；EMA/24h/恐贪保留在 tooltip
 * - 2026-10-13-2：chip 增加 BTC/ETH 现价；tooltip 重构为逐行分块（价格/24h、
 *   结构、支撑、阻力、EMA/4h、异常、检测时间），替代原「·」拼接长行
 * - 结构/EMA 形态与涨跌色跟随全局配色方案（红涨绿跌/绿涨红跌自动翻转）；
 *   破位警示固定红（告警语义，不随方案翻转）；恐贪为固定语义色
 * - 全部数据不可用 → 灰色「大盘 --」，绝不阻塞/撑破 Header 布局
 */
import { useEffect, useState } from 'react'
import type { ReactNode } from 'react'
import { Badge, Popover, Tooltip } from 'antd'
import { marketApi } from '../api/market'
import { observationsApi } from '../api/observations'
import type { BreadthItem, MarketOverview, ObservationTask, StructureItem } from '../types'
import { useScanStore } from '../stores/scanStore'
import type { ColorScheme } from '../stores/scanStore'
import { bj } from '../utils/dayjs'
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

// tooltip/chip 共用格式化（2026-10-13-2：从组件内上移，chip 价格复用）
const pct = (v: number) => `${v > 0 ? '+' : ''}${v.toFixed(2)}%`
const price = (v: number) => v.toLocaleString('en-US', { maximumFractionDigits: 0 })

// 结构快照有效期：每 2 小时未更新视为过期（任务停摆保护，回落 EMA token）
const STRUCTURE_MAX_AGE_MS = 2 * 3600 * 1000

function structureFresh(s: StructureItem | null | undefined): s is StructureItem {
  if (!s?.updated_at) return false
  const t = new Date(s.updated_at).getTime()
  return Number.isFinite(t) && Date.now() - t <= STRUCTURE_MAX_AGE_MS
}

// 结构 token：破位 → 固定红「破位⚠」；否则 trend_label（up/down 随配色方案）。
// 破位是告警语义（结构失效）而非涨跌方向，故不随红涨绿跌方案翻转
function structureToken(s: StructureItem, scheme: ColorScheme): { text: string; color: string } {
  const warn = s.anomaly_labels.length > 0 ? '⚠' : ''
  if (s.broken) return { text: `破位${warn}`, color: '#ff4d4f' }
  const color =
    s.trend === 'up' ? SCHEME_UP_DOWN[scheme].up
      : s.trend === 'down' ? SCHEME_UP_DOWN[scheme].down
        : 'rgba(255,255,255,0.85)'
  return { text: `${s.trend_label}${warn}`, color }
}

function SymbolChip({ sym, b, s }: { sym: string; b: BreadthItem; s: StructureItem | null }) {
  const colorScheme = useScanStore((st) => st.colorScheme)
  const chg = b.change_24h_pct
  const token = s ? structureToken(s, colorScheme) : null
  return (
    <span style={{ display: 'inline-flex', alignItems: 'center', gap: 6, whiteSpace: 'nowrap' }}>
      <span style={{ color: 'rgba(255,255,255,0.65)', fontSize: 13 }}>{sym}</span>
      <span style={{ color: '#fff', fontSize: 13, fontWeight: 700 }}>
        {b.price != null ? price(b.price) : '--'}
      </span>
      {token ? (
        <span style={{ color: token.color, fontSize: 13 }}>{token.text}</span>
      ) : b.ema_label ? (
        <span style={{ color: emaStateColor(b.ema_state, colorScheme), fontSize: 13 }}>{b.ema_label}</span>
      ) : null}
      <span style={{ color: schemeValueColor(chg, colorScheme), fontSize: 13, fontWeight: 600 }}>
        {chg > 0 ? '+' : ''}{chg.toFixed(2)}%
      </span>
    </span>
  )
}

export default function MarketOverviewBar() {
  const [data, setData] = useState<MarketOverview | null>(null)
  const [failed, setFailed] = useState(false)
  // 观察期到期提醒（docs/23 §3 / T7，2026-10-29）：Header 全局红点，5 分钟刷新
  // （到期由每日 08:10 任务标 due，不是秒级事件，无需跟大盘同频 60s）
  const [obsDue, setObsDue] = useState<ObservationTask[]>([])

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

  // 到期观察项（失败静默：提醒是旁路，绝不阻塞/搅动 Header）
  useEffect(() => {
    let alive = true
    const load = () => {
      observationsApi
        .list()
        .then((d) => {
          if (alive) setObsDue(d.items.filter((i) => i.status === 'due'))
        })
        .catch(() => {})
    }
    load()
    const timer = setInterval(load, 300000)
    return () => {
      alive = false
      clearInterval(timer)
    }
  }, [])

  const breadth = data?.breadth
  const structure = data?.structure ?? null
  const fg = data?.fear_greed ?? null
  const chips = breadth
    ? (['BTCUSDT', 'ETHUSDT'] as const)
        .map((sym) => (breadth[sym] ? (
          <SymbolChip
            key={sym}
            sym={sym.replace('USDT', '')}
            b={breadth[sym]}
            s={structureFresh(structure?.[sym]) ? structure![sym] : null}
          />
        ) : null))
        .filter(Boolean)
    : []

  // 观察期到期红点（docs/23 §3 T7）：点开看名称/判据/读数，决策入口在
  // 「任务记录 → 待决策」——Header 不做跨页跳转，避免抢走当前 tab
  const obsBadge = obsDue.length > 0 ? (
    <Popover
      placement="bottom"
      title={`观察期到期 ${obsDue.length} 项 · 待决策`}
      content={(
        <div style={{ maxWidth: 340, fontSize: 12 }}>
          {obsDue.map((o) => (
            <div key={o.id} style={{ marginBottom: 6 }}>
              <div style={{ fontWeight: 600 }}>
                {o.name}
                <span style={{ color: '#8c8c8c', fontWeight: 400 }}>
                  {' '}· 到期 {bj(o.due_at).format('MM-DD')}
                </span>
              </div>
              {o.decision_rule && (
                <div style={{ color: '#595959' }}>判据：{o.decision_rule}</div>
              )}
            </div>
          ))}
          <div style={{ color: '#8c8c8c', borderTop: '1px solid #f0f0f0', paddingTop: 6 }}>
            去「任务记录 → 待决策」按判据核对并记录结论
          </div>
        </div>
      )}
    >
      <span style={{
        display: 'inline-flex', alignItems: 'center', gap: 6,
        whiteSpace: 'nowrap', cursor: 'pointer',
      }}>
        <Badge status="error" />
        <span style={{ color: '#ff7875', fontSize: 13, fontWeight: 600 }}>
          待决策 {obsDue.length}
        </span>
      </span>
    </Popover>
  ) : null

  // 全部不可用 → 降级占位（避免 Header 抖动）。红点与大盘无关，仍需展示
  if (failed || (!chips.length && !fg)) {
    return (
      <span style={{ display: 'inline-flex', alignItems: 'center', gap: 14 }}>
        <Tooltip title={failed ? '大盘数据获取失败，将自动重试' : '大盘数据暂不可用'}>
          <span style={{ color: 'rgba(255,255,255,0.35)', fontSize: 13, whiteSpace: 'nowrap' }}>
            大盘 --
          </span>
        </Tooltip>
        {obsBadge}
      </span>
    )
  }

  // tooltip 逐行分块（2026-10-13-2 重构）：每币独立块——价格/24h、结构、支撑、
  // 阻力、EMA/4h 背景、异常、检测时间；无结构快照时回落 EMA 形态行
  const symbolBlocks = (['BTCUSDT', 'ETHUSDT'] as const)
    .map((sym) => {
      const b = breadth?.[sym]
      const sf = structureFresh(structure?.[sym]) ? structure![sym] : null
      if (!b && !sf) return null
      const rows: ReactNode[] = []
      if (b) {
        rows.push(
          <div key="px">
            价格 {b.price != null ? price(b.price) : '--'}
            <span style={{ marginLeft: 10 }}>24h {pct(b.change_24h_pct)}</span>
          </div>,
        )
      }
      if (sf) {
        rows.push(
          <div key="trend">
            结构 {sf.trend_label}
            {sf.broken ? ' · 破位未收回' : sf.valid === true ? ' · 信号有效' : ''}
          </div>,
        )
        if (sf.support != null) {
          rows.push(<div key="sup">支撑 {price(sf.support)}（{pct(sf.support_dist_pct ?? 0)}）</div>)
        }
        if (sf.resistance != null) {
          rows.push(<div key="res">阻力 {price(sf.resistance)}（{pct(sf.resistance_dist_pct ?? 0)}）</div>)
        }
        const emaParts = [
          b?.ema_label ? `1h EMA ${b.ema_label}` : null,
          sf.htf_label ? `4h ${sf.htf_label}` : null,
        ].filter(Boolean)
        if (emaParts.length) rows.push(<div key="ema">{emaParts.join(' · ')}</div>)
        if (sf.anomaly_labels.length) rows.push(<div key="ano">异常 {sf.anomaly_labels.join(' / ')}</div>)
        if (sf.updated_at) rows.push(<div key="ts">检测 {bj(sf.updated_at).format('MM-DD HH:mm')}</div>)
      } else if (b?.ema_label) {
        rows.push(<div key="ema">EMA {b.ema_label}</div>)
      }
      return (
        <div key={sym} style={{ marginTop: 4 }}>
          <div style={{ fontWeight: 700 }}>{sym.replace('USDT', '')}</div>
          {rows}
        </div>
      )
    })
    .filter(Boolean)

  const tooltip = (
    <div style={{ whiteSpace: 'nowrap' }}>
      {symbolBlocks}
      {fg && <div style={{ marginTop: 4 }}>恐惧贪婪指数 {fg.value}（{FG_LABELS[fg.label] ?? fg.label}）</div>}
    </div>
  )

  return (
    // 外层再包一层：到期红点自带 Popover，不能嵌在「大盘 tooltip」里（会互相抢 hover）
    <span style={{ display: 'inline-flex', alignItems: 'center', gap: 18, whiteSpace: 'nowrap' }}>
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
      {obsBadge}
    </span>
  )
}
