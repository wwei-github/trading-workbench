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


def _rec(rr, pnl=1.0, signal_type=None, pattern=None):
    now = datetime.utcnow()
    return TradeRecord(
        symbol="GTCUSDT", direction="long", status="CLOSED",
        exit_reason="sl", testnet=False, account_key="live-1",
        opened_at=now - timedelta(hours=3), closed_at=now - timedelta(hours=1),
        realized_pnl=pnl, signal_type=signal_type, pattern=pattern,
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


def _closed(hours_ago: float, pnl: float, risk: float | None = None,
            planned: float | None = None):
    """指定 closed_at 的已平仓单；risk=initial_risk（1R 口径），planned=计划风险预算"""
    now = datetime.utcnow()
    return TradeRecord(
        symbol="BTCUSDT", direction="long", status="CLOSED", exit_reason="sl",
        testnet=False, account_key="live-1",
        opened_at=now - timedelta(hours=hours_ago + 1),
        closed_at=now - timedelta(hours=hours_ago),
        realized_pnl=pnl, initial_risk=risk, planned_risk=planned,
        ai_snapshot={"risk_reward_ratio": 2.0},
    )


def test_max_drawdown_and_risk_excess(db):
    """审计 7.2 主指标：闭单累计曲线最大回撤（USDT/R）与风险超额。
    权益 +10 → -11 → -20：USDT 回撤 = 10 - (-21) = 31；R 回撤 = 1 - (-2.1) = 3.1。
    风险超额按计划预算判定：-11 在 12U 预算内不算超额（尽管 1.1R），
    -20 超 10U 预算 10U；单笔最大亏损 2.0R。
    """
    db.add_all([
        _closed(5, 10.0, risk=10.0, planned=10.0),
        _closed(3, -11.0, risk=10.0, planned=12.0),
        _closed(1, -20.0, risk=10.0, planned=10.0),
    ])
    db.commit()
    ov = build_actual_review(db, days=7)["overview"]
    assert ov["max_drawdown"] == pytest.approx(31.0)
    assert ov["max_drawdown_r"] == pytest.approx(3.1)
    assert ov["risk_excess_count"] == 1
    assert ov["risk_excess_total"] == pytest.approx(10.0)
    assert ov["max_loss_r"] == pytest.approx(2.0)


def test_risk_metrics_skip_rows_without_1r(db):
    """旧行无 1R 时：USDT 回撤照常，R 回撤/风险超额留空，不伪造确定性"""
    db.add_all([_closed(3, 8.0), _closed(1, -4.0)])
    db.commit()
    ov = build_actual_review(db, days=7)["overview"]
    assert ov["max_drawdown"] == pytest.approx(4.0)
    assert ov["max_drawdown_r"] is None
    assert ov["risk_excess_count"] == 0
    assert ov["risk_excess_total"] == pytest.approx(0.0)
    assert ov["max_loss_r"] is None


def _dim_groups(result: dict, dim: str) -> dict:
    return {g["key"]: g for g in result["groups"] if g["dim"] == dim}


def test_golden_pattern_only_active_eight(db):
    """P1-1：金K形态只列现役 8 种（0 笔也占行）；已下线 4 种的历史行
    归入「未记录」，不再各占一行恒 0"""
    from app.services.actual_review import GOLDEN_PATTERN_ORDER
    from app.services.strategy.candlestick import PATTERN_LABEL_MAP

    db.add_all([
        _rec(2.0, signal_type="trigger_pullback_confirm", pattern="hammer"),
        # 已下线形态（2026-10-09 白名单收缩）→ 并入未记录
        _rec(2.0, signal_type="trigger_pullback_confirm", pattern="bullish_harami"),
        _rec(2.0, signal_type="trigger_pullback_confirm"),
    ])
    db.commit()
    pats = _dim_groups(build_actual_review(db, days=7), "金K形态")

    assert len(GOLDEN_PATTERN_ORDER) == 8
    assert {PATTERN_LABEL_MAP.get(p, p) for p in GOLDEN_PATTERN_ORDER} <= set(pats)
    assert pats["未记录"]["count"] == 2  # NULL + 已下线形态
    assert PATTERN_LABEL_MAP["bullish_harami"] not in pats


def test_signal_family_legacy_flag(db):
    """P1-2：全为已下线触发类型的信号族标 legacy（前端折叠）；
    混合族（含仍活跃类型）不标，避免把新行一起藏掉"""
    db.add_all([
        _rec(2.0, signal_type="trigger_candlestick"),
        _rec(2.0, signal_type="trigger_pullback_confirm"),
        # 旧管线别名与活跃类型同族 → 混合族不折叠
        _rec(2.0, signal_type="uptrend_pullback"),
    ])
    db.commit()
    fams = _dim_groups(build_actual_review(db, days=7), "信号类型")

    assert fams["同向形态"]["legacy"] is True
    assert fams["回踩金K确认"]["legacy"] is False


def test_htf_aligned_split_bull_bear(db):
    """P2-2：多头/空头排列拆成两桶（合并会把两侧盈亏互相抵消）"""
    b = _rec(2.0, signal_type="trigger_pullback_confirm")
    b.ai_snapshot = {"risk_reward_ratio": 2.0, "htf_phase": "aligned_bull"}
    s = _rec(2.0, signal_type="trigger_pullback_confirm")
    s.ai_snapshot = {"risk_reward_ratio": 2.0, "htf_phase": "aligned_bear"}
    db.add_all([b, s])
    db.commit()

    buckets = _dim_groups(build_actual_review(db, days=7), "4h趋势期")
    assert buckets["排列·多"]["count"] == 1
    assert buckets["排列·空"]["count"] == 1
    assert "排列" not in buckets  # 旧的合并桶不再输出
