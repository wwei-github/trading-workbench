# -*- coding: utf-8 -*-
"""影子打标第二批次（docs/14 §十，2026-10-05）：macd_div + oi_conf 单测。

覆盖：
1. macd_div（CARD-04）：diverge_bear/diverge_bull/confirm 三桶构造 K 线、
   数据不足 None（MACD 未成形 / 柱序列不足 24 根）、输出 JSON 可序列化
2. oi_conf（CARD-02）：ΔOI confirm/diverge 分桶、无 24h 前值只存快照不打标、
   前值超出 18~30h 容差窗口不打标、openInterest 拉取异常全吞、
   uq_shadow_mark 幂等覆盖、enabled 代码开关
3. ai_tasks 配额段接线：仅定时扫描批量路径打标（手动扫描/手动单币不进观察总体）
4. 主线隔离：扫描循环对 oi_conf 占位 compute 恒得 None；oi_conf 打标失败不抛

sqlite 内存库 + 替身 client，不触网。
"""
import json
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import app.models  # noqa: E402,F401  确保全部模型注册进 Base.metadata
from app.config import settings  # noqa: E402
from app.database import Base  # noqa: E402
from app.models.scan import ScanRecord, ScanResult  # noqa: E402
from app.models.shadow import ShadowMark  # noqa: E402
from app.models.system_config import SystemConfig  # noqa: E402
from app.services import shadow as shadow_mod  # noqa: E402
from app.services.shadow import (  # noqa: E402
    SHADOW_FEATURES,
    compute_macd_div,
    tag_oi_conf,
)
from app.services.strategy.technical import analyze_macd, analyze_macd_div  # noqa: E402


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    """固定配置基线 + 清 OI 快照环（模块级进程内状态跨用例污染）"""
    monkeypatch.setattr(settings, "AI_API_KEY", "test-key")
    shadow_mod._OI_RING.clear()
    yield
    shadow_mod._OI_RING.clear()


# ── 1. macd_div（CARD-04）─────────────────────────────────────────────


def _mk_klines(closes):
    """K 线替身：high=close+0.5、low=close−0.5，末根未收盘占位（被剔除）"""
    body = [[0, c, c + 0.5, c - 0.5, c, 100.0] for c in closes]
    body.append([0, closes[-1], closes[-1], closes[-1], closes[-1], 0.0])
    return body


def _div_bear_closes():
    """前半（末24根的前12根）强动量冲高回落再冲高，后半微斜率爬升创新高：
    价创 24 根内新高而 MACD 柱峰值衰减 → diverge_bear"""
    closes = [100.0 + (0.2 if i % 2 else 0.0) for i in range(36)]  # 平铺铺垫
    closes += [100 + 2 * (i + 1) for i in range(8)]                # 强拉升 100→116
    closes += [116 - 2 * (i + 1) for i in range(3)]                # 回落 →110
    closes += [112.0]                                              # 前半末根（前半 high 峰 116.5）
    closes += [112 + 0.45 * (i + 1) for i in range(12)]            # 后半缓升 112→117.4（新高）
    return closes


def _div_bull_closes():
    """镜像：前半强动量砸低反弹再砸低，后半微斜率阴跌创新低 → diverge_bull"""
    closes = [100.0 - (0.2 if i % 2 else 0.0) for i in range(36)]
    closes += [100 - 2 * (i + 1) for i in range(8)]                # 强杀跌 100→84
    closes += [84 + 2 * (i + 1) for i in range(3)]                 # 反弹 →90
    closes += [88.0]                                               # 前半末根（前半 low 谷 83.5）
    closes += [88 - 0.45 * (i + 1) for i in range(12)]             # 后半阴跌 88→82.6（新低）
    return closes


def test_macd_div_buckets():
    bear = compute_macd_div(_mk_klines(_div_bear_closes()))
    assert bear is not None and bear["bucket"] == "diverge_bear"
    assert bear["price_high_chg_pct"] > 0          # 后半确创新高
    assert bear["hist_peak_delta"] < 0             # 柱峰值衰减

    bull = compute_macd_div(_mk_klines(_div_bull_closes()))
    assert bull is not None and bull["bucket"] == "diverge_bull"
    assert bull["price_low_chg_pct"] < 0           # 后半确创新低
    assert bull["hist_trough_delta"] > 0           # 柱低谷抬升

    flat = compute_macd_div(_mk_klines([100.0] * 60))
    assert flat is not None and flat["bucket"] == "confirm"  # 无新高新低


def test_macd_div_insufficient_none():
    assert compute_macd_div(_mk_klines([100.0] * 30)) is None  # MACD 未成形（<45）
    assert compute_macd_div(_mk_klines([100.0] * 50)) is None  # 柱序列 16 < 24
    assert compute_macd_div([]) is None


def test_macd_div_refactor_keeps_analyze_macd():
    """提取 _macd_series 重构后 analyze_macd 输出不变（回归）"""
    kl = _mk_klines(_div_bear_closes())
    r = analyze_macd(kl)
    assert r is not None and {"dif", "dea", "hist", "state", "dir"} <= set(r)


