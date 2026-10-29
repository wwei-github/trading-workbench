/**
 * 筛选漏斗关卡元数据（2026-10-28 从 FunnelPanel 抽出，docs/需求文档/18 最新筛选）
 *
 * 「筛选漏斗」Tab（FunnelPanel）与「最新筛选」Tab（LatestFunnelPanel）共用同一份
 * 关卡代码→中文文案/颜色/口径说明映射；两处各写一份必然漂移，故抽成常量模块。
 * 顺序即漏斗逻辑序（STAGE_ORDER 由它派生）。
 */
export const FUNNEL_STAGES: {
  key: string; label: string; color: string; hint: string
  /** 该关的可通过条件（2026-10-29）：有值则在下钻抽屉顶部用绿条展示，
   *  与 hint（为什么被拒）互补——只写在「拒因不止一种、通过集很小」的关卡上 */
  passHint?: string
}[] = [
  { key: 'liquidity', label: '流动性', color: 'default', hint: '24h 成交额低于门槛，不进扫描' },
  { key: 'untradable', label: '不可交易', color: 'default', hint: '（2026-10-11 退役）原下单环境未上架预过滤，已撤——未上架币照常分析，开单时逐网校验并飞书提示；仅旧轮次数据可见' },
  { key: 'new_coin', label: '新币冷静期', color: 'default', hint: '上市未满 N 天（默认 14 天）不进扫描' },
  { key: 'risk_tier', label: '风险档位', color: 'default', hint: '交易所最大允许杠杆 <10×' },
  { key: 'blacklist', label: '黑名单', color: 'default', hint: '手动添加（永久）；自动：实亏≥1.5R 冷却 14 天、≥1.8R 永久、14 天内 3 次止损冷却 14 天' },
  { key: 'gate_4h', label: '4h趋势闸门', color: 'orange', hint: '4h 无明确趋势一票否决：EMA144 走平（斜率不足）/ 缠绕（ADX<20 ∪ 21-55 间距<0.1% ∪ 21×55 锯齿 ∪ 价格穿越 144 >2 次）/ ADX 不可用 / 方向歧义 / 数据不足' },
  {
    key: 'htf_state_lock', label: '4h状态禁开', color: 'orange',
    hint: '4h 状态机不放行：① 非放行状态（拐头/宏观交叉初期/牛内回调/熊内反弹）双向禁开；② 放行状态的方向与候选逆势（锁逆势）——回踩·突破均一票否决（2026-10-20 收紧仅排列，2026-10-29 加入恢复初期）',
    passHint: '放行「多头排列 / 空头排列」与「多头恢复初期 / 空头恢复初期」四种状态：多头侧只放多单、空头侧只放空单（方向不符按锁逆势拦截）。缠绕、拐头、宏观交叉初期、牛内回调、熊内反弹仍禁开。',
  },
  { key: 'votes', label: '方向票', color: 'orange', hint: '方向票（MACD/布林带/RSI）同向张数不足 2（2026-10-15 拆角色，docs/21 O9）' },
  { key: 'direction_flip', label: '方向冲突', color: 'orange', hint: '方向票方向与 4h 闸门方向相反' },
  { key: 'structure_1h', label: '1h结构（旧轮次）', color: 'default', hint: '（2026-10-15 退役硬门：仅旧轮次数据可见；现不过门不否决，仅质量分重罚 -15）' },
  { key: 'range_score', label: '震荡闸门', color: 'orange', hint: '4 项合取任一命中即拒：1h 三线交错 / 55-144 分离过近 / ADX<18 / 波动过度扩张（Donchian 或布林带宽分位 ≥0.8）' },
  { key: 'no_trigger', label: '无触发（旧轮次汇总）', color: 'purple', hint: '2026-10-27 前未细分：通过全部门但无触发信号，也未进待确认池；判定说明列为具体未过条件' },
  { key: 'no_touch', label: '无触点', color: 'purple', hint: '未命中任何启用触点（retest/swing/ema21/ema21_4h/ema55/ema144，受方向禁用表影响）——判定说明列列出当前启用的触点集合' },
  { key: 'leg_gate', label: '反向腿不过', color: 'purple', hint: '触点成立但前置反向腿门不通过（做多须有下跌腿、做空须有反弹腿；根数/幅度/单调性任一不满足）' },
  { key: 'golden_no_context', label: '纯金K不触发', color: 'purple', hint: '本根出现同向金K，但既无回踩触点也无待确认语境——纯金K路径已于 2026-09-29 下线' },
  { key: 'pending_waiting', label: '待确认等待中', color: 'blue', hint: '触点已成立、在等确认金K（窗口内）；不是被拒，下一根仍可确认' },
  { key: 'window_expired', label: '待确认超窗', color: 'purple', hint: '待确认窗口耗尽仍未出现确认金K，pending 转终态过期' },
  { key: 'pending_flip', label: '待确认反向', color: 'purple', hint: '待确认方向与本轮方向相反，pending 作废（方向翻转）' },
  { key: 'insufficient_bars', label: 'K线不足', color: 'default', hint: '已收盘 K 线不足 30 根，触发判定未跑（新币/数据缺失）' },
  { key: 'no_golden', label: '无确认金K', color: 'purple', hint: '本根无同向金K，且无待确认语境' },
  { key: 'trigger_whitelist', label: '触发白名单外', color: 'purple', hint: '触发类型不在白名单（筛选配置）' },
  // 质量分下限（2026-10-28）：最后一道准入，候选已过全部关卡但分数不足；
  // 不是结构/趋势问题，明细里展示 raw_candidates 的分项与箱体几何。
  { key: 'quality_floor', label: '质量分下限', color: 'orange', hint: '质量分低于筛选配置下限，不进候选池与 AI（不影响待确认池 upsert 与命中历史）' },
]
/** 「无触发」关卡族（2026-10-27 docs/16 §6.3）：漏斗把 no_trigger 拆成 8 类之后，
 *  待确认池的「无触发」页签仍要一次看到这一族全部币种（否则拆分后页签会看起来空了）。
 *  含旧的 no_trigger（拆分层上线前的历史轮次）。 */
export const NO_TRIGGER_FAMILY = new Set([
  'no_trigger', 'no_touch', 'leg_gate', 'golden_no_context',
  'pending_waiting', 'window_expired', 'pending_flip', 'insufficient_bars', 'no_golden',
])
export const isNoTriggerStage = (stage: string) => NO_TRIGGER_FAMILY.has(stage)
export const STAGE_ORDER = Object.fromEntries(FUNNEL_STAGES.map((s, i) => [s.key, i]))
export const STAGE_META = Object.fromEntries(FUNNEL_STAGES.map((s) => [s.key, s]))
