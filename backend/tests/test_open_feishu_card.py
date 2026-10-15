# -*- coding: utf-8 -*-
"""自动开仓飞书卡片（2026-10-02）+ 正式网开关关闭提醒（2026-10-09）

1. _open_feishu_card 卡片结构：header 币种、概览行（方向/杠杆/风险/评分）、
   价格栅格（入场/SL/TP1/TP2，无 TP2 显示 —）、标注行（数量/名义/信号/战法）、
   环境脚注（测试网/正式网 + 账号）
2. notify_open_cards：configured=False 整批跳过；正常逐笔发送（SessionLocal
   替身注入 sqlite）；单条记录缺失不中断其余
3. 正式网自动开单关闭提醒：analysis_open_eligible 资格谓词；达标信号发橙色
   提醒卡；skip 信号/实盘已有单/未配置飞书均不发
4. 正式网未开单卡片 2026-10-15 增强：内嵌 K 线图（img 元素）+ 单账号「开单」
   按钮；图/按钮任一不可用退化为纯文字卡片；画图链路（拉K线→渲染→上传）
   失败返回空 img_key
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
from app.services import trade_engine  # noqa: E402
from app.services.trade_engine import (  # noqa: E402
    _live_off_card,
    _live_off_img_key,
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


def _live_off_db(monkeypatch, records: list, img_key: str = ""):
    """内存库 + 已 mock 的飞书通道；返回 (db, cfg, cards)。records 追加入库。

    默认把画图链路（拉K线→渲染→上传，会真连交易所）短路成空 img_key，测试
    只关心卡片结构与分支；画图链路本身另有用例覆盖。
    """
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
    monkeypatch.setattr(trade_engine, "_live_off_img_key", lambda a, cfg: img_key)
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


# ── 正式网未开单卡片内嵌 K 线图 + 开单按钮（2026-10-15）──────────────


def _synth_klines(n: int = 300, base: float = 100.0) -> list:
    """合成 K 线（不触网）：正弦走位 + 影线，尾根未收盘口径一致"""
    import math

    out = []
    ts0 = 1_700_000_000_000
    for i in range(n):
        c = base * (1 + 0.02 * math.sin(i / 7))
        o = c * 0.999
        out.append([ts0 + i * 3_600_000, o, max(o, c) * 1.004,
                    min(o, c) * 0.996, c, 1000.0])
    return out


def test_live_off_card_with_image_and_button(monkeypatch):
    """img_key → img 元素；单账号 + 令牌 → action 按钮带 act/token/account_key"""
    from app.config import settings
    monkeypatch.setattr(settings, "FEISHU_ACTION_TOKEN", "tok")
    a = _analysis()
    card = _live_off_card(a, img_key="img_v2_abc", open_account_key="live-1")
    imgs = [el for el in card["elements"] if el["tag"] == "img"]
    assert len(imgs) == 1
    assert imgs[0]["img_key"] == "img_v2_abc"
    assert imgs[0]["alt"]["tag"] == "plain_text"
    assert "KSMUSDT" in imgs[0]["alt"]["content"]
    btns = [el for el in card["elements"] if el.get("tag") == "action"]
    assert len(btns) == 1
    val = btns[0]["actions"][0]["value"]
    assert val["act"] == "open_live" and val["token"] == "tok"
    assert val["account_key"] == "live-1"
    assert set(val) == {"act", "token", "analysis_id", "account_key"}


def test_live_off_card_degrades_without_image_and_button(monkeypatch):
    """无 img_key → 无 img 元素；未传账号（多账号）/ 缺令牌 → 无按钮"""
    from app.config import settings
    monkeypatch.setattr(settings, "FEISHU_ACTION_TOKEN", "tok")
    card = _live_off_card(_analysis())
    assert [el for el in card["elements"] if el["tag"] == "img"] == []
    assert [el for el in card["elements"] if el.get("tag") == "action"] == []
    # 单账号但未配按钮令牌 → 同样不挂按钮（position_advisor 同惯例）
    monkeypatch.setattr(settings, "FEISHU_ACTION_TOKEN", "")
    card = _live_off_card(_analysis(), img_key="img_v2_abc", open_account_key="live-1")
    assert [el for el in card["elements"] if el.get("tag") == "action"] == []
    assert len([el for el in card["elements"] if el["tag"] == "img"]) == 1


def test_live_off_img_key_renders_and_uploads(monkeypatch):
    """画图链路：拉K线（3参数）→ 渲染 PNG → 上传，返回 image_key"""
    seen: dict = {}
    monkeypatch.setattr(
        "app.services.exchange_pool.ExchangePool.get_klines",
        lambda self, symbol, interval, limit, **kw: seen.update(
            symbol=symbol, interval=interval, limit=limit, **kw) or _synth_klines())

    def _upload(png: bytes) -> str:
        seen["png"] = png
        return "img_v2_ok"

    monkeypatch.setattr(feishu_bot, "upload_image", _upload)
    key = _live_off_img_key(_analysis(), _cfg())
    assert key == "img_v2_ok"
    assert seen["symbol"] == "KSMUSDT" and seen["limit"] == 300
    assert seen["allow_stale"] is True
    assert seen["png"][:8] == b"\x89PNG\r\n\x1a\n"


def test_live_off_img_key_degrades_on_error(monkeypatch):
    """K线拉取失败 / 渲染失败 → 返回空字符串（通知降级纯文字卡，不抛错）"""
    def _boom(self, *a, **k):
        raise RuntimeError("交易所全挂")

    monkeypatch.setattr("app.services.exchange_pool.ExchangePool.get_klines", _boom)
    assert _live_off_img_key(_analysis(), _cfg()) == ""
    # 数据不足（渲染返回 None）→ 同样空，且不上传
    monkeypatch.setattr(
        "app.services.exchange_pool.ExchangePool.get_klines",
        lambda self, *a, **k: _synth_klines(10))

    def _upload(png):  # pragma: nocover
        raise AssertionError("渲染失败时不应上传")

    monkeypatch.setattr(feishu_bot, "upload_image", _upload)
    assert _live_off_img_key(_analysis(), _cfg()) == ""


def test_notify_live_off_embeds_image_and_button(monkeypatch):
    """notify：单账号 + 图 key 非空 → 卡片同时带 img 与 action 按钮"""
    from app.config import settings
    monkeypatch.setattr(settings, "FEISHU_ACTION_TOKEN", "tok")
    a = _analysis()
    db, cfg, cards = _live_off_db(monkeypatch, [a], img_key="img_v2_x")
    monkeypatch.setattr("app.services.account_registry.live_enabled_accounts",
                        lambda _db: [type("A", (), {"key": "live-1"})()])
    try:
        notify_live_auto_open_off(db, cfg, str(a.id))
    finally:
        db.close()
    assert len(cards) == 1
    assert len([e for e in cards[0]["elements"] if e["tag"] == "img"]) == 1
    btn = [e for e in cards[0]["elements"] if e.get("tag") == "action"][0]
    assert btn["actions"][0]["value"]["account_key"] == "live-1"


def test_open_live_callback_dispatch_and_token(monkeypatch):
    """回调分支：令牌错 → 报错不派发；令牌对 → 后台派发（同步替身线程）"""
    from types import SimpleNamespace

    from app.config import settings
    monkeypatch.setattr(settings, "FEISHU_ACTION_TOKEN", "tok")
    called: list = []
    monkeypatch.setattr(feishu_bot, "_exec_open_live",
                        lambda aid, acc: called.append((aid, acc)))

    class _SyncThread:  # 回调用守护线程执行，测试里同步跑保证断言确定
        def __init__(self, target=None, args=(), daemon=None):
            self.target, self.args = target, args

        def start(self):
            self.target(*self.args)

    monkeypatch.setattr(feishu_bot.threading, "Thread", _SyncThread)

    def _data(value):
        return SimpleNamespace(event=SimpleNamespace(
            action=SimpleNamespace(value=value)))

    base = {"act": "open_live", "analysis_id": "aid", "account_key": "live-1"}
    resp = feishu_bot._on_card_action(_data(dict(base, token="bad")))
    assert "无效操作令牌" in resp.toast.content and called == []
    resp = feishu_bot._on_card_action(_data(dict(base, token="tok")))
    assert called == [("aid", "live-1")]
    assert "开单执行中" in resp.toast.content
    resp = feishu_bot._on_card_action(_data({"act": "open_live", "token": "tok"}))
    assert "缺少开单参数" in resp.toast.content


def test_exec_open_live_result_cards(monkeypatch):
    """执行结果三态：成交 → 绿卡带价格栅格；闸门未过 → 橙卡带原因；失败 → 红卡"""
    cards: list = []
    monkeypatch.setattr(feishu_bot, "send_app_card",
                        lambda c: cards.append(c) or True)
    monkeypatch.setattr("app.tasks.trade_tasks.manual_open_from_analysis",
                        lambda aid, acc: {"ok": True, "opened": True,
                                          "account_key": acc, "symbol": "KSMUSDT",
                                          "direction": "long", "entry_price": 12.3456,
                                          "stop_loss": 12.0, "take_profit_1": 13.2,
                                          "take_profit_2": None, "qty": 8.5})
    feishu_bot._exec_open_live("aid", "live-1")
    assert cards[0]["header"]["title"]["content"] == "✅ 正式网手动开单 · KSMUSDT"
    assert "数量 8.5" in cards[0]["elements"][-1]["elements"][0]["content"]

    monkeypatch.setattr("app.tasks.trade_tasks.manual_open_from_analysis",
                        lambda aid, acc: {"ok": True, "opened": False,
                                          "symbol": "KSMUSDT",
                                          "block_reason": "同币种已有在跑持仓"})
    feishu_bot._exec_open_live("aid", "live-1")
    assert cards[1]["header"]["title"]["content"] == "⚠️ 未开单 · KSMUSDT"
    assert "同币种已有在跑持仓" in cards[1]["elements"][0]["text"]["content"]

    monkeypatch.setattr("app.tasks.trade_tasks.manual_open_from_analysis",
                        lambda aid, acc: {"ok": False, "code": "conflict",
                                          "error": "与结算巡检/其他开仓互斥中，请稍后重试"})
    feishu_bot._exec_open_live("aid", "live-1")
    assert cards[2]["header"]["title"]["content"] == "❌ 开单未执行"
    assert "互斥" in cards[2]["elements"][0]["text"]["content"]
