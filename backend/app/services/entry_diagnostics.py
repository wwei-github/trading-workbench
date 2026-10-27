"""入场诊断快照（docs/需求文档/15-实盘止损归因与上线包：B1 + O1，2026-10-27）

本模块只算不判：不拦截任何开仓，只产出可落库的诊断字段；是否拦截由
trade_engine 按 `settings.ENTRY_POS_MIN_RATIO` 决定。取不到数据一律返回
None（调用方按"无诊断"处理），绝不影响下单主链路。

为什么要记这些（2026-10-27 归因复盘代价）：
    - `ai_snapshot->>'tp1'` 线上 56/56 全空 → 计划盈亏比当场算不出来，
      只能拿 `take_profit_1` 列反推；故 O1 直接落 `planned_rr`。
    - `pos`（成交价在信号 K 的位置）与 `room_r`（到前 24h 极值的距离）
      过去只能拿 1h K 线离线重算，且"信号 K"是哪一根要先猜（尾窗/扫描
      时刻偏移）；故开仓时刻按系统自己的 K 线口径落快照。
    - `quality_total`/`range_flag`/`shadow` 原本埋在本单关联的
      `scan_results.quality_parts` JSON 里，随时间淘汰后无法回看。

口径（与 scanner 一致）：
    信号 K = `klines[:-1]` 的最后一根（最后一根未收盘根不计入）；
    ATR14 取信号 K 及之前 14 根真实波幅均值（Wilder 简化版=简单平均，
    与 `strategy.technical.atr` 的样本口径一致，仅用于分档统计）；
    room_r 为正=还有空间，为负=入场价已越过前 24h 极值（突破追单常见）。
"""
from __future__ import annotations

from typing import Any, Optional, Sequence

# Binance 1h K 线行布局：[open_time, open, high, low, close, volume, ...]
_H, _L, _C = 2, 3, 4


def closed_bars(klines: Optional[Sequence[Sequence[Any]]]) -> list[Sequence[Any]]:
    """去掉最后一根未收盘 K（不足 2 根时原样返回，供上层按 ATR 不足处理）。"""
    if not klines:
        return []
    return list(klines[:-1]) if len(klines) >= 2 else list(klines)


def bar_position(klines: Optional[Sequence[Sequence[Any]]],
                 price: Optional[float]) -> Optional[float]:
    """成交价在信号 K（最后一根已收盘 K）中的位置。

    0 = 收在最低、1 = 收在最高；>1 / <0 表示成交价已越出该 K 线范围
    （限价单在反向极值外成交、或市价单吃到下一根的开盘跳空）。
    """
    bars = closed_bars(klines)
    if not bars or price is None:
        return None
    try:
        high, low = float(bars[-1][_H]), float(bars[-1][_L])
        px = float(price)
    except (TypeError, ValueError, IndexError):
        return None
    if high <= low or px <= 0:
        return None
    return (px - low) / (high - low)


def average_true_range(klines: Optional[Sequence[Sequence[Any]]],
                       period: int = 14) -> Optional[float]:
    """ATR(period) 简单平均（样本=已收盘 K，需 ≥period+1 根）。"""
    bars = closed_bars(klines)
    if period <= 0 or len(bars) < period + 1:
        return None
    trs: list[float] = []
    for prev, cur in zip(bars[-period - 1:-1], bars[-period:]):
        try:
            high, low = float(cur[_H]), float(cur[_L])
            prev_close = float(prev[_C])
        except (TypeError, ValueError, IndexError):
            return None
        trs.append(max(high - low, abs(high - prev_close), abs(low - prev_close)))
    if not trs:
        return None
    return sum(trs) / len(trs)


