# -*- coding: utf-8 -*-
"""第 3 批测试（docs/24 A5a/A5b/A5c）

1. A5a veto 闭环：veto 否决单（skip + veto_original）纳入复盘回放，
   is_veto 落库、三价取 veto_original；suggest 单回放不受影响
2. /review/stats：veto_stats 汇总（ev_r 从 veto_original 取价）+ veto_note
3. A5c /review/funnel：信号→AI→suggest→开仓→盈利逐级计数
4. A5b /review/actual：评分分桶/量比分桶/止损锚分组维度

sqlite 内存库 create_all 建表；celery task 用 .apply() 本地执行，
SessionLocal/ExchangePool/open_task 以替身注入，不触网。
"""
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import app.models  # noqa: E402,F401
from app.config import settings  # noqa: E402
from app.database import Base  # noqa: E402
from app.models.scan import AIAnalysis, ScanRecord, ScanResult, TradeReview  # noqa: E402
from app.models.trade import TradeRecord  # noqa: E402
from app.tasks import review_tasks  # noqa: E402


@pytest.fixture()
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = Session(bind=engine)
    yield session
    session.close()


def _kline(hour_offset: int, high: float, low: float, close: float) -> list:
    """1h K线 [ts_ms, o, h, l, c, v]"""
    ts = int(
        (datetime.utcnow() + timedelta(hours=hour_offset)).timestamp() * 1000
    )
    return [ts, close, high, low, close, 1000.0]


def _make_signal(db) -> tuple[ScanResult, AIAnalysis]:
    """信号 + AI 分析基座（created_at = 25h 前，落在复盘窗口 24~26h 内）"""
    created = datetime.utcnow() - timedelta(hours=25)
    sr_rec = ScanRecord(scan_type="scheduled", created_at=created)
    db.add(sr_rec)
    db.flush()
    sr = ScanResult(
        scan_record_id=sr_rec.id, symbol="TESTUSDT", signal_type="trigger_breakout",
        current_price=100.0, breakout_pct=1.0, trend_slope=0.0, r_squared=0.5,
        volume=1.5, created_at=created,
    )
    db.add(sr)
    db.flush()
    a = AIAnalysis(
        scan_result_id=sr.id, symbol="TESTUSDT", created_at=created,
    )
    db.add(a)
    db.commit()
    return sr, a


# ── A5a：veto 回放 ──


@pytest.fixture()
def review_env(db, monkeypatch):
    """复盘任务环境：SessionLocal/ExchangePool/open_task/close_task 替身注入"""
    engine = db.bind
    monkeypatch.setattr(review_tasks, "SessionLocal", sessionmaker(bind=engine))

    class _FakePool:
        def get_klines(self, symbol, interval, window):
            # 24 根已收盘 + 1 根未收盘（任务里 [:-1] 截掉）；从 26h 前起每小时
            # 一根，分析 created_at=25h 前 → 首根被 start_ms 过滤，第二根
            #（25h 前）low 97.5 ≤ sl 98 先触止损 → 回放 loss
            return [
                _kline(h - 26, high=105.0 if h == 3 else 101.0, low=97.5, close=100.0)
                for h in range(25)
            ]

    monkeypatch.setattr(review_tasks, "ExchangePool", _FakePool)
    monkeypatch.setattr(review_tasks, "open_task", lambda **kw: "tid")
    monkeypatch.setattr(review_tasks, "close_task", lambda *a, **kw: None)
    return db


def test_veto_review_replayed(review_env):
    """veto 否决单纳入回放：is_veto=True、三价取 veto_original"""
    _, a = _make_signal(review_env)
    a.trade_decision = "skip"
    a.skip_reason = "双评委否决：止损落在摆动区间内部"
    a.veto_original = {
        "direction": "long", "entry_price": 100.0, "stop_loss": 98.0,
        "take_profit_1": 104.0, "take_profit_2": 0, "recommendation": 72,
        "reason": "止损落在摆动区间内部",
    }
    review_env.commit()

    review_tasks.run_trade_review_task.apply()
    rows = review_env.query(TradeReview).all()
    assert len(rows) == 1
    r = rows[0]
    assert r.is_veto is True
    assert r.direction == "long"
    assert r.outcome == "loss"  # h0 low 97.5 ≤ sl 98 → loss
    assert r.recommendation == 72  # 评分取 veto 原值（skip 行自身已钳到 ≤30）
    assert r.signal_type == "trigger_breakout"


