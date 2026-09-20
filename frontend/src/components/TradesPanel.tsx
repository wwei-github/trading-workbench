/**
 * 交易记录页（docs/06）：自动交易生命周期与收益展示
 * - 概览条：在跑单子 / 已平仓 / 胜率 / 累计净收益（正绿负红）
 * - 列表：币种/方向/环境（测试网·正式网）/状态/开仓信息/三价现值/收益/平仓时间；排序：进行中（开仓时间倒序）→ 已平仓（平仓时间倒序）→ 失败殿后
 * - 展开行：左右双卡片——左 AI 分析结论快照（AiAnalysisCard），右 操作历史时间线
 * - 状态筛选：胶囊按钮组（进行中 = 运行中/TP1已止盈/TP2已止盈 三态聚合，带计数徽标）
 */
import { useCallback, useEffect, useMemo, useState } from 'react'
import {
  Button,
  Descriptions,
  Empty,
  Input,
  Modal,
  Popconfirm,
  Row,
  Segmented,
  Space,
  Spin,
  Table,
  Tag,
  Timeline,
  Tooltip,
  Typography,
  message,
} from 'antd'
import {
  AccountBookOutlined,
  CheckCircleOutlined,
  CloseCircleOutlined,
  DownOutlined,
  HistoryOutlined,
  MinusCircleOutlined,
  ReloadOutlined,
  RobotOutlined,
  RocketOutlined,
  StopOutlined,
  SwapOutlined,
  UpOutlined,
  WarningOutlined,
} from '@ant-design/icons'
import type { ColumnsType } from 'antd/es/table'
import { scanApi } from '../api/scan'
import { bj } from '../utils/dayjs'
import { SCHEME_UP_DOWN, schemeTag, schemeValueColor } from '../utils/scheme'
import { useScanStore, type ColorScheme } from '../stores/scanStore'
import AiAnalysisCard from './AiAnalysisCard'
import TradeReplayChart from './TradeReplayChart'
import StatCard from './StatCard'
import type { AccountSummary, FundAnalytics, TradeEvent, TradeRecord } from '../types'

// 状态标签：运行中三态 + 已平仓 + 失败
const STATUS_MAP: Record<string, { label: string; color: string }> = {
  OPENED: { label: '运行中', color: 'blue' },
  TP1_HIT: { label: 'TP1已止盈', color: 'cyan' },
  TP2_HIT: { label: 'TP2已止盈', color: 'geekblue' },
  CLOSED: { label: '已平仓', color: 'default' },
  FAILED: { label: '失败', color: 'red' },
}

// 跟进止损进行中的单子（TP2后 / 无TP2的TP1后），止损价已推过末档止盈价
// （有TP2看TP2，无TP2看TP1）→ 锁定利润已超过该档，状态改显「移动止损」
function statusMeta(rec: TradeRecord): { label: string; color: string } {
  const s = STATUS_MAP[rec.status] || { label: rec.status, color: 'default' }
  const trailing = rec.status === 'TP2_HIT' || (rec.status === 'TP1_HIT' && !rec.tp2)
  const refTp = rec.tp2 ?? rec.tp1
  if (trailing && refTp && rec.stop_loss) {
    const passed = rec.direction === 'long' ? rec.stop_loss > refTp : rec.stop_loss < refTp
    if (passed) return { ...s, label: '移动止损' }
  }
  return s
}

// 出场原因映射（资金分析页复用）
export const EXIT_REASON_MAP: Record<string, string> = {
  sl: '止损离场',
  tp1_then_sl: 'TP1后止损',
  tp1_trail: 'TP1后跟踪止损',
  trail_sl: 'TP2后跟踪止损',
  breakeven_sl: '保本止损',
  liquidation: '强平（保证金亏光）',
  manual: '手动平仓',
  error: '异常平仓',
}

// 操作历史事件元数据：中文标签 / 主题色 / 时间线圆点图标
const EVENT_META: Record<string, { label: string; color: string; icon: React.ReactNode }> = {
  OPEN: { label: '开仓', color: '#1677ff', icon: <RocketOutlined /> },
  TP1_FILL: { label: 'TP1 止盈成交', color: '#52c41a', icon: <CheckCircleOutlined /> },
  TP2_FILL: { label: 'TP2 止盈成交', color: '#52c41a', icon: <CheckCircleOutlined /> },
  SL_MOVE: { label: '止损移动', color: '#fa8c16', icon: <SwapOutlined /> },
  SL_FILL: { label: '止损成交', color: '#ff4d4f', icon: <CloseCircleOutlined /> },
  CANCEL: { label: '撤单', color: '#8c8c8c', icon: <MinusCircleOutlined /> },
  SETTLE: { label: '结算', color: '#1677ff', icon: <AccountBookOutlined /> },
  LIQUIDATION: { label: '强平', color: '#ff4d4f', icon: <WarningOutlined /> },
  ERROR: { label: '异常', color: '#ff4d4f', icon: <WarningOutlined /> },
}

