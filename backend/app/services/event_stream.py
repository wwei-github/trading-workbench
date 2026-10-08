"""P-09 事件驱动执行——连接层（docs/16，2026-10-07）。

每配置账号一条 user-data-stream（listenKey WS）；mark price 市场流按 WS 域
共享（双网账号分属 fstream/stream 两域，mark 价不同——每域一条，动态
SUBSCRIBE，绝不开每币一条连接）。事件处理进程内直调 event_executor
（manual_close_market 先例：backend 进程持锁执行交易动作）。

模式先例 feishu_bot.start_card_listener：lifespan 启动 daemon 线程、断线退避
重连（5s→60s 封顶）、异常吞掉不拖垮主服务；30 分钟结算巡检永久兜底——WS
失效最坏退化为轮询节奏，与 P-09 上线前等价。

风控口径（docs/16 P-09 风险表）：常驻连接 = 账号数 × 1 + WS 域数（mark 共享），
远低于币安 300 连接尝试/5min 限额；REST 增量 = listenKey 建立续约每 30 分钟
2 次/账号，事件结算复用巡检路径且有全账号锁互斥。
"""
import json
import logging
import threading
import time

from app.config import settings
from app.services.binance_trader import BinanceTrader

logger = logging.getLogger(__name__)

# REST base host → WS base（纯函数映射便于测试；未知 host 返回 None 线程退出）
_WS_HOSTS = {
    "fapi.binance.com": "wss://fstream.binance.com",
    "testnet.binancefuture.com": "wss://stream.binancefuture.com",
}

_KEEPALIVE_S = 1800        # listenKey 有效期 60 分钟，按 30 分钟续约
_RECONNECT_MIN_S = 5       # 重连退避下限（最坏 12 次/分钟 << 300/5min 限额）
_RECONNECT_MAX_S = 60
_STABLE_CONN_S = 60        # 连接活过此时长才视为稳定（才重置退避）——防
                           # 「连上即被踢」短命连接永久以 5s 间隔锤限流器
_RECONCILE_S = 60          # mark price 订阅集对账周期（在跑币种增减）
_RECV_TICK_S = 30          # recv 超时切片：keepalive/对账的调度粒度

_started = False
_start_mu = threading.Lock()
_stopped = threading.Event()


def _next_backoff(conn_started_at: float, backoff: float) -> float:
    """退避演化（2026-10-08 线上修复）：连接稳定（活过 _STABLE_CONN_S）才
    重置到下限；短命连接指数增长到上限。

    背景：Binance 1008 Too many requests 踢连接时，旧版在 connect 成功
    瞬间重置退避，被踢循环以 ~12 次/分钟永久锤限流器，限流永不冷却；
    修复后短命连接 5→10→20→40→60s 退避，给对端限流器冷却窗口。
    """
    if time.time() - conn_started_at >= _STABLE_CONN_S:
        return _RECONNECT_MIN_S
    return min(backoff * 2, _RECONNECT_MAX_S)


def ws_base(rest_base: str) -> str | None:
    """REST base URL → WS base URL（fapi→fstream；testnet→stream）"""
    host = (rest_base or "").split("//")[-1].rstrip("/")
    return _WS_HOSTS.get(host)


def _extract_fill_symbol(payload: dict) -> str | None:
    """ORDER_TRADE_UPDATE → 成交事件币种（FILLED/PARTIALLY_FILLED）。

    不区分开平侧/不匹配 algoId：react_to_fill 内部先查在跑记录（开仓侧
    成交时同币在跑单刚建仓无事件推进需求，settle 幂等零害），事件推断
    全部交给 settle_trades 以交易所为真源完成。
    """
    try:
        if payload.get("e") != "ORDER_TRADE_UPDATE":
            return None
        o = payload.get("o") or {}
        if o.get("X") not in ("FILLED", "PARTIALLY_FILLED"):
            return None
        return o.get("s") or None
    except Exception:
        return None


