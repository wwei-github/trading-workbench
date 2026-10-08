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
import type {
  ShadowBucketStat, ShadowFeatureReport, ShadowProtectStat, ShadowReentryStat,
} from '../types'

const { Text } = Typography

// 桶中文标签（对照桶标灰；unknown/未来新增桶回退原文）
const BUCKET_META: Record<string, string> = {
  diverge_bear: '价升·CVD背离',
  diverge_bull: '价跌·CVD背离',
  confirm: '价量同向',
  compress: '波动收缩',
  expand: '波动扩张',
  normal: '常态',
  sweep_reclaim: '插针收回',
  sweep_open: '插针未收回',
  no_sweep: '干净触碰',
  rec60_69: '评分 60~69（弱共振段）',
  rec70plus: '评分 70+（多重共振段）',
  r2_high: '强趋势 R²≥0.7',
  r2_mid: '弱趋势 R²0.4~0.7',
  r2_low: '震荡 R²<0.4',
}
const CONTROL_BUCKETS = new Set(['confirm', 'normal']) // 对照桶（非信号桶）

// 早期保护桶中文标签（docs/15 §五：CARD-05 A腿 / CARD-06 B腿；unknown 回退原文）
const PROTECT_BUCKET_META: Record<string, { label: string; tone?: 'success' | 'danger' }> = {
  saved: { label: '真亏→保本救回', tone: 'success' },
  injured: { label: '真赢→被扫出', tone: 'danger' },
  no_arm: { label: '未武装（MFE<0.8R）' },
  carry: { label: '武装未回踩' },
  protected: { label: '真亏→TP0救回', tone: 'success' },
  survived: { label: 'TP0后走到原出场' },
  below_gate: { label: 'TP1<1.2R 未挂TP0' },
  untouched: { label: 'TP0 未成交' },
}

// 再入场前置桶中文标签（docs/13 #12：CARD-07；unknown 回退原文）
const REENTRY_BUCKET_META: Record<string, { label: string; tone?: 'success' | 'danger' }> = {
  structure_ok: { label: '结构完好', tone: 'success' },
  structure_broken: { label: '结构破坏', tone: 'danger' },
}

function pct(v: number | null | undefined): string {
  return v === null || v === undefined ? '—' : `${(v * 100).toFixed(0)}%`
}

function rStr(v: number | null | undefined): string {
  return v === null || v === undefined ? '—' : v.toFixed(2)
}

function deltaR(v: number | null | undefined) {
  if (v === null || v === undefined) return <Text type="secondary">—</Text>
  if (Math.abs(v) < 0.005) return <Text type="secondary">0.00R</Text>
  return (
    <Text type={v > 0 ? 'success' : 'danger'}>
      {v > 0 ? '+' : ''}{v.toFixed(2)}R
    </Text>
  )
}

