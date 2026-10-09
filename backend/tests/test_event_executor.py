# -*- coding: utf-8 -*-
"""P-09 事件驱动执行——动作层单测（docs/16，2026-10-07）。

锁四件事：
1. react_to_fill：无在跑记录零动作 / 有记录全账号锁获取释放配对 + settle_trades
   被调 / 锁失败放弃不抛 / 3s 去抖（连击只触发一次结算）；
2. refresh_mark_pnl：long/short 方向、TP1/TP2 后剩余仓口径、峰值只增、
   终态跳过、60s 节流；
3. remaining_qty：分批结构推导（raw 档量优先，缺档按状态回退）；
4. liquidation_warning：距离阈值与缺数据弃权。

sqlite 内存库夹具镜像 test_ledger_hook.py；锁/结算/trader 全部桩化，
不开真实 Redis/HTTP。
"""
import sys
import uuid
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import app.services.event_executor as ee  # noqa: E402
import app.services.trade_engine as te  # noqa: E402
from app.models.trade import TradeRecord  # noqa: E402

ACC = "testnet-1"


@pytest.fixture()
def db(monkeypatch):
    engine = create_engine("sqlite:///:memory:")
    from app.database import Base

    Base.metadata.create_all(bind=engine, tables=[TradeRecord.__table__])
    monkeypatch.setattr(ee, "SessionLocal", lambda: Session(bind=engine))
    ee.reset_cooldowns()
    return Session(bind=engine)


def _rec(**kw):
    """标准在跑多单：entry 100 / qty 1 / risk 2，TP1 后余 0.25"""
    base = dict(
        id=uuid.uuid4(), symbol="BTCUSDT", direction="long", status="OPENED",
        testnet=True, account_key=ACC,
        entry_price=100.0, qty=1.0, risk_amount=2.0,
        stop_loss=98.0, tp1=101.5, tp2=0.0,
        realized_pnl=0.0, raw={"qty_tp1": 0.75, "qty_tp2": 0.0},
        opened_at=datetime(2026, 10, 7, 8, 0, 0),
    )
    base.update(kw)
    return TradeRecord(**base)


class _Recorder:
    def __init__(self, result=True):
        self.calls = []
        self.result = result

    def __call__(self, *a, **kw):
        self.calls.append((a, kw))
        return self.result


@pytest.fixture()
def locks(monkeypatch):
    """锁桩：acquire 记录并成功，release 记录；返回两个 recorder。
    同时桩化 BinanceTrader（react_to_fill 凭据守卫检查 configured，
    测试环境无 API key 会跳过结算——2026-10-07 后加入的守卫需替身）"""
    acq, rel = _Recorder(result=True), _Recorder()
    monkeypatch.setattr(ee, "acquire_open_lock",
                        lambda r, ident, wait_s, ttl_s, account=None: acq(
                            r, ident, wait_s, ttl_s, account=account))
    monkeypatch.setattr(ee, "release_open_lock",
                        lambda r, ident, account=None: rel(r, ident, account=account))
    monkeypatch.setattr(ee, "_all_lock_keys", lambda db: [ACC])
    monkeypatch.setattr(ee, "BinanceTrader",
                        lambda key: SimpleNamespace(configured=True))
    return acq, rel


@pytest.fixture()
def settle(monkeypatch):
    rec = _Recorder(result=(1, []))
    monkeypatch.setattr(te, "settle_trades", rec)
    return rec


def test_react_to_fill_no_running_record(db, locks, settle):
    """无在跑记录：零动作（锁/结算都不碰），事件零成本返回"""
    db.add(_rec(status="CLOSED"))
    db.commit()
    assert ee.react_to_fill(ACC, "BTCUSDT") is False
    assert settle.calls == [] and locks[0].calls == []


