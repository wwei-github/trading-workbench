# -*- coding: utf-8 -*-
"""回踩触点体系判定单测（docs/22，CI 一票否决）

覆盖 trigger 三个触点探测器 + 优先级标注 + 启用白名单 + 配置接线：
- retest：双来源（2026-10-16）——①事件派生（方向一致 / 窗口内 / 带宽）；②结构推导
  （新 HH 收盘越位后，被它越过的前一个 HH 即回测位，空头镜像 LL）——均须影线入带、
  收盘守住；事件缺失/过期/方向不符时回落结构推导
- swing：只认 HL（多）/LH（空）——LL/HH 不构成回踩结构；时效门 bars_ago ≤ 40
- ema55：数据门（已收盘 < ema_touch_min_bars 静默跳过）+ 排列门 + 斜率门
- 触点优先级 retest > swing > ema21；BREAKOUT 触发携带 breakout_level/candle_open
  （scanner 记 breakout_events 的事实源）；次根确认触点沿用 pending.touch_ref
- resolve_scan_filters 触点白名单/参数解析（空/全非法回落默认，不停摆）
- PUT /config 校验器触点键（空列表/非法项 400）

K线夹具与 test_trigger_two_stage 同构（7 列，klines[-1] 未收盘）；
autouse monkeypatch 固定 settings 基线，免疫本地 .env 覆盖。
"""
import sys
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import settings  # noqa: E402
from app.services.scan_filters import resolve_scan_filters  # noqa: E402
from app.services.strategy import trigger as trig  # noqa: E402
from app.services.strategy.ema import calc_ema  # noqa: E402

TS0 = 1716000000000  # ms
STEP = 3_600_000     # 1h
CHAN = 20


@pytest.fixture(autouse=True)
def _pin_settings(monkeypatch):
    monkeypatch.setattr(settings, "BREAKOUT_THRESHOLD", 0.002)
    monkeypatch.setattr(settings, "BREAKOUT_VOL_RATIO", 1.2)
    monkeypatch.setattr(settings, "BREAKOUT_CHANNEL_BARS", 20)
    monkeypatch.setattr(settings, "BREAKOUT_MAX_PCT", 1.5)
    monkeypatch.setattr(settings, "CANDLE_MAX_EMA21_DIST_PCT", 1.5)
    monkeypatch.setattr(settings, "RETEST_WINDOW_BARS", 24)
    monkeypatch.setattr(settings, "STRUCTURE_TOUCH_BAND_PCT", 0.3)
    monkeypatch.setattr(settings, "EMA_TOUCH_MIN_BARS", 200)


@pytest.fixture(autouse=True)
def _stub_1h_structure_gate(monkeypatch):
    """1H 结构门（2026-10-12 起为 ema21 触点硬门）打桩为「通过」。

    本文件聚焦触点探测/优先级/白名单，_base 夹具仅 60 根（< structure_1h 要求的
    200 根）无法自然过门——注入通过态隔离；结构门否决与非 ema21 触点豁免在
    test_strategies.py / test_structure_gate_exempts_non_ema21_touch 专测。
    """
    monkeypatch.setattr(trig, "structure_1h",
                        lambda k1, direction, cfg=None: ({"d21": 1.0}, {"aligned": True}))


def _klines(closes: list[float], highs: list[float], lows: list[float]) -> list[list]:
    out = []
    prev = closes[0]
    for i, c in enumerate(closes):
        out.append([TS0 + i * STEP, prev, highs[i], lows[i], c, 100.0, 1e6])
        prev = c
    return out


def _base(n: int = 60, base: float = 100.0) -> tuple[list, list, list]:
    closes = [base + 0.01 * (i % 5) for i in range(n)]
    highs = [c + 0.3 for c in closes]
    lows = [c - 0.3 for c in closes]
    return closes, highs, lows


def _kl(closes, highs, lows) -> list[list]:
    return _klines(closes, highs, lows)


def _flt(refs=("ema21",), **over):
    """显式 ScanFilters：touch 白名单可指定，其余按默认基线"""
    from app.services.scan_filters import ScanFilters
    kw = dict(
        min_volume_24h=3_000_000, breakout_channel_bars=CHAN,
        breakout_threshold=0.002, breakout_vol_ratio=1.2,
        breakout_max_pct=1.5, candle_max_ema21_dist_pct=1.5,
        ai_max_per_scan=10,
        pullback_touch_refs=tuple(refs),
    )
    kw.update(over)
    return ScanFilters(**kw)


def _retest_event(kl: list[list], price: float, direction: int = 1,
                  open_idx: int = -5) -> dict:
    """scanner 侧活跃破轨事件快照（事件K线默认取 klines[-5]，窗口内）"""
    return {"price": price, "direction": direction,
            "candle_open": datetime.utcfromtimestamp(kl[open_idx][0] / 1000)}


# 2026-09-29-2 复合确认：单影线锤不再单根确认，打桩改用复合形态（吞没）直通
GOLDEN_LONG = [{"pattern": "bullish_engulfing", "direction": "bullish"}]


# ── retest：突破回测位（docs/22 §3.1） ────────────────────────────────────

def test_retest_hit_long():
    """多头：下影入带（l ≤ lvl×(1+band)）、收盘守住（c ≥ lvl）→ 命中位价"""
    kl = _kl(*_base())
    lvl = 99.5
    ts, o, _, _, _, vol, qv = kl[-2]
    kl[-2] = [ts, o, o + 0.5, lvl - 0.1, lvl + 0.4, vol, qv]
    assert trig._detect_retest(kl, 1, _retest_event(kl, lvl), 24, 0.3) == pytest.approx(lvl)


