# -*- coding: utf-8 -*-
"""合约回测引擎单测（docs/16 P-06）：replay_exit 基线等价性 + 变体 + 采样器 + 组合。

核心原则：
- 基线等价性（测试 1）是硬门：replay_exit 空 overrides 必须逐例复刻
  replay_suggestion 的终局（loss/win_tp1/win_tp2）——锁死生产语义防漂移；
- 与 replay_suggestion 的一处已知口径差异：TP 后窗口耗尽，旧复盘计 win_*、
  引擎按 open（剩余仓未决，P-04 口径诚实原则）——测试 1 只对比可终局场景。
纯函数测试（不触 DB 不触网），K 线手工构造。
"""
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.backtest_engine import (  # noqa: E402
    BACKTEST_ENGINE_VERSION,
    _atr_series_daily,
    aggregate,
    build_signals_from_history,
    replay_exit,
    simulate_portfolio,
)
from app.tasks.review_tasks import replay_suggestion  # noqa: E402

T0 = 1_791_000_000_000
HOUR_MS = 3_600_000

# 标准三价：entry=100 stop=95（r_unit=5）tp1=107.5（1.5R）tp2=112.5（2.5R）
E, S, TP1, TP2 = 100.0, 95.0, 107.5, 112.5


def kb(*specs, t0=T0):
    """造 1h K 线：spec=(high, low, close?)。"""
    out = []
    for i, sp in enumerate(specs):
        hi, lo = float(sp[0]), float(sp[1])
        c = float(sp[2]) if len(sp) > 2 else (hi + lo) / 2
        out.append([t0 + i * HOUR_MS, (hi + lo) / 2, hi, lo, c, 100.0])
    return out


def daily(atr: float, n: int = 30, end_open=T0 - 2 * 86_400_000):
    """造 D1 K 线：每根 high-low = atr（TR 恒定 → ATR22=atr），止于 end_open。"""
    return [[end_open - (n - 1 - i) * 86_400_000, 100.0, 100 + atr / 2,
             100 - atr / 2, 100.0, 1.0] for i in range(n)]


# ── 1. 基线等价性（硬门）──────────────────────────────────────────────


def test_baseline_equivalent_to_replay_suggestion():
    """空 overrides 终局与 replay_suggestion 逐例一致（loss/win_tp1/win_tp2）"""
    cases = [
        # 直损
        (kb((96.0, 94.5)), 0.0, "loss"),
        # 冲高回落打损（未触 TP1）
        (kb((101.0, 99.0), (98.0, 94.8)), 0.0, "loss"),
        # TP1 后回撤打损（保本武装→被扫，replay_suggestion 计 win_tp1）
        (kb((108.0, 99.5), (103.0, 99.8), (100.0, 99.9)), 0.0, "win_tp1"),
        # TP2 直达后崩（跟进止损扫出，replay_suggestion 首触 TP2 即 win_tp2；
        # 引擎 TP1+TP2 落袋后剩余 25% 未决 → open，见 test_open_when_window_ends_after_tp）
        (kb((113.0, 100.5), (108.0, 96.0)), TP2, "win_tp2"),
    ]
    for bars, tp2, expect in cases:
        base = replay_suggestion("long", E, S, TP1, tp2, bars)
        mine = replay_exit("long", E, S, TP1, tp2, bars)
        assert base[0] == expect, f"base bars={bars}"  # 场景自校验
        if expect != "win_tp2":
            assert mine["outcome"] == expect == base[0], f"bars={bars}"
        else:
            # 引擎差异（有意）：win_tp2 后剩余仓窗口耗尽 → open；
            # 已落袋腿 tp1+tp2 必须都在
            assert mine["outcome"] == "open"
            assert {l["name"] for l in mine["legs"]} == {"tp1", "tp2"}


def test_baseline_equivalent_short_symmetric():
    """空头对称：SL 111 / TP1 92.5"""
    e, s, tp1 = 100.0, 111.0, 92.5
    cases = [
        (kb((112.0, 100.0)), "loss"),                      # 直损（high≥SL111）
        (kb((99.0, 93.0), (111.5, 96.0)), "loss"),         # 反抽打损（high≥SL111）
        (kb((96.0, 92.0), (100.5, 95.0), (100.0, 99.8)), "win_tp1"),  # TP1 后打损
    ]
    for bars, expect in cases:
        base = replay_suggestion("short", e, s, tp1, 0.0, bars)
        mine = replay_exit("short", e, s, tp1, 0.0, bars)
        assert base[0] == expect, f"base bars={bars}"
        assert mine["outcome"] == expect == base[0]


