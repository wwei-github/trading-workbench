# -*- coding: utf-8 -*-
"""每小时技术结构检测任务（structure-watch）回归（2026-10-13）。

覆盖：beat 注册（整点）/ 正常静默 / 破位告警 / 同签名去重 / 异常恢复通知 /
周期冲突 + 数据异常 / 触发延迟标记 / 摆动标注已收盘口径 / _stale·_gaps 边界；
并验证任务 detail 与顶部大盘条快照
（market_data.get_structure_snapshot）消费字段对齐（防两模块漂移）。

task_log / 任务模块 / market_data 的 SessionLocal 全部 monkeypatch 到同一
sqlite 内存库（StaticPool，test_task_records.py 同款）；取数与飞书全替身不触网。
K 线为「收盘价摆动」合成序列：trend 判定只吃 close，h/l 仅占位。
"""
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import app.models  # noqa: E402,F401
from app.database import Base  # noqa: E402
from app.models.ops import TaskRecord  # noqa: E402
from app.tasks import structure_watch as sw  # noqa: E402

_TZ_BJ = timezone(timedelta(hours=8))
_H1 = 3600_000
_H4 = 4 * 3600_000


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
    """task_log / structure_watch / market_data 共用同一测试库；摆动阶数固定 3。

    触发时间固定为「当前整点后 2 秒」（生产 beat 整点触发）：lag 门（>30s）
    在多数用例下不误报，越界场景由用例 7 单独覆盖。
    """
    from app.services import market_data, task_log

    maker = sessionmaker(bind=db_engine)
    for mod in (task_log, sw, market_data):
        monkeypatch.setattr(mod, "SessionLocal", maker)
    monkeypatch.setattr(sw, "_resolve_order", lambda: 3)
    monkeypatch.setattr(
        sw, "_now_bj",
        lambda: datetime.now(_TZ_BJ).replace(minute=0, second=2, microsecond=0),
    )
    return maker


@pytest.fixture()
def sent(monkeypatch):
    """飞书替身：记录卡片/文本，均视为发送成功"""
    box = {"cards": [], "texts": []}
    monkeypatch.setattr(sw, "send_feishu_card", lambda card: box["cards"].append(card) or True)
    monkeypatch.setattr(sw, "send_feishu_text", lambda text: box["texts"].append(text) or True)
    return box


@pytest.fixture()
def now_ts():
    return datetime.now(timezone.utc).timestamp()


def _latest(maker) -> TaskRecord:
    s = maker()
    try:
        return s.execute(select(TaskRecord).order_by(TaskRecord.started_at.desc())).scalars().first()
    finally:
        s.close()


def _seed_prev(maker, sig: str) -> None:
    """预置上一轮 completed 记录（去重/恢复判定的输入）。"""
    s = maker()
    s.add(TaskRecord(
        id=uuid4(), task_type="review", task_name="技术结构检测", trigger="scheduled",
        status="completed", summary="上一轮", detail={"anomaly_signature": sig},
        started_at=datetime.utcnow(),
    ))
    s.commit()
    s.close()


# ── 合成 K 线 ─────────────────────────────────────────────────────────


def _ramp(a: float, b: float, n: int) -> list[float]:
    """n 根从 a 线性走到 b（含两端）。"""
    if n <= 1:
        return [float(b)]
    return [a + (b - a) * i / (n - 1) for i in range(n)]


def _mk_klines(closes: list[float], step_ms: int, now_ts: float) -> list[list]:
    """按 close 序列生成 [ts,o,h,l,c,vol]；尾根 = 当前桶（未收盘）。"""
    bucket = int(now_ts * 1000) // step_ms * step_ms
    n = len(closes)
    out = []
    for i, c in enumerate(closes):
        ts = bucket - (n - 1 - i) * step_ms
        o = closes[i - 1] if i > 0 else c
        out.append([float(ts), float(o), float(max(o, c) + 1),
                    float(min(o, c) - 1), float(c), 100.0])
    return out


