// 信号类型 / 关键位位置 / K线形态 标签映射（扫描结果与关注列表共用）

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
