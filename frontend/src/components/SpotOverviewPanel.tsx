/**
 * 现货·持仓总览（docs/18 P0 只读）
 * - 状态条：三币当前计划一句话汇总 + 现金池
 * - 三币持仓卡：数量/市值/评分/建议动作；评分卡可展开明细（环境/仓位/择时三段）
 * - 评分实时算（后端读缓存K线），账户查询失败降级提示
 */
import { useCallback, useEffect, useMemo, useState } from 'react'
import {
  Alert,
  Card,
  Col,
  Descriptions,
  Row,
  Segmented,
  Space,
  Spin,
  Statistic,
  Tag,
  Tooltip,
  Typography,
} from 'antd'
import { ReloadOutlined } from '@ant-design/icons'
import { Button } from 'antd'
import { spotApi } from '../api/spot'
import type { SpotCoinOverview, SpotDecision, SpotOverview } from '../types/spot'
import { useScanStore } from '../stores/scanStore'
import { schemeValueColor } from '../utils/scheme'

const ACTION_META: Record<string, { text: string; color: string }> = {
  BUY: { text: '低买', color: 'green' },
  SELL: { text: '高卖', color: 'red' },
  HOLD: { text: '观望', color: 'blue' },
}

// 评分色阶：<40 灰、40~54 蓝、55~69 金、≥70 绿（docs/18 §3.3 份数映射同色感）
function scoreColor(total: number): string {
  if (total >= 70) return '#52c41a'
  if (total >= 55) return '#faad14'
  if (total >= 40) return '#1677ff'
  return '#8c8c8c'
}

function fmtPrice(p: number): string {
  return p >= 100 ? p.toLocaleString('en-US', { maximumFractionDigits: 2 }) : p.toFixed(4)
}

function fmtQty(q: number): string {
  return q.toLocaleString('en-US', { maximumFractionDigits: 6 })
}

// 评分明细段（评分卡展开区）
function ScoreDetail({ decision }: { decision: SpotDecision }) {
  const sections = [decision.score.env, decision.score.position, decision.score.timing]
  return (
    <div style={{ padding: '4px 0' }}>
      {sections.map((sec) => (
        <div key={sec.label} style={{ marginBottom: 8 }}>
          <Typography.Text strong>
            {sec.label}（{sec.score} 分）
          </Typography.Text>
          <Descriptions size="small" column={1} style={{ marginTop: 2 }}>
            {sec.items.map((it) => (
              <Descriptions.Item
                key={it.name}
                label={it.name}
                labelStyle={{ width: 150 }}
              >
                <Space size={8}>
                  <span>
                    {it.value === null || it.value === undefined
                      ? '-'
                      : typeof it.value === 'number' && Math.abs(it.value) < 1
                        ? `${(it.value * 100).toFixed(2)}%`
                        : String(it.value)}
                  </span>
                  <Typography.Text type="secondary">阈值 {it.threshold}</Typography.Text>
                  <span style={{ color: scoreColor(it.score) }}>+{it.score}</span>
                </Space>
              </Descriptions.Item>
            ))}
          </Descriptions>
        </div>
      ))}
      {decision.rules.gates.length > 0 && (
        <div>
          <Typography.Text strong>闸门</Typography.Text>
          <div>
            {decision.rules.gates.map((g) => (
              <Tag key={g} color="orange" style={{ marginTop: 4 }}>
                {g}
              </Tag>
            ))}
          </div>
        </div>
      )}
    </div>
  )
}

function CoinCard({
  coin,
  decision,
  expanded,
  onToggle,
}: {
  coin: SpotCoinOverview
  decision: SpotDecision | null
  expanded: boolean
  onToggle: () => void
}) {
  const { colorScheme } = useScanStore()
  const act = coin.action ? ACTION_META[coin.action] : null
  return (
    <Card
      size="small"
      title={
        <Space>
          <span style={{ fontWeight: 600 }}>{coin.asset}</span>
          <Typography.Text type="secondary">{fmtPrice(coin.price)}</Typography.Text>
          {coin.score && (
            <Tooltip title="三周期评分（环境30+仓位30+择时40）">
              <span style={{ color: scoreColor(coin.score.total), fontWeight: 700 }}>
                {coin.score.total} 分
              </span>
            </Tooltip>
          )}
        </Space>
      }
      extra={
        act && (
          <Space>
            <Tag color={act.color}>
              {act.text}
              {coin.action === 'BUY' && coin.lots ? ` ${coin.lots}份` : ''}
              {coin.action === 'SELL' && coin.lots ? ` ${coin.lots}份` : ''}
            </Tag>
            <Button type="link" size="small" onClick={onToggle}>
              {expanded ? '收起' : '评分明细'}
            </Button>
          </Space>
        )
      }
    >
      <Row gutter={12}>
        <Col span={8}>
          <Statistic
            title="持有数量"
            value={coin.qty === null || coin.qty === undefined ? '-' : fmtQty(coin.qty)}
          />
        </Col>
        <Col span={8}>
          <Statistic
            title="持仓市值"
            value={coin.market_value === null ? '-' : `${coin.market_value.toLocaleString('en-US')} U`}
          />
        </Col>
        <Col span={8}>
          <Statistic
            title="当前计划"
            value={coin.plan_text || '-'}
            valueStyle={{ fontSize: 13, lineHeight: '22px', color: '#595959' }}
          />
        </Col>
      </Row>
      {expanded && decision && (
        <div style={{ marginTop: 8, borderTop: '1px solid #f0f0f0', paddingTop: 8 }}>
          <ScoreDetail decision={decision} />
        </div>
      )}
      {coin.env && (
        <Typography.Text type="secondary" style={{ fontSize: 12 }}>
          月线{coin.env.monthly_bear ? '熊市' : '非熊'} · 周线{coin.env.weekly_above ? '牛熊线上方' : '牛熊线下方'} ·
          距ATH{' '}
          <span style={{ color: schemeValueColor(coin.env.ath_drawdown ?? 0, colorScheme) }}>
            {coin.env.ath_drawdown === null ? '-' : `${(coin.env.ath_drawdown * 100).toFixed(1)}%`}
          </span>{' '}
          · 数据截至 {decision?.as_of || '-'}
        </Typography.Text>
      )}
    </Card>
  )
}

