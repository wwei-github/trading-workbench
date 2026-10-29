/**
 * 最新筛选（2026-10-28，docs/需求文档/18-最新筛选展示/spec.md）
 *
 * 目的：在「待确认池」里加一个页签，一眼回答三个问题——
 *   ① 这一轮各关卡各拦了多少币（逐节点路径 + 吸顶条 + 卡片）
 *   ② 每个关卡卡的是"哪个条件"、具体是哪些币（下钻抽屉，按原因分组）
 *   ③ 0 命中是常态还是恶化（底部 24 轮时序）
 *
 * 数据源：
 * - GET /api/scans/funnels?hours=24 —— 逐轮快照（stages/整体计数/命中数），画时序
 * - GET /api/scans/{id}/funnel —— 当前轮的 rejects + details，供抽屉按原因分组
 *
 * 交互约定（与示例页 funnel-demo.html 对齐）：
 * - 吸顶条随滚动自动收起（无"固定展开"开关：常驻区只该在需要时占位）
 * - 栈段与卡片双向联动（悬停互相点亮）
 * - 点栈段/卡片 → 抽屉；抽屉里币种点击 → K 线弹窗
 * - 点时序柱 → 切换轮次，吸顶条出现「正在回看 …」+「回到最新一轮」
 */
import { useCallback, useEffect, useMemo, useRef, useState, type CSSProperties } from 'react'
import { Alert, Button, Drawer, Empty, Input, Modal, Spin, Tooltip, Typography, message } from 'antd'
import { ReloadOutlined, SearchOutlined } from '@ant-design/icons'
import { scanApi } from '../api/scan'
import { useScanStore } from '../stores/scanStore'
import { FUNNEL_STAGES, STAGE_META } from '../constants/funnelStages'
import KlineChart from './KlineChart'
import type { FunnelData, FunnelSnapshot } from '../types'

const { Text } = Typography

interface Group {
  key: string
  name: string
  color: string
  stages: string[]
}

/** 关卡分组（8 组）：把 19 个关卡代码压缩成"人能记住的 8 个"，顺序即语义链
 *  池过滤 → 4h 背景 → 拐头锁 → 触发层 → 待确认失效 → 震荡扣分 → 方向/白名单 → 质量分。
 *  刻意不按数量重排——吸顶条与卡片按数量降序，这里是稳定的语义序（图例用）。
 *  2026-10-28（docs/19 §4.2）：pending_waiting 是"还在池里等确认"，不是被拒，
 *  不再进拦截栈条；只把终态（超窗/反向）留在「待确认失效」组。 */
const GROUPS: Group[] = [
  { key: 'pool', name: '池过滤', color: '#a8b0b9',
    stages: ['liquidity', 'untradable', 'new_coin', 'risk_tier', 'blacklist'] },
  { key: 'gate', name: '4h 背景门', color: '#e5484d', stages: ['gate_4h'] },
  { key: 'lock', name: '拐头锁', color: '#f76b15', stages: ['htf_state_lock'] },
  { key: 'trig', name: '触发层', color: '#3b82f6',
    stages: ['no_trigger', 'no_touch', 'leg_gate', 'golden_no_context', 'no_golden', 'insufficient_bars'] },
  { key: 'pend', name: '待确认失效', color: '#0f9b8e',
    stages: ['window_expired', 'pending_flip'] },
  { key: 'range', name: '震荡计分', color: '#f0a020', stages: ['range_score'] },
  { key: 'whitel', name: '方向/白名单', color: '#8b5cf6',
    stages: ['votes', 'direction_flip', 'trigger_whitelist'] },
  { key: 'qfloor', name: '质量分', color: '#79828d', stages: ['quality_floor'] },
]

interface GroupRow extends Group {
  n: number
}

/** 逐节点路径的关卡顺序（E 方案）：用 FUNNEL_STAGES 的判定序，剔除 pending_waiting——
 *  它不是被拒，是"还在池里等确认"，单列到路径终点，不参与进入/挂掉/通过的累加。 */
const PIPE_STAGES = FUNNEL_STAGES.map((s) => s.key).filter((k) => k !== 'pending_waiting')

/** 4h 状态流转链路（2026-10-29）：抽屉里画「哪些状态放行、本轮卡住的币停在哪一态」。
 *  这里画的是最常见的推进顺序，不是硬转移表——每根 4h 收盘都按优先级从头重判；
 *  短标签为的是 560px 抽屉里一行放得下（与 STATE_LABELS 全名不同）。 */
const HTF_CHAINS: { lane: string; nodes: { key: string; label: string; pass: boolean }[] }[] = [
  {
    lane: '多头',
    nodes: [
      { key: 'tangle', label: '缠绕', pass: false },
      { key: 'macro_cross_bull', label: '金叉初期', pass: false },
      { key: 'aligned_bull', label: '多头排列', pass: true },
      { key: 'turn_bull', label: '多头拐头', pass: false },
      { key: 'bull_dip', label: '牛内回调', pass: false },
      { key: 'bull_recover', label: '多头恢复', pass: true },
      { key: 'aligned_bull', label: '多头排列', pass: true },
    ],
  },
  {
    lane: '空头',
    nodes: [
      { key: 'tangle', label: '缠绕', pass: false },
      { key: 'macro_cross_bear', label: '死叉初期', pass: false },
      { key: 'aligned_bear', label: '空头排列', pass: true },
      { key: 'turn_bear', label: '空头拐头', pass: false },
      { key: 'bear_rally', label: '熊内反弹', pass: false },
      { key: 'bear_recover', label: '空头恢复', pass: true },
      { key: 'aligned_bear', label: '空头排列', pass: true },
    ],
  },
]

/** 「通过」按 4h 放行状态分组的展示顺序（与后端 STATE_ALIGNED 同集）：
 *  多头侧在前、空头侧在后，恢复初期跟排列后面；未记录（旧轮次/数据降级）垫底。 */
const HTF_PASS_ORDER = ['多头排列', '多头恢复初期', '空头排列', '空头恢复初期']
const HTF_PASS_UNKNOWN = '未记录'
const passRank = (label: string): number => {
  const i = HTF_PASS_ORDER.indexOf(label)
  if (i >= 0) return i
  return label === HTF_PASS_UNKNOWN ? HTF_PASS_ORDER.length + 1 : HTF_PASS_ORDER.length + 0.5
}

