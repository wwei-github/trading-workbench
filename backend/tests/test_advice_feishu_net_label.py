# -*- coding: utf-8 -*-
"""AI 持仓建议飞书卡片网络标注（2026-10-04）

建议离场/滞留两张卡片的标题与首行都带「测试网/正式网」——双网同符号
同向开单时凭卡片内容（币种/价格）无法区分是哪张网的单，误点平仓按钮
会平错网。卡片体捕获断言，不发送不触网。
"""
import sys
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.position_advisor import _notify_advice, _notify_stale  # noqa: E402


@pytest.fixture()
def cards(monkeypatch):
    import app.services.feishu_bot as fb
    out = []
    monkeypatch.setattr(fb, "configured", lambda: True)
    monkeypatch.setattr(fb, "send_app_card", lambda card: out.append(card))
    return out


def _rec(testnet: bool):
    return SimpleNamespace(
        id="00000000-0000-0000-0000-000000000001",
        symbol="IOUSDT", direction="long", testnet=testnet,
        account_key="testnet-1" if testnet else "live-1",
        entry_price=0.17, opened_at=datetime.utcnow() - timedelta(hours=30),
        ai_snapshot={"current_price": 0.16, "stop_loss": 0.165},
        tp1=0.18,
    )


_ADVICE = {"action": "exit", "thesis_valid": False, "trend_broken": True,
           "reason": "逻辑破坏"}


def _first_line(card) -> str:
    """全宽高亮行：div 元素的 lark_md 文本"""
    return card["elements"][0]["text"]["content"]


def test_exit_card_net_label_both_nets(cards):
    """建议离场卡：测试网/正式网单的标题与首行各自带网标注"""
    _notify_advice(_rec(True), _ADVICE, -3.2)
    _notify_advice(_rec(False), _ADVICE, -3.2)
    assert len(cards) == 2
    tn, live = cards
    assert tn["header"]["title"]["content"].endswith("测试网 · IOUSDT")
    assert live["header"]["title"]["content"].endswith("正式网 · IOUSDT")
    assert _first_line(tn).startswith("测试网 多单")
    assert _first_line(live).startswith("正式网 多单")


def test_stale_card_net_label(cards):
    """滞留诊断卡：标题与首行带网标注（不依赖 action 类型）"""
    _notify_stale(_rec(False), {"action": "hold", "thesis_valid": True,
                                "trend_broken": False, "reason": "观察"}, 1.0)
    assert len(cards) == 1
    card = cards[0]
    assert "正式网 · IOUSDT" in card["header"]["title"]["content"]
    assert _first_line(card).startswith("正式网 多单")
