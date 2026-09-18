import { useCallback, useEffect, useState } from 'react'
import {
  Alert,
  Button,
  Card,
  Descriptions,
  Empty,
  Modal,
  Popconfirm,
  Space,
  Spin,
  Statistic,
  Table,
  Tag,
  Tooltip,
  Typography,
  message,
} from 'antd'
import {
  CheckCircleOutlined,
  ExclamationCircleOutlined,
  QuestionCircleOutlined,
  ReloadOutlined,
  RobotOutlined,
  RollbackOutlined,
  ThunderboltOutlined,
  WarningOutlined,
} from '@ant-design/icons'
import type { ColumnsType } from 'antd/es/table'
import { scanApi } from '../api/scan'
import type { Patch, PatchListResponse, PatchMetrics, PromptView } from '../types'

const { Text, Paragraph } = Typography

const STATUS_META: Record<string, { color: string; label: string }> = {
  draft: { color: 'default', label: '待确认' },
  active: { color: 'processing', label: '评估中' },
  valid: { color: 'success', label: '有效' },
  invalid: { color: 'warning', label: '无效' },
  rolled_back: { color: 'default', label: '已回滚' },
}

const VERDICT_META: Record<string, { color: string; label: string; icon: JSX.Element }> = {
  pending: { color: 'processing', label: '评估中', icon: <LoadingLabel /> },
  valid: { color: 'success', label: '有效', icon: <CheckCircleOutlined /> },
  invalid: { color: 'warning', label: '无效', icon: <ExclamationCircleOutlined /> },
  worse: { color: 'error', label: '恶化告警', icon: <WarningOutlined /> },
}

function LoadingLabel() {
  return <span>评估中</span>
}

// 目标热度/EV/胜率 「前→后」渲染：改善绿 / 恶化红 / 无变化灰
function DeltaText({ before, after, fmt, lowerIsBetter }: {
  before?: number; after?: number; fmt: (v: number) => string; lowerIsBetter?: boolean
}) {
  if (before == null || after == null) return <Text type="secondary">-</Text>
  const diff = after - before
  const improved = lowerIsBetter ? diff < 0 : diff > 0
  const worse = lowerIsBetter ? diff > 0 : diff < 0
  const arrow = diff === 0 ? '' : diff > 0 ? ' ↑' : ' ↓'
  const color = improved ? '#52c41a' : worse ? '#ff4d4f' : undefined
  return (
    <span style={{ color: color || undefined }}>
      {fmt(before)} → {fmt(after)}
      {arrow}
    </span>
  )
}

const pct = (v: number) => `${(v * 100).toFixed(0)}%`
const num = (v: number) => v.toFixed(2)

/**
 * 规则补丁（docs/12 §10）：AI 复盘产出的规则补丁全生命周期管理——
 * 草稿确认合入 → 三层指标对照评估（主热度/护栏/通过率）→ 一键回滚。
 * 全部操作走 DB 即时生效，无需重建容器。
 */