def test_retest_hit_short_symmetric():
    """空头：上影入带（h ≥ lvl×(1-band)）、收盘压在位下（c ≤ lvl）"""
    kl = _kl(*_base())
    lvl = 100.5
    ts, o, _, _, _, vol, qv = kl[-2]
    kl[-2] = [ts, o, lvl + 0.1, o - 0.5, lvl - 0.4, vol, qv]
    assert trig._detect_retest(kl, -1, _retest_event(kl, lvl, direction=-1),
                               24, 0.3) == pytest.approx(lvl)


def test_retest_direction_mismatch_ignored():
    """反向破轨事件对当前候选无效"""
    kl = _kl(*_base())
    assert trig._detect_retest(kl, 1, _retest_event(kl, 99.5, direction=-1),
                               24, 0.3) is None


def test_retest_close_below_level_rejected():
    """收盘跌破被测轨 = 突破失败，不构成回测"""
    kl = _kl(*_base())
    lvl = 99.5
    ts, o, _, _, _, vol, qv = kl[-2]
    kl[-2] = [ts, o, o + 0.5, lvl - 0.1, lvl - 0.2, vol, qv]
    assert trig._detect_retest(kl, 1, _retest_event(kl, lvl), 24, 0.3) is None


def test_retest_out_of_window_rejected():
    """事件K线早于最近 window_bars 根 → 回测语义过期"""
    kl = _kl(*_base())
    lvl = 99.5
    ts, o, _, _, _, vol, qv = kl[-2]
    kl[-2] = [ts, o, o + 0.5, lvl - 0.1, lvl + 0.4, vol, qv]
    # window=24：合法范围 klines[-26..-2]；klines[-30] 开盘太老
    assert trig._detect_retest(kl, 1, _retest_event(kl, lvl, open_idx=-30),
                               24, 0.3) is None
    # 窗口放宽到 30 → 同一事件合法
    assert trig._detect_retest(kl, 1, _retest_event(kl, lvl, open_idx=-30),
                               30, 0.3) == pytest.approx(lvl)


def test_retest_window_zero_disables():
    kl = _kl(*_base())
    assert trig._detect_retest(kl, 1, _retest_event(kl, 99.5), 0, 0.3) is None


def test_retest_without_event_none():
    assert trig._detect_retest(_kl(*_base()), 1, None, 24, 0.3) is None


# ── retest 来源②：结构推导回测位（需求①，2026-10-16）────────────────────

def _zig(anchors: list, n: int) -> list:
    """按 (idx, close) 锚点线性插值出 n 根收盘序列（相邻锚点间单调线性，
    段内无并列极值 → 摆动点唯一）"""
    out = []
    for i in range(n):
        for j in range(len(anchors) - 1):
            i0, v0 = anchors[j]
            i1, v1 = anchors[j + 1]
            if i0 <= i <= i1:
                t = (i - i0) / (i1 - i0) if i1 > i0 else 0.0
                out.append(v0 + (v1 - v0) * t)
                break
        else:
            out.append(anchors[-1][1])
    return out


def _mirror(kl: list) -> list:
    """价格轴 200 镜像（高点↔低点、开↔收）：多头夹具 → 空头镜像"""
    return [[k[0], 200 - k[1], 200 - k[3], 200 - k[2], 200 - k[4], k[5], k[6]]
            for k in kl]


def _struct_long_kl() -> list:
    """结构推导多头夹具（order=3，收盘摆动高点 H1=99.5 → B=100.0(HH) →
    A=101.0(HH)，A 为最近新 HH bars_ago=10）；信号K线（idx38）下探 B=100.0
    入带、收盘 100.4 守住 → 结构推导回测位应命中 100.0"""
    closes = _zig([(0, 98.4), (3, 98.0), (8, 99.5), (13, 98.5),
                   (18, 100.0), (23, 99.0), (28, 101.0), (38, 100.4)], 40)
    kl = _klines(closes, [c + 0.3 for c in closes], [c - 0.3 for c in closes])
    ts, o, _, _, _, vol, qv = kl[-2]
    kl[-2] = [ts, o, 100.5, 100.1, 100.4, vol, qv]
    return kl


def _struct_unbreached_kl() -> list:
    """伪越位夹具：收盘摆动高点依次 102.0(H) → 101.0(LH) → 100.0(LH) →
    100.5(HH)——最近新 HH 只高于前一摆动高点，并未越过更旧的 102.0/101.0，
    无「被越过的前一个 HH」→ 结构推导 None"""
    closes = _zig([(0, 99.0), (3, 98.5), (7, 102.0), (11, 100.5), (15, 101.0),
                   (19, 99.5), (23, 100.0), (27, 99.0), (31, 100.5), (43, 99.5)], 45)
    return _klines(closes, [c + 0.3 for c in closes], [c - 0.3 for c in closes])


def test_retest_structural_hit_long():
    """需求①：新 HH(101.0) 越过前一个 HH(100.0)，信号K线下探 100.0 入带、收盘
    守住 → 事件缺失（retest=None）也能命中回测位 100.0"""
    assert trig._detect_retest(_struct_long_kl(), 1, None, 24, 0.3) == pytest.approx(100.0)


def test_retest_structural_hit_short_mirror():
    """空头镜像：新 LL 越过前一个 LL，反抽入带、收盘压在位下 → 命中同一价位"""
    assert trig._detect_retest(_mirror(_struct_long_kl()), -1, None, 24, 0.3) \
        == pytest.approx(100.0)


