"""持仓时长分级警告单测（2026-09-27）

trade_records 主键为 PG 专用 UUID 类型（sqlite create_all 不兼容），
按 ORM 映射列手工建表后走 ORM 查询验证 check_hold_warnings：
分级阈值（12h 黄/24h 红）/ 只升级不重发 / 在跑状态过滤 / 卡片结构。
"""
import sys
import uuid
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.trade_engine import (  # noqa: E402
    _HOLD_WARN_RED_H,
    _HOLD_WARN_YELLOW_H,
    check_hold_warnings,
)

# 全部映射列建齐（ORM select(TradeRecord) 拉全列，缺列报错）；类型按 sqlite 宽松处理
_COLS = (
    "id VARCHAR(40) PRIMARY KEY, symbol VARCHAR(32), direction VARCHAR(8),"
    " scan_result_id VARCHAR(40), ai_analysis_id VARCHAR(40), recommendation FLOAT,"
    " signal_type VARCHAR(32), pattern VARCHAR(32), strategy_id VARCHAR(16), testnet BOOLEAN,"
    " account_key VARCHAR(20), ai_snapshot JSON, entry_price FLOAT, qty FLOAT,"
    " notional FLOAT, leverage INTEGER, margin_mode VARCHAR(12), margin_used FLOAT,"
    " risk_amount FLOAT, risk_budget_mode VARCHAR(8), beta FLOAT, stop_loss FLOAT, tp1 FLOAT,"
    " tp2 FLOAT, status VARCHAR(16), opened_at TIMESTAMP, closed_at TIMESTAMP,"
    " realized_pnl FLOAT, unrealized_pnl FLOAT, pnl_pct FLOAT, exit_reason VARCHAR(32),"
    " fee_total FLOAT, funding_total FLOAT, error_tags JSON, error_lesson TEXT,"
    " attributed_at TIMESTAMP, ai_advice JSON, ai_advice_at TIMESTAMP,"
    " ai_advice_pnl FLOAT, peak_total_pnl FLOAT, hold_warn_level INTEGER,"
    " rule_version VARCHAR(16), profile_key VARCHAR(16),"
    " raw JSON, created_at TIMESTAMP, updated_at TIMESTAMP"
)


def _row(symbol, status, opened_h_ago, warn_level=0, testnet=0):
    opened = f"'{(datetime.utcnow() - timedelta(hours=opened_h_ago)).isoformat(sep=' ')}'"
    # id 用无连字符 hex：sqlite 下 PG UUID 类型绑定为 hex 串（带连字符不匹配，
    # 否则 ORM UPDATE WHERE id 报 StaleDataError）
    return (
        f"INSERT INTO trade_records (id, symbol, direction, status, opened_at,"
        f" entry_price, stop_loss, unrealized_pnl, hold_warn_level, testnet) VALUES ("
        f"'{uuid.uuid4().hex}', '{symbol}', 'long', '{status}', {opened},"
        f" 100.0, 99.0, -5.0, {warn_level}, {testnet})"
    )


def _db(*rows):
    engine = create_engine("sqlite:///:memory:")
    with engine.connect() as conn:
        conn.execute(text(f"CREATE TABLE trade_records ({_COLS})"))
        for stmt in rows:
            conn.execute(text(stmt))
        conn.commit()
    return Session(bind=engine)


def test_thresholds_and_levels():
    """13h→黄卡(level 1)；25h→红卡(level 2)；5h/CLOSED 不告警"""
    db = _db(_row("A", "OPENED", 13.0), _row("B", "OPENED", 25.0),
             _row("C", "OPENED", _HOLD_WARN_YELLOW_H - 1),
             _row("D", "CLOSED", _HOLD_WARN_RED_H + 5))
    cards = check_hold_warnings(db)
    assert sorted(c["_symbol"] for c in cards) == ["A", "B"]
    yellow = next(c for c in cards if c["_symbol"] == "A")
    red = next(c for c in cards if c["_symbol"] == "B")
    assert yellow["header"]["template"] == "yellow"
    assert "持仓超12小时" in yellow["header"]["title"]["content"] and "A" in yellow["header"]["title"]["content"]
    assert red["header"]["template"] == "red"
    levels = dict(db.execute(text(
        "SELECT symbol, hold_warn_level FROM trade_records")).all())
    assert levels == {"A": 1, "B": 2, "C": 0, "D": 0}


def test_upgrade_and_idempotent():
    """已发黄卡(1)的超 24h 单升级红卡；已发红卡(2)不重发"""
    db = _db(_row("A", "OPENED", 25.0, warn_level=1),
             _row("B", "OPENED", 30.0, warn_level=2))
    cards = check_hold_warnings(db)
    assert [c["_symbol"] for c in cards] == ["A"]
    assert cards[0]["header"]["template"] == "red"
    assert check_hold_warnings(db) == []  # 幂等：本轮再查无新卡


def test_tp1_hit_status_included():
    """TP1_HIT（部分止盈仍在跑）同样参与判定"""
    db = _db(_row("A", "TP1_HIT", 24.0))
    cards = check_hold_warnings(db)
    assert len(cards) == 1 and cards[0]["header"]["template"] == "red"


def test_card_body_structure():
    """卡片含方向/开仓价/止损/浮盈盈亏列与内部日志字段"""
    db = _db(_row("BIG/USDT", "OPENED", 13.0))
    (card,) = check_hold_warnings(db)
    cols = card["elements"][1]["columns"]
    titles = [c["elements"][0]["text"]["content"].split("\n")[0].strip("*") for c in cols]
    assert titles == ["方向", "开仓价", "当前止损", "浮动盈亏"]
    assert card["_symbol"] == "BIG/USDT" and card["_hours"] == pytest.approx(13.0, abs=0.1)


def test_testnet_managed_too():
    """测试网系统单照常判定（2026-09-29：巡检只排除 App 手动单接管，
    系统自开的单含测试网都在巡检范围内）——测试网/正式网都出卡"""
    db = _db(_row("T", "OPENED", 13.0, testnet=1), _row("L", "OPENED", 13.0, testnet=0))
    cards = check_hold_warnings(db)
    assert sorted(c["_symbol"] for c in cards) == ["L", "T"]
    assert cards[0]["header"]["template"] == "yellow"
