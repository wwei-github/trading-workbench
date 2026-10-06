# -*- coding: utf-8 -*-
"""再入场前置影子（docs/13 #12，2026-10-06）：reentry_precond 单测。

覆盖：
1. compute_reentry_precond：多头结构完好（EMA 排列+ADX≥20+R²≥0.5）→
   structure_ok；震荡盘 → structure_broken；空头对称
2. 续走判定：出场后 24h 内 signed-R MFE ≥ 0 → continued_24h=True，
   hours_to_reentry 按 bar 开盘时刻计；24h 外恢复只记 continued_48h
3. 跳过口径：缺止损快照 / 结构数据不足（<66 根）/ 出场后无路径 → None
4. tag_reentry_backfill：仅选 sl 出场 + 48h 出窗单、无快照跳过、
   异常全吞（boom symbol）、幂等（已打标永不重打）
5. build_report CARD-07 分支：结构桶×续走率、测试网分层不进判定
6. registry：CARD-07 注册、compute 占位恒 None、任务名与 beat 一致

sqlite 内存库 + StaticPool + 替身 pool，不触网。
"""
import sys
import uuid
from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

sys.path.insert(0, __file__.rsplit("/", 2)[0])

import app.models  # noqa: E402,F401
from app.database import Base  # noqa: E402
from app.models.shadow import ShadowMark  # noqa: E402
from app.models.trade import TradeEvent, TradeRecord  # noqa: E402
from app.services import shadow as shadow_mod  # noqa: E402
from app.services.shadow import compute_reentry_precond  # noqa: E402


# ── 替身与数据构造 ────────────────────────────────────────────────────


class _FakeRec:
    """TradeRecord 轻替身：compute_reentry_precond 只读这些属性"""

    def __init__(self, direction="long", entry=100.0, stop=95.0, risk=100.0,
                 realized=-55.0, exit_reason="sl", closed=None):
        self.entry_price = entry
        self.stop_initial = stop
        self.risk_amount = risk
        self.direction = direction
        self.exit_reason = exit_reason
        self.realized_pnl = realized
        self.closed_at = closed or datetime(2026, 10, 4, 10, 30)


def _make_klines(closed_at: datetime, trend: str = "up") -> list[list]:
    """210 根 1h K 线：0~159 结构窗（up=单边上行 / range=震荡）、160 平仓根、
    161~208 出场后 48h（先下探 -1.1R 再回收；对空头对称上探再回落）。

    entry=100、r_unit=5（多头 stop=95）：post 第 i 根 close = 94.5+(i-161)*0.8。
    """
    t0 = int((closed_at - timedelta(hours=160)).replace(tzinfo=None).timestamp() * 1000)
    t0 = t0 // 3_600_000 * 3_600_000
    kls, prev = [], 100.0
    for i in range(209):
        ts = t0 + i * 3_600_000
        if i < 160:
            c = 100.0 + i * 0.10 if trend == "up" else 100.0 + 4 * (i % 2 * 2 - 1)
        elif i == 160:
            c = 116.0 if trend == "up" else 104.0
        else:
            j = i - 161
            c = 94.5 + j * 0.8 if trend == "up" else 105.5 - j * 0.8
        hi, lo = c + 0.3, c - 0.3
        kls.append([ts, prev, hi, lo, c, 100.0])
        prev = c
    return kls


def _assert_shape(v: dict):
    for key in ("bucket", "structure_ok", "ema_ok", "adx", "adx_ok", "r2",
                "r2_ok", "post_path", "mfe_post_r", "continued_24h",
                "continued_48h", "hours_to_reentry", "real_r"):
        assert key in v, f"缺字段 {key}"


# ── 1. 结构判定 ───────────────────────────────────────────────────────


def test_structure_ok_long():
    """多头结构完好：单边上行 pre（ema 排列/ADX/R² 全过）→ structure_ok"""
    rec = _FakeRec()
    v = compute_reentry_precond(_make_klines(rec.closed_at, "up"), rec)
    _assert_shape(v)
    assert v["bucket"] == "structure_ok" and v["structure_ok"] is True
    assert v["ema_ok"] is True and v["adx_ok"] is True and v["r2_ok"] is True
    assert v["ema144_used"] is True  # 160 根已收盘 → e144 可用
    assert v["r_unit"] == 5.0 and v["stop_initial"] == 95.0
    assert v["real_r"] == pytest.approx(-0.55, abs=1e-6)


