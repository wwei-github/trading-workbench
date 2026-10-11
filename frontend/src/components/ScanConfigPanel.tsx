import { useState, useEffect } from 'react'
import { Modal, Form, InputNumber, Select, Switch, Checkbox, message, Button, Spin, Divider, Tooltip, Row, Col } from 'antd'
import { SettingOutlined, RobotOutlined, FileTextOutlined, ApiOutlined, UndoOutlined } from '@ant-design/icons'
import { useScanStore } from '../stores/scanStore'
import type { SystemConfig } from '../types'

/** 触发类型白名单选项（与后端 ALL_TRIGGERS 一一对应；纯金K 2026-09-29 下线，
 *  金K必须与触点配合，历史行标签保留在 labels.ts） */
const TRIGGER_OPTIONS = [
  { value: 'trigger_breakout', label: '结构突破' },
  { value: 'trigger_trend_breakout', label: '趋势突破' },
  { value: 'trigger_pullback_confirm', label: '回踩金K确认' },
  { value: 'trigger_trend_pullback', label: '趋势回踩金K确认（4H闸门+1H结构）' },
]
const ALL_TRIGGER_VALUES = TRIGGER_OPTIONS.map((o) => o.value)

/** 回踩触点白名单选项（与后端 ALL_TOUCH_REFS 同源顺序；docs/22 触点体系。
 *  金K必须与触点配合（当根/次根确认），此白名单决定允许金K出现的位置。
 *  ema144 已退役（2026-10-10 三次拍板：深回撤存活率极低，后端强制过滤不再展示） */
const TOUCH_REF_OPTIONS = [
  { value: 'retest', label: '突破回测位' },
  { value: 'swing', label: '摆动结构位' },
  { value: 'ema21', label: 'EMA21' },
  { value: 'ema21_4h', label: 'EMA21(4h)' },
  { value: 'ema55', label: 'EMA55' },
]
/** 默认均线族（2026-10-08，2026-10-09 加入 4h EMA21，2026-10-10 ema144 退役）：横向结构触点（retest/swing）默认关 */
const DEFAULT_TOUCH_VALUES = ['ema21', 'ema21_4h', 'ema55']

