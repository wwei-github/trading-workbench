# -*- coding: utf-8 -*-
"""Review price-path replay boundaries and execution semantics."""
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import app.models  # noqa: E402,F401
from app.database import Base  # noqa: E402
from app.models.scan import AIAnalysis, ScanRecord, ScanResult, TradeReview  # noqa: E402
from app.tasks import review_tasks  # noqa: E402

replay_suggestion = review_tasks.replay_suggestion


@pytest.fixture()
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = Session(bind=engine)
    yield session
    session.close()


def _make_signal(db):
    created = datetime(2026, 10, 20, 16)
    sr_rec = ScanRecord(scan_type="scheduled", created_at=created)
    db.add(sr_rec)
    db.flush()
    sr = ScanResult(scan_record_id=sr_rec.id, symbol="TUSDT", signal_type="trigger_breakout",
                    current_price=100, breakout_pct=1, trend_slope=0, r_squared=.5,
                    volume=1, created_at=created)
    db.add(sr)
    db.flush()
    a = AIAnalysis(scan_result_id=sr.id, symbol="TUSDT", created_at=created)
    db.add(a)
    db.commit()
    return a


@pytest.fixture()
def review_env(db, monkeypatch):
    monkeypatch.setattr(review_tasks, "SessionLocal", sessionmaker(bind=db.bind))
    monkeypatch.setattr(review_tasks, "open_task", lambda **kw: "tid")
    monkeypatch.setattr(review_tasks, "close_task", lambda *a, **kw: None)
    return db

HOUR = 3_600_000


def bar(ts, high, low, close=100.0):
    return [ts, close, high, low, close, 1000.0]


def test_entry_must_be_touched_before_exit_levels_count():
    bars = [bar(0, 110, 95), bar(HOUR, 103, 99)]
    # First bar spans all prices but no signal entry order fill can be inferred
    # from OHLC; only a bar containing the entry activates the counterfactual.
    outcome, tp1_hit, count = replay_suggestion(
        "long", 100, 98, 104, 108, bars
    )
    assert (outcome, tp1_hit, count) == ("loss", False, 1)


def test_entry_bar_stop_is_conservative_and_target_is_not_counted():
    outcome, tp1_hit, count = replay_suggestion(
        "long", 100, 98, 104, 108, [bar(0, 105, 97)]
    )
    assert (outcome, tp1_hit, count) == ("loss", False, 1)


def test_entry_bar_target_without_stop_is_not_counted_as_win():
    outcome, tp1_hit, count = replay_suggestion(
        "long", 100, 98, 104, 108, [bar(0, 105, 100)]
    )
    assert (outcome, tp1_hit, count) == ("open", False, 1)


def test_short_entry_requires_full_entry_price_inside_bar():
    outcome, tp1_hit, count = replay_suggestion(
        "short", 100, 102, 96, 92, [bar(0, 103, 99)]
    )
    assert (outcome, tp1_hit, count) == ("loss", False, 1)


def test_utc_signal_time_and_exact_24_hour_window():
    from app.tasks.review_tasks import _review_window

    signal = datetime(2026, 1, 1, 0, tzinfo=timezone.utc)
    start_ms, end_ms = _review_window(signal)
    assert start_ms == int(signal.timestamp() * 1000)
    assert end_ms == start_ms + 24 * HOUR


def test_incomplete_hourly_window_is_detected():
    from app.tasks.review_tasks import _has_complete_window

    start = 1_000_000
    first = start - start % HOUR
    if first < start:
        first += HOUR
    bars = [bar(first + n * HOUR, 110, 90) for n in range(23)]
    assert _has_complete_window(bars, start, start + 24 * HOUR, "1h")
    assert not _has_complete_window(bars[:8] + bars[9:], start, start + 24 * HOUR, "1h")


def test_task_uses_historical_range_and_never_finalizes_missing_data(review_env, monkeypatch):
    from app.models.scan import TradeReview

    analysis = _make_signal(review_env)
    analysis.trade_decision = "suggest"
    analysis.direction = "long"
    analysis.entry_price = 100
    analysis.stop_loss = 98
    analysis.take_profit_1 = 104
    review_env.commit()
    calls = []

    class MissingPool:
        def get_klines_range(self, symbol, interval, limit, end_time_ms):
            calls.append((symbol, interval, limit, end_time_ms))
            return []

    monkeypatch.setattr("app.tasks.review_tasks.ExchangePool", MissingPool)
    monkeypatch.setattr("app.tasks.review_tasks.datetime", FrozenDateTime)
    monkeypatch.setattr(review_tasks, "REVIEW_SHARDS_PER_RUN", review_tasks.REVIEW_SHARD_COUNT)
    review_tasks.run_trade_review_task.apply()
    assert calls
    assert review_env.query(TradeReview).count() == 0


