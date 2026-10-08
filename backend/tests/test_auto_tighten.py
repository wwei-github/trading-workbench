# -*- coding: utf-8 -*-
"""N-17 自动收紧止损（2026-10-08）——批次②单测。

锁四件事：
1. tighten_candidate：口径（近 3 根已收盘低点 − 0.25ATR）与三重守卫
   （改善 ≥0.1R / 距现价 ≥0.3ATR / 方向正确）；
2. _auto_tighten 守卫链：终态 / 幂等 / 凭据缺失 / 噪音距离 / 改善不足 /
   锁占用全数放弃；成功路径撤旧挂新落 SL_MOVE 事件 + 结果卡片；
   挂单失败不落库 + 失败卡片；
3. diagnose_trade 通知路由：tighten 执行后不推建议卡（结果卡片由
   _auto_tighten 负责）/ 无候选不推 / exit 仍推带平仓按钮。
4. _notify_advice 泛化：reduce 卡无按钮。

sqlite 内存库夹具镜像 test_event_executor.py；trader/锁/飞书全桩化。
"""
import json
import sys
import uuid
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import app.services.position_advisor as pa  # noqa: E402
from app.models.trade import TradeEvent, TradeRecord  # noqa: E402

ACC = "testnet-1"


@pytest.fixture()
def db(monkeypatch):
    engine = create_engine("sqlite:///:memory:")
    from app.database import Base

    Base.metadata.create_all(bind=engine, tables=[TradeRecord.__table__, TradeEvent.__table__])
    return Session(bind=engine)


def _rec(db, **kw):
    """在跑多单：entry 100 / SL 98 / risk 2 → 1R = 2U"""
    base = dict(
        id=uuid.uuid4(), symbol="BTCUSDT", direction="long", status="OPENED",
        testnet=True, account_key=ACC,
        entry_price=100.0, qty=1.0, risk_amount=2.0,
        stop_loss=98.0, tp1=101.5, tp2=0.0,
        realized_pnl=0.0, unrealized_pnl=0.0, raw={},
        opened_at=datetime.utcnow(),
    )
    base.update(kw)
    rec = TradeRecord(**base)
    db.add(rec)
    db.commit()
    return rec


def _facts(lows=(99.0, 98.8, 98.6), price=100.0, atr=1.0, highs=None):
    """标准事实包：多单近 3 根低点 99/98.8/98.6 → 候选 = 98.6−0.25 = 98.35"""
    kl = [[i, 100.0, (highs or (101.0, 101.0, 101.0))[i], lows[i], 100.0, 1.0]
          for i in range(3)]
    return {
        "position_now": {"current_price": price},
        "kline_facts": {"atr_abs": atr},
        "klines_recent": kl + [[3, 100.0, 101.0, 99.5, 100.0, 1.0]],  # 含未收盘根
    }


# ── tighten_candidate 纯函数 ─────────────────────────────────────


def test_candidate_long_normal(db):
    """多单：min(低点)−0.25ATR=98.35，守卫全过 → 候选价"""
    rec = _rec(db)
    assert pa.tighten_candidate(_facts(), rec) == pytest.approx(98.35)


def test_candidate_short_symmetric(db):
    """空单：max(高点)+0.25ATR，对称口径"""
    rec = _rec(db, direction="short", entry_price=100.0, stop_loss=102.0)
    f = _facts(price=100.0, highs=(100.6, 100.4, 100.2))
    # 高点均值下移形态：cand = 100.6 + 0.25 = 100.85；改善 (102−100.85)×1=1.15U ≥0.2U；
    # 距现价 |100.85−100|=0.85 ≥0.3 → 通过
    assert pa.tighten_candidate(f, rec) == pytest.approx(100.85)


def test_candidate_insufficient_improvement(db):
    """改善 <0.1R（0.2U）：候选仅优于旧 SL 0.1U → 否决"""
    rec = _rec(db, stop_loss=98.3)  # 候选 98.35 改善 0.05U < 0.2U
    assert pa.tighten_candidate(_facts(), rec) is None


