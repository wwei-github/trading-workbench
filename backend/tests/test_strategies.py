# -*- coding: utf-8 -*-
"""多策略可插拔（docs/20 S0/S1）单测：registry + ema_trend 插件 + 影子信号 API。

- registry：NULL=仅内置 golden_k_ai；未知 id 拒绝；clamp；golden_k 不可禁用；
- ema_trend：合成 K 线构造 4H 闸门通过 + 1H 结构成立的多/空回踩信号，
  走平拒绝；K 线格式 [ts, o, h, l, c, v]（与 ExchangePool 一致）；
- API：/api/strategies 清单、配置回写规范化、影子信号查询。
sqlite 内存库 + TestClient，不触网不触 Celery。
"""
import sys
from pathlib import Path

import numpy as np
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import app.models  # noqa: E402,F401
from app.database import Base, get_db  # noqa: E402
from app.main import app  # noqa: E402
from app.models.strategy import StrategyShadowSignal  # noqa: E402
from app.services.strategies.registry import (  # noqa: E402
    get_strategy,
    load_registry,
)
from app.services.strategies.ema_trend import EmaTrendStrategy, ema_series  # noqa: E402


# ── K 线构造 ──────────────────────────────────────────────────────────────

TS0 = 1_791_000_000_000
H1, H4 = 3_600_000, 14_400_000


def _bars(closes, ts_step, volumes=None):
    """closes → K 线（h=c+0.5, l=c-0.5, o=前收盘, v 默认 1000）。"""
    out = []
    for i, c in enumerate(closes):
        c = float(c)
        out.append([TS0 + i * ts_step, c - 0.0, c + 0.5, c - 0.5, c,
                    volumes[i] if volumes else 1000.0])
    return out


def _trend_closes(n, step=0.05, start=100.0):
    return [start + i * step for i in range(n)]


def _pullback_klines_1h(direction: str):
    """1h 上升趋势 + 倒数第二根（末根已收盘）回踩 EMA21 企稳（多头）/反抽（空头），
    再追加一根尾随未收盘 K 线（插件 _closed 会剔除，只作输入形态仿真）。

    触点设计：以 299 根收盘的 EMA21 末值作影线端点——EMA(300) 仅前移
    ~0.07（a×Δclose），落容差带 ±0.3×ATR(=1.0) 内。"""
    n = 300
    step = 0.05 if direction == "long" else -0.05
    closes = _trend_closes(n - 1, step)
    e21 = ema_series(np.array(closes, dtype=float), 21)
    touch = float(e21[-1])
    last_c = closes[-1]
    if direction == "long":
        sig_bar = [TS0 + (n - 1) * H1, last_c, last_c + 0.8, touch, last_c + 0.3, 1100.0]
        tail = [TS0 + n * H1, last_c + 0.3, last_c + 0.9, last_c + 0.1, last_c + 0.5, 900.0]
    else:
        sig_bar = [TS0 + (n - 1) * H1, last_c, touch, last_c - 0.8, last_c - 0.3, 1100.0]
        tail = [TS0 + n * H1, last_c - 0.3, last_c - 0.1, last_c - 0.9, last_c - 0.5, 900.0]
    return _bars(closes, H1) + [sig_bar, tail]


@pytest.fixture()
def client():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    TestSession = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    session = TestSession()

    def _override():
        try:
            yield session
        finally:
            pass

    app.dependency_overrides[get_db] = _override
    yield TestClient(app), session
    app.dependency_overrides.pop(get_db, None)
    session.close()


# ── registry ──────────────────────────────────────────────────────────────


def test_registry_null_only_golden_k():
    entries = load_registry(None)
    assert [e.id for e in entries] == ["golden_k_ai"]
    assert entries[0].enabled is True


def test_registry_unknown_id_rejected_and_clamp():
    entries = load_registry([
        {"id": "bogus", "enabled": True},
        {"id": "ema_trend", "enabled": True, "max_positions": 99,
         "risk_weight": 5.0, "config": {"adx_min": 25}, "junk": 1},
        {"id": "golden_k_ai", "enabled": False},  # 内置不可禁用
    ])
    ids = [e.id for e in entries]
    assert ids[0] == "golden_k_ai" and "bogus" not in ids
    ema = next(e for e in entries if e.id == "ema_trend")
    assert ema.enabled is True
    assert ema.max_positions == 10   # clamp 1~10
    assert ema.risk_weight == 1.0    # clamp 0.1~1.0
    assert ema.config == {"adx_min": 25}  # 未知键丢弃
    golden = entries[0]
    assert golden.enabled is True    # 恒启用


