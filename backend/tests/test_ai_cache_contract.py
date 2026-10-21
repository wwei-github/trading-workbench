"""缓存复用必须保留实际风险与归因语义；只替换外部开仓分发。"""
from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

import app.models  # noqa: F401
from app.database import Base
from app.models.scan import AIAnalysis, ScanRecord, ScanResult
from app.services.trade_engine import _near_discounted_budget
from app.models.system_config import SystemConfig
from app.tasks import ai_tasks


@pytest.fixture
def db(monkeypatch):
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    monkeypatch.setattr(ai_tasks, "_dispatch_immediate_open", lambda row: None)
    with Session(engine) as session:
        yield session
    engine.dispose()


def _signal(db):
    record = ScanRecord(scan_type="manual")
    db.add(record)
    db.flush()
    row = ScanResult(scan_record_id=record.id, symbol="TESTUSDT",
                     signal_type="trigger_pullback_confirm", current_price=100,
                     breakout_pct=0, volume=100, trend_slope=0, r_squared=0.5)
    db.add(row)
    db.commit()
    return row


def test_cached_near_anchor_preserves_execution_risk(db):
    src = AIAnalysis(symbol="TESTUSDT", trade_decision="suggest", direction="long",
                     entry_price=100, stop_loss=99, take_profit_1=102,
                     recommendation=70, stop_anchor="near", position_pct=None,
                     tactic_name="回踩", tactic_reason="触点确认", htf_phase="aligned_bull")
    db.add(src)
    db.commit()
    target = _signal(db)
    ai_tasks._copy(db, target, target.symbol, src, "fp", "cached")
    copied = db.query(AIAnalysis).filter_by(scan_result_id=target.id).one()
    assert _near_discounted_budget(10, copied.stop_anchor, factor=0.7) == 7
    assert copied.tactic_name == "回踩"
    assert copied.tactic_reason == "触点确认"
    assert copied.htf_phase == "aligned_bull"
    assert copied.position_pct is None
    assert copied.pullback_wait is False


def test_copy_recomputes_wait_flag_at_latest_price(db):
    src = AIAnalysis(symbol="TESTUSDT", trade_decision="suggest", direction="long",
                     entry_price=100, stop_loss=99, take_profit_1=102,
                     recommendation=70, pullback_wait=False)
    db.add(src)
    db.commit()
    target = _signal(db)
    ai_tasks._copy(db, target, target.symbol, src, "fp", "cached", current_price=101)
    copied = db.query(AIAnalysis).filter_by(scan_result_id=target.id).one()
    assert copied.pullback_wait is True


def test_copy_keeps_veto_replay_evidence(db):
    snapshot = {"source": "htf_constraint", "direction": "long", "entry_price": 100,
                "stop_loss": 99, "take_profit_1": 102, "ab_bucket": "transition_pbc"}
    src = AIAnalysis(symbol="TESTUSDT", trade_decision="skip", recommendation=0,
                     veto_original=snapshot, htf_phase="bull_dip")
    db.add(src)
    db.commit()
    target = _signal(db)
    ai_tasks._copy(db, target, target.symbol, src, "fp", "cached")
    copied = db.query(AIAnalysis).filter_by(scan_result_id=target.id).one()
    assert copied.veto_original == snapshot
    assert copied.htf_phase == "bull_dip"


def test_copied_decision_cannot_extend_original_cache_lifetime(db):
    original_time = datetime.utcnow() - timedelta(minutes=61)
    src = AIAnalysis(symbol="TESTUSDT", trade_decision="suggest", direction="long",
                     entry_price=100, stop_loss=99, take_profit_1=102,
                     recommendation=70, created_at=original_time)
    db.add(src)
    db.commit()
    target = _signal(db)
    ai_tasks._copy(db, target, target.symbol, src, "expired-fp", "cached")
    assert ai_tasks._find_recent(db, target.symbol, "expired-fp", 60) is None


