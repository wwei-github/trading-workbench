import { useState, useEffect } from 'react'
import { Modal, Form, InputNumber, Select, Switch, message, Button, Spin, Divider, Tooltip } from 'antd'
import { SettingOutlined, RobotOutlined, FileTextOutlined, ApiOutlined, UndoOutlined } from '@ant-design/icons'
import { useScanStore } from '../stores/scanStore'
import type { SystemConfig } from '../types'

/** 筛选参数组（2026-09-28 筛选配置上页面）：字段元数据驱动渲染 */
const FILTER_FIELDS: { name: string; label: string; min: number; max: number; step?: number; tooltip: string }[] = [
  { name: 'min_volume_24h', label: '24h成交额门槛（USDT）', min: 0, max: 1e9, step: 100000,
    tooltip: '扫描候选池流动性门槛，低于此不进扫描；0=不过滤' },
  { name: 'breakout_threshold', label: '最小突破幅度（比例）', min: 0, max: 0.05, step: 0.001,
    tooltip: '收盘须越过 Donchian 轨此幅度才算突破（0.002=0.2%），毛边突破不算；0=关闭' },
  { name: 'breakout_vol_ratio', label: '突破量比门槛', min: 0, max: 10, step: 0.1,
    tooltip: '信号K线成交量 ≥ 近20根均量的此倍数才确认放量突破' },
  { name: 'breakout_max_pct', label: '突破幅度上限（%）', min: 0, max: 20, step: 0.5,
    tooltip: '越过轨超过此幅度视为追高，不产生突破触发（正式网复盘：大幅突破追入负期望）；0=关闭' },
  { name: 'candle_max_ema21_dist_pct', label: '悬空金K距离上限（%）', min: 0, max: 20, step: 0.5,
    tooltip: '信号K线收盘距 EMA21 超过此幅度的纯金K不触发（触及关键位仍放行）；0=关闭' },
  { name: 'breakout_channel_bars', label: 'Donchian通道根数', min: 5, max: 200,
    tooltip: '突破判定的回看窗口根数（收盘破 N 根高低轨）' },
  { name: 'ai_max_per_scan', label: 'AI单轮分析上限（个）', min: 1, max: 50,
    tooltip: '单次扫描批量 AI 分析的币种上限，按质量分/24h成交额取前N（控制 LLM 成本）' },
]

/** 策略配置弹窗：扫描策略 + AI 分析/策略/Agent 开关 + AI 实验功能。
 *  （2026-09-20 工具栏的 AI/策略/Agent 三个快捷开关收敛至此；交易参数见
 *  TradingConfigPanel，环境/自动交易开关见 SystemConfigPanel） */