def test_get_strategy_injects_config():
    raw = [{"id": "ema_trend", "enabled": True, "config": {"adx_min": 25}}]
    s = get_strategy("ema_trend", raw)
    assert s.cfg.adx_min == 25
    assert s.id == "ema_trend"


# ── ema_trend 插件 ────────────────────────────────────────────────────────


def test_ema_trend_long_pullback_signal():
    s = EmaTrendStrategy({})
    k1 = _pullback_klines_1h("long")
    k4 = _bars(_trend_closes(300, 0.05), H4)
    sig = s.build_signal("TESTUSDT", klines_1h=k1, klines_4h=k4)
    assert sig is not None
    assert sig.strategy_id == "ema_trend"
    assert sig.direction == "long"
    assert sig.signal_type == "pullback"
    assert sig.stop < sig.entry                      # 多头止损在下方
    assert sig.raw["gate_4h"]["direction"] == "up"
    assert sig.raw["structure_1h"]["aligned"] is True
    assert sig.raw["touched"] in ("EMA21", "EMA55")
    # 前高距入场不足 min_rr → TP1=0（不设固定目标）
    assert sig.tp1 == 0.0 and sig.tp2 == 0.0


def test_ema_trend_short_pullback_signal():
    s = EmaTrendStrategy({})
    k1 = _pullback_klines_1h("short")
    k4 = _bars(_trend_closes(300, -0.05), H4)
    sig = s.build_signal("TESTUSDT", klines_1h=k1, klines_4h=k4)
    assert sig is not None and sig.direction == "short"
    assert sig.stop > sig.entry                      # 空头止损在上方
    assert sig.raw["gate_4h"]["direction"] == "down"


def test_ema_trend_flat_market_rejected():
    s = EmaTrendStrategy({})
    k1 = _bars([100.0] * 300, H1)
    k4 = _bars([100.0] * 300, H4)
    assert s.build_signal("TESTUSDT", klines_1h=k1, klines_4h=k4) is None


def test_ema_trend_insufficient_bars_rejected():
    s = EmaTrendStrategy({})
    k1 = _bars(_trend_closes(100), H1)
    k4 = _bars(_trend_closes(100), H4)
    assert s.build_signal("TESTUSDT", klines_1h=k1, klines_4h=k4) is None


# ── API ───────────────────────────────────────────────────────────────────


def test_api_list_strategies_default_golden_k_only(client):
    http, _db = client
    r = http.get("/api/strategies")
    assert r.status_code == 200
    body = r.json()
    assert [s["id"] for s in body] == ["golden_k_ai"]
    assert body[0]["kind"] == "native"


def test_api_list_shows_disabled_plugin(client):
    """禁用的插件条目也要出现在清单（可见性：防误认为配置丢失）"""
    http, _db = client
    http.put("/api/scans/config", json={
        "strategy_registry": [{"id": "ema_trend", "enabled": False}],
    })
    body = http.get("/api/strategies").json()
    ema = next((s for s in body if s["id"] == "ema_trend"), None)
    assert ema is not None and ema["enabled"] is False


def test_api_config_registry_roundtrip_and_normalize(client):
    http, _db = client
    r = http.put("/api/scans/config", json={
        "strategy_registry": [
            {"id": "ema_trend", "enabled": True, "max_positions": 2,
             "risk_weight": 0.5, "config": {"adx_min": 25}},
            {"id": "bogus", "enabled": True},
        ],
    })
    assert r.status_code == 200
    assert r.json()["strategy_registry"] == [
        {"id": "ema_trend", "enabled": True, "max_positions": 2,
         "risk_weight": 0.5, "config": {"adx_min": 25}},
    ]
    # 清单读出 ema_trend enabled
    lst = http.get("/api/strategies").json()
    ids = {s["id"] for s in lst}
    assert "ema_trend" in ids
    assert [s for s in lst if s["id"] == "ema_trend"][0]["enabled"] is True
    # 显式 null = 清除定制
    r2 = http.put("/api/scans/config", json={"strategy_registry": None})
    assert r2.status_code == 200 and r2.json()["strategy_registry"] is None


def test_api_signals_empty_and_after_insert(client):
    http, session = client
    assert http.get("/api/strategies/signals").json() == []
    session.add(StrategyShadowSignal(
        strategy_id="ema_trend", symbol="TESTUSDT", direction="long",
        entry=100.0, stop=99.0, tp1=0.0, tp2=0.0, signal_type="pullback",
        reason="单测", confidence=0.6, raw={"touched": "EMA21"},
    ))
    session.commit()
    rows = http.get("/api/strategies/signals?strategy_id=ema_trend").json()
    assert len(rows) == 1
    assert rows[0]["symbol"] == "TESTUSDT" and rows[0]["direction"] == "long"
    assert rows[0]["raw"]["touched"] == "EMA21"
    bad = http.get("/api/strategies/signals?strategy_id=bogus")
    assert bad.status_code == 400