def _extract_mark(payload: dict) -> tuple[str, float] | None:
    """markPriceUpdate → (symbol, mark price)；裸 /ws 端点事件不包 data 层，
    兼容 combined 包装（data 字段）以备连接方式调整"""
    try:
        if payload.get("e") != "markPriceUpdate":
            data = payload.get("data") or {}
            if data.get("e") != "markPriceUpdate":
                return None
            payload = data
        return payload.get("s"), float(payload.get("p") or 0)
    except Exception:
        return None


def _run_user_data(account_key: str) -> None:
    """单账号 user-data-stream 线程：listenKey → 连接 → 事件 → keepalive/重连"""
    from websockets.sync.client import connect
    from app.services import event_executor

    backoff = _RECONNECT_MIN_S
    while not _stopped.is_set():
        trader = BinanceTrader(account_key)
        if not trader.configured:
            logger.info("事件流 %s 退出：账号凭据未配置", account_key)
            return
        base = ws_base(trader.base)
        if not base:
            logger.warning("事件流 %s 退出：未知 REST base %s", account_key, trader.base)
            return
        lk = None
        conn_started_at = time.time()
        try:
            lk = trader.create_listen_key()
            with connect(f"{base}/ws/{lk}", ping_interval=20, ping_timeout=10) as ws:
                logger.info("事件流 %s 已连接（user-data-stream）", account_key)
                ka_deadline = time.time() + _KEEPALIVE_S
                while not _stopped.is_set():
                    timeout = max(1.0, min(_RECV_TICK_S, ka_deadline - time.time()))
                    try:
                        msg = ws.recv(timeout=timeout)
                    except TimeoutError:
                        msg = None
                    if msg:
                        try:
                            payload = json.loads(msg)
                        except Exception:
                            payload = {}
                        if payload.get("e") == "listenKeyExpired":
                            logger.warning("事件流 %s listenKey 失效，重建连接", account_key)
                            raise ConnectionError("listenKeyExpired")
                        symbol = _extract_fill_symbol(payload)
                        if symbol:
                            try:
                                event_executor.react_to_fill(account_key, symbol)
                            except Exception:
                                logger.exception("事件反应异常 %s/%s", account_key, symbol)
                    if time.time() >= ka_deadline:
                        trader.keepalive_listen_key(lk)
                        ka_deadline = time.time() + _KEEPALIVE_S
        except Exception as e:
            if _stopped.is_set():
                return
            backoff = _next_backoff(conn_started_at, backoff)
            logger.warning("事件流 %s 断开：%s，%.0fs 后重连", account_key, e, backoff)
            _stopped.wait(backoff)
        finally:
            if lk:
                trader.close_listen_key(lk)


def _desired_mark_streams() -> set[str]:
    """mark price 订阅集 = 全账号在跑记录币种（小集合；空仓时为空集）"""
    from sqlalchemy import select

    from app.database import SessionLocal
    from app.models.trade import TradeRecord

    db = SessionLocal()
    try:
        rows = db.execute(
            select(TradeRecord.symbol).where(
                TradeRecord.status.in_(("OPENED", "TP1_HIT", "TP2_HIT")),
            ).distinct()
        ).scalars().all()
        return {f"{s.lower()}@markPrice@1s" for s in rows}
    finally:
        db.close()


