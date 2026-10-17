"""交易记录 API（docs/06）：列表 + 操作历史 + 复盘K线；资金分析聚合接口见 docs/14"""
import logging
import math
from datetime import datetime, timedelta, timezone
from typing import Optional
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session, joinedload

from app.database import get_db
from app.models.system_config import SystemConfig
from app.models.trade import AccountTransfer, PendingEntryOrder, TradeEvent, TradeRecord
from app.schemas.trade import (
    AccountTransferOut,
    DailyPoint,
    ExitReasonStat,
    FundAnalyticsOut,
    FundSummary,
    OpenFromAnalysisRequest,
    PendingEntryOut,
    PnlBucket,
    SymbolPnl,
    TradeEventOut,
    TradeOut,
    TransferPoint,
)
from app.services.binance_client import _INTERVAL_MINUTES
from app.services.exchange_pool import ExchangePool, AllExchangesFailed
from app.services.strategy import swing_annotations

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/trades", tags=["trades"])


def _account_scope_filter(model, scope: str):
    """scope → 账号过滤条件（docs/17 P0）：live/testnet=环境聚合（旧行为，
    按 testnet 布尔，兼容 account_key 为 NULL 的旧行）；all=全部；账号 key
    （live-1/testnet-1/…）=指定账号。未知值抛 400。"""
    if scope == "all":
        return None
    if scope in ("live", "testnet"):
        return model.testnet == (scope == "testnet")
    from app.services.account_registry import registry
    if scope in {a.key for a in registry()}:
        acc = next(a for a in registry() if a.key == scope)
        # 账号精确匹配；account_key 为 NULL 的旧行按环境回落（与 account_summary 口径一致）
        return or_(
            model.account_key == scope,
            and_(model.account_key.is_(None), model.testnet == acc.testnet),
        )
    raise HTTPException(status_code=400, detail=f"scope 只支持 live/testnet/all 或账号 key（未知：{scope}）")


@router.get("/account-summary")
def account_summary(
    account_key: Optional[str] = Query(None, description="账号 key；缺省 testnet-1（安全默认）"),
    db: Session = Depends(get_db),
):
    """指定账号合约账户资金概览（docs/06 账户维度补充 + docs/17 多账号）：
    总资金=钱包+未实现盈亏；总收益=已实现+未实现；收益率=总收益÷本金，
    本金=总资金−总收益（自洽口径，不依赖划转记录）。
    今日口径按北京自然日切（闭单 closed_at 落当日 00:00–24:00）：
    today_realized=当日闭单已实现盈亏；today_pnl_pct=（今日已实现+未实现）
    ÷ 期初钱包（当前钱包−今日已实现，忽略日内划转，近似自洽）。
    收益统计按账号 account_key 过滤（NULL 旧行按 testnet 回落）。
    2026-10-03 删环境切换：账号由前端 scope 显式解析传入，缺省 testnet-1。
    请求失败返回 available=False 前端降级显示「--」，不阻塞页面"""
    from app.services.binance_trader import BinanceTrader
    from app.services import account_registry as reg

    acc = reg.get_account(account_key) if account_key else None
    if account_key and acc is None:
        raise HTTPException(status_code=400, detail=f"账号 {account_key} 不在注册表中（检查服务器 .env）")
    if acc is None:
        acc = (reg.testnet_accounts() or reg.live_accounts() or [None])[0]
    acc_key = acc.key if acc else "testnet-1"
    testnet = acc.testnet if acc else True
    out: dict = {"testnet": testnet, "account_key": acc_key,
                 "account_name": acc.name if acc else acc_key, "available": False}
    try:
        trader = BinanceTrader(acc_key)
        wallet = trader.wallet_balance()          # 1 请求：wallet + available
        positions = trader.positions()            # 1 请求：未实现盈亏
        unrealized = 0.0
        for p in positions.values():
            up = float(p.get("unrealized_pnl") or 0)
            unrealized += up
        margin_balance = wallet["wallet"] + unrealized
        # 北京当日 00:00 → UTC 边界（北京 = UTC+8）
        day_start_utc = (
            (datetime.utcnow() + timedelta(hours=8)).replace(
                hour=0, minute=0, second=0, microsecond=0
            )
            - timedelta(hours=8)
        )
        # 账号口径：account_key 精确匹配，NULL 旧行按 testnet 布尔回落
        acc_cond = or_(
            TradeRecord.account_key == acc_key,
            and_(TradeRecord.account_key.is_(None), TradeRecord.testnet == testnet),
        )
        today_realized = db.execute(
            select(func.coalesce(func.sum(TradeRecord.realized_pnl), 0.0)).where(
                TradeRecord.status == "CLOSED",
                acc_cond,
                TradeRecord.closed_at >= day_start_utc,
            )
        ).scalar_one()
        realized = db.execute(
            select(func.coalesce(func.sum(TradeRecord.realized_pnl), 0.0)).where(
                TradeRecord.status == "CLOSED",
                acc_cond,
            )
        ).scalar_one()
        profit = float(realized) + unrealized
        principal = margin_balance - profit
        # 今日盈亏（已实现+未实现浮动）÷ 期初钱包 ≈ 当前钱包 − 今日已实现
        today_pnl = float(today_realized) + unrealized
        opening_wallet = wallet["wallet"] - float(today_realized)
        out.update(
            available=True,
            wallet=round(wallet["wallet"], 2),
            available_balance=round(wallet["available"], 2),
            unrealized=round(unrealized, 2),
            margin_balance=round(margin_balance, 2),
            realized=round(float(realized), 2),
            profit=round(profit, 2),
            profit_rate=round(profit / principal * 100, 2) if principal > 0 else None,
            today_realized=round(float(today_realized), 2),
            today_pnl_pct=round(today_pnl / opening_wallet * 100, 2) if opening_wallet > 0 else None,
        )
    except Exception as e:
        logger.warning("账户资金概览获取失败（testnet=%s）：%s", testnet, e)
    return out