/** 筛选参数组（2026-09-28 筛选配置上页面）：字段元数据驱动渲染 */
const FILTER_FIELDS: { name: string; label: string; min: number; max: number; step?: number; tooltip: string }[] = [
  { name: 'min_volume_24h', label: '24h成交额门槛（USDT）', min: 0, max: 1e9, step: 100000,
    tooltip: '扫描候选池流动性门槛，低于此不进扫描；0=不过滤' },
  { name: 'new_coin_cooldown_days', label: '新币冷静期（天）', min: 0, max: 90,
    tooltip: '上市未满 N 天的合约不进扫描：新币盘口深度极不稳定，开仓滑点达标≠止损出场不滑爆（NIGHT 案例：出场滑点 1.98%，实亏达计划风险 2.34 倍）；0=关闭' },
  { name: 'breakout_threshold', label: '最小突破幅度（比例）', min: 0, max: 0.05, step: 0.001,
    tooltip: '收盘须越过突破位（结构前高/前低或 Donchian 轨）此幅度才算突破（0.002=0.2%），毛边突破不算；0=关闭' },
  { name: 'breakout_vol_ratio', label: '突破量比门槛', min: 0, max: 10, step: 0.1,
    tooltip: '信号K线成交量 ≥ 近20根均量的此倍数才确认放量突破' },
  { name: 'breakout_max_pct', label: '突破幅度上限（%）', min: 0, max: 20, step: 0.5,
    tooltip: '越过突破位超过此幅度视为追高，不产生突破触发（正式网复盘：大幅突破追入负期望）；0=关闭' },
  { name: 'breakout_channel_bars', label: 'Donchian通道根数', min: 5, max: 200,
    tooltip: '趋势突破触发（trigger_trend_breakout）的回看窗口根数（收盘破 N 根高低轨；结构突破不受此影响）' },
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
  // 打开弹窗时的筛选参数生效值快照：保存时逐键比对，仅提交有改动的键
  // （后端缺键回落 env，避免把 env 派生值固化进 DB）
  const [filtersSnapshot, setFiltersSnapshot] = useState<Record<string, unknown>>({})
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
        allowed_triggers: eff.allowed_triggers ?? ALL_TRIGGER_VALUES,
        pullback_touch_refs: eff.pullback_touch_refs ?? DEFAULT_TOUCH_VALUES,
        structure_1h_gate: eff.structure_1h_gate ?? true,
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
      // 筛选参数组：逐键比对快照，仅提交有改动的键（后端缺键回落 env）；
      // 未动则完全不提交 scan_filters
      const scanFilters: Record<string, number | boolean | string[]> = {}
      for (const f of FILTER_FIELDS) {
        if (Number(values[f.name]) !== Number(filtersSnapshot[f.name])) {
          scanFilters[f.name] = values[f.name]
        }
      }
      const curTriggers: string[] = values.allowed_triggers || []
      const snapTriggers: string[] =
        (filtersSnapshot.allowed_triggers as string[]) || ALL_TRIGGER_VALUES
      if (curTriggers.length !== snapTriggers.length
        || curTriggers.some((t) => !snapTriggers.includes(t))) {
        scanFilters.allowed_triggers = curTriggers
      }
      // 回踩触点白名单：同 allowed_triggers 比对提交（后端 _touch_refs 缺键回落默认）
      const curRefs: string[] = values.pullback_touch_refs || []
      const snapRefs: string[] =
        (filtersSnapshot.pullback_touch_refs as string[]) || DEFAULT_TOUCH_VALUES
      if (curRefs.length !== snapRefs.length
        || curRefs.some((t) => !snapRefs.includes(t))) {
        scanFilters.pullback_touch_refs = curRefs
      }
      // 1h 结构门开关（2026-10-11）：布尔逐键比对提交（后端缺键回落 env）
      if (typeof values.structure_1h_gate === 'boolean'
        && values.structure_1h_gate !== filtersSnapshot.structure_1h_gate) {
        scanFilters.structure_1h_gate = values.structure_1h_gate
      }
      if (Object.keys(scanFilters).length > 0) data.scan_filters = scanFilters
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
      form.setFieldsValue({
        ...FILTER_FIELDS.reduce((acc, f) => ({ ...acc, [f.name]: eff[f.name] }), {}),
        allowed_triggers: eff.allowed_triggers ?? ALL_TRIGGER_VALUES,
        pullback_touch_refs: eff.pullback_touch_refs ?? DEFAULT_TOUCH_VALUES,
        structure_1h_gate: eff.structure_1h_gate ?? true,
      })
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
        width={1200}
      >
        <Spin spinning={loading}>
        <Form form={form} layout="vertical">
          <Row gutter={16}>
            <Col span={6}>
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
            </Col>
            <Col span={6}>
              <Form.Item
                label="K线数量"
                name="kline_window"
                rules={[{ required: true }]}
                tooltip="每次扫描获取的K线根数"
              >
                <InputNumber min={30} max={1500} style={{ width: '100%' }} />
              </Form.Item>
            </Col>
            <Col span={6}>
              <Form.Item
                label="重复命中窗口（小时）"
                name="repeat_window_hours"
                rules={[{ required: true }]}
                tooltip="在此时间窗口内的命中视为重复"
              >
                <InputNumber min={1} max={168} style={{ width: '100%' }} />
              </Form.Item>
            </Col>
            <Col span={6}>
              <Form.Item
                label="摆动点阶数"
                name="swing_order"
                rules={[{ required: true }]}
                tooltip="AI 事实包中摆动点确认的左右比较根数"
              >
                <InputNumber min={1} max={10} style={{ width: '100%' }} />
              </Form.Item>
            </Col>
          </Row>

          {/* 筛选参数组（2026-09-28 筛选配置上页面）：DB > env > 代码默认；四列排布 */}
          <Divider style={{ margin: '8px 0 16px' }}>
            <span style={{ fontSize: 13, color: '#999' }}>筛选参数</span>
          </Divider>
          {FILTER_FIELDS.reduce<typeof FILTER_FIELDS[]>((rows, f, i) => {
            if (i % 4 === 0) rows.push([f])
            else rows[rows.length - 1].push(f)
            return rows
          }, []).map((row, ri) => (
            <Row gutter={16} key={ri}>
              {row.map((f) => (
                <Col span={6} key={f.name}>
                  <Form.Item
                    label={f.label}
                    name={f.name}
                    rules={[{ required: true, message: `请填写${f.label}` }]}
                    tooltip={f.tooltip}
                  >
                    <InputNumber min={f.min} max={f.max} step={f.step} style={{ width: '100%' }} />
                  </Form.Item>
                </Col>
              ))}
            </Row>
          ))}
          <Form.Item
            label="触发类型白名单"
            name="allowed_triggers"
            tooltip="只产生勾选类型的信号（其余类型直接不进候选/AI 分析）；全部不勾视为配置错误，后端按全选处理"
          >
            <Checkbox.Group options={TRIGGER_OPTIONS} />
          </Form.Item>
          <Form.Item
            label="回踩触点白名单（金K允许出现的位置）"
            name="pullback_touch_refs"
            tooltip="回踩金K确认触发只认勾选的触点：触碰后当根/次根出现同向金K才触发。默认仅三条均线（EMA21/EMA21(4h)/EMA55；ema144 已退役；EMA55 另受排列+分离史结构门自筛）；横向结构触点（突破回测位/摆动结构位）语义强但依赖破轨/摆动质量，按需勾选。全部不勾回落默认三条均线"
          >
            <Checkbox.Group options={TOUCH_REF_OPTIONS} />
          </Form.Item>
          <Form.Item
            label="1h 结构门（三线排列 + 快中线发散史 + EMA144 方向）"
            name="structure_1h_gate"
            valuePropName="checked"
            tooltip="开启：排列/发散/144 方向任一不成立即否决（默认硬门）。关闭：整体跳过该判定，信号量换质量的应急开关——仅 4h 闸门+指标多数决+震荡评分+触发确认仍生效；关闭期间产生的信号在复盘按 RULE_VERSION/配置时段分组对比"
          >
            <Switch />
          </Form.Item>
          <Button size="small" icon={<UndoOutlined />} onClick={handleResetFilters} loading={saving}>
            恢复默认
          </Button>

          {/* AI 分析开关区（原工具栏快捷开关，2026-09-20 收敛至此）；四列排布 */}
          <Divider style={{ margin: '8px 0 16px' }}>
            <span style={{ fontSize: 13, color: '#999' }}>AI 分析</span>
          </Divider>
          <Row gutter={16}>
            <Col span={6}>
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
            </Col>
            <Col span={6}>
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
            </Col>
            <Col span={6}>
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
            </Col>
            <Col span={6}>
              <Form.Item
                label="双评委辩论"
                name="dual_judge_enabled"
                valuePropName="checked"
                tooltip="对'建议开单'的决策做多空辩论复核，裁判可否决（额外LLM调用，仅Agent/单次管线的suggest决策生效）"
              >
                <Switch />
              </Form.Item>
            </Col>
          </Row>
        </Form>
        </Spin>
      </Modal>
    </>
  )
}