def test_structure_broken_range():
    """震荡盘：EMA 不排列 / ADX<20 / R² 低 → structure_broken（三件必有挂）"""
    rec = _FakeRec()
    v = compute_reentry_precond(_make_klines(rec.closed_at, "range"), rec)
    assert v["bucket"] == "structure_broken" and v["structure_ok"] is False


def test_structure_ok_short_symmetric():
    """空头对称：下行结构 + r_unit=|entry-stop|=5 + post 回落同向续走"""
    rec = _FakeRec(direction="short", entry=100.0, stop=105.0)
    v = compute_reentry_precond(_make_klines(rec.closed_at, "up"), rec)
    # pre 是上行 → 空头排列不成立；post 上探为不利方向后回落到 entry 下方
    assert v["bucket"] == "structure_broken" and v["ema_ok"] is False
    # post 回落到 entry 之下（signed-R ≥ 0 对空头有利）→ 24h 内续走
    assert v["continued_24h"] is True

    rec_dn = _FakeRec(direction="short", entry=100.0, stop=105.0)
    v2 = compute_reentry_precond(_make_klines_down(rec_dn.closed_at), rec_dn)
    assert v2["bucket"] == "structure_ok" and v2["ema_ok"] is True


def _make_klines_down(closed_at: datetime) -> list[list]:
    """空头结构完好样本：单边下行 pre + post 下探后回升过 entry"""
    kls = _make_klines(closed_at, "up")
    t0 = int((closed_at - timedelta(hours=160)).replace(tzinfo=None).timestamp() * 1000)
    t0 = t0 // 3_600_000 * 3_600_000
    prev = 100.0
    for i, k in enumerate(kls):
        if i < 160:
            c = 100.0 - i * 0.10
        elif i == 160:
            c = 84.0
        else:
            c = 105.5 - (i - 161) * 0.8
        k[1], k[2], k[3], k[4] = prev, c + 0.3, c - 0.3, c
        prev = c
    return kls


# ── 2. 续走判定 ───────────────────────────────────────────────────────


def test_continued_24h_and_hours():
    """post 先下探 -1.1R 再回收：24h 内回到 entry（signed-R≥0）→ 续走；
    hours_to_reentry = 回到 entry 那根的 open 距平仓时刻"""
    rec = _FakeRec()
    v = compute_reentry_precond(_make_klines(rec.closed_at, "up"), rec)
    assert v["continued_24h"] is True and v["continued_48h"] is True
    assert 0 <= v["hours_to_reentry"] <= 24
    assert v["mfe_post_r"] > 0  # 回收超过 entry（0.8R/根 × 若干根）
    assert len(v["post_path"]) == 48
    # 首根 post：c=94.5 → hi=94.8 → r=(94.8-100)/5=-1.04
    assert v["post_path"][0][0] == pytest.approx(-1.04, abs=1e-3)


def test_no_continuation_within_24h():
    """出场后 24h 内未回到 entry（第 30 根才回收）→ continued_24h=False，
    continued_48h=True（48h 窗内到达）"""
    rec = _FakeRec()
    kls = _make_klines(rec.closed_at, "up")
    # post 根 i=161..208，改 161..190 为深水区（<entry），191 起快速回收
    for i in range(161, 191):
        c = 90.0 + (i - 161) * 0.05
        kls[i][2], kls[i][3], kls[i][4] = c + 0.1, c - 0.1, c
    for i in range(191, 209):
        c = 92.0 + (i - 191) * 0.9
        kls[i][2], kls[i][3], kls[i][4] = c + 0.1, c - 0.1, c
    v = compute_reentry_precond(kls, rec)
    assert v["continued_24h"] is False and v["continued_48h"] is True


# ── 3. 跳过口径 ───────────────────────────────────────────────────────


def test_skip_conditions():
    """缺止损快照 / 无出场后路径 / 结构数据不足 → None 不打标"""
    rec = _FakeRec()
    kls = _make_klines(rec.closed_at)
    assert compute_reentry_precond(kls, rec) is not None

    rec_no_sl = _FakeRec(stop=0)
    assert compute_reentry_precond(kls, rec_no_sl) is None

    assert compute_reentry_precond([], rec) is None

    rec_short_hist = _FakeRec()
    short_kls = [k for k in kls if k[0] < int(
        (rec_short_hist.closed_at - timedelta(hours=100)).replace(tzinfo=None).timestamp() * 1000)]
    assert len(short_kls) < 66  # 结构数据不足（ADX/EMA 不可判）
    assert compute_reentry_precond(short_kls, rec_short_hist) is None

    rec_no_post = _FakeRec()
    pre_only = [k for k in kls if k[0] < int(
        (rec_no_post.closed_at - timedelta(hours=1)).replace(tzinfo=None).timestamp() * 1000)]
    assert compute_reentry_precond(pre_only, rec_no_post) is None  # 无出场后路径