def test_fingerprint_changes_with_signal_bar_and_configuration():
    signal = {"symbol": "TESTUSDT", "signal_type": "trigger_breakout", "current_price": 100}
    cfg = SystemConfig(id=1, kline_interval="1h", min_recommendation=60)
    a = ai_tasks.compute_fingerprint(signal, context={"bar": 1000, "min_rec": cfg.min_recommendation})
    b = ai_tasks.compute_fingerprint(signal, context={"bar": 2000, "min_rec": cfg.min_recommendation})
    c = ai_tasks.compute_fingerprint(signal, context={"bar": 1000, "min_rec": 70})
    assert a != b
    assert a != c


def test_cache_context_changes_with_closed_candles_and_current_rules():
    cfg = SystemConfig(id=1, kline_interval="1h", min_recommendation=60)
    candles = [[1000, 100, 101, 99, 100, 10], [2000, 100, 101, 100, 101, 3]]
    original = ai_tasks.build_cache_context(cfg, candles, candles, "tactics", 0)
    cfg.min_recommendation = 70
    changed = ai_tasks.build_cache_context(cfg, candles, candles, "tactics", 0)
    assert original != changed
    cfg.min_recommendation = 60
    revised = [list(bar) for bar in candles]
    revised[0][4] = 99.5
    assert original != ai_tasks.build_cache_context(cfg, revised, candles, "tactics", 0)


def test_single_task_reuses_only_identical_context(db, monkeypatch):
    from app.config import settings
    cfg = SystemConfig(id=1, ai_analysis_enabled=True, dual_judge_enabled=False,
                       kline_interval="1h", min_recommendation=60)
    db.add(cfg)
    target = _signal(db)
    maker = sessionmaker(bind=db.bind)
    monkeypatch.setattr(ai_tasks, "SessionLocal", maker)
    monkeypatch.setattr(settings, "AI_API_KEY", "test-only")
    monkeypatch.setattr(settings, "FINGERPRINT_TTL_MIN", 60)
    monkeypatch.setattr(ai_tasks, "_sem_acquire", lambda: "")
    monkeypatch.setattr(ai_tasks, "_sem_release", lambda ident: None)
    monkeypatch.setattr(ai_tasks.md, "in_forming_tail", lambda *args: False)
    monkeypatch.setattr(ai_tasks.md, "screen_klines", lambda bars, *args: bars)
    monkeypatch.setattr(ai_tasks, "annotate_htf_phase", lambda result, *args, **kw: result)
    monkeypatch.setattr(ai_tasks, "htf_context", lambda bars: {
        "direction": "long", "reject_reason": None, "block_both": False})
    for name in ("push_start", "push", "push_done"):
        monkeypatch.setattr(ai_tasks.ai_progress, name, lambda *args, **kw: None)
    from app.services import tactics_match
    monkeypatch.setattr(tactics_match, "load_active_tactics", lambda scope: [])
    candles = [[i * 3600000, 100, 101, 99, 100, 10] for i in range(600)]
    class Pool:
        def get_klines(self, *args, **kwargs):
            return candles
    monkeypatch.setattr(ai_tasks, "ExchangePool", Pool)
    calls = []
    def analyze(*args, **kwargs):
        calls.append(1)
        return {"trade_decision": "suggest", "direction": "long", "entry_price": 100,
                "stop_loss": 99, "take_profit_1": 102, "recommendation": 70,
                "stop_anchor": "near"}
    monkeypatch.setattr(ai_tasks, "analyze_with_guard", analyze)
    for expected in (1, 1):
        ai_tasks.run_ai_analysis_single.run(str(target.id), str(target.scan_record_id))
        assert len(calls) == expected
    db.expire_all()
    cfg = db.get(SystemConfig, 1)
    cfg.min_recommendation = 70
    db.commit()
    ai_tasks.run_ai_analysis_single.run(str(target.id), str(target.scan_record_id))
    assert len(calls) == 2


def test_cache_rejects_incomplete_suggest_contract():
    invalid = AIAnalysis(symbol="TESTUSDT", trade_decision="suggest", direction="long",
                         stop_loss=None, take_profit_1=102)
    assert ai_tasks._conclusion_still_valid(invalid, 100) is False
