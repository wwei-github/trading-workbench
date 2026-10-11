# -*- coding: utf-8 -*-
"""分析行保护回归（2026-10-11，ALTUSDT 案例）。

_upsert_ai_analysis 原 latest-wins 原地覆盖：同一 scan_result_id 再次分析
时直接改写旧行且 created_at 不变——已开单引用的行被静默改写后，交易记录
ai_snapshot（开单时冻结）与 AI 卡片（显示新结论）脱钩，看卡片的人被误导
（线上实证：ALTUSDT 开单快照 SL=0.007845/RR=1.5，卡片却显示改写后的
SL=0.008029/RR=2.03，两轮 analysis 文本措辞不同）。

修复：被 trade_records/trade_reviews 引用的旧行摘钩保留
（scan_result_id=None，与手动单币重分析同语义），新结论另起新行照常走
开仓闸门；无引用行保持 latest-wins 原地更新（语义不变）。
"""
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

from app.database import Base  # noqa: E402
import app.models.scan  # noqa: E402,F401  (注册表)
import app.models.trade  # noqa: E402,F401
import app.models.system_config  # noqa: E402,F401
from app.models.scan import AIAnalysis, TradeReview  # noqa: E402
from app.models.trade import TradeRecord  # noqa: E402
from app.tasks.ai_tasks import _upsert_ai_analysis  # noqa: E402


@pytest.fixture()
def db():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    s = Session(engine)
    try:
        yield s
    finally:
        s.close()
        engine.dispose()


def _mk_analysis(db, scan_result_id, stop_loss=0.007845, analysis="旧结论"):
    row = AIAnalysis(
        scan_result_id=scan_result_id, symbol="ALTUSDT",
        trade_decision="suggest", direction="long",
        entry_price=0.00817, stop_loss=stop_loss,
        take_profit_1=0.008658, recommendation=75.0,
        analysis=analysis, fingerprint="fp-old",
    )
    db.add(row)
    db.commit()
    return row


# 第二轮分析的新结论（三价不同——LLM 非确定性，同数据重分析常见）
_NEW = {
    "trade_decision": "suggest", "direction": "long",
    "entry_price": 0.00817, "stop_loss": 0.008029,
    "take_profit_1": 0.008456, "recommendation": 80.0,
    "analysis": "新结论",
}


def test_referenced_by_trade_row_unhooked(db):
    """已开单引用的行：摘钩保留原值，新结论另起新行并挂回 scan_result_id"""
    rid = uuid.uuid4()
    old = _mk_analysis(db, rid)
    db.add(TradeRecord(symbol="ALTUSDT", direction="long",
                       ai_analysis_id=old.id))
    db.commit()

    row = _upsert_ai_analysis(db, rid, "ALTUSDT", _NEW, fingerprint="fp-new")
    db.commit()

    db.refresh(old)
    # 旧行原值保留、摘钩（AI 卡片继续显示开单时的结论）
    assert float(old.stop_loss) == 0.007845
    assert old.analysis == "旧结论"
    assert old.scan_result_id is None
    assert old.fingerprint == "fp-old"
    # 新结论另起新行（返回行供 _dispatch_immediate_open 走开仓闸门）
    assert row.id != old.id
    assert float(row.stop_loss) == 0.008029
    assert row.analysis == "新结论"
    assert row.scan_result_id == rid
    assert row.fingerprint == "fp-new"


def test_referenced_by_review_row_unhooked(db):
    """复盘引用同样保护（归因结论不能被改写）"""
    rid = uuid.uuid4()
    old = _mk_analysis(db, rid)
    db.add(TradeReview(ai_analysis_id=old.id, symbol="ALTUSDT",
                       outcome="win_tp1"))
    db.commit()

    row = _upsert_ai_analysis(db, rid, "ALTUSDT", _NEW, fingerprint="fp-new")
    db.commit()

    db.refresh(old)
    assert float(old.stop_loss) == 0.007845
    assert old.scan_result_id is None
    assert row.id != old.id and row.scan_result_id == rid


def test_unreferenced_row_latest_wins(db):
    """无引用行保持 latest-wins 原地更新（重复分析同 scan_result 不堆积行）"""
    rid = uuid.uuid4()
    old = _mk_analysis(db, rid)

    row = _upsert_ai_analysis(db, rid, "ALTUSDT", _NEW, fingerprint="fp-new")
    db.commit()

    db.refresh(old)
    assert row.id == old.id
    assert float(old.stop_loss) == 0.008029
    assert old.analysis == "新结论"
    assert old.scan_result_id == rid


def test_reanalysis_after_unhook_starts_new_row(db):
    """摘钩后的第三轮分析：scan_result_id 只挂新行，不撞摘钩旧行（filter 只匹配非 NULL）"""
    rid = uuid.uuid4()
    old = _mk_analysis(db, rid)
    db.add(TradeRecord(symbol="ALTUSDT", direction="long",
                       ai_analysis_id=old.id))
    db.commit()
    second = _upsert_ai_analysis(db, rid, "ALTUSDT", _NEW, fingerprint="fp-2")
    db.commit()

    third = _upsert_ai_analysis(
        db, rid, "ALTUSDT", dict(_NEW, stop_loss=0.008100, analysis="第三轮"),
        fingerprint="fp-3")
    db.commit()

    # 第三轮覆盖的是第二轮行（second 无引用），旧行不受影响
    assert third.id == second.id
    assert float(third.stop_loss) == 0.008100
    db.refresh(old)
    assert old.scan_result_id is None
    assert float(old.stop_loss) == 0.007845