@router.get("", response_model=list[TradeOut])
def list_trades(
    status: Optional[str] = None,
    scope: str = "all",
    limit: int = 100,
    db: Session = Depends(get_db),
):
    """自动交易记录（新→旧）。status 可选过滤：OPENED/TP1_HIT/TP2_HIT/CLOSED/FAILED；
    scope 支持环境聚合（live/testnet）、all 与账号 key（docs/17）"""
    q = select(TradeRecord).order_by(TradeRecord.created_at.desc()).limit(min(limit, 500))
    if status:
        q = q.where(TradeRecord.status == status.upper())
    cond = _account_scope_filter(TradeRecord, scope)
    if cond is not None:
        q = q.where(cond)
    rows = db.execute(q).scalars().all()

    # 4h 背景快照（前端「4h方向」列）：trade → ai_analysis → scan_result.votes_json.htf，
    # 两跳 IN 批量查（旧数据无 AI 分析/无 htf 键 → None 显示 -）
    ai_ids = {r.ai_analysis_id for r in rows if r.ai_analysis_id}
    htf_map: dict = {}
    if ai_ids:
        from app.models.scan import AIAnalysis, ScanResult
        ai_rows = db.execute(
            select(AIAnalysis.id, AIAnalysis.scan_result_id).where(AIAnalysis.id.in_(ai_ids))
        ).all()
        sr_ids = {sr_id for _, sr_id in ai_rows if sr_id}
        sr_map: dict = {}
        if sr_ids:
            sr_rows = db.execute(
                select(ScanResult.id, ScanResult.votes_json).where(ScanResult.id.in_(sr_ids))
            ).all()
            sr_map = {sid: (v or {}).get("htf") for sid, v in sr_rows}
        htf_map = {
            aid: sr_map.get(sr_id) for aid, sr_id in ai_rows if sr_id
        }
    out = []
    for r in rows:
        item = TradeOut.model_validate(r)
        item.htf = htf_map.get(r.ai_analysis_id)
        # TP 分批数量（前端预估收益列精确口径；旧记录 raw 无该键 → None 走前端比例回退）
        raw = r.raw or {}
        item.qty_tp1 = raw.get("qty_tp1")
        item.qty_tp2 = raw.get("qty_tp2")
        out.append(item)
    return out


@router.get("/transfers", response_model=list[AccountTransferOut])
def list_transfers(
    scope: str = "live",
    limit: int = 200,
    db: Session = Depends(get_db),
):
    """合约账户资金划转记录（新→旧，amount 正=转入/负=转出）。
    由每小时结算巡检从币安 income TRANSFER 逐账号同步，本接口只读；
    scope 支持环境聚合（live/testnet）、all 与账号 key（docs/17）。"""
    cond = _account_scope_filter(AccountTransfer, scope)
    q = select(AccountTransfer).order_by(AccountTransfer.transfer_time.desc()).limit(min(limit, 500))
    if cond is not None:
        q = q.where(cond)
    rows = db.execute(q).scalars().all()
    return [AccountTransferOut.model_validate(r) for r in rows]


