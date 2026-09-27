# -*- coding: utf-8 -*-
"""关键位子系统（docs/21，2026-09-27）：多源采集 → ATR 聚类 → 角色状态机。

替代 2026-09-14 移除的旧关键位模块：旧版是「现价在哪侧」的即时几何判定，
假突破（影线插针/收盘穿越后收回）会让失效位立刻被当新角色使用。本模块的
核心差异是**角色状态机**：

    intact ──收盘突破──▶ broken ──回踩不破──▶ flipped_confirmed
                              │超窗未回踩          │收盘反向破位
                              ▼                    ▼
                           invalid（失效）

- 突破用**收盘口径**（±0.1×ATR）：影线插针不算，杜绝假突破直接换角色
- 回踩确认：突破后 ≤KEY_LEVELS_CONFIRM_WINDOW 根内触及原位 ±0.25×ATR 且
  收盘回到突破侧（含「穿透+次根收回」的金K组合确认）
- 候选位四源：1h 摆动点 / 4h 摆动点（大结构）/ Donchian 轨 / 前日高低收盘，
  ATR 容差带内聚类，**多源共振 = 强度**；位上的影线拒绝与组合拒绝（2B 防守
  证据）额外加权
- 纯函数、无 DB 状态：每次分析对现成 K 线重放（500 根毫秒级），不落库

叶子模块：只 import swing/indicators/candlestick/config，不 import strategy
包入口（防循环导入，与 indicators.py 同约定）。
"""
from __future__ import annotations

from bisect import bisect_right
from typing import Optional

import numpy as np

from app.config import settings
from app.services.strategy.indicators import calc_atr
from app.services.strategy.swing import find_swing_points, merge_swings

# 聚类/触及/拒绝/突破的 ATR 系数（docs/21 §4/§5，集中定义防漂移）
_TOL_TOUCH = 0.25      # 触及带半宽（×ATR）：high/low 进带即一次触及
_BRK_CLOSE = 0.10      # 收盘突破阈值（×ATR）：收盘越过位价该距离才算突破
_REJECT_CAP = 2        # 拒绝事件对强度的加分封顶
_COMBO_MAX_GAP = 2     # 组合拒绝窗口（根）：穿透与收回末根的最大间隔
_DONCHIAN_BARS = 20    # Donchian 候选窗口（与 BREAKOUT_CHANNEL_BARS 同参）
_DAY_MS = 86_400_000   # UTC 日毫秒数


# ── 数据层：候选位采集 ────────────────────────────────────────────────

