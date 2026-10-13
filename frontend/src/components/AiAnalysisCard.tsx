import { useState, type CSSProperties, type ReactNode } from 'react'
import { Button, Divider, Modal, Space, Spin, Statistic, Tag, Tooltip, Typography, message } from 'antd'
import {
  ApiOutlined,
  CheckCircleOutlined,
  CloseCircleOutlined,
  RobotOutlined,
  SafetyCertificateOutlined,
} from '@ant-design/icons'
import { Bubble, ThoughtChain } from '@ant-design/x'
import { bj } from '../utils/dayjs'
import { useScanStore } from '../stores/scanStore'
import type { ColorScheme } from '../stores/scanStore'
import { SCHEME_UP_DOWN, hexToRgba, schemeTag } from '../utils/scheme'
import { scanApi } from '../api/scan'
import { TRADE_TYPE_MAP } from '../constants/labels'
import type { AIAnalysis, StageTrace } from '../types'

const { Text } = Typography

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

/** 条目结构化：序号（1./2、）+ 前置方括号标签（【波动极端】）+ 冒号短标题（4h方向：） */
interface AnalysisItem {
  num?: string
  tag?: string
  label?: string
  text: string
}

function parseItem(raw: string): AnalysisItem {
  let s = raw.trim()
  let num: string | undefined
  let tag: string | undefined
  // 序号与方括号标签先后不定（"[波动极端]1. …" / "1. 【波动极端】…"），两轮解析
  for (let pass = 0; pass < 2; pass++) {
    if (!num) {
      // (?!\d) 防误吃小数开头（如 "9.2%处…"）
      const m = s.match(/^([一二三四五六七八九十\d]{1,2})[.、．](?!\d)\s*(.+)$/)
      if (m) {
        num = m[1]
        s = m[2]
        continue
      }
    }
    if (!tag) {
      const m = s.match(/^[【\[]([^】\]]{1,12})[】\]]\s*(.+)$/)
      if (m) {
        tag = m[1]
        s = m[2]
        continue
      }
    }
    break
  }
  // 短标题只认首个冒号前 ≤12 字且不含标点空格（"4h方向：…"/"指标多数决：…"），长句不误拆
  let label: string | undefined
  const mL = s.match(/^([^：:，。；、,.()（）\[\]【】\s]{1,12})[：:]\s*(.+)$/)
  if (mL) {
    label = mL[1]
    s = mL[2]
  }
  return { num, tag, label, text: s }
}

/** AI 推理逐条展示：序号圆徽 + 标签/短标题强调 + 正文，条目间虚线分隔；
 * highlightLabels 命中的条行（如决策结论文本节的「结论：」行）以 accent 色软底强调 */
function AnalysisItemList({
  text,
  accent,
  highlightLabels,
}: {
  text?: string | null
  accent?: string
  highlightLabels?: string[]
}) {
  const rows = analysisLines(text).map(parseItem)
  if (rows.length === 0) return <span style={{ color: '#999' }}>-</span>
  const renderItem = (it: AnalysisItem, i: number) => {
    // 命中 highlightLabels 的条行（如「结论：不建议开单」）：accent 软底 + 整行加粗
    const hit = it.label != null && (highlightLabels?.includes(it.label) ?? false)
    return (
      <div
        key={i}
        style={{
          display: 'flex',
          gap: 8,
          padding: hit ? '4px 8px' : '4px 0',
          borderRadius: hit ? 6 : undefined,
          background: hit ? hexToRgba(accent ?? '#8c8c8c', 0.08) : undefined,
          borderTop: i > 0 ? '1px dashed #f0f0f0' : undefined,
        }}
      >
        {it.num != null && (
          <span
            style={{
              flex: '0 0 auto',
              width: 18,
              height: 18,
              marginTop: 3,
              borderRadius: 9,
              background: '#e6f4ff',
              color: '#1677ff',
              fontSize: 11,
              fontWeight: 700,
              display: 'inline-flex',
              alignItems: 'center',
              justifyContent: 'center',
            }}
          >
            {it.num}
          </span>
        )}
        <div
          style={{
            flex: 1,
            minWidth: 0,
            lineHeight: 1.8,
            color: '#434343',
            fontWeight: hit ? 600 : undefined,
          }}
        >
          {it.tag && (
            <Tag
              color="orange"
              bordered={false}
              style={{ marginInlineEnd: 6, fontSize: 11, lineHeight: '16px', paddingInline: 5 }}
            >
              {it.tag}
            </Tag>
          )}
          {it.label && (
            <span style={{ fontWeight: 600, color: '#262626', marginRight: 6 }}>{it.label}</span>
          )}
          {it.text}
        </div>
      </div>
    )
  }
  return <div style={{ fontSize: 13 }}>{rows.map(renderItem)}</div>
}

