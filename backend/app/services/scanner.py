"""扫描编排器（指标驱动筛选管线，2026-09-17 重设计）

筛选漏斗（指标为主、信号为触发器）：
1. 宇宙：USDT 永续，24h 成交额 ≥ MIN_VOLUME_24H（流动性门槛）
2. 体检：全池每币 1h K线 → 6 指标投票（MACD/布林/RSI/SuperTrend/ADX/OBV）；
   |净票|≥2 才补拉 4h 算第 7 票（两阶段拉取省 4h 请求）
3. 分组：7 票净票 |net|≥3 → 多头/空头候选；-2~+2 中性跳过（震荡市不开单）
4. 触发确认：方向与候选一致的入场触发（Donchian突破/黄金12形态/回踩EMA21企稳，
   见 strategy/trigger.py）；无触发 → 观察池（不落库，下轮重算）
5. 质量分排序 + 重复命中抑制（仅定时）+ 落库 → AI 配额（按质量分取前 N）

旧信号管线（Donchian+形态为主动作、EMA 硬门控）已整体替换：
方向判定权从信号路径移交给指标多数决，与 AI 提示词的多数决规则同一把尺子。
"""
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from typing import Optional
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import settings
from app.database import SessionLocal
from app.models.scan import ScanRecord, ScanResult
from app.models.system_config import SystemConfig
from app.models.trade import TradeRecord
from app.services.exchange_pool import ExchangePool
from app.services.binance_trader import BinanceTrader
from app.services.strategy.ema import analyze_ema
from app.services.strategy.indicators import volume_ratio
from app.services.strategy.technical import (
    compute_indicator_votes,
    compute_quality_score,
    HTF_SHORT,
)
from app.services.strategy.trigger import detect_trigger

logger = logging.getLogger(__name__)


def classify_volume(klines: list[list]) -> tuple[float, str]:
    """提取最新已收盘 K 线成交量并分类（比率口径复用 indicators.volume_ratio，
    与放量突破门槛 BREAKOUT_VOL_RATIO 同源）。

    klines[-1] 是未收盘 K 线，用 klines[-2]。返回 (volume, volume_type)。
    """
    if len(klines) < 22:
        return 0.0, "平量"
    vol = float(klines[-2][5])
    ratio = volume_ratio(klines)
    if ratio <= 0:  # 近20根均量为 0（volume_ratio 的兜底分支）
        return vol, "平量"
    if ratio < 0.5:
        vt = "地量"
    elif ratio < 0.8:
        vt = "缩量"
    elif ratio <= 1.2:
        vt = "平量"
    elif ratio < 2.0:
        vt = "放量"
    else:
        vt = "倍量"
    return vol, vt


def _get_scan_config(db) -> SystemConfig:
    """从数据库读取扫描策略配置"""
    cfg = db.get(SystemConfig, 1)
    if not cfg:
        # 回退到 env 默认值
        cfg = SystemConfig(
            id=1,
            ai_analysis_enabled=False,
            kline_interval=settings.KLINE_INTERVAL,
            kline_window=settings.KLINE_WINDOW,
            breakout_threshold=settings.BREAKOUT_THRESHOLD,
            r_squared_threshold=settings.R_SQUARED_THRESHOLD,
            repeat_window_hours=settings.REPEAT_WINDOW_HOURS,
            swing_order=settings.SWING_ORDER,
            pullback_tolerance=settings.PULLBACK_TOLERANCE,
        )
    return cfg


