import { useState, useEffect } from 'react'
import { Modal, Form, Input, InputNumber, Switch, Checkbox, message, Button, Spin, Tooltip, Row, Col, Select } from 'antd'
import { SettingOutlined, RobotOutlined, FileTextOutlined, UndoOutlined } from '@ant-design/icons'
import { useScanStore } from '../stores/scanStore'
import ConfigSection from './ConfigSection'
import type { SystemConfig } from '../types'
import { bj } from '../utils/dayjs'
import { scanApi } from '../api/scan'
import { diffConfig } from '../utils/configVerify'

/** 触发类型白名单选项（与后端 ALL_TRIGGERS 一一对应；纯金K 2026-09-29 下线，
 *  金K必须与触点配合，历史行标签保留在 labels.ts） */
const TRIGGER_OPTIONS = [
  { value: 'trigger_breakout', label: '结构突破' },
  { value: 'trigger_trend_breakout', label: '趋势突破' },
  { value: 'trigger_pullback_confirm', label: '回踩金K确认' },
]
const ALL_TRIGGER_VALUES = TRIGGER_OPTIONS.map((o) => o.value)

/** 回踩触点白名单选项（与后端 ALL_TOUCH_REFS 同源顺序；docs/22 触点体系。
 *  金K必须与触点配合（当根/次根确认），此白名单决定允许金K出现的位置。
 *  ema144 于 2026-10-16 复出（改判门控口径「1h EMA144 走平或顺 4h 方向」后默认启用） */
const TOUCH_REF_OPTIONS = [
  { value: 'retest', label: '突破回测位' },
  { value: 'swing', label: '摆动结构位' },
  { value: 'ema21', label: 'EMA21' },
  { value: 'ema21_4h', label: 'EMA21(4h)' },
  { value: 'ema55', label: 'EMA55' },
  { value: 'ema144', label: 'EMA144' },
]
/** 默认触点 = 回测位 + 摆动结构位 + 均线族（2026-10-08 均线族，2026-10-09 加入 4h EMA21，
 *  2026-10-14 加入 swing，2026-10-16 ema144 复出，2026-10-17 retest 默认启用〔docs/23 A1〕）——
 *  与后端 DEFAULT_TOUCH_REFS 对齐，避免前端默认把 swing 静默写回旧列表（2026-10-14） */
const DEFAULT_TOUCH_VALUES = ['retest', 'swing', 'ema21', 'ema21_4h', 'ema55', 'ema144']

/** 回踩类触发类型（2026-10-12 trend_pullback 并入）：仅此类会消费
 *  pullback_touch_refs，用于联动置灰触点白名单 */
const PULLBACK_TRIGGER_VALUES = ['trigger_pullback_confirm']

/** 筛选参数组（2026-09-28 筛选配置上页面）：字段元数据驱动渲染 */
const FILTER_FIELDS: { name: string; label: string; min: number; max: number; step?: number; tooltip: string }[] = [
  { name: 'min_volume_24h', label: '24h成交额门槛（USDT）', min: 0, max: 1e9, step: 100000,
    tooltip: '扫描候选池流动性门槛，低于此不进扫描；0=不过滤' },
  { name: 'new_coin_cooldown_days', label: '新币冷静期（天）', min: 0, max: 90,
    tooltip: '上市未满 N 天的合约不进扫描：新币盘口深度极不稳定，开仓滑点达标≠止损出场不滑爆（NIGHT 案例：出场滑点 1.98%，实亏达计划风险 2.34 倍）；0=关闭' },
  { name: 'breakout_threshold', label: '最小突破幅度（比例）', min: 0, max: 0.05, step: 0.001,
    tooltip: '收盘须越过突破位（结构前高/前低或 Donchian 轨）此幅度才算突破（0.002=0.2%），毛边突破不算；0=关闭' },
  { name: 'breakout_max_pct', label: '突破幅度上限（%）', min: 0, max: 20, step: 0.5,
    tooltip: '越过突破位超过此幅度视为追高，不产生突破触发（正式网复盘：大幅突破追入负期望）；0=关闭' },
  { name: 'breakout_channel_bars', label: 'Donchian通道根数', min: 5, max: 200,
    tooltip: '趋势突破触发（trigger_trend_breakout）的回看窗口根数（收盘破 N 根高低轨；结构突破不受此影响）' },
  { name: 'ai_max_per_scan', label: 'AI单轮分析上限（个）', min: 1, max: 50,
    tooltip: '单次扫描批量 AI 分析的币种上限，按质量分/24h成交额取前N（控制 LLM 成本）' },
  { name: 'quality_floor', label: '质量分下限（0=不过滤）', min: 0, max: 110,
    tooltip: '候选命中质量分低于该值的不进候选池（不进 AI 分析、不可开单）；'
      + '正式网 14 天已平仓样本：<80 两档均为负（-0.05R/笔左右），80-89 +0.26R、90+ +0.22R，'
      + '分水岭在 80。只影响是否成为候选，pending 与命中历史照常记录' },
]