def test_retest_structural_new_hh_out_of_window():
    """时效门复用 retest_window_bars：新 HH bars_ago=10 > window=5 → 不推导（None）"""
    assert trig._detect_retest(_struct_long_kl(), 1, None, 5, 0.3) is None


def test_retest_structural_close_below_level():
    """结构推导仍要收盘守位：信号K线下探 100.0 入带但收盘 99.8 < 位价 → None
    （防放水：扩覆盖面而非降低守位口径）"""
    kl = _struct_long_kl()
    ts, o, _, _, _, vol, qv = kl[-2]
    kl[-2] = [ts, o, 100.5, 100.1, 99.8, vol, qv]
    assert trig._detect_retest(kl, 1, None, 24, 0.3) is None


def test_retest_structural_skips_unbreached_hh():
    """伪越位排除：最近新 HH 只高于前一 LH、未高于更旧高位 → 无 B 可推导 → None"""
    assert trig._detect_retest(_struct_unbreached_kl(), 1, None, 24, 0.3) is None


def _struct_two_highs_kl() -> list:
    """边界夹具（2026-10-16 放宽项）：整个回看窗只有 2 个收盘摆动高点——
    老 100.0 标 "H"（序列末位、无前参照）、新 101.0 标 "HH"；信号K线下探
    100.0 入带、收盘 100.4 守住。放宽前 B 须严格为 HH → 推导不出；放宽后
    B 接受末位 "H" → 回测位 100.0"""
    closes = _zig([(0, 95.0), (5, 100.0), (10, 96.0), (15, 101.0), (20, 97.0)], 25)
    kl = _klines(closes, [c + 0.3 for c in closes], [c - 0.3 for c in closes])
    ts, o, _, _, _, vol, qv = kl[-2]
    kl[-2] = [ts, o, 100.6, 100.1, 100.4, vol, qv]
    return kl


def test_retest_structural_terminal_h_counts_as_prev_high():
    """放宽项：末位 "H" 无前参照、标签不足以否定「前高」身份，视同被 A 越过的
    前高——仅 2 个摆动高点时也能命中回测位 100.0（放宽前为 None）"""
    kl = _struct_two_highs_kl()
    labels = [h["label"] for h in trig.recent_swings(kl[:-1], order=3, n=10)["highs"]]
    assert labels == ["HH", "H"]          # 夹具前提：新=HH、末位=H（仅 2 个摆动高点）
    assert trig._detect_retest(kl, 1, None, 24, 0.3) == pytest.approx(100.0)


def test_retest_structural_terminal_h_short_mirror():
    """空头镜像：末位 "L" 视同被新 LL 越过的前低 → 命中同一价位"""
    assert trig._detect_retest(_mirror(_struct_two_highs_kl()), -1, None, 24, 0.3) \
        == pytest.approx(100.0)


# ── swing：摆动结构位（docs/22 §3.2） ────────────────────────────────────

def _swing_uptrend_kl() -> list[list]:
    """两段回撤递升的上升序列（n=60）：谷底 99.6@idx17 → 103.0@idx48（HL，
    bars_ago=11）；信号K线（idx58）下探触碰 HL 103.0、收盘守住"""
    closes = [100.0 + 0.1 * i for i in range(15)]                  # 100.0→101.4
    closes += [100.8, 100.2, 99.6, 100.2, 100.8, 101.4, 102.0]     # 谷底99.6@17
    closes += [102.8 + 0.1 * i for i in range(24)]                 # idx22→45: 102.8→105.5
    closes += [104.6, 103.8, 103.0, 103.8, 104.6, 105.4]           # 谷底103.0@48
    closes += [106.2, 106.4, 106.6, 106.8]                         # idx52→55
    closes += [107.0, 106.8]                                       # idx56,57
    closes += [103.4]                                              # idx58 信号K线（回踩 HL）
    closes += [103.6]                                              # idx59 未收盘
    highs = list(closes)
    lows = list(closes)
    kl = _klines(closes, highs, lows)
    # 信号K线：下影触及 103.0、收盘 103.4 守住（open 沿用前收约定会形成大阴线，无碍）
    ts, _, _, _, _, vol, qv = kl[-2]
    kl[-2] = [ts, 106.8, 103.6, 103.0, 103.4, vol, qv]
    return kl


def _swing_downtrend_kl() -> list[list]:
    """镜像序列：摆动高点 100.4@17 → 97.0@48（LH），信号K线上反抽触碰 LH"""
    kl = _swing_uptrend_kl()
    return [[k[0], 200 - k[1], 200 - k[3], 200 - k[2], 200 - k[4], k[5], k[6]]
            for k in kl]


def test_swing_touch_long_hits_hl():
    assert trig._detect_swing_touch(_swing_uptrend_kl(), 1, 0.3, 3) == pytest.approx(103.0)


def test_swing_touch_short_hits_lh():
    assert trig._detect_swing_touch(_swing_downtrend_kl(), -1, 0.3, 3) == pytest.approx(97.0)