def room_r(klines: Optional[Sequence[Sequence[Any]]],
           price: Optional[float], stop_dist: Optional[float],
           direction: Optional[str], hours: int = 24) -> Optional[float]:
    """到入场前 `hours` 根 1h K 极值的距离，按止损距离折成 R。

    多头看前高（room = 前高 − 入场价）、空头看前低（入场价 − 前低）；
    负值 = 入场价已越过前极值。止损距离缺失/为 0 时返回 None。
    """
    if price is None or not stop_dist or stop_dist <= 0 or direction not in ("long", "short"):
        return None
    bars = closed_bars(klines)[-hours:]
    if not bars:
        return None
    try:
        highs = [float(b[_H]) for b in bars]
        lows = [float(b[_L]) for b in bars]
    except (TypeError, ValueError, IndexError):
        return None
    if direction == "long":
        return (max(highs) - float(price)) / stop_dist
    return (float(price) - min(lows)) / stop_dist


def planned_rr(entry: Optional[float], stop: Optional[float],
               tp1: Optional[float]) -> Optional[float]:
    """计划盈亏比 = |TP1−入场| ÷ |入场−止损|（三价缺失或止损为 0 返回 None）。"""
    try:
        entry_f, stop_f, tp1_f = float(entry), float(stop), float(tp1)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    stop_dist = abs(entry_f - stop_f)
    if stop_dist <= 0 or tp1_f <= 0:
        return None
    return round(abs(tp1_f - entry_f) / stop_dist, 4)


def _quality_fields(quality_parts: Optional[dict]) -> dict:
    """从 `scan_results.quality_parts` 摘出复盘用字段（缺失为 None）。"""
    parts = quality_parts or {}
    total = parts.get("total")
    shadow = parts.get("shadow")
    try:
        total_f = float(total) if total is not None else None
    except (TypeError, ValueError):
        total_f = None
    return {
        "quality_total": total_f,
        # range<0 即系统自己判定"震荡扣分"（scanner 侧 RANGE_SCORE_REJECT=1
        # 只拦突破类，回踩类仅扣分，故本单仍可能带标记成交）
        "range_flag": bool(parts.get("range") is not None),
        "range_score": parts.get("range"),
        "shadow": shadow,
    }


def snapshot(*, klines: Optional[Sequence[Sequence[Any]]],
             price: Optional[float], stop_loss: Optional[float],
             direction: Optional[str], tp1: Optional[float],
             entry_price: Optional[float] = None,
             entry_path: Optional[str] = None,
             quality_parts: Optional[dict] = None,
             signal_type: Optional[str] = None,
             interval: Optional[str] = None) -> dict:
    """组装一条开仓诊断快照（可直接塞进 `trade_records.raw` / 挂单 `raw`）。

    price=成交口径价（市价单=现价、挂单=限价），stop_loss=止损价，
    entry_price=AI 计划入场价（缺省同 price，仅用于 planned_rr）。
    任何一项算不出来都只是该键为 None，不抛异常。
    """
    bars = closed_bars(klines)
    ref = entry_price if entry_price is not None else price
    stop_dist: Optional[float] = None
    if ref is not None and stop_loss is not None:
        try:
            stop_dist = abs(float(ref) - float(stop_loss))
        except (TypeError, ValueError):
            stop_dist = None
    atr = average_true_range(klines)
    stop_atr = (round(stop_dist / atr, 3)
                if (atr and atr > 0 and stop_dist and stop_dist > 0) else None)
    pos = bar_position(klines, price)
    room = room_r(klines, price, stop_dist, direction)
    snap: dict = {
        "entry_path": entry_path,
        "signal_type": signal_type,
        "interval": interval,
        "bar_time": (bars[-1][0] if bars else None),
        "pos": (round(pos, 4) if pos is not None else None),
        "stop_atr": stop_atr,
        "stop_dist": (round(float(stop_dist), 8) if stop_dist else None),
        "room_r": (round(room, 3) if room is not None else None),
        "planned_rr": planned_rr(ref, stop_loss, tp1),
    }
    snap.update(_quality_fields(quality_parts))
    return snap