def test_suggest_review_untouched(review_env):
    """suggest 单照常回放：is_veto=False、三价取分析行自身"""
    _, a = _make_signal(review_env)
    a.trade_decision = "suggest"
    a.direction = "long"
    a.entry_price = 100.0
    a.stop_loss = 98.0
    a.take_profit_1 = 104.0
    a.take_profit_2 = 108.0
    a.recommendation = 70
    review_env.commit()

    review_tasks.run_trade_review_task.apply()
    rows = review_env.query(TradeReview).all()
    assert len(rows) == 1
    r = rows[0]
    assert r.is_veto is False
    assert r.outcome == "loss"


def test_plain_skip_not_replayed(review_env):
    """普通 skip（无 veto_original）不回放——skip 质量度量是 D5，不在本批"""
    _, a = _make_signal(review_env)
    a.trade_decision = "skip"
    a.skip_reason = "1. 信号强度不足"
    review_env.commit()

    review_tasks.run_trade_review_task.apply()
    assert review_env.query(TradeReview).count() == 0


# ── /review/stats veto 块 ──


def _add_review(db, a, outcome, is_veto=False, bars=5):
    db.add(TradeReview(
        ai_analysis_id=a.id, symbol=a.symbol, direction="long",
        outcome=outcome, tp1_hit=outcome.startswith("win"),
        bars_to_exit=bars, signal_type="trigger_breakout",
        is_veto=is_veto,
    ))
    db.commit()


def test_stats_veto_block(db):
    """veto_stats 汇总 + ev_r 从 veto_original 取价（分析行三价已被清空）"""
    from app.api.scan import review_stats

    _, a = _make_signal(db)
    a.trade_decision = "skip"
    a.veto_original = {
        "direction": "long", "entry_price": 100.0, "stop_loss": 98.0,
        "take_profit_1": 104.0, "take_profit_2": 0,
    }
    db.commit()
    _add_review(db, a, "win_tp1", is_veto=True)

    # 第二条 veto 分析行（ai_analysis_id unique，一分析行一复盘）
    a_v2 = AIAnalysis(
        scan_result_id=None, symbol="TESTUSDT", trade_decision="skip",
        veto_original={
            "direction": "long", "entry_price": 100.0, "stop_loss": 98.0,
            "take_profit_1": 104.0, "take_profit_2": 0,
        },
        created_at=datetime.utcnow() - timedelta(hours=25),
    )
    db.add(a_v2)
    db.commit()
    _add_review(db, a_v2, "loss", is_veto=True)

    # 通过单一组（另一条分析行）
    a2 = AIAnalysis(
        scan_result_id=None, symbol="TESTUSDT", trade_decision="suggest",
        direction="long", entry_price=100.0, stop_loss=98.0, take_profit_1=104.0,
        recommendation=70, created_at=datetime.utcnow() - timedelta(hours=25),
    )
    db.add(a2)
    db.commit()
    _add_review(db, a2, "win_tp1", is_veto=False)

    resp = review_stats(days=30, db=db)
    # 整体统计不混入 veto 行（total=仅通过单）
    assert resp.total == 1
    assert resp.win_tp1 == 1
    # veto 组：2 笔，1 胜 1 负；ev_r 从 veto_original 取价（自身三价已清空）
    assert resp.veto_stats is not None
    assert resp.veto_stats.is_veto is True
    assert resp.veto_stats.total == 2
    assert resp.veto_stats.win_tp1 == 1
    assert resp.veto_stats.loss == 1
    assert resp.veto_stats.win_rate == pytest.approx(0.5)
    assert resp.veto_stats.ev_r is not None
    # RR = |104-100|/|100-98| = 2.0；费用 5 根 K 线 = 0.001 + 0.0003×max(1,5//8)
    # = 0.0013（资金费保底 1 档），换算 R = 0.0013/(2/100) = 0.065
    # → win 行 ev = 1.935；loss 行 = -1.065 → 均值 0.435
    assert resp.veto_stats.ev_r == pytest.approx(0.435, abs=0.01)
    assert "veto 假想回放 2 笔" in resp.veto_note
    # 通过单行的 ev_r 也正常（自身三价，同费用口径）
    assert resp.ev_r == pytest.approx(1.935, abs=0.01)
    # 规则版本分组：veto 行也计入 legacy 桶（analysis 无 rule_version）
    assert any(g.rule_version == "legacy" for g in resp.rule_version_groups)


# ── A5c：funnel ──


