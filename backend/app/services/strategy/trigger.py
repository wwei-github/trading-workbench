"""入场触发器检测（指标驱动筛选管线，2026-09-17；两段式回踩确认 2026-09-25 docs/19；
回踩触点体系 2026-09-28 docs/22）

方向由技术指标多数决确定（technical.compute_indicator_votes），本模块只回答
"此刻是否出现与候选方向一致的入场触发"：
- T1 trigger_breakout：收盘越过 Donchian 通道（BREAKOUT_CHANNEL_BARS 根高低轨）
  三重收紧：最小幅度 BREAKOUT_THRESHOLD + 放量 BREAKOUT_VOL_RATIO + 长实体短影线形态
- T2 trigger_candlestick：信号K线（klines[-2]）出现 GOLDEN_12 形态且方向一致
- T3 trigger_pullback_confirm：回踩触点企稳 + 同向金K 两段式确认——
  当根同时触碰+金K（当根确认），或上根触碰（pending）+ 本根金K（次根确认）；
  仅触碰无金K 不再当根触发，转入 pending 待确认池（scanner 持久化）

回踩触点（docs/22 §2，启用白名单见 ScanFilters.pullback_touch_refs）：
- retest  突破回测位：Donchian 破轨价（scanner 维护 breakout_events，T1 触发时记录），
  影线可入带、收盘须守住——回踩不破 = 突破有效性确认
- swing   摆动结构位：最近的 HL（多）/LH（空）防守位，只认结构延续标签
- ema21   趋势动态价值区（原口径，不加门，行为与 docs/19 完全一致）
- ema55/144 中/深回撤：数据门（ema_touch_min_bars）+ 排列门（快线在慢线同侧）
  + 斜率门（近 5 根同向）——走平/错排的慢线是震荡不是回踩
同根多触点命中按优先级标注（横向结构 > 均线；均线取最快）：
retest > swing > ema21 > ema55 > ema144

优先级（决策矩阵，docs/19 §4.1）：T1 > 当根确认 > 次根确认 > 纯金K(T2) >
触碰转 pending；pending 仅在"恰好下一根K线"有效（开盘时间精确比对）。

position 语义（docs/09 方向派生口径，2026-09-28 关键位移除后回归）：
看涨形态/回踩企稳=support、向上突破/反抽受阻=resistance。
"""
from datetime import datetime
from typing import Optional, Tuple

from app.services.scan_filters import (
    TOUCH_EMA144,
    TOUCH_EMA21,
    TOUCH_EMA55,
    TOUCH_RETEST,
    TOUCH_SWING,
    ScanFilters,
    resolve_scan_filters,
)
from app.services.strategy import recent_swings
from app.services.strategy.candlestick import (
    GOLDEN_12,
    PATTERN_LABEL_MAP,
    detect_all_patterns,
)
from app.services.strategy.ema import calc_ema
from app.services.strategy.indicators import volume_ratio
import numpy as np

# 触发类型 = signal_type（AI 提示词 SIGNAL_TYPE_LABELS 已注册同名标签）
BREAKOUT = "trigger_breakout"
CANDLESTICK = "trigger_candlestick"
PULLBACK = "trigger_pullback"          # 历史类型：两段式上线后不再产生新记录
PULLBACK_CONFIRM = "trigger_pullback_confirm"  # 回踩触点后金K确认（当根/次根）

# 回踩触点标注优先级与中文标签（docs/22 §2；与 scan_filters.ALL_TOUCH_REFS 同源顺序）
TOUCH_ORDER = (TOUCH_RETEST, TOUCH_SWING, TOUCH_EMA21, TOUCH_EMA55, TOUCH_EMA144)
TOUCH_LABELS = {
    TOUCH_RETEST: "突破回测位",
    TOUCH_SWING: "摆动结构位",
    TOUCH_EMA21: "EMA21",
    TOUCH_EMA55: "EMA55",
    TOUCH_EMA144: "EMA144",
}

# 摆动结构位时效门：防守点太老（>40 根前确认）防守意义衰减
SWING_MAX_BARS_AGO = 40

# 突破K线形态门槛：长实体、影线几乎不可见（光头光脚，防长影毛刺假突破）
BREAKOUT_BODY_RANGE_MIN = 0.75   # 实体/K线全长 ≥ 0.75
BREAKOUT_SHADOW_BODY_MAX = 0.20  # 单侧影线 ≤ 实体的 20%


