import { useEffect, useState } from 'react'
import { Popover, Typography } from 'antd'
import { scanApi } from '../api/scan'
import type { MarketRisk } from '../types'

const LEVELS = [
  { label: '市场正常', color: '#52c41a', bg: '#f6ffed', border: '#b7eb8f' },
  { label: '警戒', color: '#faad14', bg: '#fffbe6', border: '#ffe58f' },
  { label: '限制开仓', color: '#fa8c16', bg: '#fff7e6', border: '#ffd591' },
  { label: '防御模式', color: '#f5222d', bg: '#fff1f0', border: '#ffa39e' },
]

const SIGNAL_DEFS = [
  { key: 's1', name: 'BTC 24h', unit: '%', thr: '≤ -5' },
  { key: 's2', name: '广度(前20)', unit: '%', thr: '≥ 70' },
  { key: 's3', name: 'ATR 放大', unit: 'x', thr: '≥ 1.8' },
  { key: 's4', name: '3×15m 急跌', unit: '%', thr: '≤ -3.5' },
] as const

/** 市场风险横幅（docs/15 §6）：30 秒轮询，L0 单行低调展示，升级高亮 + 信号详情 */
export default function RiskBanner() {
  const [risk, setRisk] = useState<MarketRisk | null>(null)

  useEffect(() => {
    let alive = true
    const load = () => scanApi.trades.marketRisk().then((d) => { if (alive) setRisk(d) }).catch(() => {})
    load()
    const t = setInterval(load, 30_000)
    return () => { alive = false; clearInterval(t) }
  }, [])

  if (!risk) return null
  const lv = LEVELS[risk.level] ?? LEVELS[0]
  const sigText = SIGNAL_DEFS.map((d) => `${d.name} ${risk.signals[d.key] ?? '-'}${d.unit}`).join('  ')

  return (
    <Popover
      placement="bottomLeft"
      title={`市场风险 L${risk.level} · ${lv.label}`}
      content={
        <div style={{ maxWidth: 360 }}>
          <table style={{ width: '100%', fontSize: 12, marginBottom: 8 }}>
            <tbody>
              {SIGNAL_DEFS.map((d) => (
                <tr key={d.key}>
                  <td>{d.name}</td>
                  <td>{risk.signals[d.key] ?? '-'}{d.unit}</td>
                  <td style={{ color: '#999' }}>阈值 {d.thr}{d.unit}</td>
                </tr>
              ))}
            </tbody>
          </table>
          <Typography.Text type="secondary" style={{ fontSize: 12 }}>
            {risk.history.slice(0, 8).map((h) => (
              <div key={h.id}>
                {h.created_at.slice(5, 16).replace('T', ' ')} L{h.from_level}→L{h.to_level}（{h.trigger?.reason ?? ''}）
              </div>
            ))}
            {risk.history.length === 0 && <div>近 48h 无等级迁移</div>}
          </Typography.Text>
        </div>
      }
    >
      <div style={{
        display: 'inline-flex', alignItems: 'center', gap: 8,
        padding: '2px 12px', borderRadius: 4, marginBottom: 8,
        alignSelf: 'flex-start',
        background: lv.bg, border: `1px solid ${lv.border}`, color: lv.color,
        cursor: 'pointer', fontSize: 13,
      }}>
        <span>{['🟢', '🟡', '🟠', '🔴'][risk.level]}</span>
        <strong>{lv.label}</strong>
        {risk.level > 0 && <span style={{ color: '#666' }}>{sigText}</span>}
      </div>
    </Popover>
  )
}
