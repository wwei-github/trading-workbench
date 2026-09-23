/**
 * 现货·持仓总览（docs/18 P1 半自动闭环）
 * - 状态条：现金池 + 配置摘要；KPI 卡：净增币 vs 定投基准（超额）
 * - 三币持仓卡：数量/市值/评分/建议动作；评分卡可展开明细（环境/仓位/择时三段）
 * - 手动交易弹窗：第三执行入口（与飞书一键确认/周度定投共用后端风控）
 * - 评分实时算（后端读缓存K线），账户查询失败降级提示
 */
import { useCallback, useEffect, useMemo, useState } from 'react'
import {
  Alert,
  Button,
  Card,
  Col,
  Descriptions,
  InputNumber,
  message,
  Modal,
  Row,
  Segmented,
  Select,
  Space,
  Spin,
  Statistic,
  Tag,
  Tooltip,
  Typography,
} from 'antd'
import { ReloadOutlined, ThunderboltOutlined } from '@ant-design/icons'
import { spotApi } from '../api/spot'
import type { SpotCoinOverview, SpotDecision, SpotKpi, SpotOverview } from '../types/spot'
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

// 屯币 KPI 卡（docs/18 §11：净增币 vs 定投基准；KPI 是币数不是 USDT）
function KpiCard({ kpi, dcaAmount }: { kpi: Record<string, SpotKpi>; dcaAmount: number }) {
  return (
    <Card size="small" title="屯币 KPI（净增币 vs 定投基准）">
      <Row gutter={16}>
        {(['BTCUSDT', 'ETHUSDT', 'BNBUSDT'] as const).map((sym) => {
          const k = kpi[sym]
          const asset = sym.replace('USDT', '')
          const hasData = !!k && (k.bought_quote > 0 || k.sold_quote > 0)
          const excess = k?.excess_qty ?? null
          return (
            <Col span={8} key={sym}>
              <Tooltip
                title={
                  hasData
                    ? `买入合计 ${k!.bought_quote.toLocaleString('en-US')} U · 卖出合计 ${k!.sold_quote.toLocaleString('en-US')} U · 基准=首单周起每周一买入 ${dcaAmount} U`
                    : '尚无成交订单（信号确认 / 周度定投 / 手动交易后计入）'
                }
              >
                <div>
                  <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                    {asset} 净增币
                  </Typography.Text>
                  <div style={{ fontSize: 20, fontWeight: 600, lineHeight: '28px' }}>
                    {hasData
                      ? `${k!.net_qty > 0 ? '+' : ''}${fmtQty(k!.net_qty)} ${asset}`
                      : '—'}
                  </div>
                  <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                    {excess !== null && k!.benchmark_qty !== null ? (
                      <>
                        超额{' '}
                        <span style={{ color: excess >= 0 ? '#52c41a' : '#ff4d4f' }}>
                          {excess > 0 ? '+' : ''}
                          {fmtQty(excess)}
                        </span>
                        {`（基准 ${fmtQty(k!.benchmark_qty!)} · ${k!.benchmark_weeks}周×${dcaAmount}U）`}
                      </>
                    ) : hasData ? (
                      '暂无基准（缺周K缓存）'
                    ) : (
                      '暂无成交'
                    )}
                  </Typography.Text>
                </div>
              </Tooltip>
            </Col>
          )
        })}
      </Row>
    </Card>
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
  // 手动交易弹窗（P1 第三执行入口）
  const [execOpen, setExecOpen] = useState(false)
  const [execSubmitting, setExecSubmitting] = useState(false)
  const [execForm, setExecForm] = useState<{
    symbol: string
    side: 'BUY' | 'SELL'
    amount: number | null
    qty: number | null
  }>({ symbol: 'BTCUSDT', side: 'BUY', amount: null, qty: null })

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

  const openExec = () => {
    setExecForm({
      symbol: 'BTCUSDT',
      side: 'BUY',
      amount: data?.settings.per_order_usdt ?? null,
      qty: null,
    })
    setExecOpen(true)
  }

  const submitExecute = async () => {
    setExecSubmitting(true)
    try {
      const body =
        execForm.side === 'BUY'
          ? { symbol: execForm.symbol, side: 'BUY' as const, quote_amount: execForm.amount ?? undefined }
          : { symbol: execForm.symbol, side: 'SELL' as const, qty: execForm.qty ?? undefined }
      const out = await spotApi.execute(body)
      if (out.ok) {
        const detail =
          execForm.side === 'BUY'
            ? `买入 ${out.quote_spent} U → ${out.executed_qty}${execForm.symbol.replace('USDT', '')}`
            : `卖出 ${out.executed_qty}，回款 ${out.quote_spent} U`
        message.success(
          `现货下单成交：${detail}${out.risk_notes?.length ? `（${out.risk_notes.join('；')}）` : ''}`,
        )
        setExecOpen(false)
        load(viewKey)
      } else {
        message.warning(out.reason || '未执行')
      }
    } catch (e) {
      message.error((e as { response?: { data?: { detail?: string } } })?.response?.data?.detail || '下单失败')
    } finally {
      setExecSubmitting(false)
    }
  }

  const heldQty = useMemo(
    () => data?.coins.find((c) => c.symbol === execForm.symbol)?.qty ?? null,
    [data, execForm.symbol],
  )

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
          <Button type="primary" icon={<ThunderboltOutlined />} size="small" onClick={openExec}>
            手动交易
          </Button>
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
              value="P1 半自动"
              valueStyle={{ fontSize: 20 }}
              suffix={<Typography.Text type="secondary" style={{ fontSize: 12 }}>一键确认·手动执行</Typography.Text>}
            />
          </Col>
        </Row>
      </Card>

      <KpiCard kpi={data.kpi ?? {}} dcaAmount={data.settings.dca_amount} />

      {data.coins.map((c) => (
        <CoinCard
          key={c.symbol}
          coin={c}
          decision={details[c.symbol] ?? null}
          expanded={expanded === c.symbol}
          onToggle={() => toggle(c.symbol)}
        />
      ))}

      <Modal
        title="手动现货交易"
        open={execOpen}
        onCancel={() => setExecOpen(false)}
        onOk={submitExecute}
        okText="市价执行"
        confirmLoading={execSubmitting}
        okButtonProps={{
          disabled:
            execForm.side === 'BUY'
              ? !execForm.amount || execForm.amount <= 0
              : !execForm.qty || execForm.qty <= 0,
        }}
        destroyOnClose
      >
        <Space direction="vertical" style={{ width: '100%' }} size={12}>
          <Space size={12} wrap>
            <Select
              value={execForm.symbol}
              onChange={(v) => setExecForm((f) => ({ ...f, symbol: v }))}
              style={{ width: 140 }}
              options={(data?.coins ?? []).map((c) => ({
                label: `${c.asset} · ${fmtPrice(c.price)}`,
                value: c.symbol,
              }))}
            />
            <Segmented<'BUY' | 'SELL'>
              value={execForm.side}
              onChange={(v) => setExecForm((f) => ({ ...f, side: v }))}
              options={[
                { label: '买入', value: 'BUY' },
                { label: '卖出', value: 'SELL' },
              ]}
            />
          </Space>
          {execForm.side === 'BUY' ? (
            <div>
              <Typography.Text type="secondary" style={{ fontSize: 12, display: 'block', marginBottom: 4 }}>
                买入金额（USDT，≥10 U 最小名义；超出单币上限/保留垫时自动向下裁剪）
              </Typography.Text>
              <InputNumber
                value={execForm.amount}
                onChange={(v) => setExecForm((f) => ({ ...f, amount: v }))}
                min={0}
                step={10}
                style={{ width: 200 }}
                addonAfter="U"
              />
            </div>
          ) : (
            <div>
              <Typography.Text type="secondary" style={{ fontSize: 12, display: 'block', marginBottom: 4 }}>
                卖出数量（{execForm.symbol.replace('USDT', '')}
                {heldQty !== null && heldQty !== undefined ? `，持有 ${fmtQty(heldQty)}` : ''}
                ；超出波段可卖上限（free 的 30%）时自动裁剪）
              </Typography.Text>
              <InputNumber
                value={execForm.qty}
                onChange={(v) => setExecForm((f) => ({ ...f, qty: v }))}
                min={0}
                step={0.0001}
                style={{ width: 200 }}
                addonAfter={execForm.symbol.replace('USDT', '')}
              />
            </div>
          )}
          <Typography.Text type="secondary" style={{ fontSize: 12 }}>
            统一风控：单币投入上限 · USDT 保留垫 · 底仓 70% 锁定 · 单笔成交落现货订单记录
          </Typography.Text>
        </Space>
      </Modal>
    </Space>
  )
}