def detect_indicator_candidate(
    pool: ExchangePool, symbol: str, klines: list[list],
) -> Optional[dict]:
    """指标驱动单币检测（Scanner 与 watchlist.refresh 共用同一套逻辑与阈值）。

    流程：6 票预筛（|net6|<2 直接判中性，省 4h 请求）→ 补拉 4h 算第 7 票
    → 多数决分组（|net|≥3 且 voted≥5）→ 方向一致触发确认（trigger.py）→ 质量分。

    返回候选 dict（signal_type/quality_score/votes 及展示字段），每币至多 1 个；
    无候选（中性/票数不足/无触发 → 观察池）返回 None；异常向上抛由调用方处理。
    """
    ema = analyze_ema(klines)  # 背景展示用（方向判定不依赖 EMA）
    vol, vol_type = classify_volume(klines)

    # 两阶段投票：先 6 票预筛（|net6|<2 时第 7 票也够不到 3，省 4h 请求）
    votes6 = compute_indicator_votes(klines)
    if not votes6 or votes6["voted"] < 5 or abs(votes6["net"]) < 2:
        return None

    # 第 7 票：4h 背景（失败降级为无 4h 票，不中断）
    try:
        htf = pool.get_klines(symbol, "4h", 200)
    except Exception as e:
        htf = None
        logger.warning("%s 获取4h K线失败（跳过4h票）: %s", symbol, e)

    votes = compute_indicator_votes(klines, htf)
    if not votes or votes["voted"] < 5 or abs(votes["net"]) < 3:
        return None  # 中性/票数不足 → 不分组

    direction = 1 if votes["net"] > 0 else -1
    trig = detect_trigger(klines, direction)
    if trig is None:
        return None  # 有方向无触发 → 观察池（不落库）

    quality = compute_quality_score(direction, votes, trig["type"])
    votes_json = {
        "bull": votes["bull"], "bear": votes["bear"],
        "bull_n": votes["bull_n"], "bear_n": votes["bear_n"],
        "net": votes["net"], "voted": votes["voted"],
    }
    # 4h 背景级别快照（震荡/纠缠/弱·强多空），前端「4h方向」列展示；
    # 旧扫描行无此键显示 -
    htf_res = dict(votes["results"]).get(HTF_SHORT)
    if htf_res:
        votes_json["htf"] = {
            "state": htf_res.get("state"),
            "label": htf_res.get("state_label"),
            "dir": int(htf_res.get("dir") or 0),
        }
    return {
        "signal_type": trig["type"],
        # 现价 = 未收盘K线最新价（仅作入场价差参考）：信号判定（投票/触发/形态/量能）
        # 全部基于已收盘K线（klines[-2]），与扫描时刻在小时内的位置无关，同根K线同结果
        "current_price": float(klines[-1][4]),
        "breakout_pct": trig["breakout_pct"],
        "trend_slope": 0.0,
        "r_squared": 0.0,
        "pattern": trig.get("pattern"),
        "signal_reason": trig["reason"][:120],
        "position": trig["position"],
        "strength": quality / 100.0,
        "ema_state": ema["state"] if ema else None,
        "volume": vol,
        "volume_type": vol_type,
        "quality_score": quality,
        "votes": votes_json,
    }