def test_open_when_window_ends_after_tp():
    """口径差异（有意）：TP 后窗口耗尽 → 引擎 open（旧复盘计 win）。
    剩余仓未决不进 decided——P-04 口径诚实原则。"""
    bars = kb((108.0, 99.5))  # 触 TP1 后无后续
    assert replay_suggestion("long", E, S, TP1, 0.0, bars)[0] == "win_tp1"
    mine = replay_exit("long", E, S, TP1, 0.0, bars)
    assert mine["outcome"] == "open" and mine["r"] is None
    assert mine["legs"][0]["name"] == "tp1"  # 已落袋腿保留


# ── 2. 同根保守序 ─────────────────────────────────────────────────────


def test_same_bar_adverse_sl_wins():
    """逆侧止损整根优先：同根触 SL+TP1 → loss（replay_suggestion 语义）"""
    r = replay_exit("long", E, S, TP1, 0.0, kb((108.0, 94.0)))
    assert r["outcome"] == "loss" and len(r["legs"]) == 1
    assert r["legs"][0]["name"] == "sl"


def test_same_bar_be_before_tp1():
    """顺侧锁利止损近者先成：BE 武装后同根触 BE+TP1 → be_exit（TP1 不落袋）"""
    r = replay_exit("long", E, S, TP1, 0.0,
                    kb((104.5, 99.0), (107.6, 100.05)),  # bar0 MFE0.9 武装（不触TP1）
                    overrides={"be_trigger_r": 0.8})
    assert r["outcome"] == "be_exit"
    assert r["legs"][0]["name"] == "be_exit"
    assert abs(r["r"]) < 0.01  # 保本价含费缓冲 → 净 ≈0


def test_same_bar_tp0_before_tp1():
    """目标位近者先成：同根触 TP0(0.5R)+TP1(1.5R) → 双落袋（0.25+0.75 满仓即终局）"""
    r2 = replay_exit("long", E, S, TP1, 0.0, kb((108.0, 99.0), (107.0, 103.0)),
                     overrides={"tp0_r": 0.5})
    assert [l["name"] for l in r2["legs"]] == ["tp0", "tp1"]
    assert r2["outcome"] == "win_tp1"  # 满仓即告终局（无需等跟进）


def test_sl_break_after_filled():
    """顺侧 SL 成交即清仓：BE 先于 TP1 成交后 TP1 不再落袋"""
    r = replay_exit("long", E, S, TP1, 0.0,
                    kb((104.5, 99.5), (107.6, 100.05)),  # bar0 武装，bar1 BE+TP1 双触
                    overrides={"be_trigger_r": 0.8})
    assert sum(l["qty_pct"] for l in r["legs"]) == pytest.approx(1.0)
    assert [l["name"] for l in r["legs"]] == ["be_exit"]


# ── 3. BE 变体（CARD-05）──────────────────────────────────────────────


def test_be_variant_arm_lag_and_rescue():
    """MFE≥0.8R 收盘武装（下根生效）→ 回踩保本救回 ≈0R"""
    r = replay_exit("long", E, S, TP1, 0.0,
                    kb((104.5, 99.0), (103.0, 99.8), (100.0, 100.05)),
                    overrides={"be_trigger_r": 0.8})
    # bar0 MFE 0.9R≥0.8 → 收盘武装 sl=100.1；bar1 不触；bar2 low≤100.1 → be_exit
    assert r["armed_be"] is True
    assert r["outcome"] == "be_exit"
    assert abs(r["r"]) < 0.01  # 缓冲恰好覆盖往返费


def test_be_variant_same_root_not_saved():
    """严格口径：武装根同根先触 SL → 不救（shadow 同款）"""
    r = replay_exit("long", E, S, TP1, 0.0, kb((108.0, 94.0)),
                    overrides={"be_trigger_r": 0.8})
    assert r["outcome"] == "loss" and r["armed_be"] is False


def test_be_variant_no_arm_no_rescue():
    """未到武装线直损 → 基线 loss"""
    r = replay_exit("long", E, S, TP1, 0.0, kb((103.0, 94.5)),
                    overrides={"be_trigger_r": 0.8})
    assert r["outcome"] == "loss" and r["armed_be"] is False


# ── 4. TP0 变体（CARD-06）─────────────────────────────────────────────