def test_react_to_fill_triggers_settle(db, locks, settle):
    """有在跑记录：全账号锁获取释放配对 + settle_trades 恰好一次"""
    db.add(_rec())
    db.commit()
    assert ee.react_to_fill(ACC, "BTCUSDT") is True
    assert len(settle.calls) == 1
    acq, rel = locks
    assert len(acq.calls) == 1 and len(rel.calls) == 1
    # 结算实例为该账号 trader（settle 桩首参由 react_to_fill 内部构造，
    # 这里只断言锁账号维度）
    assert acq.calls[0][1].get("account") == ACC
    assert rel.calls[0][1].get("account") == ACC


def test_react_to_fill_lock_busy_gives_up(db, locks, settle):
    """锁被巡检占用：放弃（False），不结算、已获取的锁全释放——轮询兜底"""
    db.add(_rec())
    db.commit()
    acq, rel = locks
    acq.result = False  # 第一个账号就拿不到
    assert ee.react_to_fill(ACC, "BTCUSDT") is False
    assert settle.calls == []
    assert len(acq.calls) == 1
    assert rel.calls == []  # 一个都没拿到，无需释放


def test_react_to_fill_debounce(db, locks, settle):
    """3s 去抖：TP 分次成交连击只触发一次结算"""
    db.add(_rec())
    db.commit()
    assert ee.react_to_fill(ACC, "BTCUSDT") is True
    assert ee.react_to_fill(ACC, "BTCUSDT") is False  # 冷却窗口内
    assert len(settle.calls) == 1


def test_react_to_fill_trader_unconfigured(db, locks, settle, monkeypatch):
    """账号凭据缺失：不结算不抛（轮询兜底）"""
    db.add(_rec())
    db.commit()
    monkeypatch.setattr(ee, "BinanceTrader",
                        lambda key: SimpleNamespace(configured=False))
    assert ee.react_to_fill(ACC, "BTCUSDT") is False
    assert settle.calls == []


# ── refresh_mark_pnl ─────────────────────────────────────────────


def test_mark_pnl_long_opened(db):
    """OPENED 多单：uPnL=(mark−entry)×全量，峰值与 pnl_pct 同步"""
    db.add(_rec(raw={}))
    db.commit()
    assert ee.refresh_mark_pnl(ACC, "BTCUSDT", 101.0) is True
    r = db.query(TradeRecord).one()
    assert float(r.unrealized_pnl) == pytest.approx(1.0)
    assert float(r.peak_total_pnl) == pytest.approx(1.0)
    assert float(r.pnl_pct) == pytest.approx(50.0)  # 1.0 / risk 2.0


def test_mark_pnl_short_and_remaining(db):
    """空单方向取反；TP1 后按剩余仓 0.25 计"""
    db.add(_rec(direction="short", status="TP1_HIT",
                raw={"qty_tp1": 0.75, "qty_tp2": 0.0}))
    db.commit()
    assert ee.refresh_mark_pnl(ACC, "BTCUSDT", 99.0) is True  # 空单跌=赚
    r = db.query(TradeRecord).one()
    assert float(r.unrealized_pnl) == pytest.approx(0.25)  # (100−99)×0.25


def test_mark_pnl_peak_monotonic(db):
    """峰值只增：先 101 后回撤 99，peak 保持 1.0"""
    db.add(_rec(raw={}))
    db.commit()
    ee.refresh_mark_pnl(ACC, "BTCUSDT", 101.0)
    ee.reset_cooldowns()
    ee.refresh_mark_pnl(ACC, "BTCUSDT", 99.0)
    r = db.query(TradeRecord).one()
    assert float(r.peak_total_pnl) == pytest.approx(1.0)
    assert float(r.unrealized_pnl) == pytest.approx(-1.0)


def test_mark_pnl_closed_skipped_and_throttle(db):
    """终态跳过；同币 60s 节流内第二次不落库"""
    db.add(_rec(status="CLOSED"))
    db.commit()
    assert ee.refresh_mark_pnl(ACC, "BTCUSDT", 101.0) is False
    assert ee.refresh_mark_pnl(ACC, "BTCUSDT", 102.0) is False  # 冷却（首刷已消耗）