/** 状态链路：绿=放行（仅排列）· 灰=禁开 · 红框=本轮有币卡在这一态 */
function HtfStateChain({ activeLabels }: { activeLabels: Set<string> }) {
  return (
    <div style={{
      border: '1px solid #f0f0f0', borderRadius: 10, padding: '10px 12px',
      marginBottom: 10, background: '#fcfcfd',
    }}>
      <div style={{
        display: 'flex', alignItems: 'baseline', gap: 8, flexWrap: 'wrap',
        marginBottom: 4,
      }}>
        <Text strong style={{ fontSize: 12 }}>4h 状态流转</Text>
        <Text type="secondary" style={{ fontSize: 11 }}>
          每根 4h 收盘重判 · 绿=放行 · 红框=本轮卡在这
        </Text>
      </div>
      {HTF_CHAINS.map((lane) => (
        <div key={lane.lane} style={{
          display: 'flex', alignItems: 'center', flexWrap: 'wrap',
          gap: '6px 3px', marginTop: 6,
        }}>
          <span style={{ fontSize: 11, color: '#8c8c8c', width: 24, flexShrink: 0 }}>
            {lane.lane}
          </span>
          {lane.nodes.map((n, i) => (
            <span key={`${n.key}-${i}`} style={{ display: 'inline-flex', alignItems: 'center', gap: 3 }}>
              <span style={{
                fontSize: 11, lineHeight: '17px', padding: '1px 6px', borderRadius: 9,
                border: `1px solid ${activeLabels.has(n.label) ? '#ff7875'
                  : n.pass ? '#b7eb8f' : '#e8e8e8'}`,
                background: n.pass ? '#f6ffed' : '#fff',
                color: activeLabels.has(n.label) ? '#cf1322'
                  : n.pass ? '#389e0d' : '#8c8c8c',
                fontWeight: n.pass ? 600 : 400,
                boxShadow: activeLabels.has(n.label) ? '0 0 0 2px rgba(255,77,79,0.22)' : 'none',
              }}>{n.label}</span>
              {i < lane.nodes.length - 1 && (
                <span style={{ color: '#d9d9d9', fontSize: 10 }}>→</span>
              )}
            </span>
          ))}
        </div>
      ))}
      <div style={{ fontSize: 11, color: '#bfbfbf', marginTop: 8, lineHeight: 1.6 }}>
        排列需交叉满 10 根才成型；恢复初期为宏观同侧的新交叉（2026-10-29 起放行）；
        缠绕是两个方向共用的归零态；排列或恢复一旦被破坏即退出放行。
      </div>
    </div>
  )
}

/** 关卡 → 所属分组（取第一个命中的组），用于节点圆点与分组名着色 */
const STAGE_GROUP: Record<string, Group> = {}
GROUPS.forEach((g) => g.stages.forEach((k) => { STAGE_GROUP[k] = g }))

const MONO = 'ui-monospace, Menlo, monospace'
const NUM_STYLE: CSSProperties = {
  fontFamily: MONO, fontSize: 12, fontWeight: 600, color: '#1f2329',
}
const CHIP_STYLE: CSSProperties = {
  fontFamily: MONO, fontSize: 10.5, color: '#4b5460',
  background: '#f6f7f9', border: '1px solid #f0f0f0', borderRadius: 4, padding: '1px 5px',
}

const sumStages = (stages: Record<string, number> | undefined, keys: string[]) =>
  keys.reduce((a, k) => a + (stages?.[k] ?? 0), 0)

function buildGroups(stages: Record<string, number> | undefined): GroupRow[] {
  return GROUPS
    .map((g) => ({ ...g, n: sumStages(stages, g.stages) }))
    .filter((g) => g.n > 0)
    .sort((a, b) => b.n - a.n)
}

/** 分组内的"构成"摘要：把子关卡名与数量拼成一句（只列非零项） */
function groupSummary(stages: Record<string, number> | undefined, g: Group): string {
  const parts = g.stages
    .map((k) => ({ k, n: stages?.[k] ?? 0 }))
    .filter((x) => x.n > 0)
    .sort((a, b) => b.n - a.n)
    .map(({ k, n }) => `${STAGE_META[k]?.label ?? k} ${n}`)
  return parts.join(' · ')
}

type HtfEv = NonNullable<FunnelData['htf_evidence']>[string]

/** gate_4h 否定票（reject_reason → 分组文案）：scanner 不给 gate_4h 写 details
 *  （只有 votes / no_trigger 有判定文本），若沿用"details 缺失回落关卡口径说明"，
 *  整关币种会挤成同一组。这里按 funnel_json.htf_evidence[symbol].reject_reason
 *  的否定票分组：走平/缠绕/ADX 不可用/方向歧义/数据不足/数据缺失各一组。
 *  顺序即 htf_context 的判定优先级。 */
const GATE_4H_VETO: { key: string; label: string; hint: string }[] = [
  { key: 'flat', label: 'EMA144 走平', hint: '近 20 根变化 <0.5%，无明确趋势' },
  { key: 'tangle', label: '状态缠绕', hint: 'ADX<20 / 21·55 间距<0.1% / 锯齿 / 穿越 EMA144 >2 次' },
  { key: 'adx', label: 'ADX 不可用', hint: 'ADX 计算失败 → 按无趋势否决（fail-closed）' },
  { key: 'undetermined', label: '方向歧义', hint: '价格与 EMA144 位置、斜率不同号' },
  { key: 'data', label: '数据不足', hint: '已收盘 4h K 线 <200 根或 EMA 非有限值' },
  { key: 'missing', label: '数据缺失', hint: '4h K 线拉取失败/空数据（数据降级）' },
]
const VETO_META: Record<string, { label: string; hint: string }> =
  Object.fromEntries(GATE_4H_VETO.map((v) => [v.key, v]))

/** 单个币的否定证据：组头是票面，币与币之间只有数值差异，放币名的 title 上，
 *  避免 "ADX14" / "ADX15" 这种把同一张票拆成多组。 */
function vetoNote(ev?: HtfEv): string {
  const e = ev?.evidence
  switch (ev?.reject_reason) {
    case 'flat':
      return e?.slope_pct != null
        ? `20 根斜率 ${(e.slope_pct * 100).toFixed(2)}% < 0.5%` : ''
    case 'tangle':
      return [
        e?.adx != null ? `ADX ${e.adx}` : null,
        e?.wraps != null ? `近 10 根穿越 EMA144 ${e.wraps} 次` : null,
      ].filter(Boolean).join(' · ')
    case 'undetermined':
      return e?.close_vs_ema144 != null
        ? `收盘相对 EMA144 ${(e.close_vs_ema144 * 100).toFixed(2)}%（位置与斜率不同号）` : ''
    default:
      return ev?.degraded ? '数据降级' : ''
  }
}

