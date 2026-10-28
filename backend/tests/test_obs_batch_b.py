# -*- coding: utf-8 -*-
"""批次 B 观测层回归（2026-10-21，docs/需求文档/06 tasks.md B1/B2/B3）

B1 三段时间戳：ScanResult.signal_created_at / AIAnalysis.ai_finished_at /
   TradeRecord.opened_at → /review/stats latency 三段 P50/P90（不改交易行为）
B2 htf_degraded：4h 数据缺失类拒绝（拉取失败/空数据/计算异常/reject∈data·adx）
   打 degraded 标记（funnel 数据质量统计）；AI 层 4h K线缺失落 AIAnalysis
B3 复盘按 htf_degraded 分组：TradeReview 快照 + /review/stats 两组胜率/期望/盈亏比

sqlite 内存库 create_all 建表；celery task 用 .apply() 本地执行，不触网。
"""
import sys
import uuid
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import app.models  # noqa: E402,F401
from app.database import Base  # noqa: E402
from app.models.scan import AIAnalysis, ScanRecord, ScanResult, TradeReview  # noqa: E402
from app.models.trade import TradeRecord  # noqa: E402
from app.services.scanner import Scanner, detect_indicator_candidate  # noqa: E402
from app.tasks import review_tasks  # noqa: E402
from app.tasks.ai_tasks import _upsert_ai_analysis  # noqa: E402


@pytest.fixture()
def db():
    # StaticPool：同一内存库跨 session 共享（_save_results/复盘任务各自开 session）
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


# ── B1：三段时间戳 ─────────────────────────────────────────────────────


def test_save_results_writes_signal_created_at(db, monkeypatch):
    """扫描落库：候选产生时刻 detected_at → ScanResult.signal_created_at；
    历史/手工命中无该键时留 NULL（不回填 created_at，避免伪装成信号时刻）"""
    import app.services.scanner as scanner_mod

    monkeypatch.setattr(scanner_mod, "SessionLocal", sessionmaker(bind=db.bind))
    rec = ScanRecord(scan_type="scheduled", created_at=datetime.utcnow())
    db.add(rec)
    db.commit()

    detected = datetime.utcnow() - timedelta(seconds=42)
    Scanner._save_results(rec.id, [{
        "symbol": "B1USDT", "signal_type": "trigger_breakout",
        "current_price": 100.0, "breakout_pct": 1.0, "detected_at": detected,
    }])
    Scanner._save_results(rec.id, [{
        "symbol": "B1OLDUSDT", "signal_type": "trigger_breakout",
        "current_price": 100.0, "breakout_pct": 1.0,
    }])

    rows = {r.symbol: r for r in db.query(ScanResult).all()}
    assert rows["B1USDT"].signal_created_at == detected
    assert rows["B1OLDUSDT"].signal_created_at is None


def test_upsert_writes_ai_finished_at(db):
    """AI 结论落库：ai_finished_at 每次覆写（created_at 原地更新不刷新）"""
    before = datetime.utcnow()
    row = _upsert_ai_analysis(
        db, uuid.uuid4(), "B1AIUSDT",
        {"trade_decision": "skip", "analysis": "x"}, fingerprint="fp-b1",
    )
    db.commit()
    assert row.ai_finished_at is not None
    assert row.ai_finished_at >= before


def _trend_1h(n: int = 300, drift: float = 0.35) -> list[list]:
    """稳步上行 1h K 线（与 test_htf_ranging_filter 同构，供 4h 关卡前处理）"""
    kl = []
    price = 100.0
    ts = 1_700_000_000_000
    for i in range(n):
        o = price
        price += drift
        kl.append([ts + i * 3_600_000, o, price + 0.15, price - 0.25, price, 100.0, 100.0])
    return kl


class _FakePool:
    def __init__(self, klines_1h, klines_4h):
        self._m = {"1h": klines_1h, "4h": klines_4h}

    def get_klines(self, symbol, interval, limit, **kwargs):
        return self._m[interval][:limit]


def _mk_chain(db, symbol, signal_at=None, ai_finished=None, opened_at=None,
              htf_degraded=False, decision="suggest"):
    """信号 → AI 分析 →（可选）开仓 一条链，供 B1/B3 统计夹具"""
    created = ai_finished or datetime.utcnow()
    rec = ScanRecord(scan_type="scheduled", created_at=created)
    db.add(rec)
    db.flush()
    sr = ScanResult(
        scan_record_id=rec.id, symbol=symbol, signal_type="trigger_breakout",
        current_price=100.0, breakout_pct=1.0, trend_slope=0.0, r_squared=0.5,
        volume=1.5, created_at=created, signal_created_at=signal_at,
    )
    db.add(sr)
    db.flush()
    a = AIAnalysis(
        scan_result_id=sr.id, symbol=symbol, trade_decision=decision,
        direction="long", entry_price=100.0, stop_loss=98.0,
        take_profit_1=104.0, take_profit_2=108.0, recommendation=70,
        htf_degraded=htf_degraded, created_at=created, ai_finished_at=ai_finished,
    )
    db.add(a)
    db.flush()
    tr = None
    if opened_at is not None:
        tr = TradeRecord(
            symbol=symbol, direction="long", testnet=True,
            ai_analysis_id=a.id, scan_result_id=sr.id, opened_at=opened_at,
        )
        db.add(tr)
    db.commit()
    return sr, a, tr