def test_mark_pnl_bad_price(db):
    db.add(_rec(raw={}))
    db.commit()
    assert ee.refresh_mark_pnl(ACC, "BTCUSDT", 0.0) is False
    assert ee.refresh_mark_pnl(ACC, "BTCUSDT", -1.0) is False


# ── N-17：mark 流诊断派发（2026-10-08）──────────────────────────


@pytest.fixture()
def advice_env(monkeypatch):
    """AI key + 派发桩：返回 delay recorder（不真发 celery）"""
    from app.config import settings
    monkeypatch.setattr(settings, "AI_API_KEY", "test-key")
    dispatched = _Recorder()
    import app.tasks.trade_tasks as tt
    monkeypatch.setattr(tt.run_position_advice, "delay", dispatched)
    return dispatched


def test_mark_pnl_dispatches_advice(db, advice_env):
    """首次浮亏（advice_due 命中）→ 派发诊断子任务；快照照常落库"""
    db.add(_rec(raw={}))
    db.commit()
    assert ee.refresh_mark_pnl(ACC, "BTCUSDT", 95.0) is True  # -5 → 首次浮亏
    assert len(advice_env.calls) == 1
    r = db.query(TradeRecord).one()
    assert float(r.unrealized_pnl) == pytest.approx(-5.0)  # 派发不影响快照


def test_mark_pnl_advice_dispatch_cooldown(db, advice_env):
    """N-17 检测修复：LLM 失败不落库 → advice_due 持续为真，无冷却时 60s 采样
    重试风暴 60 次/h——同单派发 10 分钟冷却封顶 6 次/h"""
    db.add(_rec(raw={}))
    db.commit()
    assert ee.refresh_mark_pnl(ACC, "BTCUSDT", 95.0) is True
    assert len(advice_env.calls) == 1
    # 精确移除 mark 节流键（模拟 60s 后下一波推送；不可 reset_cooldowns——
    # 会把 advice:{id} 派发冷却一并清掉）
    ee._cooldowns.pop(f"mark:{ACC}:BTCUSDT", None)
    assert ee.refresh_mark_pnl(ACC, "BTCUSDT", 94.0) is True
    assert len(advice_env.calls) == 1  # advice 冷却内：不重派


def test_mark_pnl_no_dispatch_within_cooldown(db, advice_env):
    """冷却内 + 恶化不足 → 快照刷新但不派发"""
    db.add(_rec(ai_advice_at=datetime.utcnow(),
                ai_advice_pnl=-5.0, raw={}))
    db.commit()
    assert ee.refresh_mark_pnl(ACC, "BTCUSDT", 99.0) is True  # 恶化 1 < 3
    assert advice_env.calls == []


def test_mark_pnl_no_dispatch_without_ai_key(db, monkeypatch):
    """AI 未配置 → 不派发（advice_due 恒 False 的双保险前置）"""
    from app.config import settings
    monkeypatch.setattr(settings, "AI_API_KEY", "")
    dispatched = _Recorder()
    import app.tasks.trade_tasks as tt
    monkeypatch.setattr(tt.run_position_advice, "delay", dispatched)
    db.add(_rec(raw={}))
    db.commit()
    assert ee.refresh_mark_pnl(ACC, "BTCUSDT", 95.0) is True
    assert dispatched.calls == []


# ── 双网分析所有权（2026-10-08）：测试网孪生跟随，不独立派发 ──────


def test_mark_pnl_twin_suppressed_but_snapshot_kept(db, advice_env):
    """测试网单有在跑正式网孪生 → 快照/峰值照常刷新，但不独立派发诊断
    （建议由正式网诊断镜像同步）"""
    aid = uuid.uuid4()
    db.add(_rec(ai_analysis_id=aid, raw={}))
    db.add(_rec(testnet=False, account_key="live-1", ai_analysis_id=aid))
    db.commit()
    assert ee.refresh_mark_pnl(ACC, "BTCUSDT", 95.0) is True  # -5 → 首次浮亏
    r = db.query(TradeRecord).filter_by(account_key=ACC).one()
    assert float(r.unrealized_pnl) == pytest.approx(-5.0)  # 快照不受影响
    assert advice_env.calls == []  # 孪生在跑：不派发


