# -*- coding: utf-8 -*-
"""AI 错误归因词表与事实包回归（2026-10-15 词表修订，docs/16 §8 实证）

- 词表：新增「浮盈回撤」（实证最大漏损，此前混入口径不符的「执行偏差」）；
  退役「逆指标多数」「逆4h背景」（方向由程序多数决生成 + 4h 状态机硬约束接管，
  实盘周归因 0 次触发）；「执行偏差」定义收窄回费用摩擦
- patch_service.RULE_TAGS 自动跟随词表（新标签即日进入补丁热度闭环）
- build_facts 事实包含 signal_context：快照自持的止损锚/4h状态/量比/触点/战法，
  此前只进分组统计、归因 LLM 看不见（手动单无快照时全 None）
"""
from datetime import datetime, timedelta
from types import SimpleNamespace

from app.services.ai_attribution import ERROR_TAGS, build_facts
from app.services.patch_service import RULE_TAGS


def test_tag_vocabulary_revision():
    assert "浮盈回撤" in ERROR_TAGS
    assert "逆指标多数" not in ERROR_TAGS
    assert "逆4h背景" not in ERROR_TAGS
    # 执行偏差定义收窄：显式排除出场时机导致的浮盈缩水（归「浮盈回撤」）
    assert "浮盈回撤" in ERROR_TAGS["执行偏差"]


def test_rule_tags_follow_vocabulary():
    assert "浮盈回撤" in RULE_TAGS
    assert "逆指标多数" not in RULE_TAGS
    assert "逆4h背景" not in RULE_TAGS
    for t in ("行情异常", "无明显错误"):  # 非规则性标签恒不参与补丁触发
        assert t not in RULE_TAGS


def _rec(snapshot: dict | None = None) -> SimpleNamespace:
    now = datetime(2026, 10, 15, 8, 0, 0)
    return SimpleNamespace(
        symbol="BTCUSDT", direction="long",
        ai_snapshot=snapshot,
        opened_at=now - timedelta(hours=3), closed_at=now,
        entry_price=100.0, exit_reason="sl",
        realized_pnl=-5.0, pnl_pct=-1.0, fee_total=0.5, funding_total=0.1,
    )


def test_build_facts_signal_context_passthrough():
    rec = _rec({
        "entry_price": 100.0, "stop_loss": 99.0, "take_profit_1": 102.0,
        "take_profit_2": None, "trade_type": "pullback", "recommendation": 75,
        "position_pct": 10, "htf_phase": "aligned_bull", "stop_anchor": "near",
        "volume_ratio": 2.5, "touch_ref": "retest", "tactic_name": "破底翻",
        "analysis": "x",
    })
    facts = build_facts(rec, [])
    assert facts["signal_context"] == {
        "htf_phase": "aligned_bull",
        "stop_anchor": "near",
        "volume_ratio": 2.5,
        "touch_ref": "retest",
        "tactic_name": "破底翻",
    }


def test_build_facts_signal_context_none_without_snapshot():
    # 手动单/导入持仓：无快照 → 信号上下文全 None（LLM 按未知处理）
    facts = build_facts(_rec(), [])
    assert facts["signal_context"] == {
        "htf_phase": None, "stop_anchor": None, "volume_ratio": None,
        "touch_ref": None, "tactic_name": None,
    }
