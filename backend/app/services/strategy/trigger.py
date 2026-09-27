"""入场触发器检测（指标驱动筛选管线，2026-09-17；两段式回踩确认 2026-09-25 docs/19）

方向由技术指标多数决确定（technical.compute_indicator_votes），本模块只回答
"此刻是否出现与候选方向一致的入场触发"：
- T1 trigger_breakout：收盘越过 Donchian 通道（BREAKOUT_CHANNEL_BARS 根高低轨）
  三重收紧：最小幅度 BREAKOUT_THRESHOLD + 放量 BREAKOUT_VOL_RATIO + 长实体短影线形态
- T2 trigger_candlestick：信号K线（klines[-2]）出现 GOLDEN_12 形态且方向一致
- T3 trigger_pullback_confirm：回踩 EMA21 或已确认关键位（docs/21 §8 双对象，
  levels/atr 缺省时回落纯 EMA21）+ 同向金K 两段式确认——
  当根同时触碰+金K（当根确认），或上根触碰（pending）+ 本根金K（次根确认）；
  仅触碰无金K 不再当根触发，转入 pending 待确认池（scanner 持久化）

优先级（决策矩阵，docs/19 §4.1）：T1 > 当根确认 > 次根确认 > 纯金K(T2) >
触碰转 pending；pending 仅在"恰好下一根K线"有效（开盘时间精确比对）。

position 语义（2026-09-27 统一）：按当前关键位口径——信号K线收盘价最近的
（非失效）关键位角色即位置，与关键位块展示同源（位在现价上方=压力、下方=支撑）；
无位表/无有效角色时回落旧约定（向上突破/反抽受阻=resistance、看涨形态/回踩企稳=support）。
"""
from datetime import datetime
from typing import Optional, Tuple

from app.config import settings
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
PULLBACK_CONFIRM = "trigger_pullback_confirm"  # 回踩EMA21后金K确认（当根/次根）

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


def _detect_touch(klines: list[list], direction: int) -> Optional[float]:
    """回踩 EMA21 企稳检测（EMA21 现算，不依赖 analyze_ema 三线）。

    多头=下影触及且收盘收回线上（low ≤ EMA21 ≤ close），空头对称
    （close ≤ EMA21 ≤ high）。命中返回触碰时的 EMA21 值，未命中 None。
    """
    closes = np.array([float(k[4]) for k in klines[:-1]], dtype=float)
    e21 = calc_ema(closes, 21)
    if e21 is None:
        return None
    ema21 = float(e21[-1])
    if ema21 <= 0 or not np.isfinite(ema21):
        return None
    close_last = float(klines[-2][4])
    high_last = float(klines[-2][2])
    low_last = float(klines[-2][3])
    if direction == 1 and low_last <= ema21 <= close_last:
        return ema21
    if direction == -1 and close_last <= ema21 <= high_last:
        return ema21
    return None


def _detect_level_touch(
    klines: list[list], direction: int, levels: list, atr: float,
) -> Optional[tuple[float, dict]]:
    """已确认关键位触碰检测（docs/21 §8）：T3 回踩确认双对象之二。

    候选 = state∈{intact, flipped_confirmed} 且与候选方向同侧（做多回踩现价
    下方支撑位、做空反抽现价上方压力位），按距现价最近排序；信号K线（klines[-2]）
    low/high 进位带（price ± 0.25×ATR）且收盘收回防守侧（做多收盘≥位价、
    做空≤位价）。命中返回 (位价, 位dict)，未命中 None。

    broken（刚突破未确认）与 invalid 一律不参触——假突破回踩正是要避开的入场。
    """
    if not levels or not atr or atr <= 0:
        return None
    close_last = float(klines[-2][4])
    high_last = float(klines[-2][2])
    low_last = float(klines[-2][3])
    band = 0.25 * atr
    cands = []
    for lv in levels:
        if (lv.get("state") or "intact") not in ("intact", "flipped_confirmed"):
            continue
        p = float(lv.get("price") or 0)
        if p <= 0:
            continue
        if direction == 1 and p >= close_last:
            continue
        if direction == -1 and p <= close_last:
            continue
        cands.append((abs(close_last - p), p, lv))
    if not cands:
        return None
    cands.sort(key=lambda t: t[0])
    for _, p, lv in cands:
        if direction == 1:
            if low_last <= p + band and close_last >= p:
                return p, lv
        else:
            if high_last >= p - band and close_last <= p:
                return p, lv
    return None


