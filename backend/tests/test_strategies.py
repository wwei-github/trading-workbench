# -*- coding: utf-8 -*-
"""EMA 趋势门单测（docs/19 §2，2026-10-10 融合拍板：影子层退役并入原生触发层）。

- ema_gate：gate_4h 三重否决（斜率走平/缠绕/ADX）与方向判定、structure_1h
  三线排列 + 快中线发散度 + EMA144 方向（2026-10-11 拍板中慢线发散度退役）；
  K 线格式 [ts, o, h, l, c, v]（与 ExchangePool 一致）；
- trigger 1H 结构门硬门（2026-10-12 拍板）：回踩 1h EMA21 触点须过 structure_1h
  （三线排列 + 快中线发散史 + EMA144 方向），不过则本次回踩不产生信号（硬门否决）；
  其余触点（retest/swing/4h EMA21/EMA55）不受此门约束。趋势升级标注
  trigger_trend_pullback 已下线（并入 trigger_pullback_confirm）；
- technical：KDJ / MACD 零轴状态（融合保留资产）+ 回踩企稳加分项（§3.3 条件2/3）。
"""
import sys
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import settings  # noqa: E402
from app.services.strategy import technical as tech  # noqa: E402
from app.services.strategy import trigger as trig  # noqa: E402
from app.services.strategy.ema import calc_ema  # noqa: E402
from app.services.strategy.ema_gate import (  # noqa: E402
    gate_4h,
    structure_1h,
)
from app.services.strategy.technical import (  # noqa: E402
    analyze_stochastic,
    compute_quality_score,
    macd_zero_state,
    pullback_stabilize_bonus,
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


# ── structure_1h：三线排列 + 快中线发散度 + EMA144 方向 ────────────────────
# 「EMA144 反向否决」不设专测（与 gate_4h 缠绕否决同理）：做多要求排列
# 21>55>144 成立而 EMA144 近 20 根严格下行，连价路径下是准矛盾态（价格中枢
# 须压在 144 下方而 EMA21 均值又在 144 上方，共存窗口 ≤ 数根，合成序列无法
# 构造且真实市场几乎不出现），方向判定由上涨/下跌两测的 slope144 断言覆盖


def test_structure_1h_bullish_aligned_passes():
    k1 = _bars([100.0 + 0.5 * i for i in range(300)], H1)
    r = structure_1h(k1, "long")
    assert r is not None
    _ind, ev = r
    assert ev["aligned"] is True
    assert ev["div_fast_mid"] >= 0.5
    assert ev["slope144_pct"] > 0  # 上涨序列 EMA144 向上，144 方向条件放行
    assert "div_mid_slow" in ev    # ms 降级为观测注记，仍在证据中


def test_structure_1h_bearish_aligned_passes():
    k1 = _bars([400.0 - 0.5 * i for i in range(300)], H1)
    r = structure_1h(k1, "short")
    assert r is not None and r[1]["aligned"] is True
    assert r[1]["slope144_pct"] < 0  # 镜像：下跌序列 144 向下，空头放行


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


def test_structure_1h_divergence_history_pullback_passes():
    """分离史口径（2026-10-10 三次拍板）：前期趋势段充分分离（div_fm 超阈值），
    末段深度回踩把 EMA21 拖贴 EMA55（现值不达标）——按近 12 根
    max 判定 → 放行。触点时刻现值口径下此场景必被误杀（EMA55 回踩核心场景）
    2026-10-11 拍板后中慢线发散度不再判定，早期趋势不再被 ms 腿拦截"""
    # 上升段 260 根（间距拉开），末 40 根深回踩（缓跌，EMA21/55 间距压缩）
    closes = [100.0 + 0.5 * i for i in range(260)]
    closes += [closes[-1] - 0.12 * i for i in range(1, 41)]
    k1 = _bars(closes, H1)
    r = structure_1h(k1, "long")
    assert r is not None
    _ind, ev = r
    # 快中腿分离史（max）达标，现值被回踩压缩——审计字段可见压缩量
    assert ev["div_fast_mid"] >= 0.5
    assert ev["div_fast_mid_now"] < ev["div_fast_mid"]
    assert ev["slope144_pct"] >= 0  # 缓跌 40 根不足以翻转 EMA144，方向放行


def test_structure_1h_no_divergence_history_rejected():
    """分离史口径反震荡不丢：排列对但整段黏着（12 根内无快中线分离史）→ None。
    现值口径下若末根恰好拉开会被放行，分离史口径堵住这个假趋势漏洞"""
    closes = [100.0] * 290
    closes += [100.0 + 0.06 * i for i in range(1, 11)]  # 仅末 10 根缓拉开
    k1 = _bars(closes, H1)
    r = structure_1h(k1, "long")
    if r is not None:  # 排列成立且快中腿 max 达标才可能过；黏着构造下 max 必不达标
        _ind, ev = r
        assert ev["div_fast_mid"] < 0.5
        assert r is None  # 恒等：该场景必须被拒


def test_structure_1h_history_window_expires():
    """分离史窗口时效（2026-10-10 三次拍板）：早期趋势分离 → 深度回踩消化 →
    排列仍成立但 EMA21 已贴穿到 EMA55 附近——「曾经分离」不等于「现在还在」，
    窗口过期后 max 反映的是回踩压缩值；本例构造 max 恰好不达标（紧贴+排列破
    边缘），验证窗口语义而非机械断言 None"""
    # 上升 288 根拉开间距 → 末 60 根快速深跌回踩（EMA21 被 price 拖向 EMA55，
    # 12 根窗口内 fm 全程压缩）
    closes = [100.0 + 0.5 * i for i in range(288)]
    closes += [closes[-1] - 0.55 * i for i in range(1, 61)]
    k1 = _bars(closes, H1)
    r = structure_1h(k1, "long")
    # 快速深跌下排列大概率已破（EMA21 下穿 EMA55）→ None；排列侥幸成立时
    # 窗口内 max 也被压缩 → 快中腿不达标
    if r is not None:
        _ind, ev = r
        assert ev["div_fast_mid"] < 0.5


def test_structure_1h_evidence_carries_now_snapshot():
    """证据带现值快照 + 窗口回显（审计/定标口径，docs/19 #15）"""
    k1 = _bars([100.0 + 0.5 * i for i in range(300)], H1)
    r = structure_1h(k1, "long")
    assert r is not None
    _ind, ev = r
    assert "div_fast_mid_now" in ev and "div_mid_slow_now" in ev
    assert "slope144_pct" in ev
    assert ev["div_lookback"] == 12


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
        # EMA21 末值，带内余量保证重算后触碰仍成立；实体/全长 0.4<0.6（趋势K）
        # 与 <0.70（突破K）→ 非长实体，突破分支零干扰
        kl[-2] = [ts, ema, ema + 0.3, ema - 0.2, ema + 0.2, vol, qv]
    else:
        c = ema + 0.4
        # 悬于 EMA21 上方（low > ema）且实体 0 → 非 solid，不触任何突破分支
        kl[-2] = [ts, c, c + 0.05, c - 0.05, c, vol, qv]
    return kl


def test_ema21_touch_structure_pass_triggers_plain_signal():
    """回踩 1h EMA21 触点 + 1H 结构门通过 → 普通回踩金K确认（已无趋势升级标注）"""
    kl = _pullback_klines_1h()
    with patch.object(trig, "detect_all_patterns", return_value=GOLDEN_LONG):
        t, op = trig.detect_trigger(kl, 1)
    assert t is not None and t["type"] == trig.PULLBACK_CONFIRM
    assert t["touch_ref"] == "ema21"
    assert "trend_gate" not in t          # 趋势升级标注已下线
    assert op is None                     # 当根确认无处置动作


def test_ema21_touch_structure_fail_vetoes_signal():
    """回踩 1h EMA21 触点命中但 1H 结构门不过 → 硬门否决：不产生信号，
    并返回 expire/structure_1h 动作（无 pending 时仅供 scanner 漏斗归因）"""
    kl = _pullback_klines_1h()
    with patch.object(trig, "detect_all_patterns", return_value=GOLDEN_LONG), \
         patch.object(trig, "structure_1h", return_value=None):
        t, op = trig.detect_trigger(kl, 1)
    assert t is None
    assert op == {"op": "expire", "reason": "structure_1h"}


def test_next_candle_confirm_with_pending():
    """行3 次根确认：悬空金K + 有效 pending → 普通回踩金K确认 + confirm"""
    kl = _pullback_klines_1h(touch=False)
    pending = {"id": "x", "direction": 1,
               "stage1_candle_open": datetime.utcfromtimestamp(kl[-3][0] / 1000)}
    with patch.object(trig, "detect_all_patterns", return_value=GOLDEN_LONG):
        t, op = trig.detect_trigger(kl, 1, pending=pending)
    assert t is not None and t["type"] == trig.PULLBACK_CONFIRM
    assert op == {"op": "confirm"}


def test_pending_ema21_touch_structure_fail_expires():
    """次根确认时刻结构门复校：pending 命中 ema21 触点但结构门不过 → pending 失效"""
    kl = _pullback_klines_1h(touch=False)
    pending = {"id": "x", "direction": 1, "touch_ref": "ema21",
               "stage1_candle_open": datetime.utcfromtimestamp(kl[-3][0] / 1000)}
    with patch.object(trig, "detect_all_patterns", return_value=GOLDEN_LONG), \
         patch.object(trig, "structure_1h", return_value=None):
        t, op = trig.detect_trigger(kl, 1, pending=pending)
    assert t is None
    assert op == {"op": "expire", "reason": "structure_1h"}


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


# ── 回踩企稳加分项（docs/19 §3.3 条件2/3，2026-10-10 拍板：加分不拦截）──────


def _stab_klines(touch_vol: float, base_vol: float = 1000.0) -> list[list]:
    """300 根稳步上行 1h（MACD 真算 = above/多头）：触碰根（closed[-3]，即
    kl[-4]）量 = touch_vol，其余 base_vol → 触碰根量比 = touch_vol / base_vol"""
    kl = []
    ts = 1_791_000_000_000
    price = 100.0
    for i in range(300):
        o = price
        price += 0.3
        vol = touch_vol if i == 296 else base_vol  # kl[-4] = closed[-3] 触碰根
        kl.append([ts + i * H1, o, price + 0.2, price - 0.2, price, vol, vol * 10])
    return kl


def test_stabilize_light_volume_and_macd_above(monkeypatch):
    """缩量回踩 +3、MACD 零轴上方同向 +4 → mod +7"""
    monkeypatch.setattr(tech, "macd_zero_state",
                        lambda k, **kw: {"state": "above", "dir": 1})
    ev = pullback_stabilize_bonus(_stab_klines(touch_vol=500.0), 1, touch_idx=-3)
    assert ev["mod"] == 7
    assert ev["touch_vol_ratio"] == 0.5
    assert "缩量回踩+3" in ev["items"] and "MACD零轴上方+4" in ev["items"]


def test_stabilize_heavy_volume_and_macd_against(monkeypatch):
    """放量回踩 -5（抛压重）、MACD 反向 -4 → mod -9"""
    monkeypatch.setattr(tech, "macd_zero_state",
                        lambda k, **kw: {"state": "below", "dir": -1})
    ev = pullback_stabilize_bonus(_stab_klines(touch_vol=2000.0), 1, touch_idx=-3)
    assert ev["mod"] == -9
    assert "放量回踩-5" in ev["items"] and "MACD动量不确认-4" in ev["items"]


def test_stabilize_pullback_hold_top_bonus(monkeypatch):
    """回踩零轴不破（同向最强企稳证据）+5；常量区间量比不加不减"""
    monkeypatch.setattr(tech, "macd_zero_state",
                        lambda k, **kw: {"state": "pullback_hold", "dir": 1})
    ev = pullback_stabilize_bonus(_stab_klines(touch_vol=1000.0), 1, touch_idx=-3)
    assert ev["mod"] == 5 and "回踩零轴不破+5" in ev["items"]


def test_stabilize_same_candle_skips_volume_semantics(monkeypatch):
    """当根确认（volume_semantics=False）：触碰根即金K根，量能已由放量加分
    评估——本项跳过（无量比），只算 MACD"""
    monkeypatch.setattr(tech, "macd_zero_state",
                        lambda k, **kw: {"state": "above", "dir": 1})
    ev = pullback_stabilize_bonus(_stab_klines(touch_vol=2000.0), 1, -2,
                                  volume_semantics=False)
    assert ev["mod"] == 4 and ev["touch_vol_ratio"] is None


def test_stabilize_short_direction_mirror(monkeypatch):
    """空头镜像：零轴下方 dir=-1 与做空同向 → +4"""
    monkeypatch.setattr(tech, "macd_zero_state",
                        lambda k, **kw: {"state": "below", "dir": -1})
    ev = pullback_stabilize_bonus(_stab_klines(touch_vol=1000.0), -1, touch_idx=-3)
    assert ev["mod"] == 4


def test_stabilize_insufficient_data_skips_items():
    """数据不足：触碰根前中位量不够（23 根内 touch_idx=-3 索引 <20）静默跳过
    量能项；MACD 需 ≥45 根已收盘同样跳过 → mod 0、无名目"""
    kl = _stab_klines(touch_vol=500.0)[:23]
    ev = pullback_stabilize_bonus(kl, 1, touch_idx=-3)
    assert ev is not None
    assert ev["touch_vol_ratio"] is None and ev["macd_zero"] is None
    assert ev["mod"] == 0 and ev["items"] == []


def test_trigger_stabilize_attached_both_paths():
    """行2/行3 触发 dict 均带 stabilize 证据：当根确认无量比（跳过量能语义）、
    次根确认有触碰根量比；上涨序列 MACD above 同向 → mod 含 +4"""
    # 行2 当根确认（触碰+金K 同根）
    kl = _pullback_klines_1h()
    with patch.object(trig, "detect_all_patterns", return_value=GOLDEN_LONG):
        t, _ = trig.detect_trigger(kl, 1)
    assert t["stabilize"] is not None
    assert t["stabilize"]["touch_vol_ratio"] is None       # volume_semantics=False
    assert t["stabilize"]["macd_zero"] == "above"
    assert t["stabilize"]["mod"] == 4

    # 行3 次根确认（有效 pending + 金K）
    kl2 = _pullback_klines_1h(touch=False)
    pending = {"id": "x", "direction": 1,
               "stage1_candle_open": datetime.utcfromtimestamp(kl2[-3][0] / 1000)}
    with patch.object(trig, "detect_all_patterns", return_value=GOLDEN_LONG):
        t2, op = trig.detect_trigger(kl2, 1, pending=pending)
    assert t2["stabilize"]["touch_vol_ratio"] is not None   # 量比正常评估
    assert t2["stabilize"]["macd_zero"] == "above"
    assert op == {"op": "confirm"}


def test_quality_score_applies_stabilize():
    """compute_quality_score 接线：stabilize.mod 进总分（加分不拦截）"""
    votes = {"bull": ["MACD"], "bear": [], "bull_n": 4, "bear_n": 0,
             "net": 4, "voted": 6, "results": [("MACD", {"dir": 1})]}
    base = compute_quality_score(1, votes, "trigger_pullback_confirm")
    up = compute_quality_score(1, votes, "trigger_pullback_confirm",
                               stabilize={"mod": 7})
    dn = compute_quality_score(1, votes, "trigger_pullback_confirm",
                               stabilize={"mod": -9})
    assert up == min(100, base + 7)
    assert dn == max(0, base - 9)