// 事件详情按键的中文名
const DETAIL_KEY_MAP: Record<string, string> = {
  price: '开仓价',
  qty: '数量',
  stop_loss: '止损',
  tp1: '止盈一',
  tp2: '止盈二',
  risk_amount: '止损金额',
  entry_order_id: '开仓单ID',
  sl_order_id: '止损单ID',
  real_entry_price: '实际入场价',
  fill_price: '成交均价',
  slippage_pct: '滑点%',
  actual_risk: '实际止损风险',
  from: '原止损',
  to: '新止损',
  realized_pnl: '净盈亏',
  pnl: '本档盈亏',
  cum_pnl: '累计盈亏',
  insurance_clear: '强平清算',
  pnl_pct: '收益率',
  exit_reason: '出场原因',
  qty_tp1: 'TP1数量',
  qty_tp2: 'TP2数量',
  message: '错误信息',
}

function fmtNum(n: number | null | undefined): string {
  if (n === null || n === undefined) return '-'
  return String(n)
}

// 失败原因短文案（状态列直接展示）：已知错误码转人话，其余截断原始报错；
// 完整报错悬停查看（Tooltip），下单序列内的详细事件见展开行"操作历史"
function failReasonShort(rec: TradeRecord): string {
  const raw = rec.fail_reason || ''
  if (raw.includes('-1121')) return '交易所未上架该合约'
  return raw.length > 40 ? `${raw.slice(0, 40)}…` : raw
}

function fmtTime(s: string | null | undefined): string {
  if (!s) return '-'
  // 后端时间为 UTC naive，统一转北京时间展示
  return bj(s).format('MM-DD HH:mm:ss')
}

// 事件详情值格式化：盈亏带正负号着色、百分比补 %、出场原因转中文
// 盈亏颜色跟随全局涨跌配色方案（红涨绿跌/绿涨红跌）
function detailValue(k: string, v: unknown, scheme: ColorScheme): React.ReactNode {
  if (k === 'exit_reason') return EXIT_REASON_MAP[String(v)] || String(v)
  if (k === 'pnl_pct' || k === 'slippage_pct') return `${v}%`
  if (k === 'realized_pnl' || k === 'pnl' || k === 'cum_pnl' || k === 'insurance_clear') {
    const n = Number(v)
    if (!Number.isNaN(n)) {
      const color = n > 0 ? SCHEME_UP_DOWN[scheme].up : n < 0 ? SCHEME_UP_DOWN[scheme].down : '#595959'
      return (
        <strong style={{ color }}>
          {n > 0 ? '+' : ''}
          {n.toFixed(2)} USDT
        </strong>
      )
    }
  }
  return String(v)
}

// 事件详情 JSON → Descriptions 组件（一行两项）
function renderDetail(detail: Record<string, unknown> | null, scheme: ColorScheme) {
  if (!detail || Object.keys(detail).length === 0) return null
  return (
    <Descriptions
      size="small"
      column={2}
      colon={false}
      style={{ marginTop: 8 }}
      labelStyle={{
        fontSize: 12,
        color: '#999',
        width: 72,
        whiteSpace: 'nowrap',
        paddingInlineEnd: 8,
      }}
      contentStyle={{ fontSize: 12, color: '#333', wordBreak: 'break-all' }}
      items={Object.entries(detail).map(([k, v]) => ({
        key: k,
        label: DETAIL_KEY_MAP[k] || k,
        children: detailValue(k, v, scheme),
      }))}
    />
  )
}

// 事件卡片折叠态的关键信息摘要（一行，随事件类型取核心值）：
// 止损移动 → from→to；盈亏类 → 着色金额；开仓/成交 → 成交均价；错误 → 短文案
function eventSummary(ev: TradeEvent, scheme: ColorScheme): React.ReactNode {
  const d = (ev.detail || {}) as Record<string, unknown>
  if (d.from != null && d.to != null) {
    return (
      <span style={{ fontSize: 12, color: '#595959', fontVariantNumeric: 'tabular-nums' }}>
        {String(d.from)} → {String(d.to)}
      </span>
    )
  }
  for (const k of ['realized_pnl', 'pnl', 'insurance_clear']) {
    if (d[k] != null) {
      const n = Number(d[k])
      if (!Number.isNaN(n)) {
        return (
          <strong
            style={{
              fontSize: 12,
              color: n > 0 ? SCHEME_UP_DOWN[scheme].up : n < 0 ? SCHEME_UP_DOWN[scheme].down : '#595959',
              fontVariantNumeric: 'tabular-nums',
            }}
          >
            {n > 0 ? '+' : ''}
            {n.toFixed(2)} U
          </strong>
        )
      }
    }
  }
  if (d.fill_price != null) {
    return <span style={{ fontSize: 12, color: '#595959' }}>成交 {String(d.fill_price)}</span>
  }
  if (d.price != null) {
    return <span style={{ fontSize: 12, color: '#595959' }}>价格 {String(d.price)}</span>
  }
  if (d.message) {
    const msg = String(d.message)
    return <span style={{ fontSize: 12, color: '#ff4d4f' }}>{msg.length > 36 ? `${msg.slice(0, 36)}…` : msg}</span>
  }
  return null
}

/**
 * 交易记录页（docs/06）
 */
