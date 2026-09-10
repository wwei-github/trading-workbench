import { useEffect, useState } from 'react'
import { Button, Card, Space, Tooltip, Typography, message } from 'antd'
import { SaveOutlined, QuestionCircleOutlined } from '@ant-design/icons'
import { useScanStore } from '../stores/scanStore'

const { Text } = Typography

const DEFAULT_PROMPT = `# 我的交易策略

## 开仓原则
- 只做顺势单，不抄底不摸顶
- 突破必须带量，缩量突破视为假突破

## 风险控制
- 单笔仓位不超过总资金的 5%
- 盈亏比低于 1.5 的机会直接放弃

## 优先级
1. 上涨回调 + 底部反转形态 > 区间震荡下沿 > 下跌突破
`

/**
 * 策略提示词 Tab：Markdown 格式编辑、保存。
 * 策略开关在顶部工具栏（AI 开关旁），开启后 AI 分析时携带此策略。
 */
export default function StrategyPromptPanel() {
  const { aiConfig, fetchConfig, updateConfig } = useScanStore()

  const [text, setText] = useState('')
  const [dirty, setDirty] = useState(false)
  const [saving, setSaving] = useState(false)
  const [loaded, setLoaded] = useState(false)

  // 配置加载后初始化编辑内容（只初始化一次，避免覆盖未保存的编辑）
  useEffect(() => {
    fetchConfig().then(() => setLoaded(true))
  }, [fetchConfig])

  useEffect(() => {
    if (loaded && aiConfig && !dirty) {
      setText(aiConfig.strategy_prompt || '')
    }
  }, [loaded, aiConfig, dirty])

  const strategyEnabled = !!aiConfig?.strategy_prompt_enabled

  const handleSave = async () => {
    setSaving(true)
    try {
      await updateConfig({ strategy_prompt: text })
      setDirty(false)
      message.success('策略提示词已保存')
    } catch (e: any) {
      message.error(e?.message || '保存失败')
    } finally {
      setSaving(false)
    }
  }

  return (
    <Card
      size="small"
      title={
        <Space>
          <span>策略提示词（Markdown）</span>
          <Tooltip title={'启用方式：顶部工具栏 AI 开关旁的「策略」开关，开启后 AI 分析时携带此策略'}>
            <QuestionCircleOutlined style={{ color: '#999' }} />
          </Tooltip>
          <Text type={strategyEnabled ? 'success' : 'secondary'} style={{ fontSize: 12 }}>
            {strategyEnabled ? '已启用' : '未启用（在顶部工具栏开启）'}
          </Text>
        </Space>
      }
      extra={
        <Space>
          <Text type={dirty ? 'warning' : 'secondary'} style={{ fontSize: 12 }}>
            {dirty ? '有未保存修改' : `${text.length} 字`}
          </Text>
          <Button
            type="primary"
            size="small"
            icon={<SaveOutlined />}
            loading={saving}
            disabled={!dirty}
            onClick={handleSave}>
            保存
          </Button>
        </Space>
      }
      styles={{ body: { padding: 12 } }}>
      <textarea
        value={text}
        onChange={(e) => {
          setText(e.target.value)
          setDirty(true)
        }}
        spellCheck={false}
        placeholder="用 Markdown 编写你的交易策略，AI 分析时会作为参考。例如：# 我的策略&#10;- 只做顺势单&#10;- 突破必须带量"
        style={{
          width: '100%',
          height: 'calc(100vh - 320px)',
          minHeight: 320,
          resize: 'vertical',
          padding: 12,
          borderRadius: 6,
          border: '1px solid #d9d9d9',
          background: '#fafafa',
          color: '#333',
          fontFamily: "'SFMono-Regular', Consolas, 'Liberation Mono', Menlo, monospace",
          fontSize: 13,
          lineHeight: 1.7,
          outline: 'none',
          boxSizing: 'border-box',
        }}
      />
      <div style={{ marginTop: 8 }}>
        <Space>
          <Text type="secondary" style={{ fontSize: 12 }}>
            支持 Markdown：# 标题、- 列表、**加粗**、1. 有序列表等。保存后，开启"策略开关"即可让 AI 参考。
          </Text>
          {!text && (
            <Button size="small" type="link" onClick={() => { setText(DEFAULT_PROMPT); setDirty(true) }}>
              插入示例模板
            </Button>
          )}
        </Space>
      </div>
    </Card>
  )
}
