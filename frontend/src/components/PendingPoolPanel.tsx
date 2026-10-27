/**
 * 回踩两段式待确认池（docs/19，2026-09-25）
 * - Stage-1 回踩触碰但当根无金K 的币种转入 pending，等 N 根确认窗口内金K确认（默认 3 根）
 * - 状态流转：pending（等待中）→ confirmed（已确认进 AI）/ expired（无金K或方向失守）/ superseded（突破或当根确认抢先）
 * - 展示分段（2026-10-15）：待确认 / 后续结果 / 命中历史 / 无触发 / 筛选漏斗。
 *   「后续结果」由原「已确认/已卡掉/全部」三 tab 合并而来——展示待确认币种的结论
 *   （confirmed=已进 AI / expired·superseded=卡掉，行内「状态」列区分），仅保留
 *   最近三天（按 resolved_at，缺失回落 updated_at）；被哪个环节卡住看「卡住环节」列（后端 blocked_by）
 * - 筛选漏斗（2026-10-11）：Segmented 项，展示每轮被拒币种卡在哪一关（FunnelPanel）
 * - 无触发（2026-10-13）：Segmented 项复用 FunnelPanel 预置 no_trigger 关、
 *   隐藏漏斗概览与关卡筛选（仅保留搜索）——单独查看未触发币种及其未过条件
 * - 数据源：GET /api/scans/pullback-pendings（一次拉全量，前端按状态过滤）
 */
import { useCallback, useEffect, useMemo, useState } from 'react'
import { Button, Empty, Modal, Segmented, Spin, Table, Tag, Tooltip, Typography, message } from 'antd'
import { ReloadOutlined } from '@ant-design/icons'
import type { ColumnsType } from 'antd/es/table'
import { scanApi } from '../api/scan'
import { bj } from '../utils/dayjs'
import { patternStyle } from '../constants/labels'
import { schemeTag } from '../utils/scheme'
import { useScanStore } from '../stores/scanStore'
import KlineChart from './KlineChart'
import FunnelPanel from './FunnelPanel'
import HitHistoryPanel from './HitHistoryPanel'
import type { PullbackPendingItem } from '../types'

const { Text, Link } = Typography

type StatusFilter = 'pending' | 'outcome' | 'hit-history' | 'funnel' | 'no-trigger'

// 行内「状态」列口径（2026-10-10 归并，2026-10-13 分段收敛后沿用）：pending=待确认、
// confirmed=已确认（已进 AI）；expired/superseded 一律归并「已卡掉」，
// 具体被哪个环节卡住由「卡住环节」列展示（后端 blocked_by 字段）
const STATUS_META: Record<string, { color: string; label: string; hint: string }> = {
  pending: {
    color: 'processing', label: '待确认',
    hint: '回踩触碰进入确认窗口（默认 3 根），窗口内出现同向金K即确认入场；窗口耗尽无金K则过期',
  },
  confirmed: {
    color: 'success', label: '已确认',
    hint: '确认窗口内出现同向金K，以「回踩金K确认」信号落库并进入 AI 分析',
  },
  expired: {
    color: 'default', label: '已卡掉',
    hint: '被判定链某环节卡掉：ranging=4h 无明确趋势；range_score=震荡闸门命中（三线交错/分离过近/ADX<18/过度扩张）；window_expired=确认窗口内无金K确认失效；direction_flip=指标方向失守作废；htf_state_lock=4h 非排列状态禁开（拐头/交叉初期/恢复初期/过渡态），候选一票否决、待确认同轮失效',
  },
  superseded: {
    color: 'default', label: '已卡掉',
    hint: '被更优路径抢先（同样算卡掉）：breakout=Donchian 突破抢先触发；same_candle_confirm=当根触碰+金K直接确认',
  },
}

const REASON_LABEL: Record<string, string> = {
  ranging: '4h无明确趋势',
  structure_1h: '1H结构不符（旧轮次）',
  range_score: '震荡闸门命中',
  window_expired: '窗口内无金K',
  direction_flip: '方向失守',
  htf_state_lock: '4h状态禁开',
  breakout: '突破抢先',
  same_candle_confirm: '当根确认抢先',
}

// 触点方向化措辞（与后端 trigger._touch_ref_label 同口径）：
// 「等待确认」列拼接「窗口内看涨金K·触点摆动低点」这类待确认内容
function touchWaitLabel(touch_ref: string | null, direction: 1 | -1): string {
  if (!touch_ref) return ''
  if (touch_ref === 'retest') return direction === 1 ? '前高' : '前低'
  if (touch_ref === 'swing') return direction === 1 ? '摆动低点' : '摆动高点'
  if (touch_ref === 'ema21_4h') return 'EMA21(4h)'
  return `EMA${touch_ref.replace('ema', '')}`
}

function fmtTime(s: string | null | undefined): string {
  if (!s) return '-'
  return bj(s).format('MM-DD HH:mm')
}

