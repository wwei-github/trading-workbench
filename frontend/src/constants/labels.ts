// 信号类型 / 关键位位置 / K线形态 标签映射（扫描结果与关注列表共用）
import type { KeyLevel } from "../types";

export const SIGNAL_TYPE_MAP: Record<string, { label: string; color: string }> = {
  // 新结构分类（关键位重构）
  uptrend: { label: "上涨趋势", color: "green" },
  downtrend: { label: "下跌趋势", color: "red" },
  trend_reversal: { label: "趋势反转", color: "purple" },
  range_bound: { label: "震荡区间", color: "orange" },
  breakout: { label: "放量突破", color: "gold" },
  unknown: { label: "未分类", color: "default" },
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

// 位置标签：两类化后新数据的 kind 即角色（support→绿 / resistance→红），
// 提示取同 kind 关键位中距现价最近的一档（关键位是列表，多位同 kind，取最近才有意义）；
// 旧 kind（前高/前低等）仅历史展示，不再反查关键位（语义已变，提示无意义）
export function positionTag(
  kind: string,
  keyLevels?: KeyLevel[] | null,
  currentPrice?: number,
): { label: string; color: string; tip?: string } {
  const label = POSITION_LABEL_MAP[kind] || kind;
  const isSupportRole = POSITION_SUPPORT_KINDS.has(kind);
  if (!keyLevels?.length || !["support", "resistance"].includes(kind)) {
    return { label, color: isSupportRole ? "green" : "red" };
  }
  let hit: KeyLevel | undefined;
  if (currentPrice && currentPrice > 0) {
    hit = [...keyLevels]
      .filter((lv) => lv.kind === kind)
      .sort(
        (a, b) => Math.abs(a.price - currentPrice) - Math.abs(b.price - currentPrice),
      )[0];
  }
  const tip = hit
    ? `${label} ${hit.price}，触及 ${hit.touches} 次，区域 ${hit.zone_low}~${hit.zone_high}`
    : undefined;
  return { label, color: isSupportRole ? "green" : "red", tip };
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

export function patternStyle(v: string): { label: string; color: string } {
  if (BULLISH_PATTERNS[v]) return { label: BULLISH_PATTERNS[v], color: "green" };
  if (BEARISH_PATTERNS[v]) return { label: BEARISH_PATTERNS[v], color: "red" };
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
export const EMA_STATE_MAP: Record<string, { label: string; color: string }> = {
  bullish_align: { label: "多头排列", color: "green" },
  bearish_align: { label: "空头排列", color: "red" },
  bullish_cross: { label: "金叉", color: "cyan" },
  bearish_cross: { label: "死叉", color: "magenta" },
  turning_up: { label: "拐头向上", color: "lime" },
  turning_down: { label: "拐头向下", color: "volcano" },
  mixed: { label: "纠缠", color: "default" },
};

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
