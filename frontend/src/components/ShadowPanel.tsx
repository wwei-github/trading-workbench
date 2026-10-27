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
  ShadowBucketStat, ShadowFeatureReport, ShadowReentryStat,
} from '../types'

const { Text } = Typography

// 桶中文标签（对照桶标灰；unknown/未来新增桶回退原文）
const BUCKET_META: Record<string, string> = {
  diverge_bear: '价升·CVD背离',
  diverge_bull: '价跌·CVD背离',
  confirm: '价量同向',
  sweep_reclaim: '插针收回',
  sweep_open: '插针未收回',
  no_sweep: '干净触碰',
  rec60_69: '评分 60~69（弱共振段）',
  rec70plus: '评分 70+（多重共振段）',
  r2_high: '强趋势 R²≥0.7',
  r2_mid: '弱趋势 R²0.4~0.7',
  r2_low: '震荡 R²<0.4',
}
const CONTROL_BUCKETS = new Set(['confirm']) // 对照桶（非信号桶）

// 信号类型中文标签（CARD-10 分类型读数，2026-10-27 风险复核 R1-A；unknown 回退原文）
const SIGNAL_TYPE_META: Record<string, string> = {
  trigger_breakout: '突破',
  trigger_trend_breakout: '趋势突破',
  trigger_pullback_confirm: '回踩确认',
  unknown: '未知（宿主已清）',
}

// 标准卡含 avg_r 判定项的卡（avg R 列仅这些卡渲染）：
// CARD-09 = trade_record 宿主真实盈亏 R；CARD-01/10 = scan_result 宿主建议回放
// ev_r（预注册 2026-10-19，详见 docs/14 §十一 遗留问题 1 收口）
const AVG_R_CARDS = new Set(['CARD-01', 'CARD-09', 'CARD-10'])

// 再入场前置桶中文标签（docs/13 #12：CARD-07；unknown 回退原文）
const REENTRY_BUCKET_META: Record<string, { label: string; tone?: 'success' | 'danger' }> = {
  structure_ok: { label: '结构完好', tone: 'success' },
  structure_broken: { label: '结构破坏', tone: 'danger' },
}

function pct(v: number | null | undefined): string {
  return v === null || v === undefined ? '—' : `${(v * 100).toFixed(0)}%`
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
          {seq.locked && seq.declared_at && (
            <div style={{ marginTop: 4, color: '#faad14' }}>
              早停宣告已落锁（{new Date(seq.declared_at).toLocaleString()}）——
              首次跨界即终局，此后不随新样本回摆（预注册语义）
            </div>
          )}
        </div>
      }
    >
      <Tag color={meta.color}>{meta.label}</Tag>
    </Tooltip>
  )
}

