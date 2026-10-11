"""自动交易定时任务（docs/06）

每小时整点：settle_trades 结算/跟进在跑单子（TP/SL 事件推进、保本移损、
跟进止损、SL 挂单补挂、强平判定）。开仓不在本任务——2026-09-14 起唯一开仓
通道是即时开仓：AI 分析落库即由 ai_tasks 分发 open_trade_for_analysis
（suggest 且 ≥60 分），与结算共用同一把 Redis 开仓锁串行执行，
防「在跑单上限 / 70% 余额」在并发下被同时绕过。

每次运行写任务记录（含跳过原因），失败进「系统日志」。
"""
import logging
from typing import Optional
from uuid import UUID, uuid4

from redis import Redis
from sqlalchemy import select

from app.celery_app import celery_app
from app.config import settings
from app.database import SessionLocal
from app.models.scan import AIAnalysis
from app.models.system_config import SystemConfig
from app.models.trade import TradeRecord
from app.services.binance_trader import BinanceTrader
from app.services.task_log import close_task, open_task
from app.services.trade_engine import (
    _mark_open_block,
    settle_trades,
    sync_account_transfers,
    try_open_for_analysis,
)

logger = logging.getLogger(__name__)

# 开仓互斥锁（docs/17 P1 按账号拆分）已提取至 services/trade_locks.py（2026-10-07
# P-09：事件流模块同在 services 层共用；以下划线别名保持本文件内调用点不变）
from app.services.trade_locks import (  # noqa: E402
    OPEN_LOCK_TTL_S as _OPEN_LOCK_TTL_S,
    OPEN_LOCK_WAIT_S as _OPEN_LOCK_WAIT_S,
    acquire_open_lock as _acquire_open_lock,
    open_lock_key as _open_lock_key,
    release_open_lock as _release_open_lock,
)


def _mark_analysis_block(analysis_id: str, reason: str) -> None:
    """闸门之外丢败的兜底落因（2026-09-15）：等锁超时 / 任务执行异常同样写
    ai_analyses.open_block_reason——此前这类丢败既无交易记录也无拒开原因，
    前端"开单状态"列显示"-"成为悬案（SENTUSDT 2026-09-15 实例）。尽力而为，
    落因失败仅记日志。"""
    db = SessionLocal()
    try:
        a = db.get(AIAnalysis, UUID(str(analysis_id)))
        if a is not None:
            _mark_open_block(db, a, reason)
    except Exception:
        logger.exception("开仓失败原因兜底落库失败 analysis=%s", analysis_id)
    finally:
        db.close()


def manual_close_market(trade_id: str) -> dict:
    """手动市价平仓共享执行核心（2026-09-25，飞书卡片按钮与网页交易记录按钮同源）。

    校验状态 → 按开单账号选 trader（docs/17：account_key 缺失旧行按 testnet
    回落）→ 账号口径开仓锁（与该账号结算巡检/开仓互斥）→ 锁内重读防竞态 →
    close_market_now（先平仓后撤单，失败时 SL/TP 原封未动）。

    返回 {"ok": True, "symbol", "pnl", "pnl_pct"} 或 {"ok": False, "error"}；
    调用方（飞书侧发结果卡片 / API 侧映射 HTTP 状态码）只管呈现。
    """
    from app.services.trade_engine import close_market_now

    res: dict = {"ok": False, "error": "未知错误"}
    db = SessionLocal()
    try:
        rec = db.get(TradeRecord, UUID(str(trade_id)))
        if not rec:
            res = {"ok": False, "error": "交易记录不存在"}
        elif rec.status not in ("OPENED", "TP1_HIT", "TP2_HIT"):
            res = {"ok": False, "error": f"该交易已不在跑（{rec.status}），无需平仓"}
        else:
            trader = BinanceTrader(rec.account_key or bool(rec.testnet))
            if not trader.configured:
                res = {"ok": False, "error": "币安 API 凭据未配置，无法执行"}
            else:
                r = Redis.from_url(settings.REDIS_URL, decode_responses=True)
                ident = f"manual-close-{uuid4().hex[:8]}"
                # 账号口径锁（docs/17 P1）：与该账号的结算巡检/开仓互斥
                lock_acc = rec.account_key or ("testnet-1" if rec.testnet else "live-1")
                if not _acquire_open_lock(r, ident, wait_s=20, ttl_s=_OPEN_LOCK_TTL_S,
                                          account=lock_acc):
                    res = {"ok": False, "error": "系统忙（巡检/开仓中），请稍后重试"}
                else:
                    try:
                        db.refresh(rec)  # 锁内重读：巡检可能刚好把单子平掉了
                        if rec.status not in ("OPENED", "TP1_HIT", "TP2_HIT"):
                            res = {"ok": False, "error": f"该交易刚被结算平仓（{rec.status}）"}
                        else:
                            out = close_market_now(db, trader, rec)
                            res = {"ok": True, "symbol": rec.symbol,
                                   "pnl": float(out["pnl"]), "pnl_pct": out.get("pnl_pct")}
                    except Exception as e:
                        db.rollback()
                        logger.exception("手动市价平仓失败 %s", trade_id)
                        res = {"ok": False, "error": f"平仓失败：{str(e)[:80]}"}
                    finally:
                        _release_open_lock(r, ident, account=lock_acc)
    finally:
        db.close()
    return res


