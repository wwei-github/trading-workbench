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

# 触发类型白名单键值（与 trigger.py 同名字符串同源；此处独立定义避免循环导入）。
# trigger_candlestick（纯金K）2026-09-29 下线：金K必须与触点配合（当根/次根确认），
# 不再作为独立触发类型；历史 scan_results 行的展示映射保留（labels/TRIGGER_FAMILY）
TRIGGER_BREAKOUT = "trigger_breakout"                  # 结构突破：收盘越过未破摆动前高/前低
TRIGGER_TREND_BREAKOUT = "trigger_trend_breakout"      # 趋势突破：收盘破 N 根 Donchian 滚动轨
TRIGGER_PULLBACK_CONFIRM = "trigger_pullback_confirm"  # 回踩触点后金K确认
# trigger_trend_pullback（趋势回踩）2026-10-12 下线：它并非独立信号，而是同一
# 回踩路径的「质量分层标注」（4H闸门+1H结构通过时升级），已将其并入
# trigger_pullback_confirm；1H 结构门 2026-10-15 起降级为 ema21 触点的软校验
# （不过门不否决、仅质量分重罚 -15，见 trigger.py 与 docs/21 O9）。
# 历史 scan_results 行展示映射保留（labels/TRIGGER_FAMILY）
ALL_TRIGGERS = (TRIGGER_BREAKOUT, TRIGGER_TREND_BREAKOUT, TRIGGER_PULLBACK_CONFIRM)

# 回踩触点白名单（docs/22）：横向结构语义强于均线，标注优先级即此顺序
TOUCH_RETEST = "retest"    # 突破回测位（破位价：结构前高/前低或 Donchian 轨，回踩不破=突破有效性确认；
                           # 2026-10-16 起双来源：破轨事件 + 结构推导——新 HH 收盘越位后，被越过的前一个 HH 即回测位）
TOUCH_SWING = "swing"      # 摆动结构位（HL/LH，结构防守位）
TOUCH_EMA21 = "ema21"      # 趋势动态价值区（原口径）
TOUCH_EMA21_4H = "ema21_4h"  # 4h EMA21 大周期动态价值区（1h 信号K线影线触及，2026-10-09）
TOUCH_EMA55 = "ema55"      # 中深度回撤（排列+斜率门）
TOUCH_EMA144 = "ema144"    # 深回撤（2026-10-16 复出：1h EMA144 走平或顺 4h 方向门，
                           # 不再叠加排列门；数据门 ≥245 根，扫描链
                           # limit=max(kline_window,300) 已满足）
ALL_TOUCH_REFS = (
    TOUCH_RETEST, TOUCH_SWING, TOUCH_EMA21, TOUCH_EMA21_4H, TOUCH_EMA55, TOUCH_EMA144,
)
# 默认回测位 + 均线族 + 摆动结构位（2026-10-08 均线族，2026-10-09 加入 4h EMA21，
# 2026-10-14 加入 swing——实测回踩样本中 HL/LH 结构位占比可观，仅均线族覆盖面
# 不足；2026-10-17 retest 默认开启——2026-10-16 起它已有结构推导来源（trigger.
# _structural_retest_level，不依赖 breakout_events 先手记录），语义可靠性提升，
# 经 docs/23 方案 A1 拍板直接默认启用〔不做影子先行〕）。
# ema55 受分离史结构门自筛
# （2026-10-10 三次拍板：发散度改分离史口径，深回踩不再被触点时刻现值误杀）。
# ema144 复出（2026-10-16 用户拍板）：2026-10-10 曾因「深回撤存活率极低（排列门
# 常先杀）」退役——本次改判门控口径（去排列门，改「1h EMA144 走平或顺 4h 方向」，
# 见 trigger._detect_ema_touch）后默认启用；退役机制保留（_touch_refs 仍过滤
# RETIRED_TOUCH_REFS，当前为空集）。
RETIRED_TOUCH_REFS = ()
DEFAULT_TOUCH_REFS = (
    TOUCH_RETEST, TOUCH_SWING, TOUCH_EMA21, TOUCH_EMA21_4H, TOUCH_EMA55, TOUCH_EMA144,
)

