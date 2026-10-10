# -*- coding: utf-8 -*-
"""筛选参数组（scan_filters）解析与透传回归（2026-09-28 筛选配置上页面）。

覆盖：
1. resolve_scan_filters 优先级：DB JSONB > env(settings) > 代码默认；逐键容错
   （非法值/负数按缺失回落，不烧整轮扫描）
2. detect_trigger 透传：自定义阈值生效（突破幅度/量比/幅度上限 0=关闭/悬空距离 0=关闭）
3. PUT /config 的 _validate_scan_filters 校验（未知键/越界 400）

K线夹具按阴阳方向排列列序（ lessons：阳线 high=close/low=open 对齐），
autouse monkeypatch 固定 settings 基线，免疫本地 .env 覆盖（BREAKOUT_THRESHOLD=0.005）。
"""
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest  # noqa: E402

from app.config import settings  # noqa: E402
from app.services.scan_filters import (  # noqa: E402
    ALL_TRIGGERS,
    resolve_scan_filters,
)
from app.services.strategy import trigger as trig_mod  # noqa: E402  # detect_all_patterns 打桩用
from app.services.strategy.trigger import (  # noqa: E402
    BREAKOUT,
    PULLBACK_CONFIRM,
    ScanFilters,
    detect_trigger,
)


@pytest.fixture(autouse=True)
def _fixed_settings(monkeypatch):
    """固定筛选基线，免疫本地/服务器 .env 的 pydantic-settings 覆盖"""
    monkeypatch.setattr(settings, "MIN_VOLUME_24H", 3_000_000)
    monkeypatch.setattr(settings, "BREAKOUT_CHANNEL_BARS", 20)
    monkeypatch.setattr(settings, "BREAKOUT_THRESHOLD", 0.002)
    monkeypatch.setattr(settings, "BREAKOUT_VOL_RATIO", 1.2)
    monkeypatch.setattr(settings, "BREAKOUT_MAX_PCT", 1.5)
    monkeypatch.setattr(settings, "CANDLE_MAX_EMA21_DIST_PCT", 1.5)
    monkeypatch.setattr(settings, "AI_MAX_PER_SCAN", 10)


# ── resolve_scan_filters：优先级与容错 ────────────────────────────────


def test_resolve_none_falls_back_to_settings():
    flt = resolve_scan_filters(None)
    assert flt.min_volume_24h == 3_000_000
    assert flt.breakout_threshold == 0.002
    assert flt.breakout_vol_ratio == 1.2
    assert flt.breakout_max_pct == 1.5
    assert flt.candle_max_ema21_dist_pct == 1.5
    assert flt.breakout_channel_bars == 20
    assert flt.ai_max_per_scan == 10
    assert flt.new_coin_cooldown_days == 14


def test_resolve_db_overrides_settings():
    cfg = SimpleNamespace(scan_filters={
        "min_volume_24h": 5_000_000,
        "breakout_threshold": 0.003,
        "breakout_vol_ratio": 1.5,
        "breakout_max_pct": 0,          # 0=关闭是合法值
        "candle_max_ema21_dist_pct": 2.0,
        "breakout_channel_bars": 30,
        "ai_max_per_scan": 5,
    })
    flt = resolve_scan_filters(cfg)
    assert flt.min_volume_24h == 5_000_000
    assert flt.breakout_threshold == 0.003
    assert flt.breakout_vol_ratio == 1.5
    assert flt.breakout_max_pct == 0.0
    assert flt.candle_max_ema21_dist_pct == 2.0
    assert flt.breakout_channel_bars == 30
    assert flt.ai_max_per_scan == 5


def test_resolve_partial_keys_fall_back():
    """部分定制：缺键逐项回落 env，而非整组失效"""
    cfg = SimpleNamespace(scan_filters={"breakout_vol_ratio": 2.0})
    flt = resolve_scan_filters(cfg)
    assert flt.breakout_vol_ratio == 2.0
    assert flt.breakout_threshold == 0.002
    assert flt.ai_max_per_scan == 10


def test_resolve_invalid_values_fall_back():
    """非法值（垃圾串/负数）逐键容错回落，不抛异常"""
    cfg = SimpleNamespace(scan_filters={
        "min_volume_24h": "abc",
        "breakout_vol_ratio": -1,       # 负数非法
        "ai_max_per_scan": 0,           # 低于下限 1
        "breakout_channel_bars": 1,     # 低于内部下限 2
    })
    flt = resolve_scan_filters(cfg)
    assert flt.min_volume_24h == 3_000_000
    assert flt.breakout_vol_ratio == 1.2
    assert flt.ai_max_per_scan == 10
    assert flt.breakout_channel_bars == 20