// 「后续结果」仅保留最近三天（2026-10-13）：待确认币种的终态结论随时间累积，
// 太久远的无复盘价值，故按结论时间窗口收敛
const OUTCOME_DAYS = 3

/** 终态行（confirmed/expired/superseded）结论时间在最近 OUTCOME_DAYS 天内；
 * 结论时间以 resolved_at 为准，缺失回落 updated_at；仍 pending 的行不算结果 */
function withinOutcomeWindow(r: PullbackPendingItem, cutoffMs: number): boolean {
  if (r.status === 'pending') return false
  const t = r.resolved_at || r.updated_at
  return !!t && bj(t).valueOf() >= cutoffMs
}

export default function PendingPoolPanel() {
  const [rows, setRows] = useState<PullbackPendingItem[]>([])
  const [loading, setLoading] = useState(false)
  const [filter, setFilter] = useState<StatusFilter>('pending')
  // 弹窗展示的币种 K 线（null=关闭）；所有 tab 的币种名称均点击可看
  // 选中行整行保留（2026-10-27，docs/15 §19）：图上只标「这一笔」的命中
  // （信号K = pending 建立时刻、触碰K = Stage-1），不再显示该币全部命中历史
  const [klineRow, setKlineRow] = useState<PullbackPendingItem | null>(null)
  const colorScheme = useScanStore((s) => s.colorScheme)

  const fetchData = useCallback(async () => {
    setLoading(true)
    try {
      setRows(await scanApi.pullbackPendings.list('all', 500))
    } catch (e: any) {
      message.error(e?.response?.data?.detail || '获取待确认池失败')
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    fetchData()
  }, [fetchData])

  const counts = useMemo(() => {
    const cutoff = bj().subtract(OUTCOME_DAYS, 'day').valueOf()
    return {
      pending: rows.filter((r) => r.status === 'pending').length,
      outcome: rows.filter((r) => withinOutcomeWindow(r, cutoff)).length,
    }
  }, [rows])

  const filtered = useMemo(() => {
    if (filter === 'outcome') {
      const cutoff = bj().subtract(OUTCOME_DAYS, 'day').valueOf()
      return rows.filter((r) => withinOutcomeWindow(r, cutoff))
    }
    return rows.filter((r) => r.status === filter)
  }, [rows, filter])

  const columns: ColumnsType<PullbackPendingItem> = [
    {
      title: '币种', dataIndex: 'symbol', key: 'symbol', width: 130,
      render: (v: string, r) => (
        <Tooltip title="点击查看该笔待确认的命中K线">
          <Link strong onClick={() => setKlineRow(r)}>{v}</Link>
        </Tooltip>
      ),
    },
    {
      title: '方向', dataIndex: 'direction', key: 'direction', width: 90,
      render: (v: 1 | -1) =>
        v === 1
          ? <Tag color={schemeTag(colorScheme).up}>看多</Tag>
          : <Tag color={schemeTag(colorScheme).down}>看空</Tag>,
    },
    {
      title: '状态', dataIndex: 'status', key: 'status', width: 110,
      render: (v: string) => {
        const meta = STATUS_META[v] || { color: 'default', label: v, hint: '' }
        return (
          <Tooltip title={meta.hint}>
            <Tag color={meta.color}>{meta.label}</Tag>
          </Tooltip>
        )
      },
    },
    {
      title: '等待确认', key: 'await_confirm', width: 170,
      render: (_, r) => {
        if (r.status !== 'pending') return '-'
        const side = r.direction === 1 ? '看涨' : '看跌'
        const t = touchWaitLabel(r.touch_ref, r.direction)
        return (
          <Tooltip title="确认窗口为触碰K线之后的 N 根已收盘K线（默认 3 根）：窗口内出现同向金K即确认入场；窗口耗尽无金K则过期，突破/当根确认则让位">
            <Text>{`窗口内${side}金K${t ? `·触点${t}` : ''}`}</Text>
          </Tooltip>
        )
      },
    },
    {
      title: '卡住环节', dataIndex: 'blocked_by', key: 'blocked_by', width: 120,
      render: (v: string | null, r) => {
        // 已确认=活进 AI；待确认=还没有结论；其余（expired/superseded）都算被卡住，展示卡在哪个环节
        if (r.status === 'confirmed') return <Text type="success">已进 AI 分析</Text>
        if (r.status === 'pending') return <Text type="secondary">等待金K确认</Text>
        if (!v) return '-'
        return (
          <Tooltip title={`卡在：${REASON_LABEL[v] || v}`}>
            <Tag color="volcano" style={{ marginInlineEnd: 0 }}>{REASON_LABEL[v] || v}</Tag>
          </Tooltip>
        )
      },
    },
    {
      title: '确认形态', dataIndex: 'confirm_pattern', key: 'confirm_pattern', width: 110,
      render: (v: string | null) => {
        if (!v) return '-'
        const s = patternStyle(v, colorScheme)
        return <Tag color={s.color}>{s.label}</Tag>
      },
    },
    {
      title: '触碰EMA21', dataIndex: 'ema21', key: 'ema21', width: 110, align: 'right',
      render: (v: number | null) => (v == null ? '-' : v.toPrecision(6)),
    },
    {
      title: '质量分', dataIndex: 'quality_score', key: 'quality_score', width: 80, align: 'right',
      render: (v: number | null) => (v == null ? '-' : v),
    },
    {
      title: '触碰K线', dataIndex: 'stage1_candle_open', key: 'stage1_candle_open', width: 110,
      render: (v: string | null) => (
        <Tooltip title="Stage-1 回踩触碰 K 线的开盘时间；确认窗口为其后 N 根已收盘K线（默认 3 根）">
          {fmtTime(v)}
        </Tooltip>
      ),
    },
    {
      title: '更新时间', dataIndex: 'updated_at', key: 'updated_at', width: 110,
      render: (v: string | null) => fmtTime(v),
    },
  ]

  const segOptions: { label: string; value: StatusFilter }[] = [
    { label: `待确认 (${counts.pending})`, value: 'pending' },
    { label: `后续结果 (${counts.outcome})`, value: 'outcome' },
    { label: '命中历史', value: 'hit-history' },
    { label: '无触发', value: 'no-trigger' },
    { label: '筛选漏斗', value: 'funnel' },
  ]

  return (
    <div style={{ display: 'flex', flexDirection: 'column', height: '100%' }}>
      <div style={{ flexShrink: 0, marginBottom: 12, display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
        <Segmented<StatusFilter> value={filter} onChange={setFilter} options={segOptions} />
        {filter !== 'funnel' && filter !== 'no-trigger' && filter !== 'hit-history' && (
          <Button icon={<ReloadOutlined />} onClick={() => fetchData()} loading={loading} size="small">
            刷新
          </Button>
        )}
      </div>
      {filter === 'hit-history' ? (
        // 命中历史（2026-10-15，docs/22）：跨轮次全量扫描命中查询 + K 线标注；
        // 自带查询控件与刷新，独立子面板（与 funnel/no-trigger 同模式）
        <div style={{ flex: 1, minHeight: 0, overflowY: 'auto', overflowX: 'hidden' }}>
          <HitHistoryPanel />
        </div>
      ) : filter === 'funnel' || filter === 'no-trigger' ? (
        // 筛选漏斗（2026-10-11 拍板：作为 Segmented 项）：每轮被拒币种列表 +
        // 卡住关卡筛选 + 币种搜索 + 点击看K线。
        // 「无触发」页签复用同一面板、预置关卡筛选为 no_trigger（2026-10-12；
        // 2026-10-13 移至「待确认」之后并隐藏漏斗概览）：展示未触发币种及其
        // 未过条件（判定说明列），免去下拉切换；key 按 filter 区分，避免两个
        // 页签间切换时 React 复用实例导致预设不生效
        <div style={{ flex: 1, minHeight: 0, overflowY: 'auto', overflowX: 'hidden' }}>
          <FunnelPanel
            key={filter}
            initialStage={filter === 'no-trigger' ? 'no_trigger' : undefined}
          />
        </div>
      ) : (
        <>
          <div style={{ flex: 1, minHeight: 0, overflowY: 'auto', overflowX: 'hidden' }}>
            <Spin spinning={loading}>
              <Table<PullbackPendingItem>
                rowKey="id"
                columns={columns}
                dataSource={filtered}
                pagination={{ pageSize: 20, showSizeChanger: false, showTotal: (t) => `共 ${t} 条` }}
                size="small"
                scroll={{ x: 1140 }}
                locale={{
                  emptyText: (
                    <Empty
                      description={
                        filter === 'pending'
                          ? '暂无待确认币种——回踩触碰且当根无金K时会出现在这里'
                          : filter === 'outcome'
                            ? '近三天暂无后续结果——待确认币种确认/卡掉后出现在这里'
                            : '暂无记录'
                      }
                    />
                  ),
                }}
              />
            </Spin>
          </div>
          <Modal
            open={!!klineRow}
            title={klineRow ? `${klineRow.symbol} 这次待确认的命中K线` : ''}
            footer={null}
            width={1200}
            destroyOnClose
            onCancel={() => setKlineRow(null)}
          >
            {klineRow && (
              <div style={{ height: 640 }}>
                <KlineChart
                  symbol={klineRow.symbol}
                  limit={500}
                  hitTime={klineRow.created_at}
                  touchTime={klineRow.stage1_candle_open}
                  hitDirection={klineRow.direction}
                  hitSignalType="trigger_pullback_confirm"
                />
              </div>
            )}
          </Modal>
        </>
      )}
    </div>
  )
}
