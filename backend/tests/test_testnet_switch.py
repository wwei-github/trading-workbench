# -*- coding: utf-8 -*-
"""合约测试网总开关单测（2026-10-27）

system_config.testnet_enabled（默认 True）是合约测试网的上层闸门：
- False：只停「新增」——自动扇出、引擎开单、外部仓自动接管、测试网通知、
  自动持仓诊断/镜像、结算拉黑；存量结算/保护/挂单巡检照常
- True：与历史行为完全一致
- 缺行/读库异常按 True 兜底（升级即无感）
- 现货测试网独立（spot_enabled_accounts 不受影响）
"""
import sys
from pathlib import Path
from types import SimpleNamespace

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import app.services.account_registry as ar  # noqa: E402
import app.services.feishu_bot as fb  # noqa: E402
import app.services.trade_engine as te  # noqa: E402
from app.models.system_config import SystemConfig  # noqa: E402


class _CfgDB:
    """只读 system_config 的极简 db（其余模型查询一律返回 None）"""

    def __init__(self, cfg):
        self.cfg = cfg
        self.commits = 0

    def get(self, model, pk):
        return self.cfg if model is SystemConfig else None

    def commit(self):
        self.commits += 1

    def rollback(self):
        pass


def _tn():
    return ar.Account("testnet-1", "测试网", True, "k", "s")


def _live():
    return ar.Account("live-1", "正式网-1", False, "k2", "s2")


# ── helper：默认值 / 读取 / 异常兜底 ────────────────────────────────────────

def test_helper_defaults_true_without_row():
    """缺行（全新库未落默认行）按「开」兜底，升级后行为不变"""
    engine = create_engine("sqlite:///:memory:")
    SystemConfig.__table__.create(engine)
    db = sessionmaker(bind=engine)()
    assert ar.testnet_enabled(db) is True


def test_helper_reads_flag():
    assert ar.testnet_enabled(_CfgDB(SystemConfig(id=1, testnet_enabled=False))) is False
    assert ar.testnet_enabled(_CfgDB(SystemConfig(id=1, testnet_enabled=True))) is True


def test_helper_db_error_defaults_true():
    """读库异常按「开」兜底：不因配置读取失败静默停掉测试网新增"""
    class _Boom:
        def get(self, model, pk):
            raise RuntimeError("db down")

    assert ar.testnet_enabled(_Boom()) is True


# ── 自动扇出：总闸是上层闸门 ────────────────────────────────────────────────

def test_auto_trade_accounts_master_off_skips_testnet(monkeypatch):
    """总闸关：即使测试网分开关仍为开，也不扇出测试网账号；
    正式网分开关独立生效，不受影响"""
    monkeypatch.setattr(ar, "_registry", [_tn(), _live()])
    cfg = SystemConfig(
        id=1, trading_enabled_testnet=True, trading_enabled_live=True,
        testnet_enabled=False, live_trading_keys="live-1",
    )
    assert [a.key for a in ar.auto_trade_accounts(_CfgDB(cfg))] == ["live-1"]


def test_auto_trade_accounts_master_on_keeps_testnet(monkeypatch):
    """总闸开：与历史行为一致（双网分开关各自扇出）"""
    monkeypatch.setattr(ar, "_registry", [_tn(), _live()])
    cfg = SystemConfig(
        id=1, trading_enabled_testnet=True, trading_enabled_live=True,
        testnet_enabled=True, live_trading_keys="live-1",
    )
    assert [a.key for a in ar.auto_trade_accounts(_CfgDB(cfg))] == ["testnet-1", "live-1"]


def test_managed_accounts_keep_testnet_when_master_off(monkeypatch):
    """管理清单（结算/保护/事件流）不随总闸变化：存量持仓仍需照常收尾"""
    monkeypatch.setattr(ar, "_registry", [_tn(), _live()])
    cfg = SystemConfig(id=1, testnet_enabled=False, live_trading_keys="live-1")
    assert [a.key for a in ar.managed_accounts(_CfgDB(cfg))] == ["testnet-1", "live-1"]


