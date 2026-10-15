# -*- coding: utf-8 -*-
"""筛选链 EMA 三件套硬门回归（2026-09-28 引入 ranging，2026-10-10 二次拍板硬门化）。

detect_indicator_candidate 判定链（docs/01 §3.2）：
gate_4h（4h 趋势闸门一票否决：走平/缠绕/ADX<20 → 不跑票数不触发，pending
expire(ranging)）→ 7 票多数决 → 闸门方向一致 → range_score
（震荡评分 ≥3 → expire(range_score)）→ 触发。4h 拉取失败（htf=None）
降级放行与第 7 票口径一致；评分卡不依赖 4h，htf 缺失时照常生效。
structure_1h 硬门 2026-10-12 退役（docs/21 O7：降格为 s2 资格闸，
结构判定收敛到 trigger 层 trend_pullback 升级标注）。

用假 pool 注入 1h/4h K 线，不触网。K 线构造保证 7 票多数决通过（|net|≥3、
voted≥5），使过滤命中点唯一落在被测闸门处；票崩夹具（1h 横盘）用于证明
4h 闸门先于票数。1h 夹具须 ≥201 根（结构门/评分卡的 EMA144 种子门槛）。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest  # noqa: E402

from app.services.scanner import detect_indicator_candidate  # noqa: E402
from app.services.strategy.ema_gate import range_score  # noqa: E402


def _trend_1h(closes_start: float = 100.0, drift: float = 0.35) -> list[list]:
    """300 根稳步上行 1h：让多数 EMA/MACD/RSI 等投多票（|net| 达标），
    且满足结构门三线排列+发散（长度 ≥201 供 EMA144 种子）"""
    kl = []
    price = closes_start
    ts = 1_700_000_000_000
    for i in range(300):
        o = price
        price += drift
        kl.append([ts + i * 3_600_000, o, price + 0.15, price - 0.25, price, 100.0, 100.0])
    return kl


def _ranging_4h() -> list[list]:
    """220 根窄幅来回 4h：EMA144 走平 + ADX<20 → gate_4h 否决"""
    kl = []
    ts = 1_700_000_000_000
    price = 100.0
    for i in range(220):
        # 正弦小幅摆动（幅度 0.6），EMA 无法拉开间距，ADX 低
        o = 100.0 + 0.3 * ((i % 8) - 4) / 4
        c = 100.0 + 0.3 * (((i + 4) % 8) - 4) / 4
        kl.append([ts + i * 4 * 3_600_000, o, max(o, c) + 0.1, min(o, c) - 0.1, c, 80.0, 80.0])
        price = c
    return kl


def _trending_4h_up() -> list[list]:
    """220 根稳步上行 4h：斜率/缠绕/ADX 全过 → gate_4h 给 long（对照组）"""
    kl = []
    ts = 1_700_000_000_000
    price = 60.0
    for i in range(220):
        o = price
        price += 0.25
        kl.append([ts + i * 4 * 3_600_000, o, price + 0.2, price - 0.2, price, 90.0, 90.0])
    return kl


class _FakePool:
    """pool.get_klines 桩：1h 返回传入序列，4h 可指定 ranging/趋势"""

    def __init__(self, klines_1h, klines_4h):
        self._m = {"1h": klines_1h, "4h": klines_4h}

    def get_klines(self, symbol, interval, limit, **kwargs):
        return self._m[interval][:limit]


def _flat_1h(n: int = 110) -> list[list]:
    """窄幅来回 1h：票崩夹具（票数不过）兼震荡评分高分夹具（n≥201 时）"""
    kl = []
    ts = 1_700_000_000_000
    for i in range(n):
        o = 100.0 + 0.3 * ((i % 8) - 4) / 4
        c = 100.0 + 0.3 * (((i + 4) % 8) - 4) / 4
        kl.append([ts + i * 3_600_000, o, max(o, c) + 0.1, min(o, c) - 0.1, c, 80.0, 80.0])
    return kl


# ── gate_4h：4h 趋势闸门一票否决 ────────────────────────────────────────


def test_ranging_4h_filtered_out():
    """4h 走平/ADX弱（gate_4h 否决）：即使 1h 方向票通过也不产生候选"""
    cand, op = detect_indicator_candidate(
        _FakePool(_trend_1h(), _ranging_4h()), "TEST/USDT", _trend_1h()
    )
    assert cand is None


def test_ranging_4h_expires_pending():
    """4h 闸门否决且有活跃 pending → 同轮 expire，reason=ranging（终态死因=4h 无明确趋势）"""
    pending = {"id": "x", "direction": 1, "stage1_candle_open": 1_700_000_000_000}
    cand, op = detect_indicator_candidate(
        _FakePool(_trend_1h(), _ranging_4h()), "TEST/USDT", _trend_1h(), pending=pending
    )
    assert cand is None
    assert op is not None and op["op"] == "expire" and op["reason"] == "ranging"


def test_gate_checked_before_votes():
    """回归：1h 票崩 + 4h 闸门否决 + pending → 死因是 ranging 而非 direction_flip。
    证明闸门在票数之前命中——待确认池过滤不看票数、只看 4h 趋势背景。"""
    pending = {"id": "x", "direction": 1, "stage1_candle_open": 1_700_000_000_000}
    cand, op = detect_indicator_candidate(
        _FakePool(_flat_1h(), _ranging_4h()), "TEST/USDT", _flat_1h(), pending=pending
    )
    assert cand is None
    assert op is not None and op["op"] == "expire" and op["reason"] == "ranging"


def test_trending_4h_not_filtered():
    """对照组：4h 闸门通过（斜率/缠绕/ADX 全过）+ 1h 趋势上行 → 一路走到触发环节
    （无 pending 时硬门路径不产生 expire）"""
    cand, op = detect_indicator_candidate(
        _FakePool(_trend_1h(), _trending_4h_up()), "TEST/USDT", _trend_1h()
    )
    assert not (op and op.get("op") == "expire")


def test_htf_fetch_failure_not_filtered():
    """4h 拉取失败（htf=None）：闸门降级放行，不因缺数据过滤"""
    kl = _trend_1h()

    class _BrokenPool:
        def get_klines(self, symbol, interval, limit, **kwargs):
            if interval == "4h":
                raise RuntimeError("exchange down")
            return kl[:limit]

    cand, op = detect_indicator_candidate(_BrokenPool(), "TEST/USDT", kl)
    # 候选可能因无触发为 None，但不得被闸门挡住 —— 与趋势对照组同判
    assert not (cand is None and op and op.get("op") == "expire")


def test_gate_direction_mismatch_expires_pending():
    """4h 闸门多头 + 1h 多数决空头 → 方向一致性门杀（direction_flip），不进结构门"""
    kl = _trend_1h(closes_start=300.0, drift=-0.35)  # 1h 稳步下行 → 空头多数决
    pending = {"id": "x", "direction": -1, "stage1_candle_open": 1_700_000_000_000}
    cand, op = detect_indicator_candidate(
        _FakePool(kl, _trending_4h_up()), "TEST/USDT", kl, pending=pending
    )
    assert cand is None
    assert op is not None and op["op"] == "expire" and op["reason"] == "direction_flip"


# ── range_score：筛选链硬门（monkeypatch 强制命中）；structure_1h 硬门已退役 ──


def test_structure_gate_no_longer_vetoes():
    """O7（2026-10-12 拍板，docs/21）：1H 结构门筛选链硬门退役——真实管线下
    不再以 structure_1h 否决信号/过期 pending（s2 资格闸移到 risk_guard）"""
    kl = _trend_1h()
    pending = {"id": "x", "direction": 1, "stage1_candle_open": 1_700_000_000_000}
    cand, op = detect_indicator_candidate(
        _FakePool(kl, _trending_4h_up()), "TEST/USDT", kl, pending=pending
    )
    assert not (op and op.get("op") == "expire" and op.get("reason") == "structure_1h")


def test_range_score_gate_veto_expires_pending(monkeypatch):
    """震荡评分 ≥3（桩强制命中）→ 不产生候选，pending expire(range_score)"""
    import app.services.scanner as scanner_mod

    monkeypatch.setattr(scanner_mod, "range_score",
                        lambda k, gate_ev=None: (3, ["1H ADX过低", "布林带收口", "成交量低迷"]))
    kl = _trend_1h()
    pending = {"id": "x", "direction": 1, "stage1_candle_open": 1_700_000_000_000}
    cand, op = detect_indicator_candidate(
        _FakePool(kl, _trending_4h_up()), "TEST/USDT", kl, pending=pending
    )
    assert cand is None
    assert op is not None and op["op"] == "expire" and op["reason"] == "range_score"


# ── range_score 评分卡本身 ─────────────────────────────────────────────


def test_range_score_choppy_high():
    """正弦震荡 1h（n≥201）：距离过近/ADX 预警/反复穿越 EMA21/量低迷共振 → ≥3"""
    score, reasons = range_score(_flat_1h(300))
    assert score >= 3
    assert "21/55距离过近" in reasons
    assert "55/144距离过近" in reasons


def test_range_score_trending_low():
    """稳步趋势 1h：评分卡不误伤（唯一命中是恒量成交的 cv 项，<3）"""
    score, reasons = range_score(_trend_1h())
    assert score < 3


def _pullback_1h() -> list[list]:
    """深回踩 1h（2026-10-14 分离史夹具）：270 根 +0.4 稳步趋势后 24 根 -0.6
    回踩，全程 ±0.8 影线（真实行情 ATR 不随回踩塌缩的特征）。回踩末端
    |e21-e55|/ATR ≈ 0.17（现值口径会误判"21/55距离过近"），但近 12 根最大
    分离度 ≈ 1.85——这波趋势曾分离过，不是震荡粘合。"""
    kl = []
    price = 100.0
    ts = 1_700_000_000_000
    for i in range(270 + 24):
        o = price
        price += 0.4 if i < 270 else -0.6
        kl.append([ts + i * 3_600_000, o, max(o, price) + 0.8,
                   min(o, price) - 0.8, price, 80.0, 80.0])
    return kl


def test_range_score_pullback_not_glued():
    """项3/4 分离史口径（2026-10-14，与 structure_1h div_lookback 同源）：
    深回踩压窄均线现值不再误记震荡粘合——趋势曾分离过即不给"距离过近"分。
    回归锚点：本夹具现值 |e21-e55|/ATR≈0.17<0.5（旧口径会给分）。"""
    score, reasons = range_score(_pullback_1h())
    assert "21/55距离过近" not in reasons
    assert "55/144距离过近" not in reasons
    assert score < 3


# ── 4h 计算合并（2026-10-13）：htf_core 透传 vs 各自自算 数值等价 ──────────


def test_htf_core_merge_equivalent():
    """合并 4h 计算（2026-10-13，用户拍板）：gate_4h / analyze_htf_background
    传入共享 core 与不传（各自内部自算 htf_core）结果完全一致——去重只省算力、
    不改行为（EMA 由 ema_series 换成同公式的 calc_ema，ADX 两处本就同源）"""
    from app.services.strategy.ema_gate import gate_4h
    from app.services.strategy.technical import analyze_htf_background, htf_core

    for k4 in (_trending_4h_up(), _ranging_4h()):
        core = htf_core(k4)
        assert core is not None
        assert gate_4h(k4, core=core) == gate_4h(k4)
        assert analyze_htf_background(k4, core=core) == analyze_htf_background(k4)
