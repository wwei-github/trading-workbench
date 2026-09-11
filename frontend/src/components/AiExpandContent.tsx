import { useEffect, useRef, useState } from 'react'
import { Alert, Button, Input, Typography } from 'antd'
import { RobotOutlined, LoadingOutlined } from '@ant-design/icons'
import AiAnalysisCard from './AiAnalysisCard'
import { scanApi } from '../api/scan'
import type { AIAnalysis, AiProgressEvent } from '../types'

const { Text } = Typography

interface Props {
  aiEnabled: boolean
  ai?: AIAnalysis
  loading: boolean
  error?: string | null
  userInput: string
  onUserInput: (v: string) => void
  onTrigger: () => void
  /** 扫描结果 ID：提供时展示流式分析进度（关注列表中从未扫描过的币种没有该 ID） */
  scanResultId?: string
}

/** 进度事件 → 展示文本 */
function describeEvent(ev: AiProgressEvent): { text: string; color?: string } {
  switch (ev.t) {
    case 'start':
      return {
        text: `开始分析（${ev.pipeline === 'agent' ? 'Agent 管线' : '单次调用'}）`,
      }
    case 'gate':
      return { text: `⚡ ${ev.note || '规则闸门'}`, color: '#faad14' }
    case 'round':
      if (ev.round === 0) return { text: ev.note || '单次调用管线思考中…' }
      return {
        text: `第 ${ev.round} 轮 · 调用工具：${ev.tools?.join('、') || '无'}`,
      }
    case 'tool':
      return { text: `　↳ ${ev.tool}${ev.args ? `(${ev.args})` : ''}`, color: '#888' }
    case 'done':
      return {
        text: `✅ 分析完成${ev.decision === 'suggest' ? '：建议开单' : ev.decision === 'skip' ? '：跳过' : ''}`,
        color: '#52c41a',
      }
    case 'error':
      return { text: `❌ 分析失败：${ev.note || '未知错误'}`, color: '#ff4d4f' }
    default:
      return { text: JSON.stringify(ev) }
  }
}

/**
 * AI 分析进度面板：2s 轮询后端事件流，"流式"展示分析过程。
 * 分析结束后父组件 loading 翻转为 false，本面板卸载，仅展示结果卡片。
 */
function AiProgressPanel({ scanResultId }: { scanResultId: string }) {
  const [events, setEvents] = useState<AiProgressEvent[]>([])
  const mounted = useRef(true)

  useEffect(() => {
    mounted.current = true
    const poll = () =>
      scanApi
        .aiProgress(scanResultId)
        .then((d) => {
          if (mounted.current) setEvents(d.events)
        })
        .catch(() => undefined)
    poll()
    const timer = setInterval(poll, 2000)
    return () => {
      mounted.current = false
      clearInterval(timer)
    }
  }, [scanResultId])

  const finished = events.some((e) => e.t === 'done' || e.t === 'error')

  return (
    <div
      style={{
        padding: '10px 14px',
        background: '#fafafa',
        borderRadius: 8,
        maxHeight: 260,
        overflow: 'auto',
        fontSize: 13,
        lineHeight: '22px',
      }}
    >
      <div style={{ marginBottom: 4 }}>
        {finished ? (
          <Text type="secondary">分析已结束，正在载入结果…</Text>
        ) : (
          <span>
            <LoadingOutlined style={{ marginRight: 8 }} />
            <Text type="secondary">AI 分析进行中（过程实时展示）</Text>
          </span>
        )}
      </div>
      {events.length === 0 ? (
        <Text type="secondary">等待分析任务开始…</Text>
      ) : (
        events.map((ev, i) => {
          const { text, color } = describeEvent(ev)
          return (
            <div key={i} style={{ color: color || undefined, whiteSpace: 'pre-wrap' }}>
              {text}
            </div>
          )
        })
      )}
    </div>
  )
}

/**
 * AI 分析展开内容（扫描结果行展开 / 关注列表行展开共用）：
 * 未开启提示 → 分析中（流式进度）→ 无结果（补充说明 + 分析按钮）→ 已有结果（卡片 + 重新分析）
 * 分析结束后仅展示分析结果（进度面板随 loading 结束而消失）。
 */
export default function AiExpandContent({
  aiEnabled,
  ai,
  loading,
  error,
  userInput,
  onUserInput,
  onTrigger,
  scanResultId,
}: Props) {
  if (!aiEnabled) {
    return (
      <div style={{ textAlign: "center", padding: "20px 0" }}>
        <Text type="secondary">AI 分析未开启</Text>
      </div>
    );
  }

  // 分析进行中：流式展示进度（替代结果卡片，结束后仅展示结果）
  if (loading) {
    return (
      <div style={{ padding: "8px 0" }}>
        {scanResultId ? (
          <AiProgressPanel scanResultId={scanResultId} />
        ) : (
          <div style={{ textAlign: "center", padding: "20px 0" }}>
            <LoadingOutlined style={{ fontSize: 24 }} />
            <div style={{ marginTop: 8 }}>
              <Text type="secondary">AI 正在分析中，请稍候...</Text>
            </div>
          </div>
        )}
        {error && (
          <Alert
            type="error"
            message="AI 分析失败"
            description={error}
            showIcon
            style={{ marginTop: 8, textAlign: "left" }}
          />
        )}
      </div>
    );
  }

  if (!ai) {
    return (
      <div style={{ textAlign: "center", padding: "20px 0" }}>
        {error && (
          <Alert
            type="error"
            message="AI 分析失败"
            description={error}
            showIcon
            style={{ marginBottom: 12, textAlign: "left" }}
          />
        )}
        <Input.TextArea
          rows={2}
          placeholder="补充说明（可选）：你的判断或对 AI 的要求，将随分析一起提交"
          value={userInput}
          onChange={(e) => onUserInput(e.target.value)}
          style={{ marginBottom: 8 }}
        />
        <Button
          type="primary"
          ghost
          size="small"
          icon={<RobotOutlined />}
          onClick={onTrigger}>
          AI 分析
        </Button>
      </div>
    );
  }

  return (
    <div>
      <AiAnalysisCard ai={ai} />
      {error && (
        <Alert
          type="error"
          message="上次重新分析失败"
          description={error}
          showIcon
          style={{ marginTop: 8, textAlign: "left" }}
        />
      )}
      <Input.TextArea
        rows={2}
        placeholder="补充说明（可选）：你的判断或对 AI 的要求，将随分析一起提交"
        value={userInput}
        onChange={(e) => onUserInput(e.target.value)}
        style={{ marginTop: 8, marginBottom: 8 }}
      />
      <div style={{ textAlign: "center" }}>
        <Button
          type="primary"
          ghost
          size="small"
          icon={<RobotOutlined />}
          onClick={onTrigger}>
          重新分析
        </Button>
      </div>
    </div>
  );
}
