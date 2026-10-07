"""P-09 事件驱动执行——动作层（docs/16，2026-10-07）。

user-data-stream 成交事件 → 秒级触发现有结算链路；mark price 推流 → 分钟级
刷新在跑单盈亏快照 + 爆仓距离预警。

铁律：
- 不产生任何新策略语义——TP1 后保本移损/TP2 后跟进轨/仓位归零结算全部由
  settle_trades 既有分支推断执行（交易所为真源，零新簿记）；本模块只负责
  「把下一次巡检提前到秒级」。mark price 只写快照字段，绝不触发动作。
- 与 30 分钟巡检/即时开仓/手动平仓严格互斥：复刻 run_auto_trade_task 的
  全账号锁集合模式；等锁失败放弃 → 轮询兜底（最坏回到现状节奏）。
- 失败安全：任何异常只记日志返回 False，绝不向上抛（事件流线程调用方不中断）。
"""
import logging
import threading
import time
from typing import Optional
from uuid import uuid4

from sqlalchemy import select

from app.config import settings
from app.database import SessionLocal
from app.models.trade import TradeRecord
from app.services.binance_trader import BinanceTrader
from app.services.trade_locks import OPEN_LOCK_TTL_S, acquire_open_lock, release_open_lock

logger = logging.getLogger(__name__)

FILL_DEBOUNCE_S = 3       # 同 (账号, 币) 成交事件冷却：部分成交/多腿事件风暴兜底
MARK_REFRESH_S = 60       # 同 (账号, 币) mark price 快照刷新节流
LIQ_WARN_PCT = 0.10       # 爆仓距离预警：mark 距强平价 <10%
LIQ_WARN_COOLDOWN_S = 3600
_LOCK_WAIT_S = 10         # 事件结算等锁上限（巡检一轮通常 1-2 分钟内持锁）

_RUNNING = ("OPENED", "TP1_HIT", "TP2_HIT")

# 冷却表：key → 上次放行的 monotonic 时间（线程安全；事件流多线程共用）
_cooldowns: dict[str, float] = {}
_cooldown_mu = threading.Lock()


def _cooldown_ok(key: str, seconds: float) -> bool:
    """冷却闸门：key 距上次放行 < seconds → False；放行则记录时间戳"""
    now = time.monotonic()
    with _cooldown_mu:
        last = _cooldowns.get(key)
        if last is not None and now - last < seconds:
            return False
        _cooldowns[key] = now
        return True


def reset_cooldowns() -> None:
    """清空冷却表（测试辅助）"""
    with _cooldown_mu:
        _cooldowns.clear()


def _running_record_id(db, account_key: str, symbol: str):
    return db.execute(
        select(TradeRecord.id).where(
            TradeRecord.account_key == account_key,
            TradeRecord.symbol == symbol,
            TradeRecord.status.in_(_RUNNING),
        ).limit(1)
    ).scalar()


def _all_lock_keys(db) -> list[str]:
    """全账号锁集合 = 已配置账号 ∪ 在跑记录账号（run_auto_trade_task 同款：
    切走账号的存量单仍在结算范围，锁必须覆盖避免并发窗口）"""
    from app.services.account_registry import managed_accounts

    keys = [a.key for a in managed_accounts(db)]
    run_keys = [
        (k or ("testnet-1" if t else "live-1"))
        for k, t in db.execute(
            select(TradeRecord.account_key, TradeRecord.testnet).where(
                TradeRecord.status.in_(_RUNNING),
            )
        ).all()
    ]
    return list(dict.fromkeys(keys + sorted(set(run_keys))))


def react_to_fill(account_key: str, symbol: str) -> bool:
    """平仓侧成交事件反应：秒级触发该账号结算链路。返回是否实际执行结算。

    - 该 (账号, 币) 无在跑记录 → False（开仓侧成交/无关事件零成本返回）
    - 3s 去抖：TP 限价单分次成交/多腿同轮成交的事件风暴只放行首个
    - 全账号锁拿齐才结算（与巡检/开仓/手动平仓互斥）；等锁失败放弃，
      轮询兜底——事件路径永不阻塞、永不重试
    - 结算复用 settle_trades：TP1_FILL+保本移损/TP2_FILL+跟进轨/归零结算/
      外部平仓识别全按既有分支推断
    """
    db = SessionLocal()
    try:
        if _running_record_id(db, account_key, symbol) is None:
            return False
        if not _cooldown_ok(f"fill:{account_key}:{symbol}", FILL_DEBOUNCE_S):
            return False

        from redis import Redis
        r = Redis.from_url(settings.REDIS_URL, decode_responses=True)
        ident = f"event-{uuid4().hex[:8]}"
        lock_keys = _all_lock_keys(db)
        acquired: list[str] = []
        for k in lock_keys:
            if acquire_open_lock(r, ident, wait_s=_LOCK_WAIT_S, ttl_s=OPEN_LOCK_TTL_S,
                                 account=k):
                acquired.append(k)
            else:
                break
        if len(acquired) < len(lock_keys):
            logger.info("事件结算 %s/%s 放弃：开仓锁被占用（轮询兜底）", account_key, symbol)
            return False
        try:
            # 锁内重查（等锁期间记录可能已被巡检结算）
            if _running_record_id(db, account_key, symbol) is None:
                return False
            trader = BinanceTrader(account_key)
            if not trader.configured:
                logger.warning("事件结算 %s/%s 跳过：账号凭据未配置", account_key, symbol)
                return False
            from app.services.trade_engine import settle_trades
            settled, _advice = settle_trades(db, trader)
            logger.info("事件触发结算 %s/%s：%d 笔（user-data-stream 成交事件）",
                        account_key, symbol, settled)
            return True
        finally:
            for k in acquired:
                release_open_lock(r, ident, account=k)
    except Exception:
        logger.exception("事件结算 %s/%s 失败（轮询兜底）", account_key, symbol)
        return False
    finally:
        db.close()


