"""现货屯币 API（docs/18 P0：只读 + 配置，不下单）

- GET  /spot/overview   持仓总览（三币持仓 + 现金池 + 实时评分 + 当前计划）
- GET  /spot/score/{symbol}  单币评分明细（可解释，P0 验收 TradingView 对照用）
- GET  /spot/orders     订单列表（limit 参数）
- GET  /spot/settings   配置读取
- PUT  /spot/settings   配置写入（字段白名单 + 范围校验）

零侵入约束：独立 router；不引用合约任何服务（task/trade_engine）。
评分实时计算（读 spot_kline_cache，缓存缺失时现场拉一次币安公开K线）。
"""
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.spot import SPOT_SYMBOLS, SpotOrder
from app.models.system_config import SystemConfig
from app.services.spot_engine import evaluate_day
from app.services.spot_settings import load_spot_settings, merge_spot_settings

logger = logging.getLogger("app.spot")

router = APIRouter(prefix="/api/spot", tags=["spot"])

_TZ_BJ = timezone(timedelta(hours=8))


def _closes(raw: list) -> List[float]:
    return [float(k["c"]) for k in raw]


def _get_cache(db: Session, symbol: str, interval: str):
    from app.models.spot import SpotKlineCache

    return (
        db.query(SpotKlineCache)
        .filter(SpotKlineCache.symbol == symbol, SpotKlineCache.interval == interval)
        .first()
    )


def _ensure_klines(db: Session, client, symbol: str, interval: str, limit: int) -> list:
    """优先读缓存；缺失/过旧（日K < 今日收盘）时拉一次币安并回写缓存。"""
    from app.tasks.spot_tasks import _upsert_cache

    row = _get_cache(db, symbol, interval)
    # 日K：缓存最后一根必须已是本日收盘（BJ 08:00 后为昨天 UTC 00:00 开盘那根）；
    # 周/月K容忍更新不及时（未收盘K线本来就在序列里）
    need_refresh = True
    if row is not None and row.klines:
        if interval == "1d":
            # t 为毫秒时间戳，必须 /1000 转秒（漏除会溢出：year 58696 is out of range）
            last_open = datetime.utcfromtimestamp(float(row.klines[-1]["t"]) / 1000.0)
            # 收盘时刻（UTC +1 天）若已过去，说明缓存含已收盘最新K线
            if last_open + timedelta(days=1) <= datetime.utcnow():
                need_refresh = False
        else:
            need_refresh = False
    if need_refresh:
        try:
            raw = client.klines(symbol, interval, limit)
            _upsert_cache(db, symbol, interval, raw)
            db.commit()
            return [{"t": int(k[0]), "o": float(k[1]), "h": float(k[2]),
                     "l": float(k[3]), "c": float(k[4]), "v": float(k[5])} for k in raw]
        except Exception as e:
            logger.warning("spot %s %s 拉K线失败: %s", symbol, interval, e)
            if row is not None and row.klines:
                return row.klines  # 降级用旧缓存
            raise HTTPException(status_code=502, detail=f"{symbol} {interval} K线获取失败")
    return row.klines


def _evaluate_symbol(db: Session, client, symbol: str) -> Dict[str, Any]:
    daily = _ensure_klines(db, client, symbol, "1d", 300)
    weekly = _ensure_klines(db, client, symbol, "1w", 200)
    monthly = _ensure_klines(db, client, symbol, "1M", 150)
    last_open_ms = int(daily[-1]["t"])
    as_of = (
        datetime.fromtimestamp((last_open_ms + 86400_000) / 1000, _TZ_BJ)
    ).strftime("%Y-%m-%d")
    return evaluate_day(symbol, as_of, _closes(daily), _closes(weekly), _closes(monthly))


# ---------------- 环境与账号（docs/18：现货环境与合约完全独立） ----------------

class SpotConfigIn(BaseModel):
    spot_trading_testnet: Optional[bool] = None
    spot_enabled_keys: Optional[List[str]] = None


def _config_response(db: Session) -> Dict[str, Any]:
    from app.services.account_registry import parse_live_keys, registry, spot_enabled_accounts

    testnet = _spot_testnet(db)
    if testnet:
        # 测试网模式：展示预配置的正式网启用集合（切正式网即生效）
        cfg = db.get(SystemConfig, 1)
        enabled = parse_live_keys(cfg.spot_live_trading_keys if cfg else None)
    else:
        enabled = [a.key for a in spot_enabled_accounts(db) if not a.testnet]
    return {
        "spot_trading_testnet": testnet,
        "spot_enabled_keys": enabled,
        "accounts": [
            {
                "key": a.key,
                "name": a.name,
                "testnet": a.testnet,
                "configured": True,  # 注册表构建时已校验 key/secret 成对，进入清单即已配置
            }
            for a in registry()
        ],
    }


