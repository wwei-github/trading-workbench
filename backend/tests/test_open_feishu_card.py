# -*- coding: utf-8 -*-
"""自动开仓飞书卡片（2026-10-02）

1. _open_feishu_card 卡片结构：header 币种、概览行（方向/杠杆/风险/评分）、
   价格栅格（入场/SL/TP1/TP2，无 TP2 显示 —）、标注行（数量/名义/信号/战法）、
   环境脚注（测试网/正式网 + 账号）
2. notify_open_cards：configured=False 整批跳过；正常逐笔发送（SessionLocal
   替身注入 sqlite）；单条记录缺失不中断其余
"""
import sys
import uuid
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.models.trade import TradeRecord  # noqa: E402
from app.services import feishu_bot  # noqa: E402
from app.services.trade_engine import _open_feishu_card, notify_open_cards  # noqa: E402


def _rec(**kw) -> TradeRecord:
    base = dict(
        symbol="KSMUSDT", direction="long", status="OPENED",
        account_key="testnet-1", testnet=True,
        recommendation=75, signal_type="trigger_breakout", pattern="hammer",
        entry_price=12.3456, qty=8.5, notional=104.94, leverage=10,
        risk_amount=2.55, stop_loss=12.0, tp1=13.2, tp2=14.1,
        ai_snapshot={"tactic_name": "回踩接多", "pattern": "hammer"},
    )
    base.update(kw)
    return TradeRecord(**base)


def test_card_structure():
    """卡片含币种标题 + 两行栅格（方向/杠杆/风险/评分 + 入场/SL/TP）+ 战法标注"""
    card = _open_feishu_card(_rec())
    assert card["header"]["title"]["content"] == "🚀 自动开仓 · KSMUSDT"
    cols = [c for el in card["elements"] if el["tag"] == "column_set"
            for c in el["columns"]]
    text = [c["elements"][0]["text"]["content"] for c in cols]
    # 概览行
    assert "**方向**\n做多 Long" in text
    assert "**杠杆**\n10× 逐仓" in text
    assert "**预估风险**\n2.55 USDT" in text
    assert "**AI 评分**\n75 分" in text
    # 价格栅格（小价格 4 位小数口径）
    assert "**入场价**\n12.3456" in text
    assert "**止损 SL**\n12.0000" in text
    assert "**止盈 TP1**\n13.2000" in text
    assert "**止盈 TP2**\n14.1000" in text
    # 标注行 + 脚注
    md = [el for el in card["elements"] if el["tag"] == "div"][0]
    assert "数量 8.5" in md["text"]["content"]
    assert "战法 回踩接多" in md["text"]["content"]
    note = [el for el in card["elements"] if el["tag"] == "note"][0]
    assert "测试网" in note["elements"][0]["content"]
    assert "testnet-1" in note["elements"][0]["content"]


def test_card_short_and_no_tp2():
    """空单方向文案；TP2=0 显示 — 且标注行无战法段"""
    card = _open_feishu_card(_rec(direction="short", tp2=0, ai_snapshot={}))
    cols = [c for el in card["elements"] if el["tag"] == "column_set"
            for c in el["columns"]]
    text = [c["elements"][0]["text"]["content"] for c in cols]
    assert "**方向**\n做空 Short" in text
    assert "**止盈 TP2**\n—" in text
    md = [el for el in card["elements"] if el["tag"] == "div"][0]
    assert "战法" not in md["text"]["content"]


def test_notify_skipped_when_unconfigured(monkeypatch):
    """自建应用未配置 → 整批跳过不建会话"""
    monkeypatch.setattr(feishu_bot, "configured", lambda: False)
    sent = []

    def _boom_session():  # pragma: nocover
        raise AssertionError("未配置时不应触碰数据库")

    monkeypatch.setattr("app.database.SessionLocal", _boom_session)
    assert notify_open_cards([str(uuid.uuid4())]) == 0
    assert sent == []


def test_notify_sends_and_tolerates_missing(monkeypatch):
    """逐笔发送：存在的记录发卡、不存在的 ID 跳过不中断"""
    engine = create_engine("sqlite:///:memory:")
    from app.database import Base
    import app.models  # noqa: F401
    Base.metadata.create_all(engine)
    rec = _rec()
    with sessionmaker(bind=engine)() as db:
        db.add(rec)
        db.commit()

        monkeypatch.setattr(feishu_bot, "configured", lambda: True)
        cards = []
        monkeypatch.setattr(feishu_bot, "send_app_card",
                            lambda c: cards.append(c) or True)
        monkeypatch.setattr("app.database.SessionLocal",
                            sessionmaker(bind=engine))

        sent = notify_open_cards([str(rec.id), str(uuid.uuid4())])
        assert sent == 1
        assert len(cards) == 1
        assert cards[0]["header"]["title"]["content"] == "🚀 自动开仓 · KSMUSDT"