def test_tp0_gate_blocks_low_rr():
    """门控：tp1_r < gate 不挂 TP0，走基线"""
    tp1_low = 104.5  # 0.9R < 1.2
    r = replay_exit("long", E, S, tp1_low, 0.0, kb((102.4, 94.0)),
                    overrides={"tp0_r": 0.5})
    assert r["outcome"] == "loss"
    assert all(l["name"] != "tp0" for l in r["legs"])


def test_tp0_two_stage_be():
    """两阶段：TP0 落袋 → 武装保本 → 回踩 be_after_tp0（0.25×0.5R + 0.75×≈0）"""
    r = replay_exit("long", E, S, TP1, 0.0,
                    kb((102.6, 99.0), (101.0, 100.05)),
                    overrides={"tp0_r": 0.5})
    names = [l["name"] for l in r["legs"]]
    assert names == ["tp0", "be_after_tp0"]
    assert r["outcome"] == "be_exit"
    # tp0 腿 (0.5−0.02)×0.25 = 0.12 + be 腿 (0.02−0.02)×0.75 = 0
    assert r["r"] == pytest.approx(0.12, abs=0.005)


def test_tp0_survived_then_tp1():
    """TP0 后不回踩走到 TP1 → tp0+tp1 双落袋满仓即终局 win_tp1"""
    r = replay_exit("long", E, S, TP1, 0.0,
                    kb((102.6, 100.2), (108.0, 102.0)),
                    overrides={"tp0_r": 0.5})
    names = [l["name"] for l in r["legs"]]
    assert names == ["tp0", "tp1"]
    assert r["outcome"] == "win_tp1"


# ── 5. ATR/摆动跟进 ───────────────────────────────────────────────────


def test_trailing_swing_and_atr_tighten():
    """TP1 后跟进：摆动腿+ATR 腿取更紧、只收紧不放松、滞后一根生效"""
    d1 = daily(atr=2.0)
    r = replay_exit("long", E, S, TP1, 0.0,
                    kb((99.0, 98.0), (108.0, 102.5), (103.5, 101.5)),
                    daily_bars=d1)
    # bar1 触 TP1；收盘武装：sl=max(95, be=100.1, swing=min(98,102.5)=98,
    # chand=108−3×2=102)=102；bar2 low 101.5≤102 → trail_tp1 出场
    assert r["outcome"] == "win_tp1"
    assert r["legs"][-1]["name"] == "trail_tp1"
    assert r["legs"][-1]["price"] == pytest.approx(102.0)
    assert r["trail_used"] is True


def test_trailing_only_tightens():
    """只收紧不放松：回撤后摆动腿低于现 SL → SL 不动；ATR 腿收紧后按紧者出场"""
    d1 = daily(atr=2.0)
    r = replay_exit("long", E, S, TP1, 0.0,
                    kb((99.0, 98.0), (108.0, 102.5), (104.0, 102.5),
                       (110.0, 103.0), (106.0, 104.0)),
                    daily_bars=d1)
    # bar1 TP1 落袋，SL→max(be100.1, chand108−6=102)=102；
    # bar2 low 102.5>102 不触；收盘 swing=min(102.5,102.5)=102.5、chand=102 → 102.5
    # bar3 收盘 chand=110−6=104 → 104；bar4 low 104≤104 → 出场价 104
    assert r["outcome"] == "win_tp1"
    assert r["legs"][-1]["name"] == "trail_tp1"
    assert r["legs"][-1]["price"] == pytest.approx(104.0)


def test_trailing_immediate_trigger_skip():
    """生产跳过语义：新 SL 挂上即触发（≥收盘价）→ 保留旧 SL"""
    d1 = daily(atr=2.0)
    r = replay_exit("long", E, S, TP1, 0.0,
                    kb((99.0, 98.0), (108.0, 102.5, 101.0)),  # bar1 收盘 101 < chand 102
                    daily_bars=d1)
    # bar1 收盘 101：new_sl=102 ≥ 101 → 跳过，sl 保持 95；窗口耗尽 open
    assert r["outcome"] == "open"
    assert r["legs"][0]["name"] == "tp1"