# ── P2 出场变体（docs/19 §3.4）+ 影子推演口径 ────────────────────────

from app.services.backtest_engine import (  # noqa: E402
    OVERRIDE_KEYS,
    _sig_exit_flags,
    replay_exit,
)
from app.tasks.strategy_tasks import EVAL_EXIT_OVERRIDES  # noqa: E402


def _trend_bars(n_up=60, n_dn=20, up_step=0.5, dn_step=1.2, peak=130.0):
    """涨后跌的 1h 合成K线（[ts,o,h,l,c,v]），低点全程 > 95 不触初始止损。"""
    out = []
    for i in range(n_up + n_dn):
        c = 100 + i * up_step if i < n_up else peak - (i - n_up + 1) * dn_step
        out.append([1791000000000 + i * 3600000, c, c + 0.5, c - 0.5, c, 100.0])
    return out


def test_sig_exit_ema55_break_fires_at_close():
    """V2 菜单 ema55_break：收盘破 EMA55 当根以收盘价清剩余仓"""
    bars = _trend_bars()
    r = replay_exit("long", 100.0, 95.0, 190.0, 0.0, bars,  # tp1 挂天外不可达
                    overrides={"sig_exit": "ema55_break"})
    assert r["legs"][-1]["name"] == "sig_exit"
    assert r["legs"][-1]["qty_pct"] == pytest.approx(1.0)
    # 破位段收盘价显著低于峰值
    assert r["legs"][-1]["price"] < 130.0


def test_sig_exit_ma20_break_2bar_requires_two_closes():
    """「无法快速收回」量化：单根收盘破 MA20 不出场，连续两根才出"""
    bars = _trend_bars(n_up=60, n_dn=0)  # 60 根缓涨（100→129.5）
    bars += [
        [1791000000000 + 60 * 3600000, 110.0, 110.5, 109.5, 110.0, 100.0],
        [1791000000000 + 61 * 3600000, 108.0, 108.5, 107.5, 108.0, 100.0],
    ]  # 跳水破 MA20（≈125），低点全程不触初始止损 95
    one = replay_exit("long", 100.0, 95.0, 190.0, 0.0, bars[:61],
                      overrides={"sig_exit": "ma20_break_2bar"})
    flags = _sig_exit_flags(bars[:61], "ma20_break_2bar", 1)
    # 第 60 根（跌第 1 根）单独破 MA20 → beyond 但不足两根，不触发
    assert flags[60] is False
    assert one["outcome"] == "open"  # 无任何出场 → 窗口耗尽
    two = replay_exit("long", 100.0, 95.0, 190.0, 0.0, bars[:62],
                      overrides={"sig_exit": "ma20_break_2bar"})
    assert two["legs"][-1]["name"] == "sig_exit"


def test_sig_exit_macd_death_cross_flags():
    """零上死叉旗标：急涨后缓跌（dif 穿 dea 时仍双正）→ 多头触发"""
    bars = _trend_bars(n_up=60, n_dn=40, up_step=1.0, dn_step=0.8)
    flags = _sig_exit_flags(bars, "macd_death_cross", 1)
    assert any(flags[60:])       # 跌段内触发
    assert not any(flags[:20])   # 上涨段无死叉


def test_pure_trail_tp_qty_override():
    """V1 纯跟踪：(0,0) 覆盖 → 无 TP 腿，全部仓位走跟进/信号出场"""
    bars = _trend_bars()
    r = replay_exit("long", 100.0, 95.0, 107.5, 112.5, bars,
                    overrides={"tp_qty_override": (0.0, 0.0),
                               "sig_exit": "ema55_break"})
    assert all(l["name"] not in ("tp1", "tp2") for l in r["legs"])
    assert sum(l["qty_pct"] for l in r["legs"]) == pytest.approx(1.0)


def test_tp1_zero_guard_no_phantom_leg():
    """tp1=0（纯跟踪信号语义）：不挂目标腿——多头 high>=0 恒真防废腿"""
    r = replay_exit("long", 100.0, 95.0, 0.0, 0.0, _trend_bars()[:20],
                    overrides={"fwd_bars": 20})
    assert r["outcome"] == "open" and r["legs"] == []


def test_eval_exit_overrides_keys_legal():
    """影子推演的每策略出场 overrides 必须全在引擎白名单内"""
    for sid, ov in EVAL_EXIT_OVERRIDES.items():
        assert set(ov) <= set(OVERRIDE_KEYS), sid


