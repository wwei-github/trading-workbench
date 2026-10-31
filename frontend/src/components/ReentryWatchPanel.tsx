/**
 * 再入场检测与提醒（docs/需求文档/24，2026-10-31）
 *
 * 来源：正式网止损平仓、止损根带长影线且结构仍完好（EMA 排列 / ADX≥20 /
 * R²≥0.5）的交易。窗口内价格回到原开仓价（或开仓价 ± θR）时后端发一张
 * 飞书卡；本页只做查看与人工忽略——**不提供任何开单入口**。
 *
 * 数据源 GET /api/trades/reentry-watches（一次拉全量，前端按状态分组）。
 * 后台每小时 :06 用已收盘 1h 的 high/low 巡检，页面每 5 分钟自动刷新；
 * 即时提醒以飞书卡为准。
 */
import { useCallback, useEffect, useMemo, useState } from 'react'
import { Alert, Button, Empty, Modal, Segmented, Space, Spin, Table, Tag, Tooltip, Typography, message } from 'antd'
import { ReloadOutlined } from '@ant-design/icons'
import type { ColumnsType } from 'antd/es/table'
import { scanApi } from '../api/scan'
import { bj } from '../utils/dayjs'
import { schemeTag } from '../utils/scheme'
import { useScanStore } from '../stores/scanStore'
import KlineChart from './KlineChart'
import type { ReentryWatch } from '../types'

const { Text, Link } = Typography

type Filter = 'watching' | 'triggered' | 'finished'

const TERMINAL = new Set(['expired', 'structure_broken', 'wick_miss', 'superseded', 'dismissed'])

const STATUS_META: Record<string, { color: string; label: string; hint: string }> = {
  watching: {
    color: 'processing', label: '监控中',
    hint: '止损根影线合格且结构完好，等待价格回到触发价；每小时 :06 用已收盘 1h 巡检',
  },
  triggered: {
    color: 'success', label: '已触发',
    hint: '价格已到达触发价，已发送且仅发送一张飞书提醒（不会自动开单）',
  },
  expired: {
    color: 'default', label: '已过期',
    hint: '监控窗口内价格未回到触发价，监控结束',
  },
  structure_broken: {
    color: 'volcano', label: '结构破坏',
    hint: 'EMA 排列 / ADX≥20 / R²≥0.5 至少一项不再满足，停止监控',
  },
  wick_miss: {
    color: 'default', label: '影线不足',
    hint: '止损那根 1h 的反向影线不足（需 ≥2×实体且 ≥40% 全幅），视为有效跌破/突破，不提醒',
  },
  superseded: {
    color: 'default', label: '已有仓位',
    hint: '同币已有在跑仓位，不再重复提醒',
  },
  dismissed: {
    color: 'default', label: '已忽略',
    hint: '人工忽略，历史记录保留',
  },
}

function fmtTime(s: string | null | undefined): string {
  if (!s) return '-'
  return bj(s).format('MM-DD HH:mm')
}

function fmtCountdown(sec: number): string {
  if (sec <= 0) return '已到期'
  const h = Math.floor(sec / 3600)
  const m = Math.floor((sec % 3600) / 60)
  const s = sec % 60
  return `${String(h).padStart(2, '0')}:${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}`
}

function px(v: number | null | undefined): string {
  if (v == null) return '-'
  return Number(v).toPrecision(6)
}

/** 结构三件：EMA 排列 / ADX≥20 / R²≥0.5（tooltip 给原始读数） */
function StructureCell({ r }: { r: ReentryWatch }) {
  const items = [
    { key: 'ema', short: 'EMA', ok: r.ema_ok, detail: r.ema144_used === false ? 'EMA21/55 排列（144 不可用）' : 'EMA21/55/144 排列' },
    { key: 'adx', short: 'ADX', ok: r.adx_ok, detail: `ADX(14,1h) ${r.adx ?? '-'} ≥ 20` },
    { key: 'r2', short: 'R²', ok: r.r2_ok, detail: `R² ${r.r2 ?? '-'} ≥ 0.5` },
  ]
  return (
    <Space size={4}>
      {items.map((it) => (
        <Tooltip key={it.key} title={it.detail}>
          <Tag
            color={it.ok ? 'success' : 'default'}
            style={{ marginInlineEnd: 0, fontVariantNumeric: 'tabular-nums' }}
          >
            {it.short}{it.ok ? '✓' : '✗'}
          </Tag>
        </Tooltip>
      ))}
    </Space>
  )
}

