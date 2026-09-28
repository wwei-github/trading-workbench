"""筛选参数解析器（2026-09-28 筛选配置上页面）

筛选漏斗可调参数的单一来源（A 组 7 项）：system_config.scan_filters（JSONB，
可空）有值优先生效，缺键回落 env（settings.*），env 未配置回落代码默认。
优先级口径：DB > env > 代码默认。

ScanFilters 是纯数据容器（frozen dataclass），scanner/trigger/ai_tasks 通过
参数透传，策略函数不直接读 DB——None=按 settings 解析，老调用方零改动。
旧 SystemConfig 散列（breakout_threshold/r_squared_threshold/pullback_tolerance）
已废弃不再读，列保留不删（历史行兼容）。
"""
from dataclasses import dataclass
from math import isfinite

from app.config import settings

# 触发类型白名单键值（与 trigger.py 同名字符串同源；此处独立定义避免循环导入）
TRIGGER_BREAKOUT = "trigger_breakout"
TRIGGER_CANDLESTICK = "trigger_candlestick"
TRIGGER_PULLBACK_CONFIRM = "trigger_pullback_confirm"
ALL_TRIGGERS = (TRIGGER_BREAKOUT, TRIGGER_CANDLESTICK, TRIGGER_PULLBACK_CONFIRM)

# 回踩触点白名单（docs/22）：横向结构语义强于均线，标注优先级即此顺序
TOUCH_RETEST = "retest"    # 突破回测位（Donchian 破轨价，回踩不破=突破有效性确认）
TOUCH_SWING = "swing"      # 摆动结构位（HL/LH，结构防守位）
TOUCH_EMA21 = "ema21"      # 趋势动态价值区（原口径）
TOUCH_EMA55 = "ema55"      # 中深度回撤（排列+斜率门）
TOUCH_EMA144 = "ema144"    # 深回撤（排列+斜率门，K线窗口≥300 才实际生效）
ALL_TOUCH_REFS = (TOUCH_RETEST, TOUCH_SWING, TOUCH_EMA21, TOUCH_EMA55, TOUCH_EMA144)
DEFAULT_TOUCH_REFS = (TOUCH_EMA21,)

# scan_filters JSONB 的合法键集（PUT /config 校验与前端字段一一对应）
SCAN_FILTER_KEYS = (
    "min_volume_24h",             # ① 宇宙层：24h 成交额门槛（USDT）
    "breakout_channel_bars",      # ③ 触发层：Donchian 通道回看根数
    "breakout_threshold",         # ③ 触发层：最小突破幅度（0.002=0.2%，毛边不算）
    "breakout_vol_ratio",         # ③ 触发层：放量门槛（量比口径）
    "breakout_max_pct",           # ③ 触发层：突破幅度上限（防追高，0=关闭）
    "candle_max_ema21_dist_pct",  # ③ 触发层：悬空金K距 EMA21 上限（0=关闭）
    "ai_max_per_scan",            # ④ 配额层：单轮扫描 AI 分析上限
    "allowed_triggers",           # ③ 触发层：触发类型白名单（2026-09-28）
    "pullback_touch_refs",        # ③ 触发层：回踩触点白名单（2026-09-28 docs/22）
    "retest_window_bars",         # ③ 触发层：突破回测有效窗口（根，0=关闭 retest）
    "structure_touch_band_pct",   # ③ 触发层：retest/swing 横向位触碰带宽（%）
    "ema_touch_min_bars",         # ③ 触发层：EMA 触碰最小已收盘根数（数据门）
)


@dataclass(frozen=True)
class ScanFilters:
    min_volume_24h: float
    breakout_channel_bars: int
    breakout_threshold: float
    breakout_vol_ratio: float
    breakout_max_pct: float
    candle_max_ema21_dist_pct: float
    ai_max_per_scan: int
    allowed_triggers: tuple = ALL_TRIGGERS
    pullback_touch_refs: tuple = DEFAULT_TOUCH_REFS
    retest_window_bars: int = 24
    structure_touch_band_pct: float = 0.3
    ema_touch_min_bars: int = 200


def resolve_scan_filters(cfg=None) -> ScanFilters:
    """从 SystemConfig 行解析筛选参数（cfg 可 None；JSONB 缺键/非法值逐键回落）。

    逐键容错：类型错误/负数等非法值按缺失处理回落 env，不让一条坏配置
    烧掉整轮扫描（与量能降级口径一致）。
    """
    raw = (getattr(cfg, "scan_filters", None) or {}) if cfg is not None else {}

    def _num(key: str, default: float, minimum: float = 0.0) -> float:
        try:
            v = float(raw.get(key))
        except (TypeError, ValueError):
            return default
        # isfinite：inf/nan（直改 DB 可注入）不设防会污染门槛（inf 全量过滤）
        return v if isfinite(v) and v >= minimum else default

    def _int(key: str, default: int, minimum: int = 0) -> int:
        try:
            v = int(float(raw.get(key)))
        except (TypeError, ValueError, OverflowError):  # OverflowError: int(inf)
            return default
        return v if v >= minimum else default

    def _triggers(key: str) -> tuple:
        """白名单：list 且至少一个合法类型才生效；全不勾/全非法视为配置事故
        回落全量（静默空转一整轮扫描比多出几个信号更难排查）"""
        v = raw.get(key)
        if not isinstance(v, list):
            return ALL_TRIGGERS
        allowed = tuple(t for t in v if t in ALL_TRIGGERS)
        return allowed or ALL_TRIGGERS

    def _touch_refs(key: str) -> tuple:
        """回踩触点白名单（docs/22）：list 过滤非法项后须非空，空/全非法回落
        默认 ["ema21"]（回踩触点与触发类型不同——静默全空会让回踩管线整体
        停摆，回落默认比全量放行安全）。"""
        v = raw.get(key)
        if not isinstance(v, list):
            return DEFAULT_TOUCH_REFS
        allowed = tuple(t for t in v if t in ALL_TOUCH_REFS)
        return allowed or DEFAULT_TOUCH_REFS

    return ScanFilters(
        min_volume_24h=_num("min_volume_24h", float(settings.MIN_VOLUME_24H)),
        breakout_channel_bars=_int("breakout_channel_bars", settings.BREAKOUT_CHANNEL_BARS, minimum=5),
        breakout_threshold=_num("breakout_threshold", settings.BREAKOUT_THRESHOLD),
        breakout_vol_ratio=_num("breakout_vol_ratio", settings.BREAKOUT_VOL_RATIO),
        breakout_max_pct=_num("breakout_max_pct", settings.BREAKOUT_MAX_PCT),
        candle_max_ema21_dist_pct=_num("candle_max_ema21_dist_pct", settings.CANDLE_MAX_EMA21_DIST_PCT),
        ai_max_per_scan=_int("ai_max_per_scan", settings.AI_MAX_PER_SCAN, minimum=1),
        allowed_triggers=_triggers("allowed_triggers"),
        pullback_touch_refs=_touch_refs("pullback_touch_refs"),
        retest_window_bars=_int("retest_window_bars", settings.RETEST_WINDOW_BARS),
        structure_touch_band_pct=_num("structure_touch_band_pct", settings.STRUCTURE_TOUCH_BAND_PCT),
        ema_touch_min_bars=_int("ema_touch_min_bars", settings.EMA_TOUCH_MIN_BARS),
    )