def test_task_finalizes_unfilled_entry_as_expired(review_env, monkeypatch):
    analysis = _make_signal(review_env)
    analysis.trade_decision = "suggest"
    analysis.direction = "long"
    analysis.entry_price = 100
    analysis.stop_loss = 98
    analysis.take_profit_1 = 104
    review_env.commit()

    class NoEntryPool:
        def get_klines_range(self, symbol, interval, limit, end_time_ms):
            step = HOUR
            last_open = end_time_ms - end_time_ms % step
            first = last_open - (limit - 1) * step
            return [bar(first + n * step, 99, 90) for n in range(limit)]

    monkeypatch.setattr(review_tasks, "ExchangePool", NoEntryPool)
    monkeypatch.setattr(review_tasks, "datetime", FrozenDateTime)
    monkeypatch.setattr(review_tasks, "REVIEW_SHARDS_PER_RUN", review_tasks.REVIEW_SHARD_COUNT)
    review_tasks.run_trade_review_task.apply()
    review = review_env.query(TradeReview).one()
    assert review.outcome == "expired"


def test_first_partial_hour_is_excluded_from_counterfactual_window():
    from app.tasks.review_tasks import _has_complete_window

    start = HOUR + 10 * 60_000
    end = start + 24 * HOUR
    first_full_open = 2 * HOUR
    bars = [bar(first_full_open + i * HOUR, 105, 99) for i in range(23)]
    assert _has_complete_window(bars, start, end, "1h")
    assert not _has_complete_window(bars[1:], start, end, "1h")


def test_utc_hour_shards_cover_full_lookback_in_bounded_rounds():
    from app.tasks.review_tasks import REVIEW_SHARDS_PER_RUN, REVIEW_SHARD_COUNT, _review_shard_hours

    now = datetime(2026, 10, 21, 17, tzinfo=timezone.utc)
    visited = []
    rounds = (REVIEW_SHARD_COUNT + REVIEW_SHARDS_PER_RUN - 1) // REVIEW_SHARDS_PER_RUN
    for round_no in range(rounds):
        visited.extend(_review_shard_hours(now, round_no))
    assert len(visited) == REVIEW_SHARD_COUNT
    assert set(visited) == set(range(REVIEW_SHARD_COUNT))


def test_old_incomplete_shard_does_not_hide_later_complete_row(review_env, monkeypatch):
    from app.tasks.review_tasks import REVIEW_BATCH_SIZE, REVIEW_SHARD_COUNT, _review_shard_hours

    now = datetime(2026, 10, 21, 17, tzinfo=timezone.utc)
    selected = _review_shard_hours(now, 0)
    target_shard = selected[-1]
    old_shard = (target_shard + 40) % REVIEW_SHARD_COUNT

    for i in range(REVIEW_BATCH_SIZE + 3):
        a = _make_signal(review_env)
        a.symbol = f"OLD{i}USDT"
        a.created_at = (now - timedelta(hours=old_shard + 24, minutes=5)).replace(tzinfo=None)
        a.trade_decision = "suggest"
        a.direction = "long"
        a.entry_price = 100
        a.stop_loss = 98
        a.take_profit_1 = 104
    complete = _make_signal(review_env)
    complete.symbol = "LATERUSDT"
    complete.created_at = (now - timedelta(hours=target_shard + 24, minutes=5)).replace(tzinfo=None)
    complete.trade_decision = "suggest"
    complete.direction = "long"
    complete.entry_price = 100
    complete.stop_loss = 98
    complete.take_profit_1 = 104
    review_env.commit()
    seen = []

    class CompletePool:
        def get_klines_range(self, symbol, interval, limit, end_time_ms):
            seen.append(symbol)
            if symbol != "LATERUSDT":
                return []
            last_open = end_time_ms - end_time_ms % HOUR
            first = last_open - (limit - 1) * HOUR
            return [bar(first + n * HOUR, 99, 90) for n in range(limit)]

    monkeypatch.setattr(review_tasks, "ExchangePool", CompletePool)
    monkeypatch.setattr(review_tasks, "datetime", FrozenDateTime)
    monkeypatch.setattr(review_tasks, "REVIEW_SHARDS_PER_RUN", REVIEW_SHARD_COUNT)
    monkeypatch.setattr(review_tasks, "datetime", FrozenDateTime)
    review_tasks.run_trade_review_task.apply()
    assert "LATERUSDT" in seen
    assert review_env.query(TradeReview).filter_by(symbol="LATERUSDT").one().outcome == "expired"


