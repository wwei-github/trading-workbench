/**
 * 交易记录页（docs/06）：自动交易生命周期与收益展示
 * - 概览条：两组面板（账户资金 3 项 / 交易统计 7 项，含已结算收益），紧凑网格不分卡
 * - 列表：币种/方向/环境（测试网·正式网）/状态/开仓信息/三价现值/收益/收益率；排序：进行中（开仓时间倒序）→ 已平仓（平仓时间倒序）→ 失败殿后
 * - 收益列：运行中 = 已实现+浮动合计（每小时 :10 巡检刷新，docs/16），悬浮拆分明细
 * - AI 建议列：浮亏在跑单的 AI 持仓诊断（逻辑是否破坏/建议动作），手动可重诊（docs/16）
 * - 展开行：左右双卡片——左 AI 分析结论快照（AiAnalysisCard），右 操作历史时间线
 * - 状态筛选：胶囊按钮组（进行中 = 运行中/TP1已止盈/TP2已止盈 三态聚合，带计数徽标）
 */
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import {
  Button,
  Descriptions,
  Empty,
  Input,
  Modal,
  Popconfirm,
  Row,
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
  DeleteOutlined,
  DownOutlined,
  HistoryOutlined,
  ImportOutlined,
  MinusCircleOutlined,
  ReloadOutlined,
  RobotOutlined,
  RocketOutlined,
  StopOutlined,
  SwapOutlined,
  ThunderboltOutlined,
  UpOutlined,
  WarningOutlined,
} from '@ant-design/icons'
import type { ColumnsType } from 'antd/es/table'
import { scanApi } from '../api/scan'
import { SIGNAL_TYPE_MAP, patternStyle } from '../constants/labels'
import { bj } from '../utils/dayjs'
import { SCHEME_UP_DOWN, schemeTag, schemeValueColor } from '../utils/scheme'
import { useScanStore, type ColorScheme } from '../stores/scanStore'
import AiAnalysisCard from './AiAnalysisCard'
import TradeReplayChart from './TradeReplayChart'
import ScopeSelector, { defaultScopeOf, scopeIsTestnet } from './ScopeSelector'
import type { AccountSummary, FundAnalytics, PurgeStats, TradeEvent, TradeRecord } from '../types'
import { ADVICE_ACTION_META } from '../types'

// 状态标签：运行中三态 + 已平仓 + 失败
const STATUS_MAP: Record<string, { label: string; color: string }> = {
  OPENED: { label: '运行中', color: 'blue' },
  TP1_HIT: { label: 'TP1已止盈', color: 'cyan' },
  TP2_HIT: { label: 'TP2已止盈', color: 'geekblue' },
  CLOSED: { label: '已平仓', color: 'default' },
  FAILED: { label: '失败', color: 'red' },
}

// 导入持仓弹窗：交易所持仓数据直接展示，SL/TP1/TP2 取币安现挂条件单（不可编辑）

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
  AI_ADVICE: { label: 'AI 持仓诊断', color: '#722ed1', icon: <RobotOutlined /> },
  MANUAL_CLOSE: { label: '市价平仓', color: '#fa541c', icon: <CloseCircleOutlined /> },
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
  // AI 持仓诊断（docs/16）
  thesis_valid: '开仓逻辑',
  trend_broken: '走势走坏',
  action: '建议动作',
  reason: '理由',
  detail: '分析',
  total_pnl: '全程盈亏',
}

function fmtNum(n: number | null | undefined): string {
  if (n === null || n === undefined) return '-'
  return String(n)
}

// 概览面板指标项：unit=金额单位小字
interface OverviewMetric {
  label: string
  value: string
  unit?: string
  color?: string
}

// 概览面板分组：accent=顶部彩条/圆点颜色
interface OverviewGroup {
  accent: string
  title: string
  items: OverviewMetric[]
}