def test_candidate_too_close_to_price(db):
    """距现价 <0.3ATR：噪音区否决（JST 案例的贴价情形）"""
    rec = _rec(db)
    # min(低点)=99.96 → 候选 99.71，距现价 100 仅 0.29 < 0.3 → 否决
    f = _facts(lows=(100.0, 99.96, 99.98))
    assert pa.tighten_candidate(f, rec) is None


def test_candidate_worse_than_old_sl(db):
    """候选劣于旧止损（低点跌破旧 SL）：方向正确但改善为负 → 否决"""
    rec = _rec(db, stop_loss=99.5)
    assert pa.tighten_candidate(_facts(), rec) is None


def test_candidate_missing_inputs(db):
    """ATR 缺失 / K 线不足 / risk 缺失 → None"""
    rec = _rec(db)
    f = _facts()
    f["kline_facts"]["atr_abs"] = None
    assert pa.tighten_candidate(f, rec) is None
    assert pa.tighten_candidate(_facts(price=0), rec) is None
    rec2 = _rec(db, risk_amount=0)
    assert pa.tighten_candidate(_facts(), rec2) is None


# ── _auto_tighten 守卫链与执行 ────────────────────────────────────


class _Trader:
    """trader 桩：configured/round_price/price/open_algo_orders/cancel_order/stop_market_close"""

    def __init__(self, price=100.0, hang_fail=False):
        self.configured = True
        self.price_value = price
        self.hang_fail = hang_fail
        self.calls = []

    def round_price(self, symbol, p):
        return round(p, 2)

    def price(self, symbol):
        return self.price_value

    def open_algo_orders(self, symbol):
        # 对齐币安 /fapi/v1/openAlgoOrders 真实结构：dict 列表（含 algoId）。
        # 曾stub成 int 列表掩盖了 set(dict) unhashable 线上 bug（2026-10-08 LUNA2）
        return [{"algoId": 111, "type": "STOP_MARKET"},
                {"algoId": 222, "type": "TAKE_PRO_MARKET"}]

    def cancel_order(self, symbol, oid):
        self.calls.append(("cancel", oid))

    def stop_market_close(self, symbol, side, stop_price):
        if self.hang_fail:
            raise RuntimeError("algo hang failed")
        self.calls.append(("hang", side, stop_price))
        return {"algoId": 999}


@pytest.fixture()
def env(monkeypatch):
    """锁桩 + trader 桩 + 卡片桩；返回 (trader, locks_called, cards)"""
    from app.services import trade_locks as tl
    import app.services.binance_trader as bt

    trader = _Trader()
    # _auto_tighten 懒导入 binance_trader.BinanceTrader → 桩模块属性
    monkeypatch.setattr(bt, "BinanceTrader", lambda key: trader)
    acq, rel = [], []
    monkeypatch.setattr(tl, "acquire_open_lock",
                        lambda r, ident, wait_s, ttl_s, account=None: acq.append(account) or True)
    monkeypatch.setattr(tl, "release_open_lock",
                        lambda r, ident, account=None: rel.append(account))
    cards = []
    import app.services.feishu_bot as fb
    monkeypatch.setattr(fb, "configured", lambda: True)
    monkeypatch.setattr(fb, "send_app_card", lambda card: cards.append(card))
    return SimpleNamespace(trader=trader, acq=acq, rel=rel, cards=cards)


_ADVICE = {"action": "tighten", "thesis_valid": True, "trend_broken": False,
           "reason": "区间顶承压回落"}


def test_auto_tighten_success(db, env, monkeypatch):
    """成功路径：挂新 SL（raw 无旧单号无需撤）+ SL_MOVE 事件落库 + orange 结果卡片"""
    rec = _rec(db)
    ok = pa._auto_tighten(db, rec, 98.35, atr=1.0, advice=_ADVICE)
    assert ok is True
    assert env.trader.calls == [("hang", "SELL", 98.35)]
    db.refresh(rec)
    assert float(rec.stop_loss) == 98.35
    ev = db.query(TradeEvent).filter_by(event_type="SL_MOVE").one()
    assert ev.detail["reason"] == "ai_auto_tighten" and ev.detail["to"] == 98.35
    assert len(env.cards) == 1
    assert "已自动收紧" in env.cards[0]["header"]["title"]["content"]
    assert env.acq == [ACC] and env.rel == [ACC]


