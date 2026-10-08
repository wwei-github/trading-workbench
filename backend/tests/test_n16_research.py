"""N-16 度量先行测试（docs/16 §0.2 #3）：反抽深度纯函数 + 分桶 + 聚合主入口。"""
import uuid
from datetime import datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.models.ledger import VirtualLedger
from app.models.scan import ScanResult
from app.services import n16_research as nr
from app.services.n16_research import (
    breakout_bucket, retrace_bucket, retracement_ratio, run_n16_research,
)

_T0 = datetime(2026, 10, 8, 6, 0)
HOUR_MS = 3_600_000


def _kls(specs, base_ms):
    """specs=[(high, low, close?)] → klines（O=hl 中点，t=base+i*1h）。"""
    out = []
    for i, sp in enumerate(specs):
        hi, lo = float(sp[0]), float(sp[1])
        c = float(sp[2]) if len(sp) > 2 else (hi + lo) / 2
        out.append([base_ms + i * HOUR_MS, (hi + lo) / 2, hi, lo, c, 100.0])
    return out


# ── retracement_ratio 纯函数 ──────────────────────────────────────────────

def _leg(up=True, n=nr.LEG_BARS):
    """前段腿：线性推 100→110（up）或 110→100（down）。"""
    specs = []
    for i in range(n):
        c = 100 + i * (10 / (n - 1)) if up else 110 - i * (10 / (n - 1))
        specs.append((c + 0.5, c - 0.5, c))
    return specs


def test_ratio_long_standard_pullback():
    # 腿 100→110（H110.5/L99.5，振幅11），回踩至 106.5 → (110.5-106.5)/11
    specs = _leg(up=True) + [(110.2, 107.0, 107.5), (108.0, 106.5, 107.0)]
    r = retracement_ratio(_kls(specs, 0), "long")
    assert r == pytest.approx(4.0 / 11.0, abs=1e-9)
    assert retrace_bucket(r) == "23.6~38.2%（浅）"


def test_ratio_short_mirror_bounce():
    # 腿 110→100，反弹高点 103 → (103-99.5)/11
    specs = _leg(up=False) + [(103.0, 99.8, 100.2), (102.5, 99.5, 100.0)]
    r = retracement_ratio(_kls(specs, 0), "short")
    assert r == pytest.approx(3.5 / 11.0, abs=1e-9)
    assert retrace_bucket(r) == "23.6~38.2%（浅）"


def test_ratio_extreme_shallow_is_chase():
    # CCUSDT 命题：反弹几乎为零（长上影前夜）→ 极浅桶
    specs = _leg(up=False) + [(100.6, 99.7, 100.2), (100.9, 99.9, 100.1)]
    assert retrace_bucket(retracement_ratio(_kls(specs, 0), "short")) == "≤23.6%（极浅·近追价）"


def test_ratio_deep_pullback_over_100pct():
    # 回踩跌破腿起点 → ratio>1，落 ≥61.8% 桶（不截断，保真）
    specs = _leg(up=True) + [(104.0, 98.0, 99.0), (101.0, 97.0, 100.0)]
    r = retracement_ratio(_kls(specs, 0), "long")
    assert r > 1.0
    assert retrace_bucket(r) == "≥61.8%（深）"


def test_ratio_insufficient_bars_and_flat_leg():
    specs = _leg(up=True, n=20)  # 共 22 根 < 26
    assert retracement_ratio(_kls(specs, 0), "long") is None
    flat = [(100.0, 100.0)] * (nr.LEG_BARS + 2)  # H==L → 腿振幅 0
    assert retracement_ratio(_kls(flat, 0), "long") is None


def test_breakout_bucket_boundaries():
    assert breakout_bucket(None) == "无记录"
    assert breakout_bucket(0.3) == "≤0.5%"
    assert breakout_bucket(0.7) == "0.5~1%"
    assert breakout_bucket(1.5) == "1~1.5%"     # 触发带上沿含 1.5
    assert breakout_bucket(2.0) == ">1.5%"      # 显著追价


# ── run_n16_research 聚合 ─────────────────────────────────────────────────

@pytest.fixture()
def db():
    engine = create_engine("sqlite:///:memory:")
    from app.database import Base
    Base.metadata.create_all(
        bind=engine, tables=[VirtualLedger.__table__, ScanResult.__table__])
    return Session(bind=engine)


def _ledger_row(db, **kw):
    defaults = dict(
        symbol="BTCUSDT", direction="long", source="live",
        signal_type="trigger_pullback_confirm",
        entry_price=100.0, stop_price=99.25, tp1_price=101.125,
        tp2_price=102.5, r_unit=0.75, entry_bar_open=_T0,
        status="DONE", outcome="win_tp1", r=1.4,
    )
    defaults.update(kw)
    row = VirtualLedger(**defaults)
    db.add(row)
    db.commit()
    return row


def test_run_n16_research_aggregates(db, monkeypatch):
    sr = ScanResult(id=uuid.uuid4(), scan_record_id=uuid.uuid4(), symbol="BTCUSDT",
                    signal_type="trigger_pullback_confirm", current_price=100,
                    breakout_pct=0.8, trend_slope=0.1, r_squared=0.9)
    db.add(sr)
    db.commit()
    r1 = _ledger_row(db, scan_result_id=sr.id)                       # long 浅回踩
    r2 = _ledger_row(db, symbol="ETHUSDT", direction="short",
                     outcome="loss", r=-1.0)                          # short 无关联
    _ledger_row(db, signal_type="trigger_breakout",
                outcome="loss", r=-0.5)                               # 不适用反抽维度

    specs = _leg(up=True) + [(110.2, 107.0, 107.5), (108.0, 106.5, 107.0)]
    base_ms = int((_T0 - datetime(1970, 1, 1)).total_seconds() * 1000) - 25 * HOUR_MS
    # 26 根以信号K结尾：最后一根 open == entry_bar_open（_T0）

    def fake_fetcher(d, pool, symbol, interval, start_ms, end_ms):
        if symbol == "BTCUSDT":
            return _kls(specs, base_ms)
        return _kls(specs, base_ms + HOUR_MS)  # ETHUSDT 错位 → 无信号K → 无法计算

    out = run_n16_research(db, days=14, fetcher=fake_fetcher)
    assert out["decided_total"] == 3
    bk = {b["bucket"]: b for b in out["breakout"]["buckets"]}
    assert bk["0.5~1%"]["decided"] == 1 and bk["0.5~1%"]["exp_r"] == 1.4
    assert bk["无记录"]["decided"] == 2 and bk["无记录"]["exp_r"] == -0.75
    rt = {b["bucket"]: b for b in out["retracement"]["buckets"]}
    assert rt["23.6~38.2%（浅）"]["decided"] == 1          # 仅 BTCUSDT long
    assert rt["无法计算"]["decided"] == 1                   # ETHUSDT K线错位
    assert out["retracement"]["note"].endswith("其余 1 笔不适用")


def test_run_n16_research_empty(db):
    out = run_n16_research(db, days=7, fetcher=lambda *a: [])
    assert out["decided_total"] == 0
    assert out["breakout"]["buckets"] == [] and out["retracement"]["buckets"] == []