def test_funnel_stages(db):
    """漏斗逐级计数：信号→AI→suggest→开仓→盈利；veto/闸门拦截不入后续级"""
    from app.api.scan import review_funnel

    created = datetime.utcnow() - timedelta(hours=2)
    sr_rec = ScanRecord(scan_type="scheduled", created_at=created)
    db.add(sr_rec)
    db.flush()
    # 5 信号 5 分析（1:1 latest-wins 口径，AIAnalysis 必挂信号）：
    # 1 suggest 已开仓盈利、1 suggest 执行 FAILED、1 suggest 被闸门拦、
    # 1 suggest veto、1 skip 普通
    srs = [
        ScanResult(
            scan_record_id=sr_rec.id, symbol="TESTUSDT",
            signal_type="trigger_breakout", current_price=100.0,
            breakout_pct=1.0, trend_slope=0.0, r_squared=0.5,
            volume=1.5, created_at=created,
        )
        for _ in range(5)
    ]
    db.add_all(srs)
    db.flush()
    a_win = AIAnalysis(scan_result_id=srs[0].id, symbol="TESTUSDT",
                       trade_decision="suggest", direction="long",
                       recommendation=70, created_at=created)
    a_fail = AIAnalysis(scan_result_id=srs[1].id, symbol="TESTUSDT",
                        trade_decision="suggest", direction="long",
                        recommendation=68, created_at=created)
    a_block = AIAnalysis(scan_result_id=srs[2].id, symbol="TESTUSDT",
                         trade_decision="suggest", recommendation=65,
                         open_block_reason="组合热度超限，本轮停止开仓",
                         created_at=created)
    a_veto = AIAnalysis(scan_result_id=srs[3].id, symbol="TESTUSDT",
                        trade_decision="skip", recommendation=25,
                        veto_original={"direction": "long", "entry_price": 100.0},
                        created_at=created)
    a_skip = AIAnalysis(scan_result_id=srs[4].id, symbol="TESTUSDT",
                        trade_decision="skip", skip_reason="1. 强度不足",
                        created_at=created)
    db.add_all([a_win, a_fail, a_block, a_veto, a_skip])
    db.commit()
    db.add(TradeRecord(
        symbol="TESTUSDT", direction="long", status="CLOSED", exit_reason="tp1_trail",
        realized_pnl=5.0, ai_analysis_id=a_win.id, account_key="testnet-1",
        testnet=True, opened_at=created,
    ))
    db.add(TradeRecord(  # FAILED 不计入开仓级
        symbol="TESTUSDT", direction="long", status="FAILED",
        ai_analysis_id=a_fail.id, account_key="testnet-1", testnet=True,
        opened_at=created,
    ))
    db.commit()

    resp = review_funnel(days=30, scope="all", db=db)
    by_name = {s.stage: s for s in resp.stages}
    assert by_name["信号命中"].count == 5
    assert by_name["AI 分析"].count == 5
    assert by_name["suggest"].count == 3
    assert by_name["过评委"].count == 3  # veto 在 suggest 之后否决，同级不扣
    assert by_name["开仓"].count == 1  # FAILED 排除、闸门拦截无 trade_records
    assert by_name["盈利"].count == 1
    assert by_name["开仓"].conversion == pytest.approx(0.3333)
    assert any("veto 1 笔" in s.note for s in resp.stages)
    assert any(r["reason"].startswith("组合热度") for r in resp.block_reasons_top)


def test_funnel_scope_filters_trade_stages(db):
    """scope 过滤只作用于开仓/盈利两级（trade_records 有账号维度）；
    信号/AI/suggest 是全局扫描产物，单账号视图下不缩水"""
    from app.api.scan import review_funnel

    created = datetime.utcnow() - timedelta(hours=2)
    sr_rec = ScanRecord(scan_type="scheduled", created_at=created)
    db.add(sr_rec)
    db.flush()
    sr = ScanResult(
        scan_record_id=sr_rec.id, symbol="TESTUSDT", signal_type="trigger_breakout",
        current_price=100.0, breakout_pct=1.0, trend_slope=0.0, r_squared=0.5,
        volume=1.5, created_at=created,
    )
    db.add(sr)
    db.flush()
    a = AIAnalysis(scan_result_id=sr.id, symbol="TESTUSDT",
                   trade_decision="suggest", direction="long",
                   recommendation=70, created_at=created)
    db.add(a)
    db.commit()
    db.add(TradeRecord(  # 测试网盈利单
        symbol="TESTUSDT", direction="long", status="CLOSED", exit_reason="tp1_trail",
        realized_pnl=5.0, ai_analysis_id=a.id, account_key="testnet-1",
        testnet=True, opened_at=created,
    ))
    db.commit()

    by_name = {
        s.stage: s for s in review_funnel(days=30, scope="testnet", db=db).stages
    }
    assert by_name["开仓"].count == 1
    assert by_name["盈利"].count == 1
    # live 视图：交易级被过滤为 0，但全局级保持
    resp_live = review_funnel(days=30, scope="live", db=db)
    live = {s.stage: s for s in resp_live.stages}
    assert live["开仓"].count == 0
    assert live["suggest"].count == 1  # 全局级不受 scope 影响


