# -*- coding: utf-8 -*-
"""EMA 趋势门单测（docs/19 §2，2026-10-10 融合拍板：影子层退役并入原生触发层）。

- ema_gate：gate_4h 三重否决（斜率走平/缠绕/ADX）与方向判定、structure_1h
  三线排列 + ATR 归一发散度；K 线格式 [ts, o, h, l, c, v]（与 ExchangePool 一致）；
- trigger 趋势升级：闸门+结构全过 → trigger_trend_pullback（质量分层标注），
  任一门不过/无 htf → 普通回踩金K确认（升级不拦截，行为零回归）；
- P2 出场变体（docs/19 §3.4）：backtest_engine 的 sig_exit/tp_qty_override 回测能力；
- technical：KDJ / MACD 零轴状态（融合保留资产）。
"""
import sys
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import settings  # noqa: E402
from app.services.backtest_engine import (  # noqa: E402
    _sig_exit_flags,
    replay_exit,
)
from app.services.strategy import trigger as trig  # noqa: E402
from app.services.strategy.ema import calc_ema  # noqa: E402
from app.services.strategy.ema_gate import (  # noqa: E402
    gate_4h,
    structure_1h,
)
from app.services.strategy.technical import (  # noqa: E402
    analyze_stochastic,
    macd_zero_state,
)

TS0 = 1_791_000_000_000
H1, H4 = 3_600_000, 14_400_000


def _bars(closes, ts_step, vol=1000.0):
    """closes → 6 列 K 线（o=前收盘、h=c+0.5、l=c-0.5）。"""
    out = []
    prev = float(closes[0])
    for i, c in enumerate(closes):
        c = float(c)
        out.append([TS0 + i * ts_step, prev, c + 0.5, c - 0.5, c, vol])
        prev = c
    return out


# ── gate_4h：4H EMA144 趋势闸门（三重否决）────────────────────────────────


def test_gate_4h_uptrend_passes_long():
    k4 = _bars([100.0 + 0.8 * i for i in range(300)], H4)
    g = gate_4h(k4)
    assert g is not None
    g_dir, ev = g
    assert g_dir == "long"
    assert ev["direction"] == "up"
    assert ev["slope_pct"] >= 0.005
    assert ev["wraps"] == 0
    assert ev["adx"] is not None and ev["adx"] >= 20.0


def test_gate_4h_downtrend_passes_short():
    k4 = _bars([400.0 - 0.8 * i for i in range(300)], H4)
    g = gate_4h(k4)
    assert g is not None and g[0] == "short" and g[1]["direction"] == "down"


def test_gate_4h_flat_market_rejected():
    """走平否决：EMA144 近 20 根变化 < 0.5% → None"""
    k4 = _bars([100.0 + 0.01 * (i % 5) for i in range(300)], H4)
    assert gate_4h(k4) is None


def test_gate_4h_insufficient_bars_rejected():
    k4 = _bars([100.0 + 0.8 * i for i in range(150)], H4)
    assert gate_4h(k4) is None


# 缠绕否决（wraps>2）无法与 ADX 弱隔离——缠绕序列必然压低 ADX，由
# test_gate_4h_flat_market_rejected 的"否决并集"行为覆盖，不设专测


# ── structure_1h：三线排列 + ATR 归一发散度 ────────────────────────────────


def test_structure_1h_bullish_aligned_passes():
    k1 = _bars([100.0 + 0.5 * i for i in range(300)], H1)
    r = structure_1h(k1, "long")
    assert r is not None
    _ind, ev = r
    assert ev["aligned"] is True
    assert ev["div_fast_mid"] >= 0.5
    assert ev["div_mid_slow"] >= 1.0


def test_structure_1h_bearish_aligned_passes():
    k1 = _bars([400.0 - 0.5 * i for i in range(300)], H1)
    r = structure_1h(k1, "short")
    assert r is not None and r[1]["aligned"] is True


def test_structure_1h_direction_mismatch_rejected():
    """多头排列语境问空头结构 → aligned False → None"""
    k1 = _bars([100.0 + 0.5 * i for i in range(300)], H1)
    assert structure_1h(k1, "short") is None


