import { useState, useEffect } from 'react'
import { Modal, Form, InputNumber, Select, message, Button, Spin, Divider, Switch } from 'antd'
import { SettingOutlined } from '@ant-design/icons'
import { useScanStore } from '../stores/scanStore'
import type { SystemConfig } from '../types'

export default function ScanConfigPanel() {
  const [open, setOpen] = useState(false)
  const [form] = Form.useForm()
  const [saving, setSaving] = useState(false)
  const [loading, setLoading] = useState(false)
  const { aiConfig, updateConfig, fetchConfig } = useScanStore()

  // 每次打开 Modal 时，从后端拉取最新配置数据
  useEffect(() => {
    if (!open) return
    setLoading(true)
    fetchConfig().finally(() => {
      setLoading(false)
    })
  }, [open, fetchConfig])

  // 拿到最新配置后赋值给表单
  useEffect(() => {
    if (open && aiConfig) {
      form.setFieldsValue({
        kline_interval: aiConfig.kline_interval,
        kline_window: aiConfig.kline_window,
        breakout_threshold: aiConfig.breakout_threshold,
        r_squared_threshold: aiConfig.r_squared_threshold,
        repeat_window_hours: aiConfig.repeat_window_hours,
        swing_order: aiConfig.swing_order,
        pullback_tolerance: aiConfig.pullback_tolerance,
        key_level_tolerance: aiConfig.key_level_tolerance,
        level_merge_threshold: aiConfig.level_merge_threshold,
        max_open_trades: aiConfig.max_open_trades ?? 5,
        strategy_trend_follow_enabled: aiConfig.strategy_trend_follow_enabled ?? true,
        strategy_structure_break_enabled: aiConfig.strategy_structure_break_enabled ?? false,
        strategy_range_edge_enabled: aiConfig.strategy_range_edge_enabled ?? true,
      })
    }
  }, [open, aiConfig, form])

  const handleSave = async () => {
    try {
      const values = await form.validateFields()
      setSaving(true)
      const data: Partial<SystemConfig> = {
        kline_interval: values.kline_interval,
        kline_window: values.kline_window,
        breakout_threshold: values.breakout_threshold,
        r_squared_threshold: values.r_squared_threshold,
        repeat_window_hours: values.repeat_window_hours,
        swing_order: values.swing_order,
        pullback_tolerance: values.pullback_tolerance,
        // 关键位参数
        key_level_tolerance: values.key_level_tolerance,
        level_merge_threshold: values.level_merge_threshold,
        // 自动交易
        max_open_trades: values.max_open_trades,
        // 开单策略开关
        strategy_trend_follow_enabled: values.strategy_trend_follow_enabled,
        strategy_structure_break_enabled: values.strategy_structure_break_enabled,
        strategy_range_edge_enabled: values.strategy_range_edge_enabled,
        // P2 实验开关
        dual_judge_enabled: values.dual_judge_enabled,
      }
      await updateConfig(data)
      message.success('配置已保存')
      setOpen(false)
    } catch (e: any) {
      message.error(e?.response?.data?.detail || '保存失败')
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
        title="扫描策略配置"
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
            label="突破阈值"
            name="breakout_threshold"
            rules={[{ required: true }]}
            tooltip="突破幅度阈值，0.005 = 0.5%"
          >
            <InputNumber step={0.001} min={0} max={1} style={{ width: '100%' }} />
          </Form.Item>
          <Form.Item
            label="R² 阈值"
            name="r_squared_threshold"
            rules={[{ required: true }]}
            tooltip="趋势线拟合度阈值，越大要求越严格"
          >
            <InputNumber step={0.05} min={0} max={1} style={{ width: '100%' }} />
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
            tooltip="计算摆动点时的左右比较根数"
          >
            <InputNumber min={1} max={10} style={{ width: '100%' }} />
          </Form.Item>
          <Form.Item
            label="回调容差"
            name="pullback_tolerance"
            rules={[{ required: true }]}
            tooltip="上涨回调策略的容差比例，0.03 = 3%"
          >
            <InputNumber step={0.01} min={0} max={1} style={{ width: '100%' }} />
          </Form.Item>
          <Form.Item
            label="关键位区域半宽"
            name="key_level_tolerance"
            initialValue={0.005}
            rules={[{ required: true }]}
            tooltip="关键位是区域：中心价 ± 半宽，0.005 = ±0.5%，价格进入区域即算到位"
          >
            <InputNumber step={0.001} min={0} max={0.1} style={{ width: '100%' }} />
          </Form.Item>
          <Form.Item
            label="支撑/压力聚类合并阈值"
            name="level_merge_threshold"
            initialValue={0.005}
            rules={[{ required: true }]}
            tooltip="相互距离 ≤ 阈值的摆动点合并为一个水平区域，0.005 = 0.5%"
          >
            <InputNumber step={0.001} min={0} max={0.1} style={{ width: '100%' }} />
          </Form.Item>

          {/* 自动交易（docs/06） */}
          <Divider style={{ margin: '8px 0 16px' }}>
            <span style={{ fontSize: 13, color: '#999' }}>自动交易</span>
          </Divider>
          <Form.Item
            label="同时在跑单子上限"
            name="max_open_trades"
            initialValue={5}
            rules={[{ required: true, message: '请输入在跑单子上限' }]}
            tooltip="自动开仓的在跑单子数量上限；每小时任务先结算再开仓，评分高的优先"
          >
            <InputNumber min={1} max={50} style={{ width: '100%' }} />
          </Form.Item>

          <Form.Item
            label="开单策略：顺势交易"
            name="strategy_trend_follow_enabled"
            valuePropName="checked"
            tooltip="趋势中顺 EMA 方向开仓；EMA 三线严格排列（多单21>55>144、空单144>55>21）是其硬前提"
          >
            <Switch />
          </Form.Item>
          <Form.Item
            label="开单策略：区间边缘反转"
            name="strategy_range_edge_enabled"
            valuePropName="checked"
            tooltip="震荡区间触及顶/底边缘且出现反转形态时反向开仓，止盈看中轨"
          >
            <Switch />
          </Form.Item>
          <Form.Item
            label="开单策略：结构破位回踩"
            name="strategy_structure_break_enabled"
            valuePropName="checked"
            tooltip="123法则·N字结构·2B法则合并打法（破位-回踩-确认）；默认关闭"
          >
            <Switch />
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
