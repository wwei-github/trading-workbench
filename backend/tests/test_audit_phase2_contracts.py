"""审计第二阶段契约测试（2026-10-21）。

覆盖：
1. 评分档位硬校验（方案 A）：连续分归一到 60/70/85，低于 60 不向上放大；
2. 4h 状态快照时刻计算（含幽灵尾根）与开仓前陈旧/缺数拦截；
3. try_open_for_analysis 对降级/陈旧分析的实际拦截与原因落库。

sqlite 内存库，BinanceTrader 替身，不触网、不调 LLM。
"""
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import app.models  # noqa: E402,F401  确保全部模型注册进 Base.metadata
from app.database import Base  # noqa: E402
from app.models.patch import PromptPatch  # noqa: E402
from app.models.scan import AIAnalysis  # noqa: E402
from app.models.system_config import SystemConfig  # noqa: E402
from app.models.trade import TradeRecord  # noqa: E402
from app.services.market_data import (  # noqa: E402
    HTF_INTERVAL_MS,
    htf_open_block_reason,
    htf_snapshot_time,
)
from app.services.patch_service import apply_patch, rollback_patch  # noqa: E402
from app.services.risk_guard import snap_recommendation  # noqa: E402
from app.services.trade_engine import (  # noqa: E402
    _ai_snapshot, _remaining_risk, try_open_for_analysis,
)
from app.tasks.ai_tasks import build_cache_context  # noqa: E402

NOW = datetime(2026, 10, 21, 12, 0, 0)


def _bar(open_dt: datetime, close: float = 100.0) -> list:
    """4h K 线（币安格式：[开盘ms, o, h, l, c, vol, 收盘ms]）"""
    open_ms = int(open_dt.replace(tzinfo=timezone.utc).timestamp() * 1000)
    return [open_ms, close, close + 1, close - 1, close, 10.0, open_ms + HTF_INTERVAL_MS - 1]


# ── 评分档位（T2 方案 A）────────────────────────────────────────────


@pytest.mark.parametrize("raw, tier, kept", [
    (58, 60.0, 58.0),
    (60, 60.0, 60.0),
    (72, 70.0, 72.0),
    (78, 85.0, 78.0),   # 78 更接近 85
    (88, 85.0, 88.0),
    (95, 85.0, 95.0),
    (120, 85.0, 100.0),
    (-5, 0.0, 0.0),
    (None, 0.0, 0.0),
])
def test_snap_recommendation(raw, tier, kept):
    assert snap_recommendation(raw) == (tier, kept)


def test_snap_recommendation_low_score_not_promoted():
    """低分/缺省不得被向上归一成可开单的 60 档"""
    assert snap_recommendation(30) == (30.0, 30.0)
    assert snap_recommendation(49) == (49.0, 49.0)
    assert snap_recommendation(50) == (60.0, 50.0)
    assert snap_recommendation(59) == (60.0, 59.0)


# ── 4h 快照时刻与时效 ────────────────────────────────────────────────


def test_htf_snapshot_time_uses_last_closed_bar():
    """末根未收盘 → 快照时刻 = 倒数第二根收盘（12:00 前最后一根 08:00~12:00）"""
    klines = [
        _bar(datetime(2026, 10, 21, 0, 0)),
        _bar(datetime(2026, 10, 21, 4, 0)),
        _bar(datetime(2026, 10, 21, 8, 0)),
        _bar(datetime(2026, 10, 21, 12, 0)),  # 未收盘尾根
    ]
    assert htf_snapshot_time(klines, NOW) == datetime(2026, 10, 21, 12, 0)


def test_htf_snapshot_time_with_ghost_bar():
    """screen_klines 追加幽灵根后 [:-1] 仍指向同一根已收盘 K 线"""
    klines = [
        _bar(datetime(2026, 10, 21, 4, 0)),
        _bar(datetime(2026, 10, 21, 8, 0)),
        _bar(datetime(2026, 10, 21, 8, 0)),  # 幽灵尾根（复制原末根）
    ]
    assert htf_snapshot_time(klines, NOW) == datetime(2026, 10, 21, 12, 0)