export default function ScanConfigPanel() {
  const [open, setOpen] = useState(false)
  const [form] = Form.useForm()
  const [saving, setSaving] = useState(false)
  const [loading, setLoading] = useState(false)
  // 打开弹窗时的筛选参数生效值快照：保存时比对，未改动则不提交 scan_filters
  // （避免把 env 派生值固化进 DB，保持「缺省回落 env」语义）
  const [filtersSnapshot, setFiltersSnapshot] = useState<Record<string, number>>({})
  const { aiConfig, updateConfig, fetchConfig } = useScanStore()

  // 每次打开 Modal 时，从后端拉取最新配置数据
  useEffect(() => {
    if (!open) return
    setLoading(true)
    fetchConfig().finally(() => {
      setLoading(false)
    })
  }, [open, fetchConfig])

  // 拿到最新配置后赋值给表单（筛选参数用实际生效值做初值）
  useEffect(() => {
    if (open && aiConfig) {
      const eff = aiConfig.scan_filters_effective || {}
      setFiltersSnapshot({ ...eff })
      form.setFieldsValue({
        kline_interval: aiConfig.kline_interval,
        kline_window: aiConfig.kline_window,
        repeat_window_hours: aiConfig.repeat_window_hours,
        swing_order: aiConfig.swing_order,
        ...FILTER_FIELDS.reduce((acc, f) => ({ ...acc, [f.name]: eff[f.name] }), {}),
        ai_analysis_enabled: aiConfig.ai_analysis_enabled ?? false,
        strategy_prompt_enabled: aiConfig.strategy_prompt_enabled ?? false,
        ai_pipeline_enabled: aiConfig.ai_pipeline_enabled ?? true,
        dual_judge_enabled: aiConfig.dual_judge_enabled ?? true,
      })
    }
  }, [open, aiConfig, form])

  const aiConfigured = !!aiConfig?.ai_configured
  const hasStrategy = !!aiConfig?.strategy_prompt

  const handleSave = async () => {
    try {
      const values = await form.validateFields()
      setSaving(true)
      const data: Partial<SystemConfig> = {
        kline_interval: values.kline_interval,
        kline_window: values.kline_window,
        repeat_window_hours: values.repeat_window_hours,
        swing_order: values.swing_order,
        ai_analysis_enabled: values.ai_analysis_enabled,
        strategy_prompt_enabled: values.strategy_prompt_enabled,
        ai_pipeline_enabled: values.ai_pipeline_enabled,
        dual_judge_enabled: values.dual_judge_enabled,
      }
      // 筛选参数组：仅在有改动时提交（后端逐键校验范围）；未动则保持
      // 「缺省回落 env」，不把生效值固化进 DB
      const filters = FILTER_FIELDS.reduce(
        (acc, f) => ({ ...acc, [f.name]: values[f.name] }), {},
      ) as Record<string, number>
      const dirty = FILTER_FIELDS.some(
        (f) => Number(filters[f.name]) !== Number(filtersSnapshot[f.name]),
      )
      if (dirty) data.scan_filters = filters
      await updateConfig(data)
      message.success('配置已保存')
      setOpen(false)
    } catch (e: any) {
      message.error(e?.response?.data?.detail || '保存失败')
    } finally {
      setSaving(false)
    }
  }

  // 恢复默认：清除库内定制（scan_filters=null），后端回落 env/代码默认
  const handleResetFilters = async () => {
    try {
      setSaving(true)
      await updateConfig({ scan_filters: null } as Partial<SystemConfig>)
      await fetchConfig()
      const eff = useScanStore.getState().aiConfig?.scan_filters_effective || {}
      form.setFieldsValue(FILTER_FIELDS.reduce((acc, f) => ({ ...acc, [f.name]: eff[f.name] }), {}))
      message.success('筛选参数已恢复默认')
    } catch (e: any) {
      message.error(e?.response?.data?.detail || '恢复默认失败')
    } finally {
      setSaving(false)
    }
  }

  return (
    <>
      <Button
        icon={<SettingOutlined />}
        onClick={() => setOpen(true)}
      >
        策略配置
      </Button>
      <Modal
        title="策略配置"
        open={open}
        onCancel={() => setOpen(false)}
        onOk={handleSave}
        confirmLoading={saving}
        width={500}
      >
        <Spin spinning={loading}>
        <Form form={form} layout="vertical">
          <Form.Item
            label="K线周期"
            name="kline_interval"
            rules={[{ required: true }]}
            tooltip="K线的时间周期"
          >
            <Select
              options={[
                { value: '1m', label: '1分钟' },
                { value: '5m', label: '5分钟' },
                { value: '15m', label: '15分钟' },
                { value: '30m', label: '30分钟' },
                { value: '1h', label: '1小时' },
                { value: '2h', label: '2小时' },
                { value: '4h', label: '4小时' },
                { value: '1d', label: '1天' },
              ]}
            />
          </Form.Item>
          <Form.Item
            label="K线数量"
            name="kline_window"
            rules={[{ required: true }]}
            tooltip="每次扫描获取的K线根数"
          >
            <InputNumber min={30} max={1500} style={{ width: '100%' }} />
          </Form.Item>
          <Form.Item
            label="重复命中窗口（小时）"
            name="repeat_window_hours"
            rules={[{ required: true }]}
            tooltip="在此时间窗口内的命中视为重复"
          >
            <InputNumber min={1} max={168} style={{ width: '100%' }} />
          </Form.Item>
          <Form.Item
            label="摆动点阶数"
            name="swing_order"
            rules={[{ required: true }]}
            tooltip="AI 事实包中摆动点确认的左右比较根数"
          >
            <InputNumber min={1} max={10} style={{ width: '100%' }} />
          </Form.Item>

          {/* 筛选参数组（2026-09-28 筛选配置上页面）：DB > env > 代码默认 */}
          <Divider style={{ margin: '8px 0 16px' }}>
            <span style={{ fontSize: 13, color: '#999' }}>筛选参数</span>
          </Divider>
          {FILTER_FIELDS.map((f) => (
            <Form.Item
              key={f.name}
              label={f.label}
              name={f.name}
              rules={[{ required: true, message: `请填写${f.label}` }]}
              tooltip={f.tooltip}
            >
              <InputNumber min={f.min} max={f.max} step={f.step} style={{ width: '100%' }} />
            </Form.Item>
          ))}
          <Button size="small" icon={<UndoOutlined />} onClick={handleResetFilters} loading={saving}>
            恢复默认
          </Button>

          {/* AI 分析开关区（原工具栏快捷开关，2026-09-20 收敛至此） */}
          <Divider style={{ margin: '8px 0 16px' }}>
            <span style={{ fontSize: 13, color: '#999' }}>AI 分析</span>
          </Divider>
          <Form.Item
            label={
              <span>
                <RobotOutlined style={{ marginRight: 6 }} />筛选后自动 AI 分析
              </span>
            }
            name="ai_analysis_enabled"
            valuePropName="checked"
            tooltip={aiConfigured ? '开启后扫描命中币种自动触发 AI 分析（独立于自动交易开关；手动单币分析不受影响）' : '后端未配置 AI_API_KEY，无法使用 AI 分析'}
          >
            <Switch disabled={!aiConfigured} />
          </Form.Item>
          <Tooltip
            title={
              hasStrategy
                ? '开启后，AI 分析时将"策略提示词"一并发给 AI 参考'
                : '请先在"策略提示词"页签中编写并保存策略'
            }
          >
            <Form.Item
              label={
                <span>
                  <FileTextOutlined style={{ marginRight: 6 }} />策略开关
                </span>
              }
              name="strategy_prompt_enabled"
              valuePropName="checked"
            >
              <Switch />
            </Form.Item>
          </Tooltip>
          <Form.Item
            label={
              <span>
                <ApiOutlined style={{ marginRight: 6 }} />Agent 管线
              </span>
            }
            name="ai_pipeline_enabled"
            valuePropName="checked"
            tooltip="开启：工具循环 + 结构位锚点 + 技能库；关闭：单次调用 + 校验回炉"
          >
            <Switch disabled={!aiConfigured} />
          </Form.Item>

          {/* AI 实验功能（P2，默认关闭） */}
          <Divider style={{ margin: '8px 0 16px' }}>
            <span style={{ fontSize: 13, color: '#999' }}>AI 实验功能（默认关闭）</span>
          </Divider>
          <Form.Item
            label="双评委辩论"
            name="dual_judge_enabled"
            valuePropName="checked"
            tooltip="对'建议开单'的决策做多空辩论复核，裁判可否决（额外LLM调用，仅Agent/单次管线的suggest决策生效）"
          >
            <Switch />
          </Form.Item>
        </Form>
        </Spin>
      </Modal>
    </>
  )
}
