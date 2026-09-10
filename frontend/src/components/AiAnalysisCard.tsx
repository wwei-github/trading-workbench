import { Alert, Descriptions, Tag } from 'antd'
import { bj } from '../utils/dayjs'
import type { AIAnalysis } from '../types'

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
    </>
  );
}