def test_auto_tighten_cancels_old_sl_order(db, env, monkeypatch):
    """真实在跑单 raw.sl_order_id 存在：先撤旧 SL algo 单再挂新（_replace_sl 链）"""
    rec = _rec(db, raw={"sl_order_id": 111})
    assert pa._auto_tighten(db, rec, 98.35, 1.0, _ADVICE) is True
    assert env.trader.calls == [("cancel", 111), ("hang", "SELL", 98.35)]
    db.refresh(rec)
    assert rec.raw["sl_order_id"] == 999  # 新单号回填


def test_auto_tighten_card_loss_sign(db, env, monkeypatch):
    """N-17 检测修复：扫损金额以负号显示（损失方向），改善 R 为正"""
    rec = _rec(db)
    pa._auto_tighten(db, rec, 98.35, 1.0, _ADVICE)
    body = env.cards[0]["elements"][0]["text"]["content"]
    # entry 100 / qty 1 / risk 2：旧 SL 98 → 旧扫损 -2.00U（-1.00R）；
    # 新 SL 98.35 → 新扫损 -1.65U（-0.83R）；改善 0.35U = +0.17R
    assert "-1.65U" in body and "-0.83R" in body
    assert "-2.00U" in body and "-1.00R" in body
    assert "改善 +0.17R" in body


def test_auto_tighten_skips_closed(db, env):
    """终态单：放弃（守卫①）"""
    rec = _rec(db, status="CLOSED")
    assert pa._auto_tighten(db, rec, 98.35, 1.0, _ADVICE) is False
    assert env.acq == []


def test_auto_tighten_idempotent(db, env):
    """SL 已等于候选：幂等跳过（守卫②）"""
    rec = _rec(db, stop_loss=98.35)
    assert pa._auto_tighten(db, rec, 98.35, 1.0, _ADVICE) is False
    assert env.acq == []


def test_auto_tighten_unconfigured(db, env, monkeypatch):
    """账号凭据缺失：跳过（守卫③）"""
    import app.services.binance_trader as bt
    monkeypatch.setattr(bt, "BinanceTrader", lambda key: SimpleNamespace(configured=False))
    rec = _rec(db)
    assert pa._auto_tighten(db, rec, 98.35, 1.0, _ADVICE) is False


def test_auto_tighten_noise_distance(db, env):
    """实时价复验：新 SL 距现价 <0.3ATR → 放弃（守卫④，JST 贴价情形）"""
    rec = _rec(db)
    env.trader.price_value = 98.5  # 候选 98.35 距现价仅 0.15 < 0.3
    assert pa._auto_tighten(db, rec, 98.35, 1.0, _ADVICE) is False
    assert env.acq == []


def test_auto_tighten_insufficient_improve(db, env):
    """改善 <0.1R：放弃（守卫⑤，round_price 后复验）"""
    rec = _rec(db, stop_loss=98.3)
    assert pa._auto_tighten(db, rec, 98.35, 1.0, _ADVICE) is False
    assert env.acq == []


def test_auto_tighten_lock_busy(db, env, monkeypatch):
    """开仓锁被占用：放弃不执行（守卫⑥）"""
    from app.services import trade_locks as tl
    monkeypatch.setattr(tl, "acquire_open_lock", lambda *a, **kw: False)
    rec = _rec(db)
    assert pa._auto_tighten(db, rec, 98.35, 1.0, _ADVICE) is False
    assert env.trader.calls == []


def test_auto_tighten_hang_failure_no_db_write(db, env, monkeypatch):
    """挂新单失败：不落库（SL 保持旧价）+ 红色失败卡片（巡检兜底重挂）"""
    rec = _rec(db)
    env.trader.hang_fail = True
    ok = pa._auto_tighten(db, rec, 98.35, 1.0, _ADVICE)
    assert ok is False
    db.refresh(rec)
    assert float(rec.stop_loss) == 98.0  # 旧价未动
    assert db.query(TradeEvent).filter_by(event_type="SL_MOVE").count() == 0
    assert len(env.cards) == 1
    assert "失败" in env.cards[0]["header"]["title"]["content"]
    assert env.rel == [ACC]  # 锁正常释放


# ── diagnose_trade 通知路由（C1）─────────────────────────────────