def _up_closes(broken: bool) -> list[float]:
    """上升结构合成序列：L100(60) → H125(89) → L110(119) → H140(149) → 尾段。

    broken=False：回落到 ~132（始终守住 L110）→ 声音有效；
    broken=True ：跌到 ~90（收盘跌破 L110 未收回）→ 结构破位。
    共 260 根（259 已收盘 ≥ 200 数据门）。
    """
    tail = _ramp(140, 90 if broken else 132, 111)[1:]
    return (
        _ramp(130, 100, 61)          # bar 0..60，低点 100
        + _ramp(100, 125, 30)[1:]    # bar 61..89，高点 125
        + _ramp(125, 110, 31)[1:]    # bar 90..119，低点 110
        + _ramp(110, 140, 31)[1:]    # bar 120..149，高点 140
        + tail                       # bar 150..259
    )


def _fake_htf(dir_: int = 1, label: str = "多头排列", state: str = "aligned_bull"):
    return lambda klines: {
        "state": state, "state_label": label, "dir": dir_, "macro_aligned": True,
        "age_bars": 8, "macro_cross_age": 20, "div": None, "detail": "stub",
    }


def _wire_market(monkeypatch, now_ts, btc_1h, eth_1h, btc_4h=None, eth_4h=None, htf=None):
    """取数替身：按 (symbol, interval) 返回合成 K 线；4h 默认 160 根平线。"""
    flat4h = _mk_klines(_ramp(100, 100, 160), _H4, now_ts)
    klines = {
        ("BTCUSDT", "1h"): btc_1h,
        ("ETHUSDT", "1h"): eth_1h,
        ("BTCUSDT", "4h"): btc_4h if btc_4h is not None else flat4h,
        ("ETHUSDT", "4h"): eth_4h if eth_4h is not None else flat4h,
    }

    class _Pool:
        def get_klines(self, symbol, interval="1h", limit=240, **kw):
            return klines[(symbol, interval)]

    monkeypatch.setattr(sw, "ExchangePool", _Pool)
    monkeypatch.setattr(sw, "analyze_htf_background", htf or _fake_htf())


# ── 1. 调度注册 ───────────────────────────────────────────────────────


def test_beat_registered_at_minute_zero():
    """structure-watch 注册在 beat：task 名正确、整点（分=0）触发，误差门 30s 由任务自监控"""
    from app.celery_app import celery_app

    entry = celery_app.conf.beat_schedule["structure-watch"]
    assert entry["task"] == "app.tasks.structure_watch.run_structure_watch_task"
    assert int(entry["schedule"]._orig_minute) == 0
    from app.tasks.structure_watch import _LAG_LIMIT_SEC
    assert _LAG_LIMIT_SEC == 30


# ── 2. 正常静默 + detail/快照字段对齐 ─────────────────────────────────


def test_clean_structure_silent(wire_sessions, monkeypatch, sent, now_ts):
    """上升结构未破 + 4h 多头：无异常、不推送，detail 字段与大盘条快照消费对齐"""
    clean = _mk_klines(_up_closes(broken=False), _H1, now_ts)
    _wire_market(monkeypatch, now_ts, clean, clean)

    out = sw.run_structure_watch_task()

    assert out["signature"] == "" and out["anomaly_count"] == 0
    assert out["notified"] is False and out["notify_mode"] == "silent"
    assert sent["cards"] == [] and sent["texts"] == []

    rec = _latest(wire_sessions)
    assert rec.task_name == "技术结构检测" and rec.task_type == "review"
    assert rec.status == "completed" and rec.finished_at is not None
    assert "无异常" in rec.summary and "飞书未推送" in rec.summary

    d = rec.detail
    assert d["anomaly_signature"] == "" and d["anomalies"] == []
    assert d["swing_order"] == 3 and d["lag_seconds"] >= 0
    btc = d["symbols"]["BTCUSDT"]
    assert btc["structure"]["trend"] == "up"
    assert btc["structure"]["valid"] is True and btc["structure"]["anchor"] == 110.0
    assert btc["structure"]["label_high"] == "HH" and btc["structure"]["label_low"] == "HL"
    assert btc["key_levels"]["support"] == 110.0 and btc["key_levels"]["resistance"] == 140.0
    assert btc["htf"]["state_label"] == "多头排列"
    assert btc["integrity"] == {"k1h_gaps": 0, "k4h_gaps": 0, "k1h_closed": 259, "k4h_closed": 159}
    assert btc["error"] is None

    # 顶部大盘条快照：消费字段必须能从 detail 映射出来（防两侧漂移）
    from app.services import market_data

    snap = market_data.get_structure_snapshot()
    assert snap is not None and set(snap) == {"BTCUSDT", "ETHUSDT"}
    item = snap["BTCUSDT"]
    assert item["trend_label"] == "上涨结构" and item["broken"] is False
    assert item["support"] == 110.0 and item["support_dist_pct"] is not None
    assert item["resistance"] == 140.0
    assert item["htf_label"] == "多头排列" and item["anomaly_labels"] == []
    assert item["updated_at"] is not None