const fmtTime = (iso: string | null): string => {
  if (!iso) return '—'
  // 后端回 naive UTC ISO（无 Z），显式补 Z 再转本地，避免被当成东八区本地时间
  const d = new Date(iso.endsWith('Z') ? iso : `${iso}Z`)
  const p = (n: number) => String(n).padStart(2, '0')
  return `${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`
}

export default function LatestFunnelPanel() {
  const currentScanId = useScanStore((s) => s.currentScanId)
  const isScanning = !!useScanStore((s) => s.status?.is_scanning)

  const [snaps, setSnaps] = useState<FunnelSnapshot[]>([])
  const [listLoading, setListLoading] = useState(false)
  const [detail, setDetail] = useState<FunnelData | null>(null)
  const [detailLoading, setDetailLoading] = useState(false)
  /** null = 看最新一轮；数字 = 看历史第 idx 轮（时序柱下标） */
  const [activeIdx, setActiveIdx] = useState<number | null>(null)
  const [hot, setHot] = useState<string | null>(null)
  const [drawerGroup, setDrawerGroup] = useState<GroupRow | null>(null)
  const [search, setSearch] = useState('')
  const [klineSymbol, setKlineSymbol] = useState<string | null>(null)
  const [compact, setCompact] = useState(false)
  /** 路径节点悬停：只亮本行背景，不置灰其它内容（E 方案交互约定） */
  const [hoverStage, setHoverStage] = useState<string | null>(null)
  /** 单关卡抽屉的「通过者」名单（null = 分组抽屉/旧轮次无币池清单） */
  const [drawerPassers, setDrawerPassers] = useState<string[] | null>(null)
  const [drawerTab, setDrawerTab] = useState<'drop' | 'pass'>('drop')
  /** 单关卡抽屉（多关卡分组抽屉不展示关卡专属说明，避免口径混淆） */
  const drawerStageKey = drawerGroup && drawerGroup.stages.length === 1
    ? drawerGroup.stages[0]
    : null
  const drawerPassHint = drawerStageKey ? STAGE_META[drawerStageKey]?.passHint : undefined

  const sentinelRef = useRef<HTMLDivElement | null>(null)
  const prevScanning = useRef(false)

  const activeSnap: FunnelSnapshot | null = useMemo(() => {
    if (!snaps.length) return null
    return activeIdx === null ? snaps[snaps.length - 1] : snaps[activeIdx] ?? null
  }, [snaps, activeIdx])

  const groups = useMemo(() => buildGroups(activeSnap?.stages), [activeSnap])
  const total = useMemo(() => groups.reduce((a, g) => a + g.n, 0), [groups])
  /** 在池等待数：不是被拒，不进拦截栈条与总数（2026-10-28，docs/19 §4.2） */
  const waiting = activeSnap?.stages?.pending_waiting ?? 0

  /** 关卡 → 挂掉的币种（来自该轮 funnel.rejects；历史轮次通常没有清单） */
  const rejectByStage = useMemo(() => {
    const m = new Map<string, string[]>()
    Object.entries(detail?.rejects ?? {}).forEach(([symbol, stage]) => {
      const arr = m.get(stage)
      if (arr) arr.push(symbol)
      else m.set(stage, [symbol])
    })
    return m
  }, [detail])

  /** E 方案逐节点累加：进入 → 挂掉 → 通过；pending_waiting 不进链路，单列终点 */
  const pipe = useMemo(() => {
    const stages = activeSnap?.stages
    let enter = activeSnap?.universe ?? 0
    const rows = PIPE_STAGES.map((k) => {
      const drop = stages?.[k] ?? 0
      const out = Math.max(0, enter - drop)
      const row = { k, enter, drop, out }
      enter = out
      return row
    })
    let maxKey: string | null = null
    let maxDrop = 0
    rows.forEach((r) => {
      if (r.drop > maxDrop) { maxDrop = r.drop; maxKey = r.k }
    })
    return { rows, maxKey, maxDrop, hasStages: !!stages }
  }, [activeSnap])

  const loadList = useCallback(async () => {
    setListLoading(true)
    try {
      const r = await scanApi.funnels(24)
      setSnaps(r.items || [])
    } catch (e: any) {
      message.error(e?.response?.data?.detail || '获取逐轮漏斗失败')
    } finally {
      setListLoading(false)
    }
  }, [])

  const loadDetail = useCallback(async (scanId: string | undefined) => {
    if (!scanId) return
    setDetailLoading(true)
    try {
      setDetail((await scanApi.funnel(scanId)).funnel)
    } catch (e: any) {
      message.error(e?.response?.data?.detail || '获取该轮漏斗明细失败')
      setDetail(null)
    } finally {
      setDetailLoading(false)
    }
  }, [])

  useEffect(() => { void loadList() }, [loadList, currentScanId])

  // 扫描完成（进行中→结束翻转）后自动刷新；ref 记前值防挂载时双请求
  useEffect(() => {
    if (prevScanning.current && !isScanning) void loadList()
    prevScanning.current = isScanning
  }, [isScanning, loadList])

  // 切换轮次 → 拉该轮明细（抽屉按原因分组要用 rejects/details）
  useEffect(() => {
    void loadDetail(activeSnap?.scan_id)
    setDrawerGroup(null)
  }, [activeSnap?.scan_id, loadDetail])

  // 吸顶折叠：哨兵滚出视口即收起（用 Observer 而非 scroll 监听，兼容任意滚动容器）
  useEffect(() => {
    const node = sentinelRef.current
    if (!node) return
    const io = new IntersectionObserver(
      ([entry]) => setCompact(!entry.isIntersecting),
      { rootMargin: '-8px 0px 0px 0px', threshold: 0 },
    )
    io.observe(node)
    return () => io.disconnect()
  }, [])

  /** 抽屉数据：当前轮该组所有币 + 每个币的"卡住原因"（details 优先，缺则关卡口径；
   *  gate_4h 例外——按 htf_evidence 的否定票分组，见 GATE_4H_VETO） */
  const drawerRows = useMemo(() => {
    if (!drawerGroup) return []
    const set = new Set(drawerGroup.stages)
    const rows: { symbol: string; stage: string; reason: string; note: string }[] = []
    Object.entries(detail?.rejects ?? {}).forEach(([symbol, stage]) => {
      if (!set.has(stage)) return
      if (stage === 'gate_4h') {
        const ev = detail?.htf_evidence?.[symbol]
        const veto = ev?.reject_reason ? VETO_META[ev.reject_reason] : undefined
        rows.push({
          symbol,
          stage,
          reason: veto
            ? `${veto.label} · ${veto.hint}`
            : '未记录否定票（旧轮次无 4h 证据）',
          note: vetoNote(ev),
        })
        return
      }
      const d = detail?.details?.[symbol]
      rows.push({
        symbol,
        stage,
        reason: (d && d.trim()) || (STAGE_META[stage]?.hint ?? stage),
        note: '',
      })
    })
    return rows
  }, [drawerGroup, detail])

  const reasonGroups = useMemo(() => {
    const kw = search.trim().toUpperCase()
    const m = new Map<string, { symbol: string; stage: string; note: string }[]>()
    drawerRows
      .filter((r) => !kw || r.symbol.includes(kw))
      .forEach((r) => {
        const arr = m.get(r.reason) ?? []
        arr.push({ symbol: r.symbol, stage: r.stage, note: r.note })
        m.set(r.reason, arr)
      })
    return [...m.entries()]
      .map(([reason, coins]) => ({ reason, coins }))
      .sort((a, b) => b.coins.length - a.coins.length)
  }, [drawerRows, search])

  /** 本轮卡住的币落在链路哪几个节点上（按状态中文名匹配判定说明文案） */
  const activeStateLabels = useMemo(() => {
    const hit = new Set<string>()
    const reasons = reasonGroups.map((r) => r.reason)
    HTF_CHAINS.forEach((lane) => lane.nodes.forEach((n) => {
      if (reasons.some((r) => r.includes(n.label))) hit.add(n.label)
    }))
    return hit
  }, [reasonGroups])

  /** 抽屉-通过者：按搜索词过滤 */
  const passerRows = useMemo(() => {
    const kw = search.trim().toUpperCase()
    return (drawerPassers ?? []).filter((s) => !kw || s.includes(kw))
  }, [drawerPassers, search])

  /** 4h 状态禁开抽屉的「通过」：按 4h 放行状态分组（2026-10-29）。
   *  状态名来自 funnel_json.htf_pass_states（后端扫描时落），旧轮次无该键 →
   *  全部归「未记录」，仍平铺展示、不误报。 */
  const passerStateGroups = useMemo(() => {
    const m = new Map<string, string[]>()
    passerRows.forEach((sym) => {
      const label = detail?.htf_pass_states?.[sym] || HTF_PASS_UNKNOWN
      const arr = m.get(label)
      if (arr) arr.push(sym)
      else m.set(label, [sym])
    })
    return [...m.entries()]
      .map(([label, symbols]) => ({ label, symbols }))
      .sort((a, b) => passRank(a.label) - passRank(b.label))
  }, [passerRows, detail])

  // 数据驱动渲染（2026-10-28 修复）：本面板数据来自 /scans/funnels（近 24 轮全量），
  // 不依赖 currentScanId。刷新后 currentScanId 为 null（仅内存态），且最新一轮若
  // 0 命中，scanStore 推不出 id——旧守卫会误报「暂无扫描记录」把已有轮次全隐藏。
  if (!activeSnap) {
    return listLoading
      ? <div style={{ textAlign: "center", marginTop: 80 }}><Spin /></div>
      : <Empty description="近 24 小时暂无扫描记录，先执行一次扫描" style={{ marginTop: 80 }} />
  }

  const head = groups[0]
  const second = groups[1]
  const isHistory = activeIdx !== null
  /** 4h 闸门抽屉：按否定票（htf_evidence.reject_reason）分组 */
  const vetoDrawer = !!drawerGroup?.stages.includes('gate_4h')
  /** 4h 状态禁开抽屉：「通过」按 4h 放行状态（排列 / 恢复初期）分组 */
  const passGrouped = drawerStageKey === 'htf_state_lock'
  /** 路径最后一行的"通过"数 = 候选 + 本轮进池 + 池内等待 + 异常 */
  const lastOut = pipe.rows.length ? pipe.rows[pipe.rows.length - 1].out : activeSnap.candidates

  const openGroup = (g: GroupRow) => {
    if (isHistory) {
      message.info('历史轮次未保存币种清单，请先点「回到最新一轮」')
      return
    }
    if (!detail) {
      message.info('该轮明细仍在加载，稍候再试')
      return
    }
    setSearch('')
    setDrawerPassers(null)
    setDrawerTab('drop')
    setDrawerGroup(g)
  }

  /** 某关通过者 = 币池 − 本关及之前被拒 − 扫描异常（需要该轮落了 universe_symbols） */
  const passersOf = (k: string): string[] | null => {
    const uni = detail?.universe_symbols
    if (!uni?.length) return null
    const idx = PIPE_STAGES.indexOf(k)
    if (idx < 0) return null
    const miss = new Set(detail?.error_symbols ?? [])
    Object.entries(detail?.rejects ?? {}).forEach(([sym, st]) => {
      const si = PIPE_STAGES.indexOf(st)
      if (si >= 0 && si <= idx) miss.add(sym)
    })
    return uni.filter((s) => !miss.has(s))
  }

  /** 点路径节点 → 复用分组抽屉，只装这一关 */
  const openStage = (k: string) => {
    const n = activeSnap?.stages?.[k] ?? 0
    if (!n) return
    openGroup({
      key: k,
      name: STAGE_META[k]?.label ?? k,
      color: STAGE_GROUP[k]?.color ?? '#a8b0b9',
      stages: [k],
      n,
    })
    setDrawerPassers(passersOf(k))
  }

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 14, padding: '0 4px' }}>
      {/* ---------- 筛选路径（E 方案：逐节点明细） ---------- */}
      <div style={{
        background: '#fff', border: '1px solid #f0f0f0', borderRadius: 12,
        padding: '12px 14px 10px',
      }}>
        <div style={{ display: 'flex', alignItems: 'baseline', gap: 10, flexWrap: 'wrap', marginBottom: 8 }}>
          <Text strong style={{ fontSize: 13 }}>筛选路径</Text>
          <Text type="secondary" style={{ fontSize: 11.5 }}>
            {pipe.hasStages
              ? `从币池到候选 · ${PIPE_STAGES.length} 个关卡按判定顺序逐行累加`
              : '该轮未保存逐关明细（旧版本数据），仅保留总量'}
          </Text>
          <span style={{ marginLeft: 'auto', fontSize: 11.5, color: '#8c8c8c' }}>
            {isHistory ? '历史轮次无币种清单' : detailLoading ? '明细加载中…' : '点节点看挂掉/通过'}
          </span>
        </div>

        <div style={{ position: 'relative', paddingLeft: 26 }}>
          <div style={{
            position: 'absolute', left: 8, top: 10, bottom: 10,
            width: 1, background: '#ededed',
          }} />

          {/* 起点 */}
          <div style={{
            position: 'relative', display: 'flex', alignItems: 'baseline',
            gap: 8, flexWrap: 'wrap', padding: '4px 10px 6px',
          }}>
            <span style={{
              position: 'absolute', left: -22, top: 11, width: 7, height: 7,
              borderRadius: '50%', background: '#fff',
              border: '2px solid #8c8c8c', boxSizing: 'border-box',
            }} />
            <Text strong style={{ fontSize: 13 }}>币池 {activeSnap.universe}</Text>
            <Text type="secondary" style={{ fontSize: 11.5 }}>全部进入本轮筛选</Text>
          </div>

          {/* 逐关卡 */}
          {pipe.hasStages && pipe.rows.map((r) => {
            const color = STAGE_GROUP[r.k]?.color ?? '#a8b0b9'
            const meta = STAGE_META[r.k]
            const coins = isHistory ? [] : rejectByStage.get(r.k) ?? []
            const shown = coins.slice(0, 6)
            const live = r.drop > 0
            const rate = r.enter ? Math.round((r.drop / r.enter) * 100) : 0
            return (
              <div
                key={r.k}
                onClick={() => openStage(r.k)}
                onMouseEnter={() => { if (live) setHoverStage(r.k) }}
                onMouseLeave={() => setHoverStage(null)}
                style={{
                  position: 'relative', padding: '7px 10px 8px', borderRadius: 8,
                  background: live && hoverStage === r.k ? '#e9f1fb' : undefined,
                  opacity: live ? 1 : 0.55,
                  cursor: live ? 'pointer' : 'default',
                  transition: 'background .12s ease',
                }}
              >
                <span style={{
                  position: 'absolute', left: -22, top: 15, width: 7, height: 7,
                  borderRadius: '50%', background: '#fff',
                  border: `2px solid ${color}`, boxSizing: 'border-box',
                }} />
                <div style={{ display: 'flex', alignItems: 'center', gap: 7, flexWrap: 'wrap' }}>
                  <Tooltip title={meta?.hint}>
                    <span style={{ fontSize: 12.5, fontWeight: 600 }}>{meta?.label ?? r.k}</span>
                  </Tooltip>
                  <span style={{ fontSize: 10.5, color }}>{STAGE_GROUP[r.k]?.name}</span>
                  {r.k === pipe.maxKey && (
                    <span style={{
                      fontSize: 10, fontWeight: 600, color: '#1f7a55',
                      background: '#e9f7f1', borderRadius: 999, padding: '0 6px',
                    }}>最大落点 {rate}%</span>
                  )}
                  <span style={{ fontSize: 11.5, color: '#8c8c8c', fontVariantNumeric: 'tabular-nums' }}>
                    进入 <b style={NUM_STYLE}>{r.enter}</b>
                    <span style={{ margin: '0 2px' }}>·</span>
                    挂 <b style={{ ...NUM_STYLE, color: '#cf1322' }}>{r.drop}</b>
                    <span style={{ margin: '0 2px' }}>·</span>
                    通过 <b style={NUM_STYLE}>{r.out}</b>
                  </span>
                  {live && (
                    <span style={{
                      fontSize: 10.5, color: '#cf1322', background: '#fff1f0',
                      borderRadius: 999, padding: '0 6px',
                    }}>{rate}%</span>
                  )}
                  {live && shown.map((sym) => (
                    <span key={sym} title={sym} style={CHIP_STYLE}>{sym.replace('USDT', '')}</span>
                  ))}
                  {live && coins.length > shown.length && (
                    <span style={{
                      ...CHIP_STYLE, background: '#fff',
                      borderStyle: 'dashed', color: '#8c8c8c',
                    }}>+{coins.length - shown.length}</span>
                  )}
                </div>
              </div>
            )
          })}

          {/* 终点 */}
          <div style={{
            position: 'relative', display: 'flex', alignItems: 'baseline',
            gap: 8, flexWrap: 'wrap', padding: '8px 10px 2px', marginTop: 4,
            borderTop: '1px dashed #f0f0f0',
          }}>
            <span style={{
              position: 'absolute', left: -22, top: 14, width: 7, height: 7,
              borderRadius: '50%', background: '#fff',
              border: '2px solid #30a46c', boxSizing: 'border-box',
            }} />
            <Text strong style={{ fontSize: 13 }}>通过 {lastOut}</Text>
            <Text type="secondary" style={{ fontSize: 11.5 }}>
              候选 {activeSnap.candidates} · 本轮进池 {activeSnap.pending_pool} · 池内等待 {waiting}
              {activeSnap.errors > 0 ? ` · 异常 ${activeSnap.errors}` : ''}
            </Text>
            <Text type="secondary" style={{ fontSize: 11.5 }}>· 命中 {activeSnap.hit_count}</Text>
          </div>
        </div>

        {pipe.hasStages && (
          <div style={{ fontSize: 11.5, color: '#8c8c8c', marginTop: 4, paddingLeft: 26 }}>
            每行 = 一个关卡：进入 → 挂掉 → 通过；挂掉的币按关卡归因取前 6 个
          </div>
        )}
      </div>

      <div ref={sentinelRef} style={{ height: 1 }} />

      {/* ---------- 吸顶条 ---------- */}
      <div
        style={{
          position: 'sticky', top: 0, zIndex: 20,
          background: '#fff', border: '1px solid #f0f0f0', borderRadius: 12,
          boxShadow: compact ? '0 8px 20px -16px rgba(0,0,0,.45)' : 'none',
          transition: 'box-shadow .18s ease',
        }}
      >
        <div style={{ padding: compact ? '7px 14px 6px' : '11px 14px 8px' }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: 14, flexWrap: 'wrap' }}>
            <div style={{ display: 'flex', alignItems: 'center', gap: 7 }}>
              <span style={{
                width: 6, height: 6, borderRadius: '50%', background: '#30a46c',
                boxShadow: '0 0 0 3px rgba(48,164,108,.14)',
              }} />
              <Text strong style={{ fontSize: 13 }}>
                {isHistory ? `${fmtTime(activeSnap?.created_at ?? null)} 轮` : `${fmtTime(activeSnap?.created_at ?? null)}`}
              </Text>
              <Text type="secondary" style={{ fontSize: 12 }}>
                {isHistory ? '历史轮次（仅计数，无币种清单）' : '最近一轮扫描'}
              </Text>
            </div>

            {/* 窄屏：6 个 KPI 的 min-content 约 500px，整体不换行会溢出吸顶条。
                容器自身允许内部换行并右对齐，项宽度不受挤压（不会截断数字） */}
            <div style={{
              display: 'flex', alignItems: 'flex-end', marginLeft: 'auto',
              flexWrap: 'wrap', justifyContent: 'flex-end', rowGap: 4,
              minWidth: 0, maxWidth: '100%',
            }}>
              {([
                { label: '币池', value: activeSnap?.universe ?? 0, color: undefined },
                { label: '进管线', value: activeSnap?.scanned ?? 0, color: undefined },
                { label: '出候选', value: activeSnap?.candidates ?? 0, color: (activeSnap?.candidates ?? 0) > 0 ? '#389e0d' : '#cf1322' },
                { label: '本轮进池', value: activeSnap?.pending_pool ?? 0, color: undefined },
                { label: '池内活跃', value: activeSnap?.pending_active ?? '—', color: undefined },
                { label: '命中', value: activeSnap?.hit_count ?? 0, color: (activeSnap?.hit_count ?? 0) > 0 ? '#389e0d' : undefined },
              ]).map((k, i) => (
                <div key={k.label} style={{
                  minWidth: 58, padding: '0 12px',
                  borderLeft: i === 0 ? 'none' : '1px solid #f0f0f0',
                }}>
                  <div style={{
                    fontSize: compact ? 15 : 19, fontWeight: 600, lineHeight: 1.15,
                    fontVariantNumeric: 'tabular-nums', color: k.color,
                  }}>{k.value}</div>
                  {!compact && (
                    <div style={{ fontSize: 11.5, color: '#8c8c8c' }}>{k.label}</div>
                  )}
                </div>
              ))}
            </div>

            <Tooltip title="重新拉取逐轮漏斗">
              <Button size="small" type="text" icon={<ReloadOutlined />} onClick={() => void loadList()} />
            </Tooltip>
          </div>

          {/* 100% 堆叠条 */}
          <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginTop: compact ? 5 : 8 }}>
            <div style={{
              display: 'flex', flex: 1, height: compact ? 6 : 24, borderRadius: 7,
              overflow: 'hidden', background: '#f5f5f5', transition: 'height .18s ease',
            }}>
              {groups.map((g) => {
                const w = (g.n / (total || 1)) * 100
                return (
                  <Tooltip key={g.key} title={`${g.name}：拦 ${g.n} 个币（占 ${Math.round(w)}%）· 点击看币种`}>
                    <div
                      onClick={() => openGroup(g)}
                      onMouseEnter={() => setHot(g.key)}
                      onMouseLeave={() => setHot(null)}
                      style={{
                        width: `${w}%`, background: g.color, cursor: 'pointer',
                        position: 'relative', transition: 'opacity .14s',
                        opacity: hot && hot !== g.key ? 0.32 : 1,
                        boxShadow: 'inset -1px 0 0 rgba(255,255,255,.85)',
                        display: 'grid', placeItems: 'center',
                      }}
                    >
                      {!compact && w > 8 && (
                        <span style={{ color: '#fff', fontSize: 11, fontWeight: 600 }}>{g.n}</span>
                      )}
                    </div>
                  </Tooltip>
                )
              })}
            </div>
            <Text type="secondary" style={{ fontSize: 11.5, whiteSpace: 'nowrap' }}>
              共 {total} 次拦截{waiting > 0 ? ` · 另有 ${waiting} 个在池等待` : ''}
            </Text>
          </div>

          {/* 主要卡点（结论句，不折叠成表） */}
          {!compact && (
            <div style={{ fontSize: 12.5, color: '#4b5460', marginTop: 7, whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }}>
              <Text type="secondary">主要卡点</Text>{' '}
              <Text strong>{head ? head.name : '—'} {head?.n ?? 0}</Text>
              <Text type="secondary">（{head ? groupSummary(activeSnap?.stages, head) : '本轮没有被拦币种'}）</Text>
              {second && (
                <>
                  <span style={{ color: '#d9d9d9', margin: '0 8px' }}>|</span>
                  <Text type="secondary">其次</Text>{' '}
                  <Text strong>{second.name} {second.n}</Text>
                  <Text type="secondary">（{groupSummary(activeSnap?.stages, second)}）</Text>
                </>
              )}
            </div>
          )}
        </div>

        {isHistory && (
          <div style={{
            display: 'flex', alignItems: 'center', gap: 8, fontSize: 12,
            color: '#a35a00', background: '#fff8ed', borderTop: '1px solid #fde3bf',
            padding: '6px 14px',
          }}>
            <span>正在回看 {fmtTime(activeSnap?.created_at ?? null)} 那一轮：命中 {activeSnap?.hit_count ?? 0}　
              4h门 {activeSnap?.stages?.gate_4h ?? 0}　拐头锁 {activeSnap?.stages?.htf_state_lock ?? 0}</span>
            <Button size="small" style={{ marginLeft: 'auto' }} onClick={() => setActiveIdx(null)}>
              回到最新一轮
            </Button>
          </div>
        )}
      </div>

      {/* ---------- 卡片 ---------- */}
      {listLoading && !snaps.length ? (
        <div style={{ textAlign: 'center', padding: 60 }}><Spin /></div>
      ) : !groups.length ? (
        <Empty description="本轮没有被拦币种（全部通过或该轮无漏斗数据）" style={{ marginTop: 30 }} />
      ) : (
        <div style={{
          display: 'grid', gap: 12,
          gridTemplateColumns: 'repeat(auto-fill, minmax(250px, 1fr))',
        }}>
          {groups.map((g) => {
            const share = Math.round((g.n / (total || 1)) * 100)
            return (
              <div
                key={g.key}
                onClick={() => openGroup(g)}
                onMouseEnter={() => setHot(g.key)}
                onMouseLeave={() => setHot(null)}
                style={{
                  position: 'relative', background: '#fff', borderRadius: 12,
                  border: '1px solid #f0f0f0', borderLeft: `3px solid ${g.color}`,
                  padding: '12px 14px', cursor: 'pointer',
                  opacity: hot && hot !== g.key ? 0.5 : 1,
                  boxShadow: hot === g.key ? '0 8px 20px -14px rgba(0,0,0,.5)' : 'none',
                  transition: 'all .14s ease',
                }}
              >
                <div style={{ display: 'flex', gap: 10, alignItems: 'flex-start' }}>
                  <div style={{
                    width: 40, height: 40, borderRadius: '50%', flex: '0 0 auto',
                    background: `conic-gradient(${g.color} ${share * 3.6}deg, #eef1f5 0)`,
                    display: 'grid', placeItems: 'center', position: 'relative',
                  }}>
                    <div style={{
                      position: 'absolute', inset: 5, borderRadius: '50%', background: '#fff',
                      display: 'grid', placeItems: 'center', fontSize: 10.5, color: '#4b5460',
                    }}>{share}%</div>
                  </div>
                  <div style={{ minWidth: 0, flex: 1 }}>
                    <div style={{ fontSize: 12.5, color: '#4b5460' }}>{g.name}</div>
                    <div style={{
                      fontSize: 22, fontWeight: 600, lineHeight: 1.15,
                      fontVariantNumeric: 'tabular-nums',
                    }}>{g.n}</div>
                    <div style={{ fontSize: 11.5, color: '#8c8c8c', marginTop: 3, lineHeight: 1.5 }}>
                      {groupSummary(activeSnap?.stages, g)}
                    </div>
                  </div>
                </div>
                <div style={{ fontSize: 11.5, color: '#1677ff', marginTop: 8 }}>
                  {isHistory ? '历史轮次无币种清单' : '查看币种与卡住原因 ›'}
                </div>
              </div>
            )
          })}
        </div>
      )}

      {/* ---------- 24 轮时序 ---------- */}
      <Timeline
        snaps={snaps}
        activeIdx={activeIdx}
        onPick={(i) => setActiveIdx(i === snaps.length - 1 ? null : i)}
        onHoverGroup={setHot}
        hot={hot}
      />

      {/* ---------- 抽屉：按原因分组（gate_4h 按否定票分组） ---------- */}
      <Drawer
        open={!!drawerGroup}
        onClose={() => setDrawerGroup(null)}
        width={560}
        destroyOnClose
        title={drawerGroup ? (
          <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
            <span style={{
              display: 'inline-block', width: 8, height: 8, borderRadius: 2,
              background: drawerGroup.color,
            }} />
            <span>{drawerGroup.name} · {drawerGroup.n} 个币</span>
            <Text type="secondary" style={{ fontSize: 12, fontWeight: 400 }}>
              {drawerPassers ? '挂掉 / 通过' : vetoDrawer ? '按 4h 否定票分组' : '按卡住原因分组'}
            </Text>
          </div>
        ) : ''}
      >
        {drawerPassHint && (
          <Alert
            type="success"
            showIcon
            style={{ marginBottom: 10 }}
            message="可通过的状态"
            description={drawerPassHint}
          />
        )}
        {drawerStageKey === 'htf_state_lock' && (
          <HtfStateChain activeLabels={activeStateLabels} />
        )}
        {drawerPassers && (
          <div style={{ display: 'flex', gap: 6, marginBottom: 10 }}>
            {([
              { k: 'drop' as const, label: `挂掉 ${drawerGroup?.n ?? 0}` },
              { k: 'pass' as const, label: `通过 ${drawerPassers.length}` },
            ]).map((t) => (
              <button
                key={t.k}
                onClick={() => setDrawerTab(t.k)}
                style={{
                  border: `1px solid ${drawerTab === t.k ? '#1677ff' : '#f0f0f0'}`,
                  background: drawerTab === t.k ? '#f0f5ff' : '#fff',
                  color: drawerTab === t.k ? '#1677ff' : '#595959',
                  borderRadius: 6, padding: '4px 12px', fontSize: 12, cursor: 'pointer',
                }}
              >{t.label}</button>
            ))}
          </div>
        )}
        <div style={{
          fontSize: 12, color: '#8c8c8c', marginBottom: 10, lineHeight: 1.6,
        }}>
          {drawerTab === 'pass' && drawerPassers
            ? passGrouped
              ? '这些币通过了该关（未被本关或更早关卡拒掉，也不含扫描异常），按 4h 放行状态分组；"未记录"为旧轮次或缺 4h 状态的币。点币种名看 K 线。'
              : '这些币通过了该关（未被本关或更早关卡拒掉，也不含扫描异常）；点币种名看 K 线。'
            : vetoDrawer
              ? '每组是一种 4h 否定票（走平 / 缠绕 / ADX / 歧义 / 数据），"×N" 为该票否决的币数；悬停币名看该币证据，点击看 K 线。'
              : '每一条是一个判定条件，"×N" 是该条件卡住的币数；点币种名看 K 线。'}
          {detailLoading && <span style={{ marginLeft: 6 }}>（明细加载中…）</span>}
        </div>
        <Input
          allowClear
          prefix={<SearchOutlined />}
          placeholder="搜币种（如 ETH）"
          value={search}
          onChange={(e) => setSearch(e.target.value)}
          style={{ marginBottom: 12 }}
        />
        {drawerTab === 'pass' && drawerPassers ? (
          passerRows.length ? (
            passGrouped ? (
              passerStateGroups.map((sg) => (
                <div key={sg.label} style={{
                  border: '1px solid #f0f0f0', borderRadius: 10, padding: '10px 12px',
                  marginBottom: 10, background: '#fcfcfd',
                }}>
                  <div style={{ display: 'flex', alignItems: 'baseline', gap: 8 }}>
                    <Text strong style={{ fontSize: 12.5 }}>{sg.label}</Text>
                    <Text type="secondary" style={{ fontSize: 11.5 }}>×{sg.symbols.length}</Text>
                  </div>
                  <div style={{
                    display: 'flex', flexWrap: 'wrap', gap: '8px 14px', marginTop: 8,
                  }}>
                    {sg.symbols.map((sym) => (
                      <a
                        key={sym}
                        onClick={() => setKlineSymbol(sym)}
                        style={{ fontSize: 12, fontFamily: 'ui-monospace, Menlo, monospace' }}
                      >
                        {sym.replace('USDT', '')}
                      </a>
                    ))}
                  </div>
                </div>
              ))
            ) : (
              <div style={{ display: 'flex', flexWrap: 'wrap', gap: '8px 14px' }}>
                {passerRows.map((sym) => (
                  <a
                    key={sym}
                    onClick={() => setKlineSymbol(sym)}
                    style={{ fontSize: 12, fontFamily: 'ui-monospace, Menlo, monospace' }}
                  >
                    {sym.replace('USDT', '')}
                  </a>
                ))}
              </div>
            )
          ) : <Empty description="无匹配币种" style={{ marginTop: 40 }} />
        ) : !reasonGroups.length ? (
          <Empty
            description={drawerRows.length ? '无匹配币种' : '该关卡没有可下钻的币种清单（只有计数）'}
            style={{ marginTop: 40 }}
          />
        ) : reasonGroups.map((rg) => (
          <div key={rg.reason} style={{
            border: '1px solid #f0f0f0', borderRadius: 10, padding: '10px 12px',
            marginBottom: 10, background: '#fcfcfd',
          }}>
            <div style={{
              fontSize: 12.5, lineHeight: 1.65, color: '#434343', whiteSpace: 'normal',
              wordBreak: 'break-word',
            }}>
              {rg.reason.split('；').map((seg, i) => (
                <div key={i}>{seg}</div>
              ))}
            </div>
            <div style={{
              display: 'flex', flexWrap: 'wrap', gap: '6px 12px', marginTop: 8,
              alignItems: 'center',
            }}>
              <Text type="secondary" style={{ fontSize: 11.5 }}>×{rg.coins.length}</Text>
              {rg.coins.map((c) => (
                <a
                  key={c.symbol}
                  title={c.note ? `${c.symbol} · ${c.note}` : c.symbol}
                  onClick={() => setKlineSymbol(c.symbol)}
                  style={{ fontSize: 12, fontFamily: 'ui-monospace, Menlo, monospace' }}
                >
                  {c.symbol.replace('USDT', '')}
                </a>
              ))}
            </div>
          </div>
        ))}
      </Drawer>

      {/* ---------- K 线 ---------- */}
      <Modal
        title={`${klineSymbol ?? ''} K线（1h）`}
        open={!!klineSymbol}
        onCancel={() => setKlineSymbol(null)}
        footer={null}
        width={1200}
        destroyOnClose
      >
        {klineSymbol && (
          <div style={{ height: 640 }}>
            <KlineChart symbol={klineSymbol} limit={500} />
          </div>
        )}
      </Modal>
    </div>
  )
}