@router.get("/transfer-stats")
def transfer_stats(scope: str = "live", db: Session = Depends(get_db)):
    """资金划转统计（2026-09-26）：服务端全量聚合（不受明细 limit 截断影响）。

    - tin/tout/net：account_transfers 按 scope 全量求和（正=转入/负=转出）
    - initial_capital：系统配置「初始资金」（正式网主账号首次入金口径——划转同步
      首次接入只回看 7 天，早于接入的入金不进库，以此字段补齐账目闭环）；
      仅 scope=live 返回，测试网/全部/单账号返回 0（前端显示「--」）
    - wallet_balance：scope 覆盖账号的当前钱包余额合计（实时查币安；任一账号
      查询失败跳过，available=False 表示全部失败前端降级「--」）
    """
    from app.services.binance_trader import BinanceTrader
    from app.services.account_registry import registry

    cond = _account_scope_filter(AccountTransfer, scope)
    tin = db.execute(
        select(func.coalesce(func.sum(AccountTransfer.amount), 0.0)).where(
            AccountTransfer.amount > 0, *( [cond] if cond is not None else [] ))
    ).scalar_one()
    tout = db.execute(
        select(func.coalesce(func.sum(AccountTransfer.amount), 0.0)).where(
            AccountTransfer.amount < 0, *( [cond] if cond is not None else [] ))
    ).scalar_one()

    cfg = db.get(SystemConfig, 1)
    # 初始资金仅正式网口径（live_initial_capital=正式网主账号首次入金补齐）：
    # 测试网/全部/单账号范围无此概念，返回 0 由前端显示「--」。
    # 2026-09-28 修复：此前所有 scope 都返回 live_initial_capital，测试网页签串显正式网数据
    initial_capital = float(cfg.live_initial_capital or 0) if (cfg and scope == "live") else 0.0

    # scope → 账号清单（与 _account_scope_filter 同口径）
    if scope == "all":
        accounts = list(registry())
    elif scope == "testnet":
        accounts = [a for a in registry() if a.testnet]
    elif scope == "live":
        accounts = [a for a in registry() if not a.testnet]
    else:
        accounts = [a for a in registry() if a.key == scope]

    wallet, available = 0.0, bool(accounts)
    for a in accounts:
        try:
            wallet += BinanceTrader(a.key).wallet_balance()["wallet"]
        except Exception as e:
            logger.warning("划转统计钱包余额获取失败（%s）：%s", a.key, e)
    tin_f, tout_f = float(tin), float(tout)  # tout 原始为负值求和
    return {
        "tin": round(tin_f, 2),
        "tout": round(-tout_f, 2) or 0.0,  # or 0.0 归一化 -0.0（否则前端显示 -0.00）
        "net": round(tin_f + tout_f, 2),  # 净流入 = 转入 + 转出（原始负值）
        "initial_capital": round(initial_capital, 2),
        "wallet_balance": round(wallet, 2),
        "available": available,
    }


@router.post("/adopt-now")
def adopt_now(db: Session = Depends(get_db)):
    """手动触发一次自动导入持仓（2026-09-24）：接管逻辑与每小时巡检一致
    （交易所非零持仓未被接管的 = App 手动单，SL/TP 取条件单原值接管）。

    账号范围（2026-10-03 删环境切换）：双网管理清单 = 测试网账号 + 正式网
    启用账号（managed_accounts）——两网手动单始终全部导入，纳入巡检。
    逐账号在各自开仓锁内执行，全有全无（任一拿锁失败即 409）。"""
    from redis import Redis
    from app.config import settings
    from app.services.binance_trader import BinanceTrader
    from app.services.trade_engine import auto_adopt_positions
    from app.services.account_registry import managed_accounts
    from app.tasks.trade_tasks import (
        _acquire_open_lock, _release_open_lock, _OPEN_LOCK_TTL_S,
    )

    accs = managed_accounts(db)
    if not accs:
        raise HTTPException(status_code=400, detail="无已配置的交易账号")

    r = Redis.from_url(settings.REDIS_URL, decode_responses=True)
    ident = f"adopt-now-{uuid4().hex[:8]}"
    acquired: list[str] = []
    for a in accs:
        if _acquire_open_lock(r, ident, wait_s=10, ttl_s=_OPEN_LOCK_TTL_S,
                              account=a.key):
            acquired.append(a.key)
        else:
            break
    if len(acquired) < len(accs):
        for k in acquired:
            _release_open_lock(r, ident, account=k)
        raise HTTPException(status_code=409, detail="与结算巡检/开仓互斥中，请稍后重试")
    try:
        adopted: list[str] = []
        for a in accs:
            at = BinanceTrader(a.key)
            if not at.configured:
                continue
            try:
                adopted.extend(auto_adopt_positions(db, at))
            except Exception:
                db.rollback()
                logger.exception("导入持仓失败（%s）", a.key)
    finally:
        for k in acquired:
            _release_open_lock(r, ident, account=k)
    return {"ok": True, "adopted": adopted,
            "count": len(adopted), "accounts": [a.key for a in accs]}


@router.post("/inspect-now")
def inspect_now():
    """手动触发一次结算巡检（2026-09-26）：与每小时定时巡检完全同源——
    仅派发 celery 任务（run_auto_trade_task），结算/自动导入/AI 诊断全部
    在后台执行，HTTP 立即返回。并发安全由巡检自身的开仓锁全有全无模式
    兜底：与定时巡检/手动开单撞车时该轮自动跳过并记录，不会重复结算。"""
    from app.tasks.trade_tasks import run_auto_trade_task
    async_res = run_auto_trade_task.delay()
    return {"ok": True, "task_id": async_res.id,
            "message": "巡检已派发后台执行，完成后刷新查看结果"}


# core 失败码 → HTTP 状态码（网页端；飞书卡片按钮同源共享执行核心，
# 用 code 映射结果卡片文案而非 HTTP）
_OPEN_ERR_STATUS = {"invalid": 400, "not_found": 404, "bad_request": 400,
                    "conflict": 409, "error": 500}