def test_latency_three_segments(db):
    """/review/stats latency：三段时延 P50/P90（线性插值）；负值样本剔除"""
    from app.api.scan import review_stats

    base = datetime.utcnow() - timedelta(hours=3)
    for i, (sig_ai_s, ai_open_s) in enumerate([(60, 30), (120, 60)]):
        sig = base
        ai = sig + timedelta(seconds=sig_ai_s)
        op = ai + timedelta(seconds=ai_open_s)
        _mk_chain(db, f"LAT{i}USDT", signal_at=sig, ai_finished=ai, opened_at=op)
    # 异常链：开仓早于信号（时钟回拨）→ 各段负值剔除，样本数仍为 2
    bad_sig = base
    _mk_chain(
        db, "LATBADUSDT", signal_at=bad_sig,
        ai_finished=bad_sig - timedelta(seconds=10),
        opened_at=bad_sig - timedelta(seconds=20),
    )

    resp = review_stats(days=7, db=db)
    assert resp.latency is not None
    assert resp.latency.signal_age_s.samples == 2
    assert resp.latency.signal_age_s.p50_s == pytest.approx(90.0)
    assert resp.latency.signal_age_s.p90_s == pytest.approx(114.0)
    assert resp.latency.ai_to_open_s.samples == 2
    assert resp.latency.ai_to_open_s.p50_s == pytest.approx(45.0)
    assert resp.latency.signal_to_open_s.samples == 2
    assert resp.latency.signal_to_open_s.p50_s == pytest.approx(135.0)
    assert resp.latency.signal_to_open_s.p90_s == pytest.approx(171.0)


# ── B2：htf_degraded 标记 ──────────────────────────────────────────────


def test_detect_marks_degraded_on_4h_fetch_failure():
    """4h 拉取失败：fail-closed 拒绝（gate_4h）同时打 degraded 标记"""
    kl = _trend_1h()

    class _BrokenPool:
        def get_klines(self, symbol, interval, limit, **kwargs):
            if interval == "4h":
                raise RuntimeError("exchange down")
            return kl[:limit]

    rv: dict = {}
    cand, _ = detect_indicator_candidate(_BrokenPool(), "TEST/USDT", kl, reject_out=rv)
    assert cand is None
    assert rv["stage"] == "gate_4h"
    assert rv["degraded"] is True


def test_detect_marks_degraded_on_data_reject(monkeypatch):
    """4h 计算返回 reject_reason=data（数据不足）→ degraded"""
    import app.services.scanner as scanner_mod

    monkeypatch.setattr(scanner_mod, "htf_context", lambda kl: {"reject_reason": "data"})
    rv: dict = {}
    cand, _ = detect_indicator_candidate(
        _FakePool(_trend_1h(), _trend_1h()), "TEST/USDT", _trend_1h(), reject_out=rv
    )
    assert cand is None
    assert rv["stage"] == "gate_4h"
    assert rv["degraded"] is True


def test_detect_not_degraded_on_market_state_reject(monkeypatch):
    """市场状态类拒因（缠绕）不是数据问题，不打 degraded 标记"""
    import app.services.scanner as scanner_mod

    monkeypatch.setattr(scanner_mod, "htf_context", lambda kl: {"reject_reason": "tangle"})
    rv: dict = {}
    cand, _ = detect_indicator_candidate(
        _FakePool(_trend_1h(), _trend_1h()), "TEST/USDT", _trend_1h(), reject_out=rv
    )
    assert cand is None
    assert rv["stage"] == "gate_4h"
    assert "degraded" not in rv


def test_upsert_writes_htf_degraded(db):
    """AI 层 4h K线缺失（真 fail-open 点）→ AIAnalysis.htf_degraded=True；
    缺键按 False（正常分析）"""
    row = _upsert_ai_analysis(
        db, uuid.uuid4(), "B2DEGUSDT",
        {"trade_decision": "suggest", "htf_degraded": True, "analysis": "x"},
        fingerprint="fp-deg",
    )
    row2 = _upsert_ai_analysis(
        db, uuid.uuid4(), "B2OKUSDT",
        {"trade_decision": "suggest", "analysis": "y"}, fingerprint="fp-ok",
    )
    db.commit()
    assert row.htf_degraded is True
    assert row2.htf_degraded is False


