import { useState } from 'react'
import {
  Alert,
  Button,
  Collapse,
  Descriptions,
  Modal,
  Space,
  Spin,
  Statistic,
  Tag,
  message,
} from 'antd'
import { ApiOutlined } from '@ant-design/icons'
import { bj } from '../utils/dayjs'
import { scanApi } from '../api/scan'
import type { AIAnalysis, StageTrace } from '../types'

// AI 交易细节表：标签列统一宽度，保证左右两栏对齐
const descCell = {
  labelStyle: { width: "18%" },
  contentStyle: { width: "32%" },
};

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

interface Props {
  ai: AIAnalysis
}

/** Agent 运行轨迹弹窗：打开时拉取工具循环 trace，逐轮展示 LLM 与工具调用明细 */
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
        <Alert type="info" message="该分析由单次调用管线生成，无工具循环轨迹" />
      ) : trace ? (
        <>
          {/* 顶部统计：轮数 / 工具调用 / 总耗时 */}
          <Space size={32} style={{ marginBottom: 12 }}>
            <Statistic title="轮数" value={trace.rounds} valueStyle={{ fontSize: 20 }} />
            <Statistic title="工具调用" value={trace.tool_calls} valueStyle={{ fontSize: 20 }} />
            <Statistic
              title="总耗时"
              value={(trace.elapsed_ms / 1000).toFixed(1)}
              suffix="s"
              valueStyle={{ fontSize: 20 }}
            />
          </Space>
          {/* 逐轮展开：LLM 耗时 + 工具列表 + 调用明细 */}
          <Collapse
            size="small"
            defaultActiveKey={trace.steps.map((s) => String(s.round))}
            items={trace.steps.map((step) => ({
              key: String(step.round),
              label: `Round ${step.round} · LLM ${step.llm_ms}ms · ${step.tools.join(', ') || '无工具调用'}`,
              children: (
                <div>
                  {(step.calls || []).length === 0 ? (
                    <span style={{ color: '#999', fontSize: 12 }}>本轮无工具调用明细</span>
                  ) : (
                    (step.calls || []).map((c, i) => (
                      <div
                        key={i}
                        style={{ color: '#888', fontSize: 12, lineHeight: 1.9, fontFamily: 'monospace' }}
                      >
                        {c.tool}({JSON.stringify(c.args)}) → {c.result_len}B · {c.ms}ms
                      </div>
                    ))
                  )}
                </div>
              ),
            }))}
          />
        </>
      ) : null}
    </Modal>
  )
}

/** AI 推理逐条展示（序号换行，整齐排版） */
function NumberedText({ text }: { text?: string | null }) {
  const lines = analysisLines(text)
  if (lines.length === 0) return <span style={{ color: '#999' }}>-</span>
  return (
    <div style={{ lineHeight: 1.7 }}>
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

interface Props {
  ai: AIAnalysis
}

/** AI 分析结果卡片：决策标签 + 交易细节 + AI 推理（扫描结果 / 搜索币种共用） */
export default function AiAnalysisCard({ ai }: Props) {
  const eff = getEffectiveDecision(ai);
  const isSkip = eff.decision === "skip";
  // 运行轨迹弹窗开关（仅 Agent 生成的分析可查看）
  const [traceOpen, setTraceOpen] = useState(false);

  return (
    <>
      {/* AI 决策标签 */}
      {isSkip ? (
        <Alert
          type="error"
          showIcon
          message={
            <strong style={{ fontSize: 14 }}>
              ❌ 不建议开单
            </strong>
          }
          description={<NumberedText text={eff.reason} />}
          style={{ marginBottom: 8 }}
        />
      ) : (
        <Alert
          type="success"
          showIcon
          message={
            <strong style={{ fontSize: 14 }}>
              ✅ 建议开单
            </strong>
          }
          description={ai.skip_reason || ""}
          style={{ marginBottom: 8 }}
        />
      )}

      {/* 交易细节：仅 suggest 时展示 */}
      {!isSkip && (
        <Descriptions bordered size="small" column={2}>
          <Descriptions.Item label="方向" {...descCell}>
            {ai.direction === "long" ? (
              <Tag color="green">做多 (Long)</Tag>
            ) : ai.direction === "short" ? (
              <Tag color="red">做空 (Short)</Tag>
            ) : (
              <span style={{ color: "#999" }}>-</span>
            )}
          </Descriptions.Item>
          <Descriptions.Item label="推荐程度" {...descCell}>
            {ai.recommendation != null ? (
              <span
                style={{
                  fontWeight: 700,
                  color:
                    ai.recommendation >= 80
                      ? "#ff4d4f"
                      : ai.recommendation >= 60
                        ? "#fa8c16"
                        : ai.recommendation >= 40
                          ? "#faad14"
                          : "#8c8c8c",
                }}>
                {ai.recommendation}分
              </span>
            ) : (
              <span style={{ color: "#999" }}>-</span>
            )}
          </Descriptions.Item>
          <Descriptions.Item label="盈亏比" {...descCell}>
            {ai.risk_reward_ratio != null
              ? `${ai.risk_reward_ratio.toFixed(2)}`
              : "-"}
          </Descriptions.Item>
          <Descriptions.Item label="入场价" {...descCell}>
            {fmtPrice(ai.entry_price)}
          </Descriptions.Item>
          <Descriptions.Item label="仓位建议" {...descCell}>
            {ai.position_pct != null
              ? `${ai.position_pct}%`
              : "-"}
          </Descriptions.Item>
          <Descriptions.Item label="止损价" {...descCell}>
            <span style={{ color: "#ff4d4f" }}>
              {fmtPrice(ai.stop_loss)}
            </span>
          </Descriptions.Item>
          <Descriptions.Item label="止盈1" {...descCell}>
            <span style={{ color: "#52c41a" }}>
              {fmtPrice(ai.take_profit_1)}
            </span>
          </Descriptions.Item>
          <Descriptions.Item label="止盈2" span={2}>
            <span style={{ color: "#52c41a" }}>
              {fmtPrice(ai.take_profit_2)}
            </span>
          </Descriptions.Item>
          <Descriptions.Item label="分析时间" span={2}>
            {bj(ai.created_at).format("YYYY-MM-DD HH:mm:ss")}
          </Descriptions.Item>
          <Descriptions.Item label="AI 推理" span={2}>
            <NumberedText text={ai.analysis} />
          </Descriptions.Item>
        </Descriptions>
      )}

      {/* skip 时只显示 AI 推理 */}
      {isSkip && ai.analysis && (
        <Descriptions bordered size="small" column={1}>
          <Descriptions.Item label="AI 推理">
            <NumberedText text={ai.analysis} />
          </Descriptions.Item>
        </Descriptions>
      )}

      {/* 运行轨迹入口：仅 AI Agent 生成的分析（scan_result_id 非空）展示；手动搜索结果不显示 */}
      {ai.scan_result_id && (
        <>
          <Button
            type="link"
            size="small"
            icon={<ApiOutlined />}
            style={{ marginTop: 4, paddingLeft: 0 }}
            onClick={() => setTraceOpen(true)}
          >
            运行轨迹
          </Button>
          <TraceModal open={traceOpen} onClose={() => setTraceOpen(false)} ai={ai} />
        </>
      )}
    </>
  );
}
