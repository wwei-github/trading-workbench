# -*- coding: utf-8 -*-
"""实盘归因 RR 擦边分组（actual_review，2026-10-15）

ai_rr_tol 容差带三处共用后，计划 RR 低于铁律下限、仅靠容差放进的边际单
（擦边过闸）需与达标单分组对比胜率/EV——容差带是否漏损，数据说话。
下限取 system_config.ai_rr_min（页面覆盖）回落 env 默认；无快照归「未知」。

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
from app.database import Base  # noqa: E402
from app.models.system_config import SystemConfig  # noqa: E402
from app.models.trade import TradeRecord  # noqa: E402
from app.services.actual_review import build_actual_review  # noqa: E402


@pytest.fixture()
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    session.add(SystemConfig(id=1, kline_interval="1h"))
    session.commit()
    yield session
    session.close()


def _rec(rr, pnl=1.0):
    now = datetime.utcnow()
    return TradeRecord(
        symbol="GTCUSDT", direction="long", status="CLOSED",
        exit_reason="sl", testnet=False, account_key="live-1",
        opened_at=now - timedelta(hours=3), closed_at=now - timedelta(hours=1),
        realized_pnl=pnl,
        ai_snapshot={"risk_reward_ratio": rr} if rr is not None else None,
    )


def _rr_band_groups(result: dict) -> dict:
    return {g["key"]: g for g in result["groups"] if g["dim"] == "RR擦边"}


def test_rr_band_split_by_threshold(db):
    """1.49（仅靠容差过闸）与 1.6（达标）分桶；无快照归未知"""
    db.add_all([_rec(1.49, pnl=-1.0), _rec(1.6), _rec(None)])
    db.commit()
    bands = _rr_band_groups(build_actual_review(db, days=7))
    assert bands["擦边过闸"]["count"] == 1
    assert bands["RR达标"]["count"] == 1
    assert bands["未知"]["count"] == 1


def test_rr_band_threshold_follows_config_override(db):
    """页面覆盖 ai_rr_min=1.4 后，1.49 从擦边变为达标"""
    db.get(SystemConfig, 1).ai_rr_min = 1.4
    db.add(_rec(1.49))
    db.commit()
    bands = _rr_band_groups(build_actual_review(db, days=7))
    assert bands["RR达标"]["count"] == 1
    assert "擦边过闸" not in bands


def test_excluded_trade_not_counted_in_review(db):
    """异常单标记（2026-10-18）：excluded=true 的已平仓单不计入复盘统计口径
    （笔数/净盈亏），伪巨亏不污染资金分析/复盘。"""
    fake = _rec(1.6, pnl=-2330.51)
    fake.excluded = True
    db.add_all([_rec(1.6, pnl=10.0), fake])
    db.commit()
    result = build_actual_review(db, days=7)
    assert result["overview"]["count"] == 1
    assert result["overview"]["pnl"] == pytest.approx(10.0)
