"""双网自动交易全关时，达标信号仍要推飞书提醒（2026-10-27）。

背景（18:00 那批 9 条 suggest 静默丢失）：`_dispatch_immediate_open` 在
「双网开关全关」时直接 return，既不分发开仓任务、也不发任何提醒——用户
只看到页面里 AI 打了 60/70 分、可以开单，飞书一条都没有。

本文件锁三件事：
1. 双网全关 + 达标分析 → 发橙卡（标题带「双网均关」），且**不**分发开仓任务；
2. 任一网开启 → 照旧分发，不发「双网均关」卡（避免与既有
   open_trade_for_analysis 里的正式网关提醒重复）；
3. 不达标（评分低于门槛 / 非 suggest）→ 什么都不做。
"""
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

import app.models  # noqa: F401
from app.database import Base
from app.models.scan import AIAnalysis
from app.models.system_config import SystemConfig
from app.tasks import ai_tasks
from app.services import trade_engine


class _NoCloseSession:
    """`_dispatch_immediate_open` 会在 finally 里 close()，测试里共享会话不能真关。"""

    def __init__(self, session):
        self._session = session

    def __getattr__(self, name):
        return getattr(self._session, name)

    def close(self):  # noqa: D401 - 故意 no-op
        return None


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session
    engine.dispose()


@pytest.fixture
def sent_cards(monkeypatch):
    """拦截所有外发（飞书 HTTP / K 线图 / celery），只记录卡片。"""
    cards: list = []
    monkeypatch.setattr("app.services.feishu_bot.configured", lambda: True)
    monkeypatch.setattr(
        "app.services.feishu_bot.send_app_card",
        lambda card, testnet=False: cards.append(card) or True,
    )
    monkeypatch.setattr(
        "app.services.feishu_bot.send_app_text",
        lambda text, testnet=False: cards.append({"text": text}) or True,
    )
    monkeypatch.setattr(trade_engine, "_live_off_img_key", lambda a, cfg: "")
    monkeypatch.setattr(trade_engine, "_off_card_suppressed", lambda symbol, direction: False)
    monkeypatch.setattr(
        "app.services.account_registry.live_enabled_accounts", lambda db: []
    )
    return cards


@pytest.fixture
def dispatched(monkeypatch):
    calls: list = []
    monkeypatch.setattr(
        ai_tasks.celery_app, "send_task",
        lambda name, args=None, **kw: calls.append((name, args)),
    )
    return calls


def _cfg(db, *, live: bool, testnet: bool, testnet_enabled: bool = True,
         min_rec=None) -> SystemConfig:
    cfg = SystemConfig(
        id=1, trading_enabled_live=live, trading_enabled_testnet=testnet,
        testnet_enabled=testnet_enabled, min_recommendation=min_rec,
    )
    db.add(cfg)
    db.commit()
    return cfg


def _analysis(db, *, rec=70.0, decision="suggest", direction="long") -> AIAnalysis:
    a = AIAnalysis(
        symbol="TESTUSDT", trade_decision=decision, direction=direction,
        entry_price=100.0, stop_loss=99.0, take_profit_1=102.0,
        recommendation=rec, risk_reward_ratio=2.0,
    )
    db.add(a)
    db.commit()
    return a


def test_both_off_sends_card_and_skips_dispatch(db, sent_cards, dispatched, monkeypatch):
    _cfg(db, live=False, testnet=False)
    monkeypatch.setattr(ai_tasks, "SessionLocal", lambda: _NoCloseSession(db))
    a = _analysis(db)

    ai_tasks._dispatch_immediate_open(a)

    assert dispatched == [], "双网全关不应分发开仓任务"
    assert len(sent_cards) == 1, "达标信号必须推一张提醒卡"
    title = sent_cards[0]["header"]["title"]["content"]
    assert "双网均关" in title
    assert "TESTUSDT" in title
    body = sent_cards[0]["elements"][0]["text"]["content"]
    assert "双网自动开单均已关闭" in body


def test_testnet_switch_off_by_master_still_counts_as_both_off(
    db, sent_cards, dispatched, monkeypatch
):
    """测试网分开关开着、但测试网总开关关 → 等效双网全关，同样要提醒。"""
    _cfg(db, live=False, testnet=True, testnet_enabled=False)
    monkeypatch.setattr(ai_tasks, "SessionLocal", lambda: _NoCloseSession(db))
    a = _analysis(db)

    ai_tasks._dispatch_immediate_open(a)

    assert dispatched == []
    assert len(sent_cards) == 1
    assert "双网均关" in sent_cards[0]["header"]["title"]["content"]


def test_live_on_dispatches_without_both_off_card(db, sent_cards, dispatched, monkeypatch):
    _cfg(db, live=True, testnet=False)
    monkeypatch.setattr(ai_tasks, "SessionLocal", lambda: _NoCloseSession(db))
    a = _analysis(db)

    ai_tasks._dispatch_immediate_open(a)

    assert [c[0] for c in dispatched] == ["app.tasks.trade_tasks.open_trade_for_analysis"]
    assert dispatched[0][1] == [str(a.id)]
    assert sent_cards == [], "自动开单开着时由 succeed/fail 卡负责，不发「双网均关」卡"


def test_below_threshold_sends_nothing(db, sent_cards, dispatched, monkeypatch):
    _cfg(db, live=False, testnet=False, min_rec=80)
    monkeypatch.setattr(ai_tasks, "SessionLocal", lambda: _NoCloseSession(db))
    a = _analysis(db, rec=70.0)

    ai_tasks._dispatch_immediate_open(a)

    assert dispatched == []
    assert sent_cards == [], "评分没到门槛不算「可以开单」，不该提醒"


def test_skip_decision_sends_nothing(db, sent_cards, dispatched, monkeypatch):
    _cfg(db, live=False, testnet=False)
    monkeypatch.setattr(ai_tasks, "SessionLocal", lambda: _NoCloseSession(db))
    a = _analysis(db, decision="skip", rec=25.0)

    ai_tasks._dispatch_immediate_open(a)

    assert dispatched == []
    assert sent_cards == []


def test_same_symbol_direction_once_per_utc_day(db, sent_cards, dispatched, monkeypatch):
    """整点批量场景：同币同向当日只推一条，防整点刷屏。"""
    _cfg(db, live=False, testnet=False)
    monkeypatch.setattr(ai_tasks, "SessionLocal", lambda: _NoCloseSession(db))
    monkeypatch.setattr(trade_engine, "_off_card_suppressed", lambda symbol, direction: True)
    a = _analysis(db)

    ai_tasks._dispatch_immediate_open(a)

    assert dispatched == []
    assert sent_cards == [], "当日已推过同币同向，应跳过"


def test_card_text_keeps_live_off_wording_when_only_live_off(db):
    """正式网关、测试网开的既有路径文案不能被「双网均关」覆盖。"""
    a = _analysis(db)
    card = trade_engine._live_off_card(a, both_off=False)

    assert card["header"]["title"]["content"] == f"⚠️ 正式网未开单 · {a.symbol}"
    body = card["elements"][0]["text"]["content"]
    assert "正式网自动开单开关关闭" in body
    assert "双网" not in body
