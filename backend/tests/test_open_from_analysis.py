# -*- coding: utf-8 -*-
"""手动开单端点回归（2026-09-27 扫描结果表「开单」按钮）。

open_from_analysis 的闸门语义验证：不开真实 HTTP/DB/Redis，通过 monkeypatch
stub 掉 BinanceTrader / 开仓锁 / try_open_for_analysis，直接调用端点函数
（FastAPI 端点即普通函数，db 传 SimpleNamespace 会话桩）。

覆盖：非 suggest 400 / 分析不存在 404 / 未知账号 400 / 锁超时 409 /
闸门拦截 opened=false / 成功 opened=true + trade_id 反查。
2026-09-28 语义拆分：交易开关不再约束手动开单，且端点必须以 force_market=True
调用（市价即时入场）。
2026-10-03 删环境切换：开单账号由请求 account_key 显式指定（前端弹窗选账号），
后端不猜网——未知账号 400；双开关全关也不拦手动开单。
"""
import uuid
from datetime import datetime
from types import SimpleNamespace

import pytest

import app.api.trades as trades_api
from app.api.trades import OpenFromAnalysisRequest, open_from_analysis
from app.services.account_registry import Account

ANALYSIS_ID = uuid.uuid4()


class _HTTPException(Exception):
    """端点内 raise HTTPException 的替身捕获：记录 status_code/detail"""

    def __init__(self, status_code, detail=None):
        self.status_code = status_code
        self.detail = detail
        super().__init__(detail)


@pytest.fixture()
def _patch_http(monkeypatch):
    """把端点模块里的 HTTPException 换成带 status_code 的替身，便于断言"""
    monkeypatch.setattr(trades_api, "HTTPException", _HTTPException)


@pytest.fixture()
def _db(monkeypatch):
    """会话桩：get 返回预置对象，refresh/execute 空实现。

    2026-10-15 执行核心抽到 trade_tasks.manual_open_from_analysis（core 自建
    SessionLocal 会话），故按 trade_tasks 模块属性注入桩会话——模块级
    `from app.database import SessionLocal` 已绑定名字，patch 绑定处才生效。
    """
    state = {"analysis": None, "cfg": None, "trade_row": None}

    def get(model, id_):
        from app.models.scan import AIAnalysis
        from app.models.system_config import SystemConfig
        from app.models.trade import TradeRecord
        if model is SystemConfig:
            return state["cfg"]
        if model is AIAnalysis and id_ == ANALYSIS_ID:
            return state["analysis"]
        if model is TradeRecord:
            return state["trade_row"]
        return None

    session = SimpleNamespace(
        get=get, refresh=lambda o: None, commit=lambda: None,
        rollback=lambda: None, close=lambda: None,
        execute=lambda q: SimpleNamespace(
            scalars=lambda: SimpleNamespace(first=lambda: state["trade_row"])),
    )
    session._state = state
    from app.tasks import trade_tasks
    monkeypatch.setattr(trade_tasks, "SessionLocal", lambda: session)
    return session


@pytest.fixture()
def _cfg_default(_db):
    """双网开关默认状态（测试网开/正式网关）"""
    from app.models.system_config import SystemConfig
    _db._state["cfg"] = SystemConfig(id=1, trading_enabled_testnet=True,
                                     trading_enabled_live=False)
    return _db


@pytest.fixture()
def _analysis(_cfg_default):
    from app.models.scan import AIAnalysis
    a = AIAnalysis(
        id=ANALYSIS_ID, symbol="BTCUSDT", trade_decision="suggest",
        direction="long", entry_price=100.0, stop_loss=98.0,
        take_profit_1=104.0, recommendation=70,
        created_at=datetime.utcnow(), open_block_reason=None,
    )
    _cfg_default._state["analysis"] = a
    return a


@pytest.fixture()
def _patch_common(monkeypatch):
    """stub：账号注册表 / trader / redis 锁 / 开仓函数结果。

    端点与核心内既有函数级延迟导入（from X import Y），也有模块级绑定
    （trade_tasks.Redis / trade_tasks._acquire_open_lock），两处都必须 patch
    绑定处才能命中替身。
    """
    from app.services import account_registry as reg
    from app.services import trade_engine
    from app.tasks import trade_tasks
    import app.services.binance_trader as _bt_mod

    class _FakeRedis:
        """Redis 替身：from_url 返回无操作客户端（core 只做锁调用）"""
        @classmethod
        def from_url(cls, *a, **kw):
            return SimpleNamespace()

    class _FakeTrader:
        """trader 替身：端点仅读 configured/account_key/testnet 三个属性"""
        configured = True
        account_key = "testnet-1"
        testnet = True

    def _fake_get_account(key):
        if key == "testnet-1":
            return Account(key="testnet-1", name="t", testnet=True,
                           api_key="k", api_secret="s")
        return None

    monkeypatch.setattr(reg, "get_account", _fake_get_account)
    monkeypatch.setattr(_bt_mod, "BinanceTrader", lambda key=None: _FakeTrader())
    monkeypatch.setattr(trade_tasks, "Redis", _FakeRedis)
    monkeypatch.setattr(trade_tasks, "_acquire_open_lock", lambda *a, **kw: True)
    monkeypatch.setattr(trade_tasks, "_release_open_lock", lambda *a, **kw: None)
    holder = {"opened": True, "trade_row": None, "force_market": None,
              "trader_key": None}

    def _fake_try(db, trader, analysis_id, force_market=False):
        holder["force_market"] = force_market
        holder["trader_key"] = trader.account_key
        if holder["opened"] and holder["trade_row"] is not None:
            db._state["trade_row"] = holder["trade_row"]
        return holder["opened"]

    monkeypatch.setattr(trade_engine, "try_open_for_analysis", _fake_try)
    return holder