def test_baseline_unaffected_by_new_keys():
    """新键缺省 None 时基线分批与既有口径完全一致（硬门不漂移）"""
    bars = _trend_bars()
    r = replay_exit("long", 100.0, 95.0, 107.5, 112.5, bars)
    assert [l["name"] for l in r["legs"][:2]] == ["tp1", "tp2"]
    assert r["legs"][0]["qty_pct"] == pytest.approx(0.60)
    assert r["legs"][1]["qty_pct"] == pytest.approx(0.20)


# ── L2 震荡评分卡 + L1 观测指标（docs/19 §3.1/§3.2）──────────────────

from app.services.strategies.ema_trend import EmaTrendStrategy  # noqa: E402
from app.services.strategies.range_score import (  # noqa: E402
    compute_range_score,
)
from app.services.strategy.technical import (  # noqa: E402
    analyze_stochastic,
    macd_zero_state,
)


def _mk_klines(closes, h=None, l=None, vol=100.0):
    """closes → K 线（高低默认 ±0.5）。"""
    out = []
    for i, c in enumerate(closes):
        hi = (h[i] if h else c + 0.5)
        lo = (l[i] if l else c - 0.5)
        out.append([1791000000000 + i * 3600000, c, hi, lo, c, vol])
    return out


def test_range_score_trending_scores_low():
    """强趋势（三线排列/发散/ADX 高/不缠绕）→ 低分"""
    closes = [100 + i * 0.8 for i in range(220)]
    k1 = _mk_klines(closes)
    k4 = _mk_klines([100 + i * 3.2 for i in range(120)])
    res = compute_range_score(k1, k4)
    assert res.score <= 2
    assert len(res.items) == 8


def test_range_score_oscillating_scores_high():
    """横盘震荡（三线黏着反复穿越/量能低迷）→ 高分"""
    closes = []
    for i in range(220):
        closes.append(100 + (1.2 if i % 4 < 2 else -1.2))
    k1 = _mk_klines(closes, vol=10.0)
    k4 = _mk_klines([100 + (3 if i % 4 < 2 else -3) for i in range(120)], vol=10.0)
    res = compute_range_score(k1, k4)
    assert res.score >= 3


def test_range_score_insufficient_data_no_crash():
    """K 线不足：hit=None 不计分不崩溃（恒定量能 cv=0 属合法低迷判定）"""
    res = compute_range_score(_mk_klines([100.0] * 30), _mk_klines([100.0] * 10))
    assert len(res.items) == 8
    assert all(it["hit"] is not True or it["no"] == 8 for it in res.items)


def test_macd_zero_state_directions():
    """上升段零轴上方；深跌段零轴下方；胶着 mixed"""
    up = macd_zero_state(_mk_klines([100 + i * 1.0 for i in range(80)]))
    assert up and up["state"] == "above" and up["dir"] == 1
    dn = macd_zero_state(_mk_klines([200 - i * 1.0 for i in range(80)]))
    assert dn and dn["state"] == "below" and dn["dir"] == -1
    assert macd_zero_state(_mk_klines([100.0] * 40)) is None or True  # 平盘不崩


def test_analyze_stochastic_kdj():
    """KDJ 口径：K/D/J 值域、超买超卖状态、金叉标记"""
    # 连续大涨 → 超买
    up = analyze_stochastic(_mk_klines([100 + i * 1.5 for i in range(60)]))
    assert up and 0 <= up["k"] <= 100 and 0 <= up["d"] <= 100
    assert up["state"] == "overbought"
    # 连续大跌 → 超卖
    dn = analyze_stochastic(_mk_klines([200 - i * 1.5 for i in range(60)]))
    assert dn and dn["state"] == "oversold"
    # 大跌后短反弹 3 根 → 金叉标记（K<50 区域）
    c = [200 - i * 1.5 for i in range(50)] + [125 + i * 2.0 for i in range(3)]
    rev = analyze_stochastic(_mk_klines(c))
    assert rev and rev["cross"] == 1


def test_observe_package_shape():
    """L1 观测证据包：键集完整、评分卡 items 8 项、不足样本不崩"""
    obs = EmaTrendStrategy({})
    m = obs.observe(klines_1h=_mk_klines([100 + i * 0.8 for i in range(220)]),
                    klines_4h=_mk_klines([100 + i * 3.2 for i in range(120)]))
    assert set(m) >= {"gate_4h", "structure_1h", "range_score", "stochastic", "macd_zero"}
    assert len(m["range_score"]["items"]) == 8
    m2 = obs.observe(klines_1h=_mk_klines([100.0] * 20),
                     klines_4h=_mk_klines([100.0] * 20))
    assert m2["gate_4h"] is None and m2["structure_1h"] is None