// 带符号金额（Tooltip 明细用）：+12.00 / -3.70
function fmtSigned(n: number): string {
  return `${n > 0 ? '+' : ''}${n.toFixed(2)}`
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
  if (k === 'thesis_valid' || k === 'trend_broken') return v ? '是' : '否'
  if (k === 'action') return ADVICE_ACTION_META[String(v)]?.label || String(v)
  if (
    k === 'realized_pnl' || k === 'pnl' || k === 'cum_pnl'
    || k === 'insurance_clear' || k === 'total_pnl'
  ) {
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
// AI_ADVICE 事件隐藏 reason/detail（理由与分析只在左侧持仓诊断卡片完整展示）
function renderDetail(
  detail: Record<string, unknown> | null,
  scheme: ColorScheme,
  eventType?: string,
) {
  if (!detail || Object.keys(detail).length === 0) return null
  const entries = Object.entries(detail).filter(
    ([k]) => !(eventType === 'AI_ADVICE' && (k === 'reason' || k === 'detail')),
  )
  if (entries.length === 0) return null
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
      items={entries.map(([k, v]) => ({
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
  // AI 持仓诊断：收起态直接展示建议动作 Tag（详情展开可见完整理由）
  if (ev.event_type === 'AI_ADVICE' && d.action != null) {
    const meta = ADVICE_ACTION_META[String(d.action)]
    return (
      <Tag
        color={meta?.color || 'default'}
        style={{ marginInlineEnd: 0, fontSize: 11, lineHeight: '16px', padding: '0 6px' }}
      >
        {meta?.label || String(d.action)}
      </Tag>
    )
  }
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
  // 账号范围切换（docs/17）：默认跟随当前交易环境（多 live 账号跟随选中账号）。
  // 面板被 Tabs 保活：环境切换（defaultScope 变化）后重新跟随，手动选择不被配置刷新打断
  const { aiConfig, fetchConfig } = useScanStore()
  const defaultScope = defaultScopeOf(aiConfig)
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
  // 概览汇总（与资金分析同源）：closed/wins/win_rate/net_pnl/avg_win_r 来自 analytics 接口
  const [summary, setSummary] = useState<FundAnalytics['summary'] | null>(null)
  // 账户资金概览（当前环境）：总资金/未实现收益/收益率，与列表/统计并发拉
  const [acct, setAcct] = useState<AccountSummary | null>(null)

  // 列表按账号范围服务端过滤（上限 500）供表格与状态计数；概览统计从 analytics 接口取，
  // 保证与资金分析页数字完全一致（接口全表统计，无 500 条截断差异）
  // seq 竞态守卫：scope 切换会并发两批请求（初始环境批 + 校正批），后返回的旧响应
  // 会覆盖新数据（表现为筛选器显示正式网、数据却是测试网），仅渲染最新一批
  const seqRef = useRef(0)
  const fetchData = useCallback(async () => {
    const seq = ++seqRef.current
    setLoading(true)
    try {
      const [list, analytics, acctData] = await Promise.all([
        scanApi.trades.list(undefined, 500, scope),
        scanApi.trades.analytics({ scope }),
        scanApi.trades.accountSummary().catch(() => null),
      ])
      if (seq !== seqRef.current) return
      setRows(list)
      setSummary(analytics.summary)
      // 账户概览为当前交易账号实时数据：scope 是环境聚合时按环境匹配，
      // scope 是具体账号 key 时按账号精确匹配，不匹配则丢弃（切范围瞬时不串数）
      const acctMatch =
        acctData && acctData.available
          ? scope === 'live' || scope === 'testnet'
            ? acctData.testnet === (scope === 'testnet')
            : acctData.account_key === scope
          : false
      if (acctMatch) {
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

  // 仅初始加载 + 范围切换时重拉，不做轮询：数据源 = 每小时第 10 分结算巡检（按小时变动），
  // 手动点「刷新」按钮即可获取最新数据
  useEffect(() => {
    fetchData()
  }, [fetchData])

  // 当前范围的记录（服务端已按 scope 过滤；概览统计走 analytics 接口）
  const scopeRows = rows

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

  // 手动 AI 持仓诊断（docs/16）：提交异步任务立即返回，随后轮询列表对比
  // ai_advice_at 等结果（每 8s 一次，上限 ~100s）；期间按钮 loading 防重复提交
  const [advisingId, setAdvisingId] = useState<string | null>(null)
  const handleAdvice = async (rec: TradeRecord) => {
    setAdvisingId(rec.id)
    try {
      await scanApi.trades.advice(rec.id)
      message.success(`${rec.symbol} 已提交诊断，后台执行中`)
      const baseAt = rec.ai_advice_at
      for (let i = 0; i < 12; i++) {
        await new Promise((r) => setTimeout(r, 8000))
        try {
          const list = await scanApi.trades.list(undefined, 500)
          const cur = list.find((x) => x.id === rec.id)
          if (!cur || !['OPENED', 'TP1_HIT', 'TP2_HIT'].includes(cur.status)) break // 已平仓，诊断任务会自动跳过
          if (cur.ai_advice_at !== baseAt) {
            await fetchData()
            message.success(`${rec.symbol} 诊断完成`)
            return
          }
        } catch {
          // 网络抖动继续下一轮
        }
      }
      await fetchData()
      message.info(`${rec.symbol} 诊断仍在后台执行，稍后刷新查看结果`)
    } catch (e: any) {
      message.error(e?.response?.data?.detail || '提交诊断失败')
    } finally {
      setAdvisingId(null)
    }
  }

  // 市价平仓（2026-09-25，与飞书卡片按钮同源）：二次确认 → 同步执行（约 2~5s）
  // → 结果提示并刷新。失败分支同样刷新——后端可能实际已平（响应超时/巡检自愈中）
  const [closingId, setClosingId] = useState<string | null>(null)
  const handleCloseMarket = (rec: TradeRecord) => {
    Modal.confirm({
      title: `市价平仓 ${rec.symbol}？`,
      content: '将以市价立即全平该仓位并撤销全部挂单，按成交结果结算盈亏，不可撤销。',
      okText: '市价平仓',
      okButtonProps: { danger: true },
      cancelText: '取消',
      onOk: async () => {
        setClosingId(rec.id)
        try {
          const res = await scanApi.trades.closeMarket(rec.id)
          message.success(
            `市价平仓成功，净盈亏 ${res.pnl >= 0 ? '+' : ''}${res.pnl.toFixed(2)} USDT` +
            (res.pnl_pct != null ? `（收益率 ${res.pnl_pct}%）` : ''),
          )
        } catch (e: any) {
          message.error(e?.response?.data?.detail || '市价平仓失败，请稍后重试')
        } finally {
          setClosingId(null)
          await fetchData()
        }
      },
    })
  }

  // 一键清空测试网数据（2026-09-20，仅测试网 scope 显示）：先预检拿将删数量
  // → 弹窗二次确认（含在跑单警告）→ 执行删除并刷新。实盘数据与复盘统计不受影响
  const [purgeInfo, setPurgeInfo] = useState<PurgeStats | null>(null)
  const [purging, setPurging] = useState(false)
  const openPurge = async () => {
    try {
      setPurgeInfo(await scanApi.trades.purgeTestnet(false))
    } catch (e: any) {
      message.error(e?.response?.data?.detail || '预检失败，请重试')
    }
  }
  const handlePurge = async () => {
    setPurging(true)
    try {
      await scanApi.trades.purgeTestnet(true)
      message.success('测试网数据已清空')
      setPurgeInfo(null)
      await fetchData()
    } catch (e: any) {
      message.error(e?.response?.data?.detail || '清空失败，请重试')
    } finally {
      setPurging(false)
    }
  }

  // 导入持仓已自动化（2026-09-24）：每小时结算巡检自动检测币安 App 手动开仓
  // 并接管进系统（SL/TP 取 App 条件单原值），成功后飞书推送，无需手动操作；
  // 「导入持仓」按钮可不等整点立即触发一次同样的导入
  const [adoptingNow, setAdoptingNow] = useState(false)
  const handleAdoptNow = async () => {
    setAdoptingNow(true)
    try {
      const resp = await scanApi.trades.adoptNow()
      if (resp.count > 0) {
        message.success(`已导入 ${resp.count} 个持仓：${resp.adopted.join('、')}`)
        await fetchData()
      } else {
        message.info('无可导入持仓（交易所持仓均已接管，或手动单未同时设置止损+止盈条件单）')
      }
    } catch (e: any) {
      message.error(e?.response?.data?.detail || '导入失败')
    } finally {
      setAdoptingNow(false)
    }
  }

  // 「立即巡检」按钮（2026-09-26）：派发 celery 巡检任务（结算+自动导入+AI 诊断），
  // 后台执行、HTTP 立即返回；延迟刷新 2 次覆盖巡检约 10~30s 的执行窗口
  const [inspecting, setInspecting] = useState(false)
  const handleInspectNow = async () => {
    setInspecting(true)
    try {
      await scanApi.trades.inspectNow()
      message.info('巡检已派发后台执行（结算/导入/AI 诊断），稍后自动刷新')
      const delay = (ms: number) => new Promise((r) => setTimeout(r, ms))
      await delay(20000)
      await fetchData()
      await delay(15000)
      await fetchData()
    } catch (e: any) {
      message.error(e?.response?.data?.detail || '巡检派发失败')
    } finally {
      setInspecting(false)
    }
  }

  // AI 建议详情弹窗：点击列表「AI 建议」列打开，展示完整诊断（动作/逻辑/走势/理由）
  const [adviceView, setAdviceView] = useState<TradeRecord | null>(null)

  // 收益金额列（净盈亏 USDT），正负色跟随全局涨跌配色方案
  // 运行中 = 已实现 + 浮动合计（docs/16，每小时 :10 巡检刷新），悬浮拆分明细；
  // 结算后为全程净额；百分比单独一列展示，「已止盈」标记由状态列的 TP1/TP2 Tag 表达
  const renderPnl = (rec: TradeRecord) => {
    const hasRealized = rec.realized_pnl !== null && rec.realized_pnl !== undefined
    const hasUnreal =
      rec.status !== 'CLOSED' && rec.unrealized_pnl !== null && rec.unrealized_pnl !== undefined
    if (!hasRealized && !hasUnreal) return <span style={{ color: '#999' }}>-</span>
    const realized = hasRealized ? (rec.realized_pnl ?? 0) : 0
    const unreal = hasUnreal ? (rec.unrealized_pnl ?? 0) : 0
    const total = realized + unreal
    const main = (
      <span style={{ color: schemeValueColor(total, colorScheme), fontWeight: 600 }}>
        {total > 0 ? '+' : ''}
        {total.toFixed(2)}
      </span>
    )
    // 在跑单一律悬浮拆分（含浮盈为 0 的时刻，口径统一：显示更新时刻的总收益）
    if (!hasRealized || !hasUnreal) return main
    return (
      <Tooltip title={<span>已结算 {fmtSigned(realized)} · 浮盈/亏 {fmtSigned(unreal)} USDT</span>}>
        {main}
      </Tooltip>
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
            const detail = renderDetail(ev.detail, colorScheme, ev.event_type)
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
      // 持仓时间列：已平仓 = 平仓-开仓；运行中 = 现在-开仓（随刷新更新）。
      // 仅显示小时（如 4h，<1h 显示 <1h）；超 24h 红色预警（2026-09-26）。
      // 后端存 UTC-naive：必须 bj()（dayjs.utc 解析）取正确 epoch，否则
      // new Date() 按本地时区解析会把运行中时长虚增 8h（2026-09-26 修复）
      title: '持仓时间',
      key: 'holding_time',
      width: 80,
      render: (_: unknown, rec: TradeRecord) => {
        if (!rec.opened_at) return '-'
        const start = bj(rec.opened_at).valueOf()
        const end = rec.closed_at ? bj(rec.closed_at).valueOf() : Date.now()
        const h = Math.max(0, (end - start) / 3_600_000)
        const text = h < 1 ? '<1h' : h < 24 ? `${Math.round(h)}h` : `${Math.floor(h / 24)}d${Math.round(h % 24)}h`
        return (
          <Tooltip title={rec.closed_at
            ? `${bj(rec.opened_at).format('MM-DD HH:mm')} 开仓 · ${bj(rec.closed_at).format('MM-DD HH:mm')} 平仓`
            : `${bj(rec.opened_at).format('MM-DD HH:mm')} 开仓 · 持仓中`}>
            <span style={{ fontVariantNumeric: 'tabular-nums', color: h > 24 ? '#ff4d4f' : undefined }}>
              {text}
            </span>
          </Tooltip>
        )
      },
    },
    {
      // 信号类型列（2026-09-25）：开单时触发器类型，与扫描结果页标签同源；导入持仓/旧行显示 -
      title: '信号类型',
      dataIndex: 'signal_type',
      key: 'signal_type',
      width: 110,
      render: (v: string | null | undefined) => {
        if (!v) return '-'
        const cfg = SIGNAL_TYPE_MAP[v]
        return <Tag color={cfg?.color ?? 'default'}>{cfg?.label ?? v}</Tag>
      },
    },
    {
      // K线形态列（2026-09-25）：开单时触发形态，与扫描结果页标签同源；突破类/导入持仓/旧行显示 -
      title: 'K线形态',
      dataIndex: 'pattern',
      key: 'pattern',
      width: 100,
      render: (v: string | null | undefined) => {
        if (!v) return '-'
        const p = patternStyle(v)
        return <Tag color={p.color}>{p.label}</Tag>
      },
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
      // 4h方向列：开单时 4h 背景级别（震荡/纠缠/弱·强多空），与扫描结果「4h方向」同源
      title: (
        <Tooltip title="开单时 4h 高周期背景（ADX门控 + EMA21/55 排列与间距分级）：ADX<20=震荡不投票，EMA 间距≥1%=强、0.1%~1%=弱；悬停查看完整判定细节">
          <span>4h方向</span>
        </Tooltip>
      ),
      key: 'htf_direction',
      width: 88,
      render: (_: unknown, rec) => {
        const htf = rec.htf
        if (!htf) return <span style={{ color: '#999' }}>-</span>
        const isBull = htf.state === 'bullish'
        const isBear = htf.state === 'bearish'
        const short = htf.state === 'ranging'
          ? '震荡'
          : htf.state === 'mixed'
            ? '纠缠'
            : `${htf.label?.startsWith('强') ? '强' : '弱'}${isBull ? '多' : '空'}`
        const color = isBull
          ? schemeTag(colorScheme).up
          : isBear
            ? schemeTag(colorScheme).down
            : 'default'
        return (
          <Tooltip title={htf.label || undefined}>
            <Tag color={color} style={{ marginInlineEnd: 0 }}>{short}</Tag>
          </Tooltip>
        )
      },
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
      // AI 持仓诊断（docs/16）：仅展示建议动作 Tag，理由等详情点击弹窗查看
      title: 'AI 建议',
      key: 'ai_advice',
      width: 110,
      render: (_: unknown, rec) => {
        const adv = rec.ai_advice
        if (!adv) return <span style={{ color: '#999' }}>-</span>
        const meta = ADVICE_ACTION_META[adv.action] || ADVICE_ACTION_META.hold
        return (
          <div style={{ display: 'flex', alignItems: 'center' }} onClick={(e) => e.stopPropagation()}>
            <Tag
              color={meta.color}
              onClick={() => setAdviceView(rec)}
              title="点击查看诊断详情"
              style={{ marginInlineEnd: 0, cursor: 'pointer' }}
            >
              {meta.label}
            </Tag>
          </div>
        )
      },
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
      title: 'TP1',
      dataIndex: 'tp1',
      key: 'tp1',
      width: 90,
      render: fmtNum,
    },
    {
      // TP1预估收益 = TP1 分批数量 × (TP1价 − 开仓价)（多空各按方向取正负）。
      // 数量优先用系统真实分批 qty_tp1（有 TP2 平 50%、无 TP2 平 75%），
      // 旧记录 raw 无该键时回退比例近似（75%，比全数量更贴近无 TP2 场景）
      title: (
        <Tooltip title="预估收益 = TP1分批数量 × (TP1价 − 开仓价)；有TP2平50%、无TP2平75%（旧记录无分批数量按75%近似）">
          <span>TP1预估</span>
        </Tooltip>
      ),
      key: 'tp1_est',
      width: 100,
      render: (_: unknown, rec: TradeRecord) => {
        if (rec.qty == null || rec.tp1 == null || rec.entry_price == null) return '-'
        const q1 = rec.qty_tp1 ?? rec.qty * (rec.tp2 ? 0.5 : 0.75)
        const dir = rec.direction === 'long' ? 1 : -1
        const est = q1 * (rec.tp1 - rec.entry_price) * dir
        return (
          <span style={{ color: schemeValueColor(est, colorScheme), fontVariantNumeric: 'tabular-nums' }}>
            {est >= 0 ? '+' : ''}{est.toFixed(2)}
          </span>
        )
      },
    },
    {
      title: 'TP2',
      dataIndex: 'tp2',
      key: 'tp2',
      width: 90,
      render: (v: number | null) => (v ? fmtNum(v) : '-'),
    },
    {
      // TP2预估收益 = TP2 分批数量（开仓量 25%）× (TP2价 − 开仓价)。
      // 数量优先用系统真实分批 qty_tp2，旧记录回退 25% 近似；无 TP2 不计算
      title: (
        <Tooltip title="预估收益 = TP2分批数量（开仓量25%）× (TP2价 − 开仓价)；无 TP2 显示 -">
          <span>TP2预估</span>
        </Tooltip>
      ),
      key: 'tp2_est',
      width: 100,
      render: (_: unknown, rec: TradeRecord) => {
        // 无 TP2（null 或旧记录存 0）不计算预估，与 TP2 价格列显示 '-' 同口径
        if (rec.qty == null || !rec.tp2 || rec.entry_price == null) return '-'
        const q2 = rec.qty_tp2 ?? rec.qty * 0.25
        const dir = rec.direction === 'long' ? 1 : -1
        const est = q2 * (rec.tp2 - rec.entry_price) * dir
        return (
          <span style={{ color: schemeValueColor(est, colorScheme), fontVariantNumeric: 'tabular-nums' }}>
            {est >= 0 ? '+' : ''}{est.toFixed(2)}
          </span>
        )
      },
    },
    {
      title: '止损（现值）',
      dataIndex: 'stop_loss',
      key: 'stop_loss',
      width: 110,
      render: fmtNum,
    },
    {
      // 止损预估亏损：数量 × (止损价 − 开仓价)，运行中止损现值随移动更新
      title: (
        <Tooltip title="预估收益 = 数量 × (止损现价 − 开仓价)；移动止损（保本/跟进）后该值随之更新">
          <span>止损预估</span>
        </Tooltip>
      ),
      key: 'sl_est',
      width: 100,
      render: (_: unknown, rec: TradeRecord) => {
        if (rec.qty == null || rec.stop_loss == null || rec.entry_price == null) return '-'
        const dir = rec.direction === 'long' ? 1 : -1
        const est = rec.qty * (rec.stop_loss - rec.entry_price) * dir
        return (
          <span style={{ color: schemeValueColor(est, colorScheme), fontVariantNumeric: 'tabular-nums' }}>
            {est >= 0 ? '+' : ''}{est.toFixed(2)}
          </span>
        )
      },
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
      title: '操作',
      key: 'blacklist',
      width: 160,
      fixed: 'right',
      render: (_: unknown, rec) => {
        const inBl = blacklistSet.has(rec.symbol)
        const running = ['OPENED', 'TP1_HIT', 'TP2_HIT'].includes(rec.status)
        return (
          <Space size={0} onClick={(e) => e.stopPropagation()}>
            {running && (
              <>
                <Button
                  size="small"
                  type="link"
                  loading={advisingId === rec.id}
                  onClick={() => handleAdvice(rec)}
                >
                  重诊
                </Button>
                <Button
                  size="small"
                  type="link"
                  danger
                  loading={closingId === rec.id}
                  onClick={() => handleCloseMarket(rec)}
                >
                  市价平仓
                </Button>
              </>
            )}
            {inBl ? (
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
                  onClick={() => {
                    setBlTarget(rec)
                    setBlReason('')
                  }}
                >
                  拉黑
                </Button>
              </Tooltip>
            )}
          </Space>
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
          {/* 账号范围切换（docs/17）：单 live 账号=环境 Segmented，多账号=账号下拉 */}
          <ScopeSelector
            value={scope}
            onChange={(v) => {
              setScope(v)
              setPage(1)
            }}
          />
          {/* 一键清空测试网数据：仅测试网范围可见，预检→弹窗确认→执行 */}
          {scopeIsTestnet(scope) && (
            <Button danger icon={<DeleteOutlined />} loading={purging} onClick={openPurge}>
              清空测试网数据
            </Button>
          )}
          <Tooltip title="立即执行一次结算巡检（结算平仓/自动导入/AI 诊断），与定时巡检同源，后台执行">
            <Button icon={<ThunderboltOutlined />} loading={inspecting} onClick={handleInspectNow}>
              立即巡检
            </Button>
          </Tooltip>
          <Tooltip title="立即检测币安 App 手动开仓并接管进系统（与每小时巡检逻辑一致；需同时设置止损+止盈条件单）">
            <Button icon={<ImportOutlined />} loading={adoptingNow} onClick={handleAdoptNow}>
              导入持仓
            </Button>
          </Tooltip>
          <Button icon={<ReloadOutlined />} onClick={fetchData}>
            刷新
          </Button>
        </Space>
      </Row>

      {/* 概览条：两组面板（账户资金 5 项 / 交易统计 5 项），彩条标题 + 网格布局 */}
      <div style={{ display: 'flex', gap: 12, flexWrap: 'wrap', alignItems: 'stretch' }}>
        {/*
         * 面板样式：顶部 3px 彩条（资金=蓝 / 统计=紫）+ 白底浅阴影圆角；
         * 指标 = 小字灰 label + 大字 tabular-nums 数值，金额带 USDT 单位小字；
         * 「今日」两项淡蓝底高亮，聚焦当日表现
         */}
        {([
          {
            accent: '#1677ff',
            title: '账户资金',
            items: [
              {
                label: '保证金余额',
                value: acct?.available ? (acct.margin_balance ?? 0).toLocaleString('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 }) : '--',
                unit: 'USDT',
                color: '#1677ff',
              },
              {
                label: '钱包余额',
                value: acct?.available ? (acct.wallet ?? 0).toLocaleString('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 }) : '--',
                unit: 'USDT',
              },
              {
                label: '未实现盈亏',
                value: acct?.available ? `${(acct.unrealized ?? 0) > 0 ? '+' : ''}${(acct.unrealized ?? 0).toFixed(2)}` : '--',
                unit: 'USDT',
                color: acct?.available && (acct.unrealized ?? 0) !== 0 ? schemeValueColor(acct.unrealized ?? 0, colorScheme) : undefined,
              },
              {
                label: '今日已实现盈亏',
                value: acct?.available ? `${(acct.today_realized ?? 0) > 0 ? '+' : ''}${(acct.today_realized ?? 0).toFixed(2)}` : '--',
                unit: 'USDT',
                color: acct?.available && (acct.today_realized ?? 0) !== 0 ? schemeValueColor(acct.today_realized ?? 0, colorScheme) : undefined,
              },
              {
                label: '今日盈亏',
                value:
                  acct?.available && acct.today_pnl_pct !== null && acct.today_pnl_pct !== undefined
                    ? `${acct.today_pnl_pct > 0 ? '+' : ''}${acct.today_pnl_pct.toFixed(2)}%`
                    : '--',
                color:
                  acct?.available && acct.today_pnl_pct !== null && acct.today_pnl_pct !== undefined && acct.today_pnl_pct !== 0
                    ? schemeValueColor(acct.today_pnl_pct, colorScheme)
                    : undefined,
              },
            ],
          },
          {
            accent: '#722ed1',
            title: '交易统计',
            items: [
              { label: '运行中', value: String(stats.running), color: '#1677ff' },
              { label: '盈利单', value: String(stats.wins), color: stats.wins > 0 ? SCHEME_UP_DOWN[colorScheme].up : undefined },
              { label: '亏损单', value: String(stats.loses), color: stats.loses > 0 ? SCHEME_UP_DOWN[colorScheme].down : undefined },
              {
                label: '胜率',
                value: stats.winRate === null ? '-' : `${stats.winRate}%`,
                color: stats.winRate === null ? undefined : stats.winRate >= 50 ? SCHEME_UP_DOWN[colorScheme].up : SCHEME_UP_DOWN[colorScheme].down,
              },
              {
                label: '平均盈亏比',
                value: stats.avgWinR === null ? '-' : `${stats.avgWinR > 0 ? '+' : ''}${stats.avgWinR.toFixed(2)}R`,
                color: stats.avgWinR === null ? undefined : schemeValueColor(stats.avgWinR, colorScheme),
              },
            ],
          },
        ] as OverviewGroup[]).map((group) => (
          <div
            key={group.title}
            style={{
              background: '#fff',
              border: '1px solid #f0f0f0',
              borderTop: `3px solid ${group.accent}`,
              borderRadius: 10,
              boxShadow: '0 1px 3px rgba(0,0,0,0.04)',
              padding: '10px 16px 12px',
              flex: 1,
              minWidth: 460,
            }}
          >
            <div style={{ display: 'flex', alignItems: 'center', gap: 6, marginBottom: 8 }}>
              <span style={{ width: 6, height: 6, borderRadius: '50%', background: group.accent }} />
              <span style={{ fontSize: 13, fontWeight: 600, color: 'rgba(0,0,0,0.88)' }}>{group.title}</span>
            </div>
            <div style={{ display: 'grid', gridTemplateColumns: 'repeat(5, minmax(84px, 1fr))', gap: '4px 12px' }}>
              {group.items.map((it) => (
                <div key={it.label}>
                  <div style={{ fontSize: 12, color: '#8c8c8c', whiteSpace: 'nowrap' }}>{it.label}</div>
                  <div
                    style={{
                      fontSize: 19,
                      fontWeight: 700,
                      color: it.color || 'rgba(0,0,0,0.88)',
                      lineHeight: 1.35,
                      fontVariantNumeric: 'tabular-nums',
                      whiteSpace: 'nowrap',
                    }}
                  >
                    {it.value}
                    {it.unit && (
                      <span style={{ fontSize: 11, fontWeight: 400, color: '#8c8c8c', marginLeft: 3 }}>{it.unit}</span>
                    )}
                  </div>
                </div>
              ))}
            </div>
          </div>
        ))}
      </div>
      {/* 口径说明（一行小字，避免误解） */}
      <div style={{ fontSize: 12, color: '#999', marginTop: -4 }}>
        保证金余额 = 钱包余额 + 未实现盈亏；今日口径按北京时间自然日（闭单时刻归属当日）；
        今日盈亏 = （今日已实现 + 全部未实现）÷ 期初钱包（当前钱包 − 今日已实现，忽略日内划转）；
        胜率 = 净盈利笔数占比；平均盈亏比仅计止盈单
      </div>

      <Spin spinning={loading}>
        {scopeRows.length === 0 && !loading ? (
          <Empty
            description={
              scopeIsTestnet(scope)
                ? '暂无测试网交易记录'
                : scope === 'live'
                  ? '暂无实盘交易记录（自动交易开启后自动开单）'
                  : '该账号暂无交易记录（自动交易开启后自动开单）'
            }
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
                    {/* 持仓诊断（docs/16）：浮亏在跑单的 AI 建议与理由 */}
                    {rec.ai_advice && (() => {
                      const meta = ADVICE_ACTION_META[rec.ai_advice!.action] || ADVICE_ACTION_META.hold
                      return (
                        <div
                          style={{
                            marginTop: 10,
                            padding: '10px 14px',
                            background: 'linear-gradient(135deg, #f9f0ff 0%, #fff 60%)',
                            border: '1px solid #efdbff',
                            borderRadius: 8,
                          }}
                        >
                          <div
                            style={{
                              display: 'flex',
                              alignItems: 'center',
                              gap: 10,
                              flexWrap: 'wrap',
                            }}
                          >
                            <div
                              style={{
                                fontSize: 12,
                                fontWeight: 600,
                                color: '#722ed1',
                                display: 'flex',
                                alignItems: 'center',
                                gap: 6,
                              }}
                            >
                              <RobotOutlined />
                              持仓诊断
                            </div>
                            <Tag color={meta.color} style={{ marginInlineEnd: 0 }}>
                              {meta.label}
                            </Tag>
                            <Tag
                              color={rec.ai_advice.thesis_valid ? 'green' : 'red'}
                              style={{ marginInlineEnd: 0 }}
                            >
                              开仓逻辑{rec.ai_advice.thesis_valid ? '成立' : '已破坏'}
                            </Tag>
                            <Tag
                              color={rec.ai_advice.trend_broken ? 'red' : 'green'}
                              style={{ marginInlineEnd: 0 }}
                            >
                              走势{rec.ai_advice.trend_broken ? '已走坏' : '未走坏'}
                            </Tag>
                            <span style={{ fontSize: 12, color: '#999', marginLeft: 'auto' }}>
                              {rec.ai_advice_at ? bj(rec.ai_advice_at).format('MM-DD HH:mm') : ''}
                            </span>
                          </div>
                          {/* 理由与分析：左侧完整展示（右侧操作历史仅 Tag 摘要） */}
                          {rec.ai_advice.reason && (
                            <div style={{ fontSize: 12, color: '#595959', lineHeight: 1.6, marginTop: 6 }}>
                              <b style={{ color: '#722ed1' }}>理由：</b>{rec.ai_advice.reason}
                            </div>
                          )}
                          {rec.ai_advice.detail && (
                            <div style={{ fontSize: 12, color: '#8c8c8c', lineHeight: 1.6, marginTop: 4 }}>
                              <b style={{ color: '#722ed1' }}>分析：</b>{rec.ai_advice.detail}
                            </div>
                          )}
                        </div>
                      )
                    })()}
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

      {/* 一键清空测试网数据弹窗：预检数量 + 在跑单警告，确认后执行 */}
      <Modal
        title="清空测试网数据"
        open={!!purgeInfo}
        onOk={handlePurge}
        confirmLoading={purging}
        onCancel={() => setPurgeInfo(null)}
        okText="确认清空"
        cancelText="取消"
        okButtonProps={{ danger: true }}
        width={440}
      >
        <div style={{ fontSize: 13, lineHeight: 2 }}>
          将删除测试网的：
          <div>
            交易记录 <b>{purgeInfo?.records ?? 0}</b> 条
            {(purgeInfo?.running ?? 0) > 0 && (
              <span style={{ color: '#faad14' }}>（其中在跑 {purgeInfo?.running} 笔）</span>
            )}
            、操作历史 <b>{purgeInfo?.events ?? 0}</b> 条、划转流水{' '}
            <b>{purgeInfo?.transfers ?? 0}</b> 条
          </div>
        </div>
        <div
          style={{
            marginTop: 8,
            padding: '8px 12px',
            borderRadius: 6,
            background: '#fffbe6',
            border: '1px solid #ffe58f',
            fontSize: 12,
            color: '#874d00',
          }}
        >
          <WarningOutlined style={{ marginRight: 4 }} />
          {(purgeInfo?.running ?? 0) > 0
            ? '交易所侧在跑仓位不会被平掉，删除后将无人管理；'
            : ''}
          实盘数据与复盘统计不受影响，此操作不可恢复。
        </div>
      </Modal>

      {/* AI 建议详情弹窗：完整持仓诊断（动作/开仓逻辑/走势/理由/补充说明），
          在跑单可在弹窗内直接重新诊断 */}
      <Modal
        title={
          adviceView && (
            <Space size={8}>
              <span>AI 持仓诊断</span>
              <Typography.Text strong>{adviceView.symbol}</Typography.Text>
              <Tag
                color={adviceView.direction === 'long' ? schemeTag(colorScheme).up : schemeTag(colorScheme).down}
                style={{ marginInlineEnd: 0 }}
              >
                {adviceView.direction === 'long' ? '多' : '空'}
              </Tag>
            </Space>
          )
        }
        open={!!adviceView}
        onCancel={() => setAdviceView(null)}
        width={520}
        footer={
          adviceView && ['OPENED', 'TP1_HIT', 'TP2_HIT'].includes(adviceView.status) ? (
            <Space>
              <Button onClick={() => setAdviceView(null)}>关闭</Button>
              <Button
                type="primary"
                loading={advisingId === adviceView.id}
                onClick={async () => {
                  await handleAdvice(adviceView)
                  setAdviceView(null)
                }}
              >
                重新诊断
              </Button>
            </Space>
          ) : (
            <Button onClick={() => setAdviceView(null)}>关闭</Button>
          )
        }
      >
        {adviceView?.ai_advice && (() => {
          const adv = adviceView.ai_advice
          const meta = ADVICE_ACTION_META[adv.action] || ADVICE_ACTION_META.hold
          const totalPnl =
            (adviceView.realized_pnl ?? 0) +
            (adviceView.status !== 'CLOSED' ? adviceView.unrealized_pnl ?? 0 : 0)
          return (
            <>
              <div style={{ fontSize: 12, color: '#8c8c8c', marginBottom: 10 }}>
                状态 {STATUS_MAP[adviceView.status]?.label || adviceView.status}
                {adviceView.entry_price ? ` · 开仓价 ${adviceView.entry_price.toPrecision(6)}` : ''}
                {' · 当前收益 '}
                <span
                  style={{
                    color: totalPnl > 0 ? schemeTag(colorScheme).up : totalPnl < 0 ? schemeTag(colorScheme).down : undefined,
                    fontWeight: 600,
                  }}
                >
                  {totalPnl > 0 ? '+' : ''}
                  {totalPnl.toFixed(2)} USDT
                </span>
              </div>
              <Descriptions size="small" column={2} bordered style={{ marginBottom: 12 }}>
                <Descriptions.Item label="建议动作">
                  <Tag color={meta.color} style={{ marginInlineEnd: 0 }}>{meta.label}</Tag>
                </Descriptions.Item>
                <Descriptions.Item label="诊断时间">
                  {adviceView.ai_advice_at ? bj(adviceView.ai_advice_at).format('YYYY-MM-DD HH:mm') : '-'}
                </Descriptions.Item>
                <Descriptions.Item label="开仓逻辑">
                  <span style={{ color: adv.thesis_valid ? '#52c41a' : '#ff4d4f', fontWeight: 600 }}>
                    {adv.thesis_valid ? '仍成立' : '已破坏'}
                  </span>
                </Descriptions.Item>
                <Descriptions.Item label="当前走势">
                  <span style={{ color: adv.trend_broken ? '#ff4d4f' : '#52c41a', fontWeight: 600 }}>
                    {adv.trend_broken ? '已走坏' : '未走坏'}
                  </span>
                </Descriptions.Item>
              </Descriptions>
              <div style={{ fontSize: 13, lineHeight: 1.8 }}>{adv.reason}</div>
              {adv.detail && (
                <div style={{ fontSize: 12, color: '#8c8c8c', marginTop: 8, lineHeight: 1.7 }}>
                  {adv.detail}
                </div>
              )}
            </>
          )
        })()}
      </Modal>
    </div>
  )
}