/** AI 分析暂停时段的小时选项（整点，00:00 ~ 23:00） */
const HOUR_OPTIONS = Array.from({ length: 24 }, (_, h) => ({
  value: h,
  label: `${String(h).padStart(2, '0')}:00`,
}))

/** 策略配置弹窗：扫描策略 + AI 分析/策略开关。
 *  （2026-09-20 工具栏的 AI/策略快捷开关收敛至此；交易参数见
 *  TradingConfigPanel，环境/自动交易开关见 SystemConfigPanel） */
export default function ScanConfigPanel() {
  const [open, setOpen] = useState(false)
  const [form] = Form.useForm()
  const [saving, setSaving] = useState(false)
  const [loading, setLoading] = useState(false)
  // 打开弹窗时的筛选参数生效值快照：保存时逐键比对，仅提交有改动的键
  // （后端 PUT 对 scan_filters 做键级合并——提交键覆盖、未提交键保留库内旧值，
  //  故不会把 env 派生值固化进 DB，也不会丢掉先前定制的键）
  const [filtersSnapshot, setFiltersSnapshot] = useState<Record<string, unknown>>({})
  // AI API Key 编辑标记（2026-10-26）：接口不回传明文，输入框默认留空。
  // 只有用户实际动过该输入才提交（留空不修改库内已存 Key；手动清空=清除回退 env）
  const [apiKeyTouched, setApiKeyTouched] = useState(false)
  const { aiConfig, updateConfig, fetchConfig } = useScanStore()

  // 触发类型白名单实时值 → 驱动回踩触点白名单的置灰（未勾选任何回踩类时该白名单不生效）
  const watchedTriggers: string[] | undefined = Form.useWatch('allowed_triggers', form)
  const pullbackEnabled = (watchedTriggers ?? ALL_TRIGGER_VALUES)
    .some((t) => PULLBACK_TRIGGER_VALUES.includes(t))
  // AI 分析暂停开关 → 驱动起止整点下拉的置灰
  const pauseEnabled: boolean | undefined = Form.useWatch('ai_pause_enabled', form)

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
        // 库内历史值可能低于扫描取数下限（旧默认 240），按实际生效口径展示为 300
        kline_window: Math.max(300, aiConfig.kline_window ?? 300),
        repeat_window_hours: aiConfig.repeat_window_hours,
        swing_order: aiConfig.swing_order,
        ...FILTER_FIELDS.reduce((acc, f) => ({ ...acc, [f.name]: eff[f.name] }), {}),
        allowed_triggers: eff.allowed_triggers ?? ALL_TRIGGER_VALUES,
        pullback_touch_refs: eff.pullback_touch_refs ?? DEFAULT_TOUCH_VALUES,
        direction_vote_gate_enabled: eff.direction_vote_gate_enabled ?? true,
        direction_conflict_gate_enabled: eff.direction_conflict_gate_enabled ?? true,
        // 回踩前置反向腿门（只读展示，不参与提交）
        pullback_leg_gate_enabled: eff.pullback_leg_gate_enabled ?? true,
        pullback_leg_lookback_bars: eff.pullback_leg_lookback_bars ?? 24,
        pullback_leg_min_bars: eff.pullback_leg_min_bars ?? 2,
        pullback_leg_min_atr: eff.pullback_leg_min_atr ?? 0.5,
        ai_analysis_enabled: aiConfig.ai_analysis_enabled ?? false,
        // 暂停时段：库内 NULL 时给默认 00:00-08:00（仅表单初值，开关关闭不生效）
        ai_pause_enabled: aiConfig.ai_pause_enabled ?? false,
        ai_pause_start_hour: aiConfig.ai_pause_start_hour ?? 0,
        ai_pause_end_hour: aiConfig.ai_pause_end_hour ?? 8,
        strategy_prompt_enabled: aiConfig.strategy_prompt_enabled ?? false,
        // AI 运行时参数：库内原始值（null=未定制，留空即回落 env 默认）
        ai_model: aiConfig.ai_model ?? undefined,
        // API Key 明文永不回传：字段恒为空，占位显示已配置/未配置状态
        ai_api_key: undefined,
        ai_base_url: aiConfig.ai_base_url ?? undefined,
        llm_concurrency: aiConfig.llm_concurrency ?? undefined,
        ai_call_timeout_s: aiConfig.ai_call_timeout_s ?? undefined,
      })
      setApiKeyTouched(false)
    }
  }, [open, aiConfig, form])

  const aiConfigured = !!aiConfig?.ai_configured
  const hasStrategy = !!aiConfig?.strategy_prompt
  // AI 运行时参数生效值（作占位/提示：留空=回落 env 默认）
  const aiModelEff = aiConfig?.ai_model_effective ?? ''
  const aiBaseUrlEff = aiConfig?.ai_base_url_effective ?? ''
  const aiConcurrencyEff = aiConfig?.llm_concurrency_effective ?? 0
  const aiTimeoutEff = aiConfig?.ai_call_timeout_s_effective ?? 0
  // 凭证对状态（2026-10-26）：Key 与端点成对配置才用页面值，缺一整体回落 .env
  const aiApiKeySet = !!aiConfig?.ai_api_key_set
  const aiApiKeyMasked = aiConfig?.ai_api_key_masked ?? ''
  const aiBaseUrlSet = !!(aiConfig?.ai_base_url ?? '').trim()
  const aiPairSource = aiConfig?.ai_api_pair_source ?? 'env'
  const aiApiHint = aiPairSource === 'db'
    ? '当前生效：弹窗配置的 Key + 端点'
    : (aiApiKeySet || aiBaseUrlSet)
      ? 'Key 与端点未成对配置 → 当前整对使用 .env 默认'
      : '当前生效：后端 .env 默认'

  const handleSave = async () => {
    try {
      const values = await form.validateFields()
      // 暂停时段开启时起止整点不能相同（否则是空区间/全天二义，后端同样 400）
      if (values.ai_pause_enabled
        && values.ai_pause_start_hour === values.ai_pause_end_hour) {
        message.error('暂停时段的开始与结束不能相同')
        return
      }
      setSaving(true)
      const data: Partial<SystemConfig> = {
        // 周期固定 1h（整点小时级调度）；保存时归一化，兼容库内历史非 1h 值
        kline_interval: '1h',
        kline_window: values.kline_window,
        repeat_window_hours: values.repeat_window_hours,
        swing_order: values.swing_order,
        ai_analysis_enabled: values.ai_analysis_enabled,
        // AI 分析暂停时段（北京时间整点，[开始, 结束)；开始>结束 表示跨零点）
        ai_pause_enabled: values.ai_pause_enabled ?? false,
        ai_pause_start_hour: values.ai_pause_start_hour,
        ai_pause_end_hour: values.ai_pause_end_hour,
        strategy_prompt_enabled: values.strategy_prompt_enabled,
        // AI 运行时参数：空串/未填 → null（后端回落 env 默认）
        ai_model: (values.ai_model ?? '').trim() || null,
        ai_base_url: (values.ai_base_url ?? '').trim() || null,
        llm_concurrency: values.llm_concurrency ?? null,
        ai_call_timeout_s: values.ai_call_timeout_s ?? null,
      }
      // API Key 仅在本次实际编辑过才提交（未动=保留库内原值；清空=清除回落 env）
      if (apiKeyTouched) {
        data.ai_api_key = (values.ai_api_key ?? '').trim() || null
      }
      // 筛选参数组：逐键比对快照，仅提交有改动的键（后端键级合并：未提交键保留
      // 库内旧值，故不会把 env 派生值固化进 DB、也不会丢先前定制键）；
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
      // 方向过滤开关（2026-10-18）：布尔键同样仅提交有改动的项（未动则不提交该键，
      // 后端键级合并保留库内旧值）
      const curVoteGate = values.direction_vote_gate_enabled ?? true
      if (curVoteGate !== (filtersSnapshot.direction_vote_gate_enabled ?? true)) {
        scanFilters.direction_vote_gate_enabled = curVoteGate
      }
      const curConflictGate = values.direction_conflict_gate_enabled ?? true
      if (curConflictGate !== (filtersSnapshot.direction_conflict_gate_enabled ?? true)) {
        scanFilters.direction_conflict_gate_enabled = curConflictGate
      }
      // 1h 结构门开关（2026-10-11，2026-10-12 退役）：已从配置弹窗移除，
      // 不再提交该键（后端 scan_filters 保留兼容键位，仅读旧值不做消费）
      if (Object.keys(scanFilters).length > 0) data.scan_filters = scanFilters
      await updateConfig(data)
      // 保存回读校验（docs/23 T14b，2026-10-29）：PUT 后立即 GET 比对，别只报
      // 「已保存」——07:21 全平开关事件就是页面说成功、库里其实被改回 false。
      // 回读本身失败（网络/超时）不等于保存失败：PUT 已 200，只是缺校验
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
        direction_vote_gate_enabled: eff.direction_vote_gate_enabled ?? true,
        direction_conflict_gate_enabled: eff.direction_conflict_gate_enabled ?? true,
        pullback_leg_gate_enabled: eff.pullback_leg_gate_enabled ?? true,
        pullback_leg_lookback_bars: eff.pullback_leg_lookback_bars ?? 24,
        pullback_leg_min_bars: eff.pullback_leg_min_bars ?? 2,
        pullback_leg_min_atr: eff.pullback_leg_min_atr ?? 0.5,
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
        <div style={{ maxHeight: '64vh', overflowY: 'auto', paddingInlineEnd: 4 }}>
        <Form form={form} layout="vertical" className="config-modal-form">
          <ConfigSection title="扫描基础" hint="K线周期固定 1 小时；K线根数、重复命中窗口与摆动点阶数" first>
          <Row gutter={16}>
            <Col span={8}>
              <Form.Item
                label="K线数量"
                name="kline_window"
                rules={[{ required: true }]}
                tooltip="每次扫描获取的K线根数；后端扫描取数下限为 300 根，低于 300 按 300 取"
              >
                <InputNumber min={300} max={1500} style={{ width: '100%' }} />
              </Form.Item>
            </Col>
            <Col span={8}>
              <Form.Item
                label="重复命中窗口（小时）"
                name="repeat_window_hours"
                rules={[{ required: true }]}
                tooltip="在此时间窗口内的命中视为重复"
              >
                <InputNumber min={1} max={168} style={{ width: '100%' }} />
              </Form.Item>
            </Col>
            <Col span={8}>
              <Form.Item
                label="摆动点阶数"
                name="swing_order"
                rules={[{ required: true }]}
                tooltip="摆动点确认的左右比较根数（触发层结构位/突破、AI 事实包、K线标注共用）"
              >
                <InputNumber min={1} max={10} style={{ width: '100%' }} />
              </Form.Item>
            </Col>
          </Row>
          </ConfigSection>

          {/* 筛选参数组（2026-09-28 筛选配置上页面）：DB > env > 代码默认；四列排布 */}
          <ConfigSection title="筛选参数" hint="DB > env > 代码默认；每项含义见字段悬停提示">
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
          </ConfigSection>

          <ConfigSection title="触发与触点" hint="决定哪些信号进入候选与 AI 分析；「恢复默认」清空全部筛选定制">
          <Form.Item
            label="触发类型白名单"
            name="allowed_triggers"
            tooltip="只产生勾选类型的信号（其余类型直接不进候选/AI 分析）；全部不勾视为配置错误，后端按全选处理"
          >
            <Checkbox.Group options={TRIGGER_OPTIONS} />
          </Form.Item>
          <Form.Item
            label={
              <span style={pullbackEnabled ? undefined : { color: '#999' }}>
                回踩触点白名单（金K允许出现的位置）
              </span>
            }
            name="pullback_touch_refs"
            extra={!pullbackEnabled && (
              <span style={{ color: '#999' }}>
                当前「触发类型白名单」未勾选「回踩金K确认」，触点白名单不生效
              </span>
            )}
            tooltip="回踩金K确认只认勾选的触点：触碰后当根/次根出现同向金K才触发。默认六触点全开（突破回测位/摆动结构位/EMA21/EMA21(4h)/EMA55/EMA144；EMA144 已复出并默认启用）。EMA21 的 1H 结构门（三线排列 + 快中线发散史 + EMA144 方向）自 2026-10-15 起不再否决，未过门仅质量分 -15；其余触点不受结构门约束。所有触点命中后还须通过回踩前置反向腿门（scan_filters 内配置）。全部不勾回落默认六触点"
          >
            <Checkbox.Group options={TOUCH_REF_OPTIONS} disabled={!pullbackEnabled} />
          </Form.Item>
          <Button size="small" icon={<UndoOutlined />} onClick={handleResetFilters} loading={saving} style={{ marginBottom: 16 }}>
            恢复默认
          </Button>
          </ConfigSection>

          {/* 回踩前置反向腿门（2026-10-22 上线）：后端已生效参数，仅展示、禁止在弹窗内修改 */}
          <ConfigSection title="回踩反向腿门（只读）" hint="后端策略参数，已生效；仅供查看，弹窗内不可修改">
          <Row gutter={16}>
            <Col span={6}>
              <Form.Item
                label="反向腿门"
                name="pullback_leg_gate_enabled"
                valuePropName="checked"
                tooltip="回踩前必须有 1 小时级别的同向反转腿；关闭后回踩只认触点 + 金K。只读"
              >
                <Switch disabled checkedChildren="开" unCheckedChildren="关" />
              </Form.Item>
            </Col>
            <Col span={6}>
              <Form.Item
                label="腿回看根数"
                name="pullback_leg_lookback_bars"
                tooltip="反向腿搜索回看根数（1h）。只读"
              >
                <InputNumber disabled min={2} max={200} style={{ width: '100%' }} />
              </Form.Item>
            </Col>
            <Col span={6}>
              <Form.Item
                label="腿最少根数"
                name="pullback_leg_min_bars"
                tooltip="反向腿最少持续根数。只读"
              >
                <InputNumber disabled min={0} max={50} style={{ width: '100%' }} />
              </Form.Item>
            </Col>
            <Col span={6}>
              <Form.Item
                label="腿最小幅度（ATR）"
                name="pullback_leg_min_atr"
                tooltip="反向腿最小幅度（ATR 倍数，0=不限制）。只读"
              >
                <InputNumber disabled min={0} max={20} step={0.1} style={{ width: '100%' }} />
              </Form.Item>
            </Col>
          </Row>
          </ConfigSection>

          {/* 方向过滤开关（2026-10-18）：扫描链两道方向硬过滤的启停；默认开=保持现网门槛 */}
          <ConfigSection title="方向过滤" hint="默认开启=保持现网门槛；关闭=跳过该检测、放开信号池">
          <Row gutter={16}>
            <Col span={12}>
              <Form.Item
                label="方向票关"
                name="direction_vote_gate_enabled"
                valuePropName="checked"
                tooltip="开启：方向票（MACD/布林带/RSI）同向张数不足 2 张时否决候选（现网默认）；关闭：跳过该判定，即使只 1 张同向票也照常走后续链路"
              >
                <Switch checkedChildren="开" unCheckedChildren="关" />
              </Form.Item>
            </Col>
            <Col span={12}>
              <Form.Item
                label="方向冲突关"
                name="direction_conflict_gate_enabled"
                valuePropName="checked"
                tooltip="开启：定向方向与 4h 趋势闸门方向相反时否决候选（现网默认，深回撤触点的主要生死线）；关闭：跳过该判定，允许逆 4h 闸门方向的信号进入候选"
              >
                <Switch checkedChildren="开" unCheckedChildren="关" />
              </Form.Item>
            </Col>
          </Row>
          </ConfigSection>

          {/* AI 分析开关区（原工具栏快捷开关，2026-09-20 收敛至此）；四列排布 */}
          <ConfigSection title="AI 分析" hint="开关保存后即时生效；暂停时段仅拦「筛选后自动分析」，手动单币分析不受限">
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
                tooltip={aiConfigured ? '开启后扫描命中币种自动触发 AI 分析（独立于自动交易开关；手动单币分析不受影响）' : '尚未配置 AI API Key：可在下方「AI 运行时参数」填写 Key 与端点后开启'}
              >
                <Switch />
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
          </Row>
          {/* 暂停时段（2026-10-26）：每天北京时间 [开始, 结束) 整点内跳过筛选后的自动
              AI 分析；开始 > 结束 表示跨零点（如 22:00 → 06:00） */}
          <Row gutter={16}>
            <Col span={6}>
              <Form.Item
                label="AI 分析暂停时段"
                name="ai_pause_enabled"
                valuePropName="checked"
                tooltip={aiConfigured
                  ? '开启后每天在所选北京时间整点区间内跳过「筛选后自动 AI 分析」（扫描与自动交易巡检照常）；手动单币分析不受影响'
                  : '尚未配置 AI API Key：可在下方「AI 运行时参数」填写 Key 与端点后开启'}
              >
                <Switch
                  checkedChildren="开"
                  unCheckedChildren="关"
                />
              </Form.Item>
            </Col>
            <Col span={6}>
              <Form.Item
                label="开始整点"
                name="ai_pause_start_hour"
                tooltip="暂停起始整点（含），北京时间"
              >
                <Select
                  options={HOUR_OPTIONS}
                  style={{ width: '100%' }}
                  disabled={!pauseEnabled}
                />
              </Form.Item>
            </Col>
            <Col span={6}>
              <Form.Item
                label="结束整点"
                name="ai_pause_end_hour"
                tooltip="暂停结束整点（不含），北京时间；小于开始整点表示跨零点，如 22 → 6 即 22:00-次日 06:00"
              >
                <Select
                  options={HOUR_OPTIONS}
                  style={{ width: '100%' }}
                  disabled={!pauseEnabled}
                />
              </Form.Item>
            </Col>
          </Row>
          </ConfigSection>

          {/* AI 运行时参数（2026-10-14）：模型名/端点/并发/超时取自 .env，可在此覆盖；
              留空=回落 .env 默认（占位显示当前生效值）。API Key（2026-10-26）同样上页面，
              但必须与接口端点成对配置才生效——只配一项时整对回落 .env */}
          <ConfigSection title="AI 运行时参数" hint={`留空 = 用后端 .env 默认（占位显示当前生效值）；Key 与端点需成对配置才生效。${aiApiHint}`}>
          <Row gutter={16}>
            <Col span={8}>
              <Form.Item
                label="AI API Key"
                name="ai_api_key"
                tooltip={`${aiApiKeySet ? `库内已配置（${aiApiKeyMasked}），留空不修改；` : ''}与「AI 接口端点」同时配置才生效，只配一项则整对回落 .env 默认；清空并保存 = 清除库内 Key`}
              >
                <Input.Password
                  autoComplete="new-password"
                  allowClear
                  placeholder={aiApiKeySet
                    ? `已配置：${aiApiKeyMasked}（留空不修改）`
                    : '留空 = 用后端 .env 默认'}
                  onChange={() => setApiKeyTouched(true)}
                />
              </Form.Item>
            </Col>
            <Col span={8}>
              <Form.Item
                label="AI 接口端点"
                name="ai_base_url"
                tooltip={`当前生效：${aiBaseUrlEff || '—'}（留空=用后端 .env 默认；须 http(s):// 前缀；与 API Key 成对才生效）`}
              >
                <Input placeholder={aiBaseUrlEff} allowClear />
              </Form.Item>
            </Col>
            <Col span={8}>
              <Form.Item
                label="AI 模型名称"
                name="ai_model"
                tooltip={`当前生效：${aiModelEff || '—'}（留空=用后端 .env 默认；改后即生效）`}
              >
                <Input placeholder={aiModelEff} allowClear />
              </Form.Item>
            </Col>
          </Row>
          <Row gutter={16}>
            <Col span={8}>
              <Form.Item
                label="LLM 并发上限"
                name="llm_concurrency"
                tooltip={`当前生效：${aiConcurrencyEff}（留空=用后端 .env 默认；1~64，Redis 信号量阈值）`}
              >
                <InputNumber
                  min={1} max={64} style={{ width: '100%' }}
                  placeholder={String(aiConcurrencyEff)}
                />
              </Form.Item>
            </Col>
            <Col span={8}>
              <Form.Item
                label="AI 调用超时（秒）"
                name="ai_call_timeout_s"
                tooltip={`当前生效：${aiTimeoutEff}（留空=用后端 .env 默认；5~3600，单次 LLM 调用超时）`}
              >
                <InputNumber
                  min={5} max={3600} style={{ width: '100%' }}
                  placeholder={String(aiTimeoutEff)}
                />
              </Form.Item>
            </Col>
          </Row>
          </ConfigSection>
        </Form>
        {/* 配置水位（docs/23 T14a）：只读，后端 onupdate 维护；null=该列上线前从未记录。
            排查「改了配置没生效」先看这里——时间对得上说明改动已落库，问题在消费侧 */}
        <div style={{ fontSize: 12, color: '#999', paddingBottom: 4 }}>
          配置更新于{' '}
          {aiConfig?.updated_at
            ? `${bj(aiConfig.updated_at).format('YYYY-MM-DD HH:mm:ss')}（北京时间）`
            : '未记录'}
        </div>
        </div>
        </Spin>
      </Modal>
    </>
  )
}