def test_resolve_null_column_and_none_cfg():
    assert resolve_scan_filters(SimpleNamespace(scan_filters=None)) == resolve_scan_filters(None)


def test_resolve_inf_nan_fall_back():
    """inf/nan（直改 DB 可注入）：isfinite 防护回落默认，int(inf) 的
    OverflowError 也须被吞掉而非烧掉调用方"""
    cfg = SimpleNamespace(scan_filters={
        "min_volume_24h": "inf",
        "breakout_threshold": float("nan"),
        "breakout_channel_bars": "inf",
        "ai_max_per_scan": float("inf"),
    })
    flt = resolve_scan_filters(cfg)
    assert flt.min_volume_24h == 3_000_000
    assert flt.breakout_threshold == 0.002
    assert flt.breakout_channel_bars == 20
    assert flt.ai_max_per_scan == 10


# ── detect_trigger 透传 ───────────────────────────────────────────────


def _flat_bars(n=50, close=100.0, hi_off=0.2, lo_off=0.2, vol=100.0):
    ts = 1_700_000_000_000
    return [[ts + i * 3_600_000, close, close + hi_off, close - lo_off, close, vol, vol]
            for i in range(n)]


def _breakout_klines(close=101.5, vol=250.0):
    """48 根平盘 + 局部摆动前高（close=101.0，其后 3 根更低收盘确认且未破）
    + 放量阳线信号K（klines[-2]）+ 未收盘根；突破位=摆动结构前高口径"""
    kl = _flat_bars(48)
    ts0 = 1_700_000_000_000
    for i, c in ((48, 101.0), (49, 99.9), (50, 99.9), (51, 99.9)):
        kl.append([ts0 + i * 3_600_000, 100.0, c + 0.1, c - 0.1, c, 100.0, 100.0])
    ts = ts0 + 52 * 3_600_000
    # 信号K线：o=100.2 c=close h=close+0.1 l=100.1（阳线长实体，body/range≥0.70）
    kl.append([ts, 100.2, close + 0.1, 100.1, close, vol, vol])
    kl.append([ts + 3_600_000, close, close + 0.1, close - 0.1, close, vol, vol])  # 未收盘
    return kl


def test_trigger_breakout_with_default_threshold():
    trig, op = detect_trigger(_breakout_klines(), 1)
    assert trig is not None and trig["type"] == BREAKOUT


def test_trigger_respects_custom_breakout_threshold():
    """阈值 5%：收盘 101.5 仅越前高 1.3%（101.0 前高），自定义 flt 拦下（透传生效）；
    形态打桩隔离金K分支（信号K线本体会被判看涨吞没+触碰 EMA21）"""
    flt = ScanFilters(
        min_volume_24h=3_000_000, breakout_channel_bars=20,
        breakout_threshold=0.05, breakout_vol_ratio=1.2,
        breakout_max_pct=1.5, candle_max_ema21_dist_pct=1.5,
        ai_max_per_scan=10,
    )
    with patch.object(trig_mod, "detect_all_patterns", return_value=[]):
        trig, _ = detect_trigger(_breakout_klines(), 1, flt=flt)
    assert trig is None


def test_trigger_vol_ratio_not_hard_gate():
    """量比门槛 5：信号K量 250/中位量 100=2.5 不达标 → 放量为加分项不卡死
    （2026-09-29），仍触发且 reason 无"放量"前缀（形态打桩同上）"""
    flt = ScanFilters(
        min_volume_24h=3_000_000, breakout_channel_bars=20,
        breakout_threshold=0.002, breakout_vol_ratio=5.0,
        breakout_max_pct=1.5, candle_max_ema21_dist_pct=1.5,
        ai_max_per_scan=10,
    )
    with patch.object(trig_mod, "detect_all_patterns", return_value=[]):
        trig, _ = detect_trigger(_breakout_klines(), 1, flt=flt)
    assert trig is not None and trig["type"] == BREAKOUT
    assert "放量" not in trig["reason"]


def test_trigger_breakout_max_pct_zero_disables_chase_guard():
    """越前高 3.0%：默认 1.5% 上限拦下；上限 0（关闭）放行突破（形态打桩隔离金K分支）"""
    kl = _breakout_klines(close=104.0)
    with patch.object(trig_mod, "detect_all_patterns", return_value=[]):
        trig, _ = detect_trigger(kl, 1)
    assert trig is None  # 默认 1.5% 追高拦截
    flt = ScanFilters(
        min_volume_24h=3_000_000, breakout_channel_bars=20,
        breakout_threshold=0.002, breakout_vol_ratio=1.2,
        breakout_max_pct=0, candle_max_ema21_dist_pct=1.5,
        ai_max_per_scan=10,
    )
    with patch.object(trig_mod, "detect_all_patterns", return_value=[]):
        trig, _ = detect_trigger(kl, 1, flt=flt)
    assert trig is not None and trig["type"] == BREAKOUT


