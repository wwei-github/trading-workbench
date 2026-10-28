# -*- coding: utf-8 -*-
"""规则补丁触发双阈值（17-复盘审计 P0-3）：热度 ≥0.30 或绝对笔数 ≥20。"""
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import app.models  # noqa: E402,F401
from app.database import Base  # noqa: E402
from app.models.trade import TradeRecord  # noqa: E402
from app.services import patch_service  # noqa: E402


@pytest.fixture()
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = Session(bind=engine)
    yield session
    session.close()


def _trade(db, tags):
    db.add(TradeRecord(
        symbol="TUSDT",
        direction="long",
        closed_at=datetime.utcnow() - timedelta(days=1),
        realized_pnl=1,
        excluded=False,
        error_tags=tags,
    ))


def test_dual_threshold_heat_or_absolute_count(db, monkeypatch):
    """止损过宽 20/67≈0.2985（热度不足但笔数达标）→ 触发；追高追空 7/67 两关都不过。"""
    for _ in range(7):
        _trade(db, ["追高追空", "止损过宽"])
    for _ in range(13):
        _trade(db, ["止损过宽"])
    # 其余 47 笔均摊到三个规则标签，各自热度与笔数都不过关（避免单个标签热度超阈值）
    for _ in range(17):
        _trade(db, ["执行偏差"])
    for _ in range(15):
        _trade(db, ["逆指标多数"])
    for _ in range(15):
        _trade(db, ["区间中段开单"])
    db.commit()
    assert db.query(TradeRecord).count() == 67

    picked = []

    def fake_generate(db_, tag, heat, stats, case_ids):
        picked.append(tag)
        return None  # 不落补丁行，只验证门槛放行

    monkeypatch.setattr(patch_service, "_generate_draft", fake_generate)
    result = patch_service.run_trigger(db)

    assert picked == ["止损过宽"]
    assert any("止损过宽 热度 0.30/20 笔达标" in s for s in result["skips"])
    assert any("追高追空 热度 0.10/7 笔" in s for s in result["skips"])
    assert any("热度未达标（门槛 0.3 或绝对笔数 20）" in s for s in result["skips"])
