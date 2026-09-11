import { useState } from 'react'
import { Button, Modal, Space, Spin, Statistic, Tag, message } from 'antd'
import {
  ApiOutlined,
  CheckCircleOutlined,
  CloseCircleOutlined,
  RobotOutlined,
} from '@ant-design/icons'
import { Bubble, ThoughtChain } from '@ant-design/x'
import { bj } from '../utils/dayjs'
import { scanApi } from '../api/scan'
import { TRADE_TYPE_MAP } from '../constants/labels'
import type { AIAnalysis, StageTrace } from '../types'

function fmtPrice(v: number | null | undefined): string {
  if (v == null) return '-'
  if (v < 1) return v.toFixed(6)
  if (v < 100) return v.toFixed(4)
  return v.toFixed(2)
}

// 把 AI 文本按序号/换行拆成条目逐行展示：
// AI 输出的 "1. xxx 2. xxx" 可能挤在一行，序号前（空白 + 数字 + . 或 、）强制换行；
// (?!\d) 避免误伤小数（如盈亏比 1.5）
function analysisLines(text?: string | null): string[] {
  if (!text || !text.trim()) return []
  return text
    .replace(/\r\n?/g, '\n')
    .replace(/(\s)(\d{1,2})([.、])(?!\d)/g, '\n$2$3')
    .split('\n')
    .map((l) => l.trim())
    .filter(Boolean)
}

/** AI 推理逐条展示（序号换行，整齐排版） */
function NumberedText({ text }: { text?: string | null }) {
  const lines = analysisLines(text)
  if (lines.length === 0) return <span style={{ color: '#999' }}>-</span>
  return (
    <div style={{ lineHeight: 1.8, fontSize: 13 }}>
      {lines.map((l, i) => (
        <div key={i}>{l}</div>
      ))}
    </div>
  )
}

// 计算实际决策：综合 trade_decision、recommendation、direction 兜底判断
// - AI 明确 skip → skip
// - recommendation ≤ 30 → skip（分数太低不值得做）
// - direction 为空 且 trade_decision 也不是 suggest → skip（无明确方向）
// - 以上都不是 → suggest
export function getEffectiveDecision(
  ai?: AIAnalysis,
): { decision: string; reason: string } {
  if (!ai) return { decision: "", reason: "" };
  if (ai.trade_decision === "skip") {
    return { decision: "skip", reason: ai.skip_reason || "AI 不建议开单" };
  }
  if (ai.recommendation != null && ai.recommendation <= 30) {
    return {
      decision: "skip",
      reason: `推荐程度仅 ${ai.recommendation} 分，不值得开单${ai.skip_reason ? "；" + ai.skip_reason : ""}`,
    };
  }
  if (!ai.direction && ai.trade_decision !== "suggest") {
    return { decision: "skip", reason: "AI 未给出明确交易方向" };
  }
  return { decision: "suggest", reason: "" };
}

/** 价格小卡片：标签 + 大号价格，左侧色条区分用途 */
function PriceCard({
  label,
  value,
  color,
}: {
  label: string
  value: number | null | undefined
  color: string
}) {
  return (
    <div
      style={{
        flex: 1,
        minWidth: 0,
        padding: '8px 12px',
        borderRadius: 8,
        background: '#fafafa',
        borderLeft: `3px solid ${color}`,
      }}
    >
      <div style={{ fontSize: 12, color: '#999', marginBottom: 2 }}>{label}</div>
      <div style={{ fontSize: 16, fontWeight: 700, color, lineHeight: 1.3 }}>
        {fmtPrice(value)}
      </div>
    </div>
  )
}

