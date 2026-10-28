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

from app.config import settings
from app.models.blacklist import SymbolBlacklist

logger = logging.getLogger(__name__)

AUTO_RISK_OVERSHOOT = 1.5  # 实亏 ≥1.5R（2026-10-14 拍板，原 1.2）→ 冷却档
AUTO_COOLDOWN_DAYS = 14    # 冷却档天数（到期自动失效；2026-10-14 拍板，原 30 天）
PERMANENT_R_OVERSHOOT = 1.8  # 实亏 ≥1.8R（2026-10-14 拍板，原 1.5）→ 永久档，只能手动移出

# 计入「重复止损」的出场原因：只有按止损出场的才算，manual/tp1_trail 等不算
STOP_EXIT_REASONS = ("sl", "breakeven_sl", "trail_sl")


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


def _repeat_stopouts(db: Session, symbol: str, window_days: int,
                     exclude_trade_id=None) -> int:
    """统计该币在窗口内已按止损出场（且亏损）的正式网笔数（docs/16 §13）。

    `exclude_trade_id` 用于排除"本次正在结算的这一笔"——结算路径上 rec 的
    realized_pnl/closed_at 已经写好，同一 session 的 autoflush 会让它出现在
    查询结果里，不排除就会把本次算进历史，阈值整体差一次。
    """
    from app.models.trade import TradeRecord

    since = datetime.utcnow() - timedelta(days=window_days)
    conds = [
        TradeRecord.symbol == symbol,
        TradeRecord.testnet == False,  # noqa: E712
        TradeRecord.realized_pnl < 0,
        TradeRecord.closed_at >= since,
        TradeRecord.exit_reason.in_(STOP_EXIT_REASONS),
    ]
    if exclude_trade_id is not None:
        conds.append(TradeRecord.id != exclude_trade_id)
    rows = db.execute(select(TradeRecord.id).where(*conds)).scalars().all()
    return len(rows)


def _upsert(db: Session, symbol: str, permanent: bool, reason: str,
            cooldown_days: int) -> str:
    """写入/刷新黑名单行，返回给日志用的档位描述。"""
    existing = db.execute(
        select(SymbolBlacklist).where(SymbolBlacklist.symbol == symbol)
    ).scalar_one_or_none()
    if existing is not None:
        if existing.origin in ("manual", "stop_hard") and not existing.disabled:
            # 永久档不被冷却档事件降级（2026-10-28 修）；永久档事件可刷新理由
            if existing.origin == "stop_hard" and permanent:
                existing.reason = reason
                logger.warning("黑名单已存在 %s（永久），刷新理由：%s", symbol, reason)
                return "refreshed"
            logger.warning("黑名单已存在 %s（%s 永久），保持不变：%s",
                           symbol, existing.origin, reason)
            return "skipped-permanent"
        existing.origin = "stop_hard" if permanent else "auto"
        existing.disabled = False
        existing.reason = reason
        existing.expires_at = None if permanent else datetime.utcnow() + timedelta(days=cooldown_days)
        logger.warning("黑名单已存在 %s，刷新：%s", symbol, reason)
        return "refreshed"
    db.add(SymbolBlacklist(
        symbol=symbol,
        origin="stop_hard" if permanent else "auto",
        reason=reason,
        expires_at=None if permanent else datetime.utcnow() + timedelta(days=cooldown_days),
    ))
    logger.warning(
        "黑名单自动添加 %s：%s（%s）", symbol, reason,
        "永久档，只能手动移出" if permanent else f"冷却档 {cooldown_days} 天")
    return "added"


def register_settle(db: Session, symbol: str, planned_risk, realized_pnl,
                    exit_reason: str, trade_id=None) -> None:
    """结算后登记（2026-09-18 用户拍板口径）：实际止损金额严重超出计划风险预算
    就自动加入黑名单。

    触发条件（满足其一）：
    - 净亏损 ≥ 计划风险 × 1.5（2026-10-14 拍板，原 1.2）：滑点撑爆止损距离
      （GRIFFAIN 实例 -121.7% R）、资金费/费用侵蚀等「止损没兜住」的情形
      → 冷却 AUTO_COOLDOWN_DAYS 天（2026-10-14 拍板，原 30 天）
    - 净亏损 ≥ 计划风险 × 1.8（2026-10-14 拍板，原 1.5）：止损严重失控 → 永久档
      （origin=stop_hard，expires_at 恒 NULL），只能手动移出
    - exit_reason == liquidation：强平=止损彻底失效，无条件触发（倍数决定档位）
    - **重复止损（docs/16 §13，2026-10-27 新增）**：窗口内按止损出场且亏损的
      笔数 ≥ BLACKLIST_REPEAT_STOP_N（0=关闭）→ 冷却档。与上面两档的差别是
      **不看倍数只看次数**——14 天数据里超预算最大值仅 1.33R（1.5 档从未触发），
      而 ICNT/LIGHT/DUSK/SU 各反复止损 3~4 次、每次都在 1R 以内。

    已在黑名单：冷却档刷新理由与到期；manual/stop_hard 永久行不降级覆盖
    （2026-10-28 修）；升级到永久档则清 expires_at
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
        # 超预算档没触发 → 再看重复止损档（docs/16 §13）
        repeat_n = int(getattr(settings, "BLACKLIST_REPEAT_STOP_N", 0) or 0)
        if repeat_n <= 0 or exit_reason not in STOP_EXIT_REASONS:
            return
        window = int(getattr(settings, "BLACKLIST_REPEAT_STOP_DAYS", 14) or 14)
        # hits 只统计"已落库"的历史止损（本次结算单此时通常还没写/没提交），
        # 故本次 +1 再比较，避免永远差一次
        hits = _repeat_stopouts(db, symbol, window, exclude_trade_id=trade_id)
        if hits + 1 < repeat_n:
            return
        cooldown = int(getattr(settings, "BLACKLIST_REPEAT_COOLDOWN_DAYS", AUTO_COOLDOWN_DAYS)
                       or AUTO_COOLDOWN_DAYS)
        reason = (
            f"自动：{window} 天内第 {hits + 1} 次止损出场（含本次），计划风险 "
            f"{planned:.2f} → 实亏 {pnl:.2f}，冷却 {cooldown} 天"
        )
        _upsert(db, symbol, False, reason, cooldown)
        return

    permanent = overshoot >= PERMANENT_R_OVERSHOOT
    kind = "强平（止损失效）" if exit_reason == "liquidation" else f"止损超预算 {overshoot * 100:.0f}%"
    reason = (
        f"自动：{kind}，计划风险 {planned:.2f} → 实亏 {pnl:.2f}，"
        + (f"永久拉黑（≥{PERMANENT_R_OVERSHOOT:g}R），只能手动移出" if permanent
           else f"冷却 {AUTO_COOLDOWN_DAYS} 天")
    )
    _upsert(db, symbol, permanent, reason, AUTO_COOLDOWN_DAYS)


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
