# -*- coding: utf-8 -*-
"""冻结的旧 4h 闸门 gate_4h 参考实现（2026-10-20 随合并从 ema_gate 删除）。

**仅供离线审计脚本使用**，不属于生产代码，不得被 app/ 引用：
- htf_gate_state_diff.py：合并 Golden Diff 的「旧侧」参照（htf_context vs 旧链路）；
- htf_gate_state_conflict_replay.py：gate×状态机冲突统计的「旧侧」。

生产链路已统一到 technical.htf_context 单入口（gate_4h 三重否决拆解为
「走平=独立拒因 flat / 缠绕=detect_tangle 统一判据 / ADX=缠绕第一道」）。
本函数口径与删除前 ema_gate.gate_4h 逐行一致（阈值直接复用 technical 的
合并后常量，避免常量二次漂移）：
数据门（已收盘 ≥200）→ 走平（EMA144 近 SLOPE_BARS 根变化 <0.5%）→ 缠绕
（近 10 根收盘价穿越 EMA144 >2 次）→ ADX<20 → 方向（价×EMA144 位置 + 斜率同号）。
"""
import numpy as np

from app.services.strategy import technical as tech


def legacy_gate_4h(k4: list, core: dict = None):
    """旧 gate_4h：(direction "long"/"short", 证据 dict) 或 None（闸门不过）。"""
    core = core or tech.htf_core(k4)
    if core is None:
        return None
    closed = core["closed"]
    if len(closed) < tech.MIN_CLOSED_4H:
        return None
    c = core["closes"]
    e144 = core["e144"]
    if not np.isfinite(e144[-1]) or not np.isfinite(e144[-tech.SLOPE_BARS]):
        return None
    price = core["price"]
    slope_pct = abs(float(e144[-1]) - float(e144[-tech.SLOPE_BARS])) / price
    wraps = tech._wrap_count(c, e144)
    adx_val = core["adx"]
    ev = {"slope_pct": round(slope_pct, 5), "wraps": wraps,
          "adx": round(adx_val, 1) if adx_val is not None else None,
          "close_vs_ema144": round(price / float(e144[-1]) - 1, 5)}
    if slope_pct < tech.SLOPE_FLAT_PCT:
        return None
    if wraps > tech.TANGLE_WRAP_MAX:
        return None
    if adx_val is None or adx_val < tech.TANGLE_ADX_MIN:
        return None
    up = price > e144[-1] and e144[-1] >= e144[-tech.SLOPE_BARS]
    down = price < e144[-1] and e144[-1] <= e144[-tech.SLOPE_BARS]
    if up:
        return "long", {**ev, "direction": "up"}
    if down:
        return "short", {**ev, "direction": "down"}
    return None