# ── 3. 结构破位告警 ───────────────────────────────────────────────────


def test_break_alert_pushed(wire_sessions, monkeypatch, sent, now_ts):
    """上升结构收盘跌破摆动低点且未收回：break_down 告警卡（red）推送一次"""
    broken = _mk_klines(_up_closes(broken=True), _H1, now_ts)
    clean = _mk_klines(_up_closes(broken=False), _H1, now_ts)
    _wire_market(monkeypatch, now_ts, broken, clean)

    out = sw.run_structure_watch_task()

    assert "BTCUSDT:break_down" in out["signature"]
    assert out["notify_mode"] == "alert" and out["notified"] is True
    assert len(sent["cards"]) == 1
    assert sent["cards"][0]["header"]["template"] == "red"
    assert "技术结构异常" in sent["cards"][0]["header"]["title"]["content"]

    rec = _latest(wire_sessions)
    assert "结构破位" in rec.summary and "飞书已推送" in rec.summary
    assert rec.detail["notify_mode"] == "alert" and rec.detail["notified"] is True
    assert any("结构破位" in m and "110" in m for m in rec.detail["anomalies"])
    btc = rec.detail["symbols"]["BTCUSDT"]
    assert btc["structure"]["valid"] is False
    assert "break_down" in btc["anomalies"]


# ── 4. 同签名去重 ─────────────────────────────────────────────────────


def test_dedup_same_signature_no_push(wire_sessions, monkeypatch, sent, now_ts):
    """上轮已完成同一签名：持续异常不重复推送（notify_mode=dedup）"""
    _seed_prev(wire_sessions, "BTCUSDT:break_down")
    broken = _mk_klines(_up_closes(broken=True), _H1, now_ts)
    clean = _mk_klines(_up_closes(broken=False), _H1, now_ts)
    _wire_market(monkeypatch, now_ts, broken, clean)

    out = sw.run_structure_watch_task()

    assert out["signature"] == "BTCUSDT:break_down"
    assert out["notify_mode"] == "dedup" and out["notified"] is False
    assert sent["cards"] == [] and sent["texts"] == []
    rec = _latest(wire_sessions)
    assert rec.detail["notify_mode"] == "dedup" and rec.detail["notified"] is False


# ── 5. 异常恢复通知 ───────────────────────────────────────────────────


def test_recover_notifies(wire_sessions, monkeypatch, sent, now_ts):
    """上轮有异常、本轮无异常：推送恢复卡（green）+ summary 标记已推送"""
    _seed_prev(wire_sessions, "BTCUSDT:break_down")
    clean = _mk_klines(_up_closes(broken=False), _H1, now_ts)
    _wire_market(monkeypatch, now_ts, clean, clean)

    out = sw.run_structure_watch_task()

    assert out["signature"] == "" and out["notify_mode"] == "recover"
    assert out["notified"] is True
    assert len(sent["cards"]) == 1
    card = sent["cards"][0]
    assert card["header"]["template"] == "green"
    assert "恢复" in card["header"]["title"]["content"]
    rec = _latest(wire_sessions)
    assert "无异常" in rec.summary and "飞书已推送恢复" in rec.summary


# ── 6. 周期冲突 + 数据异常 ────────────────────────────────────────────