def _swing_shallow_dip_kl() -> list[list]:
    """深谷 99.6@17（bars_ago=42 超时效门）+ 浅坑 104.0/104.5@48-50 序列（n=60）：
    阶数 1 认 104.5@50（两侧各 1 根确认的浅摆动，HL）；阶数 3 下 104.5 左窗含
    更低点 104.0 不算 pivot，最近摆动点是 104.0@48——信号K线（idx58）下探 104.5、
    收盘 104.5：前者被触碰命中，后者未触碰（低点高于位价）→ None"""
    closes = [100.0 + 0.1 * i for i in range(15)]                  # 100.0→101.4
    closes += [100.8, 100.2, 99.6, 100.2, 100.8, 101.4, 102.0]     # 深谷99.6@17
    closes += [102.8 + 0.1 * i for i in range(26)]                 # idx22→47: 102.8→105.3
    closes += [104.0]                                              # idx48 浅坑 A
    closes += [104.6]                                              # idx49
    closes += [104.5]                                              # idx50 浅坑 B（阶数1才认）
    closes += [104.7, 104.9]                                       # idx51→52
    closes += [105.1 + 0.1 * i for i in range(5)]                  # idx53→57: 105.1→105.5
    closes += [104.5]                                              # idx58 信号K线（下探 104.5）
    closes += [105.0]                                              # idx59
    kl = _klines(closes, list(closes), list(closes))
    ts, _, _, _, _, vol, qv = kl[-2]
    kl[-2] = [ts, 105.5, 105.6, 104.5, 104.5, vol, qv]
    return kl


def test_swing_touch_order_follows_config():
    """摆动点阶数跟随配置（2026-10-12）：阶数 1 认更近的浅摆动位 104.5（触碰命中），
    阶数 3 只认 104.0（信号K线未触碰）→ None——检测层不再硬编码 3"""
    kl = _swing_shallow_dip_kl()
    assert trig._detect_swing_touch(kl, 1, 0.3, 1) == pytest.approx(104.5)
    assert trig._detect_swing_touch(kl, 1, 0.3, 3) is None


def test_swing_touch_ignores_ll_for_long():
    """LL 不构成回踩结构（接刀）：谷底递降（104.4→104.0）→ 多头不认"""
    closes = [105.0, 104.8, 104.6, 104.4, 104.6, 104.8, 105.0]     # 谷底104.4@3（L）
    closes += [105.0 + 0.1 * i for i in range(33)]                 # idx6→38: 105.0→108.2
    closes += [105.5, 104.7, 104.0, 104.7, 105.5, 106.3, 107.1]    # 谷底104.0@41（LL）
    closes += [107.9 + 0.1 * i for i in range(12)]                 # idx46→57
    closes += [108.6]                                              # idx58 信号K线（触碰 104.0）
    closes += [108.8]                                              # idx59
    kl = _klines(closes, list(closes), list(closes))
    ts, _, _, _, _, vol, qv = kl[-2]
    kl[-2] = [ts, 109.0, 108.8, 104.0, 108.6, vol, qv]
    assert trig._detect_swing_touch(kl, 1, 0.3, 3) is None


def test_swing_touch_stale_hl_ignored():
    """时效门：最新 HL bars_ago=46 > 40 → 防守意义衰减，不再触碰"""
    closes = [100.0, 99.8, 99.6, 98.9, 99.6, 99.8, 100.0]          # 谷底98.9@3（L）
    closes += [100.0 + 0.1 * i for i in range(4)]                  # idx7→10: 100.0→100.3
    closes += [100.0, 99.5, 99.1, 99.5, 100.0, 100.5, 101.0]       # 谷底99.1@13（HL，bars_ago=46）
    closes += [101.8 + 0.1 * i for i in range(40)]                 # idx18→57: 101.8→105.7
    closes += [105.3]                                              # idx58 信号K线（下探 99.1）
    closes += [105.5]                                              # idx59
    kl = _klines(closes, list(closes), list(closes))
    ts, _, _, _, _, vol, qv = kl[-2]
    kl[-2] = [ts, 105.7, 105.5, 99.1, 105.3, vol, qv]
    assert trig._detect_swing_touch(kl, 1, 0.3, 3) is None


# ── swing 三候选（2026-10-16 用户拍板需求②）──────────────────────────────
# 递升 HL 序列（旧→新）：95.0@4(L) / 96.0@15(HL,ago=30) / 97.0@27(HL,ago=18)
# / 98.0@38(HL,ago=7)；信号K线 idx44。取最近 3 个 HL 候选（新→旧：98.0/97.0/96.0），
# 按序返回最先满足「影线入带+收盘守位」者。

def _swing_three_hl_kl() -> list[list]:
    closes = _zig([(0, 96.0), (4, 95.0), (10, 99.0), (15, 96.0), (21, 99.5),
                   (27, 97.0), (33, 100.0), (38, 98.0), (42, 100.5),
                   (44, 97.5), (45, 97.5)], 46)
    return _klines(closes, [c + 0.3 for c in closes], [c - 0.3 for c in closes])


def _with_signal(kl: list, o: float, h: float, l: float, c: float) -> list:
    ts, _, _, _, _, vol, qv = kl[-2]
    kl[-2] = [ts, o, h, l, c, vol, qv]
    return kl


def test_swing_touch_second_hl_hit_when_nearest_fails():
    """需求②：最近 HL(98.0) 收盘未守住（close 97.5 < 98.0）→ 回落第 2 候选
    HL(97.0)：下影入带（low 96.9 ≤ 97.0×1.003）、收盘守住（97.5 ≥ 97.0）→ 命中 97.0"""
    kl = _with_signal(_swing_three_hl_kl(), 99.0, 99.1, 96.9, 97.5)
    assert trig._detect_swing_touch(kl, 1, 0.3, 3) == pytest.approx(97.0)


