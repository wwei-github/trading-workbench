# -*- coding: utf-8 -*-
"""辅助任务的任务记录条目单测（2026-10-05 补日志缺口 3）

此前 daily_report / ai_tasks(分发层) / shadow_tasks 三个 celery 任务无
open_task/close_task 条目——失败只在系统日志可见，任务记录页看不到
「上次成功/失败/耗时」。本文件验证三个任务的任务记录生命周期：

1. notify_daily_report：completed + 统计 summary（飞书/行情全替身）
2. run_shadow_evaluate_task：completed + 达标特征 summary
3. run_ai_analysis_task：AI 未配置 → skipped；分发成功 → completed

task_log 与任务模块的 SessionLocal 全部 monkeypatch 到同一 sqlite 内存库
（StaticPool + check_same_thread=False，TestClient 同款跨线程惯例）。
"""
import sys
from datetime import datetime
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import app.models  # noqa: E402,F401
from app.config import settings  # noqa: E402
from app.database import Base  # noqa: E402
from app.models.ops import TaskRecord  # noqa: E402
from app.models.system_config import SystemConfig  # noqa: E402


@pytest.fixture()
def db_engine():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return engine


@pytest.fixture()
def wire_sessions(db_engine, monkeypatch):
    """task_log 与三个任务模块共用同一测试库"""
    from app.services import task_log
    from app.tasks import ai_tasks, daily_report, shadow_tasks

    maker = sessionmaker(bind=db_engine)
    seed = maker()
    seed.add(SystemConfig(id=1))
    seed.commit()
    seed.close()
    for mod in (task_log, daily_report, shadow_tasks, ai_tasks):
        monkeypatch.setattr(mod, "SessionLocal", maker)
    return maker


def _latest_record(maker) -> TaskRecord:
    s = maker()
    try:
        return s.execute(
            select(TaskRecord).order_by(TaskRecord.started_at.desc())
        ).scalars().first()
    finally:
        s.close()


# ── 1. 每日日报 ──────────────────────────────────────────────────────


def test_daily_report_completes_with_summary(wire_sessions, monkeypatch):
    """日报成功：completed + 「开X/闭Y 净Z」summary + notified 落 detail"""
    from app.tasks import daily_report

    # 行情替身（不触币安）；统计走空库（0 单）
    monkeypatch.setattr(
        daily_report, "_daily_close",
        lambda sym: (sym.replace("USDT", ""), "100.00", "+1.0%"),
    )
    sent = {}
    monkeypatch.setattr(
        daily_report, "send_feishu_card", lambda card: sent.update(card=card) or True)
    monkeypatch.setattr(
        daily_report, "send_feishu_text", lambda text: sent.update(text=text) or True)

    out = daily_report.notify_daily_report()

    assert out["notified"] is True
    rec = _latest_record(wire_sessions)
    assert rec is not None
    assert rec.task_type == "review" and rec.task_name == "每日交易日报"
    assert rec.status == "completed"
    assert "开0/闭0" in rec.summary and "飞书已推送" in rec.summary
    assert rec.finished_at is not None


def test_daily_report_failure_marks_failed(wire_sessions, monkeypatch):
    """日报中途异常（行情替身抛错）：failed + error 落库，异常继续上抛"""
    from app.tasks import daily_report

    def _boom(sym):
        raise RuntimeError("币安不可用")

    monkeypatch.setattr(daily_report, "_daily_close", _boom)

    with pytest.raises(RuntimeError):
        daily_report.notify_daily_report()

    rec = _latest_record(wire_sessions)
    assert rec.status == "failed" and "币安不可用" in (rec.error or "")