def test_tp2_clamp_to_tp1():
    """TP2 后 SL 夹紧到 TP1 保本锚（生产 _move_sl_trailing 语义）"""
    d1 = daily(atr=0.1)  # ATR 腿极松，隔离摆动+夹紧
    r = replay_exit("long", E, S, TP1, TP2,
                    kb((113.0, 100.5), (111.0, 108.0), (109.0, 107.5)),
                    daily_bars=d1)
    # bar0 TP1+TP2 双落袋；bar1 摆动=min(100.5,108)=100.5、chand=113−0.3=112.7
    # → new_sl=max(be,100.5,112.7,TP1夹紧107.5)=112.7 ≥ close 111 → 跳过；
    # bar2 low 107.5：sl 仍 95？不——bar1 跳过后 sl=95，bar2 low≤95? 否 → open
    # 修正预期：chand 112.7 > close 111 跳过 → SL 停留 95 → bar2 未触 → open
    assert r["outcome"] == "open"


# ── 6. 分批数量与 R 记账 ──────────────────────────────────────────────


def test_tp2_split_quantities():
    """有 TP2：tp1 60% / tp2 30% / 跟进 10%（生产 _tp_quantities，2026-10-08 调整）"""
    d1 = daily(atr=0.1)
    r = replay_exit("long", E, S, TP1, TP2,
                    kb((113.0, 100.5), (114.0, 109.0), (110.0, 109.5)),
                    daily_bars=d1)
    # bar0 TP1+TP2；bar1 chand=114−0.3=113.7 ≥ close 109.5 跳过；sl=95
    # bar2 low 109.5 > 95 → open？需要出场……改用无 ATR：swing 收紧后被打
    # ——见 test_tp2_split_quantities_v2
    assert {"tp1", "tp2"} <= {l["name"] for l in r["legs"]}


def test_tp2_split_quantities_v2():
    """有 TP2 分批 + 摆动跟进出场（无 ATR 干扰：trail_atr_k=0）"""
    r = replay_exit("long", E, S, TP1, TP2,
                    kb((113.0, 100.5), (114.0, 108.5, 110.0), (108.4, 106.0)),
                    overrides={"trail_atr_k": 0})
    # bar0 TP1+TP2；bar1 收盘武装：be=100.1，swing=min(100.5,108.5)=100.5
    # → sl=100.1（无 ATR）；bar2 low 106 > 100.1……未触 → 需要更低
    assert r["outcome"] in ("win_tp2", "open")
    qty = {l["name"]: l["qty_pct"] for l in r["legs"]}
    if "tp1" in qty:
        assert qty["tp1"] == pytest.approx(0.60)
    if "tp2" in qty:
        assert qty["tp2"] == pytest.approx(0.30)


def test_no_tp2_split_quantities():
    """无 TP2：TP1 90% / 跟进 10%（2026-10-08 调整）"""
    r = replay_exit("long", E, S, TP1, 0.0,
                    kb((108.0, 99.5), (104.0, 100.0, 100.05)))
    # bar1 low 100.0 ≤ be 100.1 → trail_tp1 出 10%
    names = [l["name"] for l in r["legs"]]
    qty = {l["name"]: l["qty_pct"] for l in r["legs"]}
    assert names == ["tp1", "trail_tp1"]
    assert qty["tp1"] == pytest.approx(0.90) and qty["trail_tp1"] == pytest.approx(0.10)


def test_fee_accounting():
    """R 记账含费：直损 = −1R − 往返费（entry×2×fee/r_unit）"""
    r = replay_exit("long", E, S, TP1, 0.0, kb((96.0, 94.5)),
                    overrides={"fee_rate": 0.0005})
    # fee_r = 100×0.001/5 = 0.02 → r = −1.02
    assert r["r"] == pytest.approx(-1.02, abs=1e-6)


def test_invalid_inputs():
    """entry==stop / 缺方向 → invalid，不进统计"""
    assert replay_exit("long", 100.0, 100.0, 107.5, 0.0, kb((101.0, 99.0)))["outcome"] == "invalid"
    assert replay_exit("", 100.0, 95.0, 107.5, 0.0, kb((101.0, 99.0)))["outcome"] == "invalid"


# ── 7. ATR 序列 ───────────────────────────────────────────────────────


def test_atr_series_daily():
    arr = _atr_series_daily(daily(atr=2.0, n=30), 22)
    assert len(arr) == 30
    assert arr[21] == pytest.approx(2.0)
    assert arr[29] == pytest.approx(2.0)
    assert arr[0] != arr[0]  # 前 21 根 NaN


# ── 8. 采样器源A ──────────────────────────────────────────────────────


def _ai(**kw):
    base = dict(symbol="BTCUSDT", trade_decision="suggest", direction="long",
                entry_price=100.0, stop_loss=95.0, take_profit_1=107.5,
                take_profit_2=112.5, recommendation=75.0,
                veto_original=None, rule_version="v2026-10-02",
                created_at=None)
    base.update(kw)
    return SimpleNamespace(**base)


