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

AUTO_RISK_OVERSHOOT = 1.5  # 实亏 ≥1.5R（2026-10-14 拍板，原 1.2）→ 冷却档
AUTO_COOLDOWN_DAYS = 14    # 冷却档天数（到期自动失效；2026-10-14 拍板，原 30 天）
PERMANENT_R_OVERSHOOT = 1.8  # 实亏 ≥1.8R（2026-10-14 拍板，原 1.5）→ 永久档，只能手动移出


def active_symbols(db: Session) -> set[str]:
    """当前生效的黑名单集合：disabled=false 且（manual/stop_hard 永久 / auto 未过期）"""
    now = datetime.utcnow()
    rows = db.execute(select(SymbolBlacklist).where(SymbolBlacklist.disabled == False)).scalars().all()  # noqa: E712
    out: set[str] = set()
    for r in rows:
        if r.origin == "auto" and r.expires_at is not None and r.expires_at <= now:
            continue  # 冷却档到期，自然失效（行保留供历史查看）
        out.add(r.symbol)
    return out


def register_settle(db: Session, symbol: str, planned_risk, realized_pnl, exit_reason: str) -> None:
    """结算后登记（2026-09-18 用户拍板口径）：实际止损金额严重超出计划风险预算
    就自动加入黑名单。

    触发条件（满足其一）：
    - 净亏损 ≥ 计划风险 × 1.5（2026-10-14 拍板，原 1.2）：滑点撑爆止损距离
      （GRIFFAIN 实例 -121.7% R）、资金费/费用侵蚀等「止损没兜住」的情形
      → 冷却 AUTO_COOLDOWN_DAYS 天（2026-10-14 拍板，原 30 天）
    - 净亏损 ≥ 计划风险 × 1.8（2026-10-14 拍板，原 1.5）：止损严重失控 → 永久档
      （origin=stop_hard，expires_at 恒 NULL），只能手动移出
    - exit_reason == liquidation：强平=止损彻底失效，无条件触发（倍数决定档位）

    已在黑名单：冷却档刷新理由与到期；manual 行不动（本就永久，不降级覆盖）；
    升级到永久档则清 expires_at
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
    permanent = overshoot >= PERMANENT_R_OVERSHOOT
    kind = "强平（止损失效）" if exit_reason == "liquidation" else f"止损超预算 {overshoot * 100:.0f}%"
    reason = (
        f"自动：{kind}，计划风险 {planned:.2f} → 实亏 {pnl:.2f}，"
        + ("永久拉黑（≥1.5R），只能手动移出" if permanent
           else f"冷却 {AUTO_COOLDOWN_DAYS} 天")
    )

    existing = db.execute(
        select(SymbolBlacklist).where(SymbolBlacklist.symbol == symbol)
    ).scalar_one_or_none()
    if existing is not None:
        if existing.origin == "manual" and not existing.disabled:
            logger.warning("黑名单已存在 %s（手动永久），保持不变：%s", symbol, kind)
            return
        existing.origin = "stop_hard" if permanent else "auto"
        existing.disabled = False
        existing.reason = reason
        existing.expires_at = None if permanent else datetime.utcnow() + timedelta(days=AUTO_COOLDOWN_DAYS)
        logger.warning("黑名单已存在 %s，刷新：%s", symbol, kind)
        return
    db.add(SymbolBlacklist(
        symbol=symbol,
        origin="stop_hard" if permanent else "auto",
        reason=reason,
        expires_at=None if permanent else datetime.utcnow() + timedelta(days=AUTO_COOLDOWN_DAYS),
    ))
    logger.warning(
        "黑名单自动添加 %s：%s（%s）", symbol, kind,
        "永久档，只能手动移出" if permanent else f"冷却档 {AUTO_COOLDOWN_DAYS} 天")


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
