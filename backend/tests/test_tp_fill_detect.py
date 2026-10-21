# -*- coding: utf-8 -*-
"""TP 真成交判定 + 裸仓兜底（trade_engine，2026-10-20 SQDUSDT 误标修复）。

事故：测试网 SQDUSDT 空单，TP1 条件单在薄订单簿上卡在 TRIGGERING（触发未成交），
止损 closePosition 市价单只部分成交把仓位从 100% 缩到 60%，恰好过 ratio≤0.75 门槛。
旧口径只看「TP 单号不在挂单列表」→ 误判 TP1 成交，写了一条 pnl 为负的 TP1_FILL、
状态打成 TP1_HIT、并触发保本移损，掩盖了剩余裸仓（随后补挂 SL 触发被拒，裸仓呆等
6h+ 直至逼近强平）。

本测试覆盖：
- _tp_filled_real：TRIGGERING/EXPIRED/REJECTED 都不算成交，FINISHED 才算；
  仍挂在挂单列表（未触发）零额外请求直接判未成交；查询失败回落旧口径不阻塞结算。
- _sl_algo_rejected：SL 被交易所拒单识别。
- _rehang_lost_sl：SL 被拒 + 现价越过 → 升级告警不再呆等；补挂失败 → 告警。
"""
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services import trade_engine as te  # noqa: E402
from app.services.trade_engine import (  # noqa: E402
    _rehang_lost_sl,
    _sl_algo_rejected,
    _tp_filled_real,
)


class FakeTrader:
    """按脚本返回 algo 详情；统计查询次数以验证「未触发零额外请求」"""

    def __init__(self, algo_details=None, price=None, algo_fail=False):
        self._algo = algo_details or {}
        self._price = price
        self.algo_fail = algo_fail
        self.algo_calls = 0

    def algo_order_detail(self, symbol, algo_id):
        self.algo_calls += 1
        if self.algo_fail:
            raise RuntimeError("algo endpoint down")
        return self._algo.get(int(algo_id), {"algoStatus": "NEW"})

    def price(self, symbol):
        return self._price

    def round_price(self, symbol, price):
        return price


def _rec(**kw):
    base = dict(
        symbol="SQDUSDT", direction="short", testnet=True,
        entry_price=0.01818, stop_loss=0.01826,
        raw={"entry_order_id": 224104439, "sl_order_id": 1000000247129410,
             "tp1_order_id": 1000000246475258, "tp2_order_id": 1000000246475259},
    )
    base.update(kw)
    return SimpleNamespace(**base)


# ---------- _tp_filled_real：真成交判定 ----------

def test_tp_still_pending_zero_extra_request():
    """TP 仍在挂单列表（未触发）→ 判未成交，且不查 algo 接口"""
    rt = FakeTrader()
    assert _tp_filled_real(rt, "SQDUSDT", 1000000246475258,
                           {1000000246475258}) is False
    assert rt.algo_calls == 0


def test_tp_triggering_not_filled():
    """复现 SQDUSDT：TP1 卡在 TRIGGERING（不在挂单列表）→ 未成交"""
    rt = FakeTrader(algo_details={1000000246475258: {"algoStatus": "TRIGGERING"}})
    assert _tp_filled_real(rt, "SQDUSDT", 1000000246475258, set()) is False


def test_tp_expired_and_rejected_not_filled():
    rt = FakeTrader(algo_details={
        1000000246475258: {"algoStatus": "EXPIRED"},
        1000000246475259: {"algoStatus": "REJECTED"},
    })
    assert _tp_filled_real(rt, "SQDUSDT", 1000000246475258, set()) is False
    assert _tp_filled_real(rt, "SQDUSDT", 1000000246475259, set()) is False


def test_tp_finished_is_filled():
    rt = FakeTrader(algo_details={1000000246475258: {"algoStatus": "FINISHED"}})
    assert _tp_filled_real(rt, "SQDUSDT", 1000000246475258, set()) is True


def test_tp_query_fail_falls_back_to_legacy():
    """查询失败回落旧口径（不在列表即视为成交），不阻塞正常结算"""
    rt = FakeTrader(algo_fail=True)
    assert _tp_filled_real(rt, "SQDUSDT", 1000000246475258, set()) is True


