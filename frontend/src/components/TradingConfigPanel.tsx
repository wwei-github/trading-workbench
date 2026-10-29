import { useState, useEffect, useRef } from 'react'
import { Modal, Form, InputNumber, message, Button, Spin, Radio, Select, Switch, Row, Col, Alert, Table, Tag } from 'antd'
import { ThunderboltOutlined } from '@ant-design/icons'
import { useScanStore } from '../stores/scanStore'
import ConfigSection from './ConfigSection'
import type { SystemConfig, TradeRecord } from '../types'
import { scanApi } from '../api/scan'
import { diffConfig } from '../utils/configVerify'
import { bj } from '../utils/dayjs'

/** 交易配置弹窗（2026-09-20 拆分自 ScanConfigPanel）：自动交易参数 + 开仓闸门补丁参数 */
export default function TradingConfigPanel() {
  const [open, setOpen] = useState(false)
  const [form] = Form.useForm()
  const [saving, setSaving] = useState(false)
  const [loading, setLoading] = useState(false)
  // 全平开关「关→开」时的存量单提示（docs/23 T14c，2026-10-29）：
  // 只对新单生效，存量单不会被补平——不先说清，用户会以为瞬间全平
  const [runningTrades, setRunningTrades] = useState<TradeRecord[]>([])
  const [fullExitConfirm, setFullExitConfirm] = useState(false)
  const confirmFullExitRef = useRef(false)
  const { aiConfig, updateConfig, fetchConfig, fetchStatus } = useScanStore()

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
        // 止损金额模式：pct=风险基数分档×3%（默认）/ fixed=固定 USDT 金额
        risk_budget_mode: aiConfig.risk_budget_mode ?? 'pct',
        risk_fixed_amount: aiConfig.risk_fixed_amount ?? 3,
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
        // 风控参数（2026-10-02 上页面）：null = 回落 env 默认；比例字段按 % 编辑
        min_recommendation: aiConfig.min_recommendation ?? null,
        daily_loss_stop_pct: aiConfig.daily_loss_stop_pct ?? null, // % 值（9=9%）直存
        // 熔断开关两态化（2026-10-04 去「默认」档）：null（未定制旧行）按开启展示，保存即写布尔
        daily_loss_stop_enabled: aiConfig.daily_loss_stop_enabled === false ? 'off' : 'on',
        ai_rr_min: aiConfig.ai_rr_min ?? null,
        ai_rr_tol: aiConfig.ai_rr_tol ?? null,
        risk_pct: aiConfig.risk_pct ?? null, // % 值（3=3%）直存
        leverage_max: aiConfig.leverage_max ?? null,
        taker_fee_rate: aiConfig.taker_fee_rate != null
          ? Math.round(aiConfig.taker_fee_rate * 10000) / 100
          : null, // 比例 0.0005 → 0.05%
        near_anchor_factor: aiConfig.near_anchor_factor ?? null,
        portfolio_heat_cap: aiConfig.portfolio_heat_cap != null
          ? Math.round(aiConfig.portfolio_heat_cap * 10000) / 100
          : null, // 比例 0.2 → 20%
        same_direction_cap: aiConfig.same_direction_cap ?? null,
        btc_crash_pause_pct: aiConfig.btc_crash_pause_pct ?? null, // % 值（1=1%）直存
        // 周/月回撤分层熔断 + ATR 跟进（docs/13 #2/#5，% 值/倍数直存）
        weekly_dd_stop_pct: aiConfig.weekly_dd_stop_pct ?? null,
        monthly_dd_stop_pct: aiConfig.monthly_dd_stop_pct ?? null,
        atr_trail_k: aiConfig.atr_trail_k ?? null,
        tp2_enabled: aiConfig.tp2_enabled ?? false,
        tp_full_exit_enabled: aiConfig.tp_full_exit_enabled ?? false,
        tp1_qty_pct_with_tp2: aiConfig.tp1_qty_pct_with_tp2 ?? null, // % 值直存（NULL=默认 60）
        tp2_qty_pct: aiConfig.tp2_qty_pct ?? null,
        tp1_qty_pct_no_tp2: aiConfig.tp1_qty_pct_no_tp2 ?? null,
      })
    }
  }, [open, aiConfig, form])

  const handleSave = async () => {
    let values: Record<string, any>
    try {
      values = await form.validateFields()
    } catch {
      return
    }
    // 存量单提示（docs/23 T14c）：全平模式从关→开时先列在跑单，说明「不补平、
    // 只对新单生效」。拉取失败不阻断保存（提示是旁路，配置必须能存下去）
    const turningOn = !!values.tp_full_exit_enabled && !aiConfig?.tp_full_exit_enabled
    if (turningOn && !confirmFullExitRef.current) {
      try {
        const lists = await Promise.all([
          scanApi.trades.list('OPENED'),
          scanApi.trades.list('TP1_HIT'),
          scanApi.trades.list('TP2_HIT'),
        ])
        const running = lists.flat()
        if (running.length > 0) {
          setRunningTrades(running)
          setFullExitConfirm(true)
          return
        }
      } catch {
        /* 持仓查询失败：继续正常保存 */
      }
    }
    confirmFullExitRef.current = false
    await persist(values)
  }

  const persist = async (values: Record<string, any>) => {
    try {
      setSaving(true)
      const data: Partial<SystemConfig> = {
        max_open_trades: values.max_open_trades,
        trading_min_free_pct:
          values.trading_min_free_pct != null ? values.trading_min_free_pct / 100 : undefined,
        // 止损金额模式（2026-09-27）
        risk_budget_mode: values.risk_budget_mode ?? 'pct',
        risk_fixed_amount: values.risk_fixed_amount ?? 3,
        // 补丁参数（null/清空 = 恢复默认；后端区分「未传」与「传 null」）
        max_stop_pct: values.max_stop_pct != null ? values.max_stop_pct / 100 : null,
        liq_gate_buffer: values.liq_gate_buffer != null ? values.liq_gate_buffer / 100 : null,
        min_adapt_leverage: values.min_adapt_leverage ?? null,
        pullback_market_pct: values.pullback_market_pct != null ? values.pullback_market_pct / 100 : null,
        max_slippage_pct: values.max_slippage_pct != null ? values.max_slippage_pct / 100 : null,
        // 风控参数（null/清空 = 恢复 env 默认；% 值字段直存，比例字段 /100）
        min_recommendation: values.min_recommendation ?? null,
        daily_loss_stop_pct: values.daily_loss_stop_pct ?? null,
        daily_loss_stop_enabled: values.daily_loss_stop_enabled === 'on',
        ai_rr_min: values.ai_rr_min ?? null,
        ai_rr_tol: values.ai_rr_tol ?? null,
        risk_pct: values.risk_pct ?? null,
        leverage_max: values.leverage_max ?? null,
        taker_fee_rate: values.taker_fee_rate != null ? values.taker_fee_rate / 100 : null,
        near_anchor_factor: values.near_anchor_factor ?? null,
        portfolio_heat_cap: values.portfolio_heat_cap != null ? values.portfolio_heat_cap / 100 : null,
        same_direction_cap: values.same_direction_cap ?? null,
        btc_crash_pause_pct: values.btc_crash_pause_pct ?? null,
        weekly_dd_stop_pct: values.weekly_dd_stop_pct ?? null,
        monthly_dd_stop_pct: values.monthly_dd_stop_pct ?? null,
        atr_trail_k: values.atr_trail_k ?? null,
        tp2_enabled: values.tp2_enabled ?? false,
        tp_full_exit_enabled: values.tp_full_exit_enabled ?? false,
        tp1_qty_pct_with_tp2: values.tp1_qty_pct_with_tp2 ?? null,
        tp2_qty_pct: values.tp2_qty_pct ?? null,
        tp1_qty_pct_no_tp2: values.tp1_qty_pct_no_tp2 ?? null,
      }
      await updateConfig(data)
      // 保存回读校验（docs/23 T14b，2026-10-29）：PUT 后 GET 逐键比对，覆盖
      // 「全平开关显示开、库里是 false」这类无感回滚——不一致就不报成功。
      // 回读失败（网络/超时）不等于保存失败：PUT 已 200，只是缺校验
      let fresh: SystemConfig | null = null
      try {
        fresh = await scanApi.getConfig()
      } catch {
        message.warning('配置已保存，但回读校验失败（网络异常），请稍后刷新确认')
      }
      if (fresh) {
        useScanStore.setState({ aiConfig: fresh })
        const diffs = diffConfig(data, fresh)
        if (diffs.length) {
          message.error(
            `保存未生效：${diffs.slice(0, 3).join('、')}`
            + `${diffs.length > 3 ? ` 等 ${diffs.length} 项` : ''}与提交值不一致`
            + '（表单已按后端实际值刷新，请复核后重试）',
          )
          return
        }
      }
      message.success('配置已保存')
      // 顶部熔断状态卡即时刷新（此前等 30s 轮询，开关切换后被误读为「保存了没生效」）
      await fetchStatus()
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
        width={1200}
      >
        <Spin spinning={loading}>
        <div style={{ maxHeight: '64vh', overflowY: 'auto', paddingInlineEnd: 4 }}>
        <Form form={form} layout="vertical" className="config-modal-form">
          <ConfigSection title="开仓与仓位" hint="在跑单数上限与开仓所需最低可用余额" first>
          <Row gutter={16}>
            <Col span={6}>
              <Form.Item
                label="同时在跑单子上限"
                name="max_open_trades"
                initialValue={5}
                rules={[{ required: true, message: '请输入在跑单子上限' }]}
                tooltip="自动开仓的在跑单子数量上限，0 = 不限单数（仅按开仓最低可用余额判断）；每小时任务先结算再开仓，评分高的优先"
              >
                <InputNumber min={0} max={50} style={{ width: '100%' }} />
              </Form.Item>
            </Col>
            <Col span={6}>
              <Form.Item
                label="开仓最低可用余额（%）"
                name="trading_min_free_pct"
                initialValue={50}
                rules={[{ required: true, message: '请输入开仓最低可用余额比例' }]}
                tooltip="可用余额低于总资金该比例时不开新单"
              >
                <InputNumber min={0} max={100} step={1} style={{ width: '100%' }} />
              </Form.Item>
            </Col>
          </Row>
          </ConfigSection>

          <ConfigSection title="止损与闸门" hint="止损宽度/强平缓冲硬闸门与单笔止损金额口径">
          <Row gutter={16}>
            <Col span={6}>
              <Form.Item
                label="止损宽度上限（%）"
                name="max_stop_pct"
                tooltip="开仓闸门的止损宽度硬上限（AI 复盘补丁参数 max_stop_pct 的手改入口，与补丁回滚共用同一列）"
              >
                <InputNumber min={0.1} max={10} step={0.1} style={{ width: '100%' }} placeholder="不限制" />
              </Form.Item>
            </Col>
            <Col span={6}>
              <Form.Item
                label="强平闸门缓冲（%）"
                name="liq_gate_buffer"
                tooltip="强平闸门 = 1/杠杆 − 维持保证金率 − 缓冲（覆盖手续费/资金费/触发价差；AI 复盘补丁参数 liq_gate_buffer 的手改入口）"
              >
                <InputNumber min={0.1} max={5} step={0.1} style={{ width: '100%' }} placeholder="0.5" />
              </Form.Item>
            </Col>
          </Row>

          {/* 止损金额模式（2026-09-27）：pct=现行风险基数分档级别；fixed=固定 USDT 金额 */}
          <Form.Item
            label="止损金额模式"
            name="risk_budget_mode"
            initialValue="pct"
            tooltip="单笔交易触发止损时的目标亏损金额。分档级别：风险基数（钱包余额首位取整，100~5000）× 3%，随钱包规模自动缩放；固定金额：每笔固定亏损指定 USDT，不随钱包变化"
          >
            <Radio.Group
              options={[
                { label: '分档级别（基数×3%）', value: 'pct' },
                { label: '固定金额', value: 'fixed' },
              ]}
              optionType="button"
              buttonStyle="solid"
            />
          </Form.Item>
          <Form.Item noStyle shouldUpdate={(p, c) => p.risk_budget_mode !== c.risk_budget_mode}>
            {({ getFieldValue }) =>
              getFieldValue('risk_budget_mode') === 'fixed' ? (
                <Form.Item
                  label="单笔止损金额（USDT）"
                  name="risk_fixed_amount"
                  initialValue={3}
                  rules={[{ required: true, message: '请输入固定止损金额' }]}
                  tooltip="固定金额模式下每笔交易触发止损的目标亏损额（仓位 = 金额 ÷ 止损距离）。默认 3 USDT，范围 0.1~1000"
                  style={{ marginTop: 0 }}
                >
                  <InputNumber min={0.1} max={1000} step={0.5} style={{ width: '100%' }} addonAfter="USDT" />
                </Form.Item>
              ) : null
            }
          </Form.Item>
          </ConfigSection>

          <ConfigSection title="杠杆与滑点" hint="动态杠杆下限、入场偏离容差与开仓滑点闸门">
          <Row gutter={16}>
            <Col span={6}>
              <Form.Item
                label="动态杠杆下限（×）"
                name="min_adapt_leverage"
                tooltip="止损距离超 20× 强平闸门时自动降杠杆开单；安全杠杆低于此下限的宽止损单直接放弃（限制层：不是所有单都动态算完就开）。8× 约等于止损距离上限 10.5%（小币）"
              >
                <InputNumber min={1} max={20} step={1} style={{ width: '100%' }} placeholder="8" precision={0} />
              </Form.Item>
            </Col>
            <Col span={6}>
              <Form.Item
                label="入场偏离容差（%）"
                name="pullback_market_pct"
                tooltip="开仓时现价对 AI 入场价的最大偏离。待回踩距离 ≤ 容差时市价入场（盈亏比按现价复算 ≥1.5 才放行），超容差维持待回踩不下单；普通单的开仓漂移上限同用此值"
              >
                <InputNumber min={0.1} max={5} step={0.1} style={{ width: '100%' }} placeholder="1" />
              </Form.Item>
            </Col>
            <Col span={6}>
              <Form.Item
                label="开仓滑点上限（%）"
                name="max_slippage_pct"
                tooltip="开仓前按本单数量走订单簿逐层吃，预期滑点超上限放弃开单（盘口过薄的小币会撑宽止损、实际风险超预算）。0=关闭滑点闸门"
              >
                <InputNumber min={0} max={5} step={0.05} style={{ width: '100%' }} placeholder="0.3" />
              </Form.Item>
            </Col>
          </Row>
          </ConfigSection>

          <ConfigSection title="风控参数" hint="留空 = 恢复默认，保存即生效">
          <Row gutter={16}>
            <Col span={6}>
              <Form.Item
                label="开单评分门槛"
                name="min_recommendation"
                tooltip="AI 推荐度 ≥ 该值才分发自动开仓（0~100）；连损降速在此基础上额外提档"
              >
                <InputNumber min={0} max={100} step={1} style={{ width: '100%' }} placeholder="60" precision={0} />
              </Form.Item>
            </Col>
            <Col span={6}>
              <Form.Item
                label="盈亏比铁律下限"
                name="ai_rr_min"
                tooltip="按 AI 给的三价复算 RR 必须 ≥ 该值，不足直接打回（AI 提示词同源更新）"
              >
                <InputNumber min={0.5} max={10} step={0.1} style={{ width: '100%' }} placeholder="1.5" />
              </Form.Item>
            </Col>
            <Col span={6}>
              <Form.Item
                label="RR 容差（R）"
                name="ai_rr_tol"
                tooltip="同一条容差三处共用：开仓现价复算闸、AI 三价 RR 铁律、止盈一距离上限。RR ≥ 铁律下限-容差 即放行，吞 tick 取整与报价漂移的擦边值（默认 0.02R），0=恢复严格铁律"
              >
                <InputNumber min={0} max={0.2} step={0.01} style={{ width: '100%' }} placeholder="0.02" />
              </Form.Item>
            </Col>
            <Col span={6}>
              <Form.Item
                label="单笔风险比例（%）"
                name="risk_pct"
                tooltip="分档级别止损金额模式下：单笔止损预算 = 风险基数 × 该比例（仓位 = 预算 ÷ 止损距离）；固定金额模式不使用此值"
              >
                <InputNumber min={0.1} max={20} step={0.1} style={{ width: '100%' }} placeholder="3" addonAfter="%" />
              </Form.Item>
            </Col>
            <Col span={6}>
              <Form.Item
                label="杠杆上限（×）"
                name="leverage_max"
                tooltip="逐仓杠杆上限；止损距离超该杠杆的强平闸门时动态下调，安全杠杆低于动态下限的宽止损单直接放弃"
              >
                <InputNumber min={1} max={125} step={1} style={{ width: '100%' }} placeholder="20" precision={0} />
              </Form.Item>
            </Col>
          </Row>
          <Row gutter={16}>
            <Col span={6}>
              <Form.Item
                label="taker 手续费率（%）"
                name="taker_fee_rate"
                tooltip="开平仓 taker 费率（0.05% = 0.0005 比例），TP1 止损移保本的复算口径；随交易所费率调整时手改"
              >
                <InputNumber min={0} max={0.5} step={0.005} style={{ width: '100%' }} placeholder="0.05" />
              </Form.Item>
            </Col>
            <Col span={6}>
              <Form.Item
                label="近锚风险折价系数"
                name="near_anchor_factor"
                tooltip="突破类信号用近锚止损时风险预算 × 该系数（>0 且 ≤1，收紧止损贴极值的单子预算）；1=不折价"
              >
                <InputNumber min={0.01} max={1} step={0.05} style={{ width: '100%' }} placeholder="0.7" />
              </Form.Item>
            </Col>
            <Col span={6}>
              <Form.Item
                label="当日亏损熔断开关"
                name="daily_loss_stop_enabled"
                tooltip="UTC 当日已平仓净亏达阈值停开新仓（AI 分析/巡检照常），顶部卡片「熔断状态」实时展示；触发后 UTC 0 点自动恢复"
              >
                <Select
                  options={[
                    { label: '开启', value: 'on' },
                    { label: '关闭', value: 'off' },
                  ]}
                />
              </Form.Item>
            </Col>
            <Col span={6}>
              <Form.Item
                label="当日亏损熔断阈值（%）"
                name="daily_loss_stop_pct"
                tooltip="已平仓净亏 ≤ -风险基数 × 该值 时触发熔断（9% ≈ 3 笔满损，随账户金额缩放）；0=关闭"
              >
                <InputNumber min={0} max={50} step={0.5} style={{ width: '100%' }} placeholder="9" addonAfter="%" />
              </Form.Item>
            </Col>
          </Row>
          <Row gutter={16}>
            <Col span={6}>
              <Form.Item
                label="组合热度上限（%）"
                name="portfolio_heat_cap"
                tooltip="在跑单止损合计 + 本单预估 > 钱包 × 该值 时拦截新单，限制同时承受止损的总金额（0 或留空 = 关闭）"
              >
                <InputNumber min={0} max={100} step={1} style={{ width: '100%' }} placeholder="0" addonAfter="%" />
              </Form.Item>
            </Col>
            <Col span={6}>
              <Form.Item
                label="同向持仓上限"
                name="same_direction_cap"
                tooltip="同方向在跑单数达上限拦截新单，防全市场同涨同跌时同向单集体同损（0 或留空 = 不限）"
              >
                <InputNumber min={0} max={20} step={1} style={{ width: '100%' }} placeholder="0" precision={0} />
              </Form.Item>
            </Col>
            <Col span={6}>
              <Form.Item
                label="BTC 急跌警示阈值（%）"
                name="btc_crash_pause_pct"
                tooltip="BTC 1h 近 3 根已收盘累计跌幅超该值时，AI 提示词追加急跌警示（2026-10-13 起仅供提示词参考，不再拦截开单）；0=关闭"
              >
                <InputNumber min={0} max={20} step={0.1} style={{ width: '100%' }} placeholder="1" addonAfter="%" />
              </Form.Item>
            </Col>
          </Row>
          <Row gutter={16}>
            <Col span={6}>
              <Form.Item
                label="周回撤减速阈值（%）"
                name="weekly_dd_stop_pct"
                tooltip="UTC 周一起已平仓净亏 ≤ -风险基数 × 该值 时，本周剩余时间单笔预算减半（不拦开仓，周一 0 点自动恢复）；日熔断之上的二级减速，只收紧亏损侧；0=关闭"
              >
                <InputNumber min={0} max={100} step={0.5} style={{ width: '100%' }} placeholder="15" addonAfter="%" />
              </Form.Item>
            </Col>
            <Col span={6}>
              <Form.Item
                label="月回撤熔断阈值（%）"
                name="monthly_dd_stop_pct"
                tooltip="UTC 月起已平仓净亏 ≤ -风险基数 × 该值 时停开新仓，需在顶部熔断卡片「确认复盘」人工确认后恢复（本月内有效）；周熔断之上的三级总闸；0=关闭"
              >
                <InputNumber min={0} max={100} step={0.5} style={{ width: '100%' }} placeholder="25" addonAfter="%" />
              </Form.Item>
            </Col>
            <Col span={6}>
              <Form.Item
                label="ATR 跟进倍数"
                name="atr_trail_k"
                tooltip="跟进止损加 ATR 腿（Chandelier）：多单 SL=max(现SL, 持仓期最高价−k×ATR22)，空单对称，与摆动低点跟进取更紧者（只收紧不放松）；0=关闭 ATR 腿"
              >
                <InputNumber min={0} max={20} step={0.5} style={{ width: '100%' }} placeholder="3" />
              </Form.Item>
            </Col>
          </Row>
          </ConfigSection>

          <ConfigSection title="止盈与分批" hint="TP2 分批开关与两档止盈数量比例（留空 = 恢复默认）">
          <Row gutter={16}>
            <Col span={6}>
              <Form.Item
                label="TP2 分批止盈"
                name="tp2_enabled"
                valuePropName="checked"
                initialValue={false}
                tooltip="默认关闭：AI 报出的 TP2 不挂档，出场走「TP1 一次止盈 80% + 剩余 20% 跟进」。开启后：有 TP2 的单按 TP1 60% / TP2 20% / 剩余 20% 跟进分批（比例可在下方三项配置）；手动接管单不受此开关限制；止盈模式为「纯跟踪」的单不挂任何止盈档，此开关不适用"
              >
                <Switch checkedChildren="启用" unCheckedChildren="关闭" />
              </Form.Item>
            </Col>
            <Col span={6}>
              <Form.Item
                label="止盈全平模式"
                name="tp_full_exit_enabled"
                valuePropName="checked"
                initialValue={false}
                tooltip="默认关闭：分批止盈后剩余尾仓由跟进止损退出（即现有行为）。开启后不再留跟进尾仓——有 TP2 的单：TP1 按上方比例减仓，TP2 一次平掉剩余全部；无 TP2 的单：TP1 一次平掉 100% 仓位（此时 TP2 比例与「无 TP2 止盈比例」不再生效）。仅对开关开启后新开的单生效，已开单按开仓时口径管理"
              >
                <Switch checkedChildren="启用" unCheckedChildren="关闭" />
              </Form.Item>
            </Col>
          </Row>
          <Row gutter={16}>
            <Col span={6}>
              <Form.Item
                label="TP1 分批比例（有 TP2）"
                name="tp1_qty_pct_with_tp2"
                tooltip="有 TP2 的单 TP1 档止盈数量占开仓量的比例（%），剩余 = 100 − TP1 − TP2 由跟进止损退出；两档合计需 ≤95%"
              >
                <InputNumber min={1} max={99} step={1} style={{ width: '100%' }} placeholder="60" addonAfter="%" />
              </Form.Item>
            </Col>
            <Col span={6}>
              <Form.Item
                label="TP2 分批比例"
                name="tp2_qty_pct"
                tooltip="有 TP2 的单 TP2 档止盈数量占开仓量的比例（%）；两档合计需 ≤95%（剩余 ≥5% 跟进）"
              >
                <InputNumber min={1} max={99} step={1} style={{ width: '100%' }} placeholder="20" addonAfter="%" />
              </Form.Item>
            </Col>
            <Col span={6}>
              <Form.Item
                label="TP1 止盈比例（无 TP2）"
                name="tp1_qty_pct_no_tp2"
                tooltip="无 TP2 的单 TP1 一次止盈数量占开仓量的比例（%），剩余由跟进止损退出；需 ≤95%"
              >
                <InputNumber min={1} max={99} step={1} style={{ width: '100%' }} placeholder="80" addonAfter="%" />
              </Form.Item>
            </Col>
          </Row>
          </ConfigSection>
        </Form>
        </div>
        </Spin>
      </Modal>

      {/* 存量单提示（docs/23 T14c，2026-10-29）：开启全平模式不会补平在跑单，
          它们按开仓时快照（raw.tp_full_exit）继续分批 + 跟进止损 */}
      <Modal
        open={fullExitConfirm}
        title="开启止盈全平模式：现有持仓如何处理？"
        onCancel={() => setFullExitConfirm(false)}
        okText="确认开启并保存"
        cancelText="取消"
        width={780}
        confirmLoading={saving}
        onOk={async () => {
          confirmFullExitRef.current = true
          setFullExitConfirm(false)
          await handleSave()
        }}
      >
        <Alert
          type="warning"
          showIcon
          style={{ marginBottom: 12 }}
          message={`当前有 ${runningTrades.length} 张在跑单，开启后不会被补平`}
          description="全平模式按「开仓时的开关快照」管理仓位，已开的单继续按原口径退出：TP1 减仓后，剩余尾仓仍由跟进止损退出。本次开启只影响之后新开的单。"
        />
        <Table
          rowKey="id"
          size="small"
          pagination={false}
          dataSource={runningTrades}
          scroll={{ y: 260 }}
          columns={[
            { title: '币种', dataIndex: 'symbol', width: 130 },
            {
              title: '方向',
              dataIndex: 'direction',
              width: 70,
              render: (v: string) => (
                <Tag color={v === 'long' ? 'green' : 'red'} style={{ marginInlineEnd: 0 }}>
                  {v === 'long' ? '多' : '空'}
                </Tag>
              ),
            },
            {
              title: '状态',
              dataIndex: 'status',
              width: 100,
              render: (v: string) => (
                { OPENED: '运行中', TP1_HIT: 'TP1已止盈', TP2_HIT: 'TP2已止盈' }[v] ?? v
              ),
            },
            {
              title: '入场价',
              dataIndex: 'entry_price',
              width: 110,
              render: (v: number | null) => (v == null ? '-' : v),
            },
            {
              title: '开仓时间',
              dataIndex: 'opened_at',
              width: 150,
              render: (v: string | null) => (v ? bj(v).format('MM-DD HH:mm') : '-'),
            },
            {
              title: '口径',
              key: 'mode',
              render: () => <span style={{ color: '#8c8c8c' }}>按开仓时快照（不受本次影响）</span>,
            },
          ]}
        />
        <div style={{ marginTop: 10, fontSize: 12, color: '#8c8c8c' }}>
          想立刻按全平口径处理这些单，请到「交易记录」手动平仓后重开。
        </div>
      </Modal>
    </>
  )
}
