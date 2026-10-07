"""虚拟账本服务（docs/16 P-07）——开单/推进/AI标注的共享逻辑层。

设计要点：
- 三价推导与源B同源（backtest_engine.derive_signal_prices），账本口径
  TP2=3R（2026-10-07 用户拍板：出场路径与实盘 AI 单 50%/25%/25% 一致）；
- 扫描挂点/推进任务任何失败都只告警不抛——账本绝不拖累扫描与交易主链路；
- 终局口径沿用 P-04：OPEN/EXPIRED 不进 decided 分母（outcome/r 保持 NULL）。
"""
import logging
from datetime import datetime

from sqlalchemy.orm import Session

from app.config import settings
from app.services.backtest_engine import (
    BACKTEST_ENGINE_VERSION,
    derive_signal_prices,
)

logger = logging.getLogger(__name__)

# 账本三价口径：TP1=1.5R（源B 同款）、TP2=3R（实盘 AI 单典型倍数，
# 25% 尾仓吃满 win_tp2 与 TP2 后跟进收紧的完整生产出场路径）
LEDGER_RR_TP1 = 1.5
LEDGER_TP2_RR = 3.0

# 推进任务参数：单轮处理上限 / 未终局最长账龄（超期 EXPIRED）
ADVANCE_BATCH_LIMIT = 400
LEDGER_MAX_AGE_DAYS = 30


def derive_prices_for_scan(direction_int: int, entry: float, recent5: list) -> dict | None:
    """扫描挂点用三价推导（源B 公式 + 账本口径 TP2=3R）。

    direction_int: +1/-1；entry: 信号K收盘；recent5: 含信号K在内最近 5 根
    已收盘 [[ts,o,h,l,c,v]]。失败返回 None（调用方吞掉）。
    """
    return derive_signal_prices(
        direction_int, entry, recent5,
        rr_tp1=LEDGER_RR_TP1, tp2_rr=LEDGER_TP2_RR,
    )


def open_entry(db: Session, scan_row, snap: dict, *, scan_type: str = "scheduled") -> bool:
    """为一条新 ScanResult 开虚拟单（status=OPEN, source=live）。

    snap: scan_one 挂点产出的 {entry, stop, tp1, tp2, r_unit, direction,
    entry_bar_open_ms}。同 (symbol, signal_type, entry_bar_open) 已有行则跳过
    （手动/定时扫描同根K线、回填边界重叠的唯一约束去重，先到先得）。
    调用方负责 commit 与异常兜底；返回是否新插入。
    """
    from app.models.ledger import VirtualLedger

    entry_bar_open = datetime.utcfromtimestamp(int(snap["entry_bar_open_ms"]) / 1000.0)
    exists = (
        db.query(VirtualLedger.id)
        .filter(
            VirtualLedger.symbol == scan_row.symbol,
            VirtualLedger.signal_type == scan_row.signal_type,
            VirtualLedger.entry_bar_open == entry_bar_open,
        )
        .first()
    )
    if exists:
        return False
    db.add(VirtualLedger(
        scan_result_id=scan_row.id,
        symbol=scan_row.symbol,
        direction=snap["direction"],
        source="live",
        signal_type=scan_row.signal_type,
        touch_ref=scan_row.touch_ref,
        position=scan_row.position,
        ema_state=scan_row.ema_state,
        pattern=scan_row.pattern,
        quality_score=scan_row.quality_score,
        votes_json=scan_row.votes_json,
        snapshot_json={"scan_type": scan_type},
        entry_price=float(snap["entry"]),
        stop_price=float(snap["stop"]),
        tp1_price=float(snap["tp1"]),
        tp2_price=float(snap["tp2"]),
        r_unit=float(snap["r_unit"]),
        entry_bar_open=entry_bar_open,
        status="OPEN",
        rule_version=getattr(settings, "RULE_VERSION", None),
        engine_version=BACKTEST_ENGINE_VERSION,
    ))
    return True


def insert_signal(db: Session, sig: dict, *, source: str = "backfill") -> bool:
    """源B 信号 dict（build_signals_by_scan_replay 输出）开虚拟单。

    回填路径：scan_result_id=None，特征从 sig.meta 取；同
    (symbol, signal_type, entry_bar_open) 已有行（live 或先行回填）跳过——
    边界去重不依赖部署时刻水位，可重入。返回是否新插入。
    """
    from app.models.ledger import VirtualLedger

    meta = sig.get("meta") or {}
    entry_bar_open = datetime.utcfromtimestamp(
        (int(sig["entry_time_ms"]) - 3_600_000) / 1000.0)  # 收盘即入场 → 信号K开盘
    signal_type = meta.get("signal_type") or "unknown"
    exists = (
        db.query(VirtualLedger.id)
        .filter(
            VirtualLedger.symbol == sig["symbol"],
            VirtualLedger.signal_type == signal_type,
            VirtualLedger.entry_bar_open == entry_bar_open,
        )
        .first()
    )
    if exists:
        return False
    db.add(VirtualLedger(
        symbol=sig["symbol"],
        direction=sig["direction"],
        source=source,
        signal_type=signal_type,
        touch_ref=meta.get("touch_ref"),
        quality_score=meta.get("quality_score"),
        votes_json={"net": meta.get("votes_net")} if meta.get("votes_net") is not None else None,
        snapshot_json={"src": meta.get("src") or "scan"},
        entry_price=float(sig["entry"]),
        stop_price=float(sig["stop"]),
        tp1_price=float(sig["tp1"]),
        tp2_price=float(sig["tp2"]),
        r_unit=float(sig["r_unit"]),
        entry_bar_open=entry_bar_open,
        status="OPEN",
        rule_version=getattr(settings, "RULE_VERSION", None),
        engine_version=BACKTEST_ENGINE_VERSION,
    ))
    return True


def attach_ai(
    scan_result_id, decision: str | None, recommendation: float | None,
    db: Session | None = None,
) -> int:
    """AI 分析结论回填账本行（N-02 校准数据；重分析 latest-wins 覆盖）。

    db 缺省自建独立 session（ai_tasks 调用传其自身 db 亦可）；失败只告警。
    返回更新行数。
    """
    owned = db is None
    if owned:
        from app.database import SessionLocal
        db = SessionLocal()
    try:
        from app.models.ledger import VirtualLedger
        n = (
            db.query(VirtualLedger)
            .filter(VirtualLedger.scan_result_id == scan_result_id)
            .update({
                VirtualLedger.ai_decision: decision,
                VirtualLedger.ai_recommendation: recommendation,
            }, synchronize_session=False)
        )
        db.commit()
        return n
    except Exception as e:
        db.rollback()
        logger.warning("账本 AI 标注回填失败（忽略）%s: %s", scan_result_id, e)
        return 0
    finally:
        if owned:
            db.close()