def test_tp_missing_order_id_not_filled():
    rt = FakeTrader()
    assert _tp_filled_real(rt, "SQDUSDT", None, set()) is False
    assert rt.algo_calls == 0


# ---------- _sl_algo_rejected ----------

def test_sl_rejected_detected():
    rt = FakeTrader(algo_details={1000000247129410: {"algoStatus": "REJECTED"}})
    assert _sl_algo_rejected(rt, "SQDUSDT", 1000000247129410) is True


def test_sl_not_rejected_or_query_fail_is_false():
    rt = FakeTrader(algo_details={1000000247129410: {"algoStatus": "NEW"}})
    assert _sl_algo_rejected(rt, "SQDUSDT", 1000000247129410) is False
    assert _sl_algo_rejected(FakeTrader(algo_fail=True), "SQDUSDT", 1) is False
    assert _sl_algo_rejected(rt, "SQDUSDT", None) is False


# ---------- _rehang_lost_sl：裸仓兜底 ----------

def test_rehang_sl_rejected_overshoot_creates_recovery_incident(monkeypatch):
    """SL 触发被拒 + 现价已越过 → 纳入持久恢复闭环，不只发告警"""
    record = _rec(stop_loss=0.01826)
    rt = FakeTrader(
        algo_details={1000000247129410: {"algoStatus": "REJECTED"}},
        price=0.01860,  # 空单：现价 0.01860 ≥ SL 0.01826 → 越过
    )
    rt.account_key = "testnet-1"
    called = {}

    def _boom(*a, **k):
        called["rehang"] = True

    alerts = []
    incidents = []
    from app.services import protection_recovery
    monkeypatch.setattr(protection_recovery, "create_incident_after_rollback",
                        lambda db, **kw: incidents.append(kw))
    record.id = "trade-1"
    record.qty = 1.25
    record.entry_price = 0.01818
    record.opened_at = None
    monkeypatch.setattr(te, "_replace_sl", _boom)
    monkeypatch.setattr(te, "_alert_naked_position",
                        lambda rec, price, sl, reason: alerts.append(reason))

    _rehang_lost_sl(SimpleNamespace(commit=lambda: None, rollback=lambda: None),
                    rt, record, set())

    assert called == {}  # 现价越过原价位无法补挂，不应尝试
    assert incidents and incidents[0]["reason"] == "sl_rejected_overshot"
    assert incidents[0]["trade_record_id"] == "trade-1"
    assert incidents[0]["rollback_session"] is False
    assert alerts and "已进入保护事故" in alerts[0]


def test_rehang_sl_overshoot_not_rejected_skips(monkeypatch):
    """SL 非被拒、现价越过 → 维持原逻辑：疑似刚触发，跳过本轮，不告警"""
    record = _rec(stop_loss=0.01826)
    rt = FakeTrader(algo_details={1000000247129410: {"algoStatus": "NEW"}},
                    price=0.01860)
    called = {}
    alerts = []
    monkeypatch.setattr(te, "_replace_sl",
                        lambda *a, **k: called.setdefault("rehang", True))
    monkeypatch.setattr(te, "_alert_naked_position",
                        lambda rec, price, sl, reason: alerts.append(reason))

    _rehang_lost_sl(SimpleNamespace(commit=lambda: None, rollback=lambda: None),
                    rt, record, set())

    assert called == {} and alerts == []


def test_rehang_sl_failure_alerts_and_raises(monkeypatch):
    """补挂抛错（如被卡在 TRIGGERING 的 reduceOnly 单阻塞）→ 告警后上抛"""
    record = _rec(stop_loss=0.01826)
    rt = FakeTrader(algo_details={1000000247129410: {"algoStatus": "NEW"}},
                    price=0.01820)  # 空单：现价低于 SL，未越过 → 走补挂分支
    alerts = []

    def _boom(*a, **k):
        raise RuntimeError("-2022 ReduceOnly Order is rejected")

    monkeypatch.setattr(te, "_replace_sl", _boom)
    monkeypatch.setattr(te, "_alert_naked_position",
                        lambda rec, price, sl, reason: alerts.append(reason))

    raised = False
    try:
        _rehang_lost_sl(SimpleNamespace(commit=lambda: None, rollback=lambda: None),
                        rt, record, set())
    except RuntimeError:
        raised = True

    assert raised and alerts and "补挂失败" in alerts[0]