/** Agent 运行轨迹弹窗：打开时拉取工具循环 trace，ThoughtChain 逐轮展示 */
function TraceModal({ open, onClose, ai }: { open: boolean; onClose: () => void; ai: AIAnalysis }) {
  const [loading, setLoading] = useState(false)
  const [trace, setTrace] = useState<StageTrace | null | undefined>(undefined)

  // 每次打开时拉取轨迹数据
  const fetchTrace = () => {
    setLoading(true)
    scanApi
      .aiTrace(ai.id)
      .then((data) => setTrace(data.stage_trace))
      .catch((e: any) => {
        setTrace(undefined)
        message.error(e?.response?.data?.detail || e?.message || '获取运行轨迹失败')
      })
      .finally(() => setLoading(false))
  }

  return (
    <Modal
      title="Agent 运行轨迹"
      open={open}
      onCancel={onClose}
      footer={null}
      width={640}
      afterOpenChange={(visible) => {
        // 打开动画结束后再拉数据，避免弹窗未展示时请求
        if (visible) fetchTrace()
      }}
    >
      {loading ? (
        <div style={{ textAlign: 'center', padding: '40px 0' }}>
          <Spin />
        </div>
      ) : trace === null ? (
        // 单次调用管线生成，无工具循环轨迹
        <div style={{ color: '#999', padding: '16px 0', textAlign: 'center' }}>
          该分析由单次调用管线生成，无工具循环轨迹
        </div>
      ) : trace ? (
        <>
          {/* 顶部统计：轮数 / 工具调用 / 总耗时 */}
          <Space size={32} style={{ marginBottom: 16 }}>
            <Statistic title="轮数" value={trace.rounds} valueStyle={{ fontSize: 20 }} />
            <Statistic title="工具调用" value={trace.tool_calls} valueStyle={{ fontSize: 20 }} />
            <Statistic
              title="总耗时"
              value={(trace.elapsed_ms / 1000).toFixed(1)}
              suffix="s"
              valueStyle={{ fontSize: 20 }}
            />
          </Space>
          {/* ThoughtChain 逐轮：LLM 耗时 + 工具标签 + 调用明细 */}
          <ThoughtChain
            items={trace.steps.map((step) => ({
              key: String(step.round),
              title: `Round ${step.round}`,
              description: `LLM ${(step.llm_ms / 1000).toFixed(1)}s`,
              extra:
                step.tools && step.tools.length > 0 ? (
                  <Space size={4} wrap>
                    {step.tools.map((t, i) => (
                      <Tag key={i} style={{ marginInlineEnd: 0, fontSize: 11 }}>
                        {t}
                      </Tag>
                    ))}
                  </Space>
                ) : (
                  <span style={{ color: '#bbb', fontSize: 12 }}>无工具调用</span>
                ),
              content:
                (step.calls || []).length === 0 ? undefined : (
                  <div>
                    {(step.calls || []).map((c, i) => (
                      <div
                        key={i}
                        style={{
                          color: '#888',
                          fontSize: 12,
                          lineHeight: 1.9,
                          fontFamily: 'monospace',
                        }}
                      >
                        {c.tool}({JSON.stringify(c.args)}) → {c.result_len}B · {c.ms}ms
                      </div>
                    ))}
                  </div>
                ),
            }))}
          />
        </>
      ) : null}
    </Modal>
  )
}

interface Props {
  ai: AIAnalysis
}

