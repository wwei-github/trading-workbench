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

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import app.models  # noqa: E402,F401
from app.api.trades import _sum_unrealized, fund_analytics  # noqa: E402
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
