# -*- coding: utf-8 -*-
"""入场门槛动态块回归（render_signal_gate_block，原 review_stats_inject 测试留存部分）。

战绩回注（2026-09-28 上线、2026-09-29 下线）已整体移除：胜率/EV 不再注入 AI
提示词，评估只看当下事实（避免历史样本小/阶段切换时压制合理信号）。

覆盖：
1. render_signal_gate_block：触发器类型输出动态阈值（读 scan_filters 生效值，
   消除页面配置分叉）；非触发器类型空串；0=关闭项不输出
2. _build_messages 接线：门槛块/触点行进 user 消息

autouse monkeypatch 固定 settings 基线，免疫本地 .env 覆盖。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest  # noqa: E402

from app.config import settings  # noqa: E402
from app.services import ai_analyzer  # noqa: E402
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
    monkeypatch.setattr(settings, "STRUCTURE_TOUCH_BAND_PCT", 0.3)


# ── render_signal_gate_block ──────────────────────────────────────────


def test_gate_block_renders_live_thresholds():
    block = render_signal_gate_block("trigger_breakout")
    assert "收盘越过最近一个未被突破的摆动结构前高" in block
    assert "≥0.2%" in block
    assert ">1.5% 视为追高" in block
    assert "1.2 倍" in block


def test_gate_block_trend_breakout_renders_donchian():
    """趋势突破（2026-09-29）：渲染 Donchian 轨口径门槛，通道根数读生效值"""
    block = render_signal_gate_block("trigger_trend_breakout")
    assert "20 根 Donchian 滚动高低轨" in block
    assert "≥0.2%" in block
    assert "1.2 倍" in block
    # 纯金K已下线（金K必须与触点配合）→ 不再渲染门槛块
    assert render_signal_gate_block("trigger_candlestick") == ""


def test_gate_block_breakout_near_anchor_chain():
    """突破类门槛块含止损锚降级链（docs/23）：降级链+前提硬约束+资格附加线"""
    for st in ("trigger_breakout", "trigger_trend_breakout"):
        block = render_signal_gate_block(st)
        assert "止损锚（突破类专用）" in block
        assert "近锚适用前提" in block          # 仅 RR 不达标才降级
        assert "近锚资格附加线" in block        # 量比<1 无资格 / RR>4 重估 / 0.3×ATR
        assert 'stop_anchor="near"' in block
        assert "金K窗口" in block
        assert "最近 5 根已收盘K线共同极值" in block  # 近锚=金K窗口极值口径（2026-10-12-8）


def test_gate_block_pullback_no_near_anchor():
    """近锚降级链仅注入突破类，回踩类块不出现（防注入面扩大）"""
    block = render_signal_gate_block("trigger_pullback_confirm")
    assert "止损锚（突破类专用）" not in block
    assert "stop_anchor" not in block


def test_gate_block_disabled_items_omitted():
    block = render_signal_gate_block("trigger_pullback_confirm")
    assert "悬空金K拦截" not in block
    assert "追高上限未启用" not in block  # BREAKOUT_MAX_PCT 仍 1.5


def test_gate_block_non_trigger_empty():
    assert render_signal_gate_block("uptrend") == ""
    assert render_signal_gate_block("manual_search") == ""


def test_gate_block_pullback_touch_object():
    """回踩类门槛块（docs/22 触点体系）：触点 5 类说明 + 带宽口径（读生效值）；
    突破专属行（Donchian/量比）不出现在回踩块里"""
    block = render_signal_gate_block("trigger_pullback_confirm")
    assert "回踩触点" in block
    assert "retest=突破回测位" in block
    assert "两段式确认" in block
    assert "0.3%" in block  # structure_touch_band_pct 生效值
    assert "Donchian" not in block  # 突破专属行不进回踩块


# ── 止损宽度硬上限透传（2026-09-29 max_stop_pct 注入）──────────────────


def test_gate_block_stop_cap_injected():
    """配置 max_stop_pct → 门槛块注入宽度硬上限，突破类带"超限无降级资格"尾句"""
    block = render_signal_gate_block("trigger_breakout", stop_cap_pct=0.05)
    assert "止损宽度硬上限" in block
    assert "不得超过 5%" in block
    assert "结构锚宽度已超上限时无近锚降级资格" in block
    # 趋势突破与回踩类同样注入（回踩类无近锚尾句，宽度闸对全类型生效）
    assert "止损宽度硬上限" in render_signal_gate_block(
        "trigger_trend_breakout", stop_cap_pct=0.05)
    pullback = render_signal_gate_block(
        "trigger_pullback_confirm", stop_cap_pct=0.05)
    assert "止损宽度硬上限" in pullback
    assert "近锚" not in pullback


def test_gate_block_stop_cap_none_omitted():
    """未配置（None）→ 不注入宽度行（NULL=不限制，与程序闸门口径一致）"""
    for st in ("trigger_breakout", "trigger_pullback_confirm"):
        block = render_signal_gate_block(st)
        assert "止损宽度硬上限" not in block


def test_build_messages_injects_stop_cap():
    """_build_messages 接线：stop_cap_pct 透传到 user 消息"""
    user = _build_messages(
        _minimal_signal("trigger_breakout"), _KLINES, stop_cap_pct=0.05,
    )[-1]["content"]
    assert "止损宽度硬上限" in user
    assert "不得超过 5%" in user
    # 不传 → 不出现
    user2 = _build_messages(_minimal_signal("trigger_breakout"), _KLINES)
    assert "止损宽度硬上限" not in user2[-1]["content"]


# ── _build_messages 接线 ──────────────────────────────────────────────


def _minimal_signal(signal_type):
    return {
        "symbol": "TESTUSDT", "signal_type": signal_type,
        "current_price": 100.0, "breakout_pct": 0.5, "pattern": None,
        "signal_reason": "r", "position": "support", "volume_type": "正常",
        "volume": 1.0, "recent_swings": {},
    }


_KLINES = [[0, 100.0, 101.0, 99.0, 100.5, 10.0, 0, 10000.0]] * 3


@pytest.fixture(autouse=True)
def _stub_blocks(monkeypatch):
    """桩掉消息构造中的 K 线派生块（EMA/指标），只验证注入接线"""
    monkeypatch.setattr(ai_analyzer, "analyze_ema", lambda _k: None)
    monkeypatch.setattr(ai_analyzer, "render_indicator_block", lambda _k, _h, **_kw: "")


def test_build_messages_injects_gate():
    msgs = _build_messages(_minimal_signal("trigger_breakout"), _KLINES)
    user = msgs[-1]["content"]
    assert "程序入场门槛" in user
    assert "历史战绩" not in user  # 战绩回注已下线，不再出现


def test_build_messages_non_trigger_no_gate():
    msgs = _build_messages(_minimal_signal("uptrend"), _KLINES)
    user = msgs[-1]["content"]
    assert "程序入场门槛" not in user


# ── 回踩触点接线（docs/22）────────────────────────────────────────────


def test_build_messages_injects_touch_line():
    """信号带 touch_ref → user 消息渲染「回踩触点」行（中文标签+原值）"""
    sig = {**_minimal_signal("trigger_pullback_confirm"), "touch_ref": "retest"}
    user = _build_messages(sig, _KLINES)[-1]["content"]
    assert "回踩触点: 突破回测位（retest）" in user


def test_build_messages_no_touch_line_without_ref():
    """无触点信号（历史行/非回踩类型）不渲染触点行"""
    user = _build_messages(
        _minimal_signal("trigger_pullback_confirm"), _KLINES,
    )[-1]["content"]
    assert "回踩触点: 突破回测位（" not in user
