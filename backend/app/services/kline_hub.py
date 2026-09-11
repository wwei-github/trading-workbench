"""KlineHub：实时 K 线聚合推送（docs/07）

- 上游：单条币安 USDT-M WebSocket（wss://fstream.binance.com/ws raw 端点），
  按 (symbol, interval) 频道动态 SUBSCRIBE/UNSUBSCRIBE，所有浏览器/图表共享
- 断线：指数退避重连 3 次（1/2/4s）→ 降级 REST 轮询（3s/次，缓存双旁路），
  REST 期间每 30s 探测 WS 恢复；WS_OK 态看门狗对 10s 无事件的频道转 REST 兜底
  （币安 fstream 会整类静默：实测 bookTicker 有帧而 kline/aggTrade/markPrice 零帧）
- 中文合约（牛来USDT 等非 ASCII 符号）：币安 WS 订阅 ACK 成功但永不推帧（静默），
  这类频道不进 WS 订阅，由看门狗按 REST 周期（3s）轮询兜底
- 下游：SSE 端点为每条连接注册一个 asyncio.Queue（maxsize=64），满则丢最旧
  （latest-wins：kline 帧是当前 bar 的完整替换，幂等无信息损失）
- 归属：仅 uvicorn API 进程启动（惰性：首个订阅才连上游）；celery 容器不 import app.main
- 线程安全：所有方法只在事件循环内调用且检查-修改间不插入 await（纯同步），
  唯一跨线程边界是同步的 ExchangePool——调用一律 asyncio.to_thread 包装
"""
import asyncio
import json
import logging
import time

import websockets

from app.config import settings
from app.services.exchange_pool import ExchangePool

logger = logging.getLogger(__name__)

ChannelKey = tuple[str, str]  # (symbol_lower, interval)

_QUEUE_SIZE = 64          # 每个订阅者的下行信箱深度（≈16s 的 WS 帧缓冲）
_BACKOFF_S = [1, 2, 4, 8, 16, 30]   # WS 重连退避序列
_WS_MAX_ATTEMPTS = 3      # 连续失败该次数后进入 REST 降级
_REST_POLL_S = 3          # REST 降级轮询周期；亦是 WS_OK 态看门狗巡检周期
_WS_PROBE_S = 30          # REST 降级期间探测 WS 恢复的间隔
_WATCHDOG_STALE_S = 10    # WS_OK 态频道无事件判定阈值（上游静默/下架符号），超时转 REST 兜底