@router.post("/open-from-analysis")
def open_from_analysis(payload: OpenFromAnalysisRequest):
    """手动开单（2026-09-27 扫描结果表「开单」按钮）：对指定 AI 分析立即走一次
    完整开仓闸门，仅请求指定的账号、不做多账号扇出。

    与自动开仓路径（open_trade_for_analysis celery 任务）的差异：
    - 仅 payload.account_key 指定账号（2026-10-03 删环境切换：开到哪个网由
      用户在弹窗显式选择；手动点击是单行明确意图，扇出会在所有启用账号
      真实下单，语义不对）
    - 不受 trading_enabled 约束（2026-09-28 语义拆分：自动交易开关仅指
      每小时 AI 分析后自动成交；开单按钮是用户对当下价格的明确意图）
    - force_market=True：以现价市价即时入场，跳过入场偏离/待回踩时机闸门；
      其余风控闸门（RR 现价复算 / SL-TP 可挂性 / 保证金 / 滑点）全部保留
    - 闸门未过返回 200 opened=false + block_reason（业务拦截非 HTTP 错误），
      拦截原因已由 _mark_open_block 落 ai_analyses.open_block_reason

    执行核心 2026-10-15 抽到 trade_tasks.manual_open_from_analysis（飞书卡片
    「开单」按钮同源，与 manual_close_market 同一抽取惯例），本端点只把 core
    的失败码映射成 HTTP 状态码。
    """
    from app.tasks.trade_tasks import manual_open_from_analysis

    res = manual_open_from_analysis(str(payload.analysis_id), str(payload.account_key))
    if not res["ok"]:
        raise HTTPException(status_code=_OPEN_ERR_STATUS.get(res["code"], 500),
                            detail=res["error"])
    return {"ok": True, "opened": res["opened"], "pending": res.get("pending", False),
            "block_reason": res["block_reason"],
            "trade_id": res["trade_id"], "pending_entry_id": res.get("pending_entry_id"),
            "account_key": res["account_key"]}


@router.get("/pending-entries", response_model=list[PendingEntryOut])
def list_pending_entries(
    status: str = "pending",
    scope: str = "all",
    limit: int = 100,
    db: Session = Depends(get_db),
):
    """挂单入场列表（docs/27，2026-10-17）：现价盈亏比不足时在 AI 入场价挂的
    限价单。status 默认只看 pending（进行中）；scope 支持环境聚合（live/testnet）、
    all 与账号 key（docs/17，复用 _account_scope_filter）。seconds_left 为距
    1 小时 TTL 过期的剩余秒数，供前端倒计时（已过期/已了结归 0）。"""
    q = select(PendingEntryOrder).order_by(PendingEntryOrder.created_at.desc()).limit(min(limit, 500))
    if status:
        q = q.where(PendingEntryOrder.status == status.lower())
    cond = _account_scope_filter(PendingEntryOrder, scope)
    if cond is not None:
        q = q.where(cond)
    rows = db.execute(q).scalars().all()

    now = datetime.utcnow()
    out = []
    for r in rows:
        item = PendingEntryOut.model_validate(r)
        if r.status == "pending" and r.expires_at:
            item.seconds_left = max(0, int((r.expires_at - now).total_seconds()))
        else:
            item.seconds_left = 0
        out.append(item)
    return out


@router.post("/pending-entries/{pending_id}/cancel")
def cancel_pending_entry(pending_id: str, db: Session = Depends(get_db)):
    """手动撤销挂单（docs/27）：撤币安 GTC 限价单 + 标记 canceled。

    取该账号开仓锁（与结算巡检/开单互斥），锁内重读状态防并发（可能已被
    watch_pending_entries 撤单/成交落地）。撤单为 best-effort（交易所已成交
    或已撤时静默）——DB 状态仍标记 canceled，分钟巡检会按交易所真源纠正
    （成交则落地为 filled）。"""
    from uuid import UUID as _UUID
    from redis import Redis
    from app.config import settings
    from app.services.account_registry import get_account
    from app.services.binance_trader import BinanceTrader
    from app.services.trade_locks import OPEN_LOCK_TTL_S, acquire_open_lock, release_open_lock

    try:
        pid = _UUID(pending_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="挂单不存在")
    pe = db.get(PendingEntryOrder, pid)
    if pe is None:
        raise HTTPException(status_code=404, detail="挂单不存在")
    if pe.status != "pending":
        raise HTTPException(status_code=409, detail=f"挂单已了结（{pe.status}），无需撤销")

    acc_key = pe.account_key or ("testnet-1" if pe.testnet else "live-1")
    r = Redis.from_url(settings.REDIS_URL, decode_responses=True)
    ident = f"cancel-pending-{uuid4().hex[:8]}"
    if not acquire_open_lock(r, ident, wait_s=10, ttl_s=OPEN_LOCK_TTL_S, account=acc_key):
        raise HTTPException(status_code=409, detail="与结算巡检/开仓互斥中，请稍后重试")
    try:
        db.refresh(pe)
        if pe.status != "pending":
            raise HTTPException(status_code=409, detail=f"挂单已了结（{pe.status}），无需撤销")
        # 撤交易所限价单（best-effort：账号未配置/已成交/已撤均不阻断 DB 状态落定）
        if pe.limit_order_id:
            acc = get_account(acc_key)
            trader = BinanceTrader(acc.key) if acc else BinanceTrader(acc_key)
            if trader.configured:
                try:
                    trader.cancel_regular_order(pe.symbol, pe.limit_order_id)
                except Exception as e:
                    logger.warning("撤销挂单 %s 交易所撤单失败（best-effort）：%s", pe.id, e)
        pe.status = "canceled"
        pe.resolve_reason = "manual"
        pe.resolved_at = datetime.utcnow()
        db.commit()
    finally:
        release_open_lock(r, ident, account=acc_key)
    logger.info("手动撤销挂单 %s %s（账号 %s）", pe.id, pe.symbol, acc_key)
    return {"ok": True, "id": str(pe.id)}


