# -*- coding: utf-8 -*-
"""飞书「可开单未下单」卡片的入场位置提示（docs/16 §18.5 D4，2026-10-28）。

背景：双网自动交易全关时每笔都是手动单，而手动单 `force_market=True` 按设计
跳过 `entry_pos` 闸。ELSAUSDT 就是 pos=0.31 一路放行、用户在卡片上点「开单」
时并不知道"这一单自动开仓会被拦"。卡片补一行位置提示，把话说在前面。
"""
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import settings  # noqa: E402
from app.services import trade_engine  # noqa: E402


def _analysis(**kw):
    base = dict(
        id="a1", symbol="ELSAUSDT", direction="long", recommendation=60.0,
        risk_reward_ratio=1.8, entry_price=0.0804, stop_loss=0.0781,
        take_profit_1=0.0838, take_profit_2=None, tactic_name="趋势跟随",
    )
    base.update(kw)
    return SimpleNamespace(**base)


def _texts(card: dict) -> str:
    return str(card)


def test_card_shows_pos_when_below_threshold():
    card = trade_engine._live_off_card(_analysis(), pos=0.31, pos_min=0.6)
    body = _texts(card)
    assert "pos 0.31" in body
    assert "自动开仓会被拦下" in body
    assert "红色" not in body  # 只是排版标记，不是内容
    assert "<font color='red'>" in body


def test_card_shows_pos_when_above_threshold():
    card = trade_engine._live_off_card(_analysis(), pos=0.72, pos_min=0.6)
    body = _texts(card)
    assert "pos 0.72" in body
    assert "≥ 门槛 0.60" in body
    assert "<font color='red'>" not in body


def test_card_omits_pos_when_unknown():
    card = trade_engine._live_off_card(_analysis())
    assert "入场位置" not in _texts(card)


def test_card_pos_line_without_gate():
    """闸门关闭（pos_min=0）时只报数值，不给结论。"""
    card = trade_engine._live_off_card(_analysis(), pos=0.31, pos_min=0.0)
    body = _texts(card)
    assert "pos 0.31" in body
    assert "会被拦下" not in body


def test_live_off_pos_returns_none_on_failure(monkeypatch):
    class _Pool:
        def get_klines(self, *a, **k):
            raise RuntimeError("boom")

    monkeypatch.setattr("app.services.exchange_pool.ExchangePool", lambda: _Pool())
    pos, pos_min = trade_engine._live_off_pos(_analysis(), SimpleNamespace(kline_interval="1h"))
    assert pos is None
    assert pos_min == pytest.approx(float(getattr(settings, "ENTRY_POS_MIN_RATIO", 0.0) or 0.0))


def test_live_off_pos_computes_from_closed_bars(monkeypatch):
    # 已收盘根：高 100 低 80 → 价格 88 → pos = (88-80)/20 = 0.4
    kl = [[i * 3_600_000, 90.0, 100.0, 80.0, 90.0, 1.0] for i in range(5)]
    kl.append([99 * 3_600_000, 88.0, 88.0, 88.0, 88.0, 1.0])  # 未收盘根，应被排除

    class _Pool:
        def get_klines(self, *a, **k):
            return kl

    monkeypatch.setattr("app.services.exchange_pool.ExchangePool", lambda: _Pool())
    monkeypatch.setattr(settings, "ENTRY_POS_MIN_RATIO", 0.6, raising=False)
    pos, pos_min = trade_engine._live_off_pos(
        _analysis(entry_price=88.0), SimpleNamespace(kline_interval="1h"))
    assert pos == pytest.approx(0.4)
    assert pos_min == pytest.approx(0.6)