# ── 4. tag_reentry_backfill ──────────────────────────────────────────


def _add_open_event(s, trade_id: uuid.UUID, stop_loss: float):
    """生产口径：OPEN 事件 detail.stop_loss 快照（tag_reentry_backfill 从这里解包）"""
    s.add(TradeEvent(
        trade_record_id=trade_id, event_type="OPEN",
        detail={"stop_loss": stop_loss},
    ))


@pytest.fixture()
def shadow_db(monkeypatch):
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    maker = sessionmaker(bind=engine)
    monkeypatch.setattr(shadow_mod, "SessionLocal", maker, raising=False)
    # tag_reentry_backfill 内 SessionLocal 是函数内 import（app.database），一并 patch
    from app import database as db_mod
    monkeypatch.setattr(db_mod, "SessionLocal", maker, raising=False)
    return maker


def _add_closed_sl_trade(s, tid: uuid.UUID, sym: str, closed_at: datetime,
                         exit_reason: str = "sl", with_event: bool = True):
    s.add(TradeRecord(
        id=tid, symbol=sym, direction="long", entry_price=100.0,
        qty=10.0, risk_amount=100.0, stop_loss=95.0, tp1=111.0,
        status="CLOSED", exit_reason=exit_reason, realized_pnl=-55.0,
        opened_at=closed_at - timedelta(hours=160), closed_at=closed_at,
        testnet=False,
    ))
    if with_event:
        _add_open_event(s, tid, 95.0)


class _FakePool:
    """get_klines_range 替身：按 rec 平仓时刻造 210 根窗口 K 线；boom 抛异常"""

    def __init__(self, closed_at: datetime, trend="up", boom_symbols=()):
        self.kls = _make_klines(closed_at, trend)
        self.boom = set(boom_symbols)
        self.calls = []

    def get_klines_range(self, symbol, interval, limit, end_time_ms):
        self.calls.append(symbol)
        if symbol in self.boom:
            raise RuntimeError("exchange boom")
        return self.kls


def test_tag_reentry_backfill_marks_and_idempotent(shadow_db, monkeypatch):
    """sl 出场 + 48h 出窗 → 打标 1 行；已打标单重跑不重复（永不重打口径）"""
    from app.services import exchange_pool as ep_mod
    from app.services.shadow import tag_reentry_backfill

    closed_at = datetime.utcnow() - timedelta(days=3)
    maker = shadow_db
    s = maker()
    tid = uuid.uuid4()
    _add_closed_sl_trade(s, tid, "AAAUSDT", closed_at)
    s.commit()
    s.close()
    monkeypatch.setattr(ep_mod, "ExchangePool",
                        lambda: _FakePool(closed_at))

    n = tag_reentry_backfill()
    assert n == 1

    db = maker()
    marks = db.execute(select(ShadowMark)).scalars().all()
    assert len(marks) == 1
    m = marks[0]
    assert m.feature_key == "reentry_precond" and m.ref_type == "trade_record"
    assert str(m.ref_id) == str(tid)
    assert m.value_json["bucket"] in ("structure_ok", "structure_broken")
    assert m.decision_json["stage"] == "reentry_backfill"
    assert m.decision_json["testnet"] is False
    db.close()

    # 幂等：已打标单进 done 集合，重跑 0 行
    assert tag_reentry_backfill() == 0