@celery_app.task(name="app.tasks.trade_tasks.run_auto_trade_task", max_retries=0)
def run_auto_trade_task():
    task_id = open_task(task_type="trade", task_name="结算巡检", trigger="scheduled")
    try:
        from app.services.account_registry import managed_accounts

        db = SessionLocal()
        try:
            # 2026-10-03 删环境切换：巡检/接管一律双网（测试网账号 + 正式网
            # 启用账号）——管理行为覆盖所有已配置账号，是否有单由交易所决定
            accs = managed_accounts(db)
            adopt_accs = accs
        finally:
            db.close()
        if not accs:
            close_task(task_id, "skipped", summary="无可用交易账号（.env 未配置 key），跳过")
            return
        # 主账号实例（settle_trades 内部仍按记录自带 account_key 逐账号结算，docs/17 P0）
        trader = BinanceTrader(accs[0].key)
        if not trader.configured:
            logger.warning("结算巡检缺少币安 API 凭据（主账号 %s），跳过", accs[0].key)
            close_task(task_id, "skipped", summary="缺少币安 API 凭据，跳过")
            return

        # 锁集合 = 启用账号 ∪ 存量在跑单账号（切走的账号其存量单仍要结算，docs/17 P1）
        db = SessionLocal()
        try:
            run_keys = {
                (k or ("testnet-1" if t else "live-1"))
                for k, t in db.execute(
                    select(TradeRecord.account_key, TradeRecord.testnet).where(
                        TradeRecord.status.in_(("OPENED", "TP1_HIT", "TP2_HIT"))
                    )
                ).all()
            }
        finally:
            db.close()
        lock_keys = list(dict.fromkeys(
            [a.key for a in accs] + [a.key for a in adopt_accs] + sorted(run_keys)
        ))

        r = Redis.from_url(settings.REDIS_URL, decode_responses=True)
        ident = uuid4().hex
        acquired: list[str] = []
        for k in lock_keys:
            if _acquire_open_lock(r, ident, wait_s=120, ttl_s=_OPEN_LOCK_TTL_S, account=k):
                acquired.append(k)
            else:
                break
        if len(acquired) < len(lock_keys):
            for k in acquired:
                _release_open_lock(r, ident, account=k)
            close_task(task_id, "skipped", summary="开仓锁被即时开仓长期占用，本轮跳过")
            return
        try:
            db = SessionLocal()
            adopted_all: list[str] = []
            hold_cards: list[dict] = []  # 持仓时长预警卡（锁外发送）
            try:
                settled, advice_ids = settle_trades(db, trader)
                # 手动单接管 import 必须无条件执行（2026-10-02 修复）：函数内
                # from import 是局部绑定，此前放在 if not trader.testnet 分支内，
                # 当前环境测试网时该分支跳过 → 循环里 auto_adopt_positions
                # UnboundLocalError 且被 except 静默吞掉——正式网手动单失管的
                # 直接原因（叠加 enabled_accounts 不含正式网账号的清单问题）
                from app.services.trade_engine import (
                    _sweep_orphan_algo_orders,
                    auto_adopt_positions,
                )
                # 自动导入手动持仓（2026-09-24）：交易所侧未被系统在跑记录接管的
                # 非零持仓 = App 手动单，SL/TP 取 App 现挂条件单原值接管
                # （裸仓/校验不过跳过只记日志）。2026-10-03 拍板：主账号无条件
                # 接管（测试网环境 testnet-1 也导入测试网手动单，取代 2026-09-29
                # "测试网不接管"写死）+ 正式网启用账号照旧 → 测试网环境两网手动
                # 单都导入；正式网环境仅正式网（下方循环与主账号去重）
                try:
                    adopted_all.extend(auto_adopt_positions(db, trader))
                except Exception:
                    logger.exception("自动导入持仓失败（%s）", trader.account_key)
                # 多账号手动单接管 + 孤儿清扫（docs/17 P1）：主账号已在上方接管，
                # 这里遍历其余正式网启用账号各管各的（adopt_accs 只含正式网账号，
                # 与主账号 key 去重——测试网环境主账号 testnet-1 不在清单内）
                for acc in adopt_accs:
                    if acc.key == trader.account_key:
                        continue
                    at = BinanceTrader(acc.key)
                    if not at.configured:
                        continue
                    run_syms = {
                        rec.symbol for rec in db.execute(
                            select(TradeRecord).where(
                                TradeRecord.status.in_(("OPENED", "TP1_HIT", "TP2_HIT")),
                                TradeRecord.account_key == acc.key,
                            )
                        ).scalars()
                    }
                    # 其余启用账号同样自动导入手动持仓
                    try:
                        adopted_all.extend(auto_adopt_positions(db, at))
                    except Exception:
                        logger.exception("自动导入持仓失败（%s）", acc.key)
                    try:
                        _sweep_orphan_algo_orders(at, run_syms, at.positions())
                    except Exception:
                        logger.exception("孤儿清扫失败（%s）", acc.key)
                # 持仓时长分级警告（2026-09-27）：在跑单 >12h 黄卡 / >24h 红卡。
                # 锁内只做判定与 DB 标记（毫秒级），卡片在开仓锁外发送
                try:
                    from app.services.trade_engine import check_hold_warnings
                    hold_cards = check_hold_warnings(db)
                except Exception:
                    logger.exception("持仓时长警告判定失败")
            finally:
                db.close()
            if adopted_all:
                try:
                    from app.services.feishu_bot import send_app_text
                    send_app_text(
                        "📥 自动导入持仓：" + "、".join(adopted_all)
                        + "（币安 App 手动单已接管，SL/TP 按原条件单重挂，后续由系统统一管理）"
                    )
                except Exception:
                    logger.exception("自动导入持仓飞书通知失败")
        finally:
            for k in acquired:
                _release_open_lock(r, ident, account=k)

        # 持仓时长预警卡发送（2026-09-27，锁外）：飞书 HTTP 不进开仓锁；
        # 失败只记日志不影响巡检结果。_symbol/_hours 为构建侧日志字段，发送前剔除
        if hold_cards:
            from app.services.notify import send_feishu_card
            for c in hold_cards:
                symbol = c.pop("_symbol", "?")
                c.pop("_hours", None)
                try:
                    send_feishu_card(c)
                except Exception:
                    logger.exception("持仓时长警告卡片发送失败：%s", symbol)

        # AI 持仓诊断派发（docs/16）：锁外逐单派发子任务——LLM 调用（10~30s）
        # 绝不进开仓锁内，失败隔离（单笔失败不影响其余诊断与结算结果）
        for tid in advice_ids:
            run_position_advice.delay(tid)

        # 资金划转同步（账户级读操作，无需开仓锁，放在锁外不延长巡检持锁时间；
        # 两个环境各自同步；失败只记日志不影响本轮结算结果）
        transfers = 0
        try:
            db = SessionLocal()
            try:
                transfers = sync_account_transfers(db)
            finally:
                db.close()
        except Exception:
            logger.exception("资金划转同步失败")

        logger.info("结算巡检完成：%d 笔", settled)
        close_task(
            task_id, "completed",
            summary=f"结算/巡检 {settled} 笔"
                    + (f"，新增划转 {transfers} 条" if transfers else "")
                    + (f"，派发持仓诊断 {len(advice_ids)} 单" if advice_ids else ""),
            detail={"settled": settled, "transfers_synced": transfers,
                    "advice_dispatched": len(advice_ids)},
        )
    except Exception as e:
        logger.exception("结算巡检任务失败: %s", e)
        close_task(task_id, "failed", error=e)


