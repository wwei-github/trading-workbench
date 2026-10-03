# -*- coding: utf-8 -*-
"""同源镜像单归因去重（review_tasks.run_ai_attribution，2026-10-03）。

双网自动交易拆分后，同一 AI 分析（ai_analysis_id）会在测试网/正式网各落
一单——行情同源、结论同源，复盘（AI 错误归因）只做一次：
- 组内正式网优先（实盘归因更有价值），其余镜像单跳过（不调 LLM），
  打标 attributed_at + error_lesson 说明
- 跨轮场景：兄弟单已归因（attributed_at 非空）→ 后到的镜像单跳过
- 无 analysis 关联的（导入持仓/手动单）不受影响

sqlite 内存库 + attribute_trade 替身，不触网不调 LLM。
"""
import sys
import uuid
from datetime import datetime
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import app.models  # noqa: E402,F401
from app.database import Base  # noqa: E402
from app.models.system_config import SystemConfig  # noqa: E402
from app.models.trade import TradeRecord  # noqa: E402
from app.tasks import review_tasks  # noqa: E402
from app.services import ai_attribution  # noqa: E402


@pytest.fixture()
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    session.add(SystemConfig(id=1, kline_interval="1h"))
    session.commit()
    yield session
    session.close()


class _FakePool:
    @staticmethod
    def get_klines_range(symbol, interval, bars, end_ms):
        return []


@pytest.fixture()
def fake_attr(monkeypatch):
    """attribute_trade 替身：记录调用到的交易，返回固定结论"""
    calls = []

    def _fake(rec, klines):
        calls.append(rec.id)
        return {"tags": ["追高"], "lesson": "测试教训"}

    monkeypatch.setattr(ai_attribution, "attribute_trade", _fake)
    return calls


def _rec(testnet: bool, analysis_id=None, attributed=None, **kw):
    base = dict(
        symbol="GTCUSDT", direction="long", status="CLOSED",
        exit_reason="sl", testnet=testnet,
        account_key="testnet-1" if testnet else "live-1",
        ai_analysis_id=analysis_id,
        attributed_at=attributed,
        opened_at=datetime(2026, 10, 2, 10, 0),
        closed_at=datetime(2026, 10, 2, 12, 0),
    )
    base.update(kw)
    return TradeRecord(**base)


def test_sibling_live_preferred(db, fake_attr):
    """同批双网镜像单：正式网归因，测试网跳过打标"""
    aid = uuid.uuid4()
    live = _rec(testnet=False, analysis_id=aid)
    tn = _rec(testnet=True, analysis_id=aid)
    db.add_all([live, tn])
    db.commit()

    results = review_tasks.run_ai_attribution(db, _FakePool(), None, [live, tn])
    assert len(fake_attr) == 1 and fake_attr[0] == live.id  # 只归因正式网那笔
    assert len(results) == 1
    db.refresh(tn)
    assert tn.attributed_at is not None
    assert "镜像单" in (tn.error_lesson or "")
    assert tn.error_tags is None  # 跳过单不产标签，不污染统计


def test_sibling_cross_round_skip(db, fake_attr):
    """跨轮：测试网单先归因过，正式网镜像单后到 → 跳过（不重复花 LLM）"""
    aid = uuid.uuid4()
    tn = _rec(testnet=True, analysis_id=aid,
              attributed=datetime(2026, 10, 2, 13, 0))
    db.add(tn)
    db.commit()
    live = _rec(testnet=False, analysis_id=aid)
    db.add(live)
    db.commit()

    results = review_tasks.run_ai_attribution(db, _FakePool(), None, [live])
    assert fake_attr == []  # 未调 LLM
    db.refresh(live)
    assert live.attributed_at is not None
    assert "镜像单" in (live.error_lesson or "")


def test_no_analysis_unaffected(db, fake_attr):
    """导入持仓/手动单（无 analysis 关联）：各自独立归因，不受去重影响"""
    a = _rec(testnet=False)
    b = _rec(testnet=True, symbol="ETHUSDT")
    db.add_all([a, b])
    db.commit()

    results = review_tasks.run_ai_attribution(db, _FakePool(), None, [a, b])
    assert len(fake_attr) == 2
    assert len(results) == 2


def test_different_analysis_both_attributed(db, fake_attr):
    """不同 analysis（只是同币）：各自归因，不误伤"""
    a = _rec(testnet=False, analysis_id=uuid.uuid4())
    b = _rec(testnet=True, analysis_id=uuid.uuid4())
    db.add_all([a, b])
    db.commit()

    results = review_tasks.run_ai_attribution(db, _FakePool(), None, [a, b])
    assert len(fake_attr) == 2
    assert len(results) == 2