# ── 现货独立 ───────────────────────────────────────────────────────────────

def test_spot_accounts_unaffected_by_master_switch(monkeypatch):
    """现货测试网（spot_trading_testnet）独立，不受合约总闸影响"""
    monkeypatch.setattr(ar, "_registry", [_tn(), _live()])
    monkeypatch.setattr(ar.settings, "BINANCE_SPOT_TESTNET_API_KEY", "")
    monkeypatch.setattr(ar.settings, "BINANCE_SPOT_TESTNET_SECRET_KEY", "")
    cfg = SystemConfig(id=1, testnet_enabled=False, spot_trading_testnet=True)
    assert [a.key for a in ar.spot_enabled_accounts(_CfgDB(cfg))] == ["testnet-1"]


# ── 引擎开单闸门 ───────────────────────────────────────────────────────────

def test_engine_open_gate_blocks_testnet(monkeypatch):
    """引擎层总闸：测试网账号直接拒开并落 open_block_reason"""
    cfg = SystemConfig(id=1, testnet_enabled=False)
    a = SimpleNamespace(open_block_reason=None)
    db = _CfgDB(cfg)
    db.get = lambda model, pk: cfg if model is SystemConfig else a
    assert te.try_open_for_analysis(db, SimpleNamespace(testnet=True), "00000000-0000-0000-0000-000000000001") == "blocked"
    assert "测试网已关闭" in a.open_block_reason


def test_engine_open_gate_allows_live(monkeypatch):
    """总闸只拦测试网账号：正式网继续走既有资格判定"""
    cfg = SystemConfig(id=1, testnet_enabled=False)
    a = SimpleNamespace(open_block_reason=None)
    db = _CfgDB(cfg)
    db.get = lambda model, pk: cfg if model is SystemConfig else a
    monkeypatch.setattr(te, "analysis_open_eligible", lambda *_: False)
    assert te.try_open_for_analysis(db, SimpleNamespace(testnet=False), "00000000-0000-0000-0000-000000000001") == "blocked"
    assert a.open_block_reason is None  # 未命中总闸，而是资格过滤


def test_auto_adopt_skips_testnet_when_master_off(monkeypatch):
    """外部手动仓自动接管属「新增」：总闸关时测试网不再拉取可接管清单"""
    called = []
    monkeypatch.setattr(
        te, "list_adoptable_positions",
        lambda db, trader: called.append(1) or [],
    )
    cfg = SystemConfig(id=1, testnet_enabled=False)
    assert te.auto_adopt_positions(_CfgDB(cfg), SimpleNamespace(testnet=True)) == []
    assert called == []
    # 正式网不受影响（继续拉取，只是清单为空）
    assert te.auto_adopt_positions(_CfgDB(cfg), SimpleNamespace(testnet=False)) == []
    assert called == [1]


# ── 飞书通知 ───────────────────────────────────────────────────────────────

def test_feishu_notify_forced_off_by_master_switch(monkeypatch):
    """总闸关：即使测试网通知分开关为开，发送层也拦截 testnet=True 推送"""
    engine = create_engine("sqlite:///:memory:")
    SystemConfig.__table__.create(engine)
    maker = sessionmaker(bind=engine)
    db = maker()
    db.add(SystemConfig(
        id=1, feishu_testnet_notify_enabled=True, testnet_enabled=False,
    ))
    db.commit()
    db.close()
    monkeypatch.setattr("app.database.SessionLocal", maker)

    posts = []
    monkeypatch.setattr(
        fb, "_post_message",
        lambda msg_type, content: (posts.append((msg_type, content)), True)[1],
    )
    card = {"header": {"title": {"tag": "plain_text", "content": "开仓成功"}}}
    assert fb.testnet_notify_enabled() is False
    assert fb.send_app_card(card, testnet=True) is False
    assert fb.send_app_text("测试网文本", testnet=True) is False
    assert posts == []
    # 非测试网推送不受影响
    assert fb.send_app_card(card) is True
    assert len(posts) == 1
