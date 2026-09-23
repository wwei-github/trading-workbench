/**
 * 现货·历史记录（docs/18 §9.2.3 运营时间轴，react-chrono 垂直时间轴）
 * - 事件源：买卖/定投订单（绿/红）、AI 周度研判（紫）、每日评分日检（蓝）
 * - 每个节点展示触发信息与结果（成交状态/AI 研判结论/三币评分）
 * - 类型过滤 + 时间倒序；P0 阶段买卖与研判暂无数据，日检评分每天一条
 */
import { useCallback, useEffect, useMemo, useState } from 'react'
import { Button, Card, Empty, Segmented, Space, Spin, Tag, Tooltip, Typography } from 'antd'
import { ReloadOutlined } from '@ant-design/icons'
import { Chrono } from 'react-chrono'
import { spotApi } from '../api/spot'
import type { SpotTimelineDaily, SpotTimelineItem, SpotTimelineOrder, SpotTimelineReview } from '../types/spot'
import { bj } from '../utils/dayjs'

type TypeFilter = 'all' | 'order' | 'review' | 'daily_check'

const TYPE_META: Record<string, { label: string; color: string }> = {
  order: { label: '交易', color: 'green' },
  review: { label: 'AI 研判', color: 'purple' },
  daily_check: { label: '每日评分', color: 'blue' },
}

const ORDER_STATUS_META: Record<string, { label: string; color: string }> = {
  PENDING_CONFIRM: { label: '待确认', color: 'gold' },
  FILLED: { label: '已成交', color: 'green' },
  CANCELLED: { label: '已取消', color: 'default' },
  FAILED: { label: '失败', color: 'red' },
}

const TRIGGER_LABEL: Record<string, string> = {
  signal: '折价信号',
  dca: '周度定投',
  manual: '手动',
}

const REGIME_COLOR: Record<string, string> = {
  积累期: 'blue',
  上升期: 'green',
  分配期: 'orange',
  下降期: 'red',
}

const ACTION_TEXT: Record<string, string> = { BUY: '买入', SELL: '卖出', HOLD: '观望' }

function fmtPrice(v: number | null | undefined): string {
  if (v === null || v === undefined) return '-'
  return v >= 100 ? v.toLocaleString('en-US', { maximumFractionDigits: 2 }) : String(v)
}

// ---- 三类事件的卡片渲染 ----

function OrderCard({ data }: { data: SpotTimelineOrder }) {
  const buy = data.side === 'BUY'
  const status = ORDER_STATUS_META[data.status] || { label: data.status, color: 'default' }
  return (
    <div style={{ padding: '2px 0' }}>
      <Space size={8} wrap>
        <Tag color={buy ? 'green' : 'red'} style={{ fontWeight: 600 }}>
          {buy ? '买入' : '卖出'} {data.symbol.replace('USDT', '')}
        </Tag>
        <Tag color={status.color}>{status.label}</Tag>
        <Tag>{TRIGGER_LABEL[data.trigger_type] || data.trigger_type}</Tag>
        <Tag color={data.testnet ? 'blue' : 'gold'}>{data.testnet ? '测试网' : '正式网'}</Tag>
      </Space>
      <Typography.Text type="secondary" style={{ fontSize: 12, display: 'block', marginTop: 6 }}>
        价格 {fmtPrice(data.price)} · 数量 {fmtPrice(data.qty)} · 金额{' '}
        {data.quote_amount === null ? '-' : `${data.quote_amount.toLocaleString('en-US')} U`}
        {data.executed_at && ` · 成交于 ${bj(data.executed_at).format('MM-DD HH:mm')}`}
      </Typography.Text>
      {data.fail_reason && (
        <Typography.Text type="danger" style={{ fontSize: 12, display: 'block' }}>
          失败原因：{data.fail_reason}
        </Typography.Text>
      )}
      {Array.isArray((data.trigger_detail as { rule_hits?: string[] } | null)?.rule_hits) &&
        (data.trigger_detail as { rule_hits: string[] }).rule_hits.length > 0 && (
          <div style={{ marginTop: 4 }}>
            {(data.trigger_detail as { rule_hits: string[] }).rule_hits.map((h) => (
              <Tag key={h} style={{ marginBottom: 4 }}>{h}</Tag>
            ))}
          </div>
        )}
    </div>
  )
}

function ReviewCard({ data }: { data: SpotTimelineReview }) {
  return (
    <div style={{ padding: '2px 0' }}>
      <Space size={8} wrap>
        <Tag color={REGIME_COLOR[data.regime] || 'purple'} style={{ fontWeight: 600 }}>
          {data.regime}
        </Tag>
        {data.outlook &&
          Object.entries(data.outlook).map(([k, v]) => (
            <Tag key={k}>
              {k} {v}
            </Tag>
          ))}
      </Space>
      {data.commentary && (
        <Typography.Paragraph
          style={{ fontSize: 12, marginTop: 6, marginBottom: 0, whiteSpace: 'pre-wrap' }}
          ellipsis={{ rows: 4, expandable: true, symbol: '展开' }}
        >
          {data.commentary}
        </Typography.Paragraph>
      )}
      {data.risks && data.risks.length > 0 && (
        <div style={{ marginTop: 6 }}>
          {data.risks.map((r) => (
            <Tag key={r} color="orange" style={{ marginBottom: 4 }}>
              ⚠ {r}
            </Tag>
          ))}
        </div>
      )}
    </div>
  )
}

