// 信号类型 / 关键位位置 / K线形态 标签映射（扫描结果与关注列表共用）
import type { KeyLevel } from "../types";

export const SIGNAL_TYPE_MAP: Record<string, { label: string; color: string }> = {
  // 新结构分类（关键位重构）
  uptrend: { label: "上涨趋势", color: "green" },
  downtrend: { label: "下跌趋势", color: "red" },
  trend_reversal: { label: "趋势反转", color: "purple" },
  range_bound: { label: "震荡区间", color: "orange" },
  unknown: { label: "未分类", color: "default" },
  // 旧值保留映射（兼容历史记录）
  downtrend_breakout: { label: "下跌突破", color: "red" },
  uptrend_pullback: { label: "上涨回调", color: "green" },
};

export const POSITION_LABEL_MAP: Record<string, string> = {
  prev_high: "前高",
  prev_low: "前低",
  support: "支撑位",
  resistance: "压力位",
  range_top: "区间顶",
  range_bottom: "区间底",
};

// 支撑类位置（显示绿色标签）
export const POSITION_SUPPORT_KINDS = new Set([
  "prev_low",
  "support",
  "range_bottom",
]);

// 位置标签：颜色跟随关键位的实际角色（kind 仅表示来源结构）。
// 关键位被突破后角色互换（docs/03）：如支撑位跌破后位于现价上方，实际是压力（回抽），
// 此时标签显示"支撑位→压力"并标红，避免"支撑位+空头排列"这类看似矛盾的展示。
export function positionTag(
  kind: string,
  keyLevels?: KeyLevel[] | null,
): { label: string; color: string; tip?: string } {
  const base = POSITION_LABEL_MAP[kind] || kind;
  const hit = keyLevels?.find((lv) => lv.kind === kind);
  const role = hit?.role ?? (POSITION_SUPPORT_KINDS.has(kind) ? "support" : "resistance");
  const isSupportRole = role === "support";
  // kind 来源侧与实际角色不一致 → 已发生角色互换
  const flipped = hit != null && isSupportRole !== POSITION_SUPPORT_KINDS.has(kind);
  const label = flipped ? `${base}→${isSupportRole ? "支撑" : "压力"}` : base;
  const tip = hit
    ? `${base} ${hit.price}，${isSupportRole ? "支撑" : "压力"}，触及 ${hit.touches} 次`
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

export const POSITION_FILTERS = Object.entries(POSITION_LABEL_MAP).map(
  ([value, text]) => ({ text, value }),
);

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
  rule_123: { label: "123法则", color: "purple" },
  n_structure: { label: "N字结构", color: "cyan" },
  rule_2b: { label: "2B法则", color: "geekblue" },
  range_edge: { label: "区间边缘反转", color: "gold" },
};