def _is_solid_breakout_candle(klines: list[list], direction: int) -> bool:
    """突破K线必须为顺向长实体、影线几乎不可见。

    量化（2026-09-25 突破策略收紧）：
    - 顺向实体：做多要求阳线（close>open），做空要求阴线（close<open）
    - 长实体：|close-open| / (high-low) ≥ 0.75
    - 短影线：上影与下影各 ≤ 实体的 20%（接近光头光脚）
    """
    o = float(klines[-2][1])
    h = float(klines[-2][2])
    l = float(klines[-2][3])
    c = float(klines[-2][4])
    body = abs(c - o)
    rng = h - l
    if rng <= 0 or body <= 0:
        return False
    if direction == 1 and c <= o:
        return False
    if direction == -1 and c >= o:
        return False
    if body / rng < BREAKOUT_BODY_RANGE_MIN:
        return False
    upper = h - max(o, c)
    lower = min(o, c) - l
    return upper <= BREAKOUT_SHADOW_BODY_MAX * body and lower <= BREAKOUT_SHADOW_BODY_MAX * body


def _detect_ema_touch(
    klines: list[list], direction: int, period: int, flt: ScanFilters,
) -> Optional[float]:
    """EMA 触碰检测（docs/22 §3.3）。

    多头=下影触及且收盘收回线上（low ≤ EMA ≤ close），空头对称
    （close ≤ EMA ≤ high）。命中返回触碰时的 EMA 值，未命中 None。

    ema21：原口径（docs/19），不加任何门，行为完全不变；
    ema55/144：三道门防"把震荡当回踩"——
    ① 数据门：已收盘根数 < max(ema_touch_min_bars, 周期×1.7) 静默跳过
      （EMA 收敛需要窗口，KLINE_WINDOW=240 时 ema144 自动不可用，须提窗 ≥300）
    ② 排列门：多头要求快线在慢线上方（ema21>ema55 / ema55>ema144，55 不可用
      降级只查斜率），空头对称——慢线之下还有快线托着才是趋势回踩
    ③ 斜率门：被触碰 EMA 近 5 根同向（走平的慢线是震荡盘整）
    """
    closes = np.array([float(k[4]) for k in klines[:-1]], dtype=float)
    if period != 21 and len(closes) < max(flt.ema_touch_min_bars, int(period * 1.7)):
        return None
    e = calc_ema(closes, period)
    if e is None:
        return None
    ema = float(e[-1])
    if ema <= 0 or not np.isfinite(ema):
        return None
    close_last = float(klines[-2][4])
    high_last = float(klines[-2][2])
    low_last = float(klines[-2][3])
    if direction == 1 and not (low_last <= ema <= close_last):
        return None
    if direction == -1 and not (close_last <= ema <= high_last):
        return None
    if period == 21:
        return ema

    # 排列门（ema55/144）
    if period == 55:
        e_fast = calc_ema(closes, 21)
        if e_fast is not None and np.isfinite(float(e_fast[-1])) and float(e_fast[-1]) > 0:
            fast = float(e_fast[-1])
            if direction == 1 and not fast > ema:
                return None
            if direction == -1 and not fast < ema:
                return None
    else:  # 144：55 可用才查排列，不可用降级只查斜率
        e_mid = calc_ema(closes, 55)
        if e_mid is not None and np.isfinite(float(e_mid[-1])) and float(e_mid[-1]) > 0:
            mid = float(e_mid[-1])
            if direction == 1 and not mid > ema:
                return None
            if direction == -1 and not mid < ema:
                return None

    # 斜率门：近 5 根同向
    if direction == 1 and not float(e[-1]) > float(e[-6]):
        return None
    if direction == -1 and not float(e[-1]) < float(e[-6]):
        return None
    return ema


def _detect_retest(
    klines: list[list], direction: int, retest: Optional[dict],
    window_bars: int, band_pct: float,
) -> Optional[float]:
    """突破回测位触碰（docs/22 §3.1）。

    retest: scanner 侧活跃破轨事件快照 {price, direction, candle_open}
    （naive UTC，与 klines 时间戳同源换算）。判定：
    - 方向一致（反向破轨事件对当前候选无效）
    - 窗口：事件K线开盘须落在最近 window_bars 根已收盘K线内（越久回测语义越弱）；
      window_bars=0 关闭该触点
    - 带宽触碰：影线可下插入带（l ≤ lvl×(1+band)），收盘须守住位上（c ≥ lvl），
      空头对称——收盘破位 = 突破失败，不构成回测
    命中返回位价，未命中 None。
    """
    if window_bars <= 0 or not retest or retest.get("direction") != direction:
        return None
    lvl = float(retest.get("price") or 0)
    if lvl <= 0 or not np.isfinite(lvl):
        return None
    ev_open = retest.get("candle_open")
    if isinstance(ev_open, datetime):
        try:
            oldest = datetime.utcfromtimestamp(float(klines[-2 - int(window_bars)][0]) / 1000)
            anchor = datetime.utcfromtimestamp(float(klines[-2][0]) / 1000)
            if not (oldest <= ev_open <= anchor):
                return None
        except (IndexError, TypeError, ValueError, OSError):
            return None
    band = max(band_pct, 0.0) / 100.0
    close_last = float(klines[-2][4])
    high_last = float(klines[-2][2])
    low_last = float(klines[-2][3])
    if direction == 1:
        return lvl if (low_last <= lvl * (1 + band) and close_last >= lvl) else None
    return lvl if (high_last >= lvl * (1 - band) and close_last <= lvl) else None