def test_conflict_and_data_anomaly(wire_sessions, monkeypatch, sent, now_ts):
    """1h up vs 4h 空头 → conflict；4h 已收盘 99 根 → data_4h_short（四类异常标记）"""
    clean = _mk_klines(_up_closes(broken=False), _H1, now_ts)
    eth_4h_short = _mk_klines(_ramp(100, 100, 100), _H4, now_ts)
    _wire_market(monkeypatch, now_ts, clean, clean, eth_4h=eth_4h_short,
                 htf=_fake_htf(dir_=-1, label="空头排列", state="aligned_bear"))

    out = sw.run_structure_watch_task()

    assert "BTCUSDT:conflict" in out["signature"]
    assert "ETHUSDT:data_4h_short" in out["signature"]
    assert out["notify_mode"] == "alert"
    rec = _latest(wire_sessions)
    msgs = rec.detail["anomalies"]
    assert any("周期冲突" in m for m in msgs)
    assert any("4h 已收盘 99 根" in m for m in msgs)
    assert rec.detail["symbols"]["ETHUSDT"]["htf"] is None


# ── 7. 触发延迟标记 ───────────────────────────────────────────────────


def test_execution_lag_marked(wire_sessions, monkeypatch, sent, now_ts):
    """整点后 45s 才执行（>30s）：task:lag 执行异常标记 + summary 反映"""
    fixed = datetime.now(_TZ_BJ).replace(minute=0, second=45, microsecond=0)
    monkeypatch.setattr(sw, "_now_bj", lambda: fixed)
    clean = _mk_klines(_up_closes(broken=False), _H1, now_ts)
    _wire_market(monkeypatch, now_ts, clean, clean)

    out = sw.run_structure_watch_task()

    assert "task:lag" in out["signature"]
    rec = _latest(wire_sessions)
    assert rec.detail["lag_seconds"] >= 30
    assert any("触发延迟" in m for m in rec.detail["anomalies"])
    assert out["notify_mode"] == "alert"


# ── 8. 摆动标注只用已收盘口径 ─────────────────────────────────────────


def test_swing_labels_closed_only(wire_sessions, monkeypatch, sent, now_ts):
    """标注必须与 classify_structure 同口径（只用已收盘 K 线）：本数据尾根
    （未收盘）可确认 bar-3 处假摆动高点 145——含尾根口径得 HH（145 vs 120），
    已收盘口径为 LH（120 vs 前高 140）；低点两口径同为 HL（112 vs 110）。"""
    closes = _up_closes(broken=False)[:247] + [
        112, 116, 118, 120, 118, 116, 113, 112, 112, 145, 130, 120, 112,
    ]
    k1 = _mk_klines(closes, _H1, now_ts)
    _wire_market(monkeypatch, now_ts, k1, k1)

    sw.run_structure_watch_task()

    st = _latest(wire_sessions).detail["symbols"]["BTCUSDT"]["structure"]
    assert st["label_high"] == "LH"
    assert st["label_low"] == "HL"


# ── 9. _stale / _gaps 边界 ────────────────────────────────────────────


def test_stale_and_gaps_edges(now_ts):
    """_stale：空数组返回 0 不炸（旧实现 IndexError）；滞后 1 根内视为新鲜、
    ≥2 根报落后数；_gaps：空/单根为 0，非整桶间隔计 1 处。"""
    bucket = int(now_ts * 1000) // _H1 * _H1

    def bar(off: int) -> list:
        return [float(bucket - off * _H1), 0, 0, 0, 100, 0]

    assert sw._stale([], now_ts, _H1) == 0
    assert sw._gaps([], _H1) == 0
    assert sw._stale([bar(0)], now_ts, _H1) == 0
    assert sw._stale([bar(1)], now_ts, _H1) == 0  # 整点刚过：新桶可能尚未开出
    assert sw._stale([bar(2)], now_ts, _H1) == 2
    assert sw._gaps([bar(2), bar(1), bar(0)], _H1) == 0
    assert sw._gaps([bar(2), bar(0)], _H1) == 1