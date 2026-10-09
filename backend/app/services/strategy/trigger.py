"""入场触发器检测（指标驱动筛选管线，2026-09-17；两段式回踩确认 2026-09-25 docs/19；
回踩触点体系 2026-09-28 docs/22；双突破分类+纯金K下线 2026-09-29）

方向由技术指标多数决确定（technical.compute_indicator_votes），本模块只回答
"此刻是否出现与候选方向一致的入场触发"：
- T1a trigger_breakout（结构突破）：收盘越过最近未破的摆动结构前高（做多；
  做空=前低对称）——N 字/趋势延续的结构性破位
- T1b trigger_trend_breakout（趋势突破）：收盘破 N 根 Donchian 滚动高低轨
  （BREAKOUT_CHANNEL_BARS，窗口不含信号K线与未收盘根）——强趋势连续新高/新低
  无未破结构位可越时的追势入场；与 T1a 同轮互斥（结构优先，单币单候选），
  两类共享三重收紧：最小幅度 + 放量 + 长实体短影线 + 追高上限（各自按突破位衡量）
- T3 trigger_pullback_confirm：回踩触点企稳 + 同向金K 两段式确认——
  当根同时触碰+金K（当根确认），或上根触碰（pending）+ 本根金K（次根确认）；
  仅触碰无金K 不再当根触发，转入 pending 待确认池（scanner 持久化）
- 纯金K触发（trigger_candlestick）2026-09-29 下线：金K必须与触点配合，无触碰
  无有效 pending 的金K 一律不触发（正式网复盘 28 笔 6 胜负期望）

回踩触点（docs/22 §2，启用白名单见 ScanFilters.pullback_touch_refs）：
- retest  突破回测位：前高/前低破位价（scanner 维护 breakout_events，T1 触发时记录），
  影线可入带、收盘须守住——回踩不破 = 突破有效性确认
- swing   摆动结构位：最近的 HL（多）/LH（空）防守位，只认结构延续标签
- ema21   趋势动态价值区（原口径，不加门，行为与 docs/19 完全一致）
- ema55/144 中/深回撤：数据门（ema_touch_min_bars）+ 排列门（快线在慢线同侧）
  + 斜率门（近 5 根同向）——走平/错排的慢线是震荡不是回踩
同根多触点命中按优先级标注（横向结构 > 均线；均线取最快）：
retest > swing > ema21 > ema55 > ema144

优先级（决策矩阵，docs/19 §4.1）：T1a/T1b 突破 > 当根确认 > 次根确认 >
触碰转 pending；pending 仅在"恰好下一根K线"有效（开盘时间精确比对）。

position 语义（docs/09 方向派生口径，2026-09-28 关键位移除后回归）：
看涨形态/回踩企稳=support、向上突破/反抽受阻=resistance。
"""
from datetime import datetime
from typing import Optional, Tuple

from app.services.scan_filters import (
    TOUCH_EMA144,
    TOUCH_EMA21,
    TOUCH_EMA21_4H,
    TOUCH_EMA55,
    TOUCH_RETEST,
    TOUCH_SWING,
    ScanFilters,
    resolve_scan_filters,
)
from app.services.strategy import recent_swings
from app.services.strategy.candlestick import (
    PATTERN_LABEL_MAP,
    detect_all_patterns,
    resolve_golden_evidence,
)
from app.services.strategy.ema import calc_ema
from app.services.strategy.indicators import volume_ratio
import numpy as np

# 触发类型 = signal_type（AI 提示词 SIGNAL_TYPE_LABELS 已注册同名标签）
BREAKOUT = "trigger_breakout"                  # 结构突破：越过未破摆动前高/前低
TREND_BREAKOUT = "trigger_trend_breakout"      # 趋势突破：破 N 根 Donchian 滚动轨
PULLBACK = "trigger_pullback"          # 历史类型：两段式上线后不再产生新记录
PULLBACK_CONFIRM = "trigger_pullback_confirm"  # 回踩触点后金K确认（当根/次根）