def _route_env(monkeypatch, llm_action, cand):
    """diagnose_trade 全桩：LLM 返回固定 action，候选价固定"""
    content = json.dumps({
        "action": llm_action, "thesis_valid": True, "trend_broken": False,
        "reason": "r", "detail": "d",
    })
    fake_client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(
        create=lambda **kw: SimpleNamespace(choices=[SimpleNamespace(
            message=SimpleNamespace(content=content))]))))
    monkeypatch.setattr(pa, "get_client", lambda: fake_client)
    monkeypatch.setattr(pa, "build_facts", lambda db, rec: _facts())
    monkeypatch.setattr(pa, "tighten_candidate", lambda facts, rec: cand)
    pushed = {"advice": 0, "stale": 0, "tighten": 0}
    monkeypatch.setattr(pa, "_notify_advice", lambda *a: pushed.__setitem__("advice", pushed["advice"] + 1))
    monkeypatch.setattr(pa, "_notify_stale", lambda *a: pushed.__setitem__("stale", pushed["stale"] + 1))
    monkeypatch.setattr(pa, "_auto_tighten",
                        lambda *a: pushed.__setitem__("tighten", pushed["tighten"] + 1) or True)
    from app.config import settings
    monkeypatch.setattr(settings, "AI_API_KEY", "test")
    return pushed


def test_route_tighten_executed_no_advice_card(db, monkeypatch):
    """tighten 且执行成功：只走 _auto_tighten（结果卡片），不推建议卡"""
    rec = _rec(db)
    pushed = _route_env(monkeypatch, "tighten", cand=98.35)
    advice = pa.diagnose_trade(db, rec)
    assert advice["tighten_candidate"] == 98.35
    assert pushed == {"advice": 0, "stale": 0, "tighten": 1}


def test_route_tighten_no_candidate_silent(db, monkeypatch):
    """tighten 但规则否决（候选 None）：只落库不推送（噪音控制）"""
    rec = _rec(db)
    pushed = _route_env(monkeypatch, "tighten", cand=None)
    advice = pa.diagnose_trade(db, rec)
    assert advice["tighten_candidate"] is None
    assert pushed == {"advice": 0, "stale": 0, "tighten": 0}


def test_route_exit_pushes_advice_card(db, monkeypatch):
    """exit：仍推人工建议卡（自动平仓红线不动），tighten 路径零触发"""
    rec = _rec(db)
    pushed = _route_env(monkeypatch, "exit", cand=None)
    pa.diagnose_trade(db, rec)
    assert pushed == {"advice": 1, "stale": 0, "tighten": 0}


def test_notify_advice_exit_has_button(db, monkeypatch):
    """真实 _notify_advice：exit 卡带市价平仓按钮（人工红线动作）"""
    rec = _rec(db)
    cards = []
    import app.services.feishu_bot as fb
    monkeypatch.setattr(fb, "configured", lambda: True)
    monkeypatch.setattr(fb, "send_app_card", lambda c: cards.append(c))
    from app.config import settings
    monkeypatch.setattr(settings, "FEISHU_ACTION_TOKEN", "tok")
    pa._notify_advice(rec, {"action": "exit", "thesis_valid": False,
                            "trend_broken": True, "reason": "r"}, -3.2)
    btns = [e for e in cards[0]["elements"] if e.get("tag") == "action"]
    assert len(btns) == 1 and btns[0]["actions"][0]["value"]["act"] == "close_market"


def test_notify_advice_reduce_no_button(db, monkeypatch):
    """真实 _notify_advice：reduce 卡无按钮（数量拆分留人工）"""
    rec = _rec(db)
    cards = []
    import app.services.feishu_bot as fb
    monkeypatch.setattr(fb, "configured", lambda: True)
    monkeypatch.setattr(fb, "send_app_card", lambda c: cards.append(c))
    from app.config import settings
    monkeypatch.setattr(settings, "FEISHU_ACTION_TOKEN", "tok")
    pa._notify_advice(rec, {"action": "reduce", "thesis_valid": True,
                            "trend_broken": False, "reason": "r"}, -1.0)
    assert "建议减仓" in cards[0]["header"]["title"]["content"]
    assert [e for e in cards[0]["elements"] if e.get("tag") == "action"] == []
