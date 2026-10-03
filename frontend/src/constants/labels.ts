// 信号类型 / 信号位置 / K线形态 标签映射（扫描结果与关注列表共用）
import { schemeTag } from '../utils/scheme'
import type { ColorScheme } from '../stores/scanStore'

export const SIGNAL_TYPE_MAP: Record<string, { label: string; color: string }> = {
  // 新结构分类（关键位重构）
  uptrend: { label: "上涨趋势", color: "green" },
  downtrend: { label: "下跌趋势", color: "red" },
  trend_reversal: { label: "趋势反转", color: "purple" },
  range_bound: { label: "震荡区间", color: "orange" },
  breakout: { label: "放量突破", color: "gold" },
  unknown: { label: "未分类", color: "default" },
  // 指标驱动筛选（2026-09-17）：触发类型（多指标同向 + 入场触发确认）
  trigger_breakout: { label: "同向突破", color: "gold" },
  // 趋势突破（2026-09-29）：Donchian 滚动轨口径，与结构突破并存区分
  trigger_trend_breakout: { label: "趋势突破", color: "volcano" },
  trigger_candlestick: { label: "同向形态", color: "blue" }, // 2026-09-29 纯金K下线，仅历史行
  trigger_pullback: { label: "同向回踩", color: "cyan" },
  // 两段式回踩确认（2026-09-25，docs/19）：回踩后等金K确认才入场
  trigger_pullback_confirm: { label: "回踩金K确认", color: "geekblue" },
  // 旧值保留映射（兼容历史记录）
  downtrend_breakout: { label: "下跌突破", color: "red" },
  uptrend_pullback: { label: "上涨回调", color: "green" },
};

// 两类化（2026-09-12）后新数据只产生 support/resistance；其余为历史兼容展示
export const POSITION_LABEL_MAP: Record<string, string> = {
  support: "支撑位",
  resistance: "压力位",
  prev_high: "前高",
  prev_low: "前低",
  range_top: "区间顶",
  range_bottom: "区间底",
};

// 支撑类位置（旧 kind 的颜色回退；新数据 kind==role 直接判定）
export const POSITION_SUPPORT_KINDS = new Set([
  "prev_low",
  "support",
  "range_bottom",
]);

// 位置标签：新数据 position 即形态/突破方向派生的角色（support→绿 / resistance→红）；
// 旧 kind（前高/前低等）仅历史展示
export function positionTag(kind: string): { label: string; color: string } {
  const label = POSITION_LABEL_MAP[kind] || kind;
  return { label, color: POSITION_SUPPORT_KINDS.has(kind) ? "green" : "red" };
}

// 12 金K + 历史形态映射
const BULLISH_PATTERNS: Record<string, string> = {
  hammer: "锤形线",
  bullish_engulfing: "看涨吞没",
  morning_star: "启明星",
  piercing_line: "刺透线",
  bullish_harami: "看涨孕线",
  dragonfly_doji: "蜻蜓十字",
};

const BEARISH_PATTERNS: Record<string, string> = {
  shooting_star: "倒锤形线（流星）",
  hanging_man: "上吊线",
  bearish_engulfing: "看跌吞没",
  evening_star: "黄昏星",
  dark_cloud_cover: "乌云盖顶",
  bearish_harami: "看跌孕线",
  gravestone_doji: "墓碑十字",
};

const OTHER_PATTERNS: Record<string, string> = {
  inverted_hammer: "倒锤形线",
  doji: "十字星",
  close_above_prev_high: "收盘破前高",
};

// K线形态标签：多/空语义色随全局配色方案翻转（中性类 blue 不翻转）；
// scheme 缺省 = 旧版固定 green/red（过滤选项构建只取 label，不受影响）
export function patternStyle(v: string, scheme?: ColorScheme): { label: string; color: string } {
  if (BULLISH_PATTERNS[v]) {
    return { label: BULLISH_PATTERNS[v], color: scheme ? schemeTag(scheme).up : "green" };
  }
  if (BEARISH_PATTERNS[v]) {
    return { label: BEARISH_PATTERNS[v], color: scheme ? schemeTag(scheme).down : "red" };
  }
  return { label: OTHER_PATTERNS[v] || v, color: "blue" };
}

// ===== 表格列过滤选项（服务端/客户端过滤共用）=====
export const SIGNAL_TYPE_FILTERS = Object.entries(SIGNAL_TYPE_MAP).map(
  ([value, cfg]) => ({ text: cfg.label, value }),
);

// 位置过滤只保留两类（旧 kind 行不进过滤选项，但历史行仍正常渲染）
export const POSITION_FILTERS = (["support", "resistance"] as const).map((value) => ({
  text: POSITION_LABEL_MAP[value],
  value,
}));

export const PATTERN_FILTERS = Object.keys({
  ...BULLISH_PATTERNS,
  ...BEARISH_PATTERNS,
  ...OTHER_PATTERNS,
}).map((v) => ({ text: patternStyle(v).label, value: v }));

// ===== EMA 均线形态状态 =====
export const EMA_STATE_MAP: Record<string, { label: string }> = {
  bullish_align: { label: "多头排列" },
  bearish_align: { label: "空头排列" },
  bullish_cross: { label: "金叉" },
  bearish_cross: { label: "死叉" },
  turning_up: { label: "拐头向上" },
  turning_down: { label: "拐头向下" },
  mixed: { label: "纠缠" },
};

// EMA 形态渲染标签：多/空状态色随全局配色方案翻转（金叉/死叉同为多/空语义；
// 纠缠/未知 = 中性 default）。label 取 EMA_STATE_MAP，未知值原样回显
export function emaStateTag(v: string, scheme: ColorScheme): { label: string; color: string } {
  const label = EMA_STATE_MAP[v]?.label || v;
  if (v.startsWith("bullish") || v === "turning_up") return { label, color: schemeTag(scheme).up };
  if (v.startsWith("bearish") || v === "turning_down") return { label, color: schemeTag(scheme).down };
  return { label, color: "default" };
}

export const EMA_STATE_FILTERS = Object.entries(EMA_STATE_MAP).map(
  ([value, cfg]) => ({ text: cfg.label, value }),
);

// 开单类型（结构打法归类，AI 分析输出）
export const TRADE_TYPE_MAP: Record<string, { label: string; color: string }> = {
  trend_follow: { label: "顺势交易", color: "blue" },
  structure_break: { label: "结构破位回踩", color: "purple" },
  range_edge: { label: "区间边缘反转", color: "gold" },
  // 合并前的历史类型（旧分析/旧交易记录展示用）
  rule_123: { label: "123法则(已并入)", color: "purple" },
  n_structure: { label: "N字结构(已并入)", color: "cyan" },
  rule_2b: { label: "2B法则(已并入)", color: "geekblue" },
};