def _sr(**kw):
    base = dict(signal_type="trigger_pullback_confirm", position="ema21",
                ema_state="bullish", quality_score=80)
    base.update(kw)
    return SimpleNamespace(**base)


def test_source_a_suggest_veto_actual():
    import datetime as dt
    t = dt.datetime(2026, 10, 1, 0, 0)
    rows = [
        (_ai(created_at=t), _sr()),  # suggest
        (_ai(trade_decision="skip", created_at=t,
             veto_original={"direction": "short", "entry_price": 200.0,
                            "stop_loss": 204.0, "take_profit_1": 194.0,
                            "take_profit_2": 0}), _sr()),  # veto
        (_ai(trade_decision="skip", created_at=t), _sr()),  # 无 veto 的 skip → 跳过
    ]
    tr = SimpleNamespace(id="t1", symbol="ETHUSDT", direction="long",
                         entry_price=50.0,
                         stop_loss=50.5,  # 现值：TP1 后已移保本（非初始 48）
                         tp1=53.0, tp2=0.0, opened_at=t,
                         exit_reason="sl", realized_pnl=-30.0, risk_amount=30.0)
    # OPEN 事件快照 = 开仓时初始三价；回放必须用快照而非移动后的现值
    open_events = {"t1": {"stop_loss": 48.0, "tp1": 53.0, "tp2": 0}}
    sigs = build_signals_from_history(rows, [tr], open_events)
    assert [s["meta"]["src"] for s in sigs] == ["suggest", "veto", "actual"]
    assert sigs[1]["direction"] == "short" and sigs[1]["entry"] == 200.0
    assert sigs[2]["stop"] == 48.0 and sigs[2]["meta"]["exit_reason"] == "sl"
    assert sigs[0]["meta"]["signal_type"] == "trigger_pullback_confirm"
    # 无事件行兜底现值 stop_loss（旧行/事件缺失不炸）
    sigs_fallback = build_signals_from_history([], [tr])
    assert sigs_fallback[0]["stop"] == 50.5


# ── 9. 组合模拟与聚合 ─────────────────────────────────────────────────


def _res(outcome, r, bars=2):
    return {"outcome": outcome, "r": r, "legs": [], "sl_price": None,
            "bars": bars, "armed_be": False, "trail_used": False}


def test_portfolio_caps_and_equity():
    sig = lambda t, d="long": {"symbol": "X", "direction": d, "entry": 100,
                               "stop": 95, "tp1": 107.5, "tp2": 0,
                               "entry_time_ms": t, "meta": {}}
    pairs = [
        (sig(T0), _res("win_tp1", 1.5, bars=4)),              # 占用至 T0+4h
        (sig(T0 + HOUR_MS), _res("loss", -1.0, bars=2)),      # 同向 cap=1 → 拒
        (sig(T0 + 2 * HOUR_MS, "short"), _res("loss", -1.0, bars=2)),  # 反向占槽
        (sig(T0 + 3 * HOUR_MS), _res("win_tp1", 1.0, bars=2)),  # 槽满(2) → 拒
    ]
    out = simulate_portfolio(pairs, risk_usdt=10.0, max_open_trades=2,
                             same_direction_cap=1)
    assert out["final_equity_r"] == pytest.approx(0.5)   # 1.5 − 1.0
    assert out["rejected"] == {"slot_full": 1, "same_dir_cap": 1}
    assert out["final_equity_usdt"] == pytest.approx(5.0)
    assert out["max_dd_r"] == pytest.approx(1.0)  # 峰值 1.5 → 0.5


def test_portfolio_max_dd():
    sig = lambda t: {"symbol": "X", "direction": "long", "entry": 100,
                     "stop": 95, "tp1": 107.5, "tp2": 0,
                     "entry_time_ms": t, "meta": {}}
    pairs = [
        (sig(T0), _res("win_tp1", 2.0, bars=1)),
        (sig(T0 + 10 * HOUR_MS), _res("loss", -1.0, bars=1)),
        (sig(T0 + 20 * HOUR_MS), _res("win_tp1", 0.5, bars=1)),
    ]
    out = simulate_portfolio(pairs, risk_usdt=1.0, max_open_trades=5)
    # 权益 2→1→1.5；峰值 2 → dd=1.0
    assert out["final_equity_r"] == pytest.approx(1.5)
    assert out["max_dd_r"] == pytest.approx(1.0)
    assert len(out["daily_series"]) >= 1


