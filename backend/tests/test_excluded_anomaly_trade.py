# -*- coding: utf-8 -*-
"""异常单（trade_records.excluded）统计口径剔除（2026-10-18）

测试网模拟撮合曾产出伪巨亏单（HYPEUSDT 一笔 -2330.51），人工标记为异常单后
不得进入任何统计口径：资金分析汇总（笔数/胜率/净盈亏/平均盈亏比）、复盘统计、
熔断闸门、飞书日报。列表接口保留该行（要能标记/取消），故此处只测聚合口径。

sqlite 内存库，不触网。
"""
import sys
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import app.models  # noqa: E402,F401
from app.api.trades import (  # noqa: E402
    _sum_unrealized, account_summary, fund_analytics, set_trade_excluded,
)
from app.database import Base  # noqa: E402
from app.models.system_config import SystemConfig  # noqa: E402
from app.models.trade import TradeRecord  # noqa: E402


@pytest.fixture()
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    session.add(SystemConfig(id=1, kline_interval="1h"))
    session.commit()
    yield session
    session.close()


def _rec(pnl, risk=100.0, excluded=False):
    """已平仓测试网单（R = realized_pnl / risk_amount）"""
    now = datetime.utcnow()
    return TradeRecord(
        symbol="HYPEUSDT", direction="long", status="CLOSED",
        exit_reason="sl", testnet=True, account_key="testnet-1",
        opened_at=now - timedelta(hours=3), closed_at=now - timedelta(hours=1),
        realized_pnl=pnl, risk_amount=risk, excluded=excluded,
    )


def _summary(db):
    return fund_analytics(scope="all", start=None, end=None, db=db).summary


def test_summary_excludes_anomaly_trade(db):
    """资金分析汇总：excluded 单不计入笔数/胜率/净盈亏/平均盈亏比"""
    # 正常单 R=+0.10；伪巨亏单 R=-2.33（若不剔除会把平均 R 拉到 -1.12）
    db.add_all([_rec(10.0), _rec(-2330.51, risk=1000.0, excluded=True)])
    db.commit()
    s = _summary(db)
    assert s.closed == 1
    assert s.wins == 1
    assert s.net_pnl == pytest.approx(10.0)
    assert s.win_rate == pytest.approx(100.0)
    assert s.avg_r == pytest.approx(0.10)


def test_unmarked_anomaly_trade_still_counted(db):
    """对照组：未标记（excluded=False）时照旧计入——确认上面的用例非空转"""
    db.add_all([_rec(10.0), _rec(-2330.51, risk=1000.0)])
    db.commit()
    s = _summary(db)
    assert s.closed == 2
    assert s.wins == 1
    assert s.net_pnl == pytest.approx(-2320.51)
    assert s.avg_r == pytest.approx(-1.12)  # (0.10 - 2.33051) / 2


def test_cancel_mark_restores_counting(db):
    """取消标记（页面「取消异常」）后该单重新计入口径"""
    fake = _rec(-2330.51, risk=1000.0, excluded=True)
    db.add_all([_rec(10.0), fake])
    db.commit()
    assert _summary(db).closed == 1

    fake.excluded = False
    db.commit()
    assert _summary(db).closed == 2


def test_sum_unrealized_excludes_marked_open_symbol():
    """账户概览浮动盈亏：在跑异常单币种剔除（2026-10-22 扩展到未平仓单）。

    测试网 BTCDOMUSDT 异常持仓 +528.27 不得计入保证金余额/未实现盈亏/今日盈亏；
    同账号其余持仓照常合计。空集合=对照组，确认过滤非空转。
    """
    positions = {
        "BTCDOMUSDT": {"unrealized_pnl": 528.27},
        "BTCUSDT": {"unrealized_pnl": -3.0},
        "ETHUSDT": {"unrealized_pnl": None},
    }
    assert _sum_unrealized(positions, {"BTCDOMUSDT"}) == pytest.approx(-3.0)
    assert _sum_unrealized(positions, set()) == pytest.approx(525.27)


def _open_rec(symbol: str, excluded: bool, testnet: bool = True,
              account_key: str = "testnet-1") -> TradeRecord:
    return TradeRecord(
        symbol=symbol, direction="long", status="OPENED",
        testnet=testnet, account_key=account_key,
        opened_at=datetime.utcnow(), risk_amount=100.0, excluded=excluded,
    )


def _fake_trader(monkeypatch):
    """隔离交易所与账号注册表：钱包 4073.21，BTCDOM +528.27 / BTC -3.0 两个在跑持仓"""
    import app.services.binance_trader as bt
    import app.services.account_registry as ar

    class FakeTrader:
        def __init__(self, account_key=None):
            self.account_key = account_key
            self.testnet = True

        def wallet_balance(self):
            return {"wallet": 4073.21, "available": 3000.0}

        def positions(self):
            return {
                "BTCDOMUSDT": {"unrealized_pnl": 528.27},
                "BTCUSDT": {"unrealized_pnl": -3.0},
            }

    monkeypatch.setattr(bt, "BinanceTrader", FakeTrader)
    monkeypatch.setattr(
        ar, "get_account",
        lambda key: SimpleNamespace(key=key, testnet=True, name=key),
    )


def test_account_summary_excludes_marked_open_symbol(db, monkeypatch):
    """账户概览集成口径：标记异常的在跑单不计入未实现/保证金余额/今日盈亏。

    现场数据：保证金 4601.49 = 钱包 4073.21 + BTCDOM 528.27。标记后应回落为
    4070.21（仅剩 BTC -3.0），钱包余额保持交易所现金原值。
    """
    _fake_trader(monkeypatch)
    db.add_all([_open_rec("BTCDOMUSDT", excluded=True),
                _open_rec("BTCUSDT", excluded=False)])
    db.commit()

    out = account_summary(account_key="testnet-1", db=db)
    assert out["available"] is True
    assert out["wallet"] == pytest.approx(4073.21)
    assert out["unrealized"] == pytest.approx(-3.0)
    assert out["margin_balance"] == pytest.approx(4070.21)


def test_account_summary_control_not_marked(db, monkeypatch):
    """对照组：未标记时 BTCDOM 浮动照旧计入（确认上面的用例非空转）"""
    _fake_trader(monkeypatch)
    db.add(_open_rec("BTCDOMUSDT", excluded=False))
    db.commit()

    out = account_summary(account_key="testnet-1", db=db)
    assert out["unrealized"] == pytest.approx(525.27)
    assert out["margin_balance"] == pytest.approx(4598.48)


def test_account_summary_exclusion_is_per_account(db, monkeypatch):
    """账号隔离：testnet-2 标记的异常单不影响 testnet-1 概览"""
    _fake_trader(monkeypatch)
    db.add(_open_rec("BTCDOMUSDT", excluded=True, account_key="testnet-2"))
    db.commit()

    out = account_summary(account_key="testnet-1", db=db)
    assert out["unrealized"] == pytest.approx(525.27)


def test_exclude_endpoint_rejects_live_trade(db):
    """异常单标记仅限测试网（UI 亦只对 testnet 行展示）：正式网单必须拒绝"""
    live = _open_rec("BTCUSDT", excluded=False, testnet=False,
                     account_key="live-1")
    db.add(live)
    db.commit()
    with pytest.raises(Exception) as ei:
        set_trade_excluded(trade_id=str(live.id), excluded=True, db=db)
    assert getattr(ei.value, "status_code", None) == 400
    db.refresh(live)
    assert live.excluded is False