def test_review_batch_is_capped(review_env, monkeypatch):
    from app.tasks.review_tasks import REVIEW_BATCH_SIZE, REVIEW_SHARD_COUNT

    monkeypatch.setattr(review_tasks, "REVIEW_SHARDS_PER_RUN", REVIEW_SHARD_COUNT)
    monkeypatch.setattr(review_tasks, "datetime", FrozenDateTime)
    for i in range(REVIEW_BATCH_SIZE + 1):
        analysis = _make_signal(review_env)
        analysis.symbol = f"T{i}USDT"
        analysis.trade_decision = "suggest"
        analysis.direction = "long"
        analysis.entry_price = 100
        analysis.stop_loss = 98
        analysis.take_profit_1 = 104
    review_env.commit()
    requested = []

    class MissingPool:
        def get_klines_range(self, symbol, interval, limit, end_time_ms):
            requested.append(symbol)
            return []

    monkeypatch.setattr(review_tasks, "ExchangePool", MissingPool)
    monkeypatch.setattr(review_tasks, "datetime", FrozenDateTime)
    review_tasks.run_trade_review_task.apply()
    assert len(requested) == REVIEW_BATCH_SIZE


class FrozenDateTime(datetime):
    @classmethod
    def utcnow(cls):
        return datetime(2026, 10, 21, 17, 0, 0)

    @classmethod
    def now(cls, tz=None):
        value = datetime(2026, 10, 21, 17, 0, 0)
        return value.replace(tzinfo=tz) if tz else value


def test_stuck_shard_shares_budget_with_newer_shard(review_env, monkeypatch):
    """Same run: a shard full of data-less rows must not consume the whole batch."""
    from app.tasks.review_tasks import (
        REVIEW_BATCH_SIZE, REVIEW_SHARD_SECONDS, _due_shard,
    )

    now = datetime(2026, 10, 21, 17, tzinfo=timezone.utc)
    current = _due_shard(now - timedelta(hours=24))
    newer = (current - 1) % review_tasks.REVIEW_SHARD_COUNT
    stuck = (current - 2) % review_tasks.REVIEW_SHARD_COUNT

    def due_for(shard):
        base_bucket = int((now - timedelta(hours=24)).timestamp() // REVIEW_SHARD_SECONDS)
        bucket = base_bucket - ((base_bucket - shard) % review_tasks.REVIEW_SHARD_COUNT)
        return datetime.utcfromtimestamp(bucket * REVIEW_SHARD_SECONDS + 60)

    for i in range(REVIEW_BATCH_SIZE + 2):
        a = _make_signal(review_env)
        a.symbol = f"STUCK{i}USDT"
        a.created_at = due_for(stuck)
        a.trade_decision = "suggest"
        a.direction = "long"
        a.entry_price = 100
        a.stop_loss = 98
        a.take_profit_1 = 104
    later = _make_signal(review_env)
    later.symbol = "NEWERUSDT"
    later.created_at = due_for(newer)
    later.trade_decision = "suggest"
    later.direction = "long"
    later.entry_price = 100
    later.stop_loss = 98
    later.take_profit_1 = 104
    review_env.commit()
    assert _due_shard(due_for(stuck)) == stuck
    assert _due_shard(due_for(newer)) == newer
    seen = []

    class Pool:
        def get_klines_range(self, symbol, interval, limit, end_time_ms):
            seen.append(symbol)
            return []

    monkeypatch.setattr(review_tasks, "ExchangePool", Pool)
    monkeypatch.setattr(review_tasks, "datetime", FrozenDateTime)
    review_tasks.run_trade_review_task.apply()
    assert "NEWERUSDT" in seen
    assert len(seen) == REVIEW_BATCH_SIZE