def test_mark_pnl_twin_closed_dispatches(db, advice_env):
    """正式网孪生已平 → 测试网恢复自主诊断派发"""
    aid = uuid.uuid4()
    db.add(_rec(ai_analysis_id=aid, raw={}))
    db.add(_rec(testnet=False, account_key="live-1",
                ai_analysis_id=aid, status="CLOSED"))
    db.commit()
    assert ee.refresh_mark_pnl(ACC, "BTCUSDT", 95.0) is True
    assert len(advice_env.calls) == 1


def test_mark_pnl_live_always_dispatches(db, advice_env):
    """正式网单永远正常派发（Rule A 只抑制测试网侧）"""
    db.add(_rec(testnet=False, account_key="live-1", ai_analysis_id=uuid.uuid4(),
                raw={}))
    db.commit()
    assert ee.refresh_mark_pnl("live-1", "BTCUSDT", 95.0) is True
    assert len(advice_env.calls) == 1


def test_mark_pnl_dispatch_failure_isolated(db, advice_env, monkeypatch):
    """派发异常只记日志：快照照常落库、返回 True（事件流线程不中断）"""
    import app.tasks.trade_tasks as tt

    def _boom(*a, **kw):
        raise RuntimeError("broker down")
    monkeypatch.setattr(tt.run_position_advice, "delay", _boom)
    db.add(_rec(raw={}))
    db.commit()
    assert ee.refresh_mark_pnl(ACC, "BTCUSDT", 95.0) is True
    r = db.query(TradeRecord).one()
    assert float(r.unrealized_pnl) == pytest.approx(-5.0)


# ── remaining_qty / liquidation_warning ──────────────────────────


def test_remaining_qty_fallbacks():
    """raw 缺档按状态回退：TP2_HIT→25%，无 TP2 的 TP1_HIT→25%"""
    assert ee.remaining_qty(_rec(raw={})) == pytest.approx(1.0)
    assert ee.remaining_qty(_rec(status="TP2_HIT", raw={})) == pytest.approx(0.25)
    assert ee.remaining_qty(_rec(status="TP1_HIT", raw={})) == pytest.approx(0.25)
    assert ee.remaining_qty(_rec(status="TP1_HIT", tp2=103.0, raw={})) == pytest.approx(0.5)


def test_remaining_qty_planned_tranches():
    """50/25/25 计划档（raw 有占位值）：减档与状态对齐——OPENED 不减、
    TP1_HIT 只减 q1、TP2_HIT 减 q1+q2（2026-10-07 回归：初版把未成交
    计划档一并减掉，uPnL/peak 系统性低估）"""
    raw = {"qty_tp1": 0.5, "qty_tp2": 0.25}
    assert ee.remaining_qty(_rec(raw=raw)) == pytest.approx(1.0)
    assert ee.remaining_qty(_rec(status="TP1_HIT", raw=raw)) == pytest.approx(0.5)
    assert ee.remaining_qty(_rec(status="TP2_HIT", raw=raw)) == pytest.approx(0.25)
    # 75/25 路径：TP1_HIT 后 q2=0 占位不影响
    raw75 = {"qty_tp1": 0.75, "qty_tp2": 0.0}
    assert ee.remaining_qty(_rec(status="TP1_HIT", raw=raw75)) == pytest.approx(0.25)


def test_liquidation_warning():
    """mark 距强平价 <10% 告警；缺 liq 快照/距离足够 → 弃权"""
    r = _rec(raw={"liq_price": 100.0})
    assert ee.liquidation_warning(r, 95.0)  # 5% → 告警
    assert ee.liquidation_warning(r, 50.0) is None  # 50% → 距离足够
    assert ee.liquidation_warning(_rec(raw={}), 95.0) is None  # 无快照弃权