export default function SpotOverviewPanel() {
  const [data, setData] = useState<SpotOverview | null>(null)
  const [loading, setLoading] = useState(false)
  const [expanded, setExpanded] = useState<string | null>(null)
  const [details, setDetails] = useState<Record<string, SpotDecision>>({})
  // 正式网账号切换查看：null=全部启用账号聚合；非空=单账号（须在启用集合内）
  const [viewKey, setViewKey] = useState<string | null>(null)

  const load = useCallback(async (key: string | null) => {
    setLoading(true)
    try {
      const ov = await spotApi.overview(key ?? undefined)
      setData(ov)
    } catch {
      // 错误信息由 axios 拦截器统一提示
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    load(viewKey)
  }, [load, viewKey])

  const toggle = async (symbol: string) => {
    if (expanded === symbol) {
      setExpanded(null)
      return
    }
    setExpanded(symbol)
    if (!details[symbol]) {
      try {
        const d = await spotApi.score(symbol)
        setDetails((prev) => ({ ...prev, [symbol]: d }))
      } catch {
        // 拉取失败保持展开区空
      }
    }
  }

  // 正式网多账号时的切换选项：全部聚合 + 各启用账号
  const accountOptions = useMemo(() => {
    const keys = data?.account_keys ?? []
    const names = data?.account_names ?? []
    return keys.map((k, i) => ({ label: names[i] || k, value: k }))
  }, [data])

  if (loading && !data) {
    return (
      <div style={{ textAlign: 'center', padding: 80 }}>
        <Spin />
      </div>
    )
  }
  if (!data) return null

  return (
    <Space direction="vertical" style={{ width: '100%' }} size={12}>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
        <Typography.Title level={5} style={{ margin: 0 }}>
          屯币总览
          <Tag color={data.testnet ? 'blue' : 'gold'} style={{ marginLeft: 8 }}>
            {data.testnet ? '现货测试网' : '现货正式网'}
          </Tag>
        </Typography.Title>
        <Space>
          {!data.testnet && accountOptions.length > 1 && (
            <Segmented<string>
              value={viewKey ?? 'all'}
              onChange={(v) => setViewKey(v === 'all' ? null : v)}
              options={[
                { label: '全部账号', value: 'all' },
                ...accountOptions,
              ]}
              size="small"
            />
          )}
          <Button icon={<ReloadOutlined />} size="small" onClick={() => load(viewKey)} loading={loading}>
            刷新
          </Button>
        </Space>
      </div>

      {data.account_error && (
        <Alert
          type="warning"
          showIcon
          message="现货账户查询失败（评分不受影响）"
          description={data.account_error}
        />
      )}

      {/* 状态条：现金池 + 单份金额（当前计划在每张币卡的「当前计划」格） */}
      <Card size="small">
        <Row gutter={16}>
          <Col span={6}>
            <Statistic
              title="现金池 (USDT)"
              value={data.usdt_free === null ? '-' : data.usdt_free.toLocaleString('en-US')}
            />
          </Col>
          <Col span={6}>
            <Statistic title="单份金额" value={`${data.settings.per_order_usdt.toLocaleString('en-US')} U`} />
          </Col>
          <Col span={6}>
            <Statistic
              title="单币投入上限"
              value={`${data.settings.max_total_usdt.toLocaleString('en-US')} U`}
            />
          </Col>
          <Col span={6}>
            <Statistic
              title="运行模式"
              value="P0 半自动"
              valueStyle={{ fontSize: 20 }}
              suffix={<Typography.Text type="secondary" style={{ fontSize: 12 }}>评分展示·不自动下单</Typography.Text>}
            />
          </Col>
        </Row>
      </Card>

      {data.coins.map((c) => (
        <CoinCard
          key={c.symbol}
          coin={c}
          decision={details[c.symbol] ?? null}
          expanded={expanded === c.symbol}
          onToggle={() => toggle(c.symbol)}
        />
      ))}
    </Space>
  )
}