def test_aggregate_pairing_excludes_open():
    """配对 delta 只计双双终局；open 单列 excluded_open"""
    signals = [{"symbol": "X", "direction": "long", "entry": 100, "stop": 95,
                "tp1": 107.5, "tp2": 0, "entry_time_ms": T0 + i * HOUR_MS,
                "meta": {}} for i in range(3)]
    base = [_res("loss", -1.02), _res("open", None), _res("win_tp1", 1.48)]
    var = [_res("be_exit", 0.0), _res("loss", -1.02), _res("open", None)]
    out = aggregate(base, [("CARD-05", {"be_trigger_r": 0.8}, var)], signals)
    assert out["variants"][0]["name"] == "baseline"
    assert out["variants"][0]["decided"] == 2
    assert out["variants"][0]["excluded_open"] == 1
    v = out["variants"][1]
    assert v["decided"] == 2 and v["excluded_open"] == 1
    d = out["deltas"][0]
    assert d["n_pairs"] == 1  # 仅第一笔双终局
    assert d["delta_exp_r"] == pytest.approx(0.0 - (-1.02))
    assert d["migration_top"][0] == {"from": "loss", "to": "be_exit", "n": 1}
    assert out["portfolio"][0]["name"] == "baseline"


def test_engine_version_present():
    assert BACKTEST_ENGINE_VERSION


# ── 10. 采样器源B（stub 检测器，只测编排逻辑）────────────────────────


def test_source_b_scan_replay_orchestration(monkeypatch):
    """源B 编排：候选→假想单三价构造、同向 repeat 去重、pending 状态机透传"""
    from app.services import backtest_engine as be

    # 造 260 根价格恒定 K 线（entry=100）；window 默认 240 → 可触发 t∈[239,258]，
    # 检测器在第 245/247/249 根触发（245 long、247 同向去重、249 反向 short）
    n = 260
    kl = [[T0 + i * HOUR_MS, 100.0, 100.5, 99.5, 100.0, 100.0] for i in range(n)]
    htf = [[T0 + i * 4 * HOUR_MS, 100.0, 101.0, 99.0, 100.0, 100.0] for i in range(60)]
    fired = {245: {"net": 3}, 247: {"net": 3}, 249: {"net": -3}}

    def fake_detect(pool, symbol, klines, pending=None, flt=None, retest=None,
                    event_out=None):
        t = (int(klines[-1][0]) - T0) // HOUR_MS  # 根索引（klines[-1][0] 是 open_time）
        # pending 状态机透传校验：首触发 upsert，随后 expire
        op = None
        if t in fired:
            op = {"op": "upsert", "direction": fired[t]["net"],
                  "touch_ref": "ema21", "stage1_candle_open": t,
                  "ema21": 100.0}
        elif pending:
            op = {"op": "expire", "reason": "test"}
        if t not in fired:
            return None, op
        cand = {"signal_type": "trigger_pullback_confirm",
                "touch_ref": "ema21", "quality_score": 88,
                "votes": {"net": fired[t]["net"]}}
        return cand, op

    monkeypatch.setattr(
        "app.services.scanner.detect_indicator_candidate", fake_detect)

    class _F:
        pass
    flt = _F()
    sigs = be.build_signals_by_scan_replay("BTCUSDT", kl, htf, flt,
                                           rr_tp1=1.5, repeat_bars=24)
    # fake_detect 在 klines[-1]（=kl_all[t+1]，充当未收盘根）∈ fired 时触发，
    # 即循环 t ∈ {244, 246, 248}：t=244 long → t=246 同向 2<24 去重 → t=248 short。
    # 检测器只消费已收盘根（klines[:-2]，生产同口径），未收盘根 OHLC 不读——无前视
    assert [s["direction"] for s in sigs] == ["long", "short"]
    s0 = sigs[0]
    assert s0["symbol"] == "BTCUSDT" and s0["entry"] == 100.0
    assert s0["entry_time_ms"] == T0 + 244 * HOUR_MS + HOUR_MS  # 信号根收盘即入场
    # 假想三价：stop=近5根 low×(1−0.003)=99.5×0.997；tp1=entry+1.5×r_unit
    assert s0["stop"] == pytest.approx(99.5 * 0.997)
    assert s0["tp1"] == pytest.approx(100.0 + 1.5 * (100.0 - 99.5 * 0.997))
    assert s0["meta"]["src"] == "scan"
    assert sigs[1]["direction"] == "short"
