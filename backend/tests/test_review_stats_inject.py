# -*- coding: utf-8 -*-
"""战绩回注（review_stats_inject）与入场门槛动态块回归（2026-09-28）。

覆盖：
1. build_track_record_block：白名单外类型/样本不足/查询失败 → 空串（不阻断分析）；
   样本充足 → 胜率/EV 文案（口径与 /review/stats 同源：expired 不计入、EV 扣费用）
2. render_signal_gate_block：触发器类型输出动态阈值（读 scan_filters 生效值，
   消除页面配置分叉）；非触发器类型空串；0=关闭项不输出
3. _build_messages 接线：track_record 块与门槛块进 user 消息

DB 用 FakeSession 双响应序列（复盘行查询 + AIAnalysis 三价查询），不建真实库；
autouse monkeypatch 固定 settings 基线，免疫本地 .env 覆盖。
"""
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest  # noqa: E402

from app.config import settings  # noqa: E402
from app.services import ai_analyzer  # noqa: E402
from app.services.review_stats_inject import build_track_record_block  # noqa: E402
from app.services.ai_analyzer import (  # noqa: E402
    _build_messages,
    render_signal_gate_block,
)


@pytest.fixture(autouse=True)
def _fixed_settings(monkeypatch):
    """固定筛选基线，免疫本地/服务器 .env 的 pydantic-settings 覆盖"""
    monkeypatch.setattr(settings, "BREAKOUT_CHANNEL_BARS", 20)
    monkeypatch.setattr(settings, "BREAKOUT_THRESHOLD", 0.002)
    monkeypatch.setattr(settings, "BREAKOUT_VOL_RATIO", 1.2)
    monkeypatch.setattr(settings, "BREAKOUT_MAX_PCT", 1.5)
    monkeypatch.setattr(settings, "CANDLE_MAX_EMA21_DIST_PCT", 1.5)


class FakeResult:
    def __init__(self, items):
        self._items = items

    def scalars(self):
        return self

    def all(self):
        return self._items


class FakeSession:
    """按调用顺序返回预置结果集；raise_exc 时首个 execute 直接抛错"""

    def __init__(self, results, raise_exc=False):
        self._results = list(results)
        self._raise = raise_exc

    def execute(self, *_a, **_kw):
        if self._raise:
            raise RuntimeError("db down")
        return FakeResult(self._results.pop(0))


def _review(signal_type="trigger_breakout", outcome="win_tp1",
            ai_analysis_id=None, bars_to_exit=8):
    return SimpleNamespace(
        signal_type=signal_type, outcome=outcome,
        ai_analysis_id=ai_analysis_id, bars_to_exit=bars_to_exit,
    )


def _ai_price(aid, entry=100.0, stop=95.0, tp1=107.5, tp2=None):
    return SimpleNamespace(
        id=aid, entry_price=entry, stop_loss=stop,
        take_profit_1=tp1, take_profit_2=tp2,
    )


# ── build_track_record_block ──────────────────────────────────────────


def test_non_trigger_type_returns_empty():
    for st in ("manual_search", "uptrend", "downtrend", ""):
        assert build_track_record_block(FakeSession([]), st) == ""


def test_insufficient_sample_returns_empty():
    rows = [_review(outcome="win_tp1", ai_analysis_id=None) for _ in range(4)]
    assert build_track_record_block(FakeSession([rows]), "trigger_breakout") == ""


def test_db_failure_returns_empty():
    assert build_track_record_block(FakeSession([], raise_exc=True),
                                    "trigger_breakout") == ""


def test_block_contains_win_rate_and_ev():
    # 4 盈 2 亏 + 1 未决：胜率 67%；EV：win=1.5R-费用0.026R，loss=-1R-费用0.026R
    # → (4*1.474 + 2*(-1.026))/6 = 0.6407 → +0.64R
    rows = [_review(outcome="win_tp1", ai_analysis_id=f"a{i}") for i in range(4)]
    rows += [_review(outcome="loss", ai_analysis_id=f"b{i}") for i in range(2)]
    rows.append(_review(outcome="expired"))  # 未决：计入样本、不计入胜率
    prices = [_ai_price(f"a{i}") for i in range(4)] + [_ai_price("b0"), _ai_price("b1")]
    db = FakeSession([rows, prices])
    block = build_track_record_block(db, "trigger_breakout")
    assert "样本7笔" in block
    assert "胜率 67%" in block
    assert "未决1笔不计入" in block
    assert "+0.64R" in block
    assert "同向突破" in block  # 家族展示名


