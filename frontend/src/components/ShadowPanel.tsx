/**
 * 影子实验面板（复盘 Tab 子页，docs/14，2026-10-04）
 * 只读展示影子特征分桶评估：定时扫描打标 → 复盘归因 → 分桶胜率统计。
 * 铁律：影子只统计不决策——达标（有效归因样本 ≥ 卡最小样本）≠ 升格，
 * 升格必须走 docs/14 §八 人工预注册流程，本面板不提供任何开关。
 */
import { useCallback, useEffect, useState } from 'react'
import {
  Button, Card, Empty, Select, Space, Spin, Table, Tag, Tooltip, Typography, message,
} from 'antd'
import { ReloadOutlined } from '@ant-design/icons'
import type { ColumnsType } from 'antd/es/table'
import { shadowApi } from '../api/shadow'
import type { ShadowBucketStat, ShadowFeatureReport } from '../types'

const { Text } = Typography

// 桶中文标签（对照桶标灰；unknown/未来新增桶回退原文）
const BUCKET_META: Record<string, string> = {
  diverge_bear: '价升·CVD背离',
  diverge_bull: '价跌·CVD背离',
  confirm: '价量同向',
  compress: '波动收缩',
  expand: '波动扩张',
  normal: '常态',
}
const CONTROL_BUCKETS = new Set(['confirm', 'normal']) // 对照桶（非信号桶）

function pct(v: number | null | undefined): string {
  return v === null || v === undefined ? '—' : `${(v * 100).toFixed(0)}%`
}

function FeatureCard({ report }: { report: ShadowFeatureReport }) {
  const base = report.baseline

  const columns: ColumnsType<ShadowBucketStat & { key: string }> = [
    {
      title: '分桶', dataIndex: 'bucket', key: 'bucket', width: 150,
      render: (v: string, r) => (
        <Space size={4}>
          <Text strong={!CONTROL_BUCKETS.has(v)}>{BUCKET_META[v] || v}</Text>
          {CONTROL_BUCKETS.has(v) && <Tag style={{ fontSize: 11 }}>对照</Tag>}
          {r.insufficient && (
            <Tooltip title="标记数 < 30，统计意义有限，仅供参考">
              <Tag color="default" style={{ fontSize: 11 }}>小样本</Tag>
            </Tooltip>
          )}
        </Space>
      ),
    },
    { title: '标记 n', dataIndex: 'n', key: 'n', width: 80, align: 'right' },
    { title: '有效归因', dataIndex: 'counted', key: 'counted', width: 90, align: 'right',
      render: (v: number) => <Text type="secondary">{v}</Text> },
    { title: '胜率', key: 'wr', width: 80, align: 'right',
      render: (_: unknown, r) => <Text strong>{pct(r.win_rate)}</Text> },
    {
      title: 'vs 基线', key: 'diff', width: 90, align: 'right',
      render: (_: unknown, r) => {
        if (r.bucket === '__baseline__' || r.win_rate === null || base.win_rate === null) return '—'
        const d = (r.win_rate - base.win_rate) * 100
        if (Math.abs(d) < 0.5) return <Text type="secondary">±0pp</Text>
        return (
          <Text type={d > 0 ? 'success' : 'danger'}>
            {d > 0 ? '+' : ''}{d.toFixed(0)}pp
          </Text>
        )
      },
    },
  ]

  const rows: (ShadowBucketStat & { key: string })[] = [
    { key: '__baseline__', bucket: '基线（全部合计）', n: base.n, counted: base.counted, win_rate: base.win_rate },
    ...report.buckets.map((b) => ({ ...b, key: b.bucket })),
  ]

  return (
    <Card
      size="small"
      title={
        <Space size={8}>
          <Text strong>{report.name}</Text>
          <Tooltip title={`预注册标准卡 ${report.card}：口径与最小样本预注册后不可回调（docs/14 §七）`}>
            <Tag color="geekblue">{report.card}</Tag>
          </Tooltip>
          {report.sufficient
            ? <Tag color="green">样本达标（{report.min_sample}）</Tag>
            : <Tag color="orange">观察中 {base.counted}/{report.min_sample}</Tag>}
        </Space>
      }
      extra={<Text type="secondary" style={{ fontSize: 12 }}>近 {report.window_days} 天</Text>}
    >
      <Table
        rowKey="key"
        columns={columns}
        dataSource={rows}
        size="small"
        pagination={false}
        scroll={{ y: 240 }}
        locale={{ emptyText: <Empty description="该特征暂无影子标记" /> }}
      />
    </Card>
  )
}

export default function ShadowPanel() {
  const [reports, setReports] = useState<ShadowFeatureReport[]>([])
  const [loading, setLoading] = useState(false)
  const [windowDays, setWindowDays] = useState(30)

  const fetchData = useCallback(async (days: number) => {
    setLoading(true)
    try {
      setReports(await shadowApi.report(days))
    } catch (e: any) {
      message.error(e?.response?.data?.detail || '获取影子评估报告失败')
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    fetchData(windowDays)
  }, [fetchData, windowDays])

  const hasAnyMark = reports.some((r) => r.baseline.n > 0)

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
      <Card size="small">
        <Space style={{ width: '100%', justifyContent: 'space-between' }}>
          <Text type="secondary" style={{ fontSize: 12 }}>
            影子模式（docs/14）：定时扫描命中的信号只打标记录、不参与任何决策，经复盘归因后按特征分桶
            统计胜率——用真实后续走势验证特征有效性。达标 ≠ 升格，升格走人工预注册流程
          </Text>
          <Space>
            <Select
              size="small"
              value={windowDays}
              onChange={(v) => setWindowDays(v)}
              options={[{ value: 7, label: '近 7 天' }, { value: 30, label: '近 30 天' }, { value: 90, label: '近 90 天' }]}
              style={{ width: 96 }}
            />
            <Button icon={<ReloadOutlined />} onClick={() => fetchData(windowDays)}>刷新</Button>
          </Space>
        </Space>
      </Card>

      <Spin spinning={loading}>
        {!hasAnyMark ? (
          <Empty
            description="暂无影子标记（等待定时扫描打标；手动扫描不计入观察总体）"
            style={{ marginTop: 60 }}
          />
        ) : (
          <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
            {reports.map((r) => <FeatureCard key={r.feature} report={r} />)}
          </div>
        )}
      </Spin>
    </div>
  )
}