def test_structure_1h_choppy_rejected():
    """三线黏着横盘 → 排列不成立 → None"""
    k1 = _bars([100.0 + 0.01 * (i % 5) for i in range(300)], H1)
    assert structure_1h(k1, "long") is None


def test_structure_1h_insufficient_bars_rejected():
    k1 = _bars([100.0 + 0.5 * i for i in range(150)], H1)
    assert structure_1h(k1, "long") is None


# ── trigger 趋势升级（2026-10-10 融合：升级不拦截）─────────────────────────

GOLDEN_LONG = [{"pattern": "bullish_engulfing", "direction": "bullish"}]


@pytest.fixture(autouse=True)
def _pin_breakout_settings(monkeypatch):
    """固定突破门槛与新增上限，免疫本地 .env 覆盖（与 test_trigger_two_stage 同口径）"""
    monkeypatch.setattr(settings, "BREAKOUT_THRESHOLD", 0.002)
    monkeypatch.setattr(settings, "BREAKOUT_VOL_RATIO", 1.2)
    monkeypatch.setattr(settings, "BREAKOUT_MAX_PCT", 1.5)


def _kl7(closes, highs, lows):
    """7 列 K 线：[ts, o, h, l, c, v, qv]；o=前收，尾根未收盘"""
    out = []
    prev = closes[0]
    for i, c in enumerate(closes):
        out.append([TS0 + i * H1, prev, highs[i], lows[i], c, 100.0, 1e6])
        prev = c
    return out


def _pullback_klines_1h(touch: bool = True):
    """1h 强上涨 + 末段横盘回踩：信号K线（倒数第 2 根，已收盘）触碰 EMA21。

    触点设计：以 klines[:-1] 的 EMA21 末值作影线端点（touch=True 时 low=ema ≤
    close，触碰成立）；信号收盘低于横盘段摆动前高（≈204.49）与 20 根 Donchian
    上轨（≈204.79）→ 突破分支零干扰。touch=False 时信号K线悬于 EMA21 上方
    （行3 次根确认路径用）。
    """
    closes = [100.0 + 0.5 * i for i in range(210)]
    closes += [closes[-1] - 0.01 * (i % 5) for i in range(19)]   # 横盘已收盘 19 根
    closes.append(closes[-1] + 0.01)                              # 尾根（未收盘）
    highs = [c + 0.3 for c in closes]
    lows = [c - 0.3 for c in closes]
    kl = _kl7(closes, highs, lows)
    e21 = calc_ema([float(k[4]) for k in kl[:-1]], 21)
    ema = float(e21[-1])
    ts, _, _, _, _, vol, qv = kl[-2]
    if touch:
        # 下影触 EMA21、收盘收回线上（触碰判定 low ≤ ema ≤ close）。
        # low/close 各留 0.2 余量：改写信号K线收盘会挪动 klines[:-1] 重算的
        # EMA21 末值，带内余量保证重算后触碰仍成立；实体/全长 0.4<0.7 →
        # 非长实体，突破分支零干扰
        kl[-2] = [ts, ema, ema + 0.3, ema - 0.2, ema + 0.2, vol, qv]
    else:
        c = ema + 0.4
        # 悬于 EMA21 上方（low > ema）且实体 0 → 非 solid，不触任何突破分支
        kl[-2] = [ts, c, c + 0.05, c - 0.05, c, vol, qv]
    return kl


def _htf_up() -> list[list]:
    """4h 强上涨（240 根，detect_trigger 内取前 239 根已收盘判定）"""
    return _bars([100.0 + 2.0 * i for i in range(240)], H4)


def test_trend_pullback_upgrade_on_gate_pass():
    """闸门+结构全过 → 升级 trend_pullback：type/trend_gate/reason 三处标注"""
    kl = _pullback_klines_1h()
    with patch.object(trig, "detect_all_patterns", return_value=GOLDEN_LONG):
        t, op = trig.detect_trigger(kl, 1, htf=_htf_up())
    assert t is not None and t["type"] == trig.TREND_PULLBACK_CONFIRM
    assert t["trend_gate"] is not None
    assert t["trend_gate"]["direction"] == "up"
    assert t["trend_gate"]["structure_1h"]["aligned"] is True
    assert "4H趋势闸门+1H结构通过" in t["reason"]
    assert op is None          # 无 pending：当根确认无处置动作


