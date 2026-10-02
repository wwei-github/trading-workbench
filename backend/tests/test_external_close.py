# -*- coding: utf-8 -*-
"""外部平仓识别（trade_engine._detect_external_close，2026-10-02 GTCUSDT 案例）。

用户在币安网页/App 手动平仓 → 巡检发现仓位归零，bookkeeping 推断曾把出场
误标为 tp1_trail/sl。识别原理：SL/TP algo 条件单触发后的市价子单在
allOrders 里不带归属字段，但 /fapi/v1/algoOrder 的 actualOrderId 精确回填
触发子单号——最后一批平仓侧成交的 orderId 不在系统单链内 → 外部平仓。

FakeTrader 替身覆盖 rt.algo_order_detail / rt.order / rt._req 三个接口，
不触网。TradeRecord 用 SimpleNamespace（helper 只读 raw/direction/symbol/
opened_at 四个属性）。
"""
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.trade_engine import _detect_external_close  # noqa: E402

OPENED_AT = datetime(2026, 10, 1, 14, 9, 52)


class FakeTrader:
    """按脚本返回 algo 详情 / 订单详情 / userTrades"""

    def __init__(self, algo_details=None, orders=None, trades=None, algo_fail=False):
        self._algo = algo_details or {}
        self._orders = orders or {}
        self._trades = trades or []
        self.algo_fail = algo_fail

    def algo_order_detail(self, symbol, algo_id):
        if self.algo_fail:
            raise RuntimeError("algo endpoint down")
        return self._algo.get(int(algo_id), {"actualOrderId": ""})

    def order(self, symbol, order_id):
        return self._orders.get(int(order_id), {"type": "LIMIT", "timeInForce": "IOC"})

    def _req(self, method, path, params=None):
        assert (method, path) == ("GET", "/fapi/v1/userTrades")
        return self._trades


def _rec(**kw):
    base = dict(
        symbol="GTCUSDT", direction="long", opened_at=OPENED_AT,
        raw={
            "entry_order_id": 111,
            "sl_order_id": 1001,
            "tp1_order_id": 1002,
            "tp2_order_id": None,
        },
    )
    base.update(kw)
    return SimpleNamespace(**base)


# 成交行构造：t毫秒 / side / price / qty / orderId
def _fill(t, side, price, qty, oid):
    return {"time": t, "side": side, "price": price, "qty": qty, "orderId": oid}


def _ms(h, m, s=0):
    base = int(datetime(2026, 10, 1, 0, 0, tzinfo=timezone.utc).timestamp() * 1000)
    return base + ((h * 60 + m) * 60 + s) * 1000


def test_external_close_detected():
    """网页一键平仓（IOC 限价 258245574，不在系统链）→ 返回 detail，exit=manual"""
    rt = FakeTrader(
        algo_details={
            # SL 未触发：actualOrderId 空；TP1 已触发：子单 222
            1001: {"actualOrderId": ""},
            1002: {"actualOrderId": 222},
        },
        trades=[
            _fill(_ms(19, 57, 31), "SELL", "0.1199", "23601.3", 222),   # TP1 子单
            _fill(_ms(25, 18, 46), "SELL", "0.163119", "7867.2", 333),  # 外部平仓
        ],
        orders={333: {"type": "LIMIT", "timeInForce": "IOC"}},
    )
    d = _detect_external_close(rt, _rec())
    assert d is not None
    assert d["order_id"] == 333
    assert d["order_type"] == "LIMIT" and d["time_in_force"] == "IOC"
    assert abs(d["exit_price"] - 0.163119) < 1e-9
    assert d["qty"] == 7867.2
    assert d["filled_at"] == "2026-10-02 01:18:46"  # 25 时跨日（UTC 10-02 01:18）


def test_system_sl_not_flagged():
    """SL 触发子单（actualOrderId 回填）是最后一批平仓 → 不标 manual"""
    rt = FakeTrader(
        algo_details={
            1001: {"actualOrderId": 444},  # SL 触发子单
            1002: {"actualOrderId": 222},
        },
        trades=[
            _fill(_ms(19, 57, 31), "SELL", "0.1199", "23601.3", 222),
            _fill(_ms(23, 0, 5), "SELL", "0.125", "7867.2", 444),
        ],
    )
    assert _detect_external_close(rt, _rec()) is None


def test_algo_query_fail_abstains():
    """algo 详情查询失败 → 链不完整，弃权维持推断（不误标）"""
    rt = FakeTrader(algo_fail=True, trades=[
        _fill(_ms(23, 0, 5), "SELL", "0.125", "7867.2", 999),
    ])
    assert _detect_external_close(rt, _rec()) is None


def test_no_close_fills():
    """无平仓侧成交（异常数据）→ 弃权"""
    rt = FakeTrader(trades=[_fill(_ms(14, 9, 52), "BUY", "0.0984", "31468.5", 111)])
    assert _detect_external_close(rt, _rec()) is None


def test_short_direction_uses_buy_side():
    """空头平仓侧是 BUY：外部买入平仓单被识别"""
    rec = _rec(direction="short", raw={
        "entry_order_id": 111, "sl_order_id": 1001, "tp1_order_id": None,
    })
    rt = FakeTrader(
        algo_details={1001: {"actualOrderId": ""}},
        trades=[
            _fill(_ms(20, 0, 0), "SELL", "0.5", "10.0", 111),   # 开仓 SELL（非平仓侧）
            _fill(_ms(21, 0, 0), "BUY", "0.48", "10.0", 555),   # 外部买入平仓
        ],
        orders={555: {"type": "MARKET", "timeInForce": "GTC"}},
    )
    d = _detect_external_close(rt, rec)
    assert d is not None
    assert d["order_id"] == 555 and d["qty"] == 10.0


def test_last_fill_system_earlier_external_partial():
    """外部先平一部分、系统 SL 收尾 → 最后一批是系统单，不标 manual"""
    rt = FakeTrader(
        algo_details={1001: {"actualOrderId": 444}, 1002: {"actualOrderId": 222}},
        trades=[
            _fill(_ms(20, 0, 0), "SELL", "0.14", "3000", 333),  # 外部部分平仓
            _fill(_ms(23, 0, 5), "SELL", "0.125", "4867.2", 444),  # SL 收尾
        ],
    )
    assert _detect_external_close(rt, _rec()) is None