def _spot_testnet(db: Session) -> bool:
    from app.models.system_config import SystemConfig

    cfg = db.get(SystemConfig, 1)
    return bool(cfg.spot_trading_testnet) if cfg is not None else True


@router.get("/config")
def get_spot_config(db: Session = Depends(get_db)):
    """现货环境 + 启用账号（.env 注册表只读展示）。"""
    return _config_response(db)


@router.put("/config")
def put_spot_config(body: SpotConfigIn, db: Session = Depends(get_db)):
    """更新现货环境/启用账号。与合约字段零交集：
    - spot_trading_testnet：true=现货测试网（默认），false=现货正式网
    - spot_enabled_keys：正式网启用账号（必须为注册表内 live 账号）；
      空列表回落 live-1；测试网模式下仍可预配置（切正式网即生效）
    P0 无自动下单，切环境不做总闸联锁（P1 接下单时补确认交互）。"""
    from app.models.system_config import SystemConfig
    from app.services.account_registry import get_account

    cfg = db.get(SystemConfig, 1)
    if cfg is None:
        raise HTTPException(status_code=500, detail="system_config 未初始化")
    rejected: List[str] = []
    if body.spot_trading_testnet is not None:
        cfg.spot_trading_testnet = bool(body.spot_trading_testnet)
    if body.spot_enabled_keys is not None:
        cleaned: List[str] = []
        for k in body.spot_enabled_keys:
            a = get_account(k)
            if a is None or a.testnet:
                rejected.append(k)
                continue
            if k not in cleaned:
                cleaned.append(k)
        cfg.spot_live_trading_keys = ",".join(cleaned)
    db.commit()
    return {"ok": True, **_config_response(db), "rejected": rejected}


# ---------------- 持仓总览 ----------------

@router.get("/overview")
def spot_overview(account_key: Optional[str] = None, db: Session = Depends(get_db)):
    """三币持仓 + 现金池 + 实时评分。

    多账号（现货正式网多开关，docs/18）：持仓与现金池跨启用账号**聚合**；
    评分走公开K线不依赖账户；单账号查询失败不拖垮整体（该账号计 0 并记 error）。
    account_key 指定时只查该账号（正式网账号切换查看，须为现货启用集合成员）。
    """
    from app.services.account_registry import spot_enabled_accounts
    from app.services.spot_client import SpotClient

    settings_dict = load_spot_settings(db)
    spot_accs = spot_enabled_accounts(db)

    if account_key:
        spot_accs = [a for a in spot_accs if a.key == account_key]
        if not spot_accs:
            raise HTTPException(status_code=400, detail=f"账号 {account_key} 不在现货启用集合内")

    # 每启用账号一个只读客户端（公开端点也用它，testnet/live base 跟随账号属性）
    clients = [SpotClient(a.key) for a in spot_accs] or [SpotClient()]
    try:
        client = clients[0]

        # 持仓聚合（只读账户查询；失败降级，前端显示「账户不可用」）
        holdings: Dict[str, float] = {}
        usdt_free = 0.0
        account_error = None
        ok_any = False
        for c in clients:
            label = c.account.key if c.account else "现货账号"
            try:
                acc = c.fetch_account()
                ok_any = True
                for b in acc["balances"]:
                    if b["asset"] in ("BTC", "ETH", "BNB"):
                        holdings[b["asset"]] = holdings.get(b["asset"], 0.0) + b["free"] + b["locked"]
                    if b["asset"] == "USDT":
                        usdt_free += b["free"] + b["locked"]
            except Exception as e:  # 未配置 / 网络 / 鉴权失败
                account_error = f"{label}: {str(e)[:150]}"
        usdt_free = round(usdt_free, 2) if ok_any else None

        # 评分（K线公开端点，不依赖账户）
        decisions = {d["symbol"]: d for d in (
            _evaluate_symbol(db, client, s) for s in SPOT_SYMBOLS
        )}

        coins = []
        for sym in SPOT_SYMBOLS:
            asset = sym.replace("USDT", "")
            d = decisions.get(sym)
            price = d["price"] if d else client.ticker_price(sym) or 0.0
            qty = holdings.get(asset)
            coins.append({
                "symbol": sym,
                "asset": asset,
                "qty": qty,
                "price": price,
                "market_value": round(qty * price, 2) if qty is not None and price else None,
                "score": d["score"] if d else None,
                "action": d["action"] if d else None,
                "lots": d["lots"] if d else None,
                "quote_amount": d["quote_amount"] if d else None,
                "plan_text": d["rules"]["plan_text"] if d else None,
                "env": d["env"] if d else None,
            })

        return {
            "testnet": client.testnet,
            "account_keys": [c.account.key for c in clients if c.account],
            "account_names": [c.account.name for c in clients if c.account],
            "coins": coins,
            "usdt_free": usdt_free,
            "settings": settings_dict,
            "account_error": account_error,
        }
    finally:
        for c in clients:
            c.close()


