import { Fragment, useCallback, useEffect, useMemo, useRef, useState } from 'react'
import {
  Button,
  Card,
  Checkbox,
  Col,
  Empty,
  Alert,
  Modal,
  Radio,
  Row,
  Space,
  Spin,
  Statistic,
  Table,
  Tag,
  Tooltip,
  message,
} from 'antd'
import { ReloadOutlined, QuestionCircleOutlined, ThunderboltOutlined, LineChartOutlined } from '@ant-design/icons'
import type { ColumnsType } from 'antd/es/table'
import { scanApi } from '../api/scan'
import { useScanStore } from '../stores/scanStore'
import { SCHEME_UP_DOWN, schemeTag } from '../utils/scheme'
import { TRADE_TYPE_MAP } from '../constants/labels'
import type { ActualGroupStat, ActualReview, ActualTradeItem, ReviewFunnel } from '../types'
import KlineChart from './KlineChart' // 统一K线图组件（tradeId 复盘模式）
import ScopeSelector from './ScopeSelector'

// 出场原因映射（与 TradesPanel 同口径）
const EXIT_REASON_MAP: Record<string, string> = {
  sl: '止损离场',
  tp1_then_sl: 'TP1后止损',
  tp1_trail: 'TP1后跟踪止损',
  trail_sl: 'TP2后跟踪止损',
  breakeven_sl: '保本止损',
  liquidation: '强平（保证金亏光）',
  manual: '手动平仓',
  error: '异常平仓',
}

// 分组 key 中文标注：出场原因见 EXIT_REASON_MAP，其余维度按需补
// （止损锚 docs/23：structure=结构锚 / near=近锚；评分/量比分桶 key 已是中文）
const GROUP_KEY_LABELS: Record<string, string> = {
  structure: '结构锚',
  near: '近锚',
}

const fmtUsd = (v: number | null | undefined) =>
  v == null ? '-' : v.toFixed(2)

// 秒 → 人类可读（限价成交耗时）
const fmtSec = (v: number | null | undefined) =>
  v == null ? '-' : v < 60 ? `${v.toFixed(0)}s` : v < 3600 ? `${(v / 60).toFixed(1)}m` : `${(v / 3600).toFixed(1)}h`

/**
 * 实盘归因复盘（复盘重设计主轴）：trade_records 逐笔计划vs实际 +
 * 维度分组归因（开单类型/方向/出场原因/时段/错误标签）+ AI 错误归因结论。
 */