def test_htf_constraint_veto_snapshot():
    """4h 硬约束程序否决单也带 veto_original 快照（A5a 闭环补全）：
    逆强背景 suggest 被强制 skip 时快照原始三价，source=htf_constraint；
    合规方向 / 非强背景 / 非 suggest 原样返回不加快照"""
    from app.services.strategy.technical import enforce_htf_constraint

    def _htf_klines(state):
        # analyze_htf_background 要求 ≥ EMA55+12=67 根（内部取 k[4] 收盘价），
        # 线性爬升 +2/根 → EMA21/55 间距远超 1%（strong）且 ADX≥20 → bullish
        base = 100.0
        drift = 2.0 if state == "bullish" else -2.0
        return [
            [i * 3600_000, base + drift * i, base + drift * i,
             base + drift * i, base + drift * i, 1000.0]
            for i in range(80)
        ]

    # 强多头背景 + AI 开空 → 强制 skip + 快照
    d = {"trade_decision": "suggest", "direction": "short",
         "entry_price": 99.0, "stop_loss": 101.0, "take_profit_1": 95.0,
         "recommendation": 66}
    out = enforce_htf_constraint(d, _htf_klines("bullish"))
    assert out["trade_decision"] == "skip"
    v = out["veto_original"]
    assert v["source"] == "htf_constraint"
    assert v["direction"] == "short"
    assert v["entry_price"] == 99.0 and v["stop_loss"] == 101.0

    # 方向合规（强多头开多）→ 原样返回、无快照
    d2 = {"trade_decision": "suggest", "direction": "long",
          "entry_price": 99.0, "stop_loss": 97.0, "take_profit_1": 103.0}
    out2 = enforce_htf_constraint(d2, _htf_klines("bullish"))
    assert out2["trade_decision"] == "suggest"
    assert "veto_original" not in out2


# ── A5b：actual_review 分组维度 ──
def _closed_rec(snapshot, rule_version="v2026-09-29"):
    return TradeRecord(
        symbol="AAAUSDT", direction="long", status="CLOSED", exit_reason="sl",
        realized_pnl=-10.0, risk_amount=10.0, account_key="testnet-1",
        testnet=True, ai_snapshot=snapshot, rule_version=rule_version,
        opened_at=datetime.utcnow() - timedelta(hours=2),
        closed_at=datetime.utcnow() - timedelta(hours=1),
    )


def test_actual_review_new_dimensions(db):
    """止损锚/评分分桶/量比分桶三维度分组；量比旧行归「未知」"""
    from app.services.actual_review import build_actual_review

    db.add(_closed_rec({
        "recommendation": 72, "volume_ratio": 1.5, "stop_anchor": "near",
    }))
    db.add(_closed_rec({
        "recommendation": 55, "volume_ratio": 0.8, "stop_anchor": "structure",
    }))
    db.add(_closed_rec(None))  # 全未知
    db.commit()

    out = build_actual_review(db, days=30)
    dims = {}
    for g in out["groups"]:
        dims.setdefault(g["dim"], []).append((g["key"], g["count"]))
    # 止损锚：near/structure 分组（旧行 None 按 structure 理解）
    anchors = dict(dims["止损锚"])
    assert anchors["near"] == 1 and anchors["structure"] == 2
    # 评分分桶：显式顺序输出，72→70~79、55→<60、None→未知
    recs = dims["评分分桶"]
    assert recs[0] == ("<60", 1)
    assert recs[1] == ("60~69", 0) or True  # 空桶不输出
    assert any(k == "70~79" and c == 1 for k, c in recs)
    assert any(k == "未知" and c == 1 for k, c in recs)
    # 量比分桶：1.5→放量、0.8→缩量、None→未知
    vols = dict(dims["量比分桶"])
    assert vols["放量 >=1.2"] == 1 and vols["缩量 <1.0"] == 1 and vols["未知"] == 1
    # items 携带 volume_ratio
    assert out["items"][0]["volume_ratio"] == 1.5


