import { useCallback, useEffect, useState } from 'react'
import {
  Button,
  Card,
  Col,
  Empty,
  Radio,
  Row,
  Spin,
  Statistic,
  Table,
  Tag,
  Tooltip,
  Typography,
  message,
} from 'antd'
import { ReloadOutlined, QuestionCircleOutlined, InfoCircleOutlined } from '@ant-design/icons'
import type { ColumnsType } from 'antd/es/table'
import { scanApi } from '../api/scan'
import { useScanStore } from '../stores/scanStore'
import { SCHEME_UP_DOWN, schemeTag } from '../utils/scheme'
import type { LatencyMetric, ReviewGroupStat, ReviewStats } from '../types'
import { SIGNAL_TYPE_MAP } from '../constants/labels'

/**
 * 复盘统计页：AI 建议开单 24h 后逐K回放定论的汇总统计。
 * 胜率口径：expired（超时）不计入胜率分母。
 */

// 时延表行（批次 B1）
interface LatencyRow {
  key: string
  label: string
  hint: string
  metric: LatencyMetric
}
export default function ReviewStatsPanel() {
  // 统计窗口天数（默认 30 天）
  const [days, setDays] = useState<7 | 30 | 90>(30)
  const [stats, setStats] = useState<ReviewStats | null>(null)
  const [loading, setLoading] = useState(false)

  const fetchData = useCallback(async (d: number) => {
    setLoading(true)
    try {
      const data = await scanApi.reviewStats(d)
      setStats(data)
    } catch (e: any) {
      message.error(e?.response?.data?.detail || '获取复盘统计失败')
    } finally {
      setLoading(false)
    }
  }, [])

  // 初始加载 + 切换窗口时重新拉取
  useEffect(() => {
    fetchData(days)
  }, [days, fetchData])

  // 胜率百分比展示（>=50% 涨色，<50% 跌色；随全局红涨绿跌/绿涨红跌方案翻转）
  const colorScheme = useScanStore((s) => s.colorScheme)
  const renderWinRate = (rate: number) => {
    const pct = (rate * 100).toFixed(1) + '%'
    const color = rate >= 0.5 ? SCHEME_UP_DOWN[colorScheme].up : SCHEME_UP_DOWN[colorScheme].down
    return <span style={{ color, fontWeight: 600 }}>{pct}</span>
  }

  // 根据非空维度字段确定维度名与分组值（后端按 (signal_type, touch_ref) 联合分组：
  // 触发器类型先按信号类型成组，docs/22 起同类型再按回踩触点细分；旧结构分类/未识别统一归「未记录」）
  // 触点中文标签（与后端 trigger.TOUCH_LABELS 同源）
  const TOUCH_LABELS: Record<string, string> = {
    retest: '突破回测位',
    swing: '摆动结构位',
    ema21: 'EMA21',
    ema21_4h: 'EMA21(4h)',
    ema55: 'EMA55',
    ema144: 'EMA144',
  }
  const renderDim = (g: ReviewGroupStat): { dim: string; value: string } => {
    if (g.signal_type) {
      const base = SIGNAL_TYPE_MAP[g.signal_type]?.label || g.signal_type
      return {
        dim: g.touch_ref ? '信号类型·触点' : '信号类型',
        value: g.touch_ref
          ? `${base}·${TOUCH_LABELS[g.touch_ref] || g.touch_ref}`
          : base,
      }
    }
    return { dim: '其他', value: '-' }
  }

  // 期望值（R 倍数）：>0 表示该组每笔平均净赚（含费用假设后仍为正）
  const renderEv = (v: number | null) =>
    v == null ? (
      '-'
    ) : (
      <span style={{ color: v > 0 ? SCHEME_UP_DOWN[colorScheme].up : SCHEME_UP_DOWN[colorScheme].down, fontWeight: 600 }}>
        {v.toFixed(2)}R
      </span>
    )

  const columns: ColumnsType<ReviewGroupStat> = [
    {
      title: '维度',
      key: 'dim',
      width: 90,
      render: (_: unknown, g: ReviewGroupStat) => renderDim(g).dim,
    },
    {
      title: '分组',
      key: 'group',
      render: (_: unknown, g: ReviewGroupStat) => renderDim(g).value,
    },
    {
      title: '总数',
      dataIndex: 'total',
      key: 'total',
      width: 70,
      sorter: (a, b) => a.total - b.total,
    },
    {
      title: (
        <Tooltip title="胜率口径：expired（超时）不计入胜率分母">
          胜率
        </Tooltip>
      ),
      dataIndex: 'win_rate',
      key: 'win_rate',
      width: 90,
      sorter: (a, b) => a.win_rate - b.win_rate,
      render: (v: number) => renderWinRate(v),
    },
    {
      title: '胜·TP1',
      dataIndex: 'win_tp1',
      key: 'win_tp1',
      width: 80,
      render: (v: number) => <Tag color={schemeTag(colorScheme).up}>{v}</Tag>,
    },
    {
      title: '胜·TP2',
      dataIndex: 'win_tp2',
      key: 'win_tp2',
      width: 80,
      render: (v: number) => <Tag color={schemeTag(colorScheme).up}>{v}</Tag>,
    },
    {
      title: '负',
      dataIndex: 'loss',
      key: 'loss',
      width: 60,
      render: (v: number) => <Tag color={schemeTag(colorScheme).down}>{v}</Tag>,
    },
    {
      title: '超时',
      dataIndex: 'expired',
      key: 'expired',
      width: 70,
      render: (v: number) => <Tag color="default">{v}</Tag>,
    },
    {
      title: (
        <Tooltip title="期望值（R 倍数）：止损亏 1R、TP1 赚 RR1、TP2 赚 RR2，已扣费用假设（taker 往返 0.1% + 资金费 0.03%/8h）。>0 = 扣费后仍为正期望">
          期望值
        </Tooltip>
      ),
      dataIndex: 'ev_r',
      key: 'ev_r',
      width: 90,
      sorter: (a, b) => (a.ev_r ?? -99) - (b.ev_r ?? -99),
      render: (v: number | null) => renderEv(v),
    },
  ]

  // 版本分组表（规则版本 / 决策版本共用，审计 7.2 读数地基）：样本量与 95% 区间并列——
  // 区间跨 50%（胜率）或跨 0（EV）= 样本不足，不能据此判定版本优劣。
  const versionColumns = (
    versionTitle: string,
    dataIndex: 'rule_version' | 'decision_version',
  ): ColumnsType<ReviewGroupStat> => [
    {
      title: versionTitle,
      dataIndex,
      key: dataIndex,
      render: (v: string | number | null) =>
        v == null || v === 'legacy' ? (
          <Tooltip title="版本标记上线前的旧行，仅参考">
            <span>
              legacy <QuestionCircleOutlined style={{ color: '#999', fontSize: 12 }} />
            </span>
          </Tooltip>
        ) : dataIndex === 'decision_version' ? (
          `v${v}`
        ) : (
          v
        ),
    },
    {
      title: '样本',
      dataIndex: 'total',
      key: 'total',
      width: 70,
      sorter: (a, b) => a.total - b.total,
    },
    {
      title: '胜',
      key: 'win',
      width: 60,
      render: (_: unknown, g: ReviewGroupStat) => (
        <Tag color={schemeTag(colorScheme).up}>{g.win_tp1 + g.win_tp2}</Tag>
      ),
    },
    {
      title: '负',
      dataIndex: 'loss',
      key: 'loss',
      width: 60,
      render: (v: number) => <Tag color={schemeTag(colorScheme).down}>{v}</Tag>,
    },
    {
      title: (
        <Tooltip title="expired 不计分母；区间为 Wilson 95% 置信区间">
          胜率
        </Tooltip>
      ),
      dataIndex: 'win_rate',
      key: 'win_rate',
      width: 90,
      sorter: (a, b) => a.win_rate - b.win_rate,
      render: (v: number) => renderWinRate(v),
    },
    {
      title: (
        <Tooltip title="Wilson 95% 置信区间；区间跨 50% = 尚不能判断该组胜率优劣">
          胜率区间
        </Tooltip>
      ),
      key: 'win_ci',
      width: 100,
      render: (_: unknown, g: ReviewGroupStat) =>
        g.win_rate_ci_low == null || g.win_rate_ci_high == null
          ? '-'
          : `${(g.win_rate_ci_low * 100).toFixed(0)}~${(g.win_rate_ci_high * 100).toFixed(0)}%`,
    },
    {
      title: '平均EV(R)',
      dataIndex: 'ev_r',
      key: 'ev_r',
      width: 100,
      sorter: (a, b) => (a.ev_r ?? -99) - (b.ev_r ?? -99),
      render: (v: number | null) => renderEv(v),
    },
    {
      title: (
        <Tooltip title="平均 EV 的 95% 置信区间（正态近似）；区间跨 0 = 扣费后正负期望尚不显著">
          EV 95%CI
        </Tooltip>
      ),
      key: 'ev_ci',
      width: 120,
      render: (_: unknown, g: ReviewGroupStat) =>
        g.ev_r_ci_low == null || g.ev_r_ci_high == null
          ? '-'
          : `[${g.ev_r_ci_low.toFixed(2)}, ${g.ev_r_ci_high.toFixed(2)}]`,
    },
  ]

  const rvColumns = versionColumns('规则版本', 'rule_version')
  const dvColumns = versionColumns('决策版本', 'decision_version')

  const hasData = !!stats && stats.total > 0

  // 时延展示（批次 B1）：秒 → 人类可读
  const fmtSec = (v: number | null) =>
    v == null ? '-' : v < 60 ? `${v.toFixed(1)}s` : v < 3600 ? `${(v / 60).toFixed(1)}m` : `${(v / 3600).toFixed(1)}h`

  const latencyRows: LatencyRow[] =
    stats?.latency
      ? [
          {
            key: 'signal_age',
            label: '信号 → AI 结论',
            hint: 'signal_created_at → ai_finished_at；目标 P90 ≤ 5 分钟',
            metric: stats.latency.signal_age_s,
          },
          {
            key: 'ai_open',
            label: 'AI 结论 → 开仓',
            hint: 'ai_finished_at → 首次 opened_at（含风控/评委耗时）',
            metric: stats.latency.ai_to_open_s,
          },
          {
            key: 'signal_open',
            label: '信号 → 开仓',
            hint: 'signal_created_at → 首次 opened_at（端到端）',
            metric: stats.latency.signal_to_open_s,
          },
        ]
      : []

  const latencyColumns: ColumnsType<LatencyRow> = [
    {
      title: '阶段',
      key: 'label',
      render: (_: unknown, r: LatencyRow) => (
        <Tooltip title={r.hint}>
          <span>{r.label}</span>
        </Tooltip>
      ),
    },
    { title: '样本', key: 'samples', width: 80, render: (_: unknown, r: LatencyRow) => r.metric.samples },
    { title: 'P50', key: 'p50', width: 100, render: (_: unknown, r: LatencyRow) => fmtSec(r.metric.p50_s) },
    {
      title: 'P90',
      key: 'p90',
      width: 100,
      render: (_: unknown, r: LatencyRow) => {
        // signal→AI 目标 P90 ≤ 5min，超限标红提示链路慢
        const bad = r.key === 'signal_age' && r.metric.p90_s != null && r.metric.p90_s > 300
        return (
          <span style={{ color: bad ? SCHEME_UP_DOWN[colorScheme].down : undefined, fontWeight: bad ? 600 : undefined }}>
            {fmtSec(r.metric.p90_s)}
          </span>
        )
      },
    },
  ]

  // 4h 数据降级对比表（批次 B3）：降级 = 分析时 4h K线缺失，硬约束未生效
  const hdColumns: ColumnsType<ReviewGroupStat> = [
    {
      title: '4h 数据',
      key: 'htf_degraded',
      render: (_: unknown, g: ReviewGroupStat) =>
        g.htf_degraded ? (
          <Tooltip title="分析时 4h K线缺失：4h 硬约束未生效（fail-open），信号仅凭 1h 判断">
            <Tag color="warning">降级（无 4h）</Tag>
          </Tooltip>
        ) : (
          <Tag color="success">正常</Tag>
        ),
    },
    {
      title: '样本',
      dataIndex: 'total',
      key: 'total',
      width: 70,
      sorter: (a, b) => a.total - b.total,
    },
    {
      title: '胜率',
      dataIndex: 'win_rate',
      key: 'win_rate',
      width: 90,
      sorter: (a, b) => a.win_rate - b.win_rate,
      render: (v: number) => renderWinRate(v),
    },
    {
      title: '期望值',
      dataIndex: 'ev_r',
      key: 'ev_r',
      width: 90,
      sorter: (a, b) => (a.ev_r ?? -99) - (b.ev_r ?? -99),
      render: (v: number | null) => renderEv(v),
    },
    {
      title: (
        <Tooltip title="盈亏比 = 平均盈利 R / 平均亏损 R（扣费前原始 R；回放口径亏损恒 -1R，故通常等于平均盈利 R）">
          盈亏比
        </Tooltip>
      ),
      dataIndex: 'payoff',
      key: 'payoff',
      width: 90,
      render: (v: number | null) => (v == null ? '-' : `${v.toFixed(2)}`),
    },
  ]

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
      {/* 顶部：统计窗口切换 + 刷新 */}
      <Row justify="space-between" align="middle">
        <Col>
          <Radio.Group
            optionType="button"
            buttonStyle="solid"
            value={days}
            onChange={(e) => setDays(e.target.value)}
            options={[
              { value: 7, label: '7天' },
              { value: 30, label: '30天' },
              { value: 90, label: '90天' },
            ]}
          />
        </Col>
        <Col>
          <Button icon={<ReloadOutlined />} onClick={() => fetchData(days)}>
            刷新
          </Button>
        </Col>
      </Row>

      <Spin spinning={loading}>
        {hasData ? (
          <>
            {/* 汇总统计卡片 */}
            <Row gutter={[12, 12]}>
              <Col xs={12} sm={8} md={4}>
                <Card size="small">
                  <Statistic title="复盘总数" value={stats!.total} />
                </Card>
              </Col>
              <Col xs={12} sm={8} md={4}>
                <Card size="small">
                  <Statistic
                    title="胜率"
                    value={(stats!.win_rate * 100).toFixed(1)}
                    suffix="%"
                    valueStyle={{
                      color: stats!.win_rate >= 0.5 ? SCHEME_UP_DOWN[colorScheme].up : SCHEME_UP_DOWN[colorScheme].down,
                    }}
                  />
                </Card>
              </Col>
              <Col xs={12} sm={8} md={4}>
                <Card size="small">
                  <Statistic title="胜·TP1" value={stats!.win_tp1} valueStyle={{ color: SCHEME_UP_DOWN[colorScheme].up }} />
                </Card>
              </Col>
              <Col xs={12} sm={8} md={4}>
                <Card size="small">
                  <Statistic title="胜·TP2" value={stats!.win_tp2} valueStyle={{ color: SCHEME_UP_DOWN[colorScheme].up }} />
                </Card>
              </Col>
              <Col xs={12} sm={8} md={4}>
                <Card size="small">
                  <Statistic title="负·止损" value={stats!.loss} valueStyle={{ color: SCHEME_UP_DOWN[colorScheme].down }} />
                </Card>
              </Col>
              <Col xs={12} sm={8} md={4}>
                <Card size="small">
                  <Statistic title="超时" value={stats!.expired} />
                </Card>
              </Col>
              <Col xs={12} sm={8} md={4}>
                <Card size="small">
                  <Statistic
                    title={<Tooltip title="扣费用假设后的每笔平均期望（R 倍数）">期望值</Tooltip>}
                    value={stats!.ev_r == null ? '-' : stats!.ev_r.toFixed(2)}
                    suffix="R"
                    valueStyle={{
                      color: stats!.ev_r != null && stats!.ev_r > 0
                        ? SCHEME_UP_DOWN[colorScheme].up
                        : SCHEME_UP_DOWN[colorScheme].down,
                    }}
                  />
                </Card>
              </Col>
            </Row>

            {/* 信号时效（批次 B1）：三段时延 P50/P90，近 7 天窗口 */}
            {latencyRows.length > 0 && (
              <Card
                size="small"
                title={
                  <Tooltip title="signal_created_at → ai_finished_at → opened_at 三段时延，取近 7 天样本；P90 是关键路径验收口径（signal_age ≤ 5min）">
                    <span>信号时效 <QuestionCircleOutlined style={{ color: '#999', fontSize: 12 }} /></span>
                  </Tooltip>
                }
                styles={{ body: { padding: 0 } }}
              >
                <Table<LatencyRow>
                  rowKey="key"
                  size="small"
                  pagination={false}
                  dataSource={latencyRows}
                  columns={latencyColumns}
                />
              </Card>
            )}

            {/* 胜率口径说明（docs/24 A2）：建议质量口径 vs 实盘净胜率主考核口径 */}
            {stats!.win_rate_note && (
              <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                <InfoCircleOutlined style={{ marginRight: 4 }} />
                {stats!.win_rate_note}
              </Typography.Text>
            )}

            {/* 维度分组统计表 */}
            <Card
              size="small"
              title={
                <Tooltip title="胜率口径：expired（超时）不计入胜率分母">
                  <span>
                    分组统计 <QuestionCircleOutlined style={{ color: '#999', fontSize: 12 }} />
                  </span>
                </Tooltip>
              }
              styles={{ body: { padding: 0 } }}
            >
              <Table<ReviewGroupStat>
                rowKey={(g) =>
                  `${g.signal_type ?? ''}-${g.touch_ref ?? ''}-${g.position ?? ''}-${g.ema_state ?? ''}`
                }
                columns={columns}
                dataSource={stats!.groups}
                size="small"
                scroll={{ y: 320 }}
                pagination={false}
              />
            </Card>

            {/* 规则版本分组（docs/24 A0）：历史跨版本混合成绩不具证明力，按版本隔离对比 */}
            {stats!.rule_version_groups.length > 0 && (
              <Card
                size="small"
                title={
                  <Tooltip title="同版本内对比才有证明力：版本标记上线前的行归入 legacy，仅参考">
                    <span>规则版本分组 <QuestionCircleOutlined style={{ color: '#999', fontSize: 12 }} /></span>
                  </Tooltip>
                }
                styles={{ body: { padding: 0 } }}
              >
                <Table<ReviewGroupStat>
                  rowKey={(g) => `rv-${g.rule_version}`}
                  columns={rvColumns}
                  dataSource={stats!.rule_version_groups}
                  size="small"
                  pagination={false}
                />
              </Card>
            )}

            {/* 统一决策版本分组（审计 7.2 地基）：补丁/配置/提示词变更都会递增版本，
                同版本内对比才有证明力；legacy=版本机制上线前旧行 */}
            {stats!.decision_version_groups.length > 0 && (
              <Card
                size="small"
                title={
                  <Tooltip title="统一决策版本（补丁/配置/提示词变更递增）：同版本内对比才有证明力；胜率区间跨 50% 或 EV 区间跨 0 = 样本不足">
                    <span>决策版本分组 <QuestionCircleOutlined style={{ color: '#999', fontSize: 12 }} /></span>
                  </Tooltip>
                }
                styles={{ body: { padding: 0 } }}
              >
                <Table<ReviewGroupStat>
                  rowKey={(g) => `dv-${g.decision_version ?? 'legacy'}`}
                  columns={dvColumns}
                  dataSource={stats!.decision_version_groups}
                  size="small"
                  pagination={false}
                />
              </Card>
            )}

            {/* 4h 数据降级对比（批次 B3）：正常 vs 无 4h 两组，判断 fail-open 点质量 */}
            {stats!.htf_degraded_groups.length > 0 && (
              <Card
                size="small"
                title={
                  <Tooltip title="分析时 4h K线缺失 = 4h 硬约束未生效（AI 层 fail-open）。降级组胜率/期望显著更低 → 该放行点应收紧为 fail-closed 或降级折价仓位">
                    <span>4h 数据降级对比 <QuestionCircleOutlined style={{ color: '#999', fontSize: 12 }} /></span>
                  </Tooltip>
                }
                styles={{ body: { padding: 0 } }}
              >
                <Table<ReviewGroupStat>
                  rowKey={(g) => `hd-${g.htf_degraded ? 'degraded' : 'normal'}`}
                  columns={hdColumns}
                  dataSource={stats!.htf_degraded_groups}
                  size="small"
                  pagination={false}
                />
              </Card>
            )}

            {/* 双评委否决对比（docs/24 A5a）：被否决建议的假想回放 vs 通过单 */}
            {stats!.veto_stats && (
              <Card
                size="small"
                title={
                  <Tooltip title="veto 行不混入上方统计（=通过单口径）；被否决建议做假想回放单列对比——veto 组假想胜率/期望值反而更高 = 评委在错杀好单，需审视裁决质量">
                    <span>双评委否决对比 <QuestionCircleOutlined style={{ color: '#999', fontSize: 12 }} /></span>
                  </Tooltip>
                }
              >
                <Row gutter={[12, 12]}>
                  <Col xs={12} sm={8} md={4}>
                    <Statistic title="否决单样本" value={stats!.veto_stats.total} suffix="笔" />
                  </Col>
                  <Col xs={12} sm={8} md={4}>
                    <Statistic
                      title="否决单胜率"
                      value={(stats!.veto_stats.win_rate * 100).toFixed(1)}
                      suffix="%"
                      valueStyle={{
                        color: stats!.veto_stats.win_rate >= 0.5
                          ? SCHEME_UP_DOWN[colorScheme].up
                          : SCHEME_UP_DOWN[colorScheme].down,
                      }}
                    />
                  </Col>
                  <Col xs={12} sm={8} md={4}>
                    <Statistic
                      title="否决单期望值"
                      value={stats!.veto_stats.ev_r == null ? '-' : stats!.veto_stats.ev_r.toFixed(2)}
                      suffix={stats!.veto_stats.ev_r == null ? '' : 'R'}
                      valueStyle={{
                        color: stats!.veto_stats.ev_r != null && stats!.veto_stats.ev_r > 0
                          ? SCHEME_UP_DOWN[colorScheme].up
                          : SCHEME_UP_DOWN[colorScheme].down,
                      }}
                    />
                  </Col>
                  <Col xs={12} sm={8} md={4}>
                    <Statistic
                      title="通过单胜率"
                      value={(stats!.win_rate * 100).toFixed(1)}
                      suffix="%"
                      valueStyle={{
                        color: stats!.win_rate >= 0.5 ? SCHEME_UP_DOWN[colorScheme].up : SCHEME_UP_DOWN[colorScheme].down,
                      }}
                    />
                  </Col>
                  <Col xs={12} sm={8} md={4}>
                    <Statistic
                      title="通过单期望值"
                      value={stats!.ev_r == null ? '-' : stats!.ev_r.toFixed(2)}
                      suffix={stats!.ev_r == null ? '' : 'R'}
                      valueStyle={{
                        color: stats!.ev_r != null && stats!.ev_r > 0
                          ? SCHEME_UP_DOWN[colorScheme].up
                          : SCHEME_UP_DOWN[colorScheme].down,
                      }}
                    />
                  </Col>
                </Row>
                {stats!.veto_note && (
                  <Typography.Text type="secondary" style={{ fontSize: 12, marginTop: 8, display: 'block' }}>
                    {stats!.veto_note}
                  </Typography.Text>
                )}
              </Card>
            )}
          </>
        ) : (
          <Empty description="暂无复盘数据（AI 建议 24h 后自动复盘）" style={{ marginTop: 60 }} />
        )}
      </Spin>
    </div>
  )
}