@router.post("/{trade_id}/close-market")
def close_market(trade_id: str, db: Session = Depends(get_db)):
    """手动市价平仓（2026-09-25）：与飞书卡片「市价平仓」按钮完全同源
    （共享 manual_close_market：状态校验 + 按开单账号选 trader + 账号口径
    开仓锁互斥巡检 + 锁内重读）。市价全平后立即按 income 口径结算落终态。
    平仓约 2~5s，同步执行（adopt-now 同模式）。"""
    from uuid import UUID as _UUID
    from app.tasks.trade_tasks import manual_close_market

    try:
        _UUID(trade_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="交易记录不存在")
    res = manual_close_market(trade_id)
    if not res["ok"]:
        err = str(res.get("error") or "未知错误")
        if "不存在" in err:
            raise HTTPException(status_code=404, detail=err)
        if "系统忙" in err:
            raise HTTPException(status_code=409, detail=err)
        if "平仓失败" in err:
            raise HTTPException(status_code=500, detail=err)
        raise HTTPException(status_code=400, detail=err)
    return res


def _bj_day(dt: datetime) -> str:
    """UTC naive → 北京时间自然日键（DB 存 UTC，北京 = UTC+8）"""
    return (dt + timedelta(hours=8)).date().isoformat()


_PNL_EDGES = (5, 10, 20, 50, 100, 200, 500, 1000)  # |pnl| 档位切分边界


def _pnl_bucket(v: float) -> str:
    """实盈亏 → 固定档位名：负/正各 8 档 + 溢出档（边界值归离 0 更远的一档）"""
    mag = abs(v)
    for i, edge in enumerate(_PNL_EDGES):
        if mag < edge:
            lo = _PNL_EDGES[i - 1] if i else 0
            if v > 0:
                return f"{lo}~{edge}"
            return f"0~-{edge}" if lo == 0 else f"-{edge}~-{lo}"
    return ">1000" if v > 0 else ">|1000|"


def _pnl_bucket_names() -> list[str]:
    """全部档位名（深亏 → 深盈），供盈亏分布固定 x 轴顺序"""
    neg = []
    for i in range(len(_PNL_EDGES) - 1, -1, -1):
        edge = _PNL_EDGES[i]
        lo = _PNL_EDGES[i - 1] if i else 0
        neg.append(f"0~-{edge}" if lo == 0 else f"-{edge}~-{lo}")
    pos = []
    for i, e in enumerate(_PNL_EDGES):
        lo = _PNL_EDGES[i - 1] if i else 0
        pos.append(f"{lo}~{e}")
    pos.append(">1000")
    return [">|1000|"] + neg + pos


