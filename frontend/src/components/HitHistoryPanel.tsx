/**
 * 命中历史（2026-10-15，docs/22）
 * - 待确认池第 3 段：跨轮次全量查询扫描命中（ScanResult 全量，不限回踩路径）
 * - 默认最近 7 天，可改时间范围往前查全量；清空日期 = 查全量历史（all_time）
 *   （时间粒度=整点，不提供分钟选择）；
 *   币种精确搜索；「仅看首次命中」开关
 * - 点击币种名打开 K 线弹窗：最新K线 + 命中/触碰K线标签 + 时间裁剪的开单区间
 * - 数据源：GET /api/scans/hit-history（服务端分页）
 */
import { useCallback, useEffect, useMemo, useState } from 'react'
import {
  Button, Checkbox, DatePicker, Empty, Input, Modal, Space, Spin, Table, Tag, Tooltip, Typography, message,
} from 'antd'
import { ReloadOutlined } from '@ant-design/icons'
import type { ColumnsType } from 'antd/es/table'
import type { Dayjs } from 'dayjs'
import { scanApi } from '../api/scan'
import { bj } from '../utils/dayjs'
import { patternStyle, SIGNAL_TYPE_MAP } from '../constants/labels'
import { schemeTag, schemeValueColor } from '../utils/scheme'
import { useScanStore } from '../stores/scanStore'
import KlineChart from './KlineChart'
import type { HitHistoryItem } from '../types'

const { Text, Link } = Typography
const { RangePicker } = DatePicker

// 开单状态中文名（与结果表/交易记录口径一致）
const TRADE_STATUS_LABEL: Record<string, string> = {
  OPENED: '运行中',
  TP1_HIT: '部分止盈',
  TP2_HIT: '已止盈·跟踪止损',
  CLOSED: '已平仓',
  FAILED: '开单失败',
}

// 命中历史时间筛选：仅整点（1–59 分全部禁用，只可选 00）
const DISABLED_MINUTES = Array.from({ length: 59 }, (_, i) => i + 1)

// 回踩触点方向化文案（与 ResultTable.touchLabel 同口径；仅 position_ref 缺失时兜底）
function touchLabel(ref: string, dir: number): string {
  if (ref === 'ema21') return 'EMA21'
  if (ref === 'ema21_4h') return 'EMA21(4h)'
  if (ref === 'ema55') return 'EMA55'
  if (ref === 'ema144') return 'EMA144'
  if (ref === 'retest') return dir > 0 ? '前高回测' : dir < 0 ? '前低回测' : '突破回测位'
  if (ref === 'swing') return dir > 0 ? '摆动低点' : dir < 0 ? '摆动高点' : '摆动结构位'
  return ref
}

// K 线拉取根数（2026-10-15，docs/22 §5.1）：按命中时刻回溯的 1h 根数 + 缓冲，
// clamp [500, 1500]——命中久远时固定 500 根覆盖不到，需动态取根数
function klineLimit(row: HitHistoryItem): number {
  const cands = [row.created_at, row.stage1_candle_open, row.trade?.opened_at]
    .filter((v): v is string => !!v)
    .map((v) => bj(v).valueOf())
  if (!cands.length) return 500
  const bars = Math.ceil((Date.now() - Math.min(...cands)) / 3_600_000) + 50
  return Math.min(1500, Math.max(500, bars))
}

