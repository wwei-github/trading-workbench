# -*- coding: utf-8 -*-
"""关键位子系统回归（docs/21，2026-09-27）。

覆盖：四源采集、4h 缺失降级、聚类共振/加权、touches 连续合并、
单K/组合拒绝、状态机全路径（intact→broken→flipped_confirmed/invalid）、
影线突破不触发、边界降级。

夹具教训（docs/20）：阳线 c>o 时 high=close/low=open，阴线反转——
非法K线（high<low）会被形态守卫静默拒绝。
"""
from types import SimpleNamespace

import pytest

from app.services.strategy import key_levels as kl


# ── 合成K线构造器 ─────────────────────────────────────────────────────

def _mk_klines(closes: list[float], start_ms: int = 1_700_000_000_000,
               shadows: dict[int, tuple[float, float]] | None = None):
    """由收盘价序列构造 K 线（open=前收，阴阳自动）。

    shadows: {idx: (high_extra, low_extra)} 影线延伸（在阴阳规则之上叠加）。
    """
    out = []
    shadows = shadows or {}
    prev = closes[0]
    for i, c in enumerate(closes):
        o = prev
        h = max(o, c) if c >= o else o
        l = min(o, c) if c < o else o
        he, le = shadows.get(i, (0.0, 0.0))
        h += he
        l -= le
        out.append([start_ms + i * 3_600_000, o, h, l, c,
                    100.0 + i, 1000.0 + i])
        prev = c
    return out


@pytest.fixture(autouse=True)
def _patch_settings(monkeypatch):
    """固定关键位配置（免疫本地 .env 覆盖，docs/20 教训）"""
    s = SimpleNamespace(
        KEY_LEVELS_ENABLED=True, KEY_LEVELS_TOL_ATR=0.5,
        KEY_LEVELS_CONFIRM_WINDOW=12, KEY_LEVELS_MIN_SOURCES=1,
        KEY_LEVELS_PSYCHOLOGICAL=False,
    )
    for k, v in s.__dict__.items():
        monkeypatch.setattr(kl.settings, k, v, raising=False)


# ── 采集 ─────────────────────────────────────────────────────────────

def test_collect_swing_1h_basic():
    # 中段造一个明显峰与谷
    closes = ([50.0] * 5 + [51, 52, 53, 52, 51] * 1 + [50.0] * 8
              + [49, 48, 47, 48, 49] + [50.0] * 8)
    ks = _mk_klines(closes)
    cands = kl.collect_candidates(ks, None)
    srcs = {c["source"] for c in cands}
    assert "swing_1h" in srcs
    # 摆动点收盘口径：峰 53 / 谷 47 应出现
    assert any(abs(c["price"] - 53.0) < 1e-9 for c in cands)
    assert any(abs(c["price"] - 47.0) < 1e-9 for c in cands)