@router.get("/score/{symbol}")
def spot_score(symbol: str, db: Session = Depends(get_db)):
    """单币评分明细（可解释渲染）。"""
    if symbol not in SPOT_SYMBOLS:
        raise HTTPException(status_code=400, detail=f"仅支持三币: {', '.join(SPOT_SYMBOLS)}")
    from app.services.spot_client import SpotClient

    client = SpotClient()
    try:
        return _evaluate_symbol(db, client, symbol)
    finally:
        client.close()


# ---------------- 历史（docs/18 §9.2.3 运营时间轴） ----------------

@router.get("/timeline")
def spot_timeline(limit: int = 100, db: Session = Depends(get_db)):
    """运营时间轴聚合（react-chrono 数据源，docs/18 §9.2.3）：
    - order：买卖/定投事件（spot_orders，含状态与触发快照）
    - review：AI 周度研判（spot_weekly_reviews，P1 起有数据）
    - daily_check：每日评分日检（task_records type=spot，P0 即有数据）
    按时间倒序合并，最多 limit 条。
    """
    from app.models.spot import SpotWeeklyReview
    from app.models.ops import TaskRecord

    events: List[Dict[str, Any]] = []

    # 1) 买卖/定投
    for o in (
        db.query(SpotOrder)
        .order_by(SpotOrder.created_at.desc())
        .limit(min(limit, 300))
        .all()
    ):
        events.append({
            "id": f"order-{o.id}",
            "type": "order",
            "time": o.created_at.isoformat() if o.created_at else None,
            "data": {
                "symbol": o.symbol,
                "side": o.side,
                "trigger_type": o.trigger_type,
                "qty": float(o.qty) if o.qty is not None else None,
                "quote_amount": float(o.quote_amount) if o.quote_amount is not None else None,
                "price": float(o.price) if o.price is not None else None,
                "status": o.status,
                "fail_reason": o.fail_reason,
                "trigger_detail": o.trigger_detail,
                "testnet": o.testnet,
                "executed_at": o.executed_at.isoformat() if o.executed_at else None,
            },
        })

    # 2) AI 周度研判
    for r in (
        db.query(SpotWeeklyReview)
        .order_by(SpotWeeklyReview.created_at.desc())
        .limit(min(limit, 100))
        .all()
    ):
        events.append({
            "id": f"review-{r.id}",
            "type": "review",
            "time": r.created_at.isoformat() if r.created_at else None,
            "data": {
                "week": r.week,
                "regime": r.regime,
                "outlook": r.outlook,
                "risks": r.risks,
                "commentary": r.commentary,
            },
        })

    # 3) 每日评分日检（P0 的评分流水）
    for t in (
        db.query(TaskRecord)
        .filter(TaskRecord.task_type == "spot")
        .order_by(TaskRecord.started_at.desc())
        .limit(min(limit, 300))
        .all()
    ):
        detail = t.detail or {}
        events.append({
            "id": f"daily-{t.id}",
            "type": "daily_check",
            "time": t.started_at.isoformat() if t.started_at else None,
            "data": {
                "status": t.status,
                "summary": t.summary,
                "as_of": detail.get("as_of"),
                "actions": detail.get("actions"),
                "env": detail.get("env"),
                "error": t.error,
            },
        })

    events = [e for e in events if e["time"]]
    events.sort(key=lambda e: e["time"], reverse=True)
    return events[: min(limit, 300)]


# ---------------- 配置 ----------------

class SpotSettingsIn(BaseModel):
    per_order_usdt: Optional[float] = None
    max_total_usdt: Optional[float] = None
    dca_enabled: Optional[bool] = None
    dca_amount: Optional[float] = None
    usdt_reserve_pct: Optional[float] = None
    fee_rate: Optional[float] = None


@router.get("/settings")
def get_settings(db: Session = Depends(get_db)):
    return load_spot_settings(db)


@router.put("/settings")
def put_settings(body: SpotSettingsIn, db: Session = Depends(get_db)):
    from app.models.system_config import SystemConfig

    patch = {k: v for k, v in body.model_dump().items() if v is not None}
    merged, rejected = merge_spot_settings(load_spot_settings(db), patch)
    cfg = db.get(SystemConfig, 1)
    if cfg is None:
        raise HTTPException(status_code=500, detail="system_config 未初始化")
    cfg.spot_settings = merged
    db.commit()
    return {"ok": True, "settings": merged, "rejected": rejected}