function DailyCard({ data }: { data: SpotTimelineDaily }) {
  if (data.status !== 'completed') {
    return (
      <div style={{ padding: '2px 0' }}>
        <Tag color="red">{data.status === 'failed' ? '日检失败' : data.status}</Tag>
        <Typography.Text type="danger" style={{ fontSize: 12, display: 'block', marginTop: 6 }}>
          {data.error || data.summary || '无详情'}
        </Typography.Text>
      </div>
    )
  }
  const actions = data.actions || {}
  const symbols = Object.keys(actions)
  return (
    <div style={{ padding: '2px 0' }}>
      <Space size={8} wrap>
        {symbols.map((s) => {
          const a = actions[s]
          return (
            <Tooltip key={s} title={a.plan}>
              <Tag>
                {s.replace('USDT', '')}{' '}
                <span style={{ fontWeight: 700 }}>{a.score}</span>分 ·{' '}
                {ACTION_TEXT[a.action] || a.action}
                {a.action !== 'HOLD' && a.lots ? ` ${a.lots}份` : ''}
              </Tag>
            </Tooltip>
          )
        })}
      </Space>
      {symbols.length === 0 && data.summary && (
        <Typography.Text type="secondary" style={{ fontSize: 12, display: 'block', marginTop: 4 }}>
          {data.summary}
        </Typography.Text>
      )}
    </div>
  )
}

export default function SpotHistoryPanel() {
  const [items, setItems] = useState<SpotTimelineItem[]>([])
  const [loading, setLoading] = useState(false)
  const [filter, setFilter] = useState<TypeFilter>('all')

  const load = useCallback(async () => {
    setLoading(true)
    try {
      setItems(await spotApi.timeline(200))
    } catch {
      // axios 拦截器统一提示
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    load()
  }, [load])

  const filtered = useMemo(
    () => (filter === 'all' ? items : items.filter((e) => e.type === filter)),
    [items, filter],
  )

  const chronoItems = useMemo(
    () =>
      filtered.map((e) => {
        const meta = TYPE_META[e.type]
        const time = bj(e.time).format('YYYY-MM-DD HH:mm')
        if (e.type === 'order') {
          const d = e.data as SpotTimelineOrder
          return {
            title: time,
            cardTitle: (
              <Space>
                <Tag color={meta.color} style={{ marginInlineEnd: 0 }}>
                  {meta.label}
                </Tag>
                {d.side === 'BUY' ? '买入' : '卖出'} {d.symbol.replace('USDT', '')}
              </Space>
            ),
            cardSubtitle: d.status,
            cardDetailedText: <OrderCard data={d} />,
          }
        }
        if (e.type === 'review') {
          const d = e.data as SpotTimelineReview
          return {
            title: time,
            cardTitle: (
              <Space>
                <Tag color={meta.color} style={{ marginInlineEnd: 0 }}>
                  {meta.label}
                </Tag>
                {d.week}
              </Space>
            ),
            cardSubtitle: d.regime,
            cardDetailedText: <ReviewCard data={d} />,
          }
        }
        const d = e.data as SpotTimelineDaily
        return {
          title: time,
          cardTitle: (
            <Space>
              <Tag color={meta.color} style={{ marginInlineEnd: 0 }}>
                {meta.label}
              </Tag>
              {d.as_of ? `截至 ${d.as_of}` : ''}
            </Space>
          ),
          cardSubtitle: undefined,
          cardDetailedText: <DailyCard data={d} />,
        }
      }),
    [filtered],
  )

  return (
    <Card
      size="small"
      title="历史记录"
      extra={
        <Space>
          <Segmented<TypeFilter>
            value={filter}
            onChange={setFilter}
            options={[
              { label: '全部', value: 'all' },
              { label: '交易', value: 'order' },
              { label: 'AI 研判', value: 'review' },
              { label: '每日评分', value: 'daily_check' },
            ]}
          />
          <Button icon={<ReloadOutlined />} size="small" onClick={load} loading={loading} />
        </Space>
      }
    >
      {loading && items.length === 0 ? (
        <div style={{ textAlign: 'center', padding: 60 }}>
          <Spin />
        </div>
      ) : filtered.length === 0 ? (
        <Empty description="暂无历史记录（每日 08:08 评分日检后自动生成）" />
      ) : (
        <div style={{ height: 'calc(100vh - 300px)', overflowY: 'auto' }}>
          <Chrono
            items={chronoItems}
            mode="VERTICAL"
            slideShow={false}
            enableBreakPoint={false}
            disableToolbar
            theme={{
              primary: '#1677ff',
              secondary: '#f5f5f5',
              cardBgColor: '#ffffff',
              cardTitleColor: 'rgba(0,0,0,0.88)',
              cardSubtitleColor: 'rgba(0,0,0,0.45)',
              cardDetailsColor: 'rgba(0,0,0,0.88)',
              titleColor: 'rgba(0,0,0,0.65)',
              titleColorActive: '#1677ff',
            }}
            fontSizes={{ cardTitle: '14px', cardSubtitle: '12px', cardText: '13px', title: '12px' }}
          />
        </div>
      )}
    </Card>
  )
}