@celery_app.task(name="app.tasks.trade_tasks.open_trade_for_analysis", max_retries=0)
def open_trade_for_analysis(analysis_id: str):
    """单条 AI 分析完成即开仓——P1 扇出（docs/17 §3，2026-09-23）。

    AI 分析共享一次，对双网自动交易清单（auto_trade_accounts：测试网开关 +
    正式网开关各自判定的账号，2026-10-03 起与当前环境无关）每个账号独立过
    闸门/下单（各自钱包、风险基数、在跑上限；串行下单共享 IP 权重）。
    账号锁 lock:trade_open:{key} 与结算巡检互斥；单账号失败不影响其他账号。
    等锁超时或闸门未过即放弃（无重试）。任一账号成交即清除拒开原因残留
    （避免「已开单仍显示拒开文案」误导）。

    正式网自动开单开关关闭时，达标信号错过实盘开仓 → 飞书提醒（2026-10-09）；
    双网开关全关在分发层（_dispatch_immediate_open）已短路，不会进本任务。
    """
    from app.services.account_registry import auto_trade_accounts

    # 开关/启用账号以 DB 为运行时权威（env 仅首次建行注入初始值）；
    # 双网独立开关（2026-10-03 拆分）：测试网/正式网各自判定、可同时开启，
    # 与当前环境无关（不再用 enabled_accounts 按环境过滤）
    db = SessionLocal()
    try:
        cfg = db.get(SystemConfig, 1)
        if cfg is None or not (cfg.trading_enabled_testnet or cfg.trading_enabled_live):
            logger.info("即时开仓跳过：双网自动交易开关均关闭（analysis=%s）", analysis_id)
            return
        # 正式网自动开单关闭：达标信号将错过实盘开仓 → 飞书提醒（2026-10-09）。
        # 测试网照常扇出；提醒自带异常兜底，失败不影响开仓流程
        if not cfg.trading_enabled_live:
            from app.services.trade_engine import notify_live_auto_open_off
            notify_live_auto_open_off(db, cfg, str(analysis_id))
        accs = auto_trade_accounts(db)
    finally:
        db.close()
    if not accs:
        logger.warning("即时开仓跳过：双网开关开启但无可用交易账号（analysis=%s）", analysis_id)
        return

    r = Redis.from_url(settings.REDIS_URL, decode_responses=True)
    opened = 0
    opened_trade_ids: list[str] = []
    # 未上架受阻收集（2026-10-11）：扫描期不再过滤不可交易币，开单时逐网校验，
    # 未上架的（symbol, 网络名）收集后锁外统一飞书提示（HTTP 不进开仓锁）
    untradable_blocked: list[tuple[str, str]] = []
    for acc in accs:
        trader = BinanceTrader(acc.key)
        if not trader.configured:
            logger.warning("即时开仓跳过账号 %s：缺少币安 API 凭据（analysis=%s）",
                           acc.key, analysis_id)
            continue
        ident = uuid4().hex
        if not _acquire_open_lock(r, ident, wait_s=_OPEN_LOCK_WAIT_S,
                                  ttl_s=_OPEN_LOCK_TTL_S, account=acc.key):
            logger.info("即时开仓等锁超时（%s），放弃本次开仓（analysis=%s）",
                        acc.key, analysis_id)
            _mark_analysis_block(
                analysis_id,
                f"[{acc.key}] 开仓等锁超时 {_OPEN_LOCK_WAIT_S}s（与结算巡检互斥），本次放弃",
            )
            continue
        try:
            try:
                db = SessionLocal()
                try:
                    if try_open_for_analysis(db, trader, analysis_id):
                        opened += 1
                        # 锁内只取成交记录 ID（轻查询）；卡片在全部账号扇出
                        # 结束、锁外统一发送（飞书 HTTP 不进开仓锁）
                        _rid = db.execute(
                            select(TradeRecord.id).where(
                                TradeRecord.ai_analysis_id == UUID(str(analysis_id)),
                                TradeRecord.account_key == acc.key,
                                TradeRecord.status == "OPENED",
                            ).order_by(TradeRecord.opened_at.desc()).limit(1)
                        ).scalar()
                        if _rid:
                            opened_trade_ids.append(str(_rid))
                    else:
                        # 未上架拒开（open_block_reason 刚由本账号写入，会话内
                        # 直读可靠）：收集后锁外飞书。正式网有该合约的账号在上
                        # 述分支正常开单走常规开仓卡片，不在此列
                        from app.services.trade_engine import BLOCK_UNTRADABLE
                        a_row = db.get(AIAnalysis, UUID(str(analysis_id)))
                        if (a_row is not None
                                and BLOCK_UNTRADABLE in (a_row.open_block_reason or "")):
                            untradable_blocked.append(
                                (a_row.symbol, "测试网" if acc.testnet else "正式网"))
                finally:
                    db.close()
            except Exception as e:
                # 闸门之外的意外（交易所 REST 瞬断等）：任务 max_retries=0 失败即止，
                # 落因避免悬案（2026-09-15）；单账号异常不中断其余账号扇出
                logger.exception("即时开仓任务异常 account=%s analysis=%s", acc.key, analysis_id)
                _mark_analysis_block(analysis_id, f"[{acc.key}] 开仓任务异常：{e}")
        finally:
            _release_open_lock(r, ident, account=acc.key)

    # 未上架飞书提示（2026-10-11，锁外）：仅列出受阻的网络；正式网有该合约的
    # 账号已正常开单（常规开仓卡片），互不干扰。失败仅记日志
    if untradable_blocked:
        try:
            from app.services.feishu_bot import send_app_text
            send_app_text(
                "⚠️ 开单受阻：" + "；".join(
                    f"{s}（{net}未上架该合约）" for s, net in untradable_blocked)
                + "，该网络无法开单；有上架的网络已正常走开单流程"
            )
        except Exception:
            logger.exception("未上架飞书通知异常 analysis=%s", analysis_id)

    if opened:
        logger.info("即时开仓完成：%d/%d 账号成交（analysis=%s）", opened, len(accs), analysis_id)
        # 开仓飞书卡片：全部账号锁已释放，锁外逐笔发送（失败仅记日志）
        if opened_trade_ids:
            try:
                from app.services.trade_engine import notify_open_cards
                notify_open_cards(opened_trade_ids)
            except Exception:
                logger.exception("开仓飞书卡片推送异常 analysis=%s", analysis_id)
        try:
            db = SessionLocal()
            try:
                a = db.get(AIAnalysis, UUID(str(analysis_id)))
                if a is not None and a.open_block_reason:
                    a.open_block_reason = None
                    db.commit()
            finally:
                db.close()
        except Exception:
            logger.exception("清除拒开原因残留失败 analysis=%s", analysis_id)
    else:
        logger.info("即时开仓未成交（全部账号闸门未过），放弃（analysis=%s）", analysis_id)


@celery_app.task(name="app.tasks.trade_tasks.run_position_advice", max_retries=0)
def run_position_advice(trade_id: str):
    """AI 持仓诊断子任务（docs/16）：由结算巡检在锁外派发，单笔失败只记日志。

    执行前重查状态：巡检与诊断之间 SL 可能已触发结算为 CLOSED → 跳过；
    LLM 失败不落库，ai_advice_at 保持 NULL，下轮巡检按"未诊断"重试。
    """
    from app.services.position_advisor import diagnose_trade

    db = SessionLocal()
    try:
        rec = db.get(TradeRecord, UUID(str(trade_id)))
        if rec is None or rec.status not in ("OPENED", "TP1_HIT", "TP2_HIT"):
            return  # 已结算/不存在，跳过
        advice = diagnose_trade(db, rec)
        if advice is None:
            return  # 竞态丢弃：LLM 执行期间已平仓结算（position_advisor 记日志）
        logger.info("AI 持仓诊断 %s：%s（%s）", rec.symbol,
                    advice.get("action"), advice.get("reason", "")[:60])
    except Exception as e:
        logger.warning("AI 持仓诊断失败 trade=%s: %s", trade_id, e)
    finally:
        db.close()
