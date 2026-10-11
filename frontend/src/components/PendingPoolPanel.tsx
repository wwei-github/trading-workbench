/**
 * 回踩两段式待确认池（docs/19，2026-09-25）
 * - Stage-1 回踩触碰 EMA21 但当根无金K 的币种转入 pending，等下一根K线金K确认
 * - 状态流转：pending（等待中）→ confirmed（已确认进 AI）/ expired（无金K或方向失守）/ superseded（突破或当根确认抢先）
 * - 展示归并三态（2026-10-10）：待确认 / 已确认 / 已卡掉（expired+superseded 统计为卡掉），被哪个环节卡住看「卡住环节」列（后端 blocked_by）
 * - 筛选漏斗（2026-10-11）：Segmented 第五项排在「全部」后面，展示每轮被拒币种卡在哪一关（FunnelPanel）
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
import type { PullbackPendingItem } from '../types'

const { Text, Link } = Typography

type StatusFilter = 'pending' | 'confirmed' | 'blocked' | 'all' | 'funnel'

// 展示三态（2026-10-10）：pending=待确认、confirmed=已确认；expired/superseded 一律归并「已卡掉」，
// 具体被哪个环节卡住由「卡住环节」列展示（后端 blocked_by 字段）
const STATUS_META: Record<string, { color: string; label: string; hint: string }> = {
  pending: {
    color: 'processing', label: '待确认',
    hint: '回踩 EMA21 企稳，等下一根K线出现同向金K即确认入场；无金K则过期',
  },
  confirmed: {
    color: 'success', label: '已确认',
    hint: '下一根K线出现同向金K，以「回踩金K确认」信号落库并进入 AI 分析',
  },
  expired: {
    color: 'default', label: '已卡掉',
    hint: '被判定链某环节卡掉：ranging=4h 无明确趋势；structure_1h=1H 结构不符；range_score=震荡评分≥3；window_expired=次根无金K确认失效；direction_flip=指标方向失守作废',
  },
  superseded: {
    color: 'default', label: '已卡掉',
    hint: '被更优路径抢先（同样算卡掉）：breakout=Donchian 突破抢先触发；same_candle_confirm=当根触碰+金K直接确认',
  },
}

const REASON_LABEL: Record<string, string> = {
  ranging: '4h无明确趋势',
  structure_1h: '1H结构不符',
  range_score: '震荡评分≥3',
  window_expired: '次根无金K',
  direction_flip: '方向失守',
  breakout: '突破抢先',
  same_candle_confirm: '当根确认抢先',
}

// 触点方向化措辞（与后端 trigger._touch_ref_label 同口径）：
// 「等待确认」列拼接「次根看涨金K·触点摆动低点」这类待确认内容
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

export default function PendingPoolPanel() {
  const [rows, setRows] = useState<PullbackPendingItem[]>([])
  const [loading, setLoading] = useState(false)
  const [filter, setFilter] = useState<StatusFilter>('pending')
  // 弹窗展示的币种 K 线（null=关闭）；所有 tab 的币种名称均点击可看
  const [klineSymbol, setKlineSymbol] = useState<string | null>(null)
  const colorScheme = useScanStore((s) => s.colorScheme)

  const fetchData = useCallback(async () => {
    setLoading(true)
    try {
      setRows(await scanApi.pullbackPendings.list('all', 200))
    } catch (e: any) {
      message.error(e?.response?.data?.detail || '获取待确认池失败')
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    fetchData()
  }, [fetchData])

  const counts = useMemo(() => ({
    pending: rows.filter((r) => r.status === 'pending').length,
    confirmed: rows.filter((r) => r.status === 'confirmed').length,
    blocked: rows.filter((r) => r.status === 'expired' || r.status === 'superseded').length,
    all: rows.length,
  }), [rows])

  const filtered = useMemo(() => {
    if (filter === 'all') return rows
    if (filter === 'blocked') return rows.filter((r) => r.status === 'expired' || r.status === 'superseded')
    return rows.filter((r) => r.status === filter)
  }, [rows, filter])

  const columns: ColumnsType<PullbackPendingItem> = [
    {
      title: '币种', dataIndex: 'symbol', key: 'symbol', width: 130,
      render: (v: string) => (
        <Tooltip title="点击查看最新K线">
          <Link strong onClick={() => setKlineSymbol(v)}>{v}</Link>
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
          <Tooltip title="确认窗口仅限触碰K线的下一根已收盘K线：出现同向金K即确认入场；无金K过期，突破/当根确认则让位">
            <Text>{`次根${side}金K${t ? `·触点${t}` : ''}`}</Text>
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
        <Tooltip title="Stage-1 回踩触碰 K 线的开盘时间；确认窗口仅限其下一根已收盘K线">
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
    { label: `已确认 (${counts.confirmed})`, value: 'confirmed' },
    { label: `已卡掉 (${counts.blocked})`, value: 'blocked' },
    { label: `全部 (${counts.all})`, value: 'all' },
    { label: '筛选漏斗', value: 'funnel' },
  ]

  return (
    <div style={{ display: 'flex', flexDirection: 'column', height: '100%' }}>
      <div style={{ flexShrink: 0, marginBottom: 12, display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
        <Segmented<StatusFilter> value={filter} onChange={setFilter} options={segOptions} />
        {filter !== 'funnel' && (
          <Button icon={<ReloadOutlined />} onClick={() => fetchData()} loading={loading} size="small">
            刷新
          </Button>
        )}
      </div>
      {filter === 'funnel' ? (
        // 筛选漏斗（2026-10-11 拍板：作为 Segmented 第五项排在「全部」后面）：
        // 每轮被拒币种列表 + 卡住关卡筛选 + 币种搜索 + 点击看K线
        <div style={{ flex: 1, minHeight: 0, overflowY: 'auto', overflowX: 'hidden' }}>
          <FunnelPanel />
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
                      description={filter === 'pending'
                        ? '暂无待确认币种——回踩触碰且当根无金K时会出现在这里'
                        : '暂无记录'}
                    />
                  ),
                }}
              />
            </Spin>
          </div>
          <Modal
            open={!!klineSymbol}
            title={klineSymbol ? `${klineSymbol} 最新K线` : ''}
            footer={null}
            width={1200}
            destroyOnClose
            onCancel={() => setKlineSymbol(null)}
          >
            {klineSymbol && (
              <div style={{ height: 640 }}>
                <KlineChart symbol={klineSymbol} limit={500} />
              </div>
            )}
          </Modal>
        </>
      )}
    </div>
  )
}