def test_macd_div_registry_entry():
    """注册表条目 + 输出可直接进 value_json（json.dumps 不炸）"""
    f = SHADOW_FEATURES["macd_div"]
    assert f.card == "CARD-04" and f.min_sample == 60
    v = f.compute(_mk_klines(_div_bear_closes()))
    assert v is not None
    json.dumps(v)


# ── 2. oi_conf（CARD-02，tag_oi_conf）────────────────────────────────


@pytest.fixture()
def shadow_db(monkeypatch):
    """内存库 + shadow.tag_oi_conf 的 SessionLocal（app.database 内延迟导入）指向同库"""
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    maker = sessionmaker(bind=engine)
    monkeypatch.setattr("app.database.SessionLocal", maker)
    session = Session(bind=engine)
    yield session
    session.close()


def _patch_pool(monkeypatch, ois=None, error=False):
    """ExchangePool.get_open_interest 替身：返回预置 OI，记录调用"""
    calls = []

    class _FakePool:
        def get_open_interest(self, symbol):
            if error:
                raise RuntimeError("网络不可用")
            calls.append(symbol)
            return ois[symbol]

    monkeypatch.setattr("app.services.exchange_pool.ExchangePool", _FakePool)
    return calls


def _candidate(db, symbol, net=3):
    r = ScanResult(
        scan_record_id=uuid.uuid4(), symbol=symbol, signal_type="trigger_breakout",
        current_price=100.0, breakout_pct=3.0, trend_slope=0.5, r_squared=0.8,
        votes_json={"net": net, "voted": 7},
    )
    db.add(r)
    db.commit()
    return r


def _seed_ring(symbol, ts_offset_s, oi):
    shadow_mod._OI_RING[symbol] = [(time.time() - ts_offset_s, oi)]


def _marks(db):
    return db.execute(select(ShadowMark).order_by(ShadowMark.created_at)).scalars().all()


def test_oi_conf_buckets_and_payload(shadow_db, monkeypatch):
    """ΔOI 增 → confirm / 减 → diverge；方向（votes net）进 value_json 不参与分桶"""
    ra = _candidate(shadow_db, "AAAUSDT", net=3)   # 多头
    rb = _candidate(shadow_db, "BBBUSDT", net=-2)  # 空头
    _seed_ring("AAAUSDT", 86400, 1000.0)
    _seed_ring("BBBUSDT", 86400, 1000.0)
    _patch_pool(monkeypatch, ois={"AAAUSDT": 1100.0, "BBBUSDT": 900.0})

    tagged = tag_oi_conf([ra, rb])

    assert tagged == 2
    rows = {r.symbol: r for r in _marks(shadow_db)}
    assert rows["AAAUSDT"].value_json["bucket"] == "confirm"
    assert rows["AAAUSDT"].value_json["oi_chg_pct"] == pytest.approx(10.0)
    assert rows["AAAUSDT"].value_json["direction"] == "long"
    assert rows["BBBUSDT"].value_json["bucket"] == "diverge"
    assert rows["BBBUSDT"].value_json["direction"] == "short"
    for r in rows.values():
        assert r.feature_key == "oi_conf" and r.ref_type == "scan_result"
        assert r.decision_json["stage"] == "ai_quota"
        assert r.ref_id in (ra.id, rb.id)


def test_oi_conf_no_prev_snapshot_only(shadow_db, monkeypatch):
    """环内无 24h 前值：只存快照不打标（首日冷启动不产缺失组伪统计）"""
    r = _candidate(shadow_db, "AAAUSDT")
    calls = _patch_pool(monkeypatch, ois={"AAAUSDT": 1000.0})

    assert tag_oi_conf([r]) == 0
    assert _marks(shadow_db) == []
    ring = shadow_mod._OI_RING["AAAUSDT"]
    assert len(ring) == 1 and ring[0][1] == 1000.0
    assert calls == ["AAAUSDT"]  # 快照照常入环


def test_oi_conf_prev_outside_window_no_tag(shadow_db, monkeypatch):
    """前值超出 18~30h 容差窗口（如 40h 前）：不可比，不打标只存快照"""
    r = _candidate(shadow_db, "AAAUSDT")
    _seed_ring("AAAUSDT", 40 * 3600, 1000.0)
    _patch_pool(monkeypatch, ois={"AAAUSDT": 1100.0})

    assert tag_oi_conf([r]) == 0
    assert _marks(shadow_db) == []
    assert len(shadow_mod._OI_RING["AAAUSDT"]) == 2


def test_oi_conf_fetch_error_swallows(shadow_db, monkeypatch):
    """openInterest 拉取异常全吞（影子铁律）：不抛、不打标、不入环"""
    r = _candidate(shadow_db, "AAAUSDT")
    _patch_pool(monkeypatch, error=True)

    assert tag_oi_conf([r]) == 0
    assert _marks(shadow_db) == []
    assert "AAAUSDT" not in shadow_mod._OI_RING