def test_tag_reentry_backfill_selection_and_errors(shadow_db, monkeypatch):
    """入选口径与容错：非 sl 不选 / 48h 未出窗不选 / 无快照跳过 /
    K 线拉取炸吞掉不影响其余"""
    from app.services import exchange_pool as ep_mod
    from app.services.shadow import tag_reentry_backfill

    closed_ok = datetime.utcnow() - timedelta(days=3)
    maker = shadow_db
    s = maker()
    t_sl, t_tp, t_fresh, t_nosnap, t_boom = (uuid.uuid4() for _ in range(5))
    _add_closed_sl_trade(s, t_sl, "OKUSDT", closed_ok)
    _add_closed_sl_trade(s, t_tp, "TPUSDT", closed_ok, exit_reason="tp1_trail")
    _add_closed_sl_trade(s, t_fresh, "FRESHUSDT",
                         datetime.utcnow() - timedelta(hours=10))  # 48h 未出窗
    _add_closed_sl_trade(s, t_nosnap, "NOSNAPUSDT", closed_ok, with_event=False)
    _add_closed_sl_trade(s, t_boom, "BOOMUSDT", closed_ok)
    s.commit()
    s.close()
    monkeypatch.setattr(ep_mod, "ExchangePool",
                        lambda: _FakePool(closed_ok, boom_symbols=("BOOMUSDT",)))

    assert tag_reentry_backfill() == 1  # 仅 OKUSDT（其余口径不合/异常吞）

    db = maker()
    feats = {m.ref_id for m in db.execute(select(ShadowMark)).scalars()}
    assert t_sl in feats and t_boom not in feats and t_nosnap not in feats
    db.close()


# ── 5. build_report CARD-07 分支 ─────────────────────────────────────


def test_build_report_reentry(shadow_db):
    """结构桶×续走率统计：正式网主判定（n=3，续走 2/3），测试网分层对照；
    n<30 桶标 insufficient；达标判定 = 正式网 n ≥ min_sample(60)"""
    maker = shadow_db
    s = maker()

    def _mark(tid, symbol, testnet, bucket, cont):
        s.add(ShadowMark(
            feature_key="reentry_precond", ref_type="trade_record",
            ref_id=tid, symbol=symbol,
            value_json={"bucket": bucket, "continued_24h": cont,
                        "structure_ok": bucket == "structure_ok"},
            decision_json={"stage": "reentry_backfill", "testnet": testnet},
        ))

    for i in range(2):   # 正式网：结构完好且都续走
        _mark(uuid.uuid4(), f"S{i}USDT", False, "structure_ok", True)
    _mark(uuid.uuid4(), "S9USDT", False, "structure_broken", False)   # 正式网破坏桶
    _mark(uuid.uuid4(), "T1USDT", True, "structure_ok", True)         # 测试网对照
    s.commit()
    s.close()

    report = shadow_mod.build_report(maker(), window_days=30)
    rep = next(r for r in report if r["feature"] == "reentry_precond")
    assert rep["card"] == "CARD-07" and rep["min_sample"] == 60
    base = rep["baseline"]
    assert base["n"] == 3 and base["continued"] == 2
    assert base["cont_rate"] == pytest.approx(0.6667, abs=1e-3)
    assert base["structure_ok_n"] == 2
    buckets = {b["bucket"]: b for b in rep["buckets"]}
    assert buckets["structure_ok"]["n"] == 2
    assert buckets["structure_ok"]["cont_rate"] == 1.0
    assert buckets["structure_broken"]["n"] == 1
    assert buckets["structure_broken"]["cont_rate"] == 0.0
    assert all(b.get("insufficient") for b in buckets.values())  # n<30
    tbn = rep["testnet_baseline"]
    assert tbn["n"] == 1 and tbn["cont_rate"] == 1.0
    assert tbn["buckets"] == {"structure_ok": 1}
    assert rep["sufficient"] is False  # 3 < 60

    # 空特征占位（无标记的特征照常出现在报告里）
    assert any(r["feature"] == "cvd_div" for r in report)


# ── 6. registry 与任务接线 ────────────────────────────────────────────


def test_registry_and_task_wiring():
    """CARD-07 注册元数据（compute 占位恒 None）；celery beat 任务名与
    @task 装饰器名一致（打标每天 08:25 自动跑）"""
    feat = shadow_mod.SHADOW_FEATURES["reentry_precond"]
    assert feat.card == "CARD-07" and feat.min_sample == 60
    assert feat.compute([]) is None

    import re

    celery_src = open(shadow_mod.__file__.replace(
        "services/shadow.py", "celery_app.py"), encoding="utf-8").read()
    task_src = open(shadow_mod.__file__.replace(
        "services/shadow.py", "tasks/shadow_tasks.py"), encoding="utf-8").read()
    beat = set(re.findall(r'"app\.tasks\.shadow_tasks\.(\w+)"', celery_src))
    decl = set(re.findall(r'name="app\.tasks\.shadow_tasks\.(\w+)"', task_src))
    assert "run_reentry_backfill_task" in beat and beat == decl
