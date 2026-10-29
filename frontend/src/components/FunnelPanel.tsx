/**
 * 筛选漏斗（2026-10-11）：展示每轮扫描各币种卡在哪一关
 * - 数据源：GET /api/scans/{scanId}/funnel（ScanRecord.funnel_json，扫描完成时落库）
 * - 顶部漏斗概览：全市场 → 进管线 → 候选（进 AI），旁路待确认池/异常
 * - 主体被拒币种列表（Table，按漏斗逻辑序排列）：币种列点击弹K线复盘
 * - 进待确认池的币不算被拒（见同面板「待确认池」页签）；旧版本扫描/异常轮次无数据
 * - initialStage（2026-10-12）：预置关卡筛选，供「无触发」页签直接落在 no_trigger 上；
 *   该页签（2026-10-13）隐藏顶部漏斗概览（全市场/进管线等整体计数与单币未过条件无关），
 *   并隐藏关卡筛选下拉（去掉筛选、固定列表为 no_trigger 分类），仅保留币种搜索
 * - 无触发原因（2026-10-13）：no_trigger 关判定说明列展示后端采集的未过条件明细
 *   （突破卡点/触点缺失/待确认失效等，分号分隔拆行），替代静态 hint
 */
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { Button, Empty, Input, Modal, Select, Spin, Table, Tag, Tooltip, Typography, message } from 'antd'
import { ReloadOutlined } from '@ant-design/icons'
import type { ColumnsType } from 'antd/es/table'
import { scanApi } from '../api/scan'
import { useScanStore } from '../stores/scanStore'
import { qualityPartsSummary } from './ResultTable'
import KlineChart from './KlineChart'
import type { FunnelData } from '../types'

const { Text } = Typography

import { FUNNEL_STAGES, STAGE_META, STAGE_ORDER, isNoTriggerStage } from '../constants/funnelStages'

interface FunnelRow {
  symbol: string
  stage: string
  label: string
  color: string
  hint: string
  detail?: string // 判定说明明细（votes 票明细 / no_trigger 未触发原因，2026-10-13；分号分隔）
  legAtr?: number | null // 反向腿深度（ATR 倍数，docs/23 T9；仅 leg_gate 关有值）
}

