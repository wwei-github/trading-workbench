import { useState, useEffect } from 'react'
import { Modal, Form, InputNumber, message, Button, Spin } from 'antd'
import { ThunderboltOutlined } from '@ant-design/icons'
import { useScanStore } from '../stores/scanStore'
import type { SystemConfig } from '../types'

/** 交易配置弹窗（2026-09-20 拆分自 ScanConfigPanel）：自动交易参数 + 开仓闸门补丁参数 */
export default function TradingConfigPanel() {
  const [open, setOpen] = useState(false)
  const [form] = Form.useForm()
  const [saving, setSaving] = useState(false)
  const [loading, setLoading] = useState(false)
  const { aiConfig, updateConfig, fetchConfig } = useScanStore()

  useEffect(() => {
    if (!open) return
    setLoading(true)
    fetchConfig().finally(() => setLoading(false))
  }, [open, fetchConfig])

  useEffect(() => {
    if (open && aiConfig) {
      form.setFieldsValue({
        max_open_trades: aiConfig.max_open_trades ?? 5,
        // 表单里按百分比（0~100）编辑，保存时换算回 0~1 比例
        trading_min_free_pct: Math.round((aiConfig.trading_min_free_pct ?? 0.5) * 10000) / 100,
        // 补丁参数（比例→%编辑；null = 未设/默认）
        max_stop_pct: aiConfig.max_stop_pct != null ? Math.round(aiConfig.max_stop_pct * 10000) / 100 : null,
        liq_gate_buffer: aiConfig.liq_gate_buffer != null ? Math.round(aiConfig.liq_gate_buffer * 10000) / 100 : null,
        min_adapt_leverage: aiConfig.min_adapt_leverage ?? null,
        pullback_market_pct: aiConfig.pullback_market_pct != null
          ? Math.round(aiConfig.pullback_market_pct * 10000) / 100
          : null,
        max_slippage_pct: aiConfig.max_slippage_pct != null
          ? Math.round(aiConfig.max_slippage_pct * 10000) / 100
          : null,
      })
    }
  }, [open, aiConfig, form])

  const handleSave = async () => {
    try {
      const values = await form.validateFields()
      setSaving(true)
      const data: Partial<SystemConfig> = {
        max_open_trades: values.max_open_trades,
        trading_min_free_pct:
          values.trading_min_free_pct != null ? values.trading_min_free_pct / 100 : undefined,
        // 补丁参数（null/清空 = 恢复默认；后端区分「未传」与「传 null」）
        max_stop_pct: values.max_stop_pct != null ? values.max_stop_pct / 100 : null,
        liq_gate_buffer: values.liq_gate_buffer != null ? values.liq_gate_buffer / 100 : null,
        min_adapt_leverage: values.min_adapt_leverage ?? null,
        pullback_market_pct: values.pullback_market_pct != null ? values.pullback_market_pct / 100 : null,
        max_slippage_pct: values.max_slippage_pct != null ? values.max_slippage_pct / 100 : null,
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
        icon={<ThunderboltOutlined />}
        onClick={() => setOpen(true)}
      >
        交易配置
      </Button>
      <Modal
        title="交易配置"
        open={open}
        onCancel={() => setOpen(false)}
        onOk={handleSave}
        confirmLoading={saving}
        width={500}
      >
        <Spin spinning={loading}>
        <Form form={form} layout="vertical">
          <Form.Item
            label="同时在跑单子上限"
            name="max_open_trades"
            initialValue={5}
            rules={[{ required: true, message: '请输入在跑单子上限' }]}
            tooltip="自动开仓的在跑单子数量上限，0 = 不限单数（仅按开仓最低可用余额判断）；每小时任务先结算再开仓，评分高的优先"
          >
            <InputNumber min={0} max={50} style={{ width: '100%' }} />
          </Form.Item>

          <Form.Item
            label="开仓最低可用余额（%）"
            name="trading_min_free_pct"
            initialValue={50}
            rules={[{ required: true, message: '请输入开仓最低可用余额比例' }]}
            tooltip="可用余额低于总资金该比例时不开新单"
          >
            <InputNumber min={0} max={100} step={1} style={{ width: '100%' }} />
          </Form.Item>

          <Form.Item
            label="止损宽度上限（%，留空=不限制）"
            name="max_stop_pct"
            tooltip="开仓闸门的止损宽度硬上限（AI 复盘补丁参数 max_stop_pct 的手改入口，与补丁回滚共用同一列）"
          >
            <InputNumber min={0.1} max={10} step={0.1} style={{ width: '100%' }} placeholder="不限制" />
          </Form.Item>

          <Form.Item
            label="强平闸门缓冲（%，留空=默认 0.5%）"
            name="liq_gate_buffer"
            tooltip="强平闸门 = 1/杠杆 − 维持保证金率 − 缓冲（覆盖手续费/资金费/触发价差；AI 复盘补丁参数 liq_gate_buffer 的手改入口）"
          >
            <InputNumber min={0.1} max={5} step={0.1} style={{ width: '100%' }} placeholder="0.5" />
          </Form.Item>

          <Form.Item
            label="动态杠杆下限（×，留空=默认 8）"
            name="min_adapt_leverage"
            tooltip="止损距离超 20× 强平闸门时自动降杠杆开单；安全杠杆低于此下限的宽止损单直接放弃（限制层：不是所有单都动态算完就开）。8× 约等于止损距离上限 10.5%（小币）"
          >
            <InputNumber min={1} max={20} step={1} style={{ width: '100%' }} placeholder="8" precision={0} />
          </Form.Item>

          <Form.Item
            label="入场偏离容差（%，留空=默认 1）"
            name="pullback_market_pct"
            tooltip="开仓时现价对 AI 入场价的最大偏离。待回踩距离 ≤ 容差时市价入场（盈亏比按现价复算 ≥1.5 才放行），超容差维持待回踩不下单；普通单的开仓漂移上限同用此值"
          >
            <InputNumber min={0.1} max={5} step={0.1} style={{ width: '100%' }} placeholder="1" />
          </Form.Item>

          <Form.Item
            label="开仓滑点上限（%，留空=默认 0.3）"
            name="max_slippage_pct"
            tooltip="开仓前按本单数量走订单簿逐层吃，预期滑点超上限放弃开单（盘口过薄的小币会撑宽止损、实际风险超预算）。0=关闭滑点闸门"
          >
            <InputNumber min={0} max={5} step={0.05} style={{ width: '100%' }} placeholder="0.3" />
          </Form.Item>
        </Form>
        </Spin>
      </Modal>
    </>
  )
}
