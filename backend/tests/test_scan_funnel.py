# -*- coding: utf-8 -*-
"""筛选漏斗回归（2026-10-11）：被拒币种 + 卡住关卡的结构化收集与落库。

detect_indicator_candidate 各关卡经 reject_out 旁路写 {"stage": <关卡代码>}
（与 event_out 同模式，零破坏旧调用点）；主扫描 run 收口组装 funnel_json
随 ScanRecord 落库，前端「筛选漏斗」Tab 展示。关卡代码表：
gate_4h / votes / direction_flip / structure_1h / range_score /
no_trigger / trigger_whitelist + 预过滤 liquidity/new_coin/
risk_tier/blacklist（预过滤在 run_scan 内联，逻辑为谓词平移，由落库与
detect 层测试间接覆盖；untradable 预过滤 2026-10-11 退役——不再扫描期
过滤，开单时逐网校验 + 飞书提示，旧轮次漏斗数据仍可见该关卡）。

判定链测试打法：monkeypatch 打桩各闸门/票数/触发（test_scan_filters 白名单
测试同款），使命中点唯一落在被测关卡；另用真实夹具（ranging 4h）抽查
gate_4h 全管线路径。进待确认池（pending upsert）不算被拒——reject_out 不写。
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
from app.models.scan import ScanRecord  # noqa: E402
from app.services import scanner as scanner_mod  # noqa: E402
from app.services.scanner import Scanner, detect_indicator_candidate  # noqa: E402

_PASS_VOTES = {"bull": 5, "bear": 0, "bull_n": 6, "bear_n": 6,
               "net": 5, "voted": 6, "results": {}}
_FAIL_VOTES = {"bull": 1, "bear": 0, "bull_n": 2, "bear_n": 6,
               "net": 1, "voted": 3, "results": {}}
_SHORT_VOTES = {"bull": 0, "bear": 5, "bull_n": 6, "bear_n": 6,
                "net": -5, "voted": 6, "results": {}}


class FakePool:
    """4h 返回非空占位（闸门块执行）或空（跳过闸门块），1h 忽略"""

    def __init__(self, htf_nonempty=True):
        self.htf = [[0, 100.0, 101.0, 99.0, 100.0, 10.0, 10.0]] if htf_nonempty else []

    def get_klines(self, symbol, interval, limit, **kwargs):
        return self.htf[:limit]


def _detect(reject_out, klines=None, *, votes=_PASS_VOTES, gate=("long", {}), htf_nonempty=True,
            structure=({}, {}), rs=0, trigger=None, flt=None, pending=None):
    """打桩装配一次 detect：各桩默认全放行，命中点由参数控制"""
    import app.services.scanner as sm

    orig = {
        "analyze_ema": sm.analyze_ema, "classify_volume": sm.classify_volume,
        "compute_indicator_votes": sm.compute_indicator_votes,
        "gate_4h": sm.gate_4h, "structure_1h": sm.structure_1h,
        "range_score": sm.range_score, "detect_trigger": sm.detect_trigger,
    }
    sm.analyze_ema = lambda k: None
    sm.classify_volume = lambda k: (1.0, "平量")
    sm.compute_indicator_votes = lambda k, h=None, htf_bg=None: votes
    sm.gate_4h = lambda k, cfg=None: gate
    sm.structure_1h = lambda k, d, cfg=None: structure
    sm.range_score = lambda k, gate_ev=None: (rs, [])
    sm.detect_trigger = lambda *a, **k: trigger
    try:
        return detect_indicator_candidate(
            FakePool(htf_nonempty), "XUSDT", klines if klines is not None else [],
            flt=flt, pending=pending, reject_out=reject_out)
    finally:
        for k, v in orig.items():
            setattr(sm, k, v)


def test_reject_gate_4h():
    """gate_4h 主动否决（返回 None）→ stage=gate_4h"""
    rv: dict = {}
    cand, _ = _detect(rv, gate=None)
    assert cand is None and rv == {"stage": "gate_4h"}


def test_reject_votes():
    """票数不足/中性（voted<5 或 |net|<3）→ stage=votes"""
    rv: dict = {}
    cand, _ = _detect(rv, votes=_FAIL_VOTES, htf_nonempty=False)
    assert cand is None and rv == {"stage": "votes"}


def test_reject_votes_detail_attached():
    """votes 拒绝且有票 → reject_out 附 detail（2026-10-11 拍板：判定说明展示全部票）"""
    rv: dict = {}
    votes = {"bull": ["MACD"], "bear": ["KDJ"], "bull_n": 1, "bear_n": 1,
             "net": 0, "voted": 3,
             "results": [
                 ("MACD", {"dir": 1, "state_label": "多头", "detail": "金叉"}),
                 ("KDJ", {"dir": -1, "state_label": "空头", "detail": "死叉"}),
                 ("4h背景", {"dir": 0, "state_label": "缠绕"}),
                 ("BBW", {"dir": None}),
             ]}
    cand, _ = _detect(rv, votes=votes, htf_nonempty=False)
    assert cand is None and rv["stage"] == "votes"
    assert rv["detail"] == ("MACD ▲ 多头（金叉）；KDJ ▼ 空头（死叉）；"
                            "4h背景 — 缠绕；BBW —")


def test_votes_detail_render():
    """_votes_detail 渲染：dir 缺失/异常降级箭头 —，无 label/detail 只出短名"""
    from app.services.scanner import _votes_detail
    votes = {"results": [
        ("EMA排列", {"dir": 1, "state_label": "多头排列", "detail": "21>55>144"}),
        ("RSI", {"dir": "bad"}),
        ("X", {}),
    ]}
    assert _votes_detail(votes) == "EMA排列 ▲ 多头排列（21>55>144）；RSI —；X —"


def test_reject_direction_flip():
    """指标多数决空头 + 闸门多头 → stage=direction_flip（4h 方向硬门）"""
    rv: dict = {}
    cand, _ = _detect(rv, votes=_SHORT_VOTES, gate=("long", {}))
    assert cand is None and rv == {"stage": "direction_flip"}


def test_reject_structure_1h():
    """1h 三线排列+发散度不成立 → stage=structure_1h"""
    rv: dict = {}
    cand, _ = _detect(rv, structure=None, htf_nonempty=False)
    assert cand is None and rv == {"stage": "structure_1h"}


def test_reject_range_score():
    """震荡评分 ≥3 → stage=range_score"""
    rv: dict = {}
    cand, _ = _detect(rv, rs=3, htf_nonempty=False)
    assert cand is None and rv == {"stage": "range_score"}


def test_reject_no_trigger():
    """全门通过但 detect_trigger 无触发且无 pending 处置 → stage=no_trigger"""
    rv: dict = {}
    cand, _ = _detect(rv, trigger=(None, None), htf_nonempty=False)
    assert cand is None and rv == {"stage": "no_trigger"}


def test_reject_trigger_whitelist():
    """触发类型在白名单外且无 pending upsert → stage=trigger_whitelist"""
    from app.services.scan_filters import ALL_TRIGGERS
    from app.services.strategy.trigger import PULLBACK_CONFIRM, ScanFilters

    # 非空 1h 序列（白名单内分支会构造候选，current_price 取 klines[-1][4]）
    kl = [[1_700_000_000_000 + i * 3_600_000, 100.0, 101.0, 99.0, 100.0, 10.0, 10.0]
          for i in range(12)]
    flt = ScanFilters(
        min_volume_24h=1, breakout_channel_bars=20, breakout_threshold=0.002,
        breakout_vol_ratio=1.2, breakout_max_pct=0, candle_max_ema21_dist_pct=0,
        ai_max_per_scan=10, allowed_triggers=(PULLBACK_CONFIRM,),
    )
    breakout = {"type": "trigger_breakout", "direction": 1, "position": "resistance",
                "pattern": None, "breakout_pct": 0.5, "reason": "r"}
    rv: dict = {}
    cand, _ = _detect(rv, kl, trigger=(breakout, None), flt=flt, htf_nonempty=False)
    assert cand is None and rv == {"stage": "trigger_whitelist"}
    # 白名单含突破 → 候选照常产生，reject_out 不写
    flt2 = ScanFilters(
        min_volume_24h=1, breakout_channel_bars=20, breakout_threshold=0.002,
        breakout_vol_ratio=1.2, breakout_max_pct=0, candle_max_ema21_dist_pct=0,
        ai_max_per_scan=10, allowed_triggers=ALL_TRIGGERS,
    )
    rv2: dict = {}
    cand2, _ = _detect(rv2, kl, trigger=(breakout, None), flt=flt2, htf_nonempty=False)
    assert cand2 is not None and rv2 == {}


def test_pending_upsert_not_rejected():
    """触碰无金K进待确认池（upsert）不算被拒：reject_out 不写、pending_op 照常返回"""
    rv: dict = {}
    cand, op = _detect(rv, trigger=(None, {"op": "upsert", "direction": 1}),
                       htf_nonempty=False)
    assert cand is None and op["op"] == "upsert" and rv == {}


def test_reject_real_pipeline_gate_4h():
    """真实管线抽查（非打桩）：4h 走平 → gate_4h 否决 + reject 旁路一致
    （test_htf_ranging_filter 同款夹具形态：220 根窄幅 4h + 300 根上行 1h）"""
    kl_1h = []
    ts = 1_700_000_000_000
    price = 100.0
    for i in range(300):
        o = price
        price += 0.35
        kl_1h.append([ts + i * 3_600_000, o, price + 0.15, price - 0.25, price, 100.0, 100.0])
    kl_4h = []
    for i in range(220):
        o = 100.0 + 0.3 * ((i % 8) - 4) / 4
        c = 100.0 + 0.3 * (((i + 4) % 8) - 4) / 4
        kl_4h.append([ts + i * 4 * 3_600_000, o, max(o, c) + 0.1, min(o, c) - 0.1, c, 80.0, 80.0])

    class Pool:
        def __init__(self, m):
            self._m = m

        def get_klines(self, symbol, interval, limit, **kwargs):
            return self._m[interval][:limit]

    rv: dict = {}
    cand, _ = detect_indicator_candidate(
        Pool({"1h": kl_1h, "4h": kl_4h}), "TEST/USDT", kl_1h, reject_out=rv)
    assert cand is None and rv == {"stage": "gate_4h"}


# ── 落库：_update_scan_status 挂 funnel_json ─────────────────────────


@pytest.fixture()
def db(monkeypatch):
    # StaticPool + check_same_thread=False：_update_scan_status 用独立
    # SessionLocal 连接，需共享同一内存库（test_slots_full_by_net 同款惯例）
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine, tables=[ScanRecord.__table__])
    monkeypatch.setattr(scanner_mod, "SessionLocal", lambda: Session(bind=engine))
    session = Session(bind=engine)
    yield session
    session.close()


def test_update_scan_status_stores_funnel(db):
    """扫描完成时 funnel_json 落库；funnel=None（异常轮次）不动既有值"""
    rec = ScanRecord(id=uuid.uuid4(), scan_type="scheduled", status="running")
    db.add(rec)
    db.commit()

    funnel = {
        "universe": 480, "scanned": 185, "errors": 2, "candidates": 5,
        "pending_pool": 12,
        "stages": {"gate_4h": 120, "votes": 30, "no_trigger": 8},
        "rejects": {"BTCUSDT": "gate_4h", "ETHUSDT": "votes"},
    }
    Scanner._update_scan_status(
        rec.id, "completed", 5, 2, 480, funnel=funnel)
    db.expire_all()
    rec2 = db.get(ScanRecord, rec.id)
    assert rec2.status == "completed"
    assert rec2.funnel_json == funnel
    assert rec2.funnel_json["rejects"]["BTCUSDT"] == "gate_4h"

    # funnel=None（异常路径）不覆盖已存漏斗
    Scanner._update_scan_status(rec.id, "failed", 0, 480, 480, funnel=None)
    db.expire_all()
    assert db.get(ScanRecord, rec.id).funnel_json == funnel


def test_update_scan_status_funnel_absent_param(db):
    """不传 funnel（旧调用点兼容）：行为与原来一致，funnel_json 保持 NULL"""
    rec = ScanRecord(id=uuid.uuid4(), scan_type="manual", status="running")
    db.add(rec)
    db.commit()
    Scanner._update_scan_status(rec.id, "completed", 0, 0, 100)
    db.expire_all()
    rec2 = db.get(ScanRecord, rec.id)
    assert rec2.status == "completed" and rec2.funnel_json is None