@router.get("/analytics", response_model=FundAnalyticsOut)
def fund_analytics(
    scope: str = "live",
    start: Optional[str] = None,   # YYYY-MM-DD（北京时间）
    end: Optional[str] = None,     # 闭区间，含当日
    db: Session = Depends(get_db),
):
    """资金分析聚合（docs/14）：闭单按 closed_at 落北京时间自然日桶，划转按 transfer_time

    - start/end 为北京时间 YYYY-MM-DD 闭区间；DB 存 UTC naive，过滤用换算后的 UTC 边界
    - scope 支持环境聚合（live/testnet）、all 与账号 key（docs/17）；
      闭单/划转均按 scope 过滤
    """
    def _parse_day(s: str, name: str) -> datetime:
        try:
            return datetime.fromisoformat(s)
        except ValueError:
            raise HTTPException(status_code=400, detail=f"{name} 参数格式应为 YYYY-MM-DD")

    trade_cond = _account_scope_filter(TradeRecord, scope)
    transfer_cond = _account_scope_filter(AccountTransfer, scope)
    # 北京日边界 → UTC：北京 00:00 = 前一日 16:00 UTC
    lo = _parse_day(start, "start") - timedelta(hours=8) if start else None
    hi = _parse_day(end, "end") + timedelta(days=1) - timedelta(hours=8) if end else None

    base = select(TradeRecord).where(
        TradeRecord.status == "CLOSED",
        TradeRecord.closed_at.is_not(None),
    )
    if trade_cond is not None:
        base = base.where(trade_cond)
    if lo is not None:
        base = base.where(TradeRecord.closed_at >= lo)
    if hi is not None:
        base = base.where(TradeRecord.closed_at < hi)
    rows = db.execute(base).scalars().all()

    closed = len(rows)
    wins = sum(1 for r in rows if (r.realized_pnl or 0) > 0)
    net_pnl = round(sum(float(r.realized_pnl or 0) for r in rows), 2)
    fee_total = round(sum(float(r.fee_total or 0) for r in rows), 2)
    funding_total = round(sum(float(r.funding_total or 0) for r in rows), 2)
    win_rs = [
        float(r.realized_pnl) / float(r.risk_amount)
        for r in rows
        if (r.realized_pnl or 0) > 0 and r.risk_amount and float(r.risk_amount) > 0
    ]
    avg_win_r = round(sum(win_rs) / len(win_rs), 2) if win_rs else None
    # 全部已结算单的 R 均值（2026-10-12，交易记录页「平均盈亏比」用）：口径同
    # 列表「盈亏比」列（净盈亏 ÷ 计划风险额），含止盈/止损全部出场——avg_win_r
    # 仅计盈利单偏乐观；净盈亏未知或 risk_amount 缺失的行算不出 R，剔除
    all_rs = [
        float(r.realized_pnl) / float(r.risk_amount)
        for r in rows
        if r.realized_pnl is not None and r.risk_amount and float(r.risk_amount) > 0
    ]
    avg_r = round(sum(all_rs) / len(all_rs), 2) if all_rs else None

    daily: dict[str, float] = {}
    for r in rows:
        d = _bj_day(r.closed_at)
        daily[d] = daily.get(d, 0.0) + float(r.realized_pnl or 0)
    days = sorted(daily)
    equity_curve: list[DailyPoint] = []
    cum = 0.0
    for d in days:
        cum += daily[d]
        equity_curve.append(DailyPoint(date=d, pnl=round(cum, 2)))

    by_symbol_map: dict[str, dict] = {}
    for r in rows:
        m = by_symbol_map.setdefault(r.symbol, {"pnl": 0.0, "count": 0, "wins": 0})
        m["pnl"] += float(r.realized_pnl or 0)
        m["count"] += 1
        if (r.realized_pnl or 0) > 0:
            m["wins"] += 1

    exit_map: dict[str, dict] = {}
    for r in rows:
        m = exit_map.setdefault(r.exit_reason or "unknown", {"count": 0, "pnl": 0.0})
        m["count"] += 1
        m["pnl"] += float(r.realized_pnl or 0)

    buckets: dict[str, int] = {}
    for r in rows:
        name = _pnl_bucket(float(r.realized_pnl or 0))
        buckets[name] = buckets.get(name, 0) + 1

    # 划转（amount 带符号：正=转入），只统计 USDT；按 scope 过滤
    tq = select(AccountTransfer).where(AccountTransfer.asset == "USDT")
    if transfer_cond is not None:
        tq = tq.where(transfer_cond)
    if lo is not None:
        tq = tq.where(AccountTransfer.transfer_time >= lo)
    if hi is not None:
        tq = tq.where(AccountTransfer.transfer_time < hi)
    rows_t = db.execute(tq).scalars().all()
    t_daily: dict[str, dict] = {}
    for t in rows_t:
        m = t_daily.setdefault(_bj_day(t.transfer_time), {"in": 0.0, "out": 0.0})
        amt = float(t.amount)
        if amt >= 0:
            m["in"] += amt
        else:
            m["out"] += -amt
    t_in = round(sum(m["in"] for m in t_daily.values()), 2)
    t_out = round(sum(m["out"] for m in t_daily.values()), 2)

    return FundAnalyticsOut(
        summary=FundSummary(
            closed=closed,
            wins=wins,
            win_rate=round(wins / closed * 100, 1) if closed else None,
            net_pnl=net_pnl,
            fee_total=fee_total,
            funding_total=funding_total,
            avg_win_r=avg_win_r,
            avg_r=avg_r,
            transfer_in=t_in,
            transfer_out=t_out,
            transfer_net=round(t_in - t_out, 2),
        ),
        equity_curve=equity_curve,
        daily_pnl=[DailyPoint(date=d, pnl=round(daily[d], 2)) for d in days],
        by_symbol=sorted(
            (
                SymbolPnl(symbol=s, pnl=round(m["pnl"], 2), count=m["count"], wins=m["wins"])
                for s, m in by_symbol_map.items()
            ),
            key=lambda x: abs(x.pnl),
            reverse=True,
        ),
        exit_reasons=sorted(
            (
                ExitReasonStat(reason=k, count=m["count"], pnl=round(m["pnl"], 2))
                for k, m in exit_map.items()
            ),
            key=lambda x: x.count,
            reverse=True,
        ),
        pnl_distribution=[
            PnlBucket(bucket=b, count=buckets[b]) for b in _pnl_bucket_names() if b in buckets
        ],
        transfers=[
            TransferPoint(date=d, transfer_in=round(m["in"], 2), transfer_out=round(m["out"], 2))
            for d, m in sorted(t_daily.items())
        ],
    )