# ── B3：复盘按 htf_degraded 分组 ───────────────────────────────────────


def _add_review(db, a, outcome, htf_degraded=False, entry_diag=None,
                mfe_r=None, mae_r=None, signal_type="trigger_breakout"):
    db.add(TradeReview(
        ai_analysis_id=a.id, symbol=a.symbol, direction="long",
        outcome=outcome, tp1_hit=outcome.startswith("win"), bars_to_exit=5,
        signal_type=signal_type, htf_degraded=htf_degraded,
        entry_diag=entry_diag, mfe_r=mfe_r, mae_r=mae_r,
    ))
    db.commit()


def test_review_snapshots_htf_degraded(db, monkeypatch):
    """复盘任务把分析行 htf_degraded 快照到 TradeReview（B3 分组事实源）"""
    monkeypatch.setattr(review_tasks, "SessionLocal", sessionmaker(bind=db.bind))

    class _FakePool:
        def get_klines(self, symbol, interval, window):
            ts = int((datetime.utcnow() - timedelta(hours=26)).timestamp() * 1000)
            return [[ts + h * 3_600_000, 100.0, 101.0, 97.5, 100.0, 1000.0]
                    for h in range(25)]

        def get_klines_range(self, symbol, interval, limit, end_time_ms):
            # 小时对齐：复盘窗口按整点校验完整性（_expected_window_bars）
            last = end_time_ms - end_time_ms % 3_600_000
            return [[last - (limit - 1 - h) * 3_600_000,
                     100.0, 101.0, 97.5, 100.0, 1000.0]
                    for h in range(limit)]

    monkeypatch.setattr(review_tasks, "ExchangePool", _FakePool)
    monkeypatch.setattr(review_tasks, "open_task", lambda **kw: "tid")
    monkeypatch.setattr(review_tasks, "close_task", lambda *a, **kw: None)

    created = datetime.utcnow() - timedelta(hours=25)
    rec = ScanRecord(scan_type="scheduled", created_at=created)
    db.add(rec)
    db.flush()
    sr = ScanResult(
        scan_record_id=rec.id, symbol="B3USDT", signal_type="trigger_breakout",
        current_price=100.0, breakout_pct=1.0, trend_slope=0.0, r_squared=0.5,
        volume=1.5, created_at=created,
    )
    db.add(sr)
    db.flush()
    db.add(AIAnalysis(
        scan_result_id=sr.id, symbol="B3USDT", trade_decision="suggest",
        direction="long", entry_price=100.0, stop_loss=98.0, take_profit_1=104.0,
        recommendation=70, htf_degraded=True, created_at=created,
    ))
    db.commit()

    review_tasks.run_trade_review_task.apply()
    rows = db.query(TradeReview).all()
    assert len(rows) == 1
    assert rows[0].htf_degraded is True


def test_stats_htf_degraded_groups_and_payoff(db):
    """/review/stats 按 htf_degraded 分两组：胜率/期望值/盈亏比。
    正常组 1 胜(RR=2) 1 负 → payoff=2；降级组仅 1 负 → payoff=None（无盈利样本）"""
    from app.api.scan import review_stats

    now = datetime.utcnow() - timedelta(hours=2)
    _, a1, _ = _mk_chain(db, "NORM1USDT", ai_finished=now)
    _add_review(db, a1, "win_tp1", htf_degraded=False)
    _, a2, _ = _mk_chain(db, "NORM2USDT", ai_finished=now)
    _add_review(db, a2, "loss", htf_degraded=False)
    _, a3, _ = _mk_chain(db, "DEG1USDT", ai_finished=now, htf_degraded=True)
    _add_review(db, a3, "loss", htf_degraded=True)

    resp = review_stats(days=30, db=db)
    by = {g.htf_degraded: g for g in resp.htf_degraded_groups}
    assert set(by) == {False, True}
    assert by[False].total == 2
    assert by[False].win_rate == pytest.approx(0.5)
    # RR = |104-100| / |100-98| = 2.0；平均盈利 2R / 平均亏损 1R
    assert by[False].payoff == pytest.approx(2.0)
    assert by[True].total == 1
    assert by[True].loss == 1
    assert by[True].payoff is None