def test_swing_touch_nearest_hl_priority():
    """候选按新→旧优先：最近 HL(98.0) 也满足触碰时返回它，不越级取更旧位"""
    kl = _with_signal(_swing_three_hl_kl(), 98.0, 98.5, 97.6, 98.2)
    assert trig._detect_swing_touch(kl, 1, 0.3, 3) == pytest.approx(98.0)


def test_swing_touch_third_hl_fallback():
    """需求②兜底：第 1/2 候选收盘均未守住（close 96.5 < 97.0 < 98.0）→ 第 3 候选
    HL(96.0)：下影入带、收盘守住 → 命中 96.0"""
    kl = _with_signal(_swing_three_hl_kl(), 96.0, 96.6, 95.9, 96.5)
    assert trig._detect_swing_touch(kl, 1, 0.3, 3) == pytest.approx(96.0)


def test_swing_touch_second_lh_hit_mirror():
    """空头镜像：最近 LH(102) 收盘未压在位下（close 102.5 > 102）→ 第 2 候选
    LH(103)：上影入带、收盘压在位下（102.5 ≤ 103）→ 命中 103.0"""
    kl = _mirror(_with_signal(_swing_three_hl_kl(), 99.0, 99.1, 96.9, 97.5))
    assert trig._detect_swing_touch(kl, -1, 0.3, 3) == pytest.approx(103.0)


# ── ema55：数据门 / 排列门 / 斜率门（docs/22 §3.3） ───────────────────────

def _pullback_to_ema_kl(n: int, period: int, rising: bool = True) -> list[list]:
    """趋势序列 + 信号K线回踩指定 EMA（low=ema-0.2、close=ema+0.3；
    改写收盘引起的 EMA 漂移（±0.08 内）不破坏触碰区间）"""
    step = 0.05 if rising else -0.05
    base = 100.0 if rising else 200.0
    closes = [base + step * i for i in range(n)]
    highs = [c + 0.1 for c in closes]
    lows = [c - 0.1 for c in closes]
    kl = _klines(closes, highs, lows)
    e = calc_ema([float(k[4]) for k in kl[:-1]], period)
    ema = float(e[-1])
    ts, _, _, _, _, vol, qv = kl[-2]
    kl[-2] = [ts, kl[-3][4], ema + 0.4, ema - 0.2, ema + 0.3, vol, qv]
    return kl


def test_ema55_touch_uptrend_hit():
    """上升序列回踩 ema55：数据/排列/斜率三门全过 → 命中"""
    kl = _pullback_to_ema_kl(210, 55, rising=True)
    p = trig._detect_ema_touch(kl, 1, 55, _flt(refs=("ema55",)))
    assert p is not None and p > 0


def test_ema55_data_gate_skips_short_history():
    """数据门：已收盘 59 根 < 200 → 静默跳过（把震荡当回踩的防线之一）"""
    kl = _pullback_to_ema_kl(60, 55, rising=True)
    assert trig._detect_ema_touch(kl, 1, 55, _flt(refs=("ema55",))) is None


def test_ema55_data_gate_lower_threshold_hits():
    """数据门可调：99 根已收盘在默认 200 下被拦；ema_touch_min_bars=50 时放行
    （注意 55 期仍有 周期×1.7≈93 根的硬下限，防 EMA 未收敛的假触点；
    显式 ScanFilters 不读 settings，直接传字段）"""
    kl = _pullback_to_ema_kl(100, 55, rising=True)
    assert trig._detect_ema_touch(kl, 1, 55, _flt(refs=("ema55",))) is None  # 默认 200
    assert trig._detect_ema_touch(
        kl, 1, 55, _flt(refs=("ema55",), ema_touch_min_bars=50),
    ) is not None


def test_ema55_arrangement_gate_blocks_downtrend_long():
    """下降序列多头触碰 ema55：快线在慢线下方（排列门）+ 斜率向下（斜率门）→ None"""
    kl = _pullback_to_ema_kl(210, 55, rising=False)
    assert trig._detect_ema_touch(kl, 1, 55, _flt(refs=("ema55",))) is None


# ── ema144：深回撤（2026-10-16 复出，「1h EMA144 走平或顺 4h 方向」门）─────

def _ema144_kl(n: int, shape: str) -> list[list]:
    """ema144 夹具：shape='up' 上升 / 'down' 下降 / 'flat' 走平；信号K线触及 EMA144
    （多头 low=ema-0.2、close=ema+0.3；做空镜像另改写，见各用例）"""
    if shape == "up":
        closes = [100.0 + 0.05 * i for i in range(n)]
    elif shape == "down":
        closes = [200.0 - 0.05 * i for i in range(n)]
    else:
        closes = [100.0] * n
    kl = _klines(closes, [c + 0.1 for c in closes], [c - 0.1 for c in closes])
    return kl


def _touch_ema144(kl: list[list], direction: int) -> list[list]:
    e = calc_ema([float(k[4]) for k in kl[:-1]], 144)
    ema = float(e[-1])
    ts, _, _, _, _, vol, qv = kl[-2]
    if direction == 1:
        kl[-2] = [ts, kl[-3][4], ema + 0.4, ema - 0.2, ema + 0.3, vol, qv]
    else:
        kl[-2] = [ts, kl[-3][4], ema + 0.2, ema - 0.5, ema - 0.3, vol, qv]
    return kl


def test_ema144_touch_uptrend_long_hits():
    """上升序列（EMA144 向上）多头深回撤触碰 → 数据门 + 走平/顺势门全过 → 命中"""
    kl = _touch_ema144(_ema144_kl(250, "up"), 1)
    p = trig._detect_ema_touch(kl, 1, 144, _flt(refs=("ema144",)))
    assert p is not None and p > 0