def test_daily_report_splits_stats_by_network(wire_sessions, monkeypatch):
    """日报：交易统计与净收益按正式网/测试网分列（2026-10-19）

    昨日各留一笔闭单（正式网 +120 / 测试网 -40），断言卡片、任务记录
    summary 与返回值都把两网分开统计，互不混入。
    """
    import json
    from datetime import timedelta, timezone

    from app.models.trade import TradeRecord
    from app.tasks import daily_report

    bj = timezone(timedelta(hours=8))
    yday = (datetime.now(bj) - timedelta(days=1)).replace(
        hour=12, minute=0, second=0, microsecond=0, tzinfo=None)
    maker = wire_sessions
    s = maker()
    s.add_all([
        TradeRecord(symbol="BTCUSDT", direction="long", status="CLOSED",
                    testnet=False, account_key="live-1",
                    opened_at=yday, closed_at=yday, realized_pnl=120.0),
        TradeRecord(symbol="ETHUSDT", direction="short", status="CLOSED",
                    testnet=True, account_key="testnet-1",
                    opened_at=yday, closed_at=yday, realized_pnl=-40.0),
    ])
    s.commit()
    s.close()

    monkeypatch.setattr(
        daily_report, "_daily_close",
        lambda sym: (sym.replace("USDT", ""), "100.00", "+1.0%"))
    sent = {}
    monkeypatch.setattr(
        daily_report, "send_feishu_card", lambda card: sent.update(card=card) or True)
    monkeypatch.setattr(
        daily_report, "send_feishu_text", lambda text: sent.update(text=text) or True)

    out = daily_report.notify_daily_report()

    assert out["notified"] is True
    assert out["live"]["closed"] == 1 and out["live"]["net"] == pytest.approx(120.0)
    assert out["testnet"]["closed"] == 1 and out["testnet"]["net"] == pytest.approx(-40.0)

    # 卡片：两网分列 + 各自净收益
    card_text = json.dumps(sent["card"], ensure_ascii=False)
    assert "正式网" in card_text and "测试网" in card_text
    assert "+120.00 USDT" in card_text and "-40.00 USDT" in card_text

    # 任务记录 summary 分网呈现，两网互不混入
    rec = _latest_record(maker)
    assert "正式网 开1/闭1 净+120.00 USDT" in rec.summary
    assert "测试网 开1/闭1 净-40.00 USDT" in rec.summary


# ── 2. 影子评估 ──────────────────────────────────────────────────────


def test_shadow_evaluate_completes_with_summary(wire_sessions, monkeypatch):
    """影子评估成功：completed + 达标特征 summary"""
    from app.tasks import shadow_tasks

    monkeypatch.setattr(
        shadow_tasks, "build_report_safe",
        lambda db, window_days=30: [
            {"feature": "cvd_div", "baseline": {"n": 42}, "sufficient": True},
            {"feature": "vol_regime", "baseline": {"n": 5}, "sufficient": False},
        ])

    out = shadow_tasks.run_shadow_evaluate_task()
    assert out["total_marks"] == 47

    rec = _latest_record(wire_sessions)
    assert rec.task_name == "影子实验评估" and rec.status == "completed"
    assert "标记 47 条" in rec.summary and "cvd_div" in rec.summary


# ── 3. AI 分析分发 ───────────────────────────────────────────────────


def test_ai_dispatch_skipped_without_key(wire_sessions, monkeypatch):
    """未配置 AI Key：skipped + 原因 summary（此前静默 return 无记录）"""
    from app.tasks import ai_tasks

    monkeypatch.setattr(settings, "AI_API_KEY", "")

    ai_tasks.run_ai_analysis_task(str(__import__("uuid").uuid4()))

    rec = _latest_record(wire_sessions)
    assert rec.task_type == "ai" and rec.status == "skipped"
    assert "未配置 API Key" in rec.summary


def test_ai_dispatch_completed(wire_sessions, monkeypatch, db_engine):
    """分发成功：completed + 「已分发 N 个分析子任务」summary"""
    import uuid as _uuid

    from app.models.scan import ScanResult
    from app.tasks import ai_tasks

    monkeypatch.setattr(settings, "AI_API_KEY", "test-key")
    monkeypatch.setattr(settings, "FINGERPRINT_TTL_MIN", 0)

    s = wire_sessions()
    cfg = s.get(SystemConfig, 1)
    cfg.ai_analysis_enabled = True  # 默认 False 会走「开关关闭」skip 分支
    s.commit()
    sr_id = _uuid.uuid4()
    s.add(ScanResult(
        id=_uuid.uuid4(), scan_record_id=sr_id, symbol="AAAUSDT",
        signal_type="breakout", current_price=100.0, breakout_pct=3.0,
        trend_slope=0.5, r_squared=0.8,
        volume=1000.0, volume_24h=1e7, created_at=datetime.utcnow(),
    ))
    s.commit()
    s.close()

    class _FakeGroup:
        """group 替身：记录子任务签名，apply_async 空操作"""

        def __init__(self):
            self.jobs = []

        def __call__(self, gen):
            self.jobs = list(gen)
            return self

        def apply_async(self):
            pass

    dispatched = _FakeGroup()
    monkeypatch.setattr(ai_tasks, "group", dispatched)

    ai_tasks.run_ai_analysis_task(str(sr_id))

    rec = _latest_record(wire_sessions)
    assert rec.task_name == "AI 分析分发" and rec.status == "completed"
    assert "已分发 1 个" in rec.summary
    assert dispatched.jobs  # group 替身被调用（分发动作发生）
