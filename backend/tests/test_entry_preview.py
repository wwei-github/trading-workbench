"""手动开单入场位置预览（docs/15 §20，2026-10-27）。

B1 只拦自动开仓；手动开单 force_market=True 会跳过它，而双网自动开单关闭时
实盘每一笔都是手动单 → B1 实际不拦任何单。本端点把 pos 提前给前端弹窗，
让"在信号 K 下半部进场"变成知情选择。这里只验算数与降级，不验 UI。
"""
from app.api import trades
from app.models.system_config import SystemConfig


class _Pool:
    def __init__(self, klines):
        self._klines = klines
        self.calls = []

    def get_klines(self, symbol, interval, limit, allow_stale=True):
        self.calls.append((symbol, interval, limit))
        return self._klines


def _bar(ms, o, h, l, c):
    return [ms, o, h, l, c, 1.0]


def _klines():
    # 已收盘根：low=0.0797 high=0.0811；未收盘根 close=0.0804
    closed = _bar(1793112000000, 0.0800, 0.0811, 0.0797, 0.0805)
    forming = _bar(1793115600000, 0.0805, 0.0810, 0.0802, 0.0804)
    return [closed, forming]


class _Db:
    """只提供 cfg 读取（端点除此之外不碰库）。"""

    def __init__(self, cfg=None):
        self.cfg = cfg or SystemConfig(id=1, kline_interval="1h")
        self.calls = 0

    def get(self, model, pk):
        self.calls += 1
        return self.cfg


def test_pos_computed_on_closed_bar(monkeypatch):
    db = _Db()
    pool = _Pool(_klines())
    monkeypatch.setattr(trades, "ExchangePool", lambda: pool)
    monkeypatch.setattr(trades.settings if hasattr(trades, "settings") else trades, "__name__", "x", raising=False)
    out = trades.entry_preview("elsausdt", "long", db)
    assert out["symbol"] == "ELSAUSDT"
    assert out["price"] == 0.0804
    # (0.0804-0.0797)/(0.0811-0.0797) = 0.5
    assert abs(out["pos"] - 0.5) < 1e-9
    assert out["bar_low"] == 0.0797 and out["bar_high"] == 0.0811


def test_blocked_when_below_min(monkeypatch):
    from app.config import settings

    db = _Db()
    monkeypatch.setattr(trades, "ExchangePool", lambda: _Pool(_klines()))
    monkeypatch.setattr(settings, "ENTRY_POS_MIN_RATIO", 0.6, raising=False)
    out = trades.entry_preview("ELSAUSDT", "long", db)
    assert out["pos_min"] == 0.6
    assert out["blocked"] is True
    assert "0.50" in out["note"] and "−14.30R" in out["note"]


def test_not_blocked_when_above_min(monkeypatch):
    from app.config import settings

    db = _Db()
    monkeypatch.setattr(trades, "ExchangePool", lambda: _Pool(_klines()))
    monkeypatch.setattr(settings, "ENTRY_POS_MIN_RATIO", 0.4, raising=False)
    out = trades.entry_preview("ELSAUSDT", "long", db)
    assert out["blocked"] is False
    assert out["note"] is None


def test_gate_off_no_verdict(monkeypatch):
    from app.config import settings

    db = _Db()
    monkeypatch.setattr(trades, "ExchangePool", lambda: _Pool(_klines()))
    monkeypatch.setattr(settings, "ENTRY_POS_MIN_RATIO", 0.0, raising=False)
    out = trades.entry_preview("ELSAUSDT", "long", db)
    assert out["blocked"] is None
    assert out["pos"] is not None


def test_empty_klines_degrades(monkeypatch):
    db = _Db()
    monkeypatch.setattr(trades, "ExchangePool", lambda: _Pool([]))
    out = trades.entry_preview("ELSAUSDT", "long", db)
    assert out["pos"] is None and out["blocked"] is None


def test_pool_failure_degrades(monkeypatch):
    from app.services.exchange_pool import AllExchangesFailed

    db = _Db()

    class _Bad:
        def get_klines(self, *a, **kw):
            raise AllExchangesFailed("x")

    monkeypatch.setattr(trades, "ExchangePool", lambda: _Bad())
    out = trades.entry_preview("ELSAUSDT", "long", db)
    assert out["pos"] is None and out["price"] is None