export default function TradesPanel() {
  const [rows, setRows] = useState<TradeRecord[]>([])
  const [eventsMap, setEventsMap] = useState<Record<string, TradeEvent[]>>({})
  const [loading, setLoading] = useState(false)
  const [statusFilter, setStatusFilter] = useState<string>('')
  const [page, setPage] = useState(1)
  const [pageSize, setPageSize] = useState(10)
  // 环境切换（实盘/测试网）：默认跟随当前交易环境（系统配置弹窗可切，2026-09-20）；
  // 配置未加载前先按测试网（与后端 TRADING_TESTNET 初始值一致），拉到配置后校正
  const { aiConfig, fetchConfig } = useScanStore()
  const defaultScope: 'live' | 'testnet' = (aiConfig?.trading_testnet ?? true) ? 'testnet' : 'live'
  const [scope, setScope] = useState<'live' | 'testnet'>('testnet')
  const [scopeInit, setScopeInit] = useState(false)
  useEffect(() => {
    if (!aiConfig) {
      fetchConfig()
      return
    }
    if (!scopeInit) {
      setScope(defaultScope)
      setScopeInit(true)
    }
  }, [aiConfig, fetchConfig, scopeInit, defaultScope])
  // 概览汇总（与资金分析同源）：closed/wins/win_rate/net_pnl/avg_win_r 来自 analytics 接口
  const [summary, setSummary] = useState<FundAnalytics['summary'] | null>(null)
  // 账户资金概览（当前环境）：总资金/未实现收益/收益率，与列表/统计并发拉
  const [acct, setAcct] = useState<AccountSummary | null>(null)

  // 列表拉全量（上限 500）供表格与状态计数；概览统计从 analytics 接口取，
  // 保证与资金分析页数字完全一致（接口全表统计，无 500 条截断差异）
  const fetchData = useCallback(async () => {
    setLoading(true)
    try {
      const [list, analytics, acctData] = await Promise.all([
        scanApi.trades.list(undefined, 500),
        scanApi.trades.analytics({ scope }),
        scanApi.trades.accountSummary().catch(() => null),
      ])
      setRows(list)
      setSummary(analytics.summary)
      // 账户概览以当前环境为准（接口按系统配置的环境返回，切环境瞬时不匹配则丢弃）
      if (acctData && acctData.available && acctData.testnet === (scope === 'testnet')) {
        setAcct(acctData)
      } else if (acctData && !acctData.available) {
        setAcct(acctData)
      }
    } catch (e: any) {
      message.error(e?.response?.data?.detail || '获取交易记录失败')
    } finally {
      setLoading(false)
    }
  }, [scope])

  // 仅初始加载 + 环境切换时重拉，不做轮询：数据源 = 每小时第 10 分结算巡检（按小时变动），
  // 手动点「刷新」按钮即可获取最新数据
  useEffect(() => {
    fetchData()
  }, [fetchData])

  // 当前环境的记录（列表/状态计数用；概览统计走 analytics 接口）
  const scopeRows = useMemo(
    () => rows.filter((r) => r.testnet === (scope === 'testnet')),
    [rows, scope],
  )

  // 全局涨跌配色方案（红涨绿跌/绿涨红跌，页面顶部切换）
  const colorScheme = useScanStore((s) => s.colorScheme)
  // 整行点击展开/收起（受控 keys；点展开图标时 stopPropagation 由 onRow 内部处理）
  const [expandedKeys, setExpandedKeys] = useState<React.Key[]>([])
  // 操作历史事件卡片展开态（key = `${rec.id}:${idx}`，默认全部折叠只显关键信息）
  const [expandedEvts, setExpandedEvts] = useState<Set<string>>(new Set())
  const toggleEvt = (key: string) => {
    setExpandedEvts((prev) => {
      const next = new Set(prev)
      if (next.has(key)) next.delete(key)
      else next.add(key)
      return next
    })
  }
  // 复盘K线弹窗（点击币种名打开）
  const [replayTrade, setReplayTrade] = useState<TradeRecord | null>(null)

  // 黑名单状态：初始拉一次生效列表，操作后局部更新（与复盘 Tab 的 BlacklistPanel 各自独立维护）
  const [blacklistSet, setBlacklistSet] = useState<Set<string>>(new Set())
  const loadBlacklist = useCallback(async () => {
    try {
      const items = await scanApi.blacklist.list()
      setBlacklistSet(new Set(items.filter((i) => i.active).map((i) => i.symbol)))
    } catch {
      /* 状态加载失败不阻断页面，按钮按"未拉黑"渲染 */
    }
  }, [])
  useEffect(() => {
    loadBlacklist()
  }, [loadBlacklist])

  // 拉黑弹窗（加入需备注原因，可留空）
  const [blTarget, setBlTarget] = useState<TradeRecord | null>(null)
  const [blReason, setBlReason] = useState('')
  const [blSaving, setBlSaving] = useState(false)

  const handleBlacklistAdd = async () => {
    if (!blTarget) return
    setBlSaving(true)
    try {
      await scanApi.blacklist.add(blTarget.symbol, blReason.trim())
      setBlacklistSet((prev) => new Set(prev).add(blTarget.symbol))
      message.success(`${blTarget.symbol} 已加入黑名单（扫描将过滤该币种）`)
      setBlTarget(null)
    } catch (e: any) {
      message.error(e?.response?.data?.detail || '加入黑名单失败')
    } finally {
      setBlSaving(false)
    }
  }

  const handleBlacklistRemove = async (symbol: string) => {
    try {
      await scanApi.blacklist.remove(symbol)
      setBlacklistSet((prev) => {
        const next = new Set(prev)
        next.delete(symbol)
        return next
      })
      message.success(`${symbol} 已移出黑名单（重新进入扫描候选）`)
    } catch (e: any) {
      message.error(e?.response?.data?.detail || '移出黑名单失败')
    }
  }


  // 筛选 + 排序：进行中（按开仓时间倒序）→ 已平仓（按平仓时间倒序）→ 失败殿后
  const filtered = useMemo(() => {
    const base =
      statusFilter === ''
        ? scopeRows
        : statusFilter === 'running'
          ? scopeRows.filter((r) => ['OPENED', 'TP1_HIT', 'TP2_HIT'].includes(r.status))
          : scopeRows.filter((r) => r.status === statusFilter)
    const rank = (r: TradeRecord) =>
      ['OPENED', 'TP1_HIT', 'TP2_HIT'].includes(r.status) ? 0 : r.status === 'CLOSED' ? 1 : 2
    // 已平仓看平仓时间，未平仓看开仓时间；同组内新→旧
    const timeOf = (r: TradeRecord) => r.closed_at ?? r.opened_at ?? ''
    return [...base].sort(
      (a, b) => rank(a) - rank(b) || (timeOf(a) < timeOf(b) ? 1 : timeOf(a) > timeOf(b) ? -1 : 0),
    )
  }, [scopeRows, statusFilter])

  // 概览统计：在跑（三态，从当前环境列表算）；其余与资金分析页同源
  // （closed/wins/win_rate/net_pnl/avg_win_r 全部取自 analytics 接口，口径见 docs/14）
  const stats = useMemo(() => {
    const running = scopeRows.filter((r) => ['OPENED', 'TP1_HIT', 'TP2_HIT'].includes(r.status)).length
    if (!summary) {
      return { running, closed: 0, wins: 0, loses: 0, winRate: null as number | null, totalPnl: 0, avgWinR: null as number | null }
    }
    const wins = summary.wins
    const closed = summary.closed
    return {
      running,
      closed,
      wins,
      loses: closed - wins,
      winRate: summary.win_rate,
      totalPnl: summary.net_pnl,
      avgWinR: summary.avg_win_r,
    }
  }, [scopeRows, summary])

  // 状态筛选选项：进行中 = 三态聚合（运行中/TP1已止盈/TP2已止盈），带当前数量与状态色点
  const statusOptions = useMemo(() => {
    const running = scopeRows.filter((r) => ['OPENED', 'TP1_HIT', 'TP2_HIT'].includes(r.status)).length
    const count = (s: string) => scopeRows.filter((r) => r.status === s).length
    return [
      { value: '', label: '全部', count: scopeRows.length, dot: null as string | null },
      { value: 'running', label: '进行中', count: running, dot: '#1677ff' },
      { value: 'CLOSED', label: '已平仓', count: count('CLOSED'), dot: '#8c8c8c' },
      { value: 'FAILED', label: '失败', count: count('FAILED'), dot: '#ff4d4f' },
    ]
  }, [scopeRows])

  // 展开行时加载该笔交易的操作历史（失败落空数组，时间线显示"暂无"，避免永远转圈）
  const loadEvents = useCallback(async (tradeId: string) => {
    try {
      const evts = await scanApi.trades.events(tradeId)
      setEventsMap((prev) => ({ ...prev, [tradeId]: evts }))
    } catch {
      setEventsMap((prev) => (prev[tradeId] ? prev : { ...prev, [tradeId]: [] }))
    }
  }, [])

  // 收益金额列（净盈亏 USDT），正负色跟随全局涨跌配色方案
  // 运行中单子显示已实现部分（TP1/TP2 已止盈金额），结算后为全程净额；
  // 百分比单独一列展示，「已止盈」标记由状态列的 TP1已止盈/TP2已止盈 Tag 表达
  const renderPnl = (rec: TradeRecord) => {
    if (rec.realized_pnl === null || rec.realized_pnl === undefined) {
      return <span style={{ color: '#999' }}>-</span>
    }
    const pnl = rec.realized_pnl
    const color = schemeValueColor(pnl, colorScheme)
    return (
      <span style={{ color, fontWeight: 600 }}>
        {pnl > 0 ? '+' : ''}{pnl.toFixed(2)}
      </span>
    )
  }

  // 操作历史时间线：彩色图标圆点 + 白色事件卡片 + 键值详情网格
  const renderTimeline = (rec: TradeRecord) => {
    const evts = eventsMap[rec.id]
    if (!evts) return <Spin size="small" />
    if (evts.length === 0)
      return (
        <div style={{ textAlign: 'center', padding: '18px 0', color: '#bfbfbf', fontSize: 12 }}>
          <HistoryOutlined style={{ fontSize: 20, display: 'block', marginBottom: 6 }} />
          暂无操作历史
          <div style={{ marginTop: 4, fontSize: 11 }}>开仓/止盈/止损/结算等事件将按时间记录在这里</div>
        </div>
      )
    return (
      <div>
        <div style={{ fontSize: 12, color: '#999', marginBottom: 10 }}>
          共 <span style={{ color: '#595959', fontWeight: 600 }}>{evts.length}</span> 条事件
        </div>
        <Timeline
          items={evts.map((ev, idx) => {
            const meta =
              EVENT_META[ev.event_type] || {
                label: ev.event_type,
                color: '#8c8c8c',
                icon: <HistoryOutlined />,
              }
            const detail = renderDetail(ev.detail, colorScheme)
            // 折叠态关键信息（有详情才可展开；无详情事件直接平铺摘要）
            const summary = eventSummary(ev, colorScheme)
            const evtKey = `${rec.id}:${idx}`
            const evtOpen = expandedEvts.has(evtKey)
            return {
              dot: (
                <span
                  style={{
                    display: 'flex',
                    alignItems: 'center',
                    justifyContent: 'center',
                    width: 22,
                    height: 22,
                    borderRadius: '50%',
                    background: meta.color,
                    color: '#fff',
                    fontSize: 11,
                    boxShadow: '0 0 0 3px #fafafa',
                  }}
                >
                  {meta.icon}
                </span>
              ),
              children: (
                <div
                  style={{
                    background: '#fff',
                    border: '1px solid #f0f0f0',
                    borderRadius: 8,
                    padding: detail ? '8px 12px' : '10px 12px',
                    marginBottom: 2,
                    cursor: detail ? 'pointer' : 'default',
                  }}
                  onClick={() => detail && toggleEvt(evtKey)}
                >
                  <div
                    style={{
                      display: 'flex',
                      alignItems: 'baseline',
                      gap: 8,
                      flexWrap: 'wrap',
                    }}
                  >
                    <span style={{ fontWeight: 600, fontSize: 13, color: meta.color }}>
                      {meta.label}
                    </span>
                    {summary && !evtOpen && (
                      <span style={{ minWidth: 0, overflow: 'hidden', textOverflow: 'ellipsis' }}>
                        {summary}
                      </span>
                    )}
                    <span
                      style={{
                        fontSize: 12,
                        color: '#999',
                        marginLeft: 'auto',
                        fontVariantNumeric: 'tabular-nums',
                        display: 'flex',
                        alignItems: 'center',
                        gap: 6,
                      }}
                    >
                      {fmtTime(ev.created_at)}
                      {detail && (
                        <span
                          style={{
                            color: '#bfbfbf',
                            fontSize: 10,
                            display: 'inline-flex',
                            alignItems: 'center',
                          }}
                        >
                          {evtOpen ? <UpOutlined /> : <DownOutlined />}
                        </span>
                      )}
                    </span>
                  </div>
                  {evtOpen && detail}
                </div>
              ),
            }
          })}
        />
      </div>
    )
  }

  const columns: ColumnsType<TradeRecord> = [
    {
      title: '币种',
      dataIndex: 'symbol',
      key: 'symbol',
      width: 150,
      render: (v: string, rec) => (
        <Space size={4}>
          {/* 实际开仓的记录可点击打开复盘K线弹窗（FAILED/未开仓不可点）；
              行点击展开逻辑会忽略 a 标签，此处 stopPropagation 双保险 */}
          {rec.opened_at ? (
            <Tooltip title="点击查看开仓前后K线复盘">
              <a onClick={(e) => { e.stopPropagation(); setReplayTrade(rec) }} style={{ fontWeight: 600, cursor: 'pointer' }}>
                {v}
              </a>
            </Tooltip>
          ) : (
            <Typography.Text strong>{v}</Typography.Text>
          )}
          <Tag color={rec.direction === 'long' ? schemeTag(colorScheme).up : schemeTag(colorScheme).down} style={{ marginInlineEnd: 0 }}>
            {rec.direction === 'long' ? '多' : '空'}
          </Tag>
        </Space>
      ),
    },
    {
      title: '环境',
      dataIndex: 'testnet',
      key: 'testnet',
      width: 80,
      render: (v: boolean) =>
        v === false ? <Tag color="green">正式网</Tag> : <Tag color="gold">测试网</Tag>,
    },
    {
      title: '评分',
      dataIndex: 'recommendation',
      key: 'recommendation',
      width: 70,
      render: (v: number | null) =>
        v != null ? <strong style={{ color: v >= 80 ? '#ff4d4f' : '#fa8c16' }}>{v}</strong> : '-',
    },
    {
      title: '状态',
      dataIndex: 'status',
      key: 'status',
      width: 110,
      render: (v: string, rec) => {
        const s = statusMeta(rec)
        // 只显当前状态标签；止损移动/出场等明细展开行「操作历史」查看。
        // 失败状态保留原因小字（悬停看完整报错）
        if (v === 'FAILED' && rec.fail_reason) {
          return (
            <Tooltip title={rec.fail_reason}>
              <div>
                <Tag color={s.color}>{s.label}</Tag>
                <div
                  style={{
                    fontSize: 11,
                    color: '#ff4d4f',
                    lineHeight: '16px',
                    marginTop: 2,
                    overflow: 'hidden',
                    textOverflow: 'ellipsis',
                    whiteSpace: 'nowrap',
                  }}
                >
                  {failReasonShort(rec)}
                </div>
              </div>
            </Tooltip>
          )
        }
        return <Tag color={s.color}>{s.label}</Tag>
      },
    },
    {
      title: '收益',
      key: 'pnl',
      width: 100,
      render: (_: unknown, rec) => renderPnl(rec),
    },
    {
      title: '收益率',
      dataIndex: 'pnl_pct',
      key: 'pnl_pct',
      width: 80,
      render: (v: number | null) => {
        if (v === null || v === undefined) return <span style={{ color: '#999' }}>-</span>
        return (
          <span style={{ color: schemeValueColor(v, colorScheme), fontWeight: 600, fontVariantNumeric: 'tabular-nums' }}>
            {v > 0 ? '+' : ''}{v}%
          </span>
        )
      },
    },
    {
      // 盈亏比（R）：净盈亏 ÷ 计划风险额（risk_amount），按结算后全程净额算；
      // 在跑单/未结算显示 -（TP1 半程利润算 R 会虚高，与概览口径一致）
      title: '盈亏比',
      key: 'r_multiple',
      width: 80,
      render: (_: unknown, rec) => {
        if (rec.status !== 'CLOSED' || !rec.risk_amount || rec.realized_pnl == null) {
          return <span style={{ color: '#999' }}>-</span>
        }
        const r = rec.realized_pnl / rec.risk_amount
        return (
          <span style={{ color: schemeValueColor(r, colorScheme), fontWeight: 600, fontVariantNumeric: 'tabular-nums' }}>
            {r > 0 ? '+' : ''}{r.toFixed(2)}R
          </span>
        )
      },
    },
    {
      title: '出场原因',
      dataIndex: 'exit_reason',
      key: 'exit_reason',
      width: 110,
      render: (v: string | null) => (v ? EXIT_REASON_MAP[v] || v : '-'),
    },
    {
      title: '开仓价',
      dataIndex: 'entry_price',
      key: 'entry_price',
      width: 100,
      render: fmtNum,
    },
    {
      title: '止损（现值）',
      dataIndex: 'stop_loss',
      key: 'stop_loss',
      width: 110,
      render: fmtNum,
    },
    {
      title: 'TP1',
      dataIndex: 'tp1',
      key: 'tp1',
      width: 90,
      render: fmtNum,
    },
    {
      title: 'TP2',
      dataIndex: 'tp2',
      key: 'tp2',
      width: 90,
      render: (v: number | null) => (v ? fmtNum(v) : '-'),
    },
    {
      title: '数量',
      dataIndex: 'qty',
      key: 'qty',
      width: 90,
      render: fmtNum,
    },
    {
      title: '保证金',
      dataIndex: 'margin_used',
      key: 'margin_used',
      width: 90,
      render: (v: number | null) => (v !== null && v !== undefined ? v.toFixed(2) : '-'),
    },
    {
      title: '开仓时间',
      dataIndex: 'opened_at',
      key: 'opened_at',
      width: 150,
      render: fmtTime,
    },
    {
      title: '平仓时间',
      dataIndex: 'closed_at',
      key: 'closed_at',
      width: 150,
      render: fmtTime,
    },
    {
      title: '黑名单',
      key: 'blacklist',
      width: 76,
      fixed: 'right',
      render: (_: unknown, rec) => {
        const inBl = blacklistSet.has(rec.symbol)
        return inBl ? (
          <Popconfirm
            title="移出黑名单？"
            description="该币种将重新进入扫描候选池"
            okText="移出"
            cancelText="取消"
            onConfirm={() => handleBlacklistRemove(rec.symbol)}
          >
            <Tooltip title="已在黑名单中，点击移出">
              <Button size="small" type="link" danger>
                移出
              </Button>
            </Tooltip>
          </Popconfirm>
        ) : (
          <Tooltip title="加入黑名单后，扫描将过滤该币种">
            <Button
              size="small"
              type="link"
              icon={<StopOutlined />}
              onClick={(e) => {
                e.stopPropagation()
                setBlTarget(rec)
                setBlReason('')
              }}
            >
              拉黑
            </Button>
          </Tooltip>
        )
      },
    },
  ]

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
      <Row justify="space-between" align="middle">
        {/* 状态筛选：胶囊按钮组（选中=蓝底白字，带状态色点与计数徽标） */}
        <Space size={6} wrap>
          {statusOptions.map((opt) => {
            const active = statusFilter === opt.value
            return (
              <Tag.CheckableTag
                key={opt.value}
                checked={active}
                onChange={() => {
                  setStatusFilter(opt.value)
                  setPage(1)
                }}
                style={{
                  borderRadius: 999,
                  padding: '3px 12px',
                  fontSize: 13,
                  display: 'inline-flex',
                  alignItems: 'center',
                  gap: 6,
                  background: active ? undefined : '#fff',
                  border: `1px solid ${active ? 'transparent' : '#e5e5e5'}`,
                  boxShadow: active ? '0 1px 4px rgba(22,119,255,0.30)' : 'none',
                }}
              >
                {opt.dot && (
                  <span
                    style={{
                      width: 6,
                      height: 6,
                      borderRadius: '50%',
                      flexShrink: 0,
                      background: active ? '#fff' : opt.dot,
                    }}
                  />
                )}
                {opt.label}
                <span
                  style={{
                    fontSize: 11,
                    lineHeight: '16px',
                    padding: '0 6px',
                    borderRadius: 999,
                    background: active ? 'rgba(255,255,255,0.25)' : '#f5f5f5',
                    color: active ? '#fff' : '#8c8c8c',
                  }}
                >
                  {opt.count}
                </span>
              </Tag.CheckableTag>
            )
          })}
        </Space>
        <Space size={12}>
          {/* 环境切换：默认实盘，与资金分析页 scope 口径一致 */}
          <Segmented
            size="small"
            value={scope}
            onChange={(v) => {
              setScope(v as 'live' | 'testnet')
              setPage(1)
            }}
            options={[
              { label: '实盘', value: 'live' },
              { label: '测试网', value: 'testnet' },
            ]}
          />
          <Button icon={<ReloadOutlined />} size="small" onClick={fetchData}>
            刷新
          </Button>
        </Space>
      </Row>

      {/* 概览条：左=账户资金（当前环境），右=交易统计（与资金分析页同源同数） */}
      <div style={{ display: 'flex', gap: 12, flexWrap: 'wrap', alignItems: 'stretch' }}>
        <StatCard
          title="总资金（当前环境）"
          value={acct?.available ? `${(acct.margin_balance ?? 0).toLocaleString('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}` : '--'}
          color="#1677ff"
        />
        <StatCard
          title="未实现收益"
          value={
            acct?.available
              ? `${(acct.unrealized ?? 0) > 0 ? '+' : ''}${(acct.unrealized ?? 0).toFixed(2)} USDT`
              : '--'
          }
          color={acct?.available && (acct.unrealized ?? 0) !== 0 ? schemeValueColor(acct.unrealized ?? 0, colorScheme) : undefined}
        />
        <StatCard
          title="收益率"
          value={
            acct?.available && acct.profit_rate !== null && acct.profit_rate !== undefined
              ? `${acct.profit_rate > 0 ? '+' : ''}${acct.profit_rate.toFixed(2)}%`
              : '--'
          }
          color={
            acct?.available && acct.profit_rate !== null && acct.profit_rate !== undefined
              ? schemeValueColor(acct.profit_rate, colorScheme)
              : undefined
          }
        />
        {/* 资金列与交易统计列之间的分隔 */}
        <div style={{ width: 1, background: '#e8e8e8', margin: '4px 2px' }} />
        <StatCard title="在跑单子" value={String(stats.running)} color="#1677ff" />
        <StatCard
          title="盈利单"
          value={String(stats.wins)}
          color={stats.wins > 0 ? SCHEME_UP_DOWN[colorScheme].up : undefined}
        />
        <StatCard
          title="亏损单"
          value={String(stats.loses)}
          color={stats.loses > 0 ? SCHEME_UP_DOWN[colorScheme].down : undefined}
        />
        <StatCard
          title="胜率（净盈利笔数占比）"
          value={stats.winRate === null ? '-' : `${stats.winRate}%`}
          color={stats.winRate === null ? undefined : stats.winRate >= 50 ? SCHEME_UP_DOWN[colorScheme].up : SCHEME_UP_DOWN[colorScheme].down}
        />
        <StatCard
          title="累计净收益"
          value={`${stats.totalPnl > 0 ? '+' : ''}${stats.totalPnl.toFixed(2)} USDT`}
          color={stats.totalPnl === 0 ? undefined : schemeValueColor(stats.totalPnl, colorScheme)}
        />
        <StatCard
          title="平均盈亏比（止盈单）"
          value={stats.avgWinR === null ? '-' : `${stats.avgWinR > 0 ? '+' : ''}${stats.avgWinR.toFixed(2)}R`}
          color={stats.avgWinR === null ? undefined : schemeValueColor(stats.avgWinR, colorScheme)}
        />
      </div>
      {/* 资金口径说明（一行小字，避免误解） */}
      <div style={{ fontSize: 12, color: '#999', marginTop: -4 }}>
        总资金 = 钱包余额 + 未实现收益；收益率 = 总收益（已实现+未实现）÷ 本金（总资金−总收益）
      </div>

      <Spin spinning={loading}>
        {scopeRows.length === 0 && !loading ? (
          <Empty
            description={scope === 'live' ? '暂无实盘交易记录（TRADING_ENABLED 开启后自动开单）' : '暂无测试网交易记录'}
            style={{ marginTop: 60 }}
          />
        ) : (
          <Table<TradeRecord>
            rowKey="id"
            columns={columns}
            dataSource={filtered}
            size="small"
            scroll={{ x: 'max-content', y: 480 }}
            onRow={(rec) => ({
              onClick: (e) => {
                // 点展开图标/按钮/链接时不重复触发（图标点击本身会冒泡到这里）
                if ((e.target as HTMLElement).closest('button, a')) return
                // antd 的 onExpand 只在点展开图标时触发，整行点击展开带不出
                // loadEvents（历史 bug：时间线永远转圈）——展开前在此补拉
                if (!expandedKeys.includes(rec.id) && !eventsMap[rec.id]) {
                  loadEvents(rec.id)
                }
                setExpandedKeys((prev) =>
                  prev.includes(rec.id) ? prev.filter((k) => k !== rec.id) : [...prev, rec.id],
                )
              },
              style: { cursor: 'pointer' },
            })}
            pagination={{
              current: page,
              pageSize,
              total: filtered.length,
              showSizeChanger: true,
              pageSizeOptions: [10, 20, 50],
              showTotal: (t) => `共 ${t} 条`,
              position: ['bottomRight'],
              onChange: (p, ps) => {
                setPage(ps !== pageSize ? 1 : p)
                setPageSize(ps)
              },
            }}
            expandable={{
              expandedRowKeys: expandedKeys,
              onExpandedRowsChange: (keys) => setExpandedKeys([...keys]),
              expandedRowRender: (rec) => (
                <div style={{ display: 'flex', gap: 12, alignItems: 'flex-start', flexWrap: 'wrap' }}>
                  {/* 左：AI 分析结论快照（较宽） */}
                  <div
                    style={{
                      flex: '1.5 1 420px',
                      minWidth: 0,
                      padding: '12px 14px',
                      borderRadius: 8,
                      background: '#fafafa',
                      border: '1px solid #f0f0f0',
                    }}
                  >
                    <div
                      style={{
                        display: 'flex',
                        alignItems: 'center',
                        gap: 6,
                        marginBottom: 10,
                        fontWeight: 600,
                        fontSize: 13,
                        color: '#595959',
                      }}
                    >
                      <RobotOutlined style={{ color: '#1677ff' }} />
                      AI 分析结论（开单时快照）
                    </div>
                    {rec.ai_snapshot ? (
                      <AiAnalysisCard ai={rec.ai_snapshot} />
                    ) : (
                      <Typography.Text type="secondary">
                        该记录早于快照功能，未存档 AI 分析结论
                      </Typography.Text>
                    )}
                  </div>
                  {/* 右：操作历史时间线（较窄） */}
                  <div
                    style={{
                      flex: '1 1 280px',
                      minWidth: 260,
                      maxWidth: 440,
                      padding: '12px 14px',
                      borderRadius: 8,
                      background: '#fafafa',
                      border: '1px solid #f0f0f0',
                    }}
                  >
                    <div
                      style={{
                        display: 'flex',
                        alignItems: 'center',
                        gap: 6,
                        marginBottom: 10,
                        fontWeight: 600,
                        fontSize: 13,
                        color: '#595959',
                      }}
                    >
                      <HistoryOutlined style={{ color: '#fa8c16' }} />
                      操作历史
                    </div>
                    {renderTimeline(rec)}
                  </div>
                </div>
              ),
              onExpand: (expanded, rec) => {
                if (expanded && !eventsMap[rec.id]) loadEvents(rec.id)
              },
            }}
          />
        )}
      </Spin>

      {/* 复盘K线弹窗：开单前100根 + 持仓全程，初始止损止盈区域 + 关键事件标注 */}
      <Modal
        open={!!replayTrade}
        onCancel={() => setReplayTrade(null)}
        width={1080}
        footer={null}
        destroyOnClose
        title={
          replayTrade && (
            <Space size={6}>
              <span style={{ fontWeight: 600 }}>{replayTrade.symbol}</span>
              <Tag color={replayTrade.direction === 'long' ? schemeTag(colorScheme).up : schemeTag(colorScheme).down} style={{ marginInlineEnd: 0 }}>
                {replayTrade.direction === 'long' ? '多' : '空'}
              </Tag>
              <Tag color={statusMeta(replayTrade).color} style={{ marginInlineEnd: 0 }}>
                {statusMeta(replayTrade).label}
              </Tag>
              <span style={{ fontSize: 12, color: '#999', fontWeight: 400 }}>
                K线复盘 · {bj(replayTrade.opened_at).format('MM-DD HH:mm')} 开仓
                {replayTrade.closed_at ? ` · ${bj(replayTrade.closed_at).format('MM-DD HH:mm')} 平仓` : ' · 持仓中'}
              </span>
            </Space>
          )
        }
      >
        {replayTrade && (
          <div style={{ height: 560 }}>
            <TradeReplayChart tradeId={replayTrade.id} />
          </div>
        )}
      </Modal>

      {/* 拉黑弹窗：备注原因可留空（永久生效，复盘 Tab 可手动移除） */}
      <Modal
        title={blTarget ? `加入黑名单：${blTarget.symbol}` : '加入黑名单'}
        open={!!blTarget}
        onOk={handleBlacklistAdd}
        confirmLoading={blSaving}
        onCancel={() => setBlTarget(null)}
        okText="确认拉黑"
        cancelText="取消"
        width={420}
      >
        <div style={{ fontSize: 12, color: '#999', marginBottom: 8 }}>
          手动拉黑为永久生效，扫描候选池将过滤该币种；可在复盘 Tab 的「币种黑名单」中移除。
        </div>
        <Input.TextArea
          value={blReason}
          onChange={(e) => setBlReason(e.target.value)}
          placeholder="拉黑原因（可选），如：滑点过大 / 反复止损 / 妖币"
          rows={2}
          maxLength={100}
          showCount
        />
      </Modal>
    </div>
  )
}