def test_stats_entry_diag_groups_and_censored(db):
    """/review/stats 入场诊断五维分档（17-复盘审计 P0-1）+ censored 计数（P0-2）。

    仅 entry_diag 非空且落档的行进入分档（room_r<0 等区间外值不落档）；
    censored 单列计数、不进胜率分母。"""
    from app.api.scan import review_stats

    now = datetime.utcnow() - timedelta(hours=2)
    _, a1, _ = _mk_chain(db, "DIAG1USDT", ai_finished=now)
    _add_review(db, a1, "win_tp1", entry_diag={
        "pos": 0.7, "stop_atr": 2.5, "room_r": 3.0,
        "planned_rr": 2.5, "quality_score": 85,
    })
    _, a2, _ = _mk_chain(db, "DIAG2USDT", ai_finished=now)
    _add_review(db, a2, "loss", entry_diag={
        "pos": 0.1, "stop_atr": 0.8, "room_r": -0.5,
        "planned_rr": 1.2, "quality_score": 55,
    })
    # 历史行无诊断 → 不进任何分档；censored 单列计数
    _, a3, _ = _mk_chain(db, "DIAG3USDT", ai_finished=now)
    _add_review(db, a3, "censored")

    resp = review_stats(days=30, db=db)
    assert resp.censored == 1
    assert resp.win_rate == pytest.approx(0.5)  # 分母仅 win_tp1 + loss
    assert resp.expired == 0
    by_dim: dict[str, list] = {}
    for g in resp.entry_diag_groups:
        by_dim.setdefault(g.diag_dim, []).append(g)
    assert set(by_dim) == {"pos", "stop_atr", "room_r", "planned_rr", "quality_score"}
    # 两行有诊断；room_r=-0.5 落不进任何档，故该维只统计 1 行
    assert sum(g.total for g in by_dim["room_r"]) == 1
    assert sum(g.total for g in by_dim["pos"]) == 2
    assert {g.diag_bucket for g in by_dim["pos"]} == {"pos<0.25", "pos≥0.6"}


def test_stats_mfe_mae_excursions(db):
    """/review/stats 聚合 MFE/MAE（P1-3 收尾）：整体均值 + 按 outcome 的均值/中位。

    只统计 mfe_r/mae_r 非空行——NULL（历史行/未成交/计划无效）不按 0 计入，
    否则「无数据」会被误读成「没有有利偏移」。"""
    from app.api.scan import review_stats

    now = datetime.utcnow() - timedelta(hours=2)
    _, a1, _ = _mk_chain(db, "EXC1USDT", ai_finished=now)
    _add_review(db, a1, "win_tp1", entry_diag={"pos": 0.7}, mfe_r=2.0, mae_r=-0.2)
    _, a2, _ = _mk_chain(db, "EXC2USDT", ai_finished=now)
    _add_review(db, a2, "loss", entry_diag={"pos": 0.1}, mfe_r=0.5, mae_r=-1.5)
    # 无偏移数据 → 不计入均值/分位，也不单独成组
    _, a3, _ = _mk_chain(db, "EXC3USDT", ai_finished=now)
    _add_review(db, a3, "loss")

    resp = review_stats(days=30, db=db)
    # 整体：(2.0+0.5)/2 与 (-0.2-1.5)/2，NULL 行不参与
    assert resp.mfe_r_avg == pytest.approx(1.25)
    assert resp.mae_r_avg == pytest.approx(-0.85)
    by = {g.outcome: g for g in resp.excursions}
    assert set(by) == {"win_tp1", "loss"}
    assert by["win_tp1"].total_with_data == 1
    assert by["loss"].total_with_data == 1  # 无偏移的 loss 行不计
    assert by["loss"].mfe_r_avg == pytest.approx(0.5)
    assert by["loss"].mae_r_avg == pytest.approx(-1.5)
    # 单样本分位即自身
    assert by["win_tp1"].mfe_r_p50 == pytest.approx(2.0)
    assert by["win_tp1"].mae_r_p50 == pytest.approx(-0.2)
    # 分组表与入场诊断分档同带均值（pos≥0.6 档仅 a1 一行）
    assert resp.groups[0].mfe_r_avg == pytest.approx(1.25)
    pos_hi = [g for g in resp.entry_diag_groups
              if g.diag_dim == "pos" and g.diag_bucket == "pos≥0.6"]
    assert len(pos_hi) == 1
    assert pos_hi[0].mfe_r_avg == pytest.approx(2.0)
    assert pos_hi[0].mae_r_avg == pytest.approx(-0.2)


def test_stats_legacy_trigger_flag(db):
    """/review/stats 标记已下线触发类型（P1-2）：前端据此默认折叠历史信号组"""
    from app.api.scan import review_stats

    now = datetime.utcnow() - timedelta(hours=2)
    _, a1, _ = _mk_chain(db, "LEG1USDT", ai_finished=now)
    _add_review(db, a1, "loss", signal_type="trigger_candlestick")
    _, a2, _ = _mk_chain(db, "LEG2USDT", ai_finished=now)
    _add_review(db, a2, "win_tp1", signal_type="trigger_pullback_confirm")

    resp = review_stats(days=30, db=db)
    by_sig = {g.signal_type: g for g in resp.groups}
    assert by_sig["trigger_candlestick"].legacy is True
    assert by_sig["trigger_pullback_confirm"].legacy is False
