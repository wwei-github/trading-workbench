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
import KlineChart from './KlineChart'
import type { FunnelData } from '../types'

const { Text } = Typography

// 关卡元数据（顺序即漏斗逻辑序；代码与 scanner.py reject_out 语义一一对应）
const FUNNEL_STAGES: { key: string; label: string; color: string; hint: string }[] = [
  { key: 'liquidity', label: '流动性', color: 'default', hint: '24h 成交额低于门槛，不进扫描' },
  { key: 'untradable', label: '不可交易', color: 'default', hint: '（2026-10-11 退役）原下单环境未上架预过滤，已撤——未上架币照常分析，开单时逐网校验并飞书提示；仅旧轮次数据可见' },
  { key: 'new_coin', label: '新币冷静期', color: 'default', hint: '上市未满 N 天（默认 14 天）不进扫描' },
  { key: 'risk_tier', label: '风险档位', color: 'default', hint: '交易所最大允许杠杆 <10×' },
  { key: 'blacklist', label: '黑名单', color: 'default', hint: '手动添加（永久）或止损超预算自动加入（1.2R 冷却 30 天 / ≥1.5R 永久）' },
  { key: 'gate_4h', label: '4h趋势闸门', color: 'orange', hint: '4h 无明确趋势（EMA144 斜率不足/缠绕/ADX<20）一票否决' },
  { key: 'htf_state_lock', label: '4h状态禁开', color: 'orange', hint: '4h EMA 状态机双向禁开（缠绕/拐头）——回踩·突破均一票否决' },
  { key: 'votes', label: '指标多数决', color: 'orange', hint: '票数不足（voted<5）或方向中性（|net|<3）' },
  { key: 'direction_flip', label: '方向冲突', color: 'orange', hint: '指标多数决方向与 4h 闸门方向相反' },
  { key: 'structure_1h', label: '1h结构', color: 'orange', hint: '1h 三线排列 / 快中线发散度 / EMA144 方向不成立' },
  { key: 'range_score', label: '震荡评分', color: 'orange', hint: '震荡评分卡 ≥3（强震荡共识）' },
  { key: 'no_trigger', label: '无触发', color: 'purple', hint: '通过全部门但无触发信号，也未进待确认池；判定说明列为具体未过条件（新轮次）' },
  { key: 'trigger_whitelist', label: '触发白名单外', color: 'purple', hint: '触发类型不在白名单（筛选配置）' },
]
const STAGE_ORDER = Object.fromEntries(FUNNEL_STAGES.map((s, i) => [s.key, i]))
const STAGE_META = Object.fromEntries(FUNNEL_STAGES.map((s) => [s.key, s]))

interface FunnelRow {
  symbol: string
  stage: string
  label: string
  color: string
  hint: string
  detail?: string // 判定说明明细（votes 票明细 / no_trigger 未触发原因，2026-10-13；分号分隔）
}

export default function FunnelPanel({ initialStage }: { initialStage?: string } = {}) {
  const [data, setData] = useState<FunnelData | null>(null)
  const [loading, setLoading] = useState(false)
  const [klineSymbol, setKlineSymbol] = useState<string | null>(null)
  const currentScanId = useScanStore((s) => s.currentScanId)
  const isScanning = !!useScanStore((s) => s.status?.is_scanning)

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

  useEffect(() => {
    fetchData(currentScanId)
    // 换轮次重置筛选/搜索：旧轮选中的关卡可能在新轮无被拒币，残留值会让
    // Select 显示裸关卡代码（options 无该项）且列表恒空。initialStage（无触发
    // 页签）作为重置基准，不写死 'all'
    setStageFilter(initialStage ?? 'all')
    setSearch('')
  }, [currentScanId, fetchData, initialStage])

  // 扫描完成（进行中→结束翻转）自动刷新；ref 记前值防挂载时双请求
  const prevScanning = useRef(false)
  useEffect(() => {
    if (prevScanning.current && !isScanning && currentScanId) fetchData(currentScanId)
    prevScanning.current = isScanning
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [isScanning])

  // 被拒明细摊平为列表：按关卡逻辑序排列，同关卡内按币种字母序
  const rows: FunnelRow[] = useMemo(() => {
    if (!data) return []
    return Object.entries(data.rejects)
      .map(([symbol, stage]) => {
        const m = STAGE_META[stage]
        return {
          symbol,
          stage,
          label: m?.label ?? stage,
          color: m?.color ?? 'default',
          hint: m?.hint ?? '',
          detail: data.details?.[symbol],
        }
      })
      .sort((a, b) =>
        (STAGE_ORDER[a.stage] ?? 99) - (STAGE_ORDER[b.stage] ?? 99)
        || a.symbol.localeCompare(b.symbol))
  }, [data])

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
  const filteredRows = useMemo(() => {
    const kw = search.trim().toUpperCase()
    return rows.filter((r) =>
      (stageFilter === 'all' || r.stage === stageFilter)
      && (!kw || r.symbol.includes(kw)))
  }, [rows, stageFilter, search])

  const columns: ColumnsType<FunnelRow> = [
    { title: '币种', dataIndex: 'symbol', key: 'symbol', width: 140,
      render: (v: string) => <a onClick={() => setKlineSymbol(v)}>{v}</a> },
    { title: '卡住关卡', dataIndex: 'stage', key: 'stage', width: 150,
      render: (_: string, r: FunnelRow) => (
        <Tooltip title={r.hint}><Tag color={r.color}>{r.label}</Tag></Tooltip>
      ) },
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
                whiteSpace: r.stage === 'no_trigger' ? 'normal' : 'nowrap',
              }}>{seg}</div>
            ))}
          </div>
        </Tooltip>
      ) : (
        <Text type="secondary" style={{ fontSize: 12 }}>{r.hint}</Text>
      ) },
  ]

  if (!currentScanId) {
    return <Empty description="暂无扫描记录，先执行一次扫描" style={{ marginTop: 80 }} />
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
                { label: '待确认池', value: data.pending_pool, color: '#13c2c2', hint: '回踩触碰但当根无金K，转入待确认池等确认（不算被拒）' },
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
                    onClick={() => fetchData(currentScanId)}>
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