def test_ema144_touch_downtrend_long_rejected():
    """下降序列（EMA144 向下）多头触碰 → 顺势门不过（4h 多要求 EMA144 走平或向上）→ None"""
    kl = _touch_ema144(_ema144_kl(250, "down"), 1)
    assert trig._detect_ema_touch(kl, 1, 144, _flt(refs=("ema144",))) is None


def test_ema144_touch_downtrend_short_hits_mirror():
    """空头镜像：下降序列（EMA144 向下）空头反抽触碰 → 顺势门过（走平或向下）→ 命中"""
    kl = _touch_ema144(_ema144_kl(250, "down"), -1)
    p = trig._detect_ema_touch(kl, -1, 144, _flt(refs=("ema144",)))
    assert p is not None and p > 0


def test_ema144_touch_flat_allowed_both_directions():
    """走平允许：EMA144 近 20 根变化 ≈0（走平）→ 多空两侧均放行（2026-10-16 口径，
    不再沿用 5 根严格同向斜率门）"""
    long_kl = _touch_ema144(_ema144_kl(250, "flat"), 1)
    short_kl = _touch_ema144(_ema144_kl(250, "flat"), -1)
    assert trig._detect_ema_touch(long_kl, 1, 144, _flt(refs=("ema144",))) is not None
    assert trig._detect_ema_touch(short_kl, -1, 144, _flt(refs=("ema144",))) is not None


def test_ema144_data_gate_skips_short_history():
    """数据门：已收盘 239 根 < max(200, 周期×1.7≈245) → 静默跳过；250 根放行"""
    assert trig._detect_ema_touch(
        _touch_ema144(_ema144_kl(240, "up"), 1), 1, 144, _flt(refs=("ema144",))) is None
    assert trig._detect_ema_touch(
        _touch_ema144(_ema144_kl(250, "up"), 1), 1, 144, _flt(refs=("ema144",))) is not None


# ── detect_trigger 接线：优先级 / 白名单 / 触发载荷 ───────────────────────

def _ema21_of(kl: list[list]) -> float:
    e = calc_ema([float(k[4]) for k in kl[:-1]], 21)
    assert e is not None
    return float(e[-1])


def _pending(kl: list[list], direction: int = 1, anchor_idx: int = -3,
             touch_ref=None) -> dict:
    return {"id": "x", "direction": direction, "touch_ref": touch_ref,
            "stage1_candle_open": datetime.utcfromtimestamp(kl[anchor_idx][0] / 1000)}


def test_touch_priority_retest_over_ema21():
    """同根双触点（retest + ema21 同点位）→ 标注取最高优先 retest"""
    kl = _kl(*_base())
    ema = _ema21_of(kl)
    ts, o, _, _, _, vol, qv = kl[-2]
    kl[-2] = [ts, o, o + 0.4, ema, ema + 0.2, vol, qv]
    ev = _retest_event(kl, ema)
    with patch.object(trig, "detect_all_patterns", return_value=GOLDEN_LONG):
        t, _ = trig.detect_trigger(kl, 1, flt=_flt(refs=trig.TOUCH_ORDER), retest=ev)
    assert t is not None and t["type"] == trig.PULLBACK_CONFIRM
    assert t["touch_ref"] == "retest"
    assert "突破回测位" in t["reason"]


def test_row2_swing_touch_confirm():
    """摆动结构位触碰 + 当根金K → 当根确认，touch_ref=swing"""
    kl = _swing_uptrend_kl()
    with patch.object(trig, "detect_all_patterns", return_value=GOLDEN_LONG):
        t, _ = trig.detect_trigger(kl, 1, flt=_flt(refs=("swing", "ema21")))
    assert t is not None and t["type"] == trig.PULLBACK_CONFIRM
    assert t["touch_ref"] == "swing"
    assert "摆动结构位" in t["reason"]


def test_row5_swing_touch_upserts_pending_with_ref():
    """触碰无金K → upsert pending：touch_ref 记触点类型，ema21 键存触点位价"""
    kl = _swing_uptrend_kl()
    t, op = trig.detect_trigger(kl, 1, flt=_flt(refs=("swing",)))
    assert t is None and op["op"] == "upsert"
    assert op["touch_ref"] == "swing"
    assert op["ema21"] == pytest.approx(103.0)


def test_row3_confirm_uses_pending_touch_ref():
    """次根确认：触点沿用 pending.touch_ref（Stage-1 触点）"""
    kl = _kl(*_base())
    ema = _ema21_of(kl)
    c = ema + 0.2
    ts, o, _, _, _, vol, qv = kl[-2]
    kl[-2] = [ts, o, c + 0.05, c - 0.05, c, vol, qv]  # 本根不触碰（悬于线上）
    with patch.object(trig, "detect_all_patterns", return_value=GOLDEN_LONG):
        t, op = trig.detect_trigger(kl, 1, pending=_pending(kl, touch_ref="retest"))
    assert t is not None and t["type"] == trig.PULLBACK_CONFIRM
    assert t["touch_ref"] == "retest"
    assert "突破回测位" in t["reason"]
    assert op == {"op": "confirm"}