def _trade_row():
    from app.models.trade import TradeRecord
    return TradeRecord(id=uuid.uuid4(), symbol="BTCUSDT", status="OPENED",
                       account_key="testnet-1", opened_at=datetime.utcnow())


def _payload():
    return OpenFromAnalysisRequest(analysis_id=str(ANALYSIS_ID),
                                   account_key="testnet-1")


def test_happy_path_opened(_patch_http, _patch_common, _analysis, _cfg_default):
    _patch_common["trade_row"] = _trade_row()
    res = open_from_analysis(_payload())
    assert res["opened"] is True and res["ok"] is True
    assert res["trade_id"] == str(_cfg_default._state["trade_row"].id)
    assert res["account_key"] == "testnet-1"
    # 手动开单 = 市价即时入场（跳过偏离/待回踩时机闸门）
    assert _patch_common["force_market"] is True
    # 账号按请求 account_key 解析（2026-10-03 删环境切换：后端不猜网）
    assert _patch_common["trader_key"] == "testnet-1"


def test_gate_blocked_returns_200_with_reason(_patch_http, _patch_common,
                                              _analysis, _cfg_default):
    """闸门拦截（opened=false）是业务结果 200，block_reason 取自分析行"""
    _patch_common["opened"] = False
    _analysis.open_block_reason = "在跑单子已达上限 10"
    res = open_from_analysis(_payload())
    assert res["opened"] is False and res["block_reason"] == "在跑单子已达上限 10"
    assert res["trade_id"] is None


def test_switch_off_still_allowed(_patch_http, _patch_common, _analysis, _db):
    """2026-09-28 语义拆分：双网自动成交开关全关不再拦手动开单（原 400 用例反转）"""
    from app.models.system_config import SystemConfig
    _db._state["cfg"] = SystemConfig(id=1, trading_enabled_testnet=False,
                                     trading_enabled_live=False)
    _patch_common["trade_row"] = _trade_row()
    res = open_from_analysis(_payload())
    assert res["opened"] is True and res["ok"] is True


def test_unknown_account_400(_patch_http, _patch_common, _analysis, _cfg_default):
    """2026-10-03 删环境切换：account_key 不在注册表 → 400（后端不猜网）"""
    with pytest.raises(_HTTPException) as ei:
        open_from_analysis(
            OpenFromAnalysisRequest(analysis_id=str(ANALYSIS_ID),
                                    account_key="live-9"),
        )
    assert ei.value.status_code == 400


def test_non_suggest_400(_patch_http, _patch_common, _analysis, _cfg_default):
    _analysis.trade_decision = "skip"
    with pytest.raises(_HTTPException) as ei:
        open_from_analysis(_payload())
    assert ei.value.status_code == 400


def test_analysis_missing_404(_patch_http, _patch_common, _cfg_default):
    with pytest.raises(_HTTPException) as ei:
        open_from_analysis(
            OpenFromAnalysisRequest(analysis_id=str(uuid.uuid4()),
                                    account_key="testnet-1"),
        )
    assert ei.value.status_code == 404


def test_lock_timeout_409(_patch_http, _patch_common, _analysis, _cfg_default,
                          monkeypatch):
    from app.tasks import trade_tasks
    monkeypatch.setattr(trade_tasks, "_acquire_open_lock",
                        lambda *a, **kw: False)
    with pytest.raises(_HTTPException) as ei:
        open_from_analysis(_payload())
    assert ei.value.status_code == 409


def test_dedup_returns_existing_trade_id(_patch_http, _patch_common, _analysis,
                                         _cfg_default):
    """2026-10-15：opened=False 但反查到记录 ⇒ 命中去重闸门，trade_id 回填已有单
    ——调用方（飞书卡片）据此把「已有单、未重复开」与「真闸门拦截」区分呈现"""
    _patch_common["opened"] = False
    row = _trade_row()
    _cfg_default._state["trade_row"] = row
    res = open_from_analysis(_payload())
    assert res["ok"] is True and res["opened"] is False
    assert res["trade_id"] == str(row.id)


def test_prelock_exception_returns_structured_error(_patch_http, _patch_common,
                                                    _analysis, _cfg_default,
                                                    monkeypatch):
    """2026-10-15 兜底：锁外异常（账号注册表 / Redis 构建 / db.get）不再裸抛——
    统一落 code=error（网页映射 500、飞书渲染红卡），避免回调守护线程静默死掉
    不发结果卡片（原缺口，与 manual_close_market 同暴露面）"""
    from app.services import account_registry as reg

    def _boom(key):
        raise RuntimeError("注册表不可用")

    monkeypatch.setattr(reg, "get_account", _boom)
    with pytest.raises(_HTTPException) as ei:
        open_from_analysis(_payload())
    assert ei.value.status_code == 500