/** 止损根影线：反向影线 / 实体倍数 + 占全幅比例（门槛 ≥2×实体且 ≥40%） */
function WickCell({ r }: { r: ReentryWatch }) {
  if (r.stop_wick_ok == null) return <Text type="secondary">-</Text>
  const side = r.direction === 'long' ? '下影' : '上影'
  const mult = r.stop_wick_mult == null ? '实体≈0' : `${r.stop_wick_mult.toFixed(2)}×`
  const pct = r.stop_wick_pct == null ? '-' : `${(r.stop_wick_pct * 100).toFixed(0)}%`
  return (
    <Tooltip title={`止损那根 1h 的${side}：${mult}实体 / ${pct} 全幅；合格线 ≥2×实体且 ≥40% 全幅`}>
      <Tag
        color={r.stop_wick_ok ? 'success' : 'default'}
        style={{ marginInlineEnd: 0, fontVariantNumeric: 'tabular-nums' }}
      >
        {side} {mult} · {pct}{r.stop_wick_ok ? ' ✓' : ' ✗'}
      </Tag>
    </Tooltip>
  )
}

export default function ReentryWatchPanel() {
  const [rows, setRows] = useState<ReentryWatch[]>([])
  const [loading, setLoading] = useState(false)
  const [filter, setFilter] = useState<Filter>('watching')
  const [fetchedAt, setFetchedAt] = useState(Date.now())
  const [nowMs, setNowMs] = useState(Date.now())
  const [klineRow, setKlineRow] = useState<ReentryWatch | null>(null)
  const colorScheme = useScanStore((s) => s.colorScheme)

  const fetchData = useCallback(async () => {
    setLoading(true)
    try {
      setRows(await scanApi.trades.reentryWatches('all', 200))
      setFetchedAt(Date.now())
    } catch (e: any) {
      message.error(e?.response?.data?.detail || '获取再入场监控失败')
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    fetchData()
    const refreshTimer = window.setInterval(fetchData, 5 * 60_000)
    const tickTimer = window.setInterval(() => setNowMs(Date.now()), 1_000)
    return () => {
      window.clearInterval(refreshTimer)
      window.clearInterval(tickTimer)
    }
  }, [fetchData])

  const counts = useMemo(() => ({
    watching: rows.filter((r) => r.status === 'watching').length,
    triggered: rows.filter((r) => r.status === 'triggered').length,
    finished: rows.filter((r) => TERMINAL.has(r.status)).length,
  }), [rows])

  const filtered = useMemo(() => {
    if (filter === 'finished') return rows.filter((r) => TERMINAL.has(r.status))
    return rows.filter((r) => r.status === filter)
  }, [rows, filter])

  /** 剩余秒数 = 后端拉取时的 seconds_left − 页面已流逝秒数（仅 watching） */
  const leftSeconds = useCallback(
    (r: ReentryWatch) => Math.max(0, (r.seconds_left ?? 0) - Math.floor((nowMs - fetchedAt) / 1000)),
    [nowMs, fetchedAt],
  )

  const onDismiss = useCallback((r: ReentryWatch) => {
    Modal.confirm({
      title: `忽略 ${r.symbol} 的再入场提醒？`,
      content: '忽略后不再提醒该笔，历史记录保留',
      okText: '忽略',
      cancelText: '取消',
      onOk: async () => {
        try {
          await scanApi.trades.dismissReentryWatch(r.id)
          await fetchData()
        } catch (e: any) {
          message.error(e?.response?.data?.detail || '忽略失败')
        }
      },
    })
  }, [fetchData])

  const columns: ColumnsType<ReentryWatch> = [
    {
      title: '币种', dataIndex: 'symbol', key: 'symbol', width: 120,
      render: (v: string, r) => (
        <Tooltip title="点击查看该笔止损出场附近的 K 线">
          <Link strong onClick={() => setKlineRow(r)}>{v}</Link>
        </Tooltip>
      ),
    },
    {
      title: '方向', dataIndex: 'direction', key: 'direction', width: 80,
      render: (v: 'long' | 'short') => (
        <Tag color={v === 'long' ? schemeTag(colorScheme).up : schemeTag(colorScheme).down}>
          {v === 'long' ? '看多' : '看空'}
        </Tag>
      ),
    },
    {
      title: '状态', dataIndex: 'status', key: 'status', width: 110,
      render: (v: string, r) => {
        const meta = STATUS_META[v] || { color: 'default', label: v, hint: '' }
        return (
          <Tooltip title={meta.hint}>
            <Tag color={meta.color}>{meta.label}</Tag>
            {r.resolve_reason === 'structure_broken_at_close' && (
              <Text type="secondary" style={{ fontSize: 12 }}>（平仓时已破）</Text>
            )}
          </Tooltip>
        )
      },
    },
    {
      title: '原入场 / 止损', key: 'entry_stop', width: 170,
      render: (_, r) => (
        <Text style={{ fontVariantNumeric: 'tabular-nums' }}>
          {px(r.entry_price)} / {px(r.stop_initial)}
        </Text>
      ),
    },
    {
      title: '触发价', key: 'trigger', width: 140,
      render: (_, r) => (
        <Tooltip title={`触发阈值 θ=${r.theta_r}R；价格为 ${r.direction === 'long' ? '≥' : '≤'} 触发价时提醒`}>
          <span style={{ fontVariantNumeric: 'tabular-nums' }}>
            {px(r.trigger_price)}
            <Text type="secondary" style={{ fontSize: 12, marginLeft: 4 }}>
              {Number(r.theta_r) === 0 ? '回到开仓价' : `θ=${r.theta_r}R`}
            </Text>
          </span>
        </Tooltip>
      ),
    },
    {
      title: '当前价(1h收盘)', dataIndex: 'current_price', key: 'current_price', width: 130, align: 'right',
      render: (v: number | null) => px(v),
    },
    {
      title: '距触发', key: 'distance', width: 100, align: 'right',
      render: (_, r) => {
        if (r.current_price == null || !r.trigger_price) return '-'
        const pct = (r.current_price - r.trigger_price) / r.trigger_price * 100
        return (
          <Tooltip title={`(当前价 − 触发价) ÷ 触发价；多单为正=已越过触发价，空单为负=已越过`}>
            <Text type={pct > 0 ? 'success' : pct < 0 ? 'danger' : undefined}>
              {(pct > 0 ? '+' : '') + pct.toFixed(2)}%
            </Text>
          </Tooltip>
        )
      },
    },
    {
      title: '止损根影线', key: 'stop_wick', width: 190,
      render: (_, r) => <WickCell r={r} />,
    },
    {
      title: '结构', key: 'structure', width: 200,
      render: (_, r) => <StructureCell r={r} />,
    },
    {
      title: '剩余时间', key: 'left', width: 130,
      render: (_, r) => {
        if (r.status === 'watching') {
          return (
            <Tooltip title={`监控窗口至 ${fmtTime(r.expires_at)}`}>
              <span style={{ fontVariantNumeric: 'tabular-nums' }}>{fmtCountdown(leftSeconds(r))}</span>
            </Tooltip>
          )
        }
        return <Text type="secondary">{r.resolved_at ? fmtTime(r.resolved_at) : '-'}</Text>
      },
    },
    {
      title: '最近检查', dataIndex: 'last_checked_at', key: 'last_checked_at', width: 110,
      render: (v: string | null) => fmtTime(v),
    },
    {
      title: '操作', key: 'action', width: 70,
      render: (_, r) =>
        r.status === 'watching' || r.status === 'triggered' ? (
          <Button size="small" type="link" onClick={() => onDismiss(r)}>忽略</Button>
        ) : (
          <Text type="secondary">-</Text>
        ),
    },
  ]

  const emptyText = () => (
    <Empty
      description={
        filter === 'watching'
          ? '暂无监控中的止损单——正式网止损出场、止损根带长影线且结构完好时会自动出现（整点收盘后 6 分钟更新）'
          : filter === 'triggered'
            ? '暂无已触发提醒'
            : '暂无已结束记录'
      }
    />
  )

  return (
    <div style={{ display: 'flex', flexDirection: 'column', height: '100%' }}>
      <Alert
        type="warning"
        showIcon
        style={{ flexShrink: 0, marginBottom: 12 }}
        message="仅提醒，不会自动开单；回测该再入场腿期望为负"
      />
      <div style={{ flexShrink: 0, marginBottom: 12, display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
        <Segmented<Filter>
          value={filter}
          onChange={setFilter}
          options={[
            { label: `监控中 (${counts.watching})`, value: 'watching' },
            { label: `已触发 (${counts.triggered})`, value: 'triggered' },
            { label: `已结束 (${counts.finished})`, value: 'finished' },
          ]}
        />
        <Button icon={<ReloadOutlined />} onClick={() => fetchData()} loading={loading} size="small">
          刷新
        </Button>
      </div>
      <div style={{ flex: 1, minHeight: 0, overflowY: 'auto', overflowX: 'hidden' }}>
        <Spin spinning={loading}>
          <Table<ReentryWatch>
            rowKey="id"
            columns={columns}
            dataSource={filtered}
            pagination={{ pageSize: 20, showSizeChanger: false, showTotal: (t) => `共 ${t} 条` }}
            size="small"
            scroll={{ x: 1500 }}
            locale={{ emptyText: emptyText() }}
          />
        </Spin>
      </div>
      <Modal
        open={!!klineRow}
        title={klineRow ? `${klineRow.symbol} 止损出场附近 K 线` : ''}
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
              hitDirection={klineRow.direction === 'long' ? 1 : -1}
            />
          </div>
        )}
      </Modal>
    </div>
  )
}