def _detect_swing_touch(
    klines: list[list], direction: int, band_pct: float,
) -> Optional[float]:
    """摆动结构位触碰（docs/22 §3.2）。

    复用 recent_swings（收盘价摆动点，两侧各 3 根确认，无前视）：
    多头只认最近的 HL（更高低点，结构延续防守位），空头只认最近的 LH——
    LL/HH 不构成回踩结构（是接刀/追高）。时效门 bars_ago ≤ 40。
    带宽触碰与 retest 同口径（影线入带、收盘守住）。命中返回位价。
    """
    sw = recent_swings(klines, order=3, n=3)
    seq = sw["lows"] if direction == 1 else sw["highs"]
    want = "HL" if direction == 1 else "LH"
    lvl = next(
        (float(p["price"]) for p in seq
         if p["label"] == want and p["bars_ago"] <= SWING_MAX_BARS_AGO),
        None,
    )
    if lvl is None or lvl <= 0:
        return None
    band = max(band_pct, 0.0) / 100.0
    close_last = float(klines[-2][4])
    high_last = float(klines[-2][2])
    low_last = float(klines[-2][3])
    if direction == 1:
        return lvl if (low_last <= lvl * (1 + band) and close_last >= lvl) else None
    return lvl if (high_last >= lvl * (1 - band) and close_last <= lvl) else None


def _close_op(pending: Optional[dict], direction: int, supersede_reason: str) -> Optional[dict]:
    """pending 终态处置（行1/行2 路径）：方向反转一律 expire(direction_flip)——
    死因是票崩而非被抢先，审计语义优先；同方向被更强触发（T1 突破/当根确认）
    抢先 → supersede。"""
    if not pending:
        return None
    if pending.get("direction") != direction:
        return {"op": "expire", "reason": "direction_flip"}
    return {"op": "supersede", "reason": supersede_reason}