// 达标状态标签三态（2026-10-10 开庭日期感知，docs/14 §七 时间锁）：
// 样本不足 = 观察中（橙）；样本达标但未到预注册开庭日 = 达标·MM-DD 开庭（蓝，
// §八 人工裁决不得提前——时间窗防单一行情 regime 的自相关样本，与样本量正交）；
// 达标且到期（或无日历窗的到量即判卡 court_date=null）= 可开庭（绿）。
// 注意（2026-10-19，docs/14 §十一 遗留问题 2）：sufficient 为**全卡** counted 口径、
// **必要非充分**——各卡真实判据含**分桶**门槛（如 CARD-01 n≥80/桶、CARD-10 判定桶
// 各≥60），绿灯只保证够开庭样本，判定仍须人工核对 §七 卡文本。
const COURT_READY_TIP = '绿灯=已达卡最小样本（全卡 counted 口径，必要非充分）：各卡真实判据含分桶门槛（如 CARD-01 n≥80/桶、CARD-10 判定桶各≥60），判定须人工核对 docs/14 §七 卡文本'
function SufficiencyTag({ report, count }: { report: ShadowFeatureReport; count: number }) {
  if (!report.sufficient) {
    return <Tag color="orange">观察中 {count}/{report.min_sample}</Tag>
  }
  const court = report.court_date
  if (court) {
    const today = new Date()
    today.setHours(0, 0, 0, 0)
    const days = Math.round((new Date(`${court}T00:00:00`).getTime() - today.getTime()) / 86_400_000)
    if (days > 0) {
      return (
        <Tooltip title={`预注册开庭日 ${court}（docs/14 §七 时间锁）：到期前不进人工裁决，样本继续累积覆盖更多行情阶段。${COURT_READY_TIP}`}>
          <Tag color="blue">达标 · {court.slice(5)} 开庭（剩 {days} 天）</Tag>
        </Tooltip>
      )
    }
    return (
      <Tooltip title={`预注册开庭日 ${court} 已到：可按 docs/14 §八 流程人工裁决（快照 → 标准卡核对 → 拍板）。${COURT_READY_TIP}`}>
        <Tag color="green">可开庭</Tag>
      </Tooltip>
    )
  }
  return (
    <Tooltip title={`到量即判卡（无日历窗）：${COURT_READY_TIP}`}>
      <Tag color="green">可开庭</Tag>
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
        // ?? null 兜底 undefined：胜率缺失时不得算出 NaNpp
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
    // avg R 判定列：仅标准卡含 avg_r 判定项的卡渲染——CARD-09（trade_record
    // 宿主，读数即真实盈亏风险倍数）、CARD-01/10（scan_result 宿主，读数为
    // 建议回放 ev_r，预注册 2026-10-19，见 docs/14 §十一）；无此字段的卡不渲染
    ...(AVG_R_CARDS.has(report.card)
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
          <SufficiencyTag report={report} count={base.counted ?? 0} />
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
      {/* CARD-08 分族读数（2026-10-27 开庭延期）：标准卡主判定 = swing 族，
          retest 仅观察列；既有上表仍为混合聚合（向后兼容） */}
      {report.family_buckets && Object.keys(report.family_buckets).length > 0 && (
        <Text type="secondary" style={{ fontSize: 12, display: 'block', marginTop: 8 }}>
          分族读数：
          {Object.entries(report.family_buckets).map(([fam, f]) => (
            <span key={fam}>
              {' '}{fam === 'swing' ? 'swing（主判定）' : fam === 'retest' ? 'retest（观察）' : fam}
              {' '}n={f.baseline.n}
              {f.buckets.length > 0 && ` · ${f.buckets.map((b) =>
                `${BUCKET_META[b.bucket] || b.bucket} ${pct(b.win_rate)}（n=${b.n}）`).join(' · ')}`}
            </span>
          ))}
        </Text>
      )}
      {/* CARD-10 分信号类型读数（2026-10-27 风险复核 R1-A）：核对 r2_low 折价
          是否该限定突破类——仅读数，不影响判定；上表仍为混合聚合 */}
      {report.signal_type_buckets && Object.keys(report.signal_type_buckets).length > 0 && (
        <Text type="secondary" style={{ fontSize: 12, display: 'block', marginTop: 4 }}>
          分信号类型读数：
          {Object.entries(report.signal_type_buckets).map(([sig, s]) => (
            <span key={sig}>
              {' '}{SIGNAL_TYPE_META[sig] || sig}
              {' '}n={s.baseline.n}
              {s.buckets.length > 0 && ` · ${s.buckets.map((b) =>
                `${BUCKET_META[b.bucket] || b.bucket} ${pct(b.win_rate)}（n=${b.n}）`).join(' · ')}`}
            </span>
          ))}
        </Text>
      )}
    </Card>
  )
}

// 再入场前置卡（CARD-07，docs/13 #12）：trade_record 宿主——回答「止损后
// 结构完好时价格同向续走（回到开仓价）的概率」。读数是续走率 continued/n，
// 既非胜率也非反事实 ΣΔR；分层：正式网主判定，测试网对照
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
          <SufficiencyTag report={report} count={base.n} />
          <SeqTag seq={report.sequential} />
          {/* 2026-10-27 开庭：本卡 2026-10-19 已裁决（SPRT 早停落锁 h1），
              面板三态里的「可开庭」仅为样本口径，不再构成新裁决信号 */}
          <Tooltip title="2026-10-19 已按 docs/14 §八 裁决：SPRT 早停落锁 h1（LLR 2.9036 > 上界 2.7726），进 #12 预注册回放前需回放样本 n≥60；本卡不再重复裁决">
            <Tag>已裁决 2026-10-19 · 回放待 n≥60</Tag>
          </Tooltip>
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
              r.card === 'CARD-07'
                ? <ReentryFeatureCard key={r.feature} report={r} />
                : <FeatureCard key={r.feature} report={r} />
            ))}
          </div>
        )}
      </Spin>
    </div>
  )
}