class KlineHub:
    """币安 WS 上游 + SSE 下游的进程内聚合器（单例）"""

    def __init__(self) -> None:
        self._subs: dict[ChannelKey, set] = {}
        self._last_bar: dict[ChannelKey, dict] = {}
        self._last_event_ts: dict[ChannelKey, float] = {}
        self._last_degraded_msg: dict[ChannelKey, str] = {}
        self._desired: set[ChannelKey] = set()
        self._upstream_task: asyncio.Task | None = None
        self._sweep_task: asyncio.Task | None = None
        self._ws = None  # websockets 客户端连接
        self._state = "idle"  # idle / connecting / ws / rest
        self._ws_attempt = 0
        self._req_id = 0

    # ── 订阅管理（SSE 端点调用）──

    def subscribe(self, ch: ChannelKey):
        """注册一个下行信箱；首个订阅者触发上游任务启动"""
        q: asyncio.Queue = asyncio.Queue(maxsize=_QUEUE_SIZE)
        self._subs.setdefault(ch, set()).add(q)
        self._desired.add(ch)
        # 频道已处于降级态时，把当前降级原因直接告知新订阅者
        if self._last_degraded_msg.get(ch):
            self._offer(q, ("degraded", {"message": self._last_degraded_msg[ch]}))
        if self._state == "idle":
            self._state = "connecting"
            self._upstream_task = asyncio.create_task(self._run())
            self._start_sweep()
            logger.info("KlineHub 启动：频道 %s", ch)
        elif self._state == "ws" and self._ws is not None:
            # 热订阅：对现连接直接 SUBSCRIBE（rest/connecting 态由 sweep/建连覆盖）
            asyncio.ensure_future(self._send_subscribe([ch]))
        return q

    def unsubscribe(self, ch: ChannelKey, q) -> None:
        subs = self._subs.get(ch)
        if not subs:
            return
        subs.discard(q)
        if subs:
            return
        # 频道最后一个订阅者退出 → GC
        self._subs.pop(ch, None)
        self._desired.discard(ch)
        self._last_bar.pop(ch, None)
        self._last_event_ts.pop(ch, None)
        self._last_degraded_msg.pop(ch, None)
        if self._state == "ws" and self._ws is not None:
            asyncio.ensure_future(self._send_unsubscribe([ch]))
        if not self._desired:
            logger.info("KlineHub 无订阅者，上游连接关闭")
            self._state = "idle"
            self._cancel_tasks()

    def snapshot(self, ch: ChannelKey) -> dict | None:
        """该频道最新一帧 bar（新订阅者快照；可能为 None）"""
        return self._last_bar.get(ch)

    async def stop(self) -> None:
        """lifespan shutdown：取消全部后台任务并归零"""
        self._state = "idle"
        self._cancel_tasks()
        self._subs.clear()
        self._desired.clear()
        self._last_bar.clear()
        self._last_event_ts.clear()
        self._last_degraded_msg.clear()

    # ── 上游：币安 WS 总控 ──

    async def _run(self) -> None:
        """总控协程：连接 → 订阅 → 收流 → 断线退避 → 3 败进 REST 降级 → 探测恢复"""
        try:
            while self._state != "idle" and self._desired:
                self._state = "connecting"
                try:
                    async with websockets.connect(
                        f"{settings.BINANCE_WS_URL}/ws",
                        ping_interval=20, ping_timeout=20, open_timeout=10,
                    ) as ws:
                        self._ws = ws
                        self._ws_attempt = 0
                        self._state = "ws"
                        sub_ch = [c for c in sorted(self._desired) if c[0].isascii()]
                        await self._send_subscribe(sub_ch)
                        logger.info("KlineHub 币安 WS 已连接，订阅 %d 频道", len(sub_ch))
                        async for raw in ws:
                            self._handle_ws_message(raw)
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    logger.warning("KlineHub WS 断开: %s", e)
                finally:
                    self._ws = None
                if self._state == "idle":
                    return
                self._ws_attempt += 1
                if self._ws_attempt >= _WS_MAX_ATTEMPTS:
                    await self._rest_fallback_loop()
                    # 探测恢复（_state 被置回 connecting 由外层重连）或 idle 退出
                    if self._state == "idle":
                        return
                    self._ws_attempt = 0
                else:
                    delay = _BACKOFF_S[min(self._ws_attempt - 1, len(_BACKOFF_S) - 1)]
                    logger.info("KlineHub %.0fs 后重试 WS（第 %d 次）", delay, self._ws_attempt)
                    await asyncio.sleep(delay)
        except asyncio.CancelledError:
            pass
        finally:
            if self._state != "idle":
                self._state = "idle"

    async def _rest_fallback_loop(self) -> None:
        """REST 降级：sweep 持续轮询，每 30s 探测一次 WS，恢复后返回交还主循环重连"""
        self._state = "rest"
        self._start_sweep()
        logger.warning(
            "KlineHub WS 连续 %d 次失败，降级 REST 轮询（%ds/次），每 %ds 探测恢复",
            self._ws_attempt, _REST_POLL_S, _WS_PROBE_S,
        )
        while self._state == "rest" and self._desired:
            await asyncio.sleep(_WS_PROBE_S)
            if self._state != "rest" or not self._desired:
                return
            if await self._probe_ws():
                logger.info("KlineHub 探测到 WS 恢复，重新连接")
                self._state = "connecting"
                return

    async def _probe_ws(self) -> bool:
        try:
            ws = await asyncio.wait_for(
                websockets.connect(
                    f"{settings.BINANCE_WS_URL}/ws",
                    ping_interval=20, ping_timeout=20, open_timeout=10,
                ),
                timeout=15,
            )
            await ws.close()
            return True
        except Exception:
            return False

    async def _send_subscribe(self, channels: list[ChannelKey]) -> None:
        await self._send_sub_msg("SUBSCRIBE", channels)

    async def _send_unsubscribe(self, channels: list[ChannelKey]) -> None:
        await self._send_sub_msg("UNSUBSCRIBE", channels)

    async def _send_sub_msg(self, method: str, channels: list[ChannelKey]) -> None:
        # 非 ASCII 符号不进 WS：订阅能 ACK 但币安永不推帧，且异常 stream name
        # 可能触发 1008 整连接踢出（殃及其他频道），统一走 REST 轮询
        channels = [c for c in channels if c[0].isascii()]
        if not channels or self._ws is None:
            return
        self._req_id += 1
        streams = [f"{s}@kline_{i}" for s, i in channels]
        try:
            await self._ws.send(json.dumps(
                {"method": method, "params": streams, "id": self._req_id}
            ))
        except Exception as e:
            logger.warning("KlineHub %s 发送失败: %s", method, e)

    def _handle_ws_message(self, raw) -> None:
        """解析 kline 数据帧（SUBSCRIBE ack 等忽略），映射为 8 字段契约的精简 bar"""
        try:
            payload = json.loads(raw)
        except Exception:
            return
        k = payload.get("k")
        if not k:
            return
        ch = (str(payload.get("s", "")).lower(), str(k.get("i", "")))
        if ch not in self._desired:
            return
        self._last_degraded_msg.pop(ch, None)  # WS 帧已恢复，清除降级提示
        self._publish(ch, {
            "t": int(k["t"]), "o": float(k["o"]), "h": float(k["h"]),
            "l": float(k["l"]), "c": float(k["c"]), "v": float(k["v"]),
            "x": bool(k.get("x", False)),
        })

    # ── sweep：REST 降级轮询（rest 态）/ 看门狗（ws 态）──

    def _start_sweep(self) -> None:
        if self._sweep_task is None or self._sweep_task.done():
            self._sweep_task = asyncio.create_task(self._sweep_loop())

    def _cancel_tasks(self) -> None:
        for t in (self._upstream_task, self._sweep_task):
            if t is not None and not t.done():
                t.cancel()
        self._upstream_task = None
        self._sweep_task = None
        self._ws = None

    async def _sweep_loop(self) -> None:
        """双职能循环：rest 态=REST 轮询推帧；ws/connecting 态=看门狗兜底"""
        while self._state != "idle":
            try:
                if self._state == "rest":
                    await self._sweep_rest()
                    await asyncio.sleep(_REST_POLL_S)
                elif self._state == "ws":
                    await self._sweep_watchdog()
                    await asyncio.sleep(_REST_POLL_S)
                else:
                    await asyncio.sleep(1)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning("KlineHub sweep 异常: %s", e)
                await asyncio.sleep(_REST_POLL_S)

    async def _sweep_rest(self) -> None:
        """REST 轮询：逐频道取最新一根（缓存双旁路），全失败推 degraded"""
        pool = ExchangePool()
        for ch in sorted(self._desired):
            bar = await self._rest_bar(pool, ch)
            if bar is None:
                continue
            self._publish(ch, bar)

    async def _sweep_watchdog(self) -> None:
        """看门狗：WS_OK 态下兜底推帧——非 ASCII 符号每次巡检都 REST 轮询
        （币安 WS 静默无数据），其余频道 10s 无事件转 REST 兜底并告知订阅者
        （覆盖 fstream 交易流故障期与下架符号；WS 帧恢复后提示自动清除）"""
        now = time.monotonic()
        due = [
            ch for ch in list(self._subs.keys())
            if not ch[0].isascii()
            or now - self._last_event_ts.get(ch, 0) >= _WATCHDOG_STALE_S
        ]
        if not due:
            return
        pool = ExchangePool()
        for ch in due:
            if ch[0].isascii():
                self._push_degraded(ch, "币安合约WS无K线数据，已降级REST轮询（约10s/帧）")
            bar = await self._rest_bar(pool, ch)
            if bar is not None:
                self._publish(ch, bar)

    async def _rest_bar(self, pool: ExchangePool, ch: ChannelKey) -> dict | None:
        """REST 取最新一根 bar；失败推 degraded（按频道去重）"""
        symbol, interval = ch
        try:
            klines = await asyncio.to_thread(
                pool.get_recent_klines, symbol.upper(), interval, 2
            )
        except Exception as e:
            self._push_degraded(ch, str(e)[:200])
            return None
        if not klines:
            return None
        k = klines[-1]
        return {
            "t": int(k[0]), "o": float(k[1]), "h": float(k[2]),
            "l": float(k[3]), "c": float(k[4]), "v": float(k[5]),
            "x": float(k[6]) <= time.time() * 1000,  # close_time 已过 → 已收盘
        }

    # ── 下游发布 ──

    def _publish(self, ch: ChannelKey, bar: dict) -> None:
        self._last_bar[ch] = bar
        self._last_event_ts[ch] = time.monotonic()
        for q in list(self._subs.get(ch, ())):
            self._offer(q, ("bar", bar))

    def _push_degraded(self, ch: ChannelKey, message: str) -> None:
        """降级消息按频道去重：内容不变不重发，避免轮询失败期间刷屏"""
        if self._last_degraded_msg.get(ch) == message:
            return
        self._last_degraded_msg[ch] = message
        for q in list(self._subs.get(ch, ())):
            self._offer(q, ("degraded", {"message": message}))

    @staticmethod
    def _offer(q: asyncio.Queue, item) -> None:
        """满则丢最旧再入队（latest-wins，kline 帧幂等）"""
        if q.full():
            try:
                q.get_nowait()
            except asyncio.QueueEmpty:
                pass
        q.put_nowait(item)


kline_hub = KlineHub()