def detect_trigger(
    klines: list[list], direction: int,
    pending: Optional[dict] = None,
    flt: Optional[ScanFilters] = None,
    retest: Optional[dict] = None,
) -> Tuple[Optional[dict], Optional[dict]]:
    """检测与候选方向一致的入场触发（两段式决策矩阵，docs/19 §4.1 + docs/22 触点）。

    direction: 1 多 / -1 空（指标多数决的符号）
    pending: 该币活跃 pending 快照（{id, direction, stage1_candle_open, touch_ref}）或 None
    flt（可选）：筛选参数组（2026-09-28 筛选配置上页面）；None=按 settings
    解析（env/代码默认），策略函数不读 DB。
    retest（可选）：scanner 侧活跃破轨事件快照 {price, direction, candle_open}
    （docs/22 §3.1；T1 突破触发时由 scanner 记录 breakout_events）。
    返回 (trigger, pending_op) 二元组：
    - trigger: 命中的触发 dict（type/direction/position/touch_ref/pattern/
      breakout_pct/reason）或 None（无触发 → 观察池）；touch_ref 仅回踩确认
      携带（当根确认=本次命中触点，次根确认=pending 的 Stage-1 触点）
    - pending_op: 本轮对活跃 pending 的处置，None 或
      {"op": "supersede"|"confirm"|"expire"|"upsert", ...}
      ——upsert/expire 后续在 scanner 侧结合 DB 行补全（方向反转拆两步、
      quality/votes 快照由 scanner 填充），trigger.py 保持无 DB 依赖。

    信号判定全部基于已收盘K线（klines[-2]）；pending 有效期 = 其 stage1_candle_open
    恰为 klines[-3] 的开盘时间（上一根已收盘K线），免疫扫描时刻漂移与轮次缺失。
    """
    # 筛选参数组（None=按 settings 解析，env/代码默认兜底）
    flt = flt or resolve_scan_filters(None)
    n = len(klines)
    if n < 30 or direction not in (1, -1):
        return None, None
    close_last = float(klines[-2][4])
    # position 按方向派生（2026-09-28 关键位移除后回到 docs/09 原始口径）：
    # 看涨形态/回踩企稳=support、向上突破/反抽受阻=resistance
    pos_fb = "support" if direction == 1 else "resistance"
    wanted = "bullish" if direction == 1 else "bearish"

    # pending 有效期判定：Stage-1 锚点必须是上一根已收盘K线（klines[-3]）
    pending_valid = False
    if pending:
        try:
            anchor = datetime.utcfromtimestamp(float(klines[-3][0]) / 1000)
            pending_valid = pending.get("stage1_candle_open") == anchor
        except (IndexError, TypeError, ValueError, OSError):
            pending_valid = False

    # T1 Donchian 突破（窗口不含信号K线自身与未收盘根，防"自己破自己"）。
    # 三重收紧（2026-09-25）：① 最小突破幅度 BREAKOUT_THRESHOLD（毛边不算）
    # ② 放量 BREAKOUT_VOL_RATIO（量比 ≥ 近20根均量1.2倍）
    # ③ 长实体短影线形态（光头光脚，防长影毛刺）；任一不满足 → 落入金K/回踩分支
    # 幅度上限（2026-09-27 正式网复盘）：越过轨 >BREAKOUT_MAX_PCT（默认 1.5%）视为
    # 追高——突破类 ≥2% 的 9 笔 7 亏（-0.21R），大幅突破追入负期望；跳过 T1 判定，
    # 落回金K/回踩分支（趋势日后续回踩 EMA21+金K 由 T3 接住）。0=关闭。
    # 突破返回携带 breakout_level（被穿轨价）与 candle_open（信号K线开盘），
    # scanner 据此维护 breakout_events（回测触点的事实源，docs/22 §4）。
    chan_n = flt.breakout_channel_bars
    if n >= chan_n + 2:
        window = klines[-2 - chan_n:-2]
        chan_high = max(float(k[2]) for k in window)
        chan_low = min(float(k[3]) for k in window)
        vr = volume_ratio(klines)
        max_brk = flt.breakout_max_pct
        over_max = (
            (direction == 1 and close_last > chan_high * (1 + max_brk / 100))
            or (direction == -1 and close_last < chan_low * (1 - max_brk / 100))
        ) if max_brk > 0 else False
        if (_is_solid_breakout_candle(klines, direction)
                and vr >= flt.breakout_vol_ratio and not over_max):
            sig_open = datetime.utcfromtimestamp(float(klines[-2][0]) / 1000)
            if direction == 1 and close_last >= chan_high * (1 + flt.breakout_threshold):
                return {
                    "type": BREAKOUT, "direction": direction,
                    "position": "resistance",
                    "breakout_level": chan_high, "candle_open": sig_open,
                    "pattern": None,
                    "breakout_pct": (close_last - chan_high) / chan_high * 100,
                    "reason": f"指标同向·放量{vr:.1f}倍·长实体收盘突破{chan_n}根"
                              f"Donchian上轨({chan_high:.6g})",
                }, _close_op(pending, direction, "breakout")
            if direction == -1 and close_last <= chan_low * (1 - flt.breakout_threshold):
                return {
                    "type": BREAKOUT, "direction": direction,
                    "position": "support",
                    "breakout_level": chan_low, "candle_open": sig_open,
                    "pattern": None,
                    "breakout_pct": (close_last - chan_low) / chan_low * 100,
                    "reason": f"指标同向·放量{vr:.1f}倍·长实体收盘跌破{chan_n}根"
                              f"Donchian下轨({chan_low:.6g})",
                }, _close_op(pending, direction, "breakout")

    # 并行计算当根同向金K（两段式确认证据，形态检测与触点判定解耦）
    patterns = detect_all_patterns(klines, idx=-2)
    golden = next((p for p in patterns if p["pattern"] in GOLDEN_12), None)
    golden = golden if (golden and golden["direction"] == wanted) else None

    # 并行探测启用触点（docs/22 §3）：命中收集为 {ref: 位价}，标注取最高优先
    # （横向结构语义强于均线；均线取最快——最早确认、止损最紧）
    touches: dict[str, float] = {}
    refs = flt.pullback_touch_refs
    if TOUCH_RETEST in refs:
        p = _detect_retest(klines, direction, retest,
                           flt.retest_window_bars, flt.structure_touch_band_pct)
        if p is not None:
            touches[TOUCH_RETEST] = p
    if TOUCH_SWING in refs:
        p = _detect_swing_touch(klines, direction, flt.structure_touch_band_pct)
        if p is not None:
            touches[TOUCH_SWING] = p
    for ref, period in ((TOUCH_EMA21, 21), (TOUCH_EMA55, 55), (TOUCH_EMA144, 144)):
        if ref in refs:
            p = _detect_ema_touch(klines, direction, period, flt)
            if p is not None:
                touches[ref] = p
    touch_ref = next((r for r in TOUCH_ORDER if r in touches), None)
    touch_price = touches.get(touch_ref) if touch_ref else None
    touch_label = TOUCH_LABELS.get(touch_ref, "") if touch_ref else None

    # 行2：当根触碰+金K → 当根确认（比纯形态更强，pending 一律被抢先）。
    if touch_ref is not None and golden:
        return {
            "type": PULLBACK_CONFIRM, "direction": direction,
            "position": pos_fb, "touch_ref": touch_ref,
            "pattern": golden["pattern"], "breakout_pct": 0.0,
            "reason": f"指标同向·回踩{touch_label}({touch_price:.6g})企稳且当根"
                      f"{PATTERN_LABEL_MAP.get(golden['pattern'], golden['pattern'])}确认",
        }, _close_op(pending, direction, "same_candle_confirm")

    # 行3/行4：未触碰但有金K → 有效 pending 则次根确认，否则纯金K（T2 原样）。
    # 悬空金K门槛（2026-09-27 正式网复盘）：信号K线收盘距 EMA21 > 阈值（默认 1.5%）
    # 的纯金K不再触发——正式网 17 笔悬空金K全部 SL（-92 USDT），追高入场负期望。
    # row3 次根确认有回踩语境（上根触碰触点）不受影响。0=关闭。
    # 注意：悬空拦截与触点体系语义分离——深回踩由 ema55/144 触碰确认接住，
    # 不放宽悬空门槛（只在零触碰时生效）。
    if touch_ref is None and golden:
        if pending_valid and pending and pending.get("direction") == direction:
            p_ref = pending.get("touch_ref")
            p_label = TOUCH_LABELS.get(p_ref, "触点") if p_ref else "触点"
            return {
                "type": PULLBACK_CONFIRM, "direction": direction,
                "position": pos_fb, "touch_ref": p_ref,
                "pattern": golden["pattern"], "breakout_pct": 0.0,
                "reason": f"指标同向·上根回踩{p_label}企稳·本根"
                          f"{PATTERN_LABEL_MAP.get(golden['pattern'], golden['pattern'])}确认",
            }, {"op": "confirm"}
        max_dist = flt.candle_max_ema21_dist_pct
        if max_dist > 0:
            closes = np.array([float(k[4]) for k in klines[:-1]], dtype=float)
            e21 = calc_ema(closes, 21)
            if e21 is not None and float(e21[-1]) > 0:
                dist_pct = abs(close_last - float(e21[-1])) / float(e21[-1]) * 100
                if dist_pct > max_dist:
                    # 超距金K不触发；活跃 pending 按方向反转/窗口过期正常处置
                    if pending:
                        op_reason = ("direction_flip"
                                     if pending.get("direction") != direction
                                     else "window_expired")
                        return None, {"op": "expire", "reason": op_reason}
                    return None, None
        return {
            "type": CANDLESTICK, "direction": direction,
            "position": pos_fb,
            "pattern": golden["pattern"], "breakout_pct": 0.0,
            "reason": f"指标同向·{PATTERN_LABEL_MAP.get(golden['pattern'], golden['pattern'])}",
        }, ({"op": "expire",
             "reason": "direction_flip" if pending.get("direction") != direction else "window_expired"}
            if pending else None)

    # 行5：触碰但无金K → 不触发，转 pending 待确认（scanner 侧 upsert）。
    # ema21 键存 Stage-1 触碰锚点价（命中触点的位价），touch_ref 记触点类型
    if touch_ref is not None:
        return None, {
            "op": "upsert", "direction": direction, "touch_ref": touch_ref,
            "stage1_candle_open": datetime.utcfromtimestamp(float(klines[-2][0]) / 1000),
            "ema21": touch_price,
        }

    # 行6/行7：未触碰无金K → 有效 pending 窗口到期；无效 pending 也顺手终态（错过即过期）
    if pending:
        reason = "direction_flip" if pending.get("direction") != direction else "window_expired"
        return None, {"op": "expire", "reason": reason}
    return None, None
