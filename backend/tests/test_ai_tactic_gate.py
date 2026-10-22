# -*- coding: utf-8 -*-
"""战法 AI 集成回归（2026-10-02，战法仅借鉴不拦截）。

覆盖：
1. tactics_match：装载按 scope 过滤（enabled/环境）、渲染块、白名单
2. normalize_raw：空串归 None、超长截断
3. _tactic_gate / validate_decision：白名单内带出、清单外清空、skip 路径同样携带
4. 提示词注入：_build_messages 战法块进用户消息；无战法不注入

不调 LLM 不触网。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services import tactics_match  # noqa: E402
from app.services.ai_analyzer import _build_messages  # noqa: E402
from app.services.risk_guard import (  # noqa: E402
    TradeDecision,
    normalize_raw,
    validate_decision,
)


_TACTICS = [
    {"name": "EMA20回踩做多", "description": "多头排列回踩 EMA20 企稳收阳"},
    {"name": "假突破反手", "description": "创阶段新高即收回位下方，反向入场"},
]


def test_load_active_tactics_scope_filter(monkeypatch):
    """enabled + scope 过滤：停用不注入、环境不符不注入；解析失败降级空表。

    用真实 sqlite 内存库验证 SQL 级 where 过滤（假 session 测不到）。"""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

    from app.database import Base
    from app.models.tactic import Tactic

    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    TestSession = sessionmaker(bind=engine)
    s = TestSession()
    s.add_all([
        Tactic(name="A", description="a", enabled=True, scope="both"),
        Tactic(name="B", description="b", enabled=False, scope="testnet"),  # 停用
        Tactic(name="C", description="c", enabled=True, scope="live"),      # 环境不符
    ])
    s.commit()
    monkeypatch.setattr(tactics_match, "SessionLocal", TestSession)

    # testnet 环境：A（both）命中，B 停用 / C 环境不符不注入
    assert [t["name"] for t in tactics_match.load_active_tactics(True)] == ["A"]
    # live 环境：A（both）+ C（live）命中，B 停用不注入
    assert [t["name"] for t in tactics_match.load_active_tactics(False)] == ["A", "C"]
    # 删环境切换（2026-10-03）：None=双网 scope 并集，全部活动战法装载
    assert [t["name"] for t in tactics_match.load_active_tactics(None)] == ["A", "C"]
    s.close()


def test_render_block_and_whitelist():
    block = tactics_match.render_tactics_block(_TACTICS)
    assert "【战法库判定】" in block
    assert "1. EMA20回踩做多：多头排列回踩 EMA20 企稳收阳" in block
    assert "仅作借鉴参考" in block
    assert tactics_match.allowed_names(_TACTICS) == ["EMA20回踩做多", "假突破反手"]
    assert tactics_match.render_tactics_block([]) == ""  # 无战法不注入


def test_normalize_tactic_fields():
    d = normalize_raw({
        "trade_decision": "suggest", "tactic_name": "  EMA20回踩做多  ",
        "tactic_reason": "回踩企稳" * 70,  # 280 字符超长 → 截断 255
    })
    assert d.tactic_name == "EMA20回踩做多"
    assert len(d.tactic_reason) == 255
    d2 = normalize_raw({"trade_decision": "skip", "skip_reason": "x",
                        "tactic_name": "", "tactic_reason": None})
    assert d2.tactic_name is None and d2.tactic_reason is None


def _gate_signal() -> dict:
    return {"symbol": "TESTUSDT", "signal_type": "trigger_pullback_confirm",
            "tactic_names": ["EMA20回踩做多"]}


def test_validate_decision_tactic_gate():
    """白名单内：suggest/skip 均带出；清单外：清空防幻觉"""
    ok_d = TradeDecision(
        trade_decision="suggest", direction="long", trade_type="trend_follow",
        entry_price=101.0, stop_loss=99.0, take_profit_1=104.0,
        recommendation=70, tactic_name="EMA20回踩做多", tactic_reason="回踩企稳",
    )
    ok, _, fixed = validate_decision(ok_d, _gate_signal(), [])
    assert ok and fixed["tactic_name"] == "EMA20回踩做多"
    assert fixed["tactic_reason"] == "回踩企稳"

    # 清单外（LLM 编造）→ 清空
    bad_d = TradeDecision(
        trade_decision="suggest", direction="long", trade_type="trend_follow",
        entry_price=101.0, stop_loss=99.0, take_profit_1=104.0,
        recommendation=70, tactic_name="不存在的战法", tactic_reason="编造",
    )
    ok2, _, fixed2 = validate_decision(bad_d, _gate_signal(), [])
    assert ok2 and fixed2["tactic_name"] is None and fixed2["tactic_reason"] is None

    # skip 路径同样携带（命中）
    skip_d = TradeDecision(
        trade_decision="skip", skip_reason="1. 盈亏比不佳",
        tactic_name="EMA20回踩做多", tactic_reason="符合但空间不足",
    )
    ok3, _, fixed3 = validate_decision(skip_d, _gate_signal(), [])
    assert ok3 and fixed3["tactic_name"] == "EMA20回踩做多"

    # 无白名单（旧调用方未注入）→ 一律清空（防跨环境脏标注）
    plain = TradeDecision(
        trade_decision="suggest", direction="long", trade_type="trend_follow",
        entry_price=101.0, stop_loss=99.0, take_profit_1=104.0,
        recommendation=70, tactic_name="EMA20回踩做多",
    )
    ok4, _, fixed4 = validate_decision(plain, {"symbol": "X"}, [])
    assert ok4 and fixed4["tactic_name"] is None


def test_build_messages_injects_tactics_block():
    """战法块注入用户消息（摆动结构后、K线 CSV 前）；空块不注入"""
    signal = {
        "symbol": "TESTUSDT", "signal_type": "trigger_pullback_confirm",
        "current_price": 100.0, "breakout_pct": 1.0, "pattern": None,
        "signal_reason": "r", "volume_type": "平量", "volume": 1.0,
    }
    klines = [[i * 3600_000, 100, 101, 99, 100, 10.0] for i in range(50)]
    msgs = _build_messages(signal, klines, tactics_block="【战法库判定】\n1. A：b\n")
    user = msgs[1]["content"]
    assert "【战法库判定】" in user
    assert user.index("【战法库判定】") < user.index("已收盘")
    msgs2 = _build_messages(signal, klines, tactics_block="")
    assert "【战法库判定】" not in msgs2[1]["content"]