// P-08 序贯判读标签（CARD-07/09）：SPRT 早停三态 + 悬停展示读数
// h1=早停达标方向 / h0=早停否定方向 / continue=续观察（docs/14 §七 预注册）
function SeqTag({ seq }: { seq: ShadowFeatureReport['sequential'] }) {
  if (!seq) return null
  const meta = seq.verdict === 'h1'
    ? { color: 'green' as const, label: '序贯达标（早停）' }
    : seq.verdict === 'h0'
      ? { color: 'red' as const, label: '序贯否定（早停）' }
      : { color: 'default' as const, label: '序贯：继续观察' }
  return (
    <Tooltip
      title={
        <div style={{ maxWidth: 340 }}>
          <div>P-08 序贯检验（Wald SPRT，α=0.05 / β=0.20，参数预注册不可回调）</div>
          <div>LLR = {seq.llr.toFixed(2)}（停用界 [{seq.lower.toFixed(2)}, {seq.upper.toFixed(2)}]）· n = {seq.n}</div>
          {seq.wins !== undefined && seq.posterior && (
            <div>
              比例卡：{seq.wins}/{seq.n} · Beta 后验均值 {(seq.posterior.mean * 100).toFixed(1)}%
              （95% CI [{(seq.posterior.ci95_low * 100).toFixed(1)}%, {(seq.posterior.ci95_high * 100).toFixed(1)}%]）
            </div>
          )}
          {seq.mean !== undefined && (
            <div>均值卡：样本均值 {seq.mean.toFixed(2)}R（预注册 δ = {seq.delta}R）</div>
          )}
        </div>
      }
    >
      <Tag color={meta.color}>{meta.label}</Tag>
    </Tooltip>
  )
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
        // ?? null 兜底 undefined：CARD-05/06 无 win_rate 字段时不得算出 NaNpp
        if (r.key === '__baseline__' || (r.win_rate ?? null) === null || (base.win_rate ?? null) === null) return '—'
        const d = ((r.win_rate ?? 0) - (base.win_rate ?? 0)) * 100
        if (Math.abs(d) < 0.5) return <Text type="secondary">±0pp</Text>
        return (
          <Text type={d > 0 ? 'success' : 'danger'}>
            {d > 0 ? '+' : ''}{d.toFixed(0)}pp
          </Text>
        )
      },
    },
    // CARD-09 专属列：读数即真实盈亏（trade_record 宿主），avg R 是预注册
    // 裁决主读数之一（avg_r(60-69) < 0 才达标）；其他卡无此字段不渲染
    ...(report.card === 'CARD-09'
      ? [{
          title: 'avg R', key: 'avgr', width: 80, align: 'right' as const,
          render: (_: unknown, r: ShadowBucketStat & { key: string }) => {
            const v = r.avg_r
            if (v == null) return <Text type="secondary">—</Text>
            return (
              <Text strong type={v > 0 ? 'success' : 'danger'}>
                {v > 0 ? '+' : ''}{v.toFixed(2)}
              </Text>
            )
          },
        }]
      : []),
  ]

  const rows: (ShadowBucketStat & { key: string })[] = [
    { key: '__baseline__', bucket: '基线（全部合计）', n: base.n, counted: base.counted, win_rate: base.win_rate, avg_r: base.avg_r },
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
          <SeqTag seq={report.sequential} />
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

// 早期保护反事实卡（CARD-05/06，docs/15 §五）：trade_record 宿主，
// 无胜率口径——读数是反事实重放的 ΣΔR / decided，另附测试网对照组（分层不进判定）
function ProtectFeatureCard({ report }: { report: ShadowFeatureReport }) {
  const base = report.baseline
  const decided = report.decided ?? base.decided ?? 0
  const tb = report.testnet_baseline

  const columns: ColumnsType<ShadowBucketStat & Partial<ShadowProtectStat> & { key: string }> = [
    {
      title: '分桶', dataIndex: 'bucket', key: 'bucket', width: 190,
      render: (v: string, r) => {
        const meta = PROTECT_BUCKET_META[v]
        return (
          <Space size={4}>
            <Text strong={!!meta?.tone} type={meta?.tone}>{meta?.label ?? v}</Text>
            {r.insufficient && (
              <Tooltip title="n < 30，统计意义有限，仅供参考">
                <Tag style={{ fontSize: 11 }}>小样本</Tag>
              </Tooltip>
            )}
          </Space>
        )
      },
    },
    { title: 'n', dataIndex: 'n', key: 'n', width: 70, align: 'right' },
    { title: 'decided', key: 'decided', width: 80, align: 'right',
      render: (_: unknown, r) => <Text type="secondary">{r.decided ?? 0}</Text> },
    { title: 'ΣΔR', key: 'delta', width: 90, align: 'right',
      render: (_: unknown, r) => deltaR(r.decided_sum_delta_r ?? r.sum_delta_r) },
    { title: 'real→cf（R）', key: 'rcf', width: 110, align: 'right',
      render: (_: unknown, r) => (
        <Text type="secondary">{rStr(r.avg_real_r)} → {rStr(r.avg_cf_r)}</Text>
      ) },
  ]

  const rows: (ShadowBucketStat & Partial<ShadowProtectStat> & { key: string })[] = [
    {
      key: '__baseline__', bucket: '基线（全部合计）', n: base.n,
      decided: base.decided, sum_delta_r: base.sum_delta_r,
      decided_sum_delta_r: base.decided_sum_delta_r,
      avg_real_r: base.avg_real_r, avg_cf_r: base.avg_cf_r,
    },
    ...report.buckets.map((b) => ({ ...b, key: b.bucket })),
  ]

  return (
    <Card
      size="small"
      title={
        <Space size={8}>
          <Text strong>{report.name}</Text>
          <Tooltip title={`预注册标准卡 ${report.card}：反事实重放口径（docs/15 §五）——ΣΔR>0 且 saved:injured 比达标才可升格`}>
            <Tag color="geekblue">{report.card}</Tag>
          </Tooltip>
          {report.sufficient
            ? <Tag color="green">样本达标（{report.min_sample}）</Tag>
            : <Tag color="orange">观察中 {decided}/{report.min_sample}</Tag>}
        </Space>
      }
      extra={<Text type="secondary" style={{ fontSize: 12 }}>近 {report.window_days} 天 · 正式网</Text>}
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
      {(report.excluded_n ?? 0) > 0 && (
        <Text type="secondary" style={{ fontSize: 12, display: 'block', marginTop: 8 }}>
          已剔除非机械出场（manual/liquidation）{report.excluded_n} 笔：反事实按机械原计划出场重放，与真实出场不可比（docs/16 P-04）
        </Text>
      )}
      {tb && tb.n > 0 && (
        <Text type="secondary" style={{ fontSize: 12, display: 'block', marginTop: 8 }}>
          测试网对照（分层统计，不进判定）：n={tb.n} · decided={tb.decided} · ΣΔR={rStr(tb.decided_sum_delta_r ?? tb.sum_delta_r)}R
          {' '}· 桶：{Object.entries(tb.buckets).map(([k, n]) => `${PROTECT_BUCKET_META[k]?.label ?? k} ${n}`).join(' · ')}
        </Text>
      )}
    </Card>
  )
}

// 再入场前置卡（CARD-07，docs/13 #12）：trade_record 宿主——回答「止损后
// 结构完好时价格同向续走（回到开仓价）的概率」。读数是续走率 continued/n，
// 既非胜率也非反事实 ΣΔR；分层同 CARD-05/06（正式网主判定，测试网对照）
function ReentryFeatureCard({ report }: { report: ShadowFeatureReport }) {
  const base = report.baseline
  const tb = report.testnet_baseline

  const columns: ColumnsType<ShadowBucketStat & Partial<ShadowReentryStat> & { key: string }> = [
    {
      title: '分桶', dataIndex: 'bucket', key: 'bucket', width: 150,
      render: (v: string, r) => {
        const meta = REENTRY_BUCKET_META[v]
        return (
          <Space size={4}>
            <Text strong={!!meta?.tone} type={meta?.tone}>{meta?.label ?? v}</Text>
            {r.insufficient && (
              <Tooltip title="n < 30，统计意义有限，仅供参考">
                <Tag style={{ fontSize: 11 }}>小样本</Tag>
              </Tooltip>
            )}
          </Space>
        )
      },
    },
    { title: 'n', dataIndex: 'n', key: 'n', width: 70, align: 'right' },
    { title: '续走', key: 'continued', width: 80, align: 'right',
      render: (_: unknown, r) => <Text type="secondary">{r.continued ?? 0}</Text> },
    { title: '续走率', key: 'rate', width: 90, align: 'right',
      render: (_: unknown, r) => <Text strong>{pct(r.cont_rate)}</Text> },
  ]

  const rows: (ShadowBucketStat & Partial<ShadowReentryStat> & { key: string })[] = [
    { key: '__baseline__', bucket: '基线（全部止损单）', n: base.n,
      continued: base.continued, cont_rate: base.cont_rate },
    ...report.buckets.map((b) => ({ ...b, key: b.bucket })),
  ]

  return (
    <Card
      size="small"
      title={
        <Space size={8}>
          <Text strong>{report.name}</Text>
          <Tooltip title={`预注册标准卡 ${report.card}：止损后 24h 回开仓价为「续走」。P(续走|结构完好) ≥40% 才进 #12 预注册回放，<40% 直接关闭（docs/13 #12）`}>
            <Tag color="geekblue">{report.card}</Tag>
          </Tooltip>
          {report.sufficient
            ? <Tag color="green">样本达标（{report.min_sample}）</Tag>
            : <Tag color="orange">观察中 {base.n}/{report.min_sample}</Tag>}
          <SeqTag seq={report.sequential} />
        </Space>
      }
      extra={<Text type="secondary" style={{ fontSize: 12 }}>近 {report.window_days} 天 · 正式网</Text>}
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
      {tb && tb.n > 0 && (
        <Text type="secondary" style={{ fontSize: 12, display: 'block', marginTop: 8 }}>
          测试网对照（分层统计，不进判定）：n={tb.n} · 续走率 {pct(tb.cont_rate)}
          {' '}· 桶：{Object.entries(tb.buckets).map(([k, n]) => `${REENTRY_BUCKET_META[k]?.label ?? k} ${n}`).join(' · ')}
        </Text>
      )}
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
            {reports.map((r) => (
              r.card === 'CARD-05' || r.card === 'CARD-06'
                ? <ProtectFeatureCard key={r.feature} report={r} />
                : r.card === 'CARD-07'
                  ? <ReentryFeatureCard key={r.feature} report={r} />
                  : <FeatureCard key={r.feature} report={r} />
            ))}
          </div>
        )}
      </Spin>
    </div>
  )
}
