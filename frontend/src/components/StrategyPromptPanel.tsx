import { useEffect, useState } from 'react'
import {
  Button,
  Card,
  Drawer,
  Empty,
  List,
  Space,
  Spin,
  Tabs,
  Tag,
  Tooltip,
  Typography,
  message,
} from 'antd'
import { SaveOutlined, QuestionCircleOutlined } from '@ant-design/icons'
import { useScanStore } from '../stores/scanStore'
import { scanApi } from '../api/scan'
import type { SkillInfo } from '../types'

const { Text } = Typography

/** 技能库 Tab：展示只读技能列表，点击查看全文（技能是可插拔的交易打法文件） */
function SkillsTab() {
  const [skills, setSkills] = useState<SkillInfo[]>([])
  const [loading, setLoading] = useState(false)
  // 当前打开详情的技能名 + 全文内容
  const [activeName, setActiveName] = useState<string | null>(null)
  const [detail, setDetail] = useState<SkillInfo | null>(null)
  const [detailLoading, setDetailLoading] = useState(false)

  // 初始加载技能列表
  useEffect(() => {
    setLoading(true)
    scanApi
      .skills()
      .then((list) => setSkills(list))
      .catch((e: any) => message.error(e?.response?.data?.detail || '获取技能库失败'))
      .finally(() => setLoading(false))
  }, [])

  // 打开抽屉时拉取技能全文
  const openDetail = (name: string) => {
    setActiveName(name)
    setDetail(null)
    setDetailLoading(true)
    scanApi
      .skillDetail(name)
      .then((info) => setDetail(info))
      .catch((e: any) => message.error(e?.response?.data?.detail || '获取技能详情失败'))
      .finally(() => setDetailLoading(false))
  }

  return (
    <div>
      <Text type="secondary" style={{ fontSize: 12 }}>
        技能是可插拔的交易打法文件，AI Agent 决策时按触发条件按需加载（只读）
      </Text>
      <Spin spinning={loading}>
        {skills.length === 0 && !loading ? (
          <Empty description="技能库为空（backend/skills/ 目录）" style={{ marginTop: 40 }} />
        ) : (
          <List
            size="small"
            dataSource={skills}
            renderItem={(s) => (
              <List.Item
                style={{ cursor: 'pointer' }}
                onClick={() => openDetail(s.name)}
              >
                <div>
                  <Space wrap size={8}>
                    <Text strong>{s.name}</Text>
                    <Tag>v{s.version}</Tag>
                  </Space>
                  <div style={{ marginTop: 4 }}>{s.description}</div>
                  <div style={{ marginTop: 4 }}>
                    <code
                      style={{
                        fontSize: 12,
                        background: '#f5f5f5',
                        padding: '1px 6px',
                        borderRadius: 4,
                      }}
                    >
                      触发：{s.use_when}
                    </code>
                  </div>
                </div>
              </List.Item>
            )}
          />
        )}
      </Spin>

      {/* 技能全文抽屉 */}
      <Drawer
        title={activeName}
        width={520}
        open={!!activeName}
        onClose={() => setActiveName(null)}
      >
        {detailLoading ? (
          <div style={{ textAlign: 'center', marginTop: 40 }}>
            <Spin />
          </div>
        ) : detail?.body ? (
          <pre
            style={{
              whiteSpace: 'pre-wrap',
              fontFamily: 'monospace',
              fontSize: 12,
              margin: 0,
            }}
          >
            {detail.body}
          </pre>
        ) : (
          <Empty description="无技能内容" />
        )}
      </Drawer>
    </div>
  )
}

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
 * 外层 Tabs：'策略提示词'（编辑）+ '技能库'（只读浏览）。
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
    <Tabs
      items={[
        {
          key: 'prompt',
          label: '策略提示词',
          children: (
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
          ),
        },
        {
          key: 'skills',
          label: '技能库',
          children: <SkillsTab />,
        },
      ]}
    />
  )
}