# 回踩触点标注优先级与中文标签（docs/22 §2；与 scan_filters.ALL_TOUCH_REFS 同源顺序；
# 2026-10-09 加入 4h EMA21——均线取最快原则按更新频率排序：1h EMA21 > 4h EMA21 > 1h EMA55）
TOUCH_ORDER = (
    TOUCH_RETEST, TOUCH_SWING, TOUCH_EMA21, TOUCH_EMA21_4H, TOUCH_EMA55, TOUCH_EMA144,
)
TOUCH_LABELS = {
    TOUCH_RETEST: "突破回测位",
    TOUCH_SWING: "摆动结构位",
    TOUCH_EMA21: "EMA21",
    TOUCH_EMA21_4H: "EMA21(4h)",
    TOUCH_EMA55: "EMA55",
    TOUCH_EMA144: "EMA144",
}


def _touch_ref_label(touch_ref: str, direction: int) -> str:
    """触点方向化措辞（做多=支撑侧语义/做空=压力侧，与前端 touchLabel 同口径）。
    retest 用「前高/前低」（position_ref 拼接「回踩」前缀后语义通顺，不重复「回测」）"""
    if touch_ref == TOUCH_RETEST:
        return "前高" if direction == 1 else "前低"
    if touch_ref == TOUCH_SWING:
        return "摆动低点" if direction == 1 else "摆动高点"
    return TOUCH_LABELS.get(touch_ref, touch_ref)


def _position_ref_of(touch_ref: Optional[str], direction: int) -> Optional[str]:
    """回踩确认的命中位置文案（scan_results.position_ref，前端「位置」列展示）"""
    return f"回踩{_touch_ref_label(touch_ref, direction)}" if touch_ref else None


# 摆动结构位时效门：防守点太老（>40 根前确认）防守意义衰减
SWING_MAX_BARS_AGO = 40

# 突破K线形态门槛（2026-09-30-2 口径，用户拍板）：长实体占据K线主体，影线可有但要短——
# 实体 ≥70% 全长即影线合计 ≤30%（原 75%/25% 放宽，HUMA 个案：影线略长但结构强的突破
# 不再被拒）；防长影毛刺仍由主体占比承担，不做单侧影线额外限制。
# 同口径副本：candlestick.TREND_CANDLE_BODY_RANGE_MIN（趋势K线），改版须同步
BREAKOUT_BODY_RANGE_MIN = 0.70   # 实体/K线全长 ≥ 0.70