def _get_trade_events(trade_id: str, db: Session) -> list[TradeEvent]:
    """按记录 ID 升序取操作历史（不存在抛 404）"""
    rec = db.get(TradeRecord, trade_id)
    if not rec:
        raise HTTPException(status_code=404, detail="交易记录不存在")
    return db.execute(
        select(TradeEvent)
        .where(TradeEvent.trade_record_id == rec.id)
        .order_by(TradeEvent.created_at.asc())
    ).scalars().all()


@router.post("/purge-testnet")
def purge_testnet(confirm: bool = False, db: Session = Depends(get_db)):
    """一键清空测试网交易数据（两阶段：confirm=false 预检返回将删数量，
    confirm=true 执行删除；范围=测试网交易记录+操作历史+划转流水，
    不含复盘统计）。执行写入系统日志（任务记录）供审计"""
    from app.services.trade_engine import purge_testnet_data

    try:
        return purge_testnet_data(db, force=confirm)
    except Exception as e:
        logger.error("清空测试网数据失败: %s", e)
        raise HTTPException(status_code=500, detail=f"清空失败: {e}")


@router.get("/{trade_id}/events", response_model=list[TradeEventOut])
def trade_events(trade_id: str, db: Session = Depends(get_db)):
    """一笔交易的操作历史（开仓/止盈成交/止损移动/结算/异常）"""
    rows = _get_trade_events(trade_id, db)
    return [TradeEventOut.model_validate(e) for e in rows]


def _load_running_trade(trade_id: str, db: Session) -> TradeRecord:
    """手动诊断前置校验：记录存在且在跑状态（docs/16）"""
    rec = db.get(TradeRecord, trade_id)
    if not rec:
        raise HTTPException(status_code=404, detail="交易记录不存在")
    if rec.status not in ("OPENED", "TP1_HIT", "TP2_HIT"):
        raise HTTPException(status_code=400, detail="仅运行中的单子支持 AI 持仓诊断")
    return rec


@router.post("/{trade_id}/advice")
def run_advice(trade_id: str, db: Session = Depends(get_db)):
    """手动 AI 持仓诊断（docs/16）：异步派发诊断子任务，立即返回。

    同步等 LLM 会被代理层/前端超时切断（GLM 思考模型常超 30s），改为与定时
    诊断同链路（run_position_advice，锁外 celery 执行）；任务不做冷却判断，
    手动派发天然无视冷却。前端提交后轮询列表对比 ai_advice_at 获取结果。
    """
    from app.config import settings
    from app.tasks.trade_tasks import run_position_advice

    rec = _load_running_trade(trade_id, db)
    if not settings.AI_API_KEY:
        raise HTTPException(status_code=400, detail="AI 未配置（AI_API_KEY 为空）")
    run_position_advice.delay(str(rec.id))
    return {"queued": True, "symbol": rec.symbol}


