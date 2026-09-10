import { Alert, Button, Input, Typography } from 'antd'
import { RobotOutlined, LoadingOutlined } from '@ant-design/icons'
import AiAnalysisCard from './AiAnalysisCard'
import type { AIAnalysis } from '../types'

const { Text } = Typography

interface Props {
  aiEnabled: boolean
  ai?: AIAnalysis
  loading: boolean
  error?: string | null
  userInput: string
  onUserInput: (v: string) => void
  onTrigger: () => void
}

/**
 * AI 分析展开内容（扫描结果行展开 / 关注列表行展开共用）：
 * 未开启提示 → 加载中 → 无结果（补充说明 + 分析按钮）→ 已有结果（卡片 + 重新分析）
 */
export default function AiExpandContent({
  aiEnabled,
  ai,
  loading,
  error,
  userInput,
  onUserInput,
  onTrigger,
}: Props) {
  if (!aiEnabled) {
    return (
      <div style={{ textAlign: "center", padding: "20px 0" }}>
        <Text type="secondary">AI 分析未开启</Text>
      </div>
    );
  }

  if (loading && !ai) {
    return (
      <div style={{ textAlign: "center", padding: "20px 0" }}>
        <LoadingOutlined style={{ fontSize: 24 }} />
        <div style={{ marginTop: 8 }}>
          <Text type="secondary">AI 正在分析中，请稍候...</Text>
        </div>
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
          loading={loading}
          onClick={onTrigger}>
          AI 分析
        </Button>
      </div>
    );
  }

  return (
    <div>
      {loading && (
        <Alert
          type="info"
          message={
            <span>
              <LoadingOutlined /> AI 重新分析中...
            </span>
          }
          style={{ marginBottom: 8 }}
        />
      )}
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
          loading={loading}
          onClick={onTrigger}>
          重新分析
        </Button>
      </div>
    </div>
  );
}