def _close_op(pending: Optional[dict], direction: int, supersede_reason: str) -> Optional[dict]:
    """pending 终态处置（行1/行2 路径）：方向反转一律 expire(direction_flip)——
    死因是票崩而非被抢先，审计语义优先；同方向被更强触发（T1 突破/当根确认）
    抢先 → supersede。"""
    if not pending:
        return None
    if pending.get("direction") != direction:
        return {"op": "expire", "reason": "direction_flip"}
    return {"op": "supersede", "reason": supersede_reason}


def _position_by_levels(close: float, levels: Optional[list]) -> Optional[str]:
    """信号位置统一按关键位口径（2026-09-27）：距信号K线收盘价最近的
    非失效关键位角色即位置——与关键位块展示同源（intact 位在现价上方=压力、
    下方=支撑；broken 保留原角色；invalid 无角色不参选）。
    无位表/无有效角色时返回 None，调用方回落旧的按方向派生口径。"""
    if not levels:
        return None
    cands = []
    for lv in levels:
        role = lv.get("role")
        if role not in ("support", "resistance"):
            continue
        p = float(lv.get("price") or 0)
        if p > 0:
            cands.append((abs(p - close), role))
    if not cands:
        return None
    cands.sort(key=lambda t: t[0])
    return cands[0][1]


