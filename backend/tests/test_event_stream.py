# -*- coding: utf-8 -*-
"""P-09 事件流连接层——纯函数单测（docs/16，2026-10-07）。

只测无副作用部分：REST→WS 域映射、ORDER_TRADE_UPDATE 成交过滤、
markPriceUpdate 解析（含 combined 包装）。连接/重连线程不测（feishu_bot
同款 daemon 模式，行为由重连退避与巡检兜底保证）。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.event_stream import (  # noqa: E402
    _extract_fill_symbol,
    _extract_mark,
    ws_base,
)


def test_ws_base_mapping():
    """fapi→fstream；testnet→stream；未知域弃权"""
    assert ws_base("https://fapi.binance.com") == "wss://fstream.binance.com"
    assert ws_base("https://testnet.binancefuture.com") == "wss://stream.binancefuture.com"
    assert ws_base("https://api.binance.com") is None
    assert ws_base("") is None


def test_extract_fill_symbol():
    """FILLED/PARTIALLY_FILLED 放行；其余状态/事件/坏结构一律弃权"""
    fill = {"e": "ORDER_TRADE_UPDATE", "o": {"s": "BTCUSDT", "X": "FILLED"}}
    part = {"e": "ORDER_TRADE_UPDATE", "o": {"s": "ETHUSDT", "X": "PARTIALLY_FILLED"}}
    assert _extract_fill_symbol(fill) == "BTCUSDT"
    assert _extract_fill_symbol(part) == "ETHUSDT"
    assert _extract_fill_symbol({"e": "ORDER_TRADE_UPDATE",
                                 "o": {"s": "BTCUSDT", "X": "NEW"}}) is None
    assert _extract_fill_symbol({"e": "ACCOUNT_UPDATE"}) is None
    assert _extract_fill_symbol({}) is None
    assert _extract_fill_symbol({"e": "ORDER_TRADE_UPDATE", "o": None}) is None


def test_extract_mark():
    """裸 /ws 事件直取；combined 包装（data 层）兼容；坏价弃权"""
    bare = {"e": "markPriceUpdate", "s": "BTCUSDT", "p": "65000.5"}
    assert _extract_mark(bare) == ("BTCUSDT", 65000.5)
    wrapped = {"stream": "btcusdt@markPrice@1s", "data": bare}
    assert _extract_mark(wrapped) == ("BTCUSDT", 65000.5)
    assert _extract_mark({"e": "kline"}) is None
    assert _extract_mark({}) is None
    assert _extract_mark({"e": "markPriceUpdate", "s": "BTCUSDT", "p": ""}) == ("BTCUSDT", 0.0)