def test_collect_4h_mapping_and_degrade():
    # 波动序列：有摆动点与轨道推进
    closes = ([50.0] * 5 + [51, 52, 53, 52, 51] + [50.0] * 10
              + [49, 48, 47, 48, 49] + [50.0] * 11)
    ks = _mk_klines(closes)
    # 4h 缺失 → 无 swing_4h，不报错
    cands = kl.collect_candidates(ks, None)
    assert all(c["source"] != "swing_4h" for c in cands)
    # 4h 存在且 open_time 与 1h 对齐（每 4 根取样）；收盘波动出摆动点
    c4closes = ([50, 51, 52, 51, 50, 49, 48, 49, 50] * 2)
    k4 = [[ks[i][0], 50, 52, 47, c4closes[i // 4], 100, 1000]
          for i in range(0, 36, 4)]
    cands = kl.collect_candidates(ks, k4)
    srcs = {c["source"] for c in cands}
    assert "swing_4h" in srcs
    assert "swing_1h" in srcs and "donchian" in srcs and "prev_day" in srcs


def test_collect_prev_day():
    closes = [50.0] * 30
    closes[10] = 52.0  # 某日冲高
    closes[20] = 48.0  # 某日回落
    ks = _mk_klines(closes)
    cands = kl.collect_candidates(ks, None)
    pd = [c for c in cands if c["source"] == "prev_day"]
    assert pd, "应有前日高低候选"
    assert any(c["kind"] == "close" for c in pd)


def test_collect_donchian_updates():
    # idx22 的 54 在首窗（[0..19]）之后才滑入 → 触发轨道更新
    closes = [50.0] * 22 + [54.0] + [51.0] * 9
    ks = _mk_klines(closes)
    cands = kl.collect_candidates(ks, None)
    dn = [c for c in cands if c["source"] == "donchian" and c["kind"] == "high"]
    assert any(abs(c["price"] - 54.0) < 1e-9 for c in dn)
    # idx 应为极值所在根（22）而非窗口滑出位置
    hit = [c for c in dn if abs(c["price"] - 54.0) < 1e-9][0]
    assert hit["idx_1h"] == 22


# ── 聚类与统计 ────────────────────────────────────────────────────────

def test_merge_multisource_strength_and_weighted_mean():
    # 三源同价位 → strength=3；价格加权均值=同价
    cands = [
        {"source": "swing_1h", "price": 100.0, "idx_1h": 10, "kind": "low", "raw": {}},
        {"source": "swing_4h", "price": 100.2, "idx_1h": 12, "kind": "low", "raw": {}},
        {"source": "donchian", "price": 99.8, "idx_1h": 14, "kind": "low", "raw": {}},
    ]
    ks = _mk_klines([100.0] * 40)  # 位后平底K线贴在位价上 → 一次合并触及
    lv = kl.merge_levels(cands, ks, atr=1.0, tol_mult=0.5)
    assert len(lv) == 1
    assert lv[0]["sources"] == ["donchian", "swing_1h", "swing_4h"]
    assert lv[0]["strength"] == 3
    assert abs(lv[0]["price"] - 100.0) < 0.15
    assert lv[0]["touches"] == 1


def test_merge_touch_consecutive_merged():
    cands = [{"source": "swing_1h", "price": 100.0, "idx_1h": 5,
              "kind": "low", "raw": {}}]
    # 位后连续 5 根横盘在带内 → touches 应为 1（连续合并）
    closes = [100.0] * 6 + [100.3, 99.9, 100.2, 100.1, 100.0] + [100.0] * 20
    ks = _mk_klines(closes)
    lv = kl.merge_levels(cands, ks, atr=1.0, tol_mult=0.5)
    assert lv[0]["touches"] == 1


def test_merge_single_pin_rejection():
    # 影线深刺位下 0.6×ATR、收盘收回 → 1 次拒绝，strength=源1+拒绝1=2
    cands = [{"source": "swing_1h", "price": 100.0, "idx_1h": 5,
              "kind": "low", "raw": {}}]
    closes = [100.0] * 6 + [100.0] * 20
    ks = _mk_klines(closes, shadows={10: (0.0, 0.6)})  # 第10根下影穿 99.75
    lv = kl.merge_levels(cands, ks, atr=1.0, tol_mult=0.5)
    assert lv[0]["rejections"] == 1
    assert lv[0]["strength"] == 2


def test_merge_reject_cap_two():
    cands = [{"source": "swing_1h", "price": 100.0, "idx_1h": 5,
              "kind": "low", "raw": {}}]
    closes = [100.0] * 26
    shadows = {8: (0.0, 0.6), 12: (0.0, 0.6), 16: (0.0, 0.6)}
    ks = _mk_klines(closes, shadows=shadows)
    lv = kl.merge_levels(cands, ks, atr=1.0, tol_mult=0.5)
    assert lv[0]["rejections"] == 3
    assert lv[0]["strength"] == 1 + 2  # 封顶 +2


def test_merge_combo_rejection_engulfing():
    # 双K组合：K1 收盘跌破位价（穿透），K2 阳线吞没收回 → 1 次组合拒绝
    cands = [{"source": "swing_1h", "price": 100.0, "idx_1h": 25,
              "kind": "low", "raw": {}}]
    # 前 25 根建立 20 根均实体基线（小幅波动）
    closes = [100.0] * 12
    for i in range(12, 25):
        closes.append(100.0 + (0.5 if i % 2 == 0 else -0.5))
    # K25 收盘 99.0（阴线穿透 99.75 带外沿），K26 收盘 100.5（阳线吞没）
    closes.append(99.0)   # idx25 阴线：o=100.5? 前收 99.5→o=99.5,c=99.0 不够深
    # 修正：前一根收 100.5，本根 o=100.5 c=99.0（大阴线穿透）
    closes[-1] = 99.0
    closes.append(100.6)  # idx26 阳线 o=99.0 c=100.6 包住前实体
    ks = _mk_klines(closes)
    lv = kl.merge_levels(cands, ks, atr=1.0, tol_mult=0.5)
    assert lv[0]["rejections"] == 1


def test_merge_touch_and_reject_same_candle_counts_reject_only():
    # 同根进带又被影线穿透收回：应只计拒绝（更强语义），不计触及
    cands = [{"source": "swing_1h", "price": 100.0, "idx_1h": 5,
              "kind": "low", "raw": {}}]
    # 位后其余K线在 102（带外），仅第 10 根下影探到 99.5（带内且穿透）
    closes = [100.0] * 6 + [102.0] * 20
    ks = _mk_klines(closes, shadows={10: (0.0, 2.5)})
    # 跳空修正：第 6 根 o/l 抬到 102（否则过渡K线 l=100 落带内构成触及）
    ks[6][1] = ks[6][2] = ks[6][3] = 102.0
    lv = kl.merge_levels(cands, ks, atr=1.0, tol_mult=0.5)
    assert lv[0]["rejections"] == 1
    assert lv[0]["touches"] == 0


# ── 状态机 ────────────────────────────────────────────────────────────

def _one_level(price: float) -> dict:
    return {"price": price, "sources": ["swing_1h"], "touches": 0,
            "rejections": 0, "kind": "high", "strength": 1,
            "first_idx": 5, "last_idx": 5}


def test_classify_full_flip_path():
    # 收盘上破 → 回踩不破 → flipped_confirmed（role=support）
    closes = ([100.0] * 6 + [101.5]       # idx6 收盘上破（brk=0.1×ATR）
              + [101.0] * 2 + [100.05]    # 回踩触及带内且收盘仍在位上方
              + [100.3] * 10)
    ks = _mk_klines(closes)
    lv = kl.classify_levels([_one_level(100.0)], ks, atr=1.0, confirm_window=12)
    assert lv[0]["state"] == "flipped_confirmed"
    assert lv[0]["role"] == "support"
    assert lv[0]["flip_direction"] == 1


def test_classify_broken_expiry_invalid():
    # 上破后一路走高超窗未回踩 → invalid
    closes = [100.0] * 6 + [101.5] + [102.0] * 14
    ks = _mk_klines(closes)
    lv = kl.classify_levels([_one_level(100.0)], ks, atr=1.0, confirm_window=12)
    assert lv[0]["state"] == "invalid"


def test_classify_shadow_breakout_not_broken():
    # 影线插到位上方但收盘未越 0.1×ATR → 仍 intact
    closes = [100.0] * 20
    ks = _mk_klines(closes, shadows={10: (0.08, 0.0)})  # 上影 0.08<0.1
    lv = kl.classify_levels([_one_level(100.0)], ks, atr=1.0, confirm_window=12)
    assert lv[0]["state"] == "intact"


def test_classify_confirmed_then_reversed_invalid():
    # 确认后收盘反向跌破原位 → invalid（最后事件为准）
    closes = ([100.0] * 6 + [101.5] + [101.0] + [100.05]
              + [100.3] * 3 + [99.0] + [98.5])
    ks = _mk_klines(closes)
    lv = kl.classify_levels([_one_level(100.0)], ks, atr=1.0, confirm_window=12)
    assert lv[0]["state"] == "invalid"


def test_classify_combo_confirm():
    # 回踩深刺位下未收回、次根看涨吞没收回 → 组合确认
    # idx6 上破；idx7-11 高位漂移不触带；idx12 触带+收盘跌破（99.5<99.75 穿透）；
    # idx13 吞没收回（o=99.5 包住、c=101.2 越过 K12 全实体）
    closes = [100.0] * 6 + [101.5] + [101.0] * 5 + [99.5] + [101.2]
    ks = _mk_klines(closes)
    lv = kl.classify_levels([_one_level(100.0)], ks, atr=1.0, confirm_window=12)
    assert lv[0]["state"] == "flipped_confirmed"


# ── 编排器与降级 ──────────────────────────────────────────────────────

def test_compute_disabled_returns_empty(monkeypatch):
    monkeypatch.setattr(kl.settings, "KEY_LEVELS_ENABLED", False)
    ks = _mk_klines([100.0] * 60)
    assert kl.compute_key_levels(ks, None) == []


def test_compute_short_klines_returns_empty():
    assert kl.compute_key_levels(_mk_klines([100.0] * 10), None) == []


def test_compute_min_sources_filter(monkeypatch):
    monkeypatch.setattr(kl.settings, "KEY_LEVELS_MIN_SOURCES", 2)
    closes = ([50.0] * 5 + [51, 52, 53, 52, 51] + [50.0] * 8
              + [49, 48, 47, 48, 49] + [50.0] * 8)
    ks = _mk_klines(closes)
    # 单源候选强度=1 < 2 → 过滤后可能为空（无共振时）
    levels = kl.compute_key_levels(ks, None)
    assert all(l["strength"] >= 2 for l in levels)


def test_compute_sorted_by_distance():
    closes = ([50.0] * 5 + [51, 52, 53, 52, 51] + [50.0] * 8
              + [49, 48, 47, 48, 49] + [50.0] * 8)
    ks = _mk_klines(closes)
    levels = kl.compute_key_levels(ks, None)
    dists = [abs(l["distance_pct"]) for l in levels]
    assert dists == sorted(dists)


def test_classify_intact_role_by_price_side():
    # intact role 按现价侧：位在现价上方=压力、下方=支撑（2026-09-27 修正）
    ks_hi = _mk_klines([100.0] * 19 + [99.95])   # 现价略低于位 → resistance
    lv = kl.classify_levels([_one_level(100.0)], ks_hi, atr=1.0, confirm_window=12)
    assert lv[0]["state"] == "intact"
    assert lv[0]["role"] == "resistance"
    ks_lo = _mk_klines([100.0] * 19 + [100.05])  # 现价略高于位 → support
    lv = kl.classify_levels([_one_level(100.0)], ks_lo, atr=1.0, confirm_window=12)
    assert lv[0]["role"] == "support"


# ── AI 位表瘦身（select_for_ai，验证位+近场兜底口径）──────────────────

def _ai_lv(price: float, dist: float, strength: int = 1,
           touches: int = 1, state: str = "intact") -> dict:
    lv = _lv(price, state=state)
    lv["strength"] = strength
    lv["touches"] = touches
    lv["distance_pct"] = dist
    return lv


def test_select_for_ai_drop_invalid_keep_strong():
    # invalid 剔除；强位无论远近保留；输出按距离升序
    out = kl.select_for_ai([
        _ai_lv(90.0, 10.0, strength=3, touches=2),
        _ai_lv(95.0, 5.0, state="invalid"),
        _ai_lv(101.0, 1.0, strength=1),
    ])
    assert [lv["price"] for lv in out] == [101.0, 90.0]


def test_select_for_ai_near_fallback_and_cap():
    # 验证位优先 + 近场兜底 3 个；强位≥cap 时全部取强位按距离截断
    levels = ([_ai_lv(100 + i, float(i), strength=1) for i in range(1, 6)]
              + [_ai_lv(120.0, 20.0, strength=4, touches=3)])
    out = kl.select_for_ai(levels)
    assert 120.0 in [lv["price"] for lv in out]          # 强位保留
    assert len(out) <= 8
    assert out[0]["price"] == 101.0                      # 距离升序
    # 强位超过 cap → 只取前 cap 个强位（近场弱位不进）
    many_strong = [_ai_lv(100 + i, float(i), strength=4, touches=3)
                   for i in range(1, 12)]
    assert [lv["price"] for lv in kl.select_for_ai(many_strong)] == \
        [101.0, 102.0, 103.0, 104.0, 105.0, 106.0, 107.0, 108.0]


def test_select_for_ai_empty():
    assert kl.select_for_ai([]) == []


# ── P1 锚点接位表（docs/21 §7）────────────────────────────────────────

from app.services.ai_agent import _handle_submit, _resolve_price  # noqa: E402


def _sig(cur: float, levels: list | None = None,
         highs: list | None = None, lows: list | None = None) -> dict:
    return {
        "current_price": cur,
        "key_levels": levels or [],
        "recent_swings": {
            "highs": [{"price": p} for p in (highs or [])],
            "lows": [{"price": p} for p in (lows or [])],
        },
    }


def _lv(price: float, state: str = "intact", flip: int | None = None,
        strength: int = 1) -> dict:
    return {"price": price, "sources": ["swing_1h"], "touches": 1,
            "rejections": 0, "kind": "high", "strength": strength,
            "state": state, "role": None, "flip_direction": flip,
            "bars_since_event": 1, "distance_pct": 0.0}


def test_resolve_support_resistance_from_table():
    # 位表非空时结构锚点从位表解析（摆动点值不同以证明优先级）
    sig = _sig(99.0, levels=[_lv(100.0), _lv(98.0)],
               highs=[105.0], lows=[95.0])
    assert _resolve_price("support", 0, sig) == 98.0
    assert _resolve_price("resistance", 0, sig) == 100.0


def test_resolve_excludes_invalid_universally():
    sig = _sig(99.0, levels=[_lv(98.0, state="invalid"), _lv(96.0)])
    assert _resolve_price("support", 0, sig) == 96.0


def test_resolve_empty_side_returns_none_not_crash():
    # 回归（2026-09-27 bug 检查）：现价上方无合格位时 resistance 返回 None，
    # 不得因 min(空序列) 抛 ValueError 打断 Agent 工具循环
    sig = _sig(105.0, levels=[_lv(98.0), _lv(96.0)])
    assert _resolve_price("resistance", 0, sig) is None
    sig2 = _sig(95.0, levels=[_lv(102.0), _lv(104.0)])
    assert _resolve_price("support", 0, sig2) is None


def test_resolve_broken_entry_stop_vs_tp():
    # broken 位：入场/止损禁锚（purpose 过滤），止盈保留（回测原位是常见目标）
    sig = _sig(99.0, levels=[_lv(97.0, state="broken", flip=-1), _lv(96.0)])
    assert _resolve_price("support", 0, sig, purpose="entry") == 96.0
    assert _resolve_price("support", 0, sig, purpose="stop") == 96.0
    assert _resolve_price("support", 0, sig, purpose="tp") == 97.0


def test_resolve_key_support_confirmed():
    # 现价下方：intact 或向上翻转已确认合格；broken/invalid 排除；取最近
    sig = _sig(100.0, levels=[
        _lv(98.0),                                    # intact 支撑侧 ✓
        _lv(96.0, state="flipped_confirmed", flip=1),  # 向上翻转确认 ✓
        _lv(99.0, state="broken", flip=-1),            # broken ✗
        _lv(94.0, state="invalid"),                    # invalid ✗
    ])
    assert _resolve_price("key_support_confirmed", 0, sig) == 98.0
    sig2 = _sig(100.0, levels=[
        _lv(96.0, state="flipped_confirmed", flip=1), _lv(99.0, state="broken", flip=-1)])
    assert _resolve_price("key_support_confirmed", 0, sig2) == 96.0


def test_resolve_key_resistance_confirmed():
    sig = _sig(100.0, levels=[
        _lv(102.0),                                    # intact 压力侧 ✓
        _lv(104.0, state="flipped_confirmed", flip=-1),  # 向下翻转确认 ✓
        _lv(101.0, state="broken", flip=1),            # broken ✗
    ])
    assert _resolve_price("key_resistance_confirmed", 0, sig) == 102.0
    sig2 = _sig(100.0, levels=[
        _lv(104.0, state="flipped_confirmed", flip=-1),
        _lv(101.0, state="broken", flip=1)])
    assert _resolve_price("key_resistance_confirmed", 0, sig2) == 104.0


def test_resolve_key_nearest_strong():
    # 强度≥2 中距现价最近者（更近的低强度位被跳过）
    sig = _sig(100.0, levels=[
        _lv(99.0, strength=1), _lv(103.0, strength=2), _lv(105.0, strength=3)])
    assert _resolve_price("key_nearest_strong", 0, sig) == 103.0


def test_resolve_zone_refs_nth_from_table():
    sig = _sig(100.0, levels=[_lv(102.0), _lv(104.0), _lv(98.0), _lv(96.0)])
    assert _resolve_price("resistance_zone_low", 0, sig) == 102.0
    assert _resolve_price("next_resistance_zone_low", 0, sig) == 104.0
    assert _resolve_price("support_zone_high", 0, sig) == 98.0
    assert _resolve_price("next_support_zone_high", 0, sig) == 96.0


def test_resolve_fallback_swings_when_table_empty():
    # 位表空 → 原摆动点路径；key_ 锚按同义结构位兜底（开关回滚场景）
    sig = _sig(99.0, highs=[105.0], lows=[95.0])
    assert _resolve_price("support", 0, sig) == 95.0
    assert _resolve_price("resistance", 0, sig) == 105.0
    assert _resolve_price("key_support_confirmed", 0, sig) == 95.0
    assert _resolve_price("key_resistance_confirmed", 0, sig) == 105.0
    # key_nearest_strong：两侧最近摆动点取距现价近者
    assert _resolve_price("key_nearest_strong", 0, sig) == 95.0
    assert _resolve_price("resistance_zone_low", 0, sig) == 105.0
    # 摆动点不足档位时返回 None
    assert _resolve_price("next_resistance_zone_low", 0, sig) is None


def test_handle_submit_broken_stop_anchor_rejected():
    # 集成：唯一下方位是 broken → 止损锚解析失败 → 可重试错误（非静默用错位）
    sig = _sig(99.0, levels=[_lv(97.0, state="broken", flip=-1)],
               highs=[105.0])
    args = {"trade_decision": "suggest", "direction": "long",
            "entry_ref": "market", "stop_ref": "support",
            "tp1_ref": "resistance_zone_low", "recommendation": 60,
            "reason": "1. x"}
    done, result, fixed = _handle_submit(args, sig, klines=[])
    assert not done
    assert "锚点换算失败" in result
    assert "stop_ref" in result
    assert fixed is None