def collect_candidates(klines_1h: list, klines_4h: Optional[list]) -> list[dict]:
    """四源候选位：schema {"source","price","idx_1h","kind","raw"}。

    idx_1h 统一折算到 1h 轴（1h 序列下标）；kind: high|low|close。
    """
    cands: list[dict] = []
    if not klines_1h or len(klines_1h) < 2 * 3 + 1:
        return cands

    # ① 1h 摆动点（收盘价口径，order=3，与 recent_swings 同源同参）
    closes = np.array([float(k[4]) for k in klines_1h], dtype=float)
    hi_idx, lo_idx = find_swing_points(closes, closes, 3)
    for i, kind in ([(i, "high") for i in hi_idx] + [(i, "low") for i in lo_idx]):
        cands.append({"source": "swing_1h", "price": float(closes[i]),
                      "idx_1h": int(i), "kind": kind, "raw": {}})

    # ② 4h 摆动点（order=2，大结构位；open_time bisect 对齐 1h 轴）
    if klines_4h and len(klines_4h) >= 2 * 2 + 1:
        c4 = np.array([float(k[4]) for k in klines_4h], dtype=float)
        t1h = [int(k[0]) for k in klines_1h]
        hi4, lo4 = find_swing_points(c4, c4, 2)
        for i, kind in ([(i, "high") for i in hi4] + [(i, "low") for i in lo4]):
            t4 = int(klines_4h[i][0])
            j = bisect_right(t1h, t4) - 1  # ≤t4 的最近 1h 根
            if j < 0:
                j = min(i * 4, len(klines_1h) - 1)  # 回退：4h=4×1h
            cands.append({"source": "swing_4h", "price": float(c4[i]),
                          "idx_1h": int(j), "kind": kind, "raw": {}})

    # ③ Donchian 轨（滚动窗口极值更新时产出；与 T1 突破同参）
    n1 = len(klines_1h)
    for i in range(_DONCHIAN_BARS, n1):
        win = klines_1h[i - _DONCHIAN_BARS:i]
        hi_i = max(range(len(win)), key=lambda j: float(win[j][2]))
        lo_i = min(range(len(win)), key=lambda j: float(win[j][3]))
        hi, lo = float(win[hi_i][2]), float(win[lo_i][3])
        hi_idx = i - _DONCHIAN_BARS + hi_i  # 极值所在根
        lo_idx = i - _DONCHIAN_BARS + lo_i
        if i == _DONCHIAN_BARS:
            prev_hi, prev_lo = hi, lo
        # 仅当新窗极值突破旧窗（轨道推进）时记录新轨位，idx=极值所在根
        if hi > prev_hi:
            cands.append({"source": "donchian", "price": hi,
                          "idx_1h": int(hi_idx), "kind": "high", "raw": {}})
        if lo < prev_lo:
            cands.append({"source": "donchian", "price": lo,
                          "idx_1h": int(lo_idx), "kind": "low", "raw": {}})
        prev_hi, prev_lo = max(prev_hi, hi), min(prev_lo, lo)

    # ④ 前日高低收盘（UTC 日界切分；仅已完成日）
    days: dict[int, list[int]] = {}
    for i, k in enumerate(klines_1h):
        days.setdefault(int(k[0]) // _DAY_MS, []).append(i)
    day_keys = sorted(days)
    for dk in day_keys[:-1]:  # 最后一组含未收盘根，跳过
        idxs = days[dk]
        hs = [float(klines_1h[i][2]) for i in idxs]
        ls = [float(klines_1h[i][3]) for i in idxs]
        cs = [float(klines_1h[i][4]) for i in idxs]
        last = idxs[-1]
        cands.append({"source": "prev_day", "price": max(hs),
                      "idx_1h": last, "kind": "high", "raw": {}})
        cands.append({"source": "prev_day", "price": min(ls),
                      "idx_1h": last, "kind": "low", "raw": {}})
        cands.append({"source": "prev_day", "price": cs[-1],
                      "idx_1h": last, "kind": "close", "raw": {}})

    # ⑤ 整数关口（默认关：KEY_LEVELS_PSYCHOLOGICAL，防噪音稀释共振）
    if settings.KEY_LEVELS_PSYCHOLOGICAL:
        cands.extend(_psychological_levels(klines_1h))

    return cands


def _psychological_levels(klines_1h: list) -> list[dict]:
    """价格量级自适应步长的整数关口（首位 1/2/5 序列）。

    取全窗口价格范围的 1/1000 步长基准，量化到 1-2-5 序列；
    只保留窗口内实际穿越过的整数位。
    """
    prices = [float(k[4]) for k in klines_1h]
    lo_p, hi_p = min(prices), max(prices)
    if lo_p <= 0 or hi_p <= lo_p:
        return []
    span = hi_p - lo_p
    step = 10 ** int(np.floor(np.log10(span / 100)))
    for mult, m in [(1, 1), (2, 2), (5, 5)]:
        s = step * mult
        if span / s < 3:  # 步长太密（>30 个位）则升档
            continue
        base = int(lo_p / s) * s
        out = []
        v = base
        while v < hi_p:
            if v > lo_p:
                # 找第一个穿越该位的 1h 根（open<位≤close 或反向）
                for i, k in enumerate(klines_1h):
                    o, c = float(k[1]), float(k[4])
                    if (o < v <= c) or (c < v <= o):
                        out.append({"source": "psych", "price": float(v),
                                    "idx_1h": i, "kind": "close", "raw": {}})
                        break
            v += s
        if out:
            return out
    return []


# ── 聚合层：ATR 容差带聚类 ────────────────────────────────────────────

def _is_touch(k: list, price: float, atr: float) -> bool:
    """某根 K 线是否触及位价 ±0.25×ATR 带（影线口径）"""
    band = _TOL_TOUCH * atr
    return (float(k[2]) >= price - band) and (float(k[3]) <= price + band)


def _penetrated(k: list, price: float, atr: float, side: str) -> bool:
    """影线是否穿透位价对侧 ≥0.25×ATR（side=该位对现价的原侧：'below'|'above'）

    支撑位（现价上方说'above'反了——此处 side 指位的防守侧）：
    位下方是防守方的对手区。约定：kind=high/压力位 side='above'（影线向上穿），
    kind=low/支撑位 side='below'（影线向下穿）。
    """
    band = _TOL_TOUCH * atr
    if side == "below":
        return float(k[3]) < price - band
    return float(k[2]) > price + band


def _closed_back(k: list, price: float, side: str) -> bool:
    """收盘是否收回位价防守侧（side=被穿透侧：'below' 穿下则收回上方）"""
    c = float(k[4])
    return c >= price if side == "below" else c <= price


def _pattern_at(klines: list, idx: int, want: str) -> bool:
    """idx 处是否命中 GOLDEN_12 中 want 方向的形态（组合拒绝/确认用）。

    启明星/黄昏星是三根形态，detect_all_patterns(idx) 的三根检测器内部取
    idx-2..idx，无需额外回探（idx 即组合末根）。异常静默跳过（检测器自带
    try/except，此处兜底组合调用）。
    """
    from app.services.strategy.candlestick import detect_all_patterns
    try:
        for p in detect_all_patterns(klines, idx):
            if p.get("direction") == want and p["pattern"] in (
                "hammer", "bullish_engulfing", "morning_star",
                "hanging_man", "bearish_engulfing", "evening_star",
            ):
                return True
    except Exception:
        pass
    return False


def merge_levels(candidates: list[dict], klines_1h: list,
                 atr: float, tol_mult: float) -> list[dict]:
    """ATR 容差带聚类 + 触及/拒绝统计 → Level 列表。

    Level schema：
    {"price"(触及加权均值), "sources"(去重源列表), "touches", "rejections",
     "kind"(high|low|mixed), "strength", "first_idx", "last_idx"}
    """
    if not candidates or atr <= 0:
        return []
    tol = tol_mult * atr
    sorted_c = sorted(candidates, key=lambda c: c["price"])
    clusters: list[list[dict]] = []
    for c in sorted_c:
        if clusters and abs(c["price"] - clusters[-1][0]["price"]) <= tol:
            clusters[-1].append(c)
        else:
            clusters.append([c])

    n1 = len(klines_1h)
    levels: list[dict] = []
    for cl in clusters:
        # 价格 = 候选来源条数加权均值（多源落同带，被市场打磨收敛）
        price = sum(c["price"] for c in cl) / len(cl)
        sources = sorted({c["source"] for c in cl})
        kinds = {c["kind"] for c in cl if c["kind"] in ("high", "low")}
        kind = ("mixed" if {"high", "low"} <= kinds
                else ("high" if "high" in kinds else
                      ("low" if "low" in kinds else "close")))
        first_idx = min(c["idx_1h"] for c in cl)
        last_idx = max(c["idx_1h"] for c in cl)

        # 触及/拒绝统计窗口：位形成（last_idx）之后
        touches = 0
        rejections = 0
        side = "above" if kind in ("high", "mixed") else "below"
        i = last_idx + 1
        while i < n1:
            if not _is_touch(klines_1h[i], price, atr):
                i += 1
                continue
            # 进入触及段：逐根走查（段内后续根仍可能是拒绝，不能整段跳过）
            touched_any = False
            while i < n1 and _is_touch(klines_1h[i], price, atr):
                k = klines_1h[i]
                rej_i = None
                if _penetrated(k, price, atr, side) and _closed_back(k, price, side):
                    rej_i = i  # 单K pin：影线穿透+收盘收回
                else:
                    # 双K组合：本根或下一根穿透，≤2 根内末根收回 + 金K对向形态
                    want = "bullish" if side == "below" else "bearish"
                    for j in range(i + 1, min(i + _COMBO_MAX_GAP, n1 - 1) + 1):
                        kj = klines_1h[j]
                        if _closed_back(kj, price, side) and (
                                _penetrated(k, price, atr, side)
                                or _penetrated(kj, price, atr, side)):
                            if _pattern_at(klines_1h, j, want):
                                rej_i = j
                            break
                        if not _is_touch(kj, price, atr):
                            break  # 组合断裂
                if rej_i is not None:
                    rejections += 1
                    i = rej_i + 1  # 拒绝根不再计触及
                else:
                    touched_any = True
                    i += 1
            if touched_any:
                touches += 1  # 连续触及合并为一次

        strength = len(sources) + min(rejections, _REJECT_CAP)
        levels.append({
            "price": price, "sources": sources, "touches": touches,
            "rejections": rejections, "kind": kind, "strength": strength,
            "first_idx": first_idx, "last_idx": last_idx,
        })
    return levels


# ── 状态层：角色状态机 ────────────────────────────────────────────────

def classify_levels(levels: list[dict], klines_1h: list,
                    atr: float, confirm_window: int) -> list[dict]:
    """对每个关键位沿 1h K 线重放角色状态机（docs/21 §5）。

    状态：intact → broken → flipped_confirmed → invalid（最后事件为准）。
    输出在 merge 字段上补：role/state/flip_direction/bars_since_event/
    distance_pct。
    """
    if atr <= 0:
        return levels
    brk = _BRK_CLOSE * atr
    band = _TOL_TOUCH * atr
    n1 = len(klines_1h)
    cur = float(klines_1h[-1][4])  # 现价（含未收盘根的现价）

    for lv in levels:
        price = lv["price"]
        # 原 kind 决定突破方向语义：high 位被向上破、low 位被向下破
        # （close/psych 位按首现价格与后续区间相对位置归入 high/low——
        #  简化：kind=close 的位归 low（日内收盘常作支撑参照））
        base_kind = lv["kind"] if lv["kind"] in ("high", "low") else "low"

        state = "intact"
        role = None
        flip_dir = None
        event_idx = lv["last_idx"]
        i = lv["last_idx"] + 1

        while i < n1:
            k = klines_1h[i]
            c = float(k[4])
            if state == "intact":
                if c > price + brk:
                    state, flip_dir, event_idx = "broken", 1, i
                elif c < price - brk:
                    state, flip_dir, event_idx = "broken", -1, i
            elif state == "broken":
                # 回踩确认：触及原位带 + 收盘回到突破侧（含组合确认）
                broke_side = "below" if flip_dir == 1 else "above"
                touched = (float(k[3]) <= price + band
                           and float(k[2]) >= price - band)
                back = _closed_back(k, price, broke_side)
                combo = False
                if touched and not back and i + 1 < n1:
                    kn = klines_1h[i + 1]
                    if _closed_back(kn, price, broke_side):
                        want = "bullish" if flip_dir == 1 else "bearish"
                        combo = _pattern_at(klines_1h, i + 1, want)
                if touched and (back or combo):
                    state = "flipped_confirmed"
                    event_idx = i + 1 if combo else i
                    i = event_idx
                if state == "broken" and i - event_idx > confirm_window:
                    state, event_idx = "invalid", i
            elif state == "flipped_confirmed":
                # 收盘反向跌破原位 → 翻转位被证伪
                if (flip_dir == 1 and c < price - brk) or (
                        flip_dir == -1 and c > price + brk):
                    state, event_idx = "invalid", i
            i += 1

        # role：intact 按现价侧（位在现价上方=压力、下方=支撑）；flipped 按
        # 突破方向；broken 保留原角色；invalid 不定角色（仅展示）
        if state == "intact":
            role = "resistance" if cur < price else "support"
        elif state == "flipped_confirmed":
            role = "support" if flip_dir == 1 else "resistance"
        elif state == "broken":
            role = "resistance" if base_kind == "high" else "support"
        else:
            role = None

        lv["state"] = state
        lv["role"] = role
        lv["flip_direction"] = flip_dir if state in ("broken", "flipped_confirmed") else None
        lv["bars_since_event"] = n1 - 1 - event_idx
        lv["distance_pct"] = (price - cur) / cur * 100 if cur > 0 else 0.0
    return levels


# ── 编排器 ────────────────────────────────────────────────────────────

def compute_key_levels(klines_1h: list, klines_4h: Optional[list]) -> list[dict]:
    """关键位全流程：采集 → 聚类 → 状态机 → 过滤排序。

    任何数据不足/开关关闭返回 []（消费方按空表回落旧路径）。
    """
    if not settings.KEY_LEVELS_ENABLED or not klines_1h or len(klines_1h) < 30:
        return []
    atr = calc_atr(klines_1h, 14)
    if not atr or atr <= 0:
        return []
    cands = collect_candidates(klines_1h, klines_4h)
    if not cands:
        return []
    levels = merge_levels(cands, klines_1h, atr, settings.KEY_LEVELS_TOL_ATR)
    levels = [lv for lv in levels
              if len(lv["sources"]) >= settings.KEY_LEVELS_MIN_SOURCES
              or lv["strength"] >= settings.KEY_LEVELS_MIN_SOURCES]
    levels = classify_levels(levels, klines_1h, atr,
                             settings.KEY_LEVELS_CONFIRM_WINDOW)
    levels.sort(key=lambda lv: abs(lv["distance_pct"]))
    return levels


def select_for_ai(levels: list[dict], n_near: int = 3, cap: int = 8) -> list[dict]:
    """AI 事实包位表瘦身（2026-09-27 用户确认口径：验证位+近场兜底）。

    - invalid 直接剔除：锚点解析对所有 purpose 都拒绝、图表不画，纯噪音
    - 验证位保留：强度≥3（共振源+拒绝计分）且触及≥2 次（被市场验证过）
    - 近场兜底：距现价最近 n_near 个其余合格位，保证稀疏行情下结构锚点
      仍有可解析目标（避免 AI 全打回 market 锚）
    - 输入已按 |distance_pct| 升序（compute_key_levels 产出），合并重排后
      截断 cap 条

    事实包块与 P1 锚点解析共用瘦身后的同一张表（消费方注入前调用），
    保证模型看到的价格与程序解析一致。
    """
    if not levels:
        return []
    valid = [lv for lv in levels if (lv.get("state") or "intact") != "invalid"]
    strong = [lv for lv in valid
              if (lv.get("strength") or 0) >= 3 and (lv.get("touches") or 0) >= 2]
    if len(strong) >= cap:
        return strong[:cap]
    chosen = {id(lv) for lv in strong}
    near = [lv for lv in valid if id(lv) not in chosen][:n_near]
    out = sorted(strong + near, key=lambda lv: abs(lv.get("distance_pct") or 0.0))
    return out[:cap]