def test_oi_conf_idempotent_upsert(shadow_db, monkeypatch):
    """重复打标（任务重试等）→ uq 约束下覆盖不新增行"""
    r = _candidate(shadow_db, "AAAUSDT")
    _seed_ring("AAAUSDT", 86400, 1000.0)
    calls = _patch_pool(monkeypatch, ois={"AAAUSDT": 1100.0})

    assert tag_oi_conf([r]) == 1
    _seed_ring("AAAUSDT", 86400, 1200.0)  # 昨日快照换新值
    monkeypatch.setattr("app.services.exchange_pool.ExchangePool",
                        type("P", (), {"get_open_interest": lambda self, s: 1440.0}))
    assert tag_oi_conf([r]) == 1

    rows = _marks(shadow_db)
    assert len(rows) == 1
    assert rows[0].value_json["oi_chg_pct"] == pytest.approx(20.0)
    assert calls == ["AAAUSDT"]  # 首个替身的调用记录（第二个替身不记录）


def test_oi_conf_disabled_switch(shadow_db, monkeypatch):
    """enabled 代码开关置 False：不打标不触网（紧急停打通道）"""
    r = _candidate(shadow_db, "AAAUSDT")
    monkeypatch.setattr(shadow_mod, "OI_CONF_ENABLED", False)
    calls = _patch_pool(monkeypatch, ois={"AAAUSDT": 1000.0})

    assert tag_oi_conf([r]) == 0
    assert calls == []


def test_oi_conf_scan_stub_never_tags(shadow_db, monkeypatch):
    """扫描侧占位 compute 恒 None：扫描循环对 oi_conf 不打标（主线隔离）"""
    assert SHADOW_FEATURES["oi_conf"].compute([100.0] * 100) is None
    assert SHADOW_FEATURES["oi_conf"].card == "CARD-02"
    assert SHADOW_FEATURES["oi_conf"].min_sample == 60


# ── 3. ai_tasks 配额段接线 ────────────────────────────────────────────


@pytest.fixture()
def wire_sessions(monkeypatch):
    """ai_tasks / task_log 共用同一 sqlite 内存库（照 test_task_records 惯例）"""
    from sqlalchemy.pool import StaticPool

    from app.services import task_log
    from app.tasks import ai_tasks

    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    maker = sessionmaker(bind=engine)
    seed = maker()
    seed.add(SystemConfig(id=1, ai_analysis_enabled=True))
    seed.commit()
    seed.close()
    for mod in (task_log, ai_tasks):
        monkeypatch.setattr(mod, "SessionLocal", maker)
    return maker


class _FakeGroup:
    """group 替身：记录子任务签名，apply_async 空操作"""

    def __init__(self):
        self.jobs = []

    def __call__(self, gen):
        self.jobs = list(gen)
        return self

    def apply_async(self):
        pass


def _run_dispatch(monkeypatch, wire_sessions, scan_type, tag_calls):
    """造 1 条 ScanResult + 对应 scan_type 的 ScanRecord，跑一次批量分发"""
    from app.tasks import ai_tasks

    monkeypatch.setattr(settings, "FINGERPRINT_TTL_MIN", 0)
    monkeypatch.setattr(ai_tasks, "tag_oi_conf", lambda results: tag_calls.append(len(results)) or 0)

    s = wire_sessions()
    rec_id = uuid.uuid4()
    s.add(ScanRecord(id=rec_id, scan_type=scan_type))
    s.add(ScanResult(
        id=uuid.uuid4(), scan_record_id=rec_id, symbol="AAAUSDT",
        signal_type="breakout", current_price=100.0, breakout_pct=3.0,
        trend_slope=0.5, r_squared=0.8,
        volume=1000.0, volume_24h=1e7, created_at=datetime.utcnow(),
    ))
    s.commit()
    s.close()

    dispatched = _FakeGroup()
    monkeypatch.setattr(ai_tasks, "group", dispatched)
    ai_tasks.run_ai_analysis_task(str(rec_id))
    return dispatched


def test_ai_dispatch_tags_scheduled_batch(wire_sessions, monkeypatch):
    """定时扫描批量路径 → oi_conf 打标被调用（候选随配额结果传入）"""
    tag_calls = []
    _run_dispatch(monkeypatch, wire_sessions, "scheduled", tag_calls)
    assert tag_calls == [1]


def test_ai_dispatch_manual_and_single_not_tagged(wire_sessions, monkeypatch):
    """手动扫描批量与手动单币重分析均不打标（用户在场非观察总体）"""
    tag_calls = []
    _run_dispatch(monkeypatch, wire_sessions, "manual", tag_calls)

    from app.tasks import ai_tasks
    monkeypatch.setattr(ai_tasks, "tag_oi_conf",
                        lambda results: tag_calls.append(len(results)) or 0)
    s = wire_sessions()
    sr = s.execute(select(ScanResult)).scalars().first()
    s.close()
    dispatched = _FakeGroup()
    monkeypatch.setattr(ai_tasks, "group", dispatched)
    ai_tasks.run_ai_analysis_task(str(sr.scan_record_id), only_scan_result_id=str(sr.id))
    assert tag_calls == []  # 手动批量 + 单币重分析两条路径都没打标