# scan_filters JSONB 的合法键集（PUT /config 校验与前端字段一一对应）
SCAN_FILTER_KEYS = (
    "min_volume_24h",             # ① 宇宙层：24h 成交额门槛（USDT）
    "new_coin_cooldown_days",     # ① 宇宙层：新币冷静期（天，上市未满 N 天不进扫描，0=关闭）
    "breakout_channel_bars",      # ③ 触发层：Donchian 通道回看根数
    "breakout_threshold",         # ③ 触发层：最小突破幅度（0.002=0.2%，毛边不算）
    "breakout_vol_ratio",         # ③ 触发层：放量加分门槛（量比口径，达标+5分不卡死）
    "breakout_max_pct",           # ③ 触发层：突破幅度上限（防追高，0=关闭）
    "candle_max_ema21_dist_pct",  # ③ 触发层：悬空金K距 EMA21 上限（纯金K下线后无消费方，键保留兼容）
    "ai_max_per_scan",            # ④ 配额层：单轮扫描 AI 分析上限
    "allowed_triggers",           # ③ 触发层：触发类型白名单（2026-09-28）
    "pullback_touch_refs",        # ③ 触发层：回踩触点白名单（2026-09-28 docs/22）
    "retest_window_bars",         # ③ 触发层：突破回测有效窗口（根，0=关闭 retest）
    "pending_confirm_window_bars",  # ③ 触发层：回踩待确认窗口（根，金K须出现在触碰根后 1..N 根内）
    "structure_touch_band_pct",   # ③ 触发层：retest/swing 横向位触碰带宽（%）
    "ema_touch_min_bars",         # ③ 触发层：EMA 触碰最小已收盘根数（数据门）
    "structure_1h_gate",          # ② 结构层：1h 结构门开关（2026-10-11，false=跳过判定）
    # 方向过滤开关（2026-10-18）：True=过滤生效（默认，保持现网门槛），false=跳过该检测
    "direction_vote_gate_enabled",      # ② 方向层：方向票关（方向票同向张数不足是否否决候选）
    "direction_conflict_gate_enabled",  # ② 方向层：方向冲突关（定向与 4h 闸门方向相反是否否决）
    # 回踩前置反向腿门（2026-10-22）：Stage-1 触点之前须存在一段反向腿
    # （做多=下跌腿、做空=反弹腿），否则不建 pending/不确认；retest 豁免
    "pullback_leg_gate_enabled",        # ③ 触发层：腿门总开关（false=跳过）
    "pullback_leg_lookback_bars",       # ③ 触发层：腿搜索回看根数（1h）
    "pullback_leg_min_bars",            # ③ 触发层：反向腿最少持续根数
    "pullback_leg_min_atr",             # ③ 触发层：反向腿最小幅度（ATR 倍数）
)
# swing_order（摆动点阶数）不在上表：它是 SystemConfig 顶层列（前端「摆动点阶数」
# 独立提交），resolve 时单独读取并注入 ScanFilters.swing_order（2026-10-12 触发层
# 跟随配置），与 scan_filters JSONB 键集无关


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
    # 回踩待确认窗口（2026-10-13-10）：触碰根后 1..N 根已收盘K线内出现同向金K即确认；
    # 2026-10-14 默认 3→4（实测金K迟到 1 根是待确认池最大卡点，放宽约多覆盖 30% 迟到确认）
    pending_confirm_window_bars: int = 4
    structure_touch_band_pct: float = 0.3
    ema_touch_min_bars: int = 200
    new_coin_cooldown_days: int = 14
    # 摆动点阶数（2026-10-12 触发层跟随配置）：swing 触点 / 结构突破 pivot 取材
    # 与 AI 事实包、K线标注共用 SystemConfig.swing_order，检测层不再硬编码 3
    swing_order: int = 3
    # 1h 结构门开关：已于 2026-10-12 退役（docs/21 O7——结构门移出 scan 资格闸，
    # scanner 不再消费）。字段保留仅为向后兼容（DB JSONB 旧值可解析），同
    # candle_max_ema21_dist_pct 退役先例
    structure_1h_gate: bool = True
    # 方向过滤开关（2026-10-18）：True=过滤生效（默认，保持现网门槛行为）；
    # False=跳过该检测。①方向票关：同向张数 < DIR_VOTE_MIN 是否否决；
    # ②方向冲突关：定向方向与 4h 闸门方向相反是否否决（scanner 判定链消费）
    direction_vote_gate_enabled: bool = True
    direction_conflict_gate_enabled: bool = True
    # 回踩前置反向腿门（2026-10-22）：做多要求触点前一段下跌腿、做空要求一段反弹腿；
    # 默认轻档（2 根 / 0.5 ATR），优先避免过度过滤；retest 触点豁免（trigger 内处理）
    pullback_leg_gate_enabled: bool = True
    pullback_leg_lookback_bars: int = 24
    pullback_leg_min_bars: int = 2
    pullback_leg_min_atr: float = 0.5


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
        默认 DEFAULT_TOUCH_REFS（回测位+摆动结构位+均线族；回踩触点与触发类型
        不同——静默全空会让回踩管线整体停摆，回落默认比全量放行安全）。退役触点
        （RETIRED_TOUCH_REFS，当前为空集——ema144 已于 2026-10-16 复出）
        强制过滤——退役期间即使 DB 旧配置显式勾选也失效，保证「退役」语义
        不留暗门（机制保留，供后续再退役）。"""
        v = raw.get(key)
        if not isinstance(v, list):
            return DEFAULT_TOUCH_REFS
        allowed = tuple(t for t in v
                        if t in ALL_TOUCH_REFS and t not in RETIRED_TOUCH_REFS)
        return allowed or DEFAULT_TOUCH_REFS

    def _bool(key: str, default: bool) -> bool:
        """布尔开关：仅接受真布尔（DB JSONB 天然存布尔）；字符串/数字等
        非法类型按缺失回落默认——不让一条坏配置静默改变门行为。"""
        v = raw.get(key)
        return v if isinstance(v, bool) else default

    def _swing_order(default: int) -> int:
        """摆动点阶数：读 SystemConfig 顶层列（非 scan_filters JSONB，前端
        「摆动点阶数」独立提交，2026-10-12 触发层跟随配置）；None（无 DB 行）/
        非法类型 / <1 按缺失回落 env（settings.SWING_ORDER）。"""
        try:
            v = int(getattr(cfg, "swing_order", None))
        except (TypeError, ValueError, OverflowError):
            return default
        return v if v >= 1 else default

    return ScanFilters(
        min_volume_24h=_num("min_volume_24h", float(settings.MIN_VOLUME_24H)),
        new_coin_cooldown_days=_int("new_coin_cooldown_days", settings.NEW_COIN_COOLDOWN_DAYS),
        breakout_channel_bars=_int("breakout_channel_bars", settings.BREAKOUT_CHANNEL_BARS, minimum=5),
        breakout_threshold=_num("breakout_threshold", settings.BREAKOUT_THRESHOLD),
        breakout_vol_ratio=_num("breakout_vol_ratio", settings.BREAKOUT_VOL_RATIO),
        breakout_max_pct=_num("breakout_max_pct", settings.BREAKOUT_MAX_PCT),
        candle_max_ema21_dist_pct=_num("candle_max_ema21_dist_pct", settings.CANDLE_MAX_EMA21_DIST_PCT),
        ai_max_per_scan=_int("ai_max_per_scan", settings.AI_MAX_PER_SCAN, minimum=1),
        allowed_triggers=_triggers("allowed_triggers"),
        pullback_touch_refs=_touch_refs("pullback_touch_refs"),
        retest_window_bars=_int("retest_window_bars", settings.RETEST_WINDOW_BARS),
        pending_confirm_window_bars=_int(
            "pending_confirm_window_bars", settings.PENDING_CONFIRM_WINDOW_BARS, minimum=1),
        structure_touch_band_pct=_num("structure_touch_band_pct", settings.STRUCTURE_TOUCH_BAND_PCT),
        ema_touch_min_bars=_int("ema_touch_min_bars", settings.EMA_TOUCH_MIN_BARS),
        structure_1h_gate=_bool("structure_1h_gate", settings.STRUCTURE_1H_GATE),
        direction_vote_gate_enabled=_bool(
            "direction_vote_gate_enabled", settings.DIRECTION_VOTE_GATE_ENABLED),
        direction_conflict_gate_enabled=_bool(
            "direction_conflict_gate_enabled", settings.DIRECTION_CONFLICT_GATE_ENABLED),
        pullback_leg_gate_enabled=_bool("pullback_leg_gate_enabled", True),
        pullback_leg_lookback_bars=_int("pullback_leg_lookback_bars", 24, minimum=2),
        pullback_leg_min_bars=_int("pullback_leg_min_bars", 2),
        pullback_leg_min_atr=_num("pullback_leg_min_atr", 0.5),
        swing_order=_swing_order(settings.SWING_ORDER),
    )