// analysis 固定节标题（2026-10-12 前后端契约，docs/03 §3.1）：新格式两节
// 「公共检查：」「决策结论：」（skip 同样两节）；「合并结论：」保留解析兼容旧行。
// 旧行「策略一：/策略二：」标题行不再识别，落入正文平铺，不崩溃；
// 解析到 <2 个节标题（旧行/格式漂移）返回 null，回退平铺气泡
interface AnalysisSections {
  common: string[]
  decision: string[]
  merged: string[]
}

function parseAnalysisSections(text?: string | null): AnalysisSections | null {
  if (!text || !text.trim()) return null
  // 容错序号前缀变体（"一、公共检查：" / "1.决策结论："），节标题逐字匹配
  const re = /^\s*(?:[一二三四五六七八九十\d]{1,2}[、.．]?\s*)?(公共检查|决策结论|合并结论)\s*[:：]\s*(.*)$/
  const sections: AnalysisSections = { common: [], decision: [], merged: [] }
  const keyMap: Record<string, keyof AnalysisSections> = {
    公共检查: 'common',
    决策结论: 'decision',
    合并结论: 'merged',
  }
  let cur: keyof AnalysisSections | null = null
  let found = 0
  for (const raw of text.replace(/\r\n?/g, '\n').split('\n')) {
    const m = raw.match(re)
    if (m) {
      cur = keyMap[m[1]]
      found++
      if (m[2].trim()) sections[cur].push(m[2].trim())
      continue
    }
    if (cur && raw.trim()) sections[cur].push(raw.trim())
  }
  return found >= 2 ? sections : null
}

/** 策略结论徽标：suggest=涨色 / skip=跌色（随系统红绿方案翻转）/ null=灰（旧行不可用） */
function DecisionBadge({
  decision,
  scheme,
}: {
  decision: 'suggest' | 'skip' | null
  scheme: ColorScheme
}) {
  if (decision === 'suggest') {
    return (
      <Tag color={schemeTag(scheme).up} style={{ marginInlineEnd: 0 }}>
        <CheckCircleOutlined /> 建议开单
      </Tag>
    )
  }
  if (decision === 'skip') {
    return (
      <Tag color={schemeTag(scheme).down} style={{ marginInlineEnd: 0 }}>
        <CloseCircleOutlined /> 不建议
      </Tag>
    )
  }
  return <Tag style={{ marginInlineEnd: 0, color: '#999' }}>—</Tag>
}

/** 策略卡配色随结论徽标联动（涨/跌色随系统红绿方案同源翻转），左侧色条强调结论属性 */
function cardStyle(decision: 'suggest' | 'skip' | null, scheme: ColorScheme): CSSProperties {
  if (decision === 'suggest') {
    return {
      border: `1px solid ${hexToRgba(SCHEME_UP_DOWN[scheme].up, 0.35)}`,
      borderLeft: `3px solid ${SCHEME_UP_DOWN[scheme].up}`,
      background: hexToRgba(SCHEME_UP_DOWN[scheme].up, 0.05),
    }
  }
  if (decision === 'skip') {
    return {
      border: `1px solid ${hexToRgba(SCHEME_UP_DOWN[scheme].down, 0.3)}`,
      borderLeft: `3px solid ${SCHEME_UP_DOWN[scheme].down}`,
      background: hexToRgba(SCHEME_UP_DOWN[scheme].down, 0.04),
    }
  }
  return { border: '1px solid #f0f0f0', borderLeft: '3px solid #d9d9d9', background: '#fafafa' }
}

/** 分区小节标题：图标 + 灰色小标题（「AI 跳过理由」「公共检查」共用样式） */
function SectionHead({ icon, children }: { icon: ReactNode; children: ReactNode }) {
  return (
    <div style={{ display: 'flex', alignItems: 'center', gap: 6, fontSize: 12, fontWeight: 600, color: '#8c8c8c', marginBottom: 4 }}>
      {icon}
      {children}
    </div>
  )
}

/** 分析单卡（2026-10-13 合并）：AI 跳过理由 / 公共检查 / 决策结论并入同一张卡，
 * 各节之间以横线（Divider）区分；旧行无「决策结论」节（四节格式遗留）时回退
 * 「合并结论」节文本。卡片配色随决策（suggest=涨色 / skip=跌色 / null=灰） */