def remaining_qty(rec: TradeRecord) -> float:
    """按分批结构推导剩余仓位数量（USDT-M 线性，uPnL 自算分母）。

    rec.qty 是开仓量；TP1/TP2 部分止盈后剩余仓 = 开仓量 − 已止盈档。档量取
    开仓时记录的 qty_tp1/qty_tp2（计划量，交易所分批成交以此为锚）；
    raw 缺档时按状态回退（TP2_HIT 后剩余 25% 计划仓）。
    """
    qty = float(rec.qty or 0)
    raw = rec.raw or {}
    q1 = raw.get("qty_tp1")
    q2 = raw.get("qty_tp2")
    if q1 is None and q2 is None:
        if rec.status == "TP2_HIT":
            return qty * 0.25
        if rec.status == "TP1_HIT":
            return qty * 0.5 if rec.tp2 else qty * 0.25
        return qty
    remain = qty - float(q1 or 0) - float(q2 or 0)
    return max(remain, 0.0)


def liquidation_warning(rec: TradeRecord, mark_price: float) -> Optional[str]:
    """爆仓距离判定（纯函数）：返回告警文案或 None。

    mark 距强平价 <10% 告警（liq 价来自巡检顺带落库的 raw.liq_price 快照，
    缺失 → 不判定——绝不为此增发 REST）。
    """
    liq = float((rec.raw or {}).get("liq_price") or 0)
    if liq <= 0 or mark_price <= 0:
        return None
    if abs(mark_price - liq) / liq >= LIQ_WARN_PCT:
        return None
    return (f"⚠️ {rec.symbol} 现价 {mark_price:.6g} 距强平价 {liq:.6g} "
            f"不足 {LIQ_WARN_PCT:.0%}，请关注保证金")


def refresh_mark_pnl(account_key: str, symbol: str, mark_price: float) -> bool:
    """mark price 推流 → 刷新在跑单盈亏快照（节流 60s/币）。返回是否实际刷新。

    口径：USDT-M 线性自算 uPnL = ±(mark − entry) × 剩余仓（与交易所
    positionRisk 的 unrealized_pnl 差异在已实现部分的记账路径，快照用途下
    可接受；巡检每 30 分钟以交易所值覆盖校准）。峰值只增；pnl_pct 与巡检
    同公式（risk_amount 分母）。只写快照字段，不触发任何动作。
    """
    if mark_price <= 0:
        return False
    if not _cooldown_ok(f"mark:{account_key}:{symbol}", MARK_REFRESH_S):
        return False
    db = SessionLocal()
    try:
        rec = db.execute(
            select(TradeRecord).where(
                TradeRecord.account_key == account_key,
                TradeRecord.symbol == symbol,
                TradeRecord.status.in_(_RUNNING),
            ).order_by(TradeRecord.opened_at.desc()).limit(1)
        ).scalar()
        if rec is None or not rec.entry_price or not rec.qty:
            return False
        sign = 1.0 if rec.direction == "long" else -1.0
        unrealized = round(sign * (mark_price - float(rec.entry_price)) * remaining_qty(rec), 6)
        rec.unrealized_pnl = unrealized
        total = float(rec.realized_pnl or 0) + unrealized
        if total > float(rec.peak_total_pnl or 0):
            rec.peak_total_pnl = round(total, 6)
        if rec.risk_amount and float(rec.risk_amount) > 0:
            rec.pnl_pct = round(total / float(rec.risk_amount) * 100, 2)
        warn = liquidation_warning(rec, mark_price)
        db.commit()
        if warn and _cooldown_ok(f"liqwarn:{rec.symbol}", LIQ_WARN_COOLDOWN_S):
            logger.warning(warn)
        return True
    except Exception:
        db.rollback()
        logger.exception("mark price 快照刷新失败 %s/%s", account_key, symbol)
        return False
    finally:
        db.close()
