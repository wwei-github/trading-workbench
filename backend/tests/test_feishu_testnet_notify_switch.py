# -*- coding: utf-8 -*-
"""测试网飞书通知开关单测（2026-10-15）

system_config.feishu_testnet_notify_enabled 关 → 发送层拦截标注 testnet=True
的卡片/文本（静默丢弃、不发）；缺行/读库异常按「开」兜底（不丢通知）；
未标注 testnet 的推送完全不受影响。
"""
import sys
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import app.services.feishu_bot as fb  # noqa: E402
from app.models.system_config import SystemConfig  # noqa: E402


def _install_cfg_db(monkeypatch, enabled=None):
    """内存库建 system_config 单行表并注入 SessionLocal（enabled=None 表示无行）"""
    engine = create_engine("sqlite:///:memory:")
    SystemConfig.__table__.create(engine)
    maker = sessionmaker(bind=engine)
    if enabled is not None:
        db = maker()
        db.add(SystemConfig(id=1, feishu_testnet_notify_enabled=enabled))
        db.commit()
        db.close()
    monkeypatch.setattr("app.database.SessionLocal", maker)
    return engine


@pytest.fixture()
def posts(monkeypatch):
    """拦截 _post_message，记录实际发出的 (msg_type, content)"""
    out = []
    monkeypatch.setattr(
        fb, "_post_message",
        lambda msg_type, content: (out.append((msg_type, content)), True)[1],
    )
    return out


CARD = {"header": {"title": {"tag": "plain_text", "content": "开仓成功"}}}


def test_flag_off_suppresses_testnet_card(monkeypatch, posts):
    """开关关：testnet=True 的卡片被静默丢弃（返回 False 且未发）"""
    _install_cfg_db(monkeypatch, enabled=False)
    assert fb.testnet_notify_enabled() is False
    assert fb.send_app_card(CARD, testnet=True) is False
    assert posts == []


def test_flag_on_sends_testnet_card(monkeypatch, posts):
    """开关开：testnet=True 的卡片照常发送"""
    _install_cfg_db(monkeypatch, enabled=True)
    assert fb.testnet_notify_enabled() is True
    assert fb.send_app_card(CARD, testnet=True) is True
    assert len(posts) == 1 and posts[0][0] == "interactive"


def test_flag_off_suppresses_testnet_text(monkeypatch, posts):
    """开关关：testnet=True 的纯文本同样被拦截"""
    _install_cfg_db(monkeypatch, enabled=False)
    assert fb.send_app_text("未上架提示", testnet=True) is False
    assert posts == []


def test_non_testnet_card_never_consults_switch(monkeypatch, posts):
    """未标注 testnet 的推送（扫描命中卡/日报/回落等）不受开关影响"""
    _install_cfg_db(monkeypatch, enabled=False)
    assert fb.send_app_card(CARD) is True
    assert fb.send_app_text("日报") is True
    assert [t for t, _ in posts] == ["interactive", "text"]


def test_missing_row_defaults_open(monkeypatch, posts):
    """缺行（全新库未落默认行）按「开」兜底：通知不丢"""
    _install_cfg_db(monkeypatch, enabled=None)
    assert fb.testnet_notify_enabled() is True
    assert fb.send_app_card(CARD, testnet=True) is True


def test_db_error_defaults_open(monkeypatch, posts):
    """读库异常按「开」兜底：不因配置读取失败静默丢通知"""
    def _boom():
        raise RuntimeError("db down")

    monkeypatch.setattr("app.database.SessionLocal", _boom)
    assert fb.testnet_notify_enabled() is True
    assert fb.send_app_card(CARD, testnet=True) is True


def test_hold_warn_card_carries_testnet_flag():
    """持仓预警卡内部字段 _testnet 供发送层过滤（2026-10-15）"""
    from types import SimpleNamespace
    from app.services.trade_engine import _hold_warn_card

    rec = SimpleNamespace(symbol="IOUSDT", direction="long", testnet=True,
                          unrealized_pnl=-5.0, entry_price=100.0, stop_loss=99.0)
    card = _hold_warn_card(rec, 13.0, 1)
    assert card["_testnet"] is True and card["_symbol"] == "IOUSDT"