function AnalysisSectionsView({
  ai,
  sections,
  skipReason,
}: {
  ai: AIAnalysis
  sections: AnalysisSections
  /** skip 分支的「AI 跳过理由」正文（有则作为首节渲染） */
  skipReason?: string
}) {
  // 系统红绿方案（页面顶部全局切换，localStorage 持久化）
  const colorScheme = useScanStore((s) => s.colorScheme)
  // 决策结论徽标取结构化字段（trade_decision 不依赖文本解析，文本节缺失不误标）
  const decision: 'suggest' | 'skip' | null =
    ai.trade_decision === 'suggest' ? 'suggest' : ai.trade_decision === 'skip' ? 'skip' : null
  const decisionLines = sections.decision.length > 0 ? sections.decision : sections.merged
  // 决策结论配色：suggest=涨色 / skip=跌色 / null=灰（结论条与卡左色条同源）
  const decisionColor =
    decision === 'suggest'
      ? SCHEME_UP_DOWN[colorScheme].up
      : decision === 'skip'
        ? SCHEME_UP_DOWN[colorScheme].down
        : '#8c8c8c'
  const hasSkip = !!(skipReason && skipReason.trim())
  const hasCommon = sections.common.length > 0
  const hasDecision = decisionLines.length > 0
  if (!hasSkip && !hasCommon && !hasDecision) return null
  return (
    <div style={{ padding: '8px 10px', borderRadius: 8, ...cardStyle(decision, colorScheme) }}>
      {/* AI 跳过理由（skip 分支）：原独立气泡卡并入本卡首节 */}
      {hasSkip && (
        <>
          <SectionHead icon={<RobotOutlined />}>AI 跳过理由</SectionHead>
          <AnalysisItemList text={skipReason} />
        </>
      )}
      {hasSkip && (hasCommon || hasDecision) && <Divider style={{ margin: '8px 0' }} />}
      {/* 公共检查区：4h方向/指标多数决/回踩触点/形态量能/风险点逐项判定（单列） */}
      {hasCommon && (
        <>
          <SectionHead icon={<SafetyCertificateOutlined />}>公共检查</SectionHead>
          <AnalysisItemList text={sections.common.join('\n')} />
        </>
      )}
      {hasCommon && hasDecision && <Divider style={{ margin: '8px 0' }} />}
      {/* 决策结论：结论徽标取结构化字段，明细取 analysis 文本节；逐条单列展示，
          「结论：…」条行以涨跌色软底强调（accent 随系统红绿方案） */}
      {hasDecision && (
        <>
          <div style={{ display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap', marginBottom: 4 }}>
            <strong style={{ fontSize: 13 }}>决策结论</strong>
            <DecisionBadge decision={decision} scheme={colorScheme} />
          </div>
          <AnalysisItemList
            text={decisionLines.join('\n')}
            accent={decisionColor}
            highlightLabels={['结论']}
          />
        </>
      )}
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

/** 价格小卡片：标签 + 大号价格，左侧色条区分用途；sub 为可选副行
 *（如止损卡合并展示程序参考价：箭头指向程序价相对 AI 价的数值方向） */
function PriceCard({
  label,
  value,
  color,
  zeroText,
  sub,
}: {
  label: string
  value: number | null | undefined
  color: string
  /** 值为 0 时的占位文案（如止盈2 关闭时显示「未启用」），不传走原数字格式化 */
  zeroText?: string
  /** 副行：箭头符号 + 参考价文本 + 箭头/文本配色（不传不渲染） */
  sub?: { arrow: string; text: string; color?: string }
}) {
  return (
    <div
      style={{
        flex: 1,
        minWidth: 0,
        padding: '6px 10px',
        borderRadius: 8,
        background: '#fafafa',
        borderLeft: `3px solid ${color}`,
      }}
    >
      <div style={{ fontSize: 12, color: '#999', marginBottom: 1 }}>{label}</div>
      <div style={{ fontSize: 15, fontWeight: 700, color, lineHeight: 1.3 }}>
        {value === 0 && zeroText ? zeroText : fmtPrice(value)}
      </div>
      {sub && (
        <div style={{ fontSize: 12, lineHeight: 1.4, marginTop: 1 }}>
          <span style={{ color: sub.color ?? '#999', fontWeight: 700, marginRight: 4 }}>
            {sub.arrow}
          </span>
          <span style={{ color: sub.color ?? '#999' }}>{sub.text}</span>
        </div>
      )}
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
  // 全局涨跌配色方案：多空标签、止损/止盈价卡颜色随方案翻转
  const colorScheme = useScanStore((s) => s.colorScheme)
  const eff = getEffectiveDecision(ai)
  const isSkip = eff.decision === 'skip'
  // 决策横幅配色：建议=涨色 / 不建议=跌色（随系统红绿方案翻转，与结论徽标/结论卡同源）
  const bannerColor = SCHEME_UP_DOWN[colorScheme][isSkip ? 'down' : 'up']
  // analysis 两节解析（公共检查/决策结论；旧行「合并结论」兼容）：格式漂移返回 null 回退平铺
  const sections = parseAnalysisSections(ai.analysis)
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
          padding: '8px 12px',
          borderRadius: 8,
          marginBottom: 8,
          background: `linear-gradient(90deg, ${hexToRgba(bannerColor, isSkip ? 0.1 : 0.14)}, ${hexToRgba(bannerColor, 0.02)})`,
          border: `1px solid ${hexToRgba(bannerColor, 0.3)}`,
        }}
      >
        <Space size={8} wrap>
          {isSkip ? (
            <CloseCircleOutlined style={{ color: bannerColor, fontSize: 16 }} />
          ) : (
            <CheckCircleOutlined style={{ color: bannerColor, fontSize: 16 }} />
          )}
          <strong style={{ fontSize: 15 }}>
            {isSkip ? '不建议开单' : '建议开单'}
          </strong>
          {!isSkip &&
            (ai.direction === 'long' ? (
              <Tag color={schemeTag(colorScheme).up} style={{ marginInlineEnd: 0 }}>
                做多 Long
              </Tag>
            ) : ai.direction === 'short' ? (
              <Tag color={schemeTag(colorScheme).down} style={{ marginInlineEnd: 0 }}>
                做空 Short
              </Tag>
            ) : null)}
          {!isSkip && tradeTypeCfg && (
            <Tag color={tradeTypeCfg.color} style={{ marginInlineEnd: 0 }}>
              {tradeTypeCfg.label}
            </Tag>
          )}
          {/* 待回踩标识：AI 建议等回踩至入场价再入场（限价委托已移除，此类建议仅展示、不下单） */}
          {!isSkip && ai.pullback_wait && (
            <Tooltip title="AI 建议等回踩至入场价再入场；程序不会为此下单，仅作跟踪展示">
              <Tag color="orange" style={{ marginInlineEnd: 0, fontWeight: 700 }}>
                ⏳ 待回踩
              </Tag>
            </Tooltip>
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

      {/* 未开单原因：suggest 被开仓闸门拦下时后端落库（强平闸门/在跑上限/余额不足/待回踩等）；
          进入下单序列后的成败由交易记录 Tab 呈现，不走此提示 */}
      {!isSkip && ai.open_block_reason && (
        <div
          style={{
            padding: '6px 12px',
            marginBottom: 8,
            borderRadius: 8,
            fontSize: 12,
            lineHeight: '20px',
            color: '#faad14',
            background: 'rgba(250,173,20,0.08)',
            border: '1px solid rgba(250,173,20,0.30)',
          }}>
          ⚠ 未开单原因：{ai.open_block_reason}
        </div>
      )}

      {/* 战法命中（2026-10-02，仅借鉴标注）：AI 判定信号符合用户战法库中的某战法；
          不影响开单结论，复盘按战法分组对比实盘表现。suggest/skip 均可命中 */}
      {ai.tactic_name && (
        <div
          style={{
            display: 'flex',
            alignItems: 'center',
            gap: 8,
            flexWrap: 'wrap',
            padding: '6px 12px',
            marginBottom: 8,
            borderRadius: 8,
            fontSize: 12,
            lineHeight: '20px',
            color: '#1677ff',
            background: 'rgba(22,119,255,0.06)',
            border: '1px solid rgba(22,119,255,0.30)',
          }}>
          <Tag color="blue" style={{ marginInlineEnd: 0, fontWeight: 700 }}>⚔ 战法匹配</Tag>
          <strong>{ai.tactic_name}</strong>
          {ai.tactic_reason && (
            <Text type="secondary" style={{ fontSize: 12 }}>{ai.tactic_reason}</Text>
          )}
        </div>
      )}

      {/* skip：两节解析成功时「AI 跳过理由 + 公共检查 + 决策结论」并入单卡（横线分隔）；
          解析失败回退「AI 跳过理由」气泡 */}
      {isSkip ? (
        <>
          {sections ? (
            <AnalysisSectionsView ai={ai} sections={sections} skipReason={eff.reason} />
          ) : (
            <Bubble
              variant="shadow"
              avatar={{
                icon: <RobotOutlined />,
                style: { background: '#8c8c8c', color: '#fff' },
              }}
              content={eff.reason || ai.analysis || ''}
              messageRender={(content) => <AnalysisItemList text={content as string} />}
              styles={{
                header: { fontSize: 12, color: '#8c8c8c', fontWeight: 600, paddingBottom: 2 },
                content: { maxWidth: '100%', paddingTop: 0 },
              }}
              header="AI 跳过理由"
            />
          )}
        </>
      ) : (
        <>
          {/* 价格卡：入场 / 止损（主值 AI 报价，副行箭头+程序参考价合并展示）/ 止盈1 / 止盈2 */}
          <div style={{ display: 'flex', gap: 8, marginBottom: 8 }}>
            <PriceCard label="入场价" value={ai.entry_price} color="#1677ff" />
            {/* 止损合并卡（2026-10-11）：主值=AI 报价（实际下单用）；程序止损参考
                （0.5×ATR 外扩锚价，仅对比不下单）以副行箭头并入——箭头=程序价相对
                AI 价的数值方向（↓更低/↑更高/＝一致），橙色=程序比 AI 更远离入场
                （多头更低/空头更高，更保守）；skip 行/旧数据/ATR 不可算为 null 只显主值 */}
            {(() => {
              const calc = ai.calc_stop_loss
              const sl = ai.stop_loss
              const wider =
                (ai.direction === 'long' && calc != null && (sl == null || calc < sl)) ||
                (ai.direction === 'short' && calc != null && (sl == null || calc > sl))
              const arrow = calc == null || sl == null ? '' : calc > sl ? '↑' : calc < sl ? '↓' : '＝'
              const card = (
                <PriceCard
                  label="止损价"
                  value={sl}
                  color={SCHEME_UP_DOWN[colorScheme].down}
                  sub={calc != null
                    ? { arrow: arrow || '＝', text: fmtPrice(calc), color: wider ? '#fa8c16' : undefined }
                    : undefined}
                />
              )
              return calc != null ? (
                <Tooltip title={`副行为程序止损参考（0.5×ATR 外扩：前一同侧摆动点与金K极值取更远者）——箭头指向程序价相对 AI 报价的方向（↓更低/↑更高/＝一致），${wider ? '橙色=程序比 AI 更远离入场（更保守）' : '灰色=程序不比 AI 更远'}。实际下单仍用主值（AI 报价）`}>
                  <div style={{ flex: 1, minWidth: 0 }}>{card}</div>
                </Tooltip>
              ) : card
            })()}
            <PriceCard label="止盈1" value={ai.take_profit_1} color={SCHEME_UP_DOWN[colorScheme].up} />
            {/* TP2 关闭时落库 0：显示「未启用」而非 0.0000 */}
            <PriceCard label="止盈2" value={ai.take_profit_2} color={SCHEME_UP_DOWN[colorScheme].up} zeroText="未启用" />
          </div>
          {/* 指标行：盈亏比 / 仓位 / 分析时间 / 运行轨迹 */}
          <div
            style={{
              display: 'flex',
              alignItems: 'center',
              gap: 14,
              flexWrap: 'wrap',
              padding: '0 4px',
              marginBottom: 8,
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
                onClick={() => setTraceOpen(true)}
              >
                运行轨迹
              </Button>
            )}
          </div>
          {/* AI 推理：两节解析成功走单卡分区展示（公共检查/决策结论，横线分隔），失败回退平铺气泡 */}
          {sections ? (
            <AnalysisSectionsView ai={ai} sections={sections} />
          ) : (
            <Bubble
              variant="shadow"
              avatar={{
                icon: <RobotOutlined />,
                style: { background: '#1677ff', color: '#fff' },
              }}
              content={ai.analysis || ''}
              messageRender={(content) => <AnalysisItemList text={content as string} />}
              styles={{
                header: { fontSize: 12, color: '#8c8c8c', fontWeight: 600, paddingBottom: 2 },
                content: { maxWidth: '100%', paddingTop: 0 },
              }}
              header="AI 推理"
            />
          )}
        </>
      )}

      <TraceModal open={traceOpen} onClose={() => setTraceOpen(false)} ai={ai} />
    </>
  )
}