def _hanging_hammer_klines():
    """50 根平盘 + 远离 EMA21 的悬空锤形线（下影≥2×实体、上影≤15%全长）"""
    kl = _flat_bars(50)
    ts = 1_700_000_000_000 + 50 * 3_600_000
    # 锤形线：o=105.2 c=105.4（body=0.2）l=103.6（下影1.6≥2×0.2）h=105.5（上影0.1）
    kl.append([ts, 105.2, 105.5, 103.6, 105.4, 150.0, 150.0])
    kl.append([ts + 3_600_000, 105.4, 105.5, 105.3, 105.4, 150.0, 150.0])
    return kl


def test_trigger_hanging_golden_blocked_by_default():
    """悬空金K（距 EMA21 ≈5%）无触碰 → 不触发（纯金K 2026-09-29 下线，
    金K必须与触点配合；原悬空距离门槛随之下线，此处覆盖极端距离）"""
    trig, _ = detect_trigger(_hanging_hammer_klines(), 1)
    assert trig is None


# ── _validate_scan_filters（PUT /config 校验器）────────────────────────


def test_validate_scan_filters_clean_and_reject():
    from fastapi import HTTPException

    from app.api.scan import _validate_scan_filters
    out = _validate_scan_filters({"breakout_vol_ratio": "1.8", "ai_max_per_scan": 5})
    assert out == {"breakout_vol_ratio": 1.8, "ai_max_per_scan": 5.0}

    with pytest.raises(HTTPException):
        _validate_scan_filters({"unknown_key": 1})
    with pytest.raises(HTTPException):
        _validate_scan_filters({"breakout_max_pct": 99})
    with pytest.raises(HTTPException):
        _validate_scan_filters({"breakout_vol_ratio": "x"})


# ── 新币冷静期（2026-10-04 NIGHT 案例：上市未满 N 天不进扫描）──────────


def test_resolve_new_coin_cooldown():
    """DB 覆盖生效；0=关闭是合法值；负数/垃圾串逐键容错回落 env 默认"""
    flt = resolve_scan_filters(SimpleNamespace(scan_filters={"new_coin_cooldown_days": 7}))
    assert flt.new_coin_cooldown_days == 7
    assert resolve_scan_filters(
        SimpleNamespace(scan_filters={"new_coin_cooldown_days": 0})
    ).new_coin_cooldown_days == 0
    bad = resolve_scan_filters(SimpleNamespace(scan_filters={
        "new_coin_cooldown_days": "abc", "x": 1}))
    assert bad.new_coin_cooldown_days == 14


def test_validate_scan_filters_new_coin_cooldown():
    """PUT 校验：0~90 合法，越界 400"""
    from fastapi import HTTPException

    from app.api.scan import _validate_scan_filters
    out = _validate_scan_filters({"new_coin_cooldown_days": 14})
    assert out == {"new_coin_cooldown_days": 14.0}
    assert _validate_scan_filters({"new_coin_cooldown_days": 0}) == {"new_coin_cooldown_days": 0.0}
    with pytest.raises(HTTPException):
        _validate_scan_filters({"new_coin_cooldown_days": 91})
    with pytest.raises(HTTPException):
        _validate_scan_filters({"new_coin_cooldown_days": -1})


def test_config_effective_contains_all_filter_fields():
    """/config 的 scan_filters_effective 须覆盖 FILTER_FIELDS 全部数值键——
    前端表单初值按此渲染，漏键即「配置里不显示默认值」（2026-10-04
    new_coin_cooldown_days 漏挂案例）"""
    import re
    from pathlib import Path

    src = (Path(__file__).resolve().parents[1]
           / "app" / "api" / "scan.py").read_text(encoding="utf-8")
    block = src.split("scan_filters_effective={", 1)[1].split("},", 1)[0]
    emitted = set(re.findall(r'"([a-z0-9_]+)":', block))
    from app.services.scan_filters import SCAN_FILTER_KEYS
    numeric_keys = {k for k in SCAN_FILTER_KEYS if k not in ("allowed_triggers", "pullback_touch_refs")}
    missing = numeric_keys - emitted
    assert not missing, f"scan_filters_effective 漏键: {missing}"


# ── 触发类型白名单（2026-09-29：结构突破/趋势突破/回踩金K确认，纯金K下线）───