def test_htf_snapshot_time_insufficient_data():
    assert htf_snapshot_time([], NOW) is None
    assert htf_snapshot_time([_bar(datetime(2026, 10, 21, 8, 0))], NOW) is None


def test_htf_open_block_reason_matrix():
    assert "缺失" in htf_open_block_reason(True, None, NOW)
    assert htf_open_block_reason(False, None, NOW) is None  # 旧行兼容
    assert htf_open_block_reason(
        False, NOW - timedelta(hours=4, minutes=10), NOW
    ) is None
    assert "陈旧" in htf_open_block_reason(
        False, NOW - timedelta(hours=5), NOW
    )


# ── 执行层拦截 ──────────────────────────────────────────────────────


@pytest.fixture()
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = Session(bind=engine)
    session.add(SystemConfig(id=1))
    session.commit()
    yield session
    session.close()


class _Trader:
    account_key = "testnet-1"
    testnet = True

    def positions(self):
        return {}

    def wallet_balance(self):
        return {"wallet": 1000.0, "available": 900.0}


def _analysis(db, **kw) -> AIAnalysis:
    a = AIAnalysis(
        symbol="TESTUSDT", trade_decision="suggest", direction="long",
        entry_price=100.0, stop_loss=98.0, take_profit_1=104.0,
        recommendation=70, created_at=datetime.utcnow(), **kw,
    )
    db.add(a)
    db.commit()
    return a


def test_open_blocked_when_htf_degraded(db):
    a = _analysis(db, htf_degraded=True)
    assert try_open_for_analysis(db, _Trader(), a.id) == "blocked"
    db.refresh(a)
    assert "4h 数据缺失" in (a.open_block_reason or "")


def test_open_blocked_when_htf_stale(db):
    a = _analysis(db, htf_as_of=datetime.utcnow() - timedelta(hours=5))
    assert try_open_for_analysis(db, _Trader(), a.id) == "blocked"
    db.refresh(a)
    assert "陈旧" in (a.open_block_reason or "")


def test_legacy_analysis_without_htf_as_of_not_blocked_by_htf(db):
    """旧行 htf_as_of=NULL 不因 4h 闸被拦（继续走后续闸门即可）"""
    a = _analysis(db)
    # 后续闸门链缺交易替身能力会抛错或返回 blocked，但原因不应是 4h
    try:
        try_open_for_analysis(db, _Trader(), a.id)
    except Exception:
        pass
    db.refresh(a)
    assert "4h 状态" not in (a.open_block_reason or "")
    assert "4h 数据" not in (a.open_block_reason or "")


# ── 风险口径拆分（T3）────────────────────────────────────────────────


def _risk_rec(**kw):
    base = dict(
        symbol="TSTUSDT", direction="long", status="OPENED",
        entry_price=100.0, stop_loss=98.0, qty=1.0, raw={},
    )
    base.update(kw)
    return TradeRecord(**base)


def test_remaining_risk_scales_with_tranches():
    """剩余风险随 TP1/TP2 分批量缩减（1×2U → 0.4×2U → 0.2×2U）"""
    raw = {"qty_tp1": 0.6, "qty_tp2": 0.2}
    rec = _risk_rec(raw=raw)
    assert _remaining_risk(rec) == pytest.approx(2.0)
    rec.status = "TP1_HIT"
    assert _remaining_risk(rec) == pytest.approx(0.8)
    rec.status = "TP2_HIT"
    assert _remaining_risk(rec) == pytest.approx(0.4)