def detect_trigger(
    klines: list[list], direction: int,
    pending: Optional[dict] = None,
    levels: Optional[list] = None,
    atr: Optional[float] = None,
) -> Tuple[Optional[dict], Optional[dict]]:
    """检测与候选方向一致的入场触发（两段式决策矩阵，docs/19 §4.1）。

    direction: 1 多 / -1 空（指标多数决的符号）
    pending: 该币活跃 pending 快照（{id, direction, stage1_candle_open}）或 None
    levels/atr（docs/21 §8，可选）：关键位位表与 1h ATR——提供时 T3 回踩确认
    对象升级为双对象（已确认关键位 OR EMA21），缺省回落纯 EMA21 原行为
    （levels=None 时 _detect_level_touch 短路，老调用方零改动兼容）。
    返回 (trigger, pending_op) 二元组：
    - trigger: 命中的触发 dict（type/direction/position/pattern/breakout_pct/
      reason/dual_touch——仅 row2 当根确认携带，EMA21 与关键位双锚共振标志）
      或 None（无触发 → 观察池）
    - pending_op: 本轮对活跃 pending 的处置，None 或
      {"op": "supersede"|"confirm"|"expire"|"upsert", ...}
      ——upsert/expire 后续在 scanner 侧结合 DB 行补全（方向反转拆两步、
      quality/votes 快照由 scanner 填充），trigger.py 保持无 DB 依赖。

    信号判定全部基于已收盘K线（klines[-2]）；pending 有效期 = 其 stage1_candle_open
    恰为 klines[-3] 的开盘时间（上一根已收盘K线），免疫扫描时刻漂移与轮次缺失。
    """
    n = len(klines)
    if n < 30 or direction not in (1, -1):
        return None, None
    close_last = float(klines[-2][4])
    # 位置统一口径：现价最近关键位角色（无位表/无有效角色回落按方向派生）
    lv_pos = _position_by_levels(close_last, levels)
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
    chan_n = settings.BREAKOUT_CHANNEL_BARS
    if n >= chan_n + 2:
        window = klines[-2 - chan_n:-2]
        chan_high = max(float(k[2]) for k in window)
        chan_low = min(float(k[3]) for k in window)
        vr = volume_ratio(klines)
        max_brk = settings.BREAKOUT_MAX_PCT
        over_max = (
            (direction == 1 and close_last > chan_high * (1 + max_brk / 100))
            or (direction == -1 and close_last < chan_low * (1 - max_brk / 100))
        ) if max_brk > 0 else False
        if (_is_solid_breakout_candle(klines, direction)
                and vr >= settings.BREAKOUT_VOL_RATIO and not over_max):
            if direction == 1 and close_last >= chan_high * (1 + settings.BREAKOUT_THRESHOLD):
                return {
                    "type": BREAKOUT, "direction": direction,
                    "position": lv_pos or "resistance",
                    "pattern": None,
                    "breakout_pct": (close_last - chan_high) / chan_high * 100,
                    "reason": f"指标同向·放量{vr:.1f}倍·长实体收盘突破{chan_n}根"
                              f"Donchian上轨({chan_high:.6g})",
                }, _close_op(pending, direction, "breakout")
            if direction == -1 and close_last <= chan_low * (1 - settings.BREAKOUT_THRESHOLD):
                return {
                    "type": BREAKOUT, "direction": direction,
                    "position": lv_pos or "support",
                    "pattern": None,
                    "breakout_pct": (close_last - chan_low) / chan_low * 100,
                    "reason": f"指标同向·放量{vr:.1f}倍·长实体收盘跌破{chan_n}根"
                              f"Donchian下轨({chan_low:.6g})",
                }, _close_op(pending, direction, "breakout")

    # 并行计算两个确认证据：当根同向金K + 回踩触碰（docs/21 §8 双对象：
    # 已确认关键位优先展示——结构位语义强于动态均线，EMA21 兜底）
    patterns = detect_all_patterns(klines, idx=-2)
    golden = next((p for p in patterns if p["pattern"] in GOLDEN_12), None)
    golden = golden if (golden and golden["direction"] == wanted) else None
    touch_ema = _detect_touch(klines, direction)
    touch_lv = _detect_level_touch(klines, direction, levels, atr) \
        if levels and atr else None
    if touch_lv is not None:
        touch_price = touch_lv[0]
        touch_label = ("翻转位" if touch_lv[1].get("state") == "flipped_confirmed"
                       else "关键位")
    elif touch_ema is not None:
        touch_price, touch_label = touch_ema, "EMA21"
    else:
        touch_price, touch_label = None, None

    # 行2：当根触碰+金K → 当根确认（比纯形态更强，pending 一律被抢先）。
    # dual_touch：EMA21 与关键位双锚共振（2026-09-27）——质量分加权用
    if touch_price is not None and golden:
        return {
            "type": PULLBACK_CONFIRM, "direction": direction,
            "position": lv_pos or pos_fb,
            "pattern": golden["pattern"], "breakout_pct": 0.0,
            "dual_touch": touch_ema is not None and touch_lv is not None,
            "reason": f"指标同向·回踩{touch_label}({touch_price:.6g})企稳且当根"
                      f"{PATTERN_LABEL_MAP.get(golden['pattern'], golden['pattern'])}确认",
        }, _close_op(pending, direction, "same_candle_confirm")

    # 行3/行4：未触碰但有金K → 有效 pending 则次根确认，否则纯金K（T2 原样）。
    # 悬空金K门槛（2026-09-27 正式网复盘）：信号K线收盘距 EMA21 > 阈值（默认 1.5%）
    # 的纯金K不再触发——正式网 17 笔悬空金K全部 SL（-92 USDT），追高入场负期望。
    # 拦截语义天然为「EMA21 与关键位双未触达」（OR 闸门）：触及关键位（带内+防守侧）
    # 时 touch_lv 命中直接走 row2 当根确认、不进本分支——贴结构位的金K距 EMA21
    # 再远也放行；仅拦"离 EMA21 远且不贴任何关键位"的悬空金K。
    # row3 次根确认有回踩语境（上根触碰 EMA21/关键位）不受影响。0=关闭。
    if touch_price is None and golden:
        if pending_valid and pending and pending.get("direction") == direction:
            return {
                "type": PULLBACK_CONFIRM, "direction": direction,
                "position": lv_pos or pos_fb,
                "pattern": golden["pattern"], "breakout_pct": 0.0,
                "reason": f"指标同向·上根回踩企稳·本根"
                          f"{PATTERN_LABEL_MAP.get(golden['pattern'], golden['pattern'])}确认",
            }, {"op": "confirm"}
        max_dist = settings.CANDLE_MAX_EMA21_DIST_PCT
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
            "position": lv_pos or pos_fb,
            "pattern": golden["pattern"], "breakout_pct": 0.0,
            "reason": f"指标同向·{PATTERN_LABEL_MAP.get(golden['pattern'], golden['pattern'])}",
        }, ({"op": "expire",
             "reason": "direction_flip" if pending.get("direction") != direction else "window_expired"}
            if pending else None)

    # 行5：触碰但无金K → 不触发，转 pending 待确认（scanner 侧 upsert）。
    # ema21 键存触碰价（EMA21 值或关键位价，DB 列兼容不迁移）
    if touch_price is not None:
        return None, {
            "op": "upsert", "direction": direction,
            "stage1_candle_open": datetime.utcfromtimestamp(float(klines[-2][0]) / 1000),
            "ema21": touch_price,
        }

    # 行6/行7：未触碰无金K → 有效 pending 窗口到期；无效 pending 也顺手终态（错过即过期）
    if pending:
        reason = "direction_flip" if pending.get("direction") != direction else "window_expired"
        return None, {"op": "expire", "reason": reason}
    return None, None
