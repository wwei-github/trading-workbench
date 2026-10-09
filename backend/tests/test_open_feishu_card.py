# -*- coding: utf-8 -*-
"""自动开仓飞书卡片（2026-10-02）+ 正式网开关关闭提醒（2026-10-09）

1. _open_feishu_card 卡片结构：header 币种、概览行（方向/杠杆/风险/评分）、
   价格栅格（入场/SL/TP1/TP2，无 TP2 显示 —）、标注行（数量/名义/信号/战法）、
   环境脚注（测试网/正式网 + 账号）
2. notify_open_cards：configured=False 整批跳过；正常逐笔发送（SessionLocal
   替身注入 sqlite）；单条记录缺失不中断其余
3. 正式网自动开单关闭提醒：analysis_open_eligible 资格谓词；达标信号发橙色
   提醒卡；skip 信号/实盘已有单/未配置飞书均不发
"""
import sys
import uuid
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.models.scan import AIAnalysis  # noqa: E402
from app.models.system_config import SystemConfig  # noqa: E402
from app.models.trade import TradeRecord  # noqa: E402
from app.services import feishu_bot  # noqa: E402
from app.services.trade_engine import (  # noqa: E402
    _live_off_card,
    _open_feishu_card,
    analysis_open_eligible,
    notify_live_auto_open_off,
    notify_open_cards,
)


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


# ── 正式网自动开单关闭提醒（2026-10-09）──────────────────────────────


def _analysis(**kw) -> AIAnalysis:
    base = dict(
        symbol="KSMUSDT", trade_decision="suggest", direction="long",
        entry_price=12.3456, stop_loss=12.0, take_profit_1=13.2,
        take_profit_2=None, risk_reward_ratio=2.0, recommendation=75,
        tactic_name="回踩接多",
    )
    base.update(kw)
    return AIAnalysis(**base)


def _cfg(**kw) -> SystemConfig:
    base = dict(id=1, trading_enabled_testnet=True, trading_enabled_live=False,
                min_recommendation=None)
    base.update(kw)
    return SystemConfig(**base)


def test_live_off_card_structure():
    """提醒卡：橙色头 + 达标说明 + 概览/价格栅格 + 手动开单指引脚注"""
    card = _live_off_card(_analysis())
    assert card["header"]["title"]["content"] == "⚠️ 正式网未开单 · KSMUSDT"
    assert card["header"]["template"] == "orange"
    md = [el for el in card["elements"] if el["tag"] == "div"][0]
    assert "正式网自动开单开关关闭" in md["text"]["content"]
    cols = [c for el in card["elements"] if el["tag"] == "column_set"
            for c in el["columns"]]
    text = [c["elements"][0]["text"]["content"] for c in cols]
    assert "**方向**\n做多 Long" in text
    assert "**AI 评分**\n75 分" in text
    assert "**盈亏比**\n2.0" in text
    assert "**入场价**\n12.3456" in text
    assert "**止盈 TP2**\n—" in text  # TP2 关闭口径：None → —
    note = [el for el in card["elements"] if el["tag"] == "note"][0]
    assert "手动开单" in note["elements"][0]["content"]
    assert "战法 回踩接多" in note["elements"][0]["content"]


def test_analysis_open_eligible_predicate():
    """资格谓词：suggest+方向+三价齐+评分达标 → True；各缺项 → False"""
    assert analysis_open_eligible(_analysis(), 60) is True
    assert analysis_open_eligible(_analysis(trade_decision="skip"), 60) is False
    assert analysis_open_eligible(_analysis(direction=None), 60) is False
    assert analysis_open_eligible(_analysis(stop_loss=None), 60) is False
    assert analysis_open_eligible(_analysis(recommendation=59.9), 60) is False
    assert analysis_open_eligible(None, 60) is False


def _live_off_db(monkeypatch, records: list):
    """内存库 + 已 mock 的飞书通道；返回 (db, cards)。records 追加入库"""
    engine = create_engine("sqlite:///:memory:")
    from app.database import Base
    import app.models  # noqa: F401
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    cfg = _cfg()
    db.add(cfg)
    for r in records:
        db.add(r)
    db.commit()
    cards: list = []
    monkeypatch.setattr(feishu_bot, "configured", lambda: True)
    monkeypatch.setattr(feishu_bot, "send_app_card",
                        lambda c: cards.append(c) or True)
    return db, cfg, cards


def test_notify_live_off_sends_for_eligible(monkeypatch):
    """正式网关 + 达标信号 → 发一张提醒卡（测试网开关开着不影响）"""
    a = _analysis()
    db, cfg, cards = _live_off_db(monkeypatch, [a])
    try:
        notify_live_auto_open_off(db, cfg, str(a.id))
        assert len(cards) == 1
        assert cards[0]["header"]["title"]["content"] == "⚠️ 正式网未开单 · KSMUSDT"
    finally:
        db.close()


def test_notify_live_off_skips_ineligible_and_live_opened(monkeypatch):
    """skip 信号不发；该分析正式网已有成交（手动开）不发；testnet 有单仍发"""
    # skip → 不发
    a_skip = _analysis(trade_decision="skip")
    db, cfg, cards = _live_off_db(monkeypatch, [a_skip])
    notify_live_auto_open_off(db, cfg, str(a_skip.id))
    assert cards == []
    # 正式网已有单（含手动开）→ 不发
    a_opened = _analysis(symbol="SUIUSDT")
    db.add(a_opened)
    db.flush()  # UUID 主键默认值 flush 时才生成，先落再引用
    db.add(TradeRecord(symbol="SUIUSDT", direction="long", status="OPENED",
                       account_key="live-1", testnet=False,
                       ai_analysis_id=a_opened.id))
    db.commit()
    notify_live_auto_open_off(db, cfg, str(a_opened.id))
    assert cards == []
    # 测试网有单不构成「实盘已有」→ 照发
    a_testnet = _analysis(symbol="ARBUSDT")
    db.add(a_testnet)
    db.flush()
    db.add(TradeRecord(symbol="ARBUSDT", direction="long", status="OPENED",
                       account_key="testnet-1", testnet=True,
                       ai_analysis_id=a_testnet.id))
    db.commit()
    notify_live_auto_open_off(db, cfg, str(a_testnet.id))
    assert len(cards) == 1
    db.close()


def test_notify_live_off_unconfigured(monkeypatch):
    """飞书未配置：静默跳过不发不炸"""
    a = _analysis()
    engine = create_engine("sqlite:///:memory:")
    from app.database import Base
    import app.models  # noqa: F401
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    db.add(_cfg())
    db.add(a)
    db.commit()
    monkeypatch.setattr(feishu_bot, "configured", lambda: False)

    def _boom(card):  # pragma: nocover
        raise AssertionError("未配置时不应尝试发送")

    monkeypatch.setattr(feishu_bot, "send_app_card", _boom)
    notify_live_auto_open_off(db, _cfg(), str(a.id))
    db.close()
