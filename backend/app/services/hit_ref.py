"""命中引用装配（2026-10-27，docs/需求文档/15 §19「命中标注归属」）

一条扫描命中（`ScanResult`）的展示口径只有一份，平铺三处使用方：
- 扫描结果行（`ScanResultOut` 派生字段）
- 交易复盘图（`/trades/{id}/klines` 的 `hit`）
- 命中历史列表（`/scans/hit-history`）

派生口径（原为 `api/scan.py` 私有实现，2026-10-27 迁出复用，行为不变）：
- `signal_candle_open`：扫描时刻所在整根；尾窗内（:55~:00）近收盘未收盘根被
  `screen_klines` 视作信号根（幽灵尾根），此时信号根=该整点根，否则=上一根已收盘根
- `hit_direction`：信号族 + position 优先（回踩确认逆 1h 方向票，不能用 votes.net），
  旧/未知行回退方向票净票 → 趋势斜率
- `stage1_candle_open`：回踩路径的触碰K线，经 `pullback_pendings.confirmed_scan_result_id`
  反查（非回踩路径为空）

为什么抽出来：图表要"这次交易的命中标注"，前端就必须能按 `scan_result_id` 单点拿到
信号K线/触碰K线/形态根/方向/滞后；分散在接口里会导致三处口径漂移（历史教训：
`touch_refs_for` 与白名单两套口径）。
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Iterable, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.models.scan import PullbackPending, ScanResult
from app.services import market_data as md

# 信号族 → "该位置为多头" 的映射（trigger.py 方向派生口径）
_POS_LONG_BY_TYPE = {
    "trigger_breakout": "resistance",
    "trigger_trend_breakout": "resistance",
    "trigger_pullback": "support",
    "trigger_pullback_confirm": "support",
}


def signal_candle_open(r: ScanResult, interval: str = "1h") -> Optional[datetime]:
    """信号K线开盘时间（UTC naive）——见模块 docstring"""
    ts = r.signal_created_at or r.created_at
    if ts is None:
        return None
    bar = ts.replace(minute=0, second=0, microsecond=0)
    return bar if md.in_forming_tail(interval, ts) else bar - timedelta(hours=1)


def hit_direction(r: ScanResult) -> int:
    """命中方向：1 多 / -1 空 / 0 未知——见模块 docstring"""
    pos = (r.position or "").strip().lower()
    long_pos = _POS_LONG_BY_TYPE.get((r.signal_type or "").strip().lower())
    if long_pos and pos in ("support", "resistance"):
        return 1 if pos == long_pos else -1
    votes = r.votes_json or {}
    net = votes.get("net")
    if isinstance(net, (int, float)) and net != 0:
        return 1 if net > 0 else -1
    slope = float(r.trend_slope or 0)
    return 1 if slope > 0 else -1 if slope < 0 else 0


def stage1_map(db: Session, scan_result_ids: Iterable) -> dict:
    """批量反查触碰K线：{scan_result_id: stage1_candle_open}（仅回踩路径有值）"""
    ids = [i for i in scan_result_ids if i is not None]
    if not ids:
        return {}
    rows = db.execute(
        select(PullbackPending.confirmed_scan_result_id, PullbackPending.stage1_candle_open)
        .where(PullbackPending.confirmed_scan_result_id.in_(ids))
    ).all()
    return {cid: s1 for cid, s1 in rows}


def entry_lag_warn(r: ScanResult) -> bool:
    """组合确认滞后告警（单点判定，前端只渲染不重算阈值）"""
    v = r.entry_lag_pct
    return v is not None and float(v) >= settings.COMBO_LAG_WARN_PCT


def payload(r: ScanResult, stage1: Optional[datetime] = None) -> dict:
    """单条命中的展示载荷（与 `/scans/hit-history` 的 item 同口径，不含开单摘要）"""
    bar_open = signal_candle_open(r)
    return {
        "id": str(r.id),
        "symbol": r.symbol,
        "created_at": r.created_at.isoformat() if r.created_at else None,
        "signal_candle_open": bar_open.isoformat() if bar_open else None,
        "signal_type": r.signal_type,
        "pattern": r.pattern,
        "golden_combo": bool(r.golden_combo),
        "pattern_candle_open": (
            r.pattern_candle_open.isoformat() if r.pattern_candle_open else None
        ),
        "entry_lag_pct": float(r.entry_lag_pct) if r.entry_lag_pct is not None else None,
        "entry_lag_warn": entry_lag_warn(r),
        "touch_ref": r.touch_ref,
        "position_ref": r.position_ref,
        "quality_score": r.quality_score,
        "is_repeat": bool(r.is_repeat),
        "direction": hit_direction(r),
        "stage1_candle_open": stage1.isoformat() if stage1 else None,
    }


def payload_for(db: Session, scan_result_id) -> Optional[dict]:
    """按 scan_result_id 取单条命中载荷（交易复盘用）；行不存在返回 None"""
    if scan_result_id is None:
        return None
    r = db.get(ScanResult, scan_result_id)
    if r is None:
        return None
    return payload(r, stage1_map(db, [r.id]).get(r.id))