class Scanner:
    """扫描编排器（同步 + 线程池并发）"""

    def __init__(self):
        self.pool = ExchangePool()
        self.concurrency = settings.BINANCE_CONCURRENCY

    def run(self, scan_record_id: UUID) -> None:
        db = SessionLocal()
        status = "failed"
        hit_count = 0
        error_count = 0
        coin_count = 0
        try:
            record = db.get(ScanRecord, scan_record_id)
            if not record:
                logger.error("扫描记录不存在: %s", scan_record_id)
                return

            # 从数据库读取扫描策略配置
            cfg = _get_scan_config(db)
            kline_interval = cfg.kline_interval
            kline_window = cfg.kline_window
            repeat_window_hours = cfg.repeat_window_hours

            logger.info("开始扫描 #%s (interval=%s, window=%d)", scan_record_id, kline_interval, kline_window)

            # 1. 获取合约交易对列表（按 24h 成交额降序）
            symbols_data = self.pool.get_usdt_swap_symbols_with_volume()

            coin_count = len(symbols_data)
            logger.info("共 %d 个合约交易对待扫描（按24h成交额降序）", len(symbols_data))

            # 24h 成交额过滤：流动性差的合约不进扫描（省K线请求，剔除不可交易标的）
            min_vol = settings.MIN_VOLUME_24H
            before = len(symbols_data)
            symbols_data = [
                s for s in symbols_data
                if float(s.get("volume_24h") or 0) >= min_vol
            ]
            logger.info(
                "24h成交额 ≥ %s USDT 过滤：%d → %d",
                f"{min_vol:,.0f}", before, len(symbols_data),
            )

            # C0 下单环境可交易过滤（2026-09-19，ARX 案例）：数据源永远走正式网
            # （ARXUSDT 有 K线、AI 照常分析），下单环境（system_config.trading_testnet
            # 决定，2026-09-20 起运行时可切）未上架 → 开单 -1121 Invalid symbol
            # 白白烧 AI 配额 + FAILED 记录。在扫描宇宙阶段用下单环境 exchangeInfo
            # 剔除；空表（拉取失败）放行不阻断
            # （_bt 实例化必须在 C0 之前——此前放在 C 段导致 UnboundLocalError，
            #   04:02/05:02 两轮扫描全崩的根因）
            _bt = BinanceTrader(bool(cfg.trading_testnet))
            listed = _bt.listed_symbols()
            if listed:
                before = len(symbols_data)
                symbols_data = [s for s in symbols_data if s["symbol"] in listed]
                logger.info("下单环境可交易过滤（%d 个合约）：%d → %d", len(listed), before, len(symbols_data))

            # C 交易所风险档位过滤（2026-09-18）：最大允许杠杆 <10× 的币不进候选
            # （交易所对其流动性/风险的终极标注；4USDT 案例）。注意不能按首档 MMR
            # 过滤——币安 2026 已把多数山寨首档 MMR 统一调到 1.5%~2%（首档名义
            # 5k~10k 内 MMR 均为此值，BTC 0.4% 是孤例），按 MMR 过滤会误拦 58%。
            # 一次 leverageBracket 全市场拉取、进程缓存 24h；失败（空表）放行不阻断
            risk_tiers = _bt.risk_tier_all()
            if risk_tiers:
                before = len(symbols_data)
                symbols_data = [
                    s for s in symbols_data
                    if (rt := risk_tiers.get(s["symbol"])) is None or rt[1] >= 10
                ]
                logger.info("风险档位过滤（最大杠杆 <10×）：%d → %d", before, len(symbols_data))

            # D 黑名单过滤（2026-09-18）：手动添加（永久）/ 连续止损自动加入（冷却期），
            # 命中不进扫描。集合为空时跳过查询
            from app.services.blacklist_service import active_symbols
            blacklisted = active_symbols(db)
            if blacklisted:
                before = len(symbols_data)
                symbols_data = [s for s in symbols_data if s["symbol"] not in blacklisted]
                logger.info("黑名单过滤（%d 个生效）：%d → %d", len(blacklisted), before, len(symbols_data))

            # 构建 symbol -> volume 映射
            volume_map = {s["symbol"]: s["volume_24h"] for s in symbols_data}
            symbols = [s["symbol"] for s in symbols_data]

            # 2. 线程池并发扫描每个币种（指标驱动筛选，见模块 docstring）
            hits: list[dict] = []

            def scan_one(symbol: str) -> tuple[str, list[dict], bool]:
                """指标多数决定向 → 触发确认 → 质量分。返回 (symbol, [候选...], is_error)"""
                try:
                    # EMA144/指标暖机需要足够历史，至少拉 300 根
                    klines = self.pool.get_klines(
                        symbol,
                        interval=kline_interval,
                        limit=max(kline_window, 300),
                    )
                    cand = detect_indicator_candidate(self.pool, symbol, klines)
                    if cand is None:
                        return symbol, [], False
                    cand["symbol"] = symbol
                    return symbol, [cand], False
                except Exception as e:
                    logger.warning("扫描 %s 失败: %s", symbol, e)
                    return symbol, [], True

            with ThreadPoolExecutor(max_workers=self.concurrency) as executor:
                futures = {executor.submit(scan_one, s): s for s in symbols}
                for future in as_completed(futures):
                    symbol, signals, is_error = future.result()
                    if is_error:
                        error_count += 1
                    for sig in signals:
                        sig["volume_24h"] = volume_map.get(symbol, 0)
                        hits.append({"symbol": symbol, **sig})

            # 3. 按质量分降序（同分按 24h 成交额）——AI 配额按此顺序取前 N
            hits.sort(
                key=lambda x: (x.get("quality_score", 0), x.get("volume_24h", 0)),
                reverse=True,
            )

            # 4. 重复命中抑制（仅定时扫描）：同 (symbol, signal_type) 在
            #    repeat_window_hours 内已有命中的本轮不落库——持续趋势里 Donchian 突破
            #    每根收盘K线都在创新高，不抑制会同币同信号每轮重复命中。
            #    手动扫描不做抑制（2026-09-20）：手动 = 对最近已收盘K线的完整评估，
            #    与 :02 定时扫描同一根K线应得同样结果；抑制会让手动结果
            #    「看起来完全不同」（实为定时已落库的命中被滤掉）。
            #    AI 成本由指纹缓存（FINGERPRINT_TTL_MIN）兜底，同币同信号不重复调 LLM
            if record.scan_type == "scheduled":
                new_hits, suppressed = self._suppress_repeat_hits(hits, repeat_window_hours)
                if suppressed:
                    logger.info("重复命中抑制 #%s：%d 条已在 %dh 内命中过，不再落库", scan_record_id, suppressed, repeat_window_hours)
            else:
                new_hits, suppressed = hits, 0

            # 5. 写入结果（用独立 session，避免长时间扫描后主 session 连接失效）
            self._save_results(scan_record_id, new_hits)

            # 6. 触发 AI 分析（任务内自检开关；批量按24h成交额取前 AI_MAX_PER_SCAN 个）
            # 满仓跳过（docs/06）：定时扫描时在跑单已达上限则不做 AI 分析（省 LLM 成本，
            # 反正开不了仓）；手动扫描/手动分析不受限。下次结算释放额度后自然恢复
            from app.tasks.ai_tasks import run_ai_analysis_task
            try:
                if record.scan_type == "scheduled" and self._slots_full():
                    logger.info("在跑单子已达上限，定时扫描后跳过本轮 AI 分析 #%s", scan_record_id)
                else:
                    run_ai_analysis_task.delay(str(scan_record_id))
            except Exception as e:
                logger.warning("AI 分析分发失败（忽略）: %s", e)

            hit_count = len(new_hits)
            status = "completed"

            logger.info(
                "扫描完成 #%s: 扫描%d个, 命中%d个（抑制重复%d个）",
                scan_record_id,
                len(symbols),
                len(new_hits),
                suppressed,
            )
        except Exception as e:
            logger.exception("扫描异常 #%s: %s", scan_record_id, e)
        finally:
            # 确保扫描状态总是更新（用独立 session 避免事务已中止）
            try:
                db.rollback()
            except Exception:
                pass
            self._update_scan_status(scan_record_id, status, hit_count, error_count, coin_count)
            db.close()

    @staticmethod
    def _save_results(
        scan_record_id: UUID,
        hits: list[dict],
    ) -> None:
        """用独立 session 写入扫描结果，避免主 session 连接失效或死锁"""
        if not hits:
            return
        sdb = SessionLocal()
        try:
            for h in hits:
                sdb.add(
                    ScanResult(
                        scan_record_id=scan_record_id,
                        symbol=h["symbol"],
                        signal_type=h.get("signal_type", "unknown"),
                        current_price=h["current_price"],
                        breakout_pct=h["breakout_pct"],
                        trend_slope=h.get("trend_slope", 0),
                        r_squared=h.get("r_squared", 0),
                        pattern=h.get("pattern"),
                        signal_reason=h.get("signal_reason"),
                        strength=h.get("strength"),
                        ema_state=h.get("ema_state"),
                        position=h.get("position"),
                        volume_24h=h.get("volume_24h", 0),
                        volume=h.get("volume", 0),
                        volume_type=h.get("volume_type", "平量"),
                        quality_score=h.get("quality_score"),
                        votes_json=h.get("votes"),
                    )
                )
            sdb.commit()
            logger.info("写入 %d 条扫描结果 #%s", len(hits), scan_record_id)
        except Exception as e:
            logger.exception("写入扫描结果失败 #%s: %s", scan_record_id, e)
            sdb.rollback()
            raise
        finally:
            sdb.close()

    @staticmethod
    def _update_scan_status(
        scan_record_id: UUID,
        status: str,
        hit_count: int,
        error_count: int,
        coin_count: int,
    ) -> None:
        """独立 session 更新扫描状态，确保即使主事务失败也能写入"""
        sdb = SessionLocal()
        try:
            record = sdb.get(ScanRecord, scan_record_id)
            if record:
                record.status = status
                record.hit_count = hit_count
                record.error_count = error_count
                if coin_count:
                    record.coin_count = coin_count
                record.finished_at = datetime.utcnow()
                sdb.commit()
        except Exception as e:
            logger.exception("更新扫描状态失败 #%s: %s", scan_record_id, e)
            sdb.rollback()
        finally:
            sdb.close()

    @staticmethod
    def _slots_full() -> bool:
        """在跑单子数是否已达 max_open_trades 上限（独立短会话，避免长扫描后连接失效）。

        查询失败按未满仓处理：宁可多花一次 AI 分析，不因统计故障漏掉开仓机会。
        """
        sdb = SessionLocal()
        try:
            cfg = sdb.get(SystemConfig, 1)
            if not cfg:
                return False
            running = sdb.execute(
                select(func.count(TradeRecord.id)).where(
                    TradeRecord.status.in_(("OPENED", "TP1_HIT", "TP2_HIT"))
                )
            ).scalar() or 0
            # 0 = 不限单数，仅靠余额风控兜底
            if not cfg.max_open_trades or cfg.max_open_trades <= 0:
                return False
            return running >= cfg.max_open_trades
        except Exception:
            logger.exception("查询在跑单子数失败，按未满仓处理")
            return False
        finally:
            sdb.close()

    @staticmethod
    def _suppress_repeat_hits(hits: list[dict], repeat_window_hours: int) -> tuple[list[dict], int]:
        """重复命中抑制：同 (symbol, signal_type) 在 repeat_window_hours 内已有命中
        记录的，本轮不再落库——持续趋势里 Donchian 突破每根收盘K线都在创新高，
        不抑制会同币同信号每轮重复命中、重复触发 AI 分析。

        用独立 session 避免与主事务死锁；查询失败按未重复处理（不拦新信号）。
        返回 (新命中列表, 抑制条数)。
        """
        if not hits:
            return [], 0
        seen: set[tuple[str, str]] = set()
        sdb = SessionLocal()
        try:
            since = datetime.utcnow() - timedelta(hours=repeat_window_hours)
            symbols = list({h["symbol"] for h in hits})
            rows = sdb.execute(
                select(ScanResult.symbol, ScanResult.signal_type).where(
                    ScanResult.symbol.in_(symbols), ScanResult.created_at >= since
                )
            ).all()
            seen = {(r[0], r[1] or "unknown") for r in rows}
        except Exception as e:
            logger.warning("查询重复命中失败（按未重复处理）: %s", e)
            sdb.rollback()
        finally:
            sdb.close()
        new_hits = [
            h for h in hits
            if (h["symbol"], h.get("signal_type", "unknown")) not in seen
        ]
        return new_hits, len(hits) - len(new_hits)