@router.get("/{trade_id}/klines")
def trade_klines(
    trade_id: str,
    interval: Optional[str] = None,
    db: Session = Depends(get_db),
):
    """交易复盘K线：开单前 200 根 + 持仓期间直到止损/平仓 + 平仓后至多 5 根，附初始止损止盈与关键事件

    - 初始 SL/TP 取自 OPEN 事件 detail（记录上的 stop_loss 是现值，跟踪止损后已被更新）
    - 已平仓记录平仓后再多显示至多 5 根 K 线（观察出场后走势，验证出场是否过早；
      平仓后不足 5 根时交易所有多少给多少，endTime 超前于最新K线不会报错）
    - 总根数 = 200 + 持仓根数 + 平仓后根数，上限 500（超长持仓自动压缩开仓前根数，持仓段始终完整）
    - 开单前 200 根（2026-10-11，原 100）：主图默认显示 EMA21/55/144 三线，EMA144
      需约 143 根热身才有值，100 根前置不够
    - 历史区间直接请求交易所（多所故障转移），不读不写缓存
    - interval（2026-10-08）：可选查询参数（1h/4h 等），缺省回落 cfg.kline_interval
      ——弹窗内切换周期复盘，事件箭头按事件时刻映射到所在K线，与周期无关
    """
    try:
        rec = db.get(TradeRecord, UUID(trade_id))
    except ValueError:
        raise HTTPException(status_code=404, detail="交易记录不存在")
    if not rec:
        raise HTTPException(status_code=404, detail="交易记录不存在")
    if rec.status == "FAILED" or rec.opened_at is None:
        raise HTTPException(status_code=400, detail="该记录未实际开仓，无复盘K线")

    cfg = db.get(SystemConfig, 1)
    if interval:
        if interval not in _INTERVAL_MINUTES:
            raise HTTPException(status_code=400, detail=f"不支持的周期：{interval}")
    else:
        interval = cfg.kline_interval if cfg else "1h"
    interval_ms = _INTERVAL_MINUTES.get(interval, 60) * 60_000

    # DB 存 UTC naive 时间戳，显式按 UTC 换算（naive.timestamp() 会按本地时区解释）
    opened_dt: datetime = rec.opened_at
    end_dt: datetime = rec.closed_at or datetime.utcnow()
    held_bars = math.ceil(max(0, (end_dt - opened_dt).total_seconds() * 1000) / interval_ms)
    after_bars = 5 if rec.closed_at else 0  # 平仓后走势观察窗（进行中单子无）
    bars = min(500, 200 + held_bars + after_bars)  # 前置 200：EMA144 热身需 ~143 根
    # 减 1ms：边界恰在整点时不含下一根K线（平仓在K线中间时仍含所在K线）；
    # 已平仓再延 after_bars 根——平仓后第 5 根收盘边界为截止点
    end_ms = int(end_dt.replace(tzinfo=timezone.utc).timestamp() * 1000) - 1
    if after_bars:
        end_ms += after_bars * interval_ms

    pool = ExchangePool()
    try:
        klines = pool.get_klines_range(rec.symbol, interval, bars, end_ms)
    except AllExchangesFailed as e:
        raise HTTPException(status_code=503, detail=f"K线获取失败（{e.summary}）")

    # 摆动结构标注（与扫描图/AI 事实包同源同参 n=20，界面一致性 2026-10-10、
    # 2026-10-11 与 AI 对齐由 5 放宽 20）
    swings = swing_annotations(klines, order=cfg.swing_order if cfg else 3, n=20)

    evts = db.execute(
        select(TradeEvent)
        .where(TradeEvent.trade_record_id == rec.id)
        .order_by(TradeEvent.created_at.asc())
    ).scalars().all()

    # 初始风控价：OPEN 事件 detail 优先，老记录（无 OPEN 事件）回退记录现值；
    # 0/None 归一为 null（未设 TP2 的单子 tp2=0，传 0 到前端会画出异常区域）
    open_detail = next((e.detail or {} for e in evts if e.event_type == "OPEN"), {})

    def _nz(v) -> Optional[float]:
        if v is None:
            return None
        f = float(v)
        return f if f != 0 else None

    entry_price = (
        _nz(open_detail.get("entry_price") or rec.entry_price)
        if open_detail.get("adopted")
        # 接管单 OPEN detail.price 是接管时刻现价（真实持仓均价在 entry_price 字段），
        # 直接用作入场价会把复盘图画偏（KSMUSDT 案例）；普通单 price 即实际成交价
        else _nz(open_detail.get("price") or rec.entry_price)
    )
    stop_loss = _nz(open_detail.get("stop_loss") or rec.stop_loss)
    tp1 = _nz(open_detail.get("tp1") or rec.tp1)
    tp2 = _nz(open_detail.get("tp2") or rec.tp2)

    return {
        "trade_id": str(rec.id),
        "symbol": rec.symbol,
        "direction": rec.direction,
        "interval": interval,
        "status": rec.status,
        "exit_reason": rec.exit_reason,
        "entry_price": entry_price,
        "stop_loss": stop_loss,
        "current_stop_loss": _nz(rec.stop_loss),  # 跟踪止损现值（SL_MOVE 后更新）；与初始快照不同时前端加画现止损线
        "tp1": tp1,
        "tp2": tp2,
        # 止盈模式快照（历史字段：2026-10-12 策略二/trail 删除后停写，
        # 新行 null；旧行 null 按 ladder 理解）
        "tp_mode": rec.tp_mode,
        "opened_at": opened_dt.isoformat() if opened_dt else None,
        "closed_at": rec.closed_at.isoformat() if rec.closed_at else None,
        "klines": [
            {
                "time": int(k[0]),
                "open": float(k[1]),
                "high": float(k[2]),
                "low": float(k[3]),
                "close": float(k[4]),
                "volume": float(k[5]),
            }
            for k in klines
        ],
        # 摆动结构标注（HH/LH/HL/LL，与扫描图一致）
        "swings": swings,
        # 图上标箭头的关键事件（SL_MOVE 等过程事件不标）
        "events": [
            {
                "event_type": e.event_type,
                "created_at": e.created_at.isoformat(),
                "detail": e.detail,
            }
            for e in evts
            if e.event_type in ("OPEN", "TP1_FILL", "TP2_FILL", "SL_FILL")
        ],
    }


@router.get("/{trade_id}/reconciliation")
def trade_reconciliation(trade_id: str, db: Session = Depends(get_db)):
    """逐笔 R 泄漏对账（docs/24 A1 查因先行）：计划三价/分批 vs 实际成交对照，
    泄漏拆解为入场滑点/手续费/资金费/SL 穿价/TP1 滑点（USDT + R 双口径），
    附理论 R vs 实际 R 汇总与对账缺口提示（缺失项 null 并落 gaps）。
    单笔按 id 直查，不加 scope 参数。"""
    from uuid import UUID as _UUID

    from app.services.trade_recon import reconcile_trade

    try:
        _UUID(trade_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="交易记录不存在")
    rec = db.get(TradeRecord, trade_id)
    if not rec:
        raise HTTPException(status_code=404, detail="交易记录不存在")
    return reconcile_trade(db, rec)