def _run_mark_price(domain: str) -> None:
    """单 WS 域 mark price 线程：/ws 裸端点 + 动态 SUBSCRIBE（随在跑币种增减）。

    域内全部账号共享一条连接；事件路由按域内账号扇出（refresh_mark_pnl
    自带 60s/币节流与在跑记录校验，非本域持仓的调用在冷却闸门挡下零成本）。
    """
    from websockets.sync.client import connect
    from app.services import event_executor

    backoff = _RECONNECT_MIN_S
    while not _stopped.is_set():
        conn_started_at = time.time()
        try:
            with connect(f"{domain}/ws", ping_interval=20, ping_timeout=10) as ws:
                subs: set[str] = set()
                logger.info("mark price 流已连接（%s）", domain)
                while not _stopped.is_set():
                    # 对账：在跑币种增减 → SUBSCRIBE/UNSCRIBE 增量（每域一条
                    # 连接；增减合并为单条批量消息——逐条连发在重连风暴下
                    # 叠加连接尝试，触发 Binance 1008 限流）
                    desired = _desired_mark_streams()
                    msg_id = int(time.time())
                    to_sub = sorted(desired - subs)
                    to_unsub = sorted(subs - desired)
                    if to_sub:
                        ws.send(json.dumps({"method": "SUBSCRIBE", "params": to_sub, "id": msg_id}))
                    if to_unsub:
                        ws.send(json.dumps({"method": "UNSUBSCRIBE", "params": to_unsub, "id": msg_id + 1}))
                    subs = desired
                    # 域内账号 key 本轮缓存（避免每条 mark 事件重建 trader 实例）
                    keys = _domain_account_keys(domain)
                    deadline = time.time() + _RECONCILE_S
                    while time.time() < deadline and not _stopped.is_set():
                        try:
                            msg = ws.recv(timeout=max(1.0, min(_RECV_TICK_S, deadline - time.time())))
                        except TimeoutError:
                            msg = None
                        if not msg:
                            continue
                        try:
                            payload = json.loads(msg)
                        except Exception:
                            continue
                        mark = _extract_mark(payload)
                        if not mark:
                            continue  # SUBSCRIBE ack 等控制帧
                        symbol, price = mark
                        for key in keys:
                            try:
                                event_executor.refresh_mark_pnl(key, symbol, price)
                            except Exception:
                                logger.exception("mark 快照异常 %s/%s", key, symbol)
        except Exception as e:
            if _stopped.is_set():
                return
            backoff = _next_backoff(conn_started_at, backoff)
            logger.warning("mark price 流 %s 断开：%s，%.0fs 后重连", domain, e, backoff)
            _stopped.wait(backoff)


def _ws_accounts() -> list[BinanceTrader]:
    """已配置账号的 trader 实例（WS 域归纳用）"""
    from app.database import SessionLocal
    from app.services.account_registry import managed_accounts

    db = SessionLocal()
    try:
        out = []
        for acc in managed_accounts(db):
            t = BinanceTrader(acc.key)
            if t.configured and ws_base(t.base):
                out.append(t)
        return out
    finally:
        db.close()


def _domain_account_keys(domain: str) -> list[str]:
    """指定 WS 域内的账号 key（对账时缓存一次，事件路由用）"""
    return [t.account_key for t in _ws_accounts() if ws_base(t.base) == domain]


def start_event_stream() -> None:
    """lifespan 入口：bootstrap 线程发现账号后起 user-data + mark price 线程。

    幂等（重复调用直接返回）；账号发现失败（DB 未就绪/临时故障）不永久
    禁用——bootstrap 每 60s 重试（与 mark price 线程「无账号重查」同款）；
    任何异常吞掉只记日志：事件流不可用只影响执行时效，30 分钟巡检兜底。
    """
    global _started
    if not settings.EVENT_EXEC_ENABLED:
        return
    with _start_mu:
        if _started:
            return
        _started = True
    threading.Thread(target=_bootstrap, daemon=True, name="binance-ws-bootstrap").start()


def _bootstrap() -> None:
    """账号发现 + 线程编排：DB 未就绪/无账号时 60s 重查，成功即编排并退出"""
    while not _stopped.is_set():
        try:
            accounts = _ws_accounts()
        except Exception:
            logger.warning("事件流账号发现失败（DB 未就绪？），60s 后重试")
            accounts = None
        if accounts:
            domains: list[str] = []
            for t in accounts:
                threading.Thread(
                    target=_run_user_data, args=(t.account_key,),
                    daemon=True, name=f"binance-ws-{t.account_key}",
                ).start()
                d = ws_base(t.base)
                if d not in domains:
                    domains.append(d)
            for d in domains:
                threading.Thread(target=_run_mark_price, args=(d,),
                                 daemon=True,
                                 name=f"binance-ws-mark-{d.split('//')[-1]}").start()
            logger.info("P-09 事件流已启动：%d 账号 user-data-stream + %d 域 mark price（轮询兜底保留）",
                        len(accounts), len(domains))
            return
        if accounts == []:
            logger.info("事件流待命：无已配置账号，60s 后重查")
        _stopped.wait(60)