def test_resolve_trigger_whitelist():
    cfg = SimpleNamespace(scan_filters={"allowed_triggers": ["trigger_pullback_confirm"]})
    flt = resolve_scan_filters(cfg)
    assert flt.allowed_triggers == ("trigger_pullback_confirm",)


def test_resolve_trigger_whitelist_trend_breakout():
    """趋势突破（2026-09-29 新类型）与趋势回踩（2026-10-10 融合新类型）合法可勾选；
    纯金K已从 ALL_TRIGGERS 移除，存量配置含它时被过滤、其余合法项保留"""
    from app.services.scan_filters import (
        TRIGGER_TREND_BREAKOUT,
        TRIGGER_TREND_PULLBACK,
    )
    cfg = SimpleNamespace(scan_filters={"allowed_triggers": [
        "trigger_breakout", "trigger_trend_breakout",
        "trigger_pullback_confirm", "trigger_trend_pullback"]})
    assert resolve_scan_filters(cfg).allowed_triggers == ALL_TRIGGERS
    legacy = SimpleNamespace(scan_filters={"allowed_triggers": [
        "trigger_candlestick", "trigger_breakout"]})
    assert resolve_scan_filters(legacy).allowed_triggers == ("trigger_breakout",)
    assert TRIGGER_TREND_BREAKOUT in ALL_TRIGGERS
    assert TRIGGER_TREND_PULLBACK in ALL_TRIGGERS


def test_resolve_trigger_whitelist_invalid_falls_back():
    """非 list/全不勾/全非法 → 回落全量（静默空转比多出信号更难排查）"""
    for bad in ("trigger_breakout", 3, [], ["bogus"], ["bogus", 1], None):
        cfg = SimpleNamespace(scan_filters={"allowed_triggers": bad})
        assert resolve_scan_filters(cfg).allowed_triggers == ALL_TRIGGERS, bad
    # 非法项过滤、合法项保留
    cfg = SimpleNamespace(scan_filters={"allowed_triggers": ["trigger_breakout", "bogus"]})
    assert resolve_scan_filters(cfg).allowed_triggers == ("trigger_breakout",)


def test_validate_scan_filters_trigger_whitelist():
    from fastapi import HTTPException

    from app.api.scan import _validate_scan_filters
    out = _validate_scan_filters({"allowed_triggers": ["trigger_pullback_confirm"]})
    assert out == {"allowed_triggers": ["trigger_pullback_confirm"]}
    with pytest.raises(HTTPException):
        _validate_scan_filters({"allowed_triggers": []})
    with pytest.raises(HTTPException):
        _validate_scan_filters({"allowed_triggers": ["bogus"]})
    with pytest.raises(HTTPException):
        _validate_scan_filters({"allowed_triggers": "trigger_breakout"})
    with pytest.raises(HTTPException):
        _validate_scan_filters({"unknown_key": 1})


def test_candidate_whitelist_drops_disallowed_triggers(monkeypatch):
    """scanner 白名单：白名单外触发类型不产生候选；pending 处置照常透传"""
    import app.services.scanner as scanner_mod

    votes = {"bull": 5, "bear": 0, "bull_n": 6, "bear_n": 6,
             "net": 5, "voted": 6, "results": {}}
    monkeypatch.setattr(scanner_mod, "analyze_ema", lambda k: None)
    monkeypatch.setattr(scanner_mod, "classify_volume", lambda k: (1.0, "平量"))
    monkeypatch.setattr(scanner_mod, "compute_indicator_votes", lambda k, h=None: votes)

    class FakePool:
        def get_klines(self, *a, **k):
            return []

    breakout = {"type": BREAKOUT, "direction": 1, "position": "resistance",
                "pattern": None, "breakout_pct": 0.5, "reason": "r"}
    monkeypatch.setattr(scanner_mod, "detect_trigger",
                        lambda *a, **k: (dict(breakout), None))

    def _flt(allowed):
        return ScanFilters(
            min_volume_24h=3_000_000, breakout_channel_bars=20,
            breakout_threshold=0.002, breakout_vol_ratio=1.2,
            breakout_max_pct=1.5, candle_max_ema21_dist_pct=1.5,
            ai_max_per_scan=10, allowed_triggers=allowed,
        )

    # 白名单只留回踩 → 突破触发被丢弃
    cand, op = scanner_mod.detect_indicator_candidate(
        FakePool(), "XUSDT", _flat_bars(), flt=_flt((PULLBACK_CONFIRM,)))
    assert cand is None and op is None
    # 白名单含突破 → 候选照常产生
    cand, _ = scanner_mod.detect_indicator_candidate(
        FakePool(), "XUSDT", _flat_bars(), flt=_flt(ALL_TRIGGERS))
    assert cand is not None and cand["signal_type"] == BREAKOUT
