"""币种黑名单服务（2026-09-18）：手动 + 自动（止损金额严重超预算）双来源

- active_symbols()：扫描宇宙 / AI 分析 / 开仓前的过滤集合（expired/disabled 自动剔除）
- register_settle()：结算后调用——实际亏损严重超出计划风险预算
  （≥ AUTO_RISK_OVERSHOOT 倍，含强平）自动加入，冷却 AUTO_COOLDOWN_DAYS 天
- 手动添加/移除由 API 层直接调本模块 add_manual/remove
"""
import logging
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.blacklist import SymbolBlacklist

logger = logging.getLogger(__name__)

AUTO_RISK_OVERSHOOT = 1.2  # 实际亏损达计划风险预算的 1.2 倍（超 20%）视为严重超出
AUTO_COOLDOWN_DAYS = 30    # 自动黑名单冷却天数（到期自动失效）


def active_symbols(db: Session) -> set[str]:
    """当前生效的黑名单集合：disabled=false 且（manual 永久 / auto 未过期）"""
    now = datetime.utcnow()
    rows = db.execute(select(SymbolBlacklist).where(SymbolBlacklist.disabled == False)).scalars().all()  # noqa: E712
    out: set[str] = set()
    for r in rows:
        if r.origin == "auto" and r.expires_at is not None and r.expires_at <= now:
            continue  # 自动冷却到期，自然失效（行保留供历史查看）
        out.add(r.symbol)
    return out


def register_settle(db: Session, symbol: str, planned_risk, realized_pnl, exit_reason: str) -> None:
    """结算后登记（2026-09-18 用户拍板口径）：实际止损金额严重超出计划风险预算
    就自动加入黑名单，冷却 AUTO_COOLDOWN_DAYS 天。

    触发条件（满足其一）：
    - 净亏损 ≥ 计划风险 × 1.2：滑点撑爆止损距离（GRIFFAIN 实例 -121.7% R）、
      资金费/费用侵蚀、强平（-126.4% R）等「止损没兜住」的情形
    - exit_reason == liquidation：强平=止损彻底失效，无条件拉黑

    已在黑名单则刷新理由与到期时间（不重复建行）
    """
    try:
        planned = float(planned_risk or 0)
        pnl = float(realized_pnl or 0)
    except (TypeError, ValueError):
        return
    if planned <= 0 or pnl >= 0:
        return  # 盈利单/无预算，不涉及
    overshoot = -pnl / planned
    if exit_reason != "liquidation" and overshoot < AUTO_RISK_OVERSHOOT:
        return
    kind = "强平（止损失效）" if exit_reason == "liquidation" else f"止损超预算 {overshoot * 100:.0f}%"
    reason = f"自动：{kind}，计划风险 {planned:.2f} → 实亏 {pnl:.2f}，冷却 {AUTO_COOLDOWN_DAYS} 天"

    existing = db.execute(
        select(SymbolBlacklist).where(SymbolBlacklist.symbol == symbol)
    ).scalar_one_or_none()
    if existing is not None:
        existing.origin = "auto"
        existing.disabled = False
        existing.reason = reason
        existing.expires_at = datetime.utcnow() + timedelta(days=AUTO_COOLDOWN_DAYS)
        logger.warning("黑名单已存在 %s，刷新：%s", symbol, kind)
        return
    db.add(SymbolBlacklist(
        symbol=symbol,
        origin="auto",
        reason=reason,
        expires_at=datetime.utcnow() + timedelta(days=AUTO_COOLDOWN_DAYS),
    ))
    logger.warning("黑名单自动添加 %s：%s，冷却 %d 天", symbol, kind, AUTO_COOLDOWN_DAYS)


def add_manual(db: Session, symbol: str, reason: str = "") -> SymbolBlacklist:
    """手动添加（永久）：已存在则恢复启用并转 manual"""
    existing = db.execute(
        select(SymbolBlacklist).where(SymbolBlacklist.symbol == symbol)
    ).scalar_one_or_none()
    if existing is not None:
        existing.origin = "manual"
        existing.disabled = False
        existing.expires_at = None
        existing.reason = reason or existing.reason
        return existing
    row = SymbolBlacklist(symbol=symbol, origin="manual", reason=reason or "手动添加")
    db.add(row)
    return row


def remove(db: Session, symbol: str) -> bool:
    """手动移除（删行）：黑名单是过滤用途，无历史保留价值"""
    row = db.execute(
        select(SymbolBlacklist).where(SymbolBlacklist.symbol == symbol)
    ).scalar_one_or_none()
    if row is None:
        return False
    db.delete(row)
    return True