def test_next_candle_confirm_upgrade_with_pending():
    """行3 次根确认同样升级：闸门全过 + 有效 pending → trend_pullback + confirm"""
    kl = _pullback_klines_1h(touch=False)
    pending = {"id": "x", "direction": 1,
               "stage1_candle_open": datetime.utcfromtimestamp(kl[-3][0] / 1000)}
    with patch.object(trig, "detect_all_patterns", return_value=GOLDEN_LONG):
        t, op = trig.detect_trigger(kl, 1, pending=pending, htf=_htf_up())
    assert t is not None and t["type"] == trig.TREND_PULLBACK_CONFIRM
    assert t["trend_gate"] is not None
    assert op == {"op": "confirm"}


def test_pullback_without_htf_stays_plain():
    """htf=None（4h 拉取失败）→ 不升级，普通回踩金K确认（行为零回归）"""
    kl = _pullback_klines_1h()
    with patch.object(trig, "detect_all_patterns", return_value=GOLDEN_LONG):
        t, op = trig.detect_trigger(kl, 1)
    assert t is not None and t["type"] == trig.PULLBACK_CONFIRM
    assert t["trend_gate"] is None
    assert "4H趋势闸门" not in t["reason"]


def test_trend_gate_direction_mismatch_stays_plain():
    """4h 闸门方向与信号方向相反（多头信号遇空头闸门）→ 不升级"""
    kl = _pullback_klines_1h()
    htf = _bars([500.0 - 2.0 * i for i in range(240)], H4)   # 4h 下跌 → short
    with patch.object(trig, "detect_all_patterns", return_value=GOLDEN_LONG):
        t, _ = trig.detect_trigger(kl, 1, htf=htf)
    assert t is not None and t["type"] == trig.PULLBACK_CONFIRM
    assert t["trend_gate"] is None


def test_trend_gate_flat_4h_stays_plain():
    """4h 走平（闸门三重否决）→ 不升级，回落普通回踩"""
    kl = _pullback_klines_1h()
    htf = _bars([100.0 + 0.01 * (i % 5) for i in range(240)], H4)
    with patch.object(trig, "detect_all_patterns", return_value=GOLDEN_LONG):
        t, _ = trig.detect_trigger(kl, 1, htf=htf)
    assert t is not None and t["type"] == trig.PULLBACK_CONFIRM
    assert t["trend_gate"] is None


# ── P2 出场变体（docs/19 §3.4）：backtest_engine overrides 回测能力 ────────


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


def test_baseline_unaffected_by_new_keys():
    """新键缺省 None 时基线分批与既有口径完全一致（硬门不漂移）"""
    bars = _trend_bars()
    r = replay_exit("long", 100.0, 95.0, 107.5, 112.5, bars)
    assert [l["name"] for l in r["legs"][:2]] == ["tp1", "tp2"]
    assert r["legs"][0]["qty_pct"] == pytest.approx(0.60)
    assert r["legs"][1]["qty_pct"] == pytest.approx(0.20)


# ── 技术指标（融合保留资产）：KDJ / MACD 零轴 ─────────────────────────────


def _mk_klines(closes, h=None, l=None, vol=100.0):
    """closes → K 线（高低默认 ±0.5）。"""
    out = []
    for i, c in enumerate(closes):
        hi = (h[i] if h else c + 0.5)
        lo = (l[i] if l else c - 0.5)
        out.append([1791000000000 + i * 3600000, c, hi, lo, c, vol])
    return out


def test_macd_zero_state_directions():
    """上升段零轴上方；深跌段零轴下方；平盘调用不崩"""
    up = macd_zero_state(_mk_klines([100 + i * 1.0 for i in range(80)]))
    assert up and up["state"] == "above" and up["dir"] == 1
    dn = macd_zero_state(_mk_klines([200 - i * 1.0 for i in range(80)]))
    assert dn and dn["state"] == "below" and dn["dir"] == -1
    macd_zero_state(_mk_klines([100.0] * 40))  # 平盘不崩即可


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