def test_remaining_risk_zero_when_stop_moved_past_entry():
    """保本/跟进止损移到盈利侧 → 当前亏损风险为 0（已锁定利润不算风险）"""
    rec = _risk_rec(status="TP1_HIT", stop_loss=100.1, raw={})
    assert _remaining_risk(rec) == 0.0
    rec.direction = "short"
    rec.stop_loss = 99.9
    assert _remaining_risk(rec) == 0.0


def test_remaining_risk_uses_exchange_qty_for_manual_reduce():
    """手动减仓：以交易所实际剩余量（amt）为准，而非开仓原始数量"""
    rec = _risk_rec()
    assert _remaining_risk(rec, qty=0.3) == pytest.approx(0.6)


def test_risk_unit_prefers_initial_risk():
    """1R 基准：initial_risk 优先，旧行回落 risk_amount"""
    assert _risk_rec(initial_risk=12.0, risk_amount=10.0).risk_unit == pytest.approx(12.0)
    assert _risk_rec(initial_risk=None, risk_amount=10.0).risk_unit == pytest.approx(10.0)
    assert _risk_rec(initial_risk=None, risk_amount=None).risk_unit == 0.0


# ── 统一决策版本（T5）────────────────────────────────────────────────


def test_param_patch_bumps_version_and_rollback_restores_source_value(db):
    """参数补丁：合入记录来源值并递增版本；回滚恢复旧自定义值而非 NULL"""
    cfg = db.get(SystemConfig, 1)
    cfg.max_stop_pct = 0.02
    cfg.decision_version = 4
    db.commit()

    patch = PromptPatch(
        target_tag="追高", kind="param", param_key="max_stop_pct",
        param_value="0.03", new_text="收紧止损宽度",
    )
    db.add(patch)
    db.commit()

    apply_patch(db, patch.id)
    assert int(cfg.decision_version) == 5
    assert float(cfg.max_stop_pct) == pytest.approx(0.03)
    assert patch.before_value_json == {
        "decision_version": 4, "param_key": "max_stop_pct", "param_value": 0.02,
    }

    rollback_patch(db, patch.id)
    assert float(cfg.max_stop_pct) == pytest.approx(0.02)  # 来源值，不是 NULL
    assert int(cfg.decision_version) == 6


def test_prompt_patch_rollback_restores_prompt_and_bumps_version(db):
    cfg = db.get(SystemConfig, 1)
    cfg.strategy_prompt = "原始策略"
    db.commit()
    patch = PromptPatch(target_tag="追高", kind="prompt", new_text="新增覆盖规则")
    db.add(patch)
    db.commit()

    apply_patch(db, patch.id)
    assert "新增覆盖规则" in cfg.strategy_prompt
    assert int(cfg.decision_version) == 1

    rollback_patch(db, patch.id)
    assert cfg.strategy_prompt == "原始策略"
    assert int(cfg.decision_version) == 2


def test_legacy_param_patch_without_before_snapshot_falls_back_to_default(db):
    """旧补丁无合入前快照：回滚保持原行为（回落 NULL/默认）"""
    cfg = db.get(SystemConfig, 1)
    cfg.max_stop_pct = 0.03
    patch = PromptPatch(
        target_tag="追高", kind="param", param_key="max_stop_pct",
        param_value="0.03", new_text="旧补丁", status="active",
    )
    db.add(patch)
    db.commit()

    rollback_patch(db, patch.id)
    assert cfg.max_stop_pct is None


def test_cache_context_tracks_decision_version():
    """缓存指纹随决策版本变化：跨版本不得复用旧结论"""
    cfg = SystemConfig(id=1, decision_version=3)
    ctx = build_cache_context(cfg, [], [], "tactics", 0)
    assert ctx["decision_version"] == 3
    cfg.decision_version = 4
    assert build_cache_context(cfg, [], [], "tactics", 0) != ctx


def test_trade_snapshot_carries_decision_version(db):
    a = _analysis(db, decision_version=7)
    assert _ai_snapshot(a)["decision_version"] == 7