def _is_solid_breakout_candle(klines: list[list], direction: int) -> bool:
    """突破K线必须为顺向长实体，影线可有但要短（2026-09-30-2 口径）。

    量化（2026-09-25 突破策略收紧，2026-09-28 放宽影线口径，2026-09-30-2 实体门
    0.75→0.70 再放宽）：
    - 顺向实体：做多要求阳线（close>open），做空要求阴线（close<open）
    - 长实体：|close-open| / (high-low) ≥ 0.70——实体占据 K 线主体，
      影线合计 ≤30% 即天然短影线，不做单侧影线额外限制
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
    return body / rng >= BREAKOUT_BODY_RANGE_MIN


def _pullback_volume_note(klines: list[list]) -> str:
    """回踩金K量能注记（docs/25 §3.1）：补齐「大量」要素，双口径——

    - 量比 = 信号K线量 ÷ 近 20 根中位量（程序唯一口径，与突破路径一致）
    - 较前根 = 信号K线量 vs 前一根（知识库口径「第二根量超前一根为加分项」）

    注入 reason 后随信号落库 → AI 事实包 / 前端 / 复盘快照全链路可见；
    数据不足（volume_ratio 返回 0）或前根量为 0 时静默省略对应段。
    """
    vr = volume_ratio(klines)
    if vr <= 0:
        return ""
    note = f"·量比{vr:.1f}(vs中位)"
    try:
        prev_v = float(klines[-3][5])
        cur_v = float(klines[-2][5])
        if prev_v > 0:
            note += f"·较前根×{cur_v / prev_v:.1f}"
    except (IndexError, TypeError, ValueError):
        pass
    return note


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


def _detect_htf_ema21_touch(
    klines: list[list], htf_klines: list[list], direction: int,
) -> Optional[float]:
    """4h EMA21 跨周期触碰检测（2026-10-09）。

    语义：1h 信号K线（klines[-2]）影线/收盘触及 4h EMA21 水平——大周期动态
    价值区回踩，是 1h EMA21 的跨周期放大版。触碰带语义与 _detect_ema_touch
    完全一致（多头 low ≤ EMA ≤ close，空头对称），不带 band（同 ema21 原口径）。

    不加排列/斜率门：4h 震荡（ranging）与方向冲突已被扫描端闸门（震荡过滤 +
    方向一致门）前置过滤，此处只做触碰判定。4h EMA 取最新已收盘根
    （htf_klines[-1] 为未收盘根，与 analyze_htf_background 同口径剔除）；
    尾根差 ≤4h 对 21 周期 EMA 的影响可忽略，且同一 4h 根内的 1h K线共享
    同一 4h EMA21 快照，口径自洽。
    """
    if len(htf_klines) < 22:
        return None
    closes = np.array([float(k[4]) for k in htf_klines[:-1]], dtype=float)
    e = calc_ema(closes, 21)
    if e is None:
        return None
    ema = float(e[-1])
    if ema <= 0 or not np.isfinite(ema):
        return None
    close_last = float(klines[-2][4])
    high_last = float(klines[-2][2])
    low_last = float(klines[-2][3])
    if direction == 1 and low_last <= ema <= close_last:
        return ema
    if direction == -1 and close_last <= ema <= high_last:
        return ema
    return None


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


def _unbroken_pivot_level(swings: list[dict], klines: list[list], high: bool) -> Optional[float]:
    """最近一个自形成以来未被收盘价突破的摆动极值位（新→旧扫描，2026-09-28）。

    swings 为 recent_swings(klines[:-1]) 的 highs/lows（按 bars_ago 新→旧，相对
    最后一根已收盘根）；摆动点为收盘价口径、两侧各 3 根确认。判定区间 =
    摆动K线后一根 ~ 信号K线前一根（klines[idx+1:-2]）：做高向任一收盘 > 该位
    即前高已破，做空向任一收盘 < 该位即前低已破（收盘恰等于该位是触碰不是破位），
    继续向更旧一个找。全部已破（强趋势连续新高）返回 None——
    无结构前高可破，不触发突破信号。"""
    n_bars = len(klines)
    for s in swings:
        idx = n_bars - 2 - int(s["bars_ago"])
        level = float(s["price"])
        if idx < 0 or idx >= n_bars - 2:
            continue
        seg = [float(k[4]) for k in klines[idx + 1:n_bars - 2]]
        broken = any(c > level for c in seg) if high else any(c < level for c in seg)
        if not broken:
            return level
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


def detect_trigger(
    klines: list[list], direction: int,
    pending: Optional[dict] = None,
    flt: Optional[ScanFilters] = None,
    retest: Optional[dict] = None,
    htf: Optional[list] = None,
) -> Tuple[Optional[dict], Optional[dict]]:
    """检测与候选方向一致的入场触发（两段式决策矩阵，docs/19 §4.1 + docs/22 触点）。

    direction: 1 多 / -1 空（指标多数决的符号）
    pending: 该币活跃 pending 快照（{id, direction, stage1_candle_open, touch_ref}）或 None
    flt（可选）：筛选参数组（2026-09-28 筛选配置上页面）；None=按 settings
    解析（env/代码默认），策略函数不读 DB。
    retest（可选）：scanner 侧活跃破轨事件快照 {price, direction, candle_open}
    （docs/22 §3.1；T1 突破触发时由 scanner 记录 breakout_events）。
    htf（可选）：4h 原始K线（尾根未收盘，与 analyze_htf_background 同源序列）；
    None/过短 → ema21_4h 触点静默跳过（4h 数据缺失不阻断 1h 触点体系）。
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

    # T1 双突破分类（2026-09-29）：同一根信号K线按突破位口径互斥归类，结构优先——
    # ① T1a 结构突破：突破位 = 最近一个自形成以来未被收盘价突破的摆动结构前高
    #    （做多）/前低（做空），收盘越过它才算 N 字/趋势延续的结构性破位；
    # ② T1b 趋势突破：结构突破未命中时回落 20 根 Donchian 滚动轨口径——强趋势
    #    连续新高/新低时摆动位全破（ref=None）无结构可越，恢复 8373eb6 之前的
    #    强趋势追势入场，作为独立类型与结构突破并存（复盘分组对比胜率）。
    # 两类共享闸门：① 最小突破幅度（毛边不算）② 长实体短影线
    # （实体≥70%全长，2026-09-30-2 放宽）；放量为加分项（2026-09-29：≥量比门槛 +5 质量分，
    # 不卡死——缩量突破放行但排序自然落后）；追高上限各自按突破位衡量
    # （越位 >1.5% 视为追高，0=关闭）。
    # 突破返回携带 breakout_level（前高/前低/轨价）与 candle_open（信号K线开盘），
    # scanner 据此维护 breakout_events（回测触点=破位价回测的事实源，docs/22 §4）。
    if n >= 30:
        # 摆动结构取信号K线之前的已收盘K线（klines[:-1]）——信号K线是潜在破位者，
        # 不参与结构检测，否则其极值收盘会杀死邻近 pivot（如破位阴线压掉前低）
        swings = recent_swings(klines[:-1], order=3, n=10)
        vr = volume_ratio(klines)
        max_brk = flt.breakout_max_pct
        thr = flt.breakout_threshold
        sig_open = datetime.utcfromtimestamp(float(klines[-2][0]) / 1000)
        solid = _is_solid_breakout_candle(klines, direction)
        vol_desc = f"放量{vr:.1f}倍·" if vr >= flt.breakout_vol_ratio else ""
        ref = None
        if solid:
            ref = _unbroken_pivot_level(
                swings["highs"] if direction == 1 else swings["lows"],
                klines, high=(direction == 1),
            )
        if ref is not None:
            over_max = (
                (direction == 1 and close_last > ref * (1 + max_brk / 100))
                or (direction == -1 and close_last < ref * (1 - max_brk / 100))
            ) if max_brk > 0 else False
            if not over_max:
                if direction == 1 and close_last >= ref * (1 + thr):
                    return {
                        "type": BREAKOUT, "direction": direction,
                        "position": "resistance",
                        "breakout_level": ref, "candle_open": sig_open,
                        "pattern": None,
                        "position_ref": f"破前高 {ref:.6g}",
                        "breakout_pct": (close_last - ref) / ref * 100,
                        "reason": f"指标同向·{vol_desc}长实体收盘突破前高({ref:.6g})",
                    }, _close_op(pending, direction, "breakout")
                if direction == -1 and close_last <= ref * (1 - thr):
                    return {
                        "type": BREAKOUT, "direction": direction,
                        "position": "support",
                        "breakout_level": ref, "candle_open": sig_open,
                        "pattern": None,
                        "position_ref": f"破前低 {ref:.6g}",
                        "breakout_pct": (close_last - ref) / ref * 100,
                        "reason": f"指标同向·{vol_desc}长实体收盘跌破前低({ref:.6g})",
                    }, _close_op(pending, direction, "breakout")
        # T1b 趋势突破（Donchian 滚动轨，窗口不含信号K线自身与未收盘根，
        # 防"自己破自己"）：结构突破未命中（无未破结构位 / 未达幅度 / 追高拦截）
        # 时的回落口径，2026-09-27 追高复盘的"跳过 T1 判定落回金K分支"改为独立类型
        chan_n = flt.breakout_channel_bars
        if solid and n >= chan_n + 2:
            window = klines[-2 - chan_n:-2]
            chan_high = max(float(k[2]) for k in window)
            chan_low = min(float(k[3]) for k in window)
            over_max = (
                (direction == 1 and close_last > chan_high * (1 + max_brk / 100))
                or (direction == -1 and close_last < chan_low * (1 - max_brk / 100))
            ) if max_brk > 0 else False
            if not over_max:
                if direction == 1 and close_last >= chan_high * (1 + thr):
                    return {
                        "type": TREND_BREAKOUT, "direction": direction,
                        "position": "resistance",
                        "breakout_level": chan_high, "candle_open": sig_open,
                        "pattern": None,
                        "position_ref": f"破轨高 {chan_high:.6g}",
                        "breakout_pct": (close_last - chan_high) / chan_high * 100,
                        "reason": f"指标同向·{vol_desc}长实体收盘突破{chan_n}根"
                                  f"Donchian上轨({chan_high:.6g})",
                    }, _close_op(pending, direction, "breakout")
                if direction == -1 and close_last <= chan_low * (1 - thr):
                    return {
                        "type": TREND_BREAKOUT, "direction": direction,
                        "position": "support",
                        "breakout_level": chan_low, "candle_open": sig_open,
                        "pattern": None,
                        "position_ref": f"破轨低 {chan_low:.6g}",
                        "breakout_pct": (close_last - chan_low) / chan_low * 100,
                        "reason": f"指标同向·{vol_desc}长实体收盘跌破{chan_n}根"
                                  f"Donchian下轨({chan_low:.6g})",
                    }, _close_op(pending, direction, "breakout")

    # 当根金K证据统一裁决（2026-09-29 复合确认；2026-10-01 收窄：仅流星线维持
    # 复合确认（双流星/流星+趋势K），锤形线恢复单根直接确认；2026-10-01-2 倒锤
    # 形线退出看涨白名单——见 candlestick.resolve_golden_evidence；触点框架要求不变）
    patterns = detect_all_patterns(klines, idx=-2)
    golden, golden_note = resolve_golden_evidence(klines, wanted, patterns)

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
    if TOUCH_EMA21_4H in refs and htf:
        p = _detect_htf_ema21_touch(klines, htf, direction)
        if p is not None:
            touches[TOUCH_EMA21_4H] = p
    touch_ref = next((r for r in TOUCH_ORDER if r in touches), None)
    touch_price = touches.get(touch_ref) if touch_ref else None
    touch_label = TOUCH_LABELS.get(touch_ref, "") if touch_ref else None

    # 量能注记（docs/25 §3.1）：回踩金K路径补「大量」要素，双口径见 helper
    vol_note = _pullback_volume_note(klines)

    # 行2：当根触碰+金K → 当根确认（比纯形态更强，pending 一律被抢先）。
    if touch_ref is not None and golden:
        _label = PATTERN_LABEL_MAP.get(golden["pattern"], golden["pattern"])
        # 证据描述：单形态="当根X确认"；组合=note 原文+确认（如
        # "双流星组合确认"/"前根锤形线+当根趋势K确认"）
        _ev = f"当根{_label}确认" if not golden_note else f"{golden_note}确认"
        return {
            "type": PULLBACK_CONFIRM, "direction": direction,
            "position": pos_fb, "touch_ref": touch_ref,
            "position_ref": _position_ref_of(touch_ref, direction),
            "pattern": golden["pattern"], "breakout_pct": 0.0,
            "reason": f"指标同向·回踩{touch_label}({touch_price:.6g})企稳且{_ev}{vol_note}",
        }, _close_op(pending, direction, "same_candle_confirm")

    # 行3/行4：未触碰但有金K → 有效 pending 则次根确认（有回踩语境）。
    # 纯金K独立触发 2026-09-29 下线：正式网复盘纯金K 28 笔 6 胜（-52.15 USDT），
    # 无回踩语境的形态追入负期望——金K必须与触点配合（当根/次根确认），
    # 无有效 pending 的金K 一律不触发。悬空金K门槛（candle_max_ema21_dist_pct，
    # 2026-09-27）随之整体失效，配置键保留向后兼容；活跃 pending 照常终态处置。
    if touch_ref is None and golden:
        if pending_valid and pending and pending.get("direction") == direction:
            p_ref = pending.get("touch_ref")
            p_label = TOUCH_LABELS.get(p_ref, "触点") if p_ref else "触点"
            _label = PATTERN_LABEL_MAP.get(golden["pattern"], golden["pattern"])
            _ev = f"本根{_label}确认" if not golden_note else f"{golden_note}确认"
            return {
                "type": PULLBACK_CONFIRM, "direction": direction,
                "position": pos_fb, "touch_ref": p_ref,
                "position_ref": _position_ref_of(p_ref, direction),
                "pattern": golden["pattern"], "breakout_pct": 0.0,
                "reason": f"指标同向·上根回踩{p_label}企稳·{_ev}{vol_note}",
            }, {"op": "confirm"}
        if pending:
            return None, {"op": "expire",
                          "reason": "direction_flip" if pending.get("direction") != direction
                          else "window_expired"}
        return None, None

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