export default function ReviewPatchesPanel() {
  const [data, setData] = useState<PatchListResponse | null>(null)
  const [loading, setLoading] = useState(false)
  const [triggering, setTriggering] = useState(false)
  const [detail, setDetail] = useState<Patch | null>(null)
  const [promptView, setPromptView] = useState<PromptView | null>(null)
  const [promptOpen, setPromptOpen] = useState(false)

  const fetchData = useCallback(async () => {
    setLoading(true)
    try {
      setData(await scanApi.patches())
    } catch (e: any) {
      message.error(e?.response?.data?.detail || '获取补丁列表失败')
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    fetchData()
  }, [fetchData])

  const openPromptView = async () => {
    try {
      setPromptView(await scanApi.promptView())
      setPromptOpen(true)
    } catch (e: any) {
      message.error(e?.response?.data?.detail || '获取提示词视图失败')
    }
  }

  const doApply = async (p: Patch) => {
    try {
      await scanApi.applyPatch(p.id)
      message.success(`补丁「${p.target_tag}」已合入，下轮分析/开仓即生效`)
      setDetail(null)
      fetchData()
    } catch (e: any) {
      message.error(e?.response?.data?.detail || '合入失败')
    }
  }

  const doRollback = async (p: Patch) => {
    try {
      await scanApi.rollbackPatch(p.id)
      message.success(`补丁「${p.target_tag}」已回滚，即时生效`)
      setDetail(null)
      fetchData()
    } catch (e: any) {
      message.error(e?.response?.data?.detail || '回滚失败')
    }
  }

  const doDelete = async (p: Patch) => {
    try {
      await scanApi.deletePatch(p.id)
      message.success('草稿已删除')
      fetchData()
    } catch (e: any) {
      message.error(e?.response?.data?.detail || '删除失败')
    }
  }

  const doTrigger = async () => {
    setTriggering(true)
    try {
      const r = await scanApi.triggerPatches()
      message.success(r.created ? `生成 ${r.created} 条补丁草稿` : '本次无标签达到触发阈值（或均处冷却期）')
      fetchData()
    } catch (e: any) {
      message.error(e?.response?.data?.detail || '触发失败')
    } finally {
      setTriggering(false)
    }
  }

  const columns: ColumnsType<Patch> = [
    {
      title: '状态',
      width: 110,
      render: (_, p) => {
        const badge = p.verdict === 'worse'
          ? VERDICT_META.worse
          : VERDICT_META[p.verdict] ?? VERDICT_META.pending
        const st = STATUS_META[p.status]
        return (
          <Space size={4}>
            <Tag color={p.verdict === 'worse' ? 'error' : st?.color} icon={p.verdict === 'worse' ? badge.icon : undefined}>
              {p.verdict === 'worse' ? '恶化告警' : st?.label ?? p.status}
            </Tag>
          </Space>
        )
      },
    },
    {
      title: '来源',
      width: 90,
      render: (_, p) => (p.origin === 'ai' ? <Tag icon={<RobotOutlined />}>AI复盘</Tag> : <Tag>✋手动</Tag>),
    },
    { title: '目标标签', dataIndex: 'target_tag', width: 110 },
    {
      title: '落点',
      width: 130,
      render: (_, p) =>
        p.kind === 'param'
          ? <Tag color="purple">参数·{p.param_key}</Tag>
          : <Tag color="geekblue">提示词覆盖段</Tag>,
    },
    {
      title: '合入时间',
      dataIndex: 'applied_at',
      width: 150,
      render: (v: string | null) => (v ? new Date(v).toLocaleString('zh-CN', { hour12: false }) : '-'),
    },
    {
      title: <span>目标热度<Tooltip title="近7天该标签笔数÷已归因笔数。合入前→实时，下降 ≥50% 判有效"><QuestionCircleOutlined /></Tooltip></span>,
      width: 170,
      render: (_, p) => {
        const b = p.metrics_before_json?.heat?.[p.target_tag]
        const a = p.status === 'draft' ? undefined
          : (p.metrics_after_json?.after?.heat?.[p.target_tag] ?? data?.stats?.heat?.[p.target_tag])
        return <DeltaText before={b} after={a} fmt={pct} lowerIsBetter />
      },
    },
    {
      title: <span>EV/笔<Tooltip title="近7天每笔平均净盈亏 USDT，整体护栏"><QuestionCircleOutlined /></Tooltip></span>,
      width: 150,
      render: (_, p) => (
        <DeltaText
          before={p.metrics_before_json?.ev_per_trade}
          after={p.status === 'draft' ? undefined
            : (p.metrics_after_json?.after?.ev_per_trade ?? data?.stats?.ev_per_trade)}
          fmt={num}
        />
      ),
    },
    {
      title: <span>胜率<Tooltip title="近7天已平仓交易胜率，整体护栏"><QuestionCircleOutlined /></Tooltip></span>,
      width: 140,
      render: (_, p) => (
        <DeltaText
          before={p.metrics_before_json?.win_rate}
          after={p.status === 'draft' ? undefined
            : (p.metrics_after_json?.after?.win_rate ?? data?.stats?.win_rate)}
          fmt={pct}
        />
      ),
    },
    {
      title: '操作',
      width: 170,
      render: (_, p) => (
        <Space size={4}>
          <Button size="small" type="link" onClick={() => setDetail(p)}>对照</Button>
          {p.status === 'draft' && (
            <>
              <Popconfirm title="确认合入该补丁？" onConfirm={() => doApply(p)}>
                <Button size="small" type="link">合入</Button>
              </Popconfirm>
              <Popconfirm title="删除该草稿？" onConfirm={() => doDelete(p)}>
                <Button size="small" type="link" danger>删除</Button>
              </Popconfirm>
            </>
          )}
          {p.status !== 'draft' && p.status !== 'rolled_back' && (
            <Popconfirm title="确认回滚？恢复合入前状态，即时生效" onConfirm={() => doRollback(p)}>
              <Button size="small" type="link" icon={<RollbackOutlined />}>回滚</Button>
            </Popconfirm>
          )}
        </Space>
      ),
    },
  ]

  const stats: PatchMetrics | undefined = data?.stats
  const alerts = (data?.items ?? []).filter((p) => p.verdict === 'worse')

  return (
    <div>
      {/* 恶化告警置顶条 */}
      {alerts.length > 0 && (
        <Alert
          type="error"
          showIcon
          style={{ marginBottom: 12 }}
          message={`${alerts.length} 条补丁评估恶化，建议回滚`}
          description={alerts.map((p) => (
            <div key={p.id}>
              「{p.target_tag}」{(p.metrics_after_json?.checks ?? []).join('；')}
            </div>
          ))}
          action={<Button size="small" danger onClick={() => setDetail(alerts[0])}>查看对照</Button>}
        />
      )}

      <Card
        size="small"
        title={
          <Space>
            规则补丁
            <Tooltip title="AI 错误归因发现高频错误标签（近7天热度≥0.5 且样本≥8）时生成补丁草稿；人工确认合入后进入评估期（第7天首评、14天终评）。系统自带提示词永不改动，提示词补丁以覆盖规则段追加生效。">
              <QuestionCircleOutlined />
            </Tooltip>
          </Space>
        }
        extra={
          <Space>
            <Button size="small" onClick={openPromptView}>查看当前生效提示词</Button>
            <Button size="small" icon={<ThunderboltOutlined />} loading={triggering} onClick={doTrigger}>
              手动触发生成
            </Button>
            <Button size="small" icon={<ReloadOutlined />} onClick={fetchData} />
          </Space>
        }
      >
        {/* 当前实时窗口统计 */}
        {stats && (
          <Descriptions size="small" column={4} style={{ marginBottom: 12 }}>
            <Descriptions.Item label="近7天已归因">{stats.attributed_total ?? 0} 笔</Descriptions.Item>
            <Descriptions.Item label="EV/笔">{num(stats.ev_per_trade ?? 0)} U</Descriptions.Item>
            <Descriptions.Item label="胜率">{pct(stats.win_rate ?? 0)}</Descriptions.Item>
            <Descriptions.Item label="建议通过率">{pct(stats.pass_rate ?? 0)}</Descriptions.Item>
          </Descriptions>
        )}

        <Table<Patch>
          rowKey="id"
          size="small"
          loading={loading}
          columns={columns}
          dataSource={data?.items ?? []}
          scroll={{ y: 400 }}
          pagination={false}
          locale={{ emptyText: <Empty description="暂无补丁（等待归因数据积累或手动触发生成）" /> }}
        />
      </Card>

      {/* 对照弹窗：三层指标明细 */}
      <Modal
        open={!!detail}
        title={detail ? `补丁对照：${detail.target_tag}` : ''}
        onCancel={() => setDetail(null)}
        footer={
          detail && detail.status !== 'draft' && detail.status !== 'rolled_back'
            ? [
                <Popconfirm key="rb" title="确认回滚？恢复合入前状态，即时生效" onConfirm={() => doRollback(detail)}>
                  <Button danger icon={<RollbackOutlined />}>回滚补丁</Button>
                </Popconfirm>,
                <Button key="close" onClick={() => setDetail(null)}>关闭</Button>,
              ]
            : [<Button key="close" onClick={() => setDetail(null)}>关闭</Button>]
        }
        width={720}
      >
        {detail && (
          <>
            <Descriptions size="small" column={2} style={{ marginBottom: 8 }}>
              <Descriptions.Item label="状态">
                {STATUS_META[detail.status]?.label ?? detail.status}
                {detail.verdict === 'worse' && <Tag color="error" icon={<WarningOutlined />}>恶化告警</Tag>}
                {detail.verdict === 'valid' && <Tag color="success">有效</Tag>}
              </Descriptions.Item>
              <Descriptions.Item label="评估进度">
                {detail.status === 'draft'
                  ? '未合入'
                  : detail.metrics_after_json?.evaluated_days != null
                    ? `已评估 ${detail.metrics_after_json.evaluated_days} 天（7天首评/14天终评）`
                    : '合入未满 7 天'}
              </Descriptions.Item>
            </Descriptions>

            <Card size="small" title="补丁内容" style={{ marginBottom: 8 }}>
              {detail.kind === 'prompt' ? (
                <>
                  {detail.old_text && (
                    <Paragraph type="secondary" style={{ marginBottom: 4 }}>
                      <Text strong>系统原文段（引用，不改动）：</Text>
                      <blockquote style={{ borderLeft: '3px solid #ddd', paddingLeft: 8 }}>
                        {detail.old_text}
                      </blockquote>
                    </Paragraph>
                  )}
                  <Paragraph style={{ marginBottom: 0 }}>
                    <Text strong>覆盖规则（追加生效）：</Text>
                    <blockquote style={{ borderLeft: '3px solid #1677ff', paddingLeft: 8 }}>
                      {detail.new_text}
                    </blockquote>
                  </Paragraph>
                </>
              ) : (
                <Paragraph style={{ marginBottom: 0 }}>
                  <Text strong>参数 {detail.param_key}：</Text>
                  建议值 {detail.param_value}（比例口径）。
                  {detail.new_text}
                </Paragraph>
              )}
            </Card>

            <Card size="small" title="三层指标对照（合入前 → 实时/评估后）">
              {detail.status === 'draft' ? (
                <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="合入后开始评估" />
              ) : (
                <>
                  {(detail.metrics_after_json?.checks ?? []).map((c, i) => (
                    <div key={i} style={{ marginBottom: 2 }}>• {c}</div>
                  ))}
                  <div style={{ marginTop: 6 }}>
                    <Text type="secondary">
                      建议（通过率）：{pct(detail.metrics_after_json?.after?.pass_rate ?? data?.stats?.pass_rate ?? 0)}，
                      护栏三告警线 30%
                    </Text>
                  </div>
                </>
              )}
            </Card>
          </>
        )}
      </Modal>

      {/* 当前生效提示词分层视图 */}
      <Modal
        open={promptOpen}
        title="当前生效提示词（分层视图，只读）"
        onCancel={() => setPromptOpen(false)}
        footer={<Button onClick={() => setPromptOpen(false)}>关闭</Button>}
        width={860}
      >
        {promptView && (
          <>
            <Card size="small" title="① 系统自带（代码常量，永不改动）" style={{ marginBottom: 8 }}>
              <Paragraph style={{ whiteSpace: 'pre-wrap', maxHeight: 220, overflow: 'auto', marginBottom: 0 }}>
                {promptView.system_prompt}
              </Paragraph>
            </Card>
            <Card size="small" title="② 用户自定义（strategy_prompt 手编区）" style={{ marginBottom: 8 }}>
              {promptView.user_custom
                ? <Paragraph style={{ whiteSpace: 'pre-wrap', marginBottom: 0 }}>{promptView.user_custom}</Paragraph>
                : <Text type="secondary">（空）</Text>}
            </Card>
            <Card
              size="small"
              title="③ 补丁覆盖段（AI 复盘生成 / 参数）"
              extra={
                <Text type="secondary">
                  max_stop_pct={promptView.params.max_stop_pct ?? '未设'}，
                  liq_gate_buffer={promptView.params.liq_gate_buffer ?? '默认'}
                </Text>
              }
            >
              {promptView.patch_segments.length === 0 && <Text type="secondary">（无生效补丁）</Text>}
              {promptView.patch_segments.map((s) => (
                <div key={s.marker} style={{ marginBottom: 8 }}>
                  <Space size={6}>
                    <Tag icon={<RobotOutlined />} color="geekblue">AI复盘</Tag>
                    <Tag>{s.tag}</Tag>
                    <Tag color={STATUS_META[s.status === 'orphan' ? 'rolled_back' : s.status]?.color}>
                      {STATUS_META[s.status === 'orphan' ? 'rolled_back' : s.status]?.label ?? s.status}
                    </Tag>
                    {s.applied_at && (
                      <Text type="secondary" style={{ fontSize: 12 }}>
                        合入 {new Date(s.applied_at).toLocaleDateString('zh-CN')}
                      </Text>
                    )}
                  </Space>
                  <Paragraph style={{ whiteSpace: 'pre-wrap', marginBottom: 0, marginTop: 4 }}>
                    {s.text}
                  </Paragraph>
                </div>
              ))}
            </Card>
          </>
        )}
      </Modal>
    </div>
  )
}