/** AI 分析结果卡片：决策横幅 + 价格卡 + 指标行 + AI 推理气泡（Ant Design X） */
export default function AiAnalysisCard({ ai }: Props) {
  const eff = getEffectiveDecision(ai)
  const isSkip = eff.decision === 'skip'
  // 运行轨迹弹窗开关（仅 Agent 生成的分析可查看）
  const [traceOpen, setTraceOpen] = useState(false)

  const tradeTypeCfg = ai.trade_type ? TRADE_TYPE_MAP[ai.trade_type] : undefined

  return (
    <>
      {/* 决策横幅：语义色渐变 + 方向/开单类型/推荐度 */}
      <div
        style={{
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'space-between',
          gap: 8,
          flexWrap: 'wrap',
          padding: '10px 14px',
          borderRadius: 8,
          marginBottom: 10,
          background: isSkip
            ? 'linear-gradient(90deg, rgba(255,77,79,0.10), rgba(255,77,79,0.02))'
            : 'linear-gradient(90deg, rgba(82,196,26,0.14), rgba(82,196,26,0.02))',
          border: `1px solid ${isSkip ? 'rgba(255,77,79,0.30)' : 'rgba(82,196,26,0.30)'}`,
        }}
      >
        <Space size={8} wrap>
          {isSkip ? (
            <CloseCircleOutlined style={{ color: '#ff4d4f', fontSize: 18 }} />
          ) : (
            <CheckCircleOutlined style={{ color: '#52c41a', fontSize: 18 }} />
          )}
          <strong style={{ fontSize: 15 }}>
            {isSkip ? '不建议开单' : '建议开单'}
          </strong>
          {!isSkip &&
            (ai.direction === 'long' ? (
              <Tag color="green" style={{ marginInlineEnd: 0 }}>
                做多 Long
              </Tag>
            ) : ai.direction === 'short' ? (
              <Tag color="red" style={{ marginInlineEnd: 0 }}>
                做空 Short
              </Tag>
            ) : null)}
          {!isSkip && tradeTypeCfg && (
            <Tag color={tradeTypeCfg.color} style={{ marginInlineEnd: 0 }}>
              {tradeTypeCfg.label}
            </Tag>
          )}
        </Space>
        {ai.recommendation != null && (
          <span style={{ fontWeight: 700, whiteSpace: 'nowrap' }}>
            <span style={{ fontSize: 12, color: '#999', marginRight: 6 }}>推荐度</span>
            <span
              style={{
                fontSize: 16,
                color:
                  ai.recommendation >= 80
                    ? '#ff4d4f'
                    : ai.recommendation >= 60
                      ? '#fa8c16'
                      : ai.recommendation >= 40
                        ? '#faad14'
                        : '#8c8c8c',
              }}
            >
              {ai.recommendation}分
            </span>
          </span>
        )}
      </div>

      {/* skip：理由 + AI 推理，均走推理气泡 */}
      {isSkip ? (
        <Bubble
          variant="shadow"
          avatar={{
            icon: <RobotOutlined />,
            style: { background: '#8c8c8c', color: '#fff' },
          }}
          content={eff.reason || ai.analysis || ''}
          messageRender={(content) => <NumberedText text={content as string} />}
          styles={{
            header: { fontSize: 12, color: '#999', paddingBottom: 0 },
            content: { maxWidth: '100%' },
          }}
          header="AI 跳过理由"
        />
      ) : (
        <>
          {/* 价格卡：入场 / 止损 / 止盈1 / 止盈2 */}
          <div style={{ display: 'flex', gap: 8, marginBottom: 10 }}>
            <PriceCard label="入场价" value={ai.entry_price} color="#1677ff" />
            <PriceCard label="止损价" value={ai.stop_loss} color="#ff4d4f" />
            <PriceCard label="止盈1" value={ai.take_profit_1} color="#52c41a" />
            <PriceCard label="止盈2" value={ai.take_profit_2} color="#52c41a" />
          </div>
          {/* 指标行：盈亏比 / 仓位 / 分析时间 / 运行轨迹 */}
          <div
            style={{
              display: 'flex',
              alignItems: 'center',
              gap: 20,
              flexWrap: 'wrap',
              padding: '0 4px',
              marginBottom: 10,
              fontSize: 13,
            }}
          >
            <span>
              <span style={{ color: '#999', marginRight: 6 }}>盈亏比</span>
              <strong>
                {ai.risk_reward_ratio != null ? ai.risk_reward_ratio.toFixed(2) : '-'}
              </strong>
            </span>
            <span>
              <span style={{ color: '#999', marginRight: 6 }}>仓位建议</span>
              <strong>{ai.position_pct != null ? `${ai.position_pct}%` : '-'}</strong>
            </span>
            <span>
              <span style={{ color: '#999', marginRight: 6 }}>分析时间</span>
              {bj(ai.created_at).format('MM-DD HH:mm:ss')}
            </span>
            {ai.scan_result_id && (
              <Button
                type="link"
                size="small"
                icon={<ApiOutlined />}
                style={{ padding: 0, height: 'auto' }}
                onClick={() => setTraceOpen(true)}
              >
                运行轨迹
              </Button>
            )}
          </div>
          {/* AI 推理气泡 */}
          <Bubble
            variant="shadow"
            avatar={{
              icon: <RobotOutlined />,
              style: { background: '#1677ff', color: '#fff' },
            }}
            content={ai.analysis || ''}
            messageRender={(content) => <NumberedText text={content as string} />}
            styles={{
              header: { fontSize: 12, color: '#999', paddingBottom: 0 },
              content: { maxWidth: '100%' },
            }}
            header="AI 推理"
          />
        </>
      )}

      <TraceModal open={traceOpen} onClose={() => setTraceOpen(false)} ai={ai} />
    </>
  )
}