export default function HitHistoryPanel() {
  const colorScheme = useScanStore((s) => s.colorScheme)
  const [items, setItems] = useState<HitHistoryItem[]>([])
  const [total, setTotal] = useState(0)
  const [loading, setLoading] = useState(false)
  const [page, setPage] = useState(1)
  const [pageSize] = useState(20)
  // 查询条件：默认最近 7 天，时间粒度=整点（分钟固定 00，不提供分钟选择）；
  // 清空（null）= 不限下界查全量（all_time），右端仍按选值/now
  const [range, setRange] = useState<[Dayjs, Dayjs] | null>([
    bj().subtract(7, 'day').startOf('hour'),
    bj().startOf('hour').add(1, 'hour'), // 右端取下一整点，保证覆盖"到现在"（不丢当前小时）
  ])
  const [symbol, setSymbol] = useState('')
  const [firstOnly, setFirstOnly] = useState(false)
  // 弹窗展示的命中行（null=关闭）
  const [detail, setDetail] = useState<HitHistoryItem | null>(null)

  const fetchData = useCallback(
    async (pageNo = 1) => {
      setLoading(true)
      try {
        const res = await scanApi.hitHistory({
          start: range ? range[0].toISOString() : undefined,
          end: range ? range[1].toISOString() : undefined,
          all_time: !range,
          symbol: symbol.trim() || undefined,
          include_repeat: !firstOnly,
          page: pageNo,
          page_size: pageSize,
        })
        setItems(res.items)
        setTotal(res.total)
        setPage(pageNo)
      } catch (e: any) {
        message.error(e?.response?.data?.detail || '获取命中历史失败')
      } finally {
        setLoading(false)
      }
    },
    [range, symbol, firstOnly, pageSize],
  )

  useEffect(() => {
    fetchData(1)
  }, [fetchData])

  const columns: ColumnsType<HitHistoryItem> = [
    {
      title: '币种', dataIndex: 'symbol', key: 'symbol', width: 130,
      render: (v: string, r) => (
        <Tooltip title="点击查看信号K线（标注命中/触碰）">
          <Link strong onClick={() => setDetail(r)}>{v}</Link>
        </Tooltip>
      ),
    },
    {
      title: '信号K线', dataIndex: 'signal_candle_open', key: 'signal_candle_open', width: 120,
      render: (v: string | null) => (
        v ? bj(v).format('MM-DD HH:mm') : <Text type="secondary">-</Text>
      ),
    },
    {
      title: '扫描时间', dataIndex: 'created_at', key: 'created_at', width: 120,
      render: (v: string) => bj(v).format('MM-DD HH:mm'),
    },
    {
      title: '方向', dataIndex: 'direction', key: 'direction', width: 90,
      render: (v: 1 | -1 | 0) => {
        if (v === 1) return <Tag color={schemeTag(colorScheme).up}>看多</Tag>
        if (v === -1) return <Tag color={schemeTag(colorScheme).down}>看空</Tag>
        return <Text type="secondary">-</Text>
      },
    },
    {
      title: '信号类型', dataIndex: 'signal_type', key: 'signal_type', width: 130,
      render: (v: string) => {
        const s = SIGNAL_TYPE_MAP[v]
        return s ? <Tag color={s.color}>{s.label}</Tag> : <Text type="secondary">{v}</Text>
      },
    },
    {
      title: '形态', dataIndex: 'pattern', key: 'pattern', width: 110,
      render: (v: string | null) => {
        if (!v) return <Text type="secondary">-</Text>
        const s = patternStyle(v, colorScheme)
        return <Tag color={s.color}>{s.label}</Tag>
      },
    },
    {
      title: '位置', key: 'position', width: 130,
      render: (_, r) => {
        if (r.position_ref) {
          const isPullback = r.position_ref.startsWith('回踩')
          // 位价尾巴移入悬停（与结果表同口径）
          const short = r.position_ref.replace(/\s+[0-9][\d.eE+-]*$/, '')
          return (
            <Tooltip title={r.position_ref}>
              <Tag color={isPullback ? 'purple' : 'geekblue'} style={{ marginInlineEnd: 0 }}>{short}</Tag>
            </Tooltip>
          )
        }
        if (r.touch_ref) return <Tag color="purple">{touchLabel(r.touch_ref, r.direction)}</Tag>
        return <Text type="secondary">-</Text>
      },
    },
    {
      title: '质量分', dataIndex: 'quality_score', key: 'quality_score', width: 80, align: 'right',
      render: (v: number | null) => (v == null ? '-' : v),
    },
    {
      title: '开单状态', key: 'trade_status', width: 110,
      render: (_, r) => {
        if (!r.trade) return <Text type="secondary">未开单</Text>
        const label = TRADE_STATUS_LABEL[r.trade.status] || r.trade.status
        const color = r.trade.status === 'CLOSED' ? 'default'
          : r.trade.status === 'FAILED' ? 'error' : 'processing'
        return (
          <Tooltip title={r.trade.testnet ? '测试网单' : '正式网单'}>
            <Tag color={color}>{label}</Tag>
          </Tooltip>
        )
      },
    },
    {
      title: '盈亏', key: 'pnl', width: 100, align: 'right',
      render: (_, r) => {
        const pnl = r.trade?.realized_pnl
        if (pnl == null) return <Text type="secondary">-</Text>
        return <span style={{ color: schemeValueColor(pnl, colorScheme) }}>{pnl.toFixed(2)}</span>
      },
    },
    {
      title: '重复', dataIndex: 'is_repeat', key: 'is_repeat', width: 80,
      render: (v: boolean) => (v ? <Tag color="orange">重复</Tag> : <Text type="secondary">-</Text>),
    },
  ]

  const limit = useMemo(() => (detail ? klineLimit(detail) : 500), [detail])

  return (
    <div style={{ display: 'flex', flexDirection: 'column', height: '100%' }}>
      <div style={{ flexShrink: 0, marginBottom: 12, display: 'flex', gap: 8, flexWrap: 'wrap', alignItems: 'center' }}>
        <RangePicker
          showTime={{ format: 'HH:00' }}
          format="MM-DD HH:00"
          disabledTime={() => ({ disabledMinutes: () => DISABLED_MINUTES })}
          value={range}
          placeholder={['开始（清空=全量）', '结束']}
          onChange={(v) => {
            // 仅整点：分钟列被禁用（只可选 00），此处再归一兜底；清空 → null（all_time 全量）
            if (v && v[0] && v[1]) setRange([v[0].startOf('hour'), v[1].startOf('hour')])
            else setRange(null)
          }}
        />
        <Input.Search
          placeholder="币种（如 BTCUSDT）"
          allowClear
          style={{ width: 200 }}
          onSearch={(v) => setSymbol(v)}
        />
        <Checkbox checked={firstOnly} onChange={(e) => setFirstOnly(e.target.checked)}>
          仅看首次命中
        </Checkbox>
        <Button icon={<ReloadOutlined />} onClick={() => fetchData(page)} loading={loading} size="small">
          刷新
        </Button>
        <Text type="secondary" style={{ fontSize: 12 }}>
          默认最近 7 天；清空日期 = 查询全量历史
        </Text>
      </div>
      <div style={{ flex: 1, minHeight: 0, overflowY: 'auto', overflowX: 'hidden' }}>
        <Spin spinning={loading}>
          <Table<HitHistoryItem>
            rowKey="id"
            columns={columns}
            dataSource={items}
            size="small"
            scroll={{ x: 1200 }}
            pagination={{
              current: page,
              pageSize,
              total,
              showSizeChanger: false,
              onChange: (p) => fetchData(p),
              showTotal: (t) => `共 ${t} 条`,
            }}
            locale={{
              emptyText: <Empty description="所选范围内暂无命中记录" />,
            }}
          />
        </Spin>
      </div>
      <Modal
        open={!!detail}
        title={
          detail
            ? detail.signal_candle_open
              ? `${detail.symbol} 信号K线（${bj(detail.signal_candle_open).format('MM-DD HH:mm')}–${bj(detail.signal_candle_open).add(1, 'hour').format('HH:mm')}）`
              : `${detail.symbol} 命中K线（最新K线）`
            : ''
        }
        footer={null}
        width={1200}
        destroyOnClose
        onCancel={() => setDetail(null)}
      >
        {detail && (
          <>
            <div style={{ height: 640 }}>
              <KlineChart
                symbol={detail.symbol}
                limit={limit}
                hitTime={detail.created_at}
                signalCandleOpen={detail.signal_candle_open}
                touchTime={detail.stage1_candle_open}
                hitDirection={detail.direction}
                tradeOverlay={detail.trade}
                hitSignalType={detail.signal_type}
                hitPattern={detail.pattern}
              />
            </div>
            <Space size="small" style={{ marginTop: 8, fontSize: 12, color: '#8b93a7' }}>
              <span>
                <Tag color="#1677ff">信号K线</Tag>
                {detail.signal_candle_open
                  ? `${bj(detail.signal_candle_open).format('YYYY-MM-DD HH:mm')}–${bj(detail.signal_candle_open).add(1, 'hour').format('HH:mm')}`
                  : '-'}
              </span>
              <span>
                <Tag color="default">扫描</Tag>
                {bj(detail.created_at).format('YYYY-MM-DD HH:mm')}
              </span>
              {detail.stage1_candle_open && (
                <span>
                  <Tag color="#722ed1">触碰</Tag>
                  {bj(detail.stage1_candle_open).format('YYYY-MM-DD HH:mm')}
                </span>
              )}
            </Space>
          </>
        )}
      </Modal>
    </div>
  )
}