/* ------------------------------------------------------------------ */

interface TimelineProps {
  snaps: FunnelSnapshot[]
  activeIdx: number | null
  onPick: (i: number) => void
  hot: string | null
  onHoverGroup: (k: string | null) => void
}

/** 24 轮时序：每根柱按 8 组堆叠；柱下数字为该轮命中数（0 显示「·」，
 *  避免把"真的没有"读成"数据缺失"）。点击切轮次。 */
function Timeline({ snaps, activeIdx, onPick, hot, onHoverGroup }: TimelineProps) {
  if (!snaps.length) return null
  const totals = snaps.map((s) => GROUPS.reduce((a, g) => a + sumStages(s.stages, g.stages), 0))
  const maxTotal = Math.max(1, ...totals)
  const lastIdx = snaps.length - 1

  return (
    <div style={{
      background: '#fff', border: '1px solid #f0f0f0', borderRadius: 12,
      padding: '12px 14px 8px',
    }}>
      <div style={{ display: 'flex', alignItems: 'baseline', gap: 10, marginBottom: 4 }}>
        <Text strong style={{ fontSize: 13 }}>最近 {snaps.length} 轮</Text>
        <Text type="secondary" style={{ fontSize: 11.5 }}>
          柱高 = 该轮各关卡拦掉的币数 · 柱下数字 = 该轮命中数 · 点柱子回看那一轮
        </Text>
      </div>
      <div style={{ display: 'flex', alignItems: 'flex-end', gap: 3, height: 118, marginTop: 6 }}>
        {snaps.map((s, i) => {
          const sel = activeIdx === null ? i === lastIdx : i === activeIdx
          return (
            <Tooltip
              key={s.scan_id}
              title={`${fmtTime(s.created_at)} · 命中 ${s.hit_count}　
4h门 ${s.stages?.gate_4h ?? 0} · 拐头锁 ${s.stages?.htf_state_lock ?? 0} ·
触发层 ${sumStages(s.stages, GROUPS[3].stages)} · 待确认失效 ${sumStages(s.stages, GROUPS[4].stages)}`}
            >
              <div
                onClick={() => onPick(i)}
                onMouseEnter={() => onHoverGroup(null)}
                style={{
                  flex: 1, height: '100%', cursor: 'pointer', display: 'flex',
                  flexDirection: 'column-reverse', gap: 1, borderRadius: 3,
                  background: sel ? '#f0f5ff' : undefined,
                }}
              >
                {GROUPS.map((g) => {
                  const n = sumStages(s.stages, g.stages)
                  if (!n) return null
                  return (
                    <div key={g.key} style={{
                      height: `${(n / maxTotal) * 100}%`, background: g.color,
                      borderRadius: 2, opacity: hot && hot !== g.key ? 0.35 : 1,
                      outline: sel ? '1px solid rgba(0,0,0,.12)' : undefined,
                    }} />
                  )
                })}
              </div>
            </Tooltip>
          )
        })}
      </div>
      <div style={{ display: 'flex', gap: 3, marginTop: 5 }}>
        {snaps.map((s) => (
          <div key={s.scan_id} style={{
            flex: 1, textAlign: 'center', fontSize: 10,
            color: s.hit_count > 0 ? '#434343' : '#d9d9d9',
            fontVariantNumeric: 'tabular-nums',
          }}>{s.hit_count > 0 ? s.hit_count : '·'}</div>
        ))}
      </div>
      <div style={{ display: 'flex', gap: 3, marginTop: 1 }}>
        {snaps.map((s, i) => (
          <div key={s.scan_id} style={{
            flex: 1, textAlign: 'center', fontSize: 10, color: '#bfbfbf', whiteSpace: 'nowrap',
          }}>{i % 4 === 0 ? fmtTime(s.created_at).slice(6) : ''}</div>
        ))}
      </div>
      <div style={{
        display: 'flex', gap: 14, flexWrap: 'wrap', marginTop: 10,
        paddingTop: 8, borderTop: '1px dashed #f0f0f0', fontSize: 11.5, color: '#595959',
      }}>
        {GROUPS.map((g) => (
          <span key={g.key} style={{ display: 'inline-flex', alignItems: 'center', gap: 5 }}>
            <i style={{ width: 8, height: 8, borderRadius: 2, background: g.color }} />
            {g.name}
          </span>
        ))}
        <span style={{ marginLeft: 'auto', color: '#bfbfbf' }}>
          命中数为 0 的轮次显示「·」；图例顺序固定，便于跨轮对比
        </span>
      </div>
    </div>
  )
}