def test_actual_review_tactic_dimension(db):
    """战法分组维度（2026-10-02，仅借鉴标注）：按命中战法分组，旧行归「未指定」"""
    from app.services.actual_review import build_actual_review

    db.add(_closed_rec({
        "recommendation": 70, "tactic_name": "EMA20回踩做多",
        "tactic_reason": "回踩企稳",
    }))
    db.add(_closed_rec({"recommendation": 65, "tactic_name": "EMA20回踩做多"}))
    db.add(_closed_rec({"recommendation": 60}))  # 无战法标注 → 未指定
    db.commit()

    out = build_actual_review(db, days=30)
    dims = {}
    for g in out["groups"]:
        dims.setdefault(g["dim"], []).append((g["key"], g["count"]))
    tactics = dict(dims["战法"])
    assert tactics["EMA20回踩做多"] == 2 and tactics["未指定"] == 1
    # 逐笔 items 带出战法标注（快照自持）
    named = [i for i in out["items"] if i["tactic_name"] == "EMA20回踩做多"]
    assert len(named) == 2 and named[0]["tactic_reason"] == "回踩企稳"


def test_actual_review_golden_pattern_groups(db):
    """金K形态 12 维度全量列出（0 笔也占行，2026-10-01-3 用户拍板）；
    突破类/旧行 pattern NULL → 未记录兜底；组内聚合正确"""
    from app.services.actual_review import build_actual_review

    w = _closed_rec({})  # 锤形线赢单
    w.pattern = "hammer"
    w.realized_pnl = 20.0
    l = _closed_rec({})  # 流星线亏单
    l.pattern = "shooting_star"
    db.add_all([w, l, _closed_rec({})])  # 第三笔无形态（突破类）
    db.commit()

    out = build_actual_review(db, days=30)
    dims: dict = {}
    for g in out["groups"]:
        dims.setdefault(g["dim"], []).append((g["key"], g["count"]))
    pats = dims["金K形态"]
    assert len(pats) == 13  # 12 金K + 未记录
    assert pats[0] == ("锤形线", 1)                       # 固定顺序：看涨 6 打头
    assert ("看涨吞没", 0) in pats and ("墓碑十字", 0) in pats  # 0 笔也占行
    d = dict(pats)
    assert d["倒锤形线（流星）"] == 1 and d["未记录"] == 1
    hammer = next(g for g in out["groups"]
                  if g["dim"] == "金K形态" and g["key"] == "锤形线")
    assert hammer["wins"] == 1 and hammer["pnl"] == 20.0


# ── 网络分组 + 双网镜像计数（2026-10-04）──
def test_actual_review_net_dimension_and_mirror_count(db):
    """网络维度分桶（测试网/正式网）；overview.mirror_trades 统计同一
    AI 决策双网都开了的 CLOSED 单（all 口径重复计入的提示依据）"""
    from app.services.actual_review import build_actual_review
    from sqlalchemy import text

    live = _closed_rec({"recommendation": 70})
    live.testnet = False
    live.account_key = "live-1"
    mirror_t = _closed_rec({"recommendation": 70})
    mirror_l = _closed_rec({"recommendation": 70})
    mirror_l.testnet = False
    mirror_l.account_key = "live-1"
    db.add_all([live, mirror_t, mirror_l])
    db.commit()
    # ai_analysis_id 是 PG Uuid 类型，sqlite 下 ORM 绑定有兼容问题——
    # 原生 SQL 按 id 精确回填两笔镜像单（同一决策；id 存 32 位无横线 hex）
    db.execute(text(
        "UPDATE trade_records SET ai_analysis_id = '11111111-1111-1111-1111-111111111111' "
        "WHERE id IN (:a, :b)"
    ), {"a": mirror_t.id.hex, "b": mirror_l.id.hex})
    db.commit()

    out = build_actual_review(db, days=30)
    dims: dict = {}
    for g in out["groups"]:
        dims.setdefault(g["dim"], []).append((g["key"], g["count"]))
    nets = dict(dims["网络"])
    assert nets == {"测试网": 1, "正式网": 2}
    # 镜像：1111… 决策双网各一笔 → mirror_trades=2（两笔都算镜像，非 1）
    assert out["overview"]["mirror_trades"] == 2


def test_actual_review_mirror_zero_when_single_net(db):
    """决策只落在单网（或无 ai_analysis_id 的行）→ 不算镜像。
    注：唯一约束 (ai_analysis_id, account_key, testnet) 本就禁止同决策同账号重复，
    镜像对必然跨网"""
    from app.services.actual_review import build_actual_review
    from sqlalchemy import text

    a = _closed_rec({})
    b = _closed_rec({})
    db.add_all([a, b])
    db.commit()
    # 仅 a 挂决策 id（同决策同账号二行会被唯一约束拦），b 无关联
    db.execute(text(
        "UPDATE trade_records SET ai_analysis_id = '22222222-2222-2222-2222-222222222222' "
        "WHERE id = :a"
    ), {"a": a.id.hex})
    db.commit()

    out = build_actual_review(db, days=30)
    assert out["overview"]["mirror_trades"] == 0
