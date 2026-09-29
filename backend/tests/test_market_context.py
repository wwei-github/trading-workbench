# -*- coding: utf-8 -*-
"""市场环境注入回归（docs/24 A4①）：AI 事实包追加资金费率 + BTC 状态摘要。

P0 单次调用管线无工具，普跌日 AI 无环境感知；注入后 AI 可感知费率拥挤与
BTC 急跌。全部数据源失败静默降级为不注入。

覆盖：
1. build_market_context：费率行格式/异常阈值（>0.1% 警示，多空拥挤方向）、
   BTC 4h 趋势标签（多头/空头/纠缠）、1h 近3根累计涨跌与急跌警示阈值
   （BTC_CRASH_PAUSE_PCT，0=关闭）、查询失败逐段降级/整体 None
2. get_futures_klines 薄封装（MockTransport，不触网）
3. _build_messages 接线：紧跟指标块注入、开关关闭不注入、构建异常不报错
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx  # noqa: E402
import pytest  # noqa: E402

from app.config import settings  # noqa: E402
from app.services import ai_analyzer, market_data  # noqa: E402
from app.services.ai_analyzer import _build_messages  # noqa: E402
from app.services.binance_client import BinanceClient  # noqa: E402
from app.services.market_data import build_market_context  # noqa: E402


# ── 构造工具 ──────────────────────────────────────────────────────────


def _klines(closes):
    """由收盘价序列构造 K 线（h=c+1, l=c-1, o=前收；最后一根模拟未收盘）"""
    out = []
    for i, c in enumerate(closes):
        ts = i * 3_600_000
        o = closes[i - 1] if i else c
        out.append([ts, o, c + 1.0, c - 1.0, c, 10.0, ts + 3_599_000, 1000.0])
    return out


class FakeClient:
    """按 interval 返回预设 K 线的假 BinanceClient"""

    def __init__(self, k4=None, k1=None, fail=False):
        self.k4, self.k1, self.fail = k4, k1, fail
        self.calls = []

    def get_futures_klines(self, symbol, interval, limit):
        self.calls.append((symbol, interval, limit))
        if self.fail:
            raise RuntimeError("网络不可用")
        assert symbol == "BTCUSDT"
        return self.k4 if interval == "4h" else self.k1


def _funding(rate_pct):
    return {"funding_rate_pct": rate_pct, "open_interest": 123.0}


@pytest.fixture
def no_funding(monkeypatch):
    """默认无费率数据（各用例按需覆盖）"""
    monkeypatch.setattr(market_data, "get_funding", lambda s: None)


# ── 资金费率行 ────────────────────────────────────────────────────────


def test_funding_line_normal(no_funding, monkeypatch):
    monkeypatch.setattr(market_data, "get_funding", lambda s: _funding(0.0123))
    out = build_market_context(FakeClient(), "TESTUSDT")
    assert out is not None
    assert "资金费率 0.0123%/8h" in out
    assert "费率异常" not in out


@pytest.mark.parametrize("rate,side", [(0.25, "多头拥挤"), (-0.25, "空头拥挤")])
def test_funding_line_abnormal_warning(no_funding, monkeypatch, rate, side):
    """绝对值 >0.1% → 追加拥挤方向与逆向风险警示"""
    monkeypatch.setattr(market_data, "get_funding", lambda s: _funding(rate))
    out = build_market_context(FakeClient(), "TESTUSDT")
    assert f"资金费率 {rate:.4f}%/8h" in out
    assert f"（费率异常，{side}，逆向风险）" in out


def test_funding_line_threshold_boundary(no_funding, monkeypatch):
    """恰为 0.1%（不大于）不算异常，不警示"""
    monkeypatch.setattr(market_data, "get_funding", lambda s: _funding(0.1))
    out = build_market_context(FakeClient(), "TESTUSDT")
    assert "资金费率 0.1000%/8h" in out
    assert "费率异常" not in out


# ── BTC 状态行 ────────────────────────────────────────────────────────


def _btc_client(trend_step, chg_closes):
    k4 = _klines([50000.0 + i * trend_step for i in range(60)])
    k1 = _klines(chg_closes)
    return FakeClient(k4=k4, k1=k1)


def test_btc_line_bullish_with_change(no_funding):
    client = _btc_client(20.0, [100.0] * 21 + [100.3, 100.5, 100.5, 100.5])
    out = build_market_context(client, "TESTUSDT")
    assert out is not None
    assert "BTC 4h 多头" in out
    # 近3根已收盘累计：closed[-1]/closed[-4]-1 = 100.5/100.0-1 = +0.5%
    assert "1h 近3根 +0.50%" in out
    assert "BTC 短线急跌" not in out
    # 拉取规格：4h 60 根（EMA55 成形需 ≥55 已收盘）+ 1h 24 根
    assert ("BTCUSDT", "4h", 60) in client.calls
    assert ("BTCUSDT", "1h", 24) in client.calls


def test_btc_line_bearish_and_entangled(no_funding):
    bear = build_market_context(_btc_client(-20.0, [100.0] * 25), "TESTUSDT")
    assert "BTC 4h 空头" in bear
    flat = build_market_context(_btc_client(0.0, [100.0] * 25), "TESTUSDT")
    assert "BTC 4h 纠缠" in flat


def test_btc_crash_warning_threshold(no_funding, monkeypatch):
    """1h 近3根累计跌幅超阈值 → 急跌警示；阈值 0=关闭"""
    # 98.0/100.0-1 = -2.0%（未收盘根不计入）
    client = _btc_client(10.0, [100.0] * 20 + [99.5, 99.0, 98.0, 98.0])
    monkeypatch.setattr(settings, "BTC_CRASH_PAUSE_PCT", 1.0)
    out = build_market_context(client, "TESTUSDT")
    assert "1h 近3根 -2.00%" in out
    assert "（BTC 短线急跌，山寨开单需谨慎）" in out
    # 跌幅未超阈值 → 无警示
    client2 = _btc_client(10.0, [100.0] * 20 + [99.8, 99.6, 99.5, 99.5])
    assert "BTC 短线急跌" not in build_market_context(client2, "TESTUSDT")
    # 阈值 0 = 关闭警示（仍注入涨跌幅本身）
    monkeypatch.setattr(settings, "BTC_CRASH_PAUSE_PCT", 0)
    out0 = build_market_context(client, "TESTUSDT")
    assert "1h 近3根 -2.00%" in out0
    assert "BTC 短线急跌" not in out0


# ── 降级路径 ──────────────────────────────────────────────────────────


def test_degrade_funding_failure_only(no_funding):
    """费率失败只降级费率行，BTC 行照常"""
    out = build_market_context(_btc_client(10.0, [100.0] * 25), "TESTUSDT")
    assert out is not None
    assert "资金费率" not in out
    assert "BTC 4h 多头" in out


def test_degrade_btc_failure_only(monkeypatch):
    """BTC K 线失败只降级 BTC 行，费率行照常"""
    monkeypatch.setattr(market_data, "get_funding", lambda s: _funding(0.0123))
    out = build_market_context(FakeClient(fail=True), "TESTUSDT")
    assert out is not None
    assert "资金费率 0.0123%/8h" in out
    assert "BTC" not in out


def test_degrade_all_failure_returns_none(no_funding):
    """全失败 → None（调用方不注入）"""
    assert build_market_context(FakeClient(fail=True), "TESTUSDT") is None


# ── get_futures_klines 薄封装（MockTransport，不触网）─────────────────


def test_binance_client_get_futures_klines():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["symbol"] == "BTCUSDT"
        assert request.url.params["interval"] == "4h"
        assert request.url.params["limit"] == "60"
        return httpx.Response(200, json=[[
            1727000000000, "50000", "50100", "49900", "50050",
            "12.5", 1727003599999, "625000",
        ]])

    bc = BinanceClient()
    try:
        bc.futures_client = httpx.Client(
            transport=httpx.MockTransport(handler),
            base_url=settings.BINANCE_FUTURES_URL,
            timeout=5,
        )
        rows = bc.get_futures_klines("BTCUSDT", "4h", 60)
        assert rows[0][0] == 1727000000000
        assert rows[0][4] == 50050.0
        assert rows[0][7] == 625000.0
    finally:
        bc.close()


# ── _build_messages 接线 ──────────────────────────────────────────────


def _minimal_signal(symbol="TESTUSDT"):
    return {
        "symbol": symbol, "signal_type": "trigger_breakout",
        "current_price": 100.0, "breakout_pct": 0.5, "pattern": None,
        "signal_reason": "r", "position": "support", "volume_type": "正常",
        "volume": 1.0, "recent_swings": {},
    }


_KLINES = [[0, 100.0, 101.0, 99.0, 100.5, 10.0, 0, 10000.0]] * 3


def test_build_messages_injects_market_context(monkeypatch):
    """开关开启 → 【市场环境】块紧跟指标块注入（含费率与 BTC 行）"""
    monkeypatch.setattr(settings, "MARKET_CONTEXT_ENABLED", True)
    monkeypatch.setattr(ai_analyzer, "render_indicator_block", lambda _k, _h: "")
    monkeypatch.setattr(ai_analyzer, "analyze_ema", lambda _k: None)
    monkeypatch.setattr(
        market_data, "get_funding", lambda s: _funding(0.0123))
    monkeypatch.setattr(
        ai_analyzer, "get_kline_client",
        lambda: _btc_client(10.0, [100.0] * 25),
    )
    user = _build_messages(_minimal_signal(), _KLINES)[-1]["content"]
    assert "【市场环境】" in user
    assert "资金费率 0.0123%/8h" in user
    assert "BTC 4h 多头" in user
    # 紧跟指标块之后（此处指标块被桩空 → 位于量能行与摆动/K线段之间）
    assert user.index("【市场环境】") > user.index("量能:")


def test_build_messages_disabled_no_injection(monkeypatch):
    """开关关闭（conftest 基线）→ 整块不出现"""
    assert settings.MARKET_CONTEXT_ENABLED is False  # conftest 已关
    user = _build_messages(_minimal_signal(), _KLINES)[-1]["content"]
    assert "【市场环境】" not in user


def test_build_messages_builder_exception_degrades(monkeypatch):
    """构建函数抛异常 → 静默降级不注入，不影响消息构造"""
    monkeypatch.setattr(settings, "MARKET_CONTEXT_ENABLED", True)

    def _boom(_client, _symbol):
        raise RuntimeError("boom")

    monkeypatch.setattr(ai_analyzer, "build_market_context", _boom)
    user = _build_messages(_minimal_signal(), _KLINES)[-1]["content"]
    assert "【市场环境】" not in user