def test_ev_skips_rows_without_ai_prices():
    # 无 AI 三价的行跳过 EV（与 /review/stats._ev_r 同口径）：仅 2 笔带三价的亏损
    rows = [_review(outcome="loss") for _ in range(3)]  # 无 ai_analysis_id
    rows += [_review(outcome="loss", ai_analysis_id="b0"),
             _review(outcome="loss", ai_analysis_id="b1")]
    db = FakeSession([rows, [_ai_price("b0"), _ai_price("b1")]])
    block = build_track_record_block(db, "trigger_breakout")
    assert "-1.03R" in block  # -1 - 0.026 ≈ -1.03


def test_expired_only_returns_empty():
    rows = [_review(outcome="expired") for _ in range(6)]
    assert build_track_record_block(FakeSession([rows]), "trigger_breakout") == ""


# ── render_signal_gate_block ──────────────────────────────────────────


def test_gate_block_renders_live_thresholds():
    block = render_signal_gate_block("trigger_breakout")
    assert "收盘越过 20 根高低轨" in block
    assert "≥0.2%" in block
    assert ">1.5% 视为追高" in block
    assert "1.2 倍" in block
    # 悬空金K拦截行归属回踩/金K块（突破块只渲染突破门槛）
    pullback_block = render_signal_gate_block("trigger_candlestick")
    assert ">1.5% 不触发" in pullback_block


def test_gate_block_disabled_items_omitted(monkeypatch):
    monkeypatch.setattr(settings, "CANDLE_MAX_EMA21_DIST_PCT", 0)
    block = render_signal_gate_block("trigger_pullback_confirm")
    assert "悬空金K拦截" not in block
    assert "追高上限未启用" not in block  # BREAKOUT_MAX_PCT 仍 1.5


def test_gate_block_non_trigger_empty():
    assert render_signal_gate_block("uptrend") == ""
    assert render_signal_gate_block("manual_search") == ""


def test_gate_block_pullback_touch_object_by_mode(monkeypatch):
    """回踩类门槛块：触碰对象文案随 pullback_key_level_only 切换；
    突破专属行（Donchian/量比）不再出现在回踩块里"""
    from dataclasses import replace

    from app.services import scan_filters as sf_mod

    base = sf_mod.resolve_scan_filters(None)

    def _with_mode(on: bool):
        return replace(base, pullback_key_level_only=on)

    monkeypatch.setattr(sf_mod, "resolve_scan_filters",
                        lambda _cfg=None: _with_mode(False))
    block = render_signal_gate_block("trigger_pullback_confirm")
    assert "已确认关键位 或 EMA21" in block
    assert "两段式确认" in block
    assert "Donchian" not in block  # 突破专属行不进回踩块

    monkeypatch.setattr(sf_mod, "resolve_scan_filters",
                        lambda _cfg=None: _with_mode(True))
    block = render_signal_gate_block("trigger_pullback_confirm")
    assert "仅已确认关键位" in block
    assert "EMA21 不作为触碰对象" in block


# ── _build_messages 接线 ──────────────────────────────────────────────


def _minimal_signal(signal_type):
    return {
        "symbol": "TESTUSDT", "signal_type": signal_type,
        "current_price": 100.0, "breakout_pct": 0.5, "pattern": None,
        "signal_reason": "r", "position": "support", "volume_type": "正常",
        "volume": 1.0, "recent_swings": {}, "key_levels": None,
    }


_KLINES = [[0, 100.0, 101.0, 99.0, 100.5, 10.0, 0, 10000.0]] * 3


@pytest.fixture(autouse=True)
def _stub_blocks(monkeypatch):
    """桩掉消息构造中的 K 线派生块（EMA/指标），只验证注入接线"""
    monkeypatch.setattr(ai_analyzer, "analyze_ema", lambda _k: None)
    monkeypatch.setattr(ai_analyzer, "render_indicator_block", lambda _k, _h: "")


def test_build_messages_injects_track_record_and_gate():
    msgs = _build_messages(
        _minimal_signal("trigger_breakout"), _KLINES, track_record="TEST_TRACK_BLOCK",
    )
    user = msgs[-1]["content"]
    assert "TEST_TRACK_BLOCK" in user
    assert "程序入场门槛" in user


def test_build_messages_non_trigger_no_gate():
    msgs = _build_messages(_minimal_signal("uptrend"), _KLINES, track_record="")
    user = msgs[-1]["content"]
    assert "程序入场门槛" not in user
    assert "历史战绩" not in user