export default function FunnelPanel({ initialStage }: { initialStage?: string } = {}) {
  const [data, setData] = useState<FunnelData | null>(null)
  const [loading, setLoading] = useState(false)
  const [klineSymbol, setKlineSymbol] = useState<string | null>(null)
  const currentScanId = useScanStore((s) => s.currentScanId)
  const isScanning = !!useScanStore((s) => s.status?.is_scanning)
  // 兜底轮次（2026-10-28）：刷新后 currentScanId 为 null（仅内存态），且最新一轮
  // 0 命中时 scanStore 推不出 id（items[0].scan_record_id 取不到）——旧守卫会把
  // 已有漏斗数据整块隐藏。这里回落到 /scans/funnels 里最近一个有漏斗数据的轮次。
  const [fallbackId, setFallbackId] = useState<string | null>(null)
  const [resolving, setResolving] = useState(false)

  const fetchData = useCallback(async (scanId: string | null) => {
    if (!scanId) return
    setLoading(true)
    try {
      setData((await scanApi.funnel(scanId)).funnel)
    } catch (e: any) {
      message.error(e?.response?.data?.detail || '获取筛选漏斗失败')
    } finally {
      setLoading(false)
    }
  }, [])

  // 兜底解析：近 24h 最近一轮有漏斗数据的扫描（刷新后 currentScanId 缺失时使用）
  const resolveFallback = useCallback(async () => {
    setResolving(true)
    try {
      const r = await scanApi.funnels(24)
      const items = (r.items || []).filter(
        (it) => it.scan_id && Object.keys(it.stages || {}).length > 0)
      const latest = items[items.length - 1]?.scan_id ?? null
      setFallbackId(latest)
      if (latest) await fetchData(latest)
    } catch {
      setFallbackId(null)
    } finally {
      setResolving(false)
    }
  }, [fetchData])

  useEffect(() => {
    // 换轮次重置筛选/搜索：旧轮选中的关卡可能在新轮无被拒币，残留值会让
    // Select 显示裸关卡代码（options 无该项）且列表恒空。initialStage（无触发
    // 页签）作为重置基准，不写死 'all'
    setStageFilter(initialStage ?? 'all')
    setSearch('')
    setLegBand('all')
    if (currentScanId) {
      setFallbackId(null)
      fetchData(currentScanId)
      return
    }
    // 无当前轮次（刷新后 / 最新一轮 0 命中）→ 回落到最近一轮有漏斗数据的扫描
    void resolveFallback()
  }, [currentScanId, fetchData, initialStage, resolveFallback])

  // 扫描完成（进行中→结束翻转）自动刷新；ref 记前值防挂载时双请求
  const prevScanning = useRef(false)
  useEffect(() => {
    if (prevScanning.current && !isScanning) {
      if (currentScanId) fetchData(currentScanId)
      else void resolveFallback()
    }
    prevScanning.current = isScanning
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [isScanning])

  // 质量分下限被拦候选的原始快照（symbol → score/parts，漏斗落库前快照）
  const rawBySymbol = useMemo(() => {
    const m = new Map<string, { score?: number | null; parts?: Record<string, unknown> }>()
    for (const c of data?.raw_candidates ?? []) {
      if (c.symbol) m.set(c.symbol, c)
    }
    return m
  }, [data])

  // 被拒明细摊平为列表：按关卡逻辑序排列，同关卡内按币种字母序
  const rows: FunnelRow[] = useMemo(() => {
    if (!data) return []
    return Object.entries(data.rejects)
      .map(([symbol, stage]) => {
        const m = STAGE_META[stage]
        let detail = data.details?.[symbol]
        // 质量分下限（2026-10-28）：被砍候选不落 scan_results，用 raw_candidates
        // 快照补判定说明——差多少分、哪个分项拖的、箱体几何（宽度/实体重叠）
        const raw = stage === 'quality_floor' ? rawBySymbol.get(symbol) : undefined
        if (!detail && raw) {
          const parts = raw.parts as Record<string, number> | undefined
          const segs = qualityPartsSummary(parts)
          const geo = parts && typeof parts.box_atr === 'number'
            ? `箱体几何：箱宽 ${parts.box_atr}×ATR14 / 相邻实体平均重叠 ${typeof parts.box_overlap === 'number' ? parts.box_overlap : '—'}`
            : ''
          detail = [
            `质量分 ${raw.score ?? '—'}（低于下限）`,
            segs ? `分项：${segs}` : '',
            geo,
          ].filter(Boolean).join('；')
        }
        // 腿门证据（docs/23 T9）：判定说明里已有「腿深 x.xxATR」文本，这里取
        // 结构化数值供档位筛选；老轮次无 leg_evidence → null（归「无腿证据」档）
        const leg = data.leg_evidence?.[symbol]
        return {
          symbol,
          stage,
          label: m?.label ?? stage,
          color: m?.color ?? 'default',
          hint: m?.hint ?? '',
          detail,
          legAtr: typeof leg?.atr_mult === 'number' ? leg.atr_mult : null,
        }
      })
      .sort((a, b) =>
        (STAGE_ORDER[a.stage] ?? 99) - (STAGE_ORDER[b.stage] ?? 99)
        || a.symbol.localeCompare(b.symbol))
  }, [data, rawBySymbol])

  // 各关卡计数（概览统计块 + 关卡筛选下拉共用）
  const stageCounts = useMemo(() => {
    if (!data) return []
    return FUNNEL_STAGES
      .map((st) => ({ ...st, count: data.stages[st.key] ?? 0 }))
      .filter((g) => g.count > 0)
  }, [data])

  // 关卡筛选 + 币种搜索（大小写不敏感的包含匹配）
  const [stageFilter, setStageFilter] = useState<string>('all')
  const [search, setSearch] = useState<string>('')
  // 腿深档位（docs/23 T9，2026-10-29）：「无触发」页签专用。观察期要看腿门
  // min_atr 降到 0.3 能多放出多少候选、那批候选的期望值——按 0.5/0.8 分档即可
  // 直接对照判据（0.3 档增量候选 avgR ≥ 0.1R 且胜率 ≥ 45%）
  const [legBand, setLegBand] = useState<'all' | 'lt05' | 'mid' | 'ge08' | 'none'>('all')
  const filteredRows = useMemo(() => {
    const kw = search.trim().toUpperCase()
    return rows.filter((r) => {
      if (stageFilter !== 'all') {
        const okStage = stageFilter === 'no_trigger'
          ? isNoTriggerStage(r.stage)
          : r.stage === stageFilter
        if (!okStage) return false
      }
      if (kw && !r.symbol.includes(kw)) return false
      if (legBand === 'all') return true
      const v = r.legAtr
      if (legBand === 'none') return v == null
      if (v == null) return false
      if (legBand === 'lt05') return v < 0.5
      if (legBand === 'mid') return v >= 0.5 && v < 0.8
      return v >= 0.8
    })
  }, [rows, stageFilter, search, legBand])

  const columns: ColumnsType<FunnelRow> = [
    { title: '币种', dataIndex: 'symbol', key: 'symbol', width: 140,
      render: (v: string) => <a onClick={() => setKlineSymbol(v)}>{v}</a> },
    { title: '卡住关卡', dataIndex: 'stage', key: 'stage', width: 150,
      render: (_: string, r: FunnelRow) => {
        const tag = <Tag color={r.color}>{r.label}</Tag>
        // 「无触发」页去掉浮动提示（2026-10-13）：未过条件已在「判定说明」列展示，
        // Tag 悬停 tooltip 冗余；漏斗页保留（无明细关的 hint 悬停速览）
        return initialStage === 'no_trigger' ? tag : <Tooltip title={r.hint}>{tag}</Tooltip>
      } },
    // 腿深列（docs/23 T9）：「无触发」页签专用——只有 leg_gate 关有数值，
    // 其余未过条件的币显「—」（档位选「无腿证据」即可把它们单独筛出来）
    ...(initialStage === 'no_trigger' ? [{
      title: '腿深',
      dataIndex: 'legAtr',
      key: 'legAtr',
      width: 110,
      render: (v: number | null | undefined) => (
        v == null
          ? <Text type="secondary" style={{ fontSize: 12 }}>—</Text>
          : <span style={{ fontVariantNumeric: 'tabular-nums', fontSize: 12 }}>
              {v.toFixed(2)}×ATR
            </span>
      ),
    }] as ColumnsType<FunnelRow> : []),
    { title: '判定说明', key: 'hint',
      render: (_: unknown, r: FunnelRow) => r.detail ? (
        // 明细行（后端渲染，分号分隔拆行）：votes 关=全部票明细（▲多/▼空/
        // —弃权，单行不折）；no_trigger 关=未过条件（2026-10-13，长文本折行）。
        // 静态 hint（为何被拒）放悬停
        <Tooltip title={r.hint}>
          <div>
            {r.detail.split('；').map((seg, i) => (
              <div key={i} style={{
                fontSize: 12, lineHeight: '18px',
                whiteSpace: isNoTriggerStage(r.stage) ? 'normal' : 'nowrap',
              }}>{seg}</div>
            ))}
          </div>
        </Tooltip>
      ) : (
        <Text type="secondary" style={{ fontSize: 12 }}>{r.hint}</Text>
      ) },
  ]

  // 无当前轮次且兜底仍在解析/详情加载 → Spin，避免先闪「暂无扫描记录」
  if (!currentScanId && !fallbackId) {
    if (resolving || loading) {
      return <div style={{ textAlign: 'center', padding: 60 }}><Spin /></div>
    }
    return <Empty description="近 24 小时暂无扫描记录，先执行一次扫描" style={{ marginTop: 80 }} />
  }

  return (
    <div style={{ padding: '0 4px', display: 'flex', flexDirection: 'column', gap: 12 }}>
      {loading && !data ? (
        <div style={{ textAlign: 'center', padding: 60 }}><Spin /></div>
      ) : !data ? (
        <Empty description="该轮无漏斗数据（旧版本扫描或异常轮次）" style={{ marginTop: 60 }} />
      ) : (
        <>
          {/* 漏斗概览：统计块 + 漏斗箭头（「无触发」页签隐藏，2026-10-13：
              全市场/进管线等整体漏斗信息与单币未过条件无关） */}
          {initialStage !== 'no_trigger' && (
            <div style={{
              display: 'flex', flexWrap: 'wrap', gap: 12, alignItems: 'center',
              padding: '10px 16px', background: '#fafafa', borderRadius: 8, border: '1px solid #f0f0f0',
            }}>
              {([
                { label: '全市场', value: data.universe, color: '#1677ff', hint: '全市场 USDT 永续合约数' },
                { label: '进管线', value: data.scanned, color: '#1677ff', hint: '预过滤（流动性/新币/风险档位/黑名单）后进指标管线数；未上架币不再过滤，开单时逐网校验' },
                { label: '候选进AI', value: data.candidates, color: '#52c41a', hint: '通过全部筛选关卡、产生信号候选并进入 AI 分析' },
                { label: '本轮进池', value: data.pending_pool, color: '#13c2c2', hint: '回踩触碰但当根无金K，本轮新建/刷新待确认（含刷新，不是池内总数；不算被拒）' },
                ...(data.errors > 0
                  ? [{ label: '异常', value: data.errors, color: '#ff4d4f', hint: '拉K线/指标计算异常数' }]
                  : []),
              ] as { label: string; value: number; color: string; hint: string }[]).flatMap((s, i) => (
                i === 0
                  ? [<StatBlock key={s.label} {...s} />]
                  : [<Text key={`a-${s.label}`} type="secondary">→</Text>, <StatBlock key={s.label} {...s} />]
              ))}
            </div>
          )}

          {/* 工具栏：关卡筛选（「无触发」页签隐藏，2026-10-13：该页签去掉筛选、
              固定列表为 no_trigger 分类，仅保留搜索）+ 币种搜索 + 刷新 */}
          <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap', alignItems: 'center' }}>
            {initialStage !== 'no_trigger' && (
              <Select
                value={stageFilter}
                onChange={setStageFilter}
                size="small"
                style={{ minWidth: 170 }}
                options={[
                  { value: 'all', label: `全部关卡 (${rows.length})` },
                  ...stageCounts.map((g) => ({ value: g.key, label: `${g.label} (${g.count})` })),
                ]}
              />
            )}
            {/* 腿深档位（docs/23 T9）：「无触发」页签按腿门深度分档，观察期
                用 0.3 档增量候选的 avgR/胜率对照判据（docs/23 §3.4） */}
            {initialStage === 'no_trigger' && (
              <Tooltip title="按反向腿门深度分档：观察 min_atr 降到 0.3 会多放出哪些候选">
                <Select
                  value={legBand}
                  onChange={setLegBand}
                  size="small"
                  style={{ minWidth: 180 }}
                  options={[
                    { value: 'all', label: '全部腿深' },
                    { value: 'lt05', label: '腿深 <0.5ATR' },
                    { value: 'mid', label: '腿深 0.5~0.8ATR' },
                    { value: 'ge08', label: '腿深 ≥0.8ATR' },
                    { value: 'none', label: '无腿证据' },
                  ]}
                />
              </Tooltip>
            )}
            <Input
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              placeholder="搜索币种，如 BTC"
              size="small"
              allowClear
              style={{ width: 200 }}
            />
            <span style={{ flex: 1 }} />
            {isScanning && <Text type="secondary" style={{ fontSize: 12 }}>扫描进行中，完成后自动刷新…</Text>}
            <Button size="small" icon={<ReloadOutlined />} loading={loading}
                    onClick={() => fetchData(currentScanId ?? fallbackId)}>
              刷新
            </Button>
          </div>

          <Table<FunnelRow>
            rowKey="symbol"
            size="small"
            columns={columns}
            dataSource={filteredRows}
            loading={loading}
            pagination={{ pageSize: 50, showSizeChanger: false, showTotal: (n) => `共 ${n} 个` }}
            locale={{ emptyText: <Empty
              description={
                rows.length === 0
                  ? (initialStage === 'no_trigger' ? '本轮无未触发币种' : '本轮没有被拒币种')
                  : (initialStage === 'no_trigger'
                    ? '无匹配币种（调整搜索词）'
                    : '无匹配币种（调整关卡筛选或搜索词）')
              }
              style={{ marginTop: 40 }} /> }}
          />
        </>
      )}

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

// 漏斗概览统计块：大数字 + 小标签，悬停显示口径说明
function StatBlock({ label, value, color, hint }: { label: string; value: number; color: string; hint: string }) {
  return (
    <Tooltip title={hint}>
      <div style={{ textAlign: 'center', minWidth: 72, cursor: 'default' }}>
        <div style={{ fontSize: 20, fontWeight: 600, lineHeight: '24px', color }}>{value}</div>
        <div style={{ fontSize: 12, color: '#8c8c8c' }}>{label}</div>
      </div>
    </Tooltip>
  )
}