export default function ReviewActualPanel() {
  const [days, setDays] = useState<7 | 30 | 90>(30)
  const [data, setData] = useState<ActualReview | null>(null)
  // 全漏斗转化率（docs/24 A5c）：与归因统计共用 days/scope，随刷新按钮一起刷新
  const [funnel, setFunnel] = useState<ReviewFunnel | null>(null)
  const [loading, setLoading] = useState(false)
  const [attributing, setAttributing] = useState(false)
  const [replayTrade, setReplayTrade] = useState<ActualTradeItem | null>(null)
  // 账号范围（docs/17）：复盘面板默认「正式网」（2026-10-04 用户指定）——
  // 测试网是模拟撮合，成绩证明不了策略期望，归因考核锚定正式网；all 口径
  // 另含双网镜像单重复计入（顶部有提示）。面板保活，手动选择后保持
  const { aiConfig, fetchConfig } = useScanStore()
  const defaultScope = 'live'
  const lastDefaultRef = useRef<string | null>(null)
  const [scope, setScope] = useState<string>(defaultScope)
  useEffect(() => {
    if (!aiConfig) {
      fetchConfig()
      return
    }
    if (lastDefaultRef.current !== defaultScope) {
      lastDefaultRef.current = defaultScope
      setScope(defaultScope)
    }
  }, [aiConfig, fetchConfig, defaultScope])

  const colorScheme = useScanStore((s) => s.colorScheme)
  const upColor = SCHEME_UP_DOWN[colorScheme].up
  const downColor = SCHEME_UP_DOWN[colorScheme].down

  // seq 竞态守卫：scope/days 快速切换时并发多批请求，仅渲染最新一批（旧响应丢弃）
  const seqRef = useRef(0)
  const fetchData = useCallback(async (d: number, sc: string) => {
    const seq = ++seqRef.current
    setLoading(true)
    try {
      const [resp, funnelData] = await Promise.all([
        scanApi.actualReview(d, sc),
        // 漏斗失败不阻断主面板（主统计独立可用）
        scanApi.reviewFunnel(d, sc).catch(() => null),
      ])
      if (seq !== seqRef.current) return
      setData(resp)
      setFunnel(funnelData)
    } catch (e: any) {
      message.error(e?.response?.data?.detail || '获取实盘归因失败')
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    fetchData(days, scope)
  }, [days, scope, fetchData])

  // 一键归因：处理全部待归因（≤5 笔），完成后刷新
  const runAttribution = useCallback(async () => {
    setAttributing(true)
    try {
      const res = await scanApi.attributeTrades()
      const ok = res.results.filter((r) => r.ok).length
      message.success(`AI 归因完成：${ok}/${res.results.length} 笔`)
      fetchData(days, scope)
    } catch (e: any) {
      message.error(e?.response?.data?.detail || 'AI 归因失败')
    } finally {
      setAttributing(false)
    }
  }, [days, scope, fetchData])

  const pnlColor = (v: number | null | undefined) =>
    v == null || v === 0 ? undefined : v > 0 ? upColor : downColor

  // ── 分组表 ──
  // 排序状态：点排序后维度列合并自动失效（排序顺序会打乱维度分组顺序）
  const [groupSorted, setGroupSorted] = useState(false)
  // 已下线触发类型（P1-2）：仅历史行会出现，默认折叠避免表格噪音
  const [showLegacySignal, setShowLegacySignal] = useState(false)
  const visibleGroups = useMemo(
    () => (data?.groups ?? []).filter(
      (g) => showLegacySignal || !(g.dim === '信号类型' && g.legacy),
    ),
    [data, showLegacySignal],
  )
  // 维度列合并单元格：后端 groups 已按维度连续输出，同维度首行 rowSpan=组内行数，其余 0
  const dimSpans = useMemo(() => {
    const groups = visibleGroups
    if (groupSorted) return {}
    const spans: Record<string, number> = {}
    let i = 0
    while (i < groups.length) {
      let j = i + 1
      while (j < groups.length && groups[j].dim === groups[i].dim) j++
      spans[`${groups[i].dim}-${groups[i].key}`] = j - i
      for (let k = i + 1; k < j; k++) spans[`${groups[k].dim}-${groups[k].key}`] = 0
      i = j
    }
    return spans
  }, [visibleGroups, groupSorted])

  const groupColumns: ColumnsType<ActualGroupStat> = [
    {
      title: '维度', dataIndex: 'dim', width: 90,
      onCell: (g: ActualGroupStat) => ({ rowSpan: dimSpans[`${g.dim}-${g.key}`] ?? 1 }),
    },
    { title: '分组', dataIndex: 'key', render: (v: string, g: ActualGroupStat) => (
        <span>
          {GROUP_KEY_LABELS[v] || EXIT_REASON_MAP[v] || v}
          {g.legacy && <Tag style={{ marginLeft: 6 }}>历史</Tag>}
        </span>
      ) },
    {
      title: '笔数', dataIndex: 'count', width: 70,
      sorter: (a, b) => a.count - b.count,
    },
    { title: '胜', dataIndex: 'wins', width: 60, render: (v: number) => <Tag color={schemeTag(colorScheme).up}>{v}</Tag> },
    { title: '负', dataIndex: 'losses', width: 60, render: (v: number) => <Tag color={schemeTag(colorScheme).down}>{v}</Tag> },
    {
      title: (<Tooltip title="胜 = 净盈亏为正的笔数占比">胜率</Tooltip>),
      dataIndex: 'win_rate', width: 85,
      sorter: (a, b) => a.win_rate - b.win_rate,
      render: (v: number) => (
        <span style={{ color: v >= 0.5 ? upColor : downColor, fontWeight: 600 }}>
          {(v * 100).toFixed(0)}%
        </span>
      ),
    },
    {
      title: '盈亏 (USDT)', dataIndex: 'pnl', width: 110,
      sorter: (a, b) => a.pnl - b.pnl,
      render: (v: number) => <span style={{ color: pnlColor(v), fontWeight: 600 }}>{fmtUsd(v)}</span>,
    },
    {
      title: (<Tooltip title="平均盈利 / |平均亏损|，>1 表示赚的时候比亏的时候多">盈亏比</Tooltip>),
      dataIndex: 'payoff', width: 80,
      render: (v: number | null) => (v == null ? '-' : v.toFixed(2)),
    },
    {
      title: (<Tooltip title="每笔平均净盈亏（USDT）">期望值/笔</Tooltip>),
      dataIndex: 'ev_per_trade', width: 100,
      sorter: (a, b) => a.ev_per_trade - b.ev_per_trade,
      render: (v: number) => <span style={{ color: pnlColor(v) }}>{fmtUsd(v)}</span>,
    },
  ]

  // ── 明细表 ──
  const itemColumns: ColumnsType<ActualTradeItem> = [
    { title: '币种', dataIndex: 'symbol', width: 110, fixed: 'left' },
    {
      title: '方向', dataIndex: 'direction', width: 70,
      render: (v: string) => (v === 'long' ? <Tag color={schemeTag(colorScheme).up}>多</Tag> : <Tag color={schemeTag(colorScheme).down}>空</Tag>),
    },
    {
      title: '类型', dataIndex: 'trade_type', width: 100,
      render: (v: string) => TRADE_TYPE_MAP[v]?.label || v,
    },
    {
      title: '出场原因', dataIndex: 'exit_reason', width: 110,
      render: (v: string | null) =>
        v ? <Tag color={v === 'liquidation' ? 'red' : v === 'sl' ? 'volcano' : 'green'}>{EXIT_REASON_MAP[v] || v}</Tag> : '-',
    },
    {
      title: (<Tooltip title="AI 计划单笔最大亏损（planned_risk，旧行回落 risk_amount）vs 实际净盈亏">计划→实际</Tooltip>),
      key: 'plan_actual', width: 140,
      render: (_: unknown, t: ActualTradeItem) => (
        <Space size={4}>
          <span style={{ color: '#999' }}>-{fmtUsd(t.planned_risk ?? t.risk_amount)}</span>
          <span>→</span>
          <span style={{ color: pnlColor(t.realized_pnl), fontWeight: 600 }}>{fmtUsd(t.realized_pnl)}</span>
        </Space>
      ),
    },
    {
      title: (<Tooltip title="手续费 + 资金费（计划外成本）">费用</Tooltip>),
      key: 'cost', width: 110,
      render: (_: unknown, t: ActualTradeItem) => {
        if (t.fee_total == null && t.funding_total == null) return '-'
        const cost = (t.fee_total || 0) + (t.funding_total || 0)
        return <span style={{ color: '#fa8c16' }}>{fmtUsd(cost)}</span>
      },
    },
    { title: '持仓', dataIndex: 'hold_hours', width: 85, render: (v: number) => (v >= 24 ? `${(v / 24).toFixed(1)}天` : `${v}h`) },
    {
      title: '错误标签', dataIndex: 'error_tags', width: 200,
      render: (tags: string[], t: ActualTradeItem) =>
        t.attributed
          ? tags.map((tag) => <Tag key={tag} color={tag === '无明显错误' ? 'default' : 'orange'}>{tag}</Tag>)
          : <Tag color="default">待归因</Tag>,
    },
    {
      title: '操作', key: 'action', width: 90, fixed: 'right',
      render: (_: unknown, t: ActualTradeItem) => (
        <Button
          size="small" icon={<LineChartOutlined />}
          onClick={() => setReplayTrade(t)}
        >
          K线
        </Button>
      ),
    },
  ]

  const ov = data?.overview
  const pendingCount = data ? data.items.filter((t) => !t.attributed).length : 0
  // 双网镜像提示（2026-10-04）：all 口径下同一 AI 决策的双网单重复计入，
  // 统计口径偏测试网（模拟撮合），归因结论以正式网口径为准
  const mirrorN = data?.overview.mirror_trades ?? 0
  const showMirrorHint = mirrorN > 0 && (scope === 'all')

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
      {/* 顶部：窗口切换 + 归因按钮 + 刷新 */}
      <Row justify="space-between" align="middle">
        <Col>
          <Space>
            <ScopeSelector value={scope} onChange={setScope} />
            <Radio.Group
              optionType="button"
              buttonStyle="solid"
              value={days}
              onChange={(e) => setDays(e.target.value)}
              options={[
                { value: 7, label: '7天' },
                { value: 30, label: '30天' },
                { value: 90, label: '90天' },
              ]}
            />
            <Tooltip title={`对已平仓交易做 AI 错误归因（追高/止损宽/逆指标…），每轮 ≤5 笔${pendingCount ? `，当前待归因 ${pendingCount} 笔` : ''}`}>
              <Button
                type="primary" ghost size="small"
                icon={<ThunderboltOutlined />}
                loading={attributing}
                disabled={!pendingCount}
                onClick={runAttribution}
              >
                AI 归因{pendingCount ? `（${pendingCount}）` : ''}
              </Button>
            </Tooltip>
          </Space>
        </Col>
        <Col>
          <Button icon={<ReloadOutlined />} onClick={() => fetchData(days, scope)}>刷新</Button>
        </Col>
      </Row>

      <Spin spinning={loading}>
        {/* 全漏斗转化率（docs/24 A5c）：与归因统计共用 days/scope，定位"价值在哪一级流失"；
            独立于下方已平仓空态展示（信号/AI/suggest 各级可能仍有数据） */}
        {funnel && funnel.stages.length > 0 && (
          <Card
            size="small"
            title={
              <Tooltip title="信号命中 → AI 分析 → suggest → 过评委 → 开仓 → 盈利，逐级转化率定位价值在哪一级流失；百分比为相对上一级转化率">
                <span>全漏斗 <QuestionCircleOutlined style={{ color: '#999', fontSize: 12 }} /></span>
              </Tooltip>
            }
            styles={{ body: { padding: '12px 16px' } }}
          >
            <div style={{ display: 'flex', flexWrap: 'wrap', alignItems: 'center', rowGap: 6 }}>
              {funnel.stages.map((st, i) => (
                <Fragment key={st.stage}>
                  {i > 0 && <span style={{ margin: '0 10px', color: '#bbb' }}>→</span>}
                  <Tooltip title={st.note || st.stage}>
                    <span style={{ cursor: st.note ? 'help' : 'default' }}>
                      {st.stage} <b>{st.count}</b>
                      {st.conversion != null && (
                        <span style={{ color: '#999', marginLeft: 4, fontSize: 12 }}>
                          {(st.conversion * 100).toFixed(1)}%
                        </span>
                      )}
                    </span>
                  </Tooltip>
                </Fragment>
              ))}
            </div>
            {funnel.block_reasons_top.length > 0 && (
              <div style={{ marginTop: 8 }}>
                {funnel.block_reasons_top.map((b) => (
                  <Tag key={b.reason} color="orange">
                    {b.reason} ×{b.count}
                  </Tag>
                ))}
              </div>
            )}
            {/* 限价成交率（审计 7.2 主指标）：pending 未定论不计分母 */}
            {funnel.limit_fill && funnel.limit_fill.total > 0 && (() => {
              const lf = funnel.limit_fill
              const decided = lf.filled + lf.canceled + lf.expired + lf.failed
              return (
                <div style={{ marginTop: 8, fontSize: 12 }}>
                  <Tooltip title={lf.note}>
                    <span>
                      限价成交率{' '}
                      <b style={{ color: lf.fill_rate != null && lf.fill_rate < 0.5 ? downColor : upColor }}>
                        {lf.fill_rate == null ? '-' : `${(lf.fill_rate * 100).toFixed(1)}%`}
                      </b>
                      {` （成交 ${lf.filled + lf.failed} / 已定论 ${decided}，待定 ${lf.pending}）`}
                      {lf.failed > 0 && ` · 保护失败 ${lf.failed}`}
                      {lf.p50_fill_s != null && ` · 成交耗时 P50 ${fmtSec(lf.p50_fill_s)} / P90 ${fmtSec(lf.p90_fill_s)}`}
                    </span>
                  </Tooltip>
                </div>
              )
            })()}
          </Card>
        )}

        {/* 异常保护时长（审计 7.2 主指标）：独立于已平仓空态，事故本身就是读数 */}
        {data?.protection && data.protection.total > 0 && (
          <Card
            size="small"
            title={
              <Tooltip title={data.protection.note}>
                <span>异常保护 <QuestionCircleOutlined style={{ color: '#999', fontSize: 12 }} /></span>
              </Tooltip>
            }
            styles={{ body: { padding: '12px 16px' } }}
          >
            <Row gutter={24}>
              <Col span={6}>
                <Statistic title="事故数" value={data.protection.total} suffix="起" />
              </Col>
              <Col span={6}>
                <Statistic
                  title={<Tooltip title="未解除（pending/closing/awaiting_reconcile）/ 状态未知转人工复核">未解除 / 人工</Tooltip>}
                  value={`${data.protection.unresolved} / ${data.protection.manual_review}`}
                  valueStyle={{ color: data.protection.unresolved + data.protection.manual_review > 0 ? downColor : undefined }}
                />
              </Col>
              <Col span={6}>
                <Statistic title="累计封锁" value={data.protection.total_blocked_hours.toFixed(1)} suffix="h" />
              </Col>
              <Col span={6}>
                <Statistic
                  title={<Tooltip title="单次事故封锁时长 P90 / 最长；未解除计到当前时刻">P90 / 最长</Tooltip>}
                  value={`${data.protection.p90_blocked_hours ?? '-'} / ${data.protection.max_blocked_hours ?? '-'}`}
                  suffix="h"
                />
              </Col>
            </Row>
          </Card>
        )}

        {ov && ov.count > 0 ? (
          <>
            {/* 总览统计卡片 */}
            <Row gutter={[12, 12]}>
              <Col xs={12} sm={8} md={4}>
                <Card size="small"><Statistic title="已平仓" value={ov.count} /></Card>
              </Col>
              <Col xs={12} sm={8} md={4}>
                <Card size="small">
                  <Statistic
                    title="胜率"
                    value={(ov.win_rate * 100).toFixed(0)}
                    suffix="%"
                    valueStyle={{ color: ov.win_rate >= 0.5 ? upColor : downColor }}
                  />
                </Card>
              </Col>
              <Col xs={12} sm={8} md={4}>
                <Card size="small">
                  <Statistic
                    title="总盈亏 (USDT)"
                    value={fmtUsd(ov.pnl)}
                    valueStyle={{ color: pnlColor(ov.pnl), fontWeight: 600 }}
                  />
                </Card>
              </Col>
              <Col xs={12} sm={8} md={4}>
                <Card size="small">
                  <Statistic
                    title={<Tooltip title="平均盈利 / |平均亏损|">盈亏比</Tooltip>}
                    value={ov.payoff == null ? '-' : ov.payoff.toFixed(2)}
                  />
                </Card>
              </Col>
              <Col xs={12} sm={8} md={4}>
                <Card size="small">
                  <Statistic
                    title="期望值/笔"
                    value={fmtUsd(ov.ev_per_trade)}
                    valueStyle={{ color: pnlColor(ov.ev_per_trade) }}
                  />
                </Card>
              </Col>
              <Col xs={12} sm={8} md={4}>
                <Card size="small">
                  <Statistic title="平均持仓" value={ov.avg_hold_hours >= 24 ? `${(ov.avg_hold_hours / 24).toFixed(1)}天` : `${ov.avg_hold_hours}h`} />
                </Card>
              </Col>
            </Row>

            {/* 计划 vs 实际对照 */}
            <Card size="small" title="计划 vs 实际" styles={{ body: { padding: '12px 16px' } }}>
              <Row gutter={24}>
                <Col span={6}>
                  <Statistic
                    title={<Tooltip title="AI 开单时锁定的单笔风险合计（3% 预算 × 笔数口径）">计划风险合计</Tooltip>}
                    value={fmtUsd(ov.planned_risk_total)}
                    suffix="U"
                  />
                </Col>
                <Col span={6}>
                  <Statistic
                    title="实际盈亏合计"
                    value={fmtUsd(ov.pnl)}
                    suffix="U"
                    valueStyle={{ color: pnlColor(ov.pnl) }}
                  />
                </Col>
                <Col span={6}>
                  <Statistic
                    title={<Tooltip title="手续费合计（开平 taker 费用，负值）">手续费</Tooltip>}
                    value={fmtUsd(ov.total_fee)}
                    suffix="U"
                    valueStyle={{ color: '#fa8c16' }}
                  />
                </Col>
                <Col span={6}>
                  <Statistic
                    title={<Tooltip title="资金费合计（持仓期间多空支付/收取）">资金费</Tooltip>}
                    value={fmtUsd(ov.total_funding)}
                    suffix="U"
                    valueStyle={{ color: '#fa8c16' }}
                  />
                </Col>
              </Row>
              {ov.plan_gap > 0.5 && (
                <div style={{ marginTop: 8, color: '#fa8c16', fontSize: 13 }}>
                  亏超计划 {fmtUsd(ov.plan_gap)} U——主要来自手续费/资金费/滑点（4USDT 案例：费用+资金费吃掉超预算 10%）
                </div>
              )}
              {/* 最大回撤与风险超额（审计 7.2 主指标） */}
              <Row gutter={24} style={{ marginTop: 12 }}>
                <Col span={6}>
                  <Statistic
                    title={<Tooltip title="窗口内闭单累计净盈亏的峰谷差（不含浮盈/划转，跨窗口不可比）">最大回撤</Tooltip>}
                    value={fmtUsd(ov.max_drawdown)}
                    suffix="U"
                    valueStyle={{ color: ov.max_drawdown > 0 ? downColor : undefined }}
                  />
                </Col>
                <Col span={6}>
                  <Statistic
                    title={<Tooltip title="以 1R 累计权益的最大回撤；缺失 1R 的旧行不参与">最大回撤 (R)</Tooltip>}
                    value={ov.max_drawdown_r == null ? '-' : ov.max_drawdown_r.toFixed(2)}
                  />
                </Col>
                <Col span={6}>
                  <Statistic
                    title={<Tooltip title="单笔实际净亏超过计划风险预算（旧行无预算回落 1R）的笔数；超幅合计 = 实际亏损 − 预算">风险超额</Tooltip>}
                    value={ov.risk_excess_count}
                    suffix={ov.risk_excess_count > 0 ? `笔 / ${fmtUsd(ov.risk_excess_total)}U` : '笔'}
                    valueStyle={{ color: ov.risk_excess_count > 0 ? downColor : undefined }}
                  />
                </Col>
                <Col span={6}>
                  <Statistic
                    title={<Tooltip title="单笔最大亏损（R），>1 表示亏穿止损（滑点/费用/跳空）">单笔最大亏损</Tooltip>}
                    value={ov.max_loss_r == null ? '-' : `${ov.max_loss_r.toFixed(2)}R`}
                  />
                </Col>
              </Row>
            </Card>

            {/* 双网镜像提示（2026-10-04）：all 口径含同一 AI 决策的双网单，
                统计偏测试网，归因结论锚定正式网（「网络」分组可直接对比） */}
            {showMirrorHint && (
              <Alert
                type="warning"
                showIcon
                message={`当前「全部」口径含 ${mirrorN} 笔双网镜像单（同一 AI 决策在测试网/正式网各开一笔，重复计入），且样本大头来自测试网（模拟撮合）——归因结论请以「正式网」口径为准；「网络」分组可对比两网执行差异（滑点/费用）。`}
              />
            )}

            {/* 维度分组归因 */}
            <Card
              size="small"
              title={
                <Tooltip title="按维度归因：哪组亏得最多，就收紧哪组（如某开单类型全亏 → 提示词收紧该打法）">
                  <span>分组归因 <QuestionCircleOutlined style={{ color: '#999', fontSize: 12 }} /></span>
                </Tooltip>
              }
              extra={
                <Checkbox
                  checked={showLegacySignal}
                  onChange={(e) => setShowLegacySignal(e.target.checked)}
                >
                  <span style={{ fontSize: 12 }}>显示历史信号</span>
                </Checkbox>
              }
              styles={{ body: { padding: 0 } }}
            >
              <Table<ActualGroupStat>
                rowKey={(g) => `${g.dim}-${g.key}`}
                columns={groupColumns}
                dataSource={visibleGroups}
                size="small"
                scroll={{ y: 240 }}
                pagination={false}
                onChange={(_pg, _fs, sorter) => {
                  const s = Array.isArray(sorter) ? sorter[0] : sorter
                  setGroupSorted(!!s?.order)
                }}
              />
            </Card>

            {/* 逐笔明细 */}
            <Card size="small" title="逐笔明细（AI 归因结论）" styles={{ body: { padding: 0 } }}>
              <Table<ActualTradeItem>
                rowKey="id"
                columns={itemColumns}
                dataSource={data!.items}
                size="small"
                scroll={{ x: 900, y: 400 }}
                expandable={{
                  expandedRowRender: (t) =>
                    t.attributed ? (
                      <div style={{ fontSize: 13 }}>
                        <b>AI 教训：</b>{t.error_lesson || '（无）'}
                        <span style={{ color: '#999', marginLeft: 12 }}>
                          计划：入场 {t.entry_plan ?? '-'} / 止损 {t.sl_plan ?? '-'} / TP1 {t.tp1_plan ?? '-'}
                          ｜实际成交 {t.entry_actual ?? '-'}
                        </span>
                      </div>
                    ) : (
                      <span style={{ color: '#999' }}>尚未 AI 归因（点击上方「AI 归因」按钮）</span>
                    ),
                }}
              />
            </Card>
          </>
        ) : (
          <Empty description={`${days} 天内没有已平仓交易`} style={{ marginTop: 60 }} />
        )}
      </Spin>

      {/* 交易复盘K线弹窗（与交易记录页同组件） */}
      <Modal
        open={!!replayTrade}
        title={`${replayTrade?.symbol ?? ''} 交易复盘K线`}
        footer={null}
        width={1400}
        onCancel={() => setReplayTrade(null)}
        destroyOnClose
      >
        {replayTrade && (
          <div style={{ height: 700 }}>
            <KlineChart tradeId={replayTrade.id} />
          </div>
        )}
      </Modal>
    </div>
  )
}