def test_row3_confirm_legacy_pending_without_ref():
    """存量 pending 无 touch_ref（docs/22 前的行）→ 触点为 None，理由兜底「触点」"""
    kl = _kl(*_base())
    ema = _ema21_of(kl)
    c = ema + 0.2
    ts, o, _, _, _, vol, qv = kl[-2]
    kl[-2] = [ts, o, c + 0.05, c - 0.05, c, vol, qv]
    with patch.object(trig, "detect_all_patterns", return_value=GOLDEN_LONG):
        t, _ = trig.detect_trigger(kl, 1, pending=_pending(kl, touch_ref=None))
    assert t is not None and t.get("touch_ref") is None
    assert "触点" in t["reason"]


def test_touch_disabled_by_whitelist():
    """白名单未启用 retest：破轨事件在场也不探测 → 金K无触碰不触发
    （纯金K 2026-09-29 下线，金K必须与触点配合）"""
    kl = _kl(*_base())
    lvl = 99.5
    ts, o, _, _, _, vol, qv = kl[-2]
    kl[-2] = [ts, o, o + 0.5, lvl - 0.1, lvl + 0.4, vol, qv]  # 只触碰 retest 位
    ev = _retest_event(kl, lvl)
    with patch.object(trig, "detect_all_patterns", return_value=GOLDEN_LONG):
        t, _ = trig.detect_trigger(kl, 1, flt=_flt(refs=("ema21",)), retest=ev)
    assert t is None


def test_breakout_trigger_carries_event_payload():
    """T1 突破触发携带 breakout_level + candle_open（scanner 记事件的事实源）"""
    kl = _kl(*_base())
    o = float(kl[-3][4])
    c = 100.04 + 0.5  # 收盘越过摆动前高 100.04（_base 微扰序列最近未破 pivot）
    ts, _, _, _, _, _, qv = kl[-2]
    kl[-2] = [ts, o, c, o, c, 150.0, qv]
    t, _ = trig.detect_trigger(kl, 1, flt=_flt())
    assert t is not None and t["type"] == trig.BREAKOUT
    assert t["breakout_level"] == pytest.approx(100.04)
    assert t["candle_open"] == datetime.utcfromtimestamp(kl[-2][0] / 1000)


# ── resolve_scan_filters：触点白名单与参数 ───────────────────────────────

def test_resolve_default_touch_refs():
    """默认触点 = 摆动结构位 + 均线族（2026-10-08 均线族，2026-10-09 加入 4h EMA21，
    2026-10-14 加入 swing——回踩样本中 HL/LH 结构位占比可观；2026-10-16 ema144
    复出并默认启用；retest 仍需配置显式启用）"""
    flt = resolve_scan_filters(None)
    assert flt.pullback_touch_refs == (
        "swing", "ema21", "ema21_4h", "ema55", "ema144")
    assert flt.retest_window_bars == 24
    assert flt.structure_touch_band_pct == 0.3
    assert flt.ema_touch_min_bars == 200


def test_resolve_touch_refs_filter_and_fallback():
    """非法项过滤保留合法项；空/全非法/非 list 回落默认（swing+均线族四触点，
    触点全空会停摆回踩管线）；ema144 已于 2026-10-16 复出（退役过滤集清空），
    显式配置不再被过滤"""
    cfg = SimpleNamespace(scan_filters={"pullback_touch_refs": ["retest", "bogus", "ema55"]})
    assert resolve_scan_filters(cfg).pullback_touch_refs == ("retest", "ema55")
    for bad in (None, "retest", [], ["bogus"], ["bogus", 1]):
        cfg2 = SimpleNamespace(scan_filters={"pullback_touch_refs": bad})
        assert resolve_scan_filters(cfg2).pullback_touch_refs == \
            ("swing", "ema21", "ema21_4h", "ema55", "ema144"), bad
    cfg3 = SimpleNamespace(scan_filters={
        "pullback_touch_refs": ["ema21", "ema144", "retest"]})
    assert resolve_scan_filters(cfg3).pullback_touch_refs == ("ema21", "ema144", "retest")


def test_resolve_touch_params_override():
    cfg = SimpleNamespace(scan_filters={
        "retest_window_bars": 48, "structure_touch_band_pct": 0.5,
        "ema_touch_min_bars": 100,
    })
    flt = resolve_scan_filters(cfg)
    assert flt.retest_window_bars == 48
    assert flt.structure_touch_band_pct == 0.5
    assert flt.ema_touch_min_bars == 100


# ── _validate_scan_filters：触点键校验（PUT /config）─────────────────────

def test_validate_scan_filters_touch_keys():
    from fastapi import HTTPException
    from app.api.scan import _validate_scan_filters

    out = _validate_scan_filters({
        "pullback_touch_refs": ["retest", "ema55"],
        "retest_window_bars": 48,
        "structure_touch_band_pct": 0.5,
        "ema_touch_min_bars": 150,
    })
    assert out == {
        "pullback_touch_refs": ["retest", "ema55"],
        "retest_window_bars": 48.0,
        "structure_touch_band_pct": 0.5,
        "ema_touch_min_bars": 150.0,
    }
    # 空列表 / 非法项 / 非 list → 400（触点全空会让回踩管线停摆，不接受静默回落）
    for bad in ([], ["bogus"], "ema21"):
        with pytest.raises(HTTPException):
            _validate_scan_filters({"pullback_touch_refs": bad})
    with pytest.raises(HTTPException):
        _validate_scan_filters({"retest_window_bars": 999})
    with pytest.raises(HTTPException):
        _validate_scan_filters({"structure_touch_band_pct": -1})
    with pytest.raises(HTTPException):
        _validate_scan_filters({"ema_touch_min_bars": "x"})


# ── 4h EMA21 跨周期触点（2026-10-09）──────────────────────────────────────

def _htf_flat(base: float, n: int = 60) -> list[list]:
    """4h K线夹具：平盘序列（步长 4h，尾根未收盘）——平盘 EMA21 恒等于 base"""
    return [
        [i * 4 * 3_600_000, base, base + 0.2, base - 0.2, base, 100.0, 1e6]
        for i in range(n)
    ]


def test_htf_ema21_touch_long():
    """多头：1h 信号K线下影触及 4h EMA21（平盘 EMA=base）且收盘收回线上"""
    kl = _kl(*_base())
    ts, o, _, _, _, vol, qv = kl[-2]
    kl[-2] = [ts, o, o + 0.4, 99.8, 100.1, vol, qv]
    assert trig._detect_htf_ema21_touch(kl, _htf_flat(99.9), 1) == pytest.approx(99.9)


def test_htf_ema21_touch_short_symmetric():
    """空头：上影触及（h ≥ EMA）、收盘压在线下（c ≤ EMA）"""
    kl = _kl(*_base())
    ts, o, _, _, _, vol, qv = kl[-2]
    kl[-2] = [ts, o, 100.2, o - 0.4, 99.9, vol, qv]
    assert trig._detect_htf_ema21_touch(kl, _htf_flat(100.1), -1) == pytest.approx(100.1)


def test_htf_ema21_no_touch_when_off_level():
    """信号K线整体悬于 4h EMA21 上方（未触及）→ None"""
    kl = _kl(*_base())
    assert trig._detect_htf_ema21_touch(kl, _htf_flat(95.0), 1) is None


def test_htf_ema21_too_few_bars_skipped():
    """4h 根数不足（<22，EMA21 无法收敛）→ None 静默跳过"""
    kl = _kl(*_base())
    assert trig._detect_htf_ema21_touch(kl, _htf_flat(100.0, n=10), 1) is None


def test_row2_htf_ema21_touch_confirm():
    """4h EMA21 触碰 + 当根金K → 当根确认，touch_ref=ema21_4h（仅 4h 白名单，
    1h EMA21 未启用故不参与优先级竞争）"""
    kl = _kl(*_base())
    ts, o, _, _, _, vol, qv = kl[-2]
    kl[-2] = [ts, o, o + 0.4, 99.8, 100.1, vol, qv]
    with patch.object(trig, "detect_all_patterns", return_value=GOLDEN_LONG):
        t, _ = trig.detect_trigger(
            kl, 1, flt=_flt(refs=("ema21_4h",)), htf=_htf_flat(99.9))
    assert t is not None and t["type"] == trig.PULLBACK_CONFIRM
    assert t["touch_ref"] == "ema21_4h"
    assert "EMA21(4h)" in t["reason"]
    assert t["position_ref"] == "回踩EMA21(4h)"


def test_row5_htf_ema21_touch_upserts_pending():
    """触碰无金K → upsert pending：touch_ref=ema21_4h，ema21 键存 4h 触点位价"""
    kl = _kl(*_base())
    ts, o, _, _, _, vol, qv = kl[-2]
    kl[-2] = [ts, o, o + 0.4, 99.8, 100.1, vol, qv]
    t, op = trig.detect_trigger(kl, 1, flt=_flt(refs=("ema21_4h",)), htf=_htf_flat(99.9))
    assert t is None and op["op"] == "upsert"
    assert op["touch_ref"] == "ema21_4h"
    assert op["ema21"] == pytest.approx(99.9)


def test_touch_priority_ema21_over_htf_ema21():
    """同根 1h/4h EMA21 双触点（同点位）→ 均线取最快，标注 1h EMA21"""
    kl = _kl(*_base())
    e = _ema21_of(kl)
    ts, o, _, _, _, vol, qv = kl[-2]
    kl[-2] = [ts, o, e + 0.4, e - 0.1, e + 0.2, vol, qv]
    with patch.object(trig, "detect_all_patterns", return_value=GOLDEN_LONG):
        t, _ = trig.detect_trigger(
            kl, 1, flt=_flt(refs=("ema21", "ema21_4h")), htf=_htf_flat(e))
    assert t is not None and t["touch_ref"] == "ema21"


def test_htf_ema21_skipped_without_htf():
    """4h K线缺失（htf=None）→ ema21_4h 静默跳过，1h 触点体系照常工作"""
    kl = _kl(*_base())
    e = _ema21_of(kl)
    ts, o, _, _, _, vol, qv = kl[-2]
    kl[-2] = [ts, o, e + 0.4, e - 0.1, e + 0.2, vol, qv]
    with patch.object(trig, "detect_all_patterns", return_value=GOLDEN_LONG):
        t, _ = trig.detect_trigger(
            kl, 1, flt=_flt(refs=("ema21", "ema21_4h")), htf=None)
    assert t is not None and t["touch_ref"] == "ema21"


def test_structure_gate_exempts_non_ema21_touch():
    """1H 结构门只约束 ema21 触点：4h EMA21 触点即使结构门不过仍正常触发"""
    kl = _kl(*_base())
    ts, o, _, _, _, vol, qv = kl[-2]
    kl[-2] = [ts, o, o + 0.4, 99.8, 100.1, vol, qv]
    with patch.object(trig, "detect_all_patterns", return_value=GOLDEN_LONG), \
         patch.object(trig, "structure_1h", return_value=None):
        t, _ = trig.detect_trigger(
            kl, 1, flt=_flt(refs=("ema21_4h",)), htf=_htf_flat(99.9))
    assert t is not None and t["touch_ref"] == "ema21_4h"
