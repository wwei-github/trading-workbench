# -*- coding: utf-8 -*-
"""BTC-β 加权组合热度（docs/13 #1）+ 影子模式（docs/14）单测。

覆盖：
1. get_beta：对 BTC 日收益回归（1h K 每 24 根采样）、BTC 恒 1.0、样本不足/
   零方差/负 β/查询失败 → None（调用方按 1.0 保守）、24h 进程内缓存命中
2. 热度闸门 β 加权：β>1 放大拦截（原口径放行的组合被拦下）、β<1 折价放行、
   旧行/缺失 β=None 回落 1.0（原口径等价回归）
3. 影子特征 compute 分桶：cvd_div 三桶 + 样本不足、r2_slope 三桶
4. scanner._save_results 打标：仅定时扫描、value_json/decision_json 快照、
   uq_shadow_mark 唯一约束（幂等基座）、影子落库失败不影响扫描结果本身
5. build_report 归因链 join（win/pending/no_analysis/loss）、分桶统计、
   达标判定、窗口过滤、无标记空态、scan 宿主卡 avg_r 读数（预注册 R 口径）
6. 异常隔离：build_report 抛错时日报附录降级为空、行格式符合预期

sqlite 内存库 + 替身 client，不触网。
"""
import sys
import uuid
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pytest
from sqlalchemy import create_engine, insert, select
from sqlalchemy.orm import Session, sessionmaker

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import app.models  # noqa: E402,F401  确保全部模型注册进 Base.metadata
from app.config import settings  # noqa: E402
from app.database import Base  # noqa: E402
from app.models.scan import AIAnalysis, ScanResult, TradeReview  # noqa: E402
from app.models.shadow import ShadowMark  # noqa: E402
from app.models.system_config import SystemConfig  # noqa: E402
from app.models.trade import TradeRecord  # noqa: E402
from app.services import market_data  # noqa: E402
from app.services.market_data import get_beta  # noqa: E402
from app.services.scanner import Scanner  # noqa: E402
from app.services.shadow import (  # noqa: E402
    SHADOW_FEATURES,
    build_report,
    compute_cvd_div,
    compute_r2_slope,
)
from app.services.trade_engine import try_open_for_analysis  # noqa: E402
from app.tasks import shadow_tasks  # noqa: E402


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    """固定配置基线 + 清进程内缓存（β TTL 缓存跨用例污染）"""
    monkeypatch.setattr(settings, "CONSECUTIVE_LOSS_DERATE", True)
    monkeypatch.setattr(settings, "PORTFOLIO_HEAT_CAP", 0.15)
    monkeypatch.setattr(settings, "SAME_DIRECTION_CAP", 6)
    monkeypatch.setattr(settings, "SPREAD_MAX_PCT", 0.003)
    monkeypatch.setattr(settings, "TAKER_FEE_RATE", 0.0005)
    monkeypatch.setattr(settings, "TRADING_MIN_RECOMMENDATION", 60.0)
    monkeypatch.setattr(settings, "TRADING_RISK_PCT", 3.0)
    monkeypatch.setattr(settings, "DAILY_LOSS_STOP_PCT", 9.0)
    monkeypatch.setattr(settings, "DAILY_LOSS_STOP_ENABLED", True)
    monkeypatch.setattr(settings, "NEAR_ANCHOR_RISK_FACTOR", 0.7)
    market_data._cache.clear()
    yield
    market_data._cache.clear()


@pytest.fixture()
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = Session(bind=engine)
    session.add(SystemConfig(id=1))
    session.commit()
    yield session


# ── 1. get_beta：日收益回归 ───────────────────────────────────────────

_BARS = market_data._BETA_BARS  # 745 = 24×30 + 25


def _beta_klines(sampled: list[float]) -> list[list]:
    """1h K 替身：只在采样位（[:-1][::24] → 0,24,…,720）放收盘价，其余占位"""
    kl = [[0.0] * 10 for _ in range(_BARS)]
    for i, c in enumerate(sampled):
        kl[i * 24][4] = c
    kl[-1][4] = sampled[-1]  # 末根未收盘，被 [:-1] 剔除
    return kl


class _BetaClient:
    """get_futures_klines 替身：按 symbol 返回预置 K 线，可注入异常"""

    def __init__(self, sym=None, btc=None, error=False):
        self.sym, self.btc, self.error = sym, btc, error
        self.calls = 0

    def get_futures_klines(self, symbol, interval, bars):
        self.calls += 1
        if self.error:
            raise RuntimeError("网络不可用")
        return self.btc if symbol == "BTCUSDT" else self.sym


def _series_from_returns(rets: np.ndarray, base=100.0) -> list[float]:
    """日收益序列 → 收盘价序列（31 个采样点）"""
    closes = [base]
    for r in rets:
        closes.append(closes[-1] * (1 + r))
    return closes


def test_beta_btc_constant_one():
    """BTCUSDT 恒 1.0（自身回归），不触 client"""
    client = _BetaClient(error=True)
    assert get_beta(client, "BTCUSDT") == 1.0
    assert client.calls == 0


def test_beta_regression_two(monkeypatch):
    """sym 日收益 = 2×BTC 日收益 → β=2（cov/var 线性）"""
    rng = np.random.default_rng(7)
    btc_r = 0.01 * np.sin(np.linspace(0, 3.14, 30)) + rng.normal(0, 0.001, 30)
    btc_c = _series_from_returns(btc_r)
    sym_c = _series_from_returns(2 * btc_r)
    client = _BetaClient(sym=_beta_klines(sym_c), btc=_beta_klines(btc_c))
    beta = get_beta(client, "AAAUSDT")
    assert beta == pytest.approx(2.0, rel=1e-6)


def test_beta_cached_across_calls():
    """24h 进程内缓存：第二次调用不再拉 K 线"""
    rng = np.random.default_rng(3)
    btc_r = 0.008 * np.sin(np.linspace(0, 2.0, 30))
    btc_c = _series_from_returns(btc_r)
    sym_c = _series_from_returns(1.2 * btc_r)
    client = _BetaClient(sym=_beta_klines(sym_c), btc=_beta_klines(btc_c))
    get_beta(client, "BBBUSDT")
    get_beta(client, "BBBUSDT")
    assert client.calls == 2  # 首次 sym+btc 各一次，第二次 0 次


def test_beta_query_failure_none_and_cached():
    """K 线查询失败 → None（保守按 1.0），失败也缓存（TTL 内不重复打网络）"""
    client = _BetaClient(error=True)
    assert get_beta(client, "CCCUSDT") is None
    assert get_beta(client, "CCCUSDT") is None
    assert client.calls == 1


def test_beta_insufficient_sample_none():
    """日采样点 < 15 → None（不产缺失组伪回归）"""
    kl = [[0.0] * 10 for _ in range(200)]
    for i in range(8):  # 8 个采样点
        kl[i * 24][4] = 100 + i
    client = _BetaClient(sym=kl, btc=kl)
    assert get_beta(client, "DDDUSDT") is None


def test_beta_zero_variance_none():
    """BTC 日收益零方差（横盘）→ None"""
    flat = _beta_klines([100.0] * 31)
    up = _beta_klines(_series_from_returns(np.linspace(0.001, 0.002, 30)))
    client = _BetaClient(sym=up, btc=flat)
    assert get_beta(client, "EEEUSDT") is None


def test_beta_negative_none():
    """负 β（与 BTC 反向）→ 回归不可信，按缺失处理"""
    btc_r = 0.01 * np.sin(np.linspace(0, 3.0, 30))
    client = _BetaClient(
        sym=_beta_klines(_series_from_returns(-2 * btc_r)),
        btc=_beta_klines(_series_from_returns(btc_r)),
    )
    assert get_beta(client, "FFFUSDT") is None


# ── 2. 热度闸门 β 加权 ────────────────────────────────────────────────


class _FakeTrader:
    """闸门链替身：wallet 可用；price 抛错 = 流程穿透到 _attempt_open"""

    account_key = "testnet-1"
    testnet = True

    def __init__(self, wallet=1000.0):
        self._wallet = wallet

    def listed_symbols(self):
        return set()

    def positions(self):
        return {}

    def wallet_balance(self):
        return {"wallet": self._wallet, "available": self._wallet * 0.9}

    def price(self, symbol):
        raise RuntimeError("替身不提供价格")


def _running(db, risk=40.0, beta=None):
    rec = TradeRecord(
        symbol=f"RUN{uuid.uuid4().hex[:6]}USDT", direction="long", status="OPENED",
        account_key="testnet-1", testnet=True,
        opened_at=datetime.utcnow(), risk_amount=risk, beta=beta,
    )
    db.add(rec)
    db.commit()
    return rec


def _analysis(db, rec=90):
    a = AIAnalysis(
        symbol="TESTUSDT", trade_decision="suggest", direction="long",
        entry_price=100.0, stop_loss=98.0, take_profit_1=104.0,
        recommendation=rec, created_at=datetime.utcnow(),
    )
    db.add(a)
    db.commit()
    return a


@pytest.fixture()
def gate_env(monkeypatch, db):
    """β替身：get_beta 返回可配值"""
    holder = {"beta": 1.0}
    monkeypatch.setattr(market_data, "get_beta", lambda client, symbol: holder["beta"])
    monkeypatch.setattr(market_data, "get_kline_client", lambda: None)
    return holder


def test_heat_beta_amplify_blocks(db, gate_env):
    """β=1.5 加权拦截：在跑 2×40×1.5 + 本单 30×1.5 = 165 > 150；
    原口径 80 + 30 = 110 ≤ 150 本应放行——β 加权把隐性集中暴露出来"""
    gate_env["beta"] = 1.5
    _running(db, risk=40.0, beta=1.5)
    _running(db, risk=40.0, beta=1.5)
    a = _analysis(db)
    assert try_open_for_analysis(db, _FakeTrader(), a.id) == "blocked"
    assert "组合热度超限（β加权）" in (a.open_block_reason or "")


def test_heat_beta_discount_passes(db, gate_env):
    """β=0.5 折价放行：在跑 4×40×0.5 + 本单 30×0.5 = 95 ≤ 150；
    原口径 190 > 150 会拦——低 β 币对组合热度贡献小，不再被误伤"""
    gate_env["beta"] = 0.5
    for _ in range(4):
        _running(db, risk=40.0, beta=0.5)
    a = _analysis(db)
    assert try_open_for_analysis(db, _FakeTrader(), a.id) == "blocked"
    assert "组合热度" not in (a.open_block_reason or "")
    assert "开仓失败" in (a.open_block_reason or "")  # 穿透到 _attempt_open


def test_heat_beta_missing_rows_orig_caliber(db, gate_env):
    """在跑旧行 β=NULL + 本单 β 缺失 → 全按 1.0 = 原口径（回归：160+30=190>150 拦截）"""
    gate_env["beta"] = None  # get_beta 返回 None → beta_new = 1.0
    for _ in range(4):
        _running(db, risk=40.0, beta=None)
    a = _analysis(db)
    assert try_open_for_analysis(db, _FakeTrader(), a.id) == "blocked"
    assert "组合热度超限（β加权）" in (a.open_block_reason or "")


# ── 3. 影子特征 compute 分桶 ──────────────────────────────────────────


def _cvd_bars(closes, vols, tbs):
    """1h K 替身：index 4=close、5=volume、9=takerBuyBase"""
    assert len(closes) == len(vols) == len(tbs)
    return [[0, 0, 0, 0, c, v, 0, 0, 0, tb] for c, v, tb in zip(closes, vols, tbs)]


def test_cvd_div_buckets():
    up = [100 * (1 + 0.0015 * i) for i in range(26)]      # 24 根涨幅 >1%
    down = list(reversed(up))
    flat = [100.0] * 26
    vols = [100.0] * 26

    bear = compute_cvd_div(_cvd_bars(up, vols, np.linspace(80, 30, 26)))
    assert bear["bucket"] == "diverge_bear"               # 价升 + CVD 走低
    bull = compute_cvd_div(_cvd_bars(down, vols, np.linspace(30, 80, 26)))
    assert bull["bucket"] == "diverge_bull"               # 价跌 + CVD 走高
    confirm = compute_cvd_div(_cvd_bars(up, vols, np.linspace(30, 80, 26)))
    assert confirm["bucket"] == "confirm"                 # 价量同向
    weak = compute_cvd_div(_cvd_bars(flat, vols, np.linspace(80, 30, 26)))
    assert weak["bucket"] == "confirm"                    # 价未动（<±1%）不算背离


def test_cvd_div_insufficient_none():
    assert compute_cvd_div(_cvd_bars([100.0] * 25, [100.0] * 25, [60.0] * 25)) is None


def test_compute_outputs_json_serializable():
    """compute 输出直接进 value_json（SQLAlchemy JSON 列 = json.dumps）：
    np.float64 是 float 子类能过，但 np.bool_/np.float32/ndarray 会 TypeError
    → 打标被 except 吞掉静默全灭——此测试守住未来口径改动不引入非纯类型"""
    import json

    up = [100 * (1 + 0.0015 * i) for i in range(26)]
    for value in (
        compute_cvd_div(_cvd_bars(up, [100.0] * 26, np.linspace(80, 30, 26))),
        compute_r2_slope(_cvd_bars(up, [100.0] * 26, np.linspace(30, 80, 26))),
    ):
        assert value is not None
        json.dumps(value)  # 不抛 TypeError


# ── 4. scanner._save_results 打标 ─────────────────────────────────────


def _hit(symbol, shadow=None):
    return {
        "symbol": symbol, "signal_type": "trigger_breakout", "current_price": 100.0,
        "breakout_pct": 0.01, "trend_slope": 0.5, "r_squared": 0.8,
        "volume_24h": 1e6, "volume": 100.0, "volume_type": "放量",
        **({"shadow": shadow} if shadow is not None else {}),
    }


@pytest.fixture()
def scanner_db(monkeypatch):
    """内存库 + scanner.SessionLocal 指向同一 engine（_save_results 独立会话）"""
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    maker = Session(bind=engine)
    monkeypatch.setattr("app.services.scanner.SessionLocal", sessionmaker(bind=engine))
    yield maker
    maker.close()


def test_save_results_tags_scheduled(scanner_db):
    """定时扫描命中 → shadow 落 shadow_marks，value/decision 快照齐全"""
    shadow = {"cvd_div": {"bucket": "diverge_bear", "price_chg_pct": 2.1, "cvd_chg_pct": -3.0}}
    ids = Scanner._save_results(uuid.uuid4(), [_hit("AAAUSDT", shadow)], scan_type="scheduled")
    row = scanner_db.execute(select(ShadowMark)).scalar_one()
    assert row.feature_key == "cvd_div"
    assert row.ref_type == "scan_result"
    assert row.ref_id == ids["AAAUSDT"]
    assert row.symbol == "AAAUSDT"
    assert row.value_json["bucket"] == "diverge_bear"
    assert row.decision_json == {"stage": "scan_hit", "scan_type": "scheduled"}


def test_save_results_manual_not_tagged(scanner_db):
    """手动扫描（用户在场非观察总体）→ 不打标"""
    Scanner._save_results(uuid.uuid4(), [_hit("BBBUSDT", {"cvd_div": {"bucket": "confirm"}})],
                          scan_type="manual")
    assert scanner_db.execute(select(ShadowMark)).scalar_one_or_none() is None


def test_save_results_no_shadow_no_mark(scanner_db):
    """无 shadow 键（特征计算全 None）→ 不落空标记"""
    Scanner._save_results(uuid.uuid4(), [_hit("CCCUSDT")], scan_type="scheduled")
    assert scanner_db.execute(select(ShadowMark)).scalar_one_or_none() is None


def test_save_results_shadow_corrupt_does_not_break_results(scanner_db):
    """影子落库失败（脏 value_json 序列化炸）只回滚影子，扫描结果照常落库"""
    bad = {"cvd_div": {"bucket": {"not", "serializable"}}}  # set → json.dumps 必炸
    Scanner._save_results(uuid.uuid4(), [_hit("DDDUSDT", bad)], scan_type="scheduled")
    assert scanner_db.execute(select(ScanResult)).scalar_one() is not None
    assert scanner_db.execute(select(ShadowMark)).scalar_one_or_none() is None


def test_shadow_mark_unique_constraint(scanner_db):
    """uq_shadow_mark (feature_key, ref_type, ref_id)：同宿主同特征只允许一行（幂等基座）"""
    rid = uuid.uuid4()
    scanner_db.add(ShadowMark(feature_key="cvd_div", ref_type="scan_result",
                              ref_id=rid, symbol="XUSDT"))
    scanner_db.commit()
    scanner_db.add(ShadowMark(feature_key="cvd_div", ref_type="scan_result",
                              ref_id=rid, symbol="XUSDT"))
    with pytest.raises(Exception):
        scanner_db.commit()
    scanner_db.rollback()


# ── 5. build_report 归因链 join ───────────────────────────────────────


def _scan_result(db, symbol="SUSDT") -> ScanResult:
    sr = ScanResult(
        scan_record_id=uuid.uuid4(), symbol=symbol, signal_type="trigger_breakout",
        current_price=100.0, breakout_pct=0.01, trend_slope=0.5, r_squared=0.8,
    )
    db.add(sr)
    db.commit()
    return sr


def _mark(db, feature, ref_id, bucket):
    db.add(ShadowMark(feature_key=feature, ref_type="scan_result", ref_id=ref_id,
                      symbol="SUSDT", value_json={"bucket": bucket},
                      decision_json={"stage": "scan_hit"}))
    db.commit()


def test_build_report_attribution_join(db):
    """归因链 ScanResult → AIAnalysis → TradeReview.outcome：
    有复盘计入胜率、未复盘 pending、无 AI 分析 no_analysis（不进胜率分母）"""
    sr_win, sr_pending, sr_noai, sr_loss = (_scan_result(db) for _ in range(4))

    a_win = AIAnalysis(symbol="SUSDT", trade_decision="suggest", direction="long",
                       recommendation=80, created_at=datetime.utcnow(),
                       scan_result_id=sr_win.id)
    a_loss = AIAnalysis(symbol="SUSDT", trade_decision="suggest", direction="long",
                        recommendation=80, created_at=datetime.utcnow(),
                        scan_result_id=sr_loss.id)
    a_pending = AIAnalysis(symbol="SUSDT", trade_decision="suggest", direction="long",
                           recommendation=80, created_at=datetime.utcnow(),
                           scan_result_id=sr_pending.id)
    db.add_all([a_win, a_loss, a_pending])
    db.commit()
    db.add_all([
        TradeReview(ai_analysis_id=a_win.id, symbol="SUSDT", outcome="win_tp1"),
        TradeReview(ai_analysis_id=a_loss.id, symbol="SUSDT", outcome="loss"),
    ])
    db.commit()

    _mark(db, "cvd_div", sr_win.id, "diverge_bear")
    _mark(db, "cvd_div", sr_pending.id, "confirm")
    _mark(db, "cvd_div", sr_noai.id, "diverge_bear")
    _mark(db, "cvd_div", sr_loss.id, "confirm")
    # 已删除卡的孤儿标记（CARD-03 vol_regime 2026-10-13 关闭并删代码，DB 历史
    # marks 保留）：注册表无条目 → 不进报告（历史归档不影响新评估）
    _mark(db, "vol_regime", sr_win.id, "compress")

    rep = {r["feature"]: r for r in build_report(db, window_days=30)}
    cvd = rep["cvd_div"]
    assert cvd["baseline"] == {"n": 4, "counted": 2, "win_rate": 0.5, "avg_r": None}
    buckets = {b["bucket"]: b for b in cvd["buckets"]}
    assert buckets["diverge_bear"]["n"] == 2 and buckets["diverge_bear"]["counted"] == 1
    assert buckets["diverge_bear"]["win_rate"] == 1.0  # 只计入 win_tp1
    assert buckets["confirm"]["counted"] == 1 and buckets["confirm"]["win_rate"] == 0.0
    assert buckets["diverge_bear"].get("insufficient") is True   # n<30 小样本标注
    assert buckets["confirm"].get("insufficient") is True
    assert cvd["sufficient"] is False  # counted 2 < min_sample 80

    # CARD-03 于 2026-10-13 裁决关闭并删代码（docs/14 §十一）→ 不出报告（marks 仍在库）
    assert "vol_regime" not in rep


def test_build_report_scan_avg_r(db):
    """scan_result 宿主卡 avg_r 读数（预注册 2026-10-19，docs/14 §十一 遗留问题 1）：
    R 逐字复用建议回放 ev_r（api/scan.py::_ev_r）——win_tp1=(TP1−entry)/stop_dist、
    loss=−1.0R、win_tp2=(TP2−entry)/stop_dist，各减往返费用（0.001+
    0.0003×max(1,bars//8)，折成 R）；expired/缺三价不计 avg_r 分母（胜率分母照计）。"""
    sr1, sr2, sr3, sr4 = (_scan_result(db) for _ in range(4))
    prices = dict(entry_price=100.0, stop_loss=95.0,
                  take_profit_1=105.0, take_profit_2=110.0)
    a1 = AIAnalysis(symbol="SUSDT", trade_decision="suggest", direction="long",
                    recommendation=80, created_at=datetime.utcnow(),
                    scan_result_id=sr1.id, **prices)
    a2 = AIAnalysis(symbol="SUSDT", trade_decision="suggest", direction="long",
                    recommendation=80, created_at=datetime.utcnow(),
                    scan_result_id=sr2.id, **prices)
    a3 = AIAnalysis(symbol="SUSDT", trade_decision="suggest", direction="long",
                    recommendation=80, created_at=datetime.utcnow(),
                    scan_result_id=sr3.id, **prices)
    a4 = AIAnalysis(symbol="SUSDT", trade_decision="suggest", direction="long",
                    recommendation=80, created_at=datetime.utcnow(),
                    scan_result_id=sr4.id)  # 缺三价 → 不回放 R
    db.add_all([a1, a2, a3, a4])
    db.commit()
    db.add_all([
        TradeReview(ai_analysis_id=a1.id, symbol="SUSDT", outcome="win_tp1", bars_to_exit=1),
        TradeReview(ai_analysis_id=a2.id, symbol="SUSDT", outcome="loss", bars_to_exit=1),
        TradeReview(ai_analysis_id=a3.id, symbol="SUSDT", outcome="win_tp2", bars_to_exit=1),
        TradeReview(ai_analysis_id=a4.id, symbol="SUSDT", outcome="loss", bars_to_exit=1),
    ])
    db.commit()
    _mark(db, "cvd_div", sr1.id, "confirm")
    _mark(db, "cvd_div", sr2.id, "confirm")
    _mark(db, "cvd_div", sr3.id, "diverge_bear")
    _mark(db, "cvd_div", sr4.id, "diverge_bear")

    cvd = {r["feature"]: r for r in build_report(db, window_days=30)}["cvd_div"]
    buckets = {b["bucket"]: b for b in cvd["buckets"]}
    # 费用 0.001+0.0003×1=0.0013 → 折算 R = 0.0013/(5/100)=0.026
    # win_tp1 = 1.0−0.026 = 0.974；loss = −1.0−0.026 = −1.026 → 均值 −0.026
    assert buckets["confirm"]["counted"] == 2
    assert buckets["confirm"]["avg_r"] == pytest.approx(-0.026, abs=1e-4)
    # win_tp2 = 2.0−0.026 = 1.974；sr4 缺三价 → R None（不计 avg_r 分母，胜率分母照计）
    assert buckets["diverge_bear"]["counted"] == 2
    assert buckets["diverge_bear"]["avg_r"] == pytest.approx(1.974, abs=1e-4)
    # baseline：四笔 counted、R 有效 3 笔 → (0.974 − 1.026 + 1.974)/3
    assert cvd["baseline"]["counted"] == 4
    assert cvd["baseline"]["avg_r"] == pytest.approx(0.6407, abs=1e-4)


def test_build_report_sufficient_threshold(db, monkeypatch):
    """达标判定 = 有效归因计数 ≥ 卡最小样本（预注册后不可回调；测试里调小验证翻转）"""
    monkeypatch.setattr(SHADOW_FEATURES["cvd_div"], "min_sample", 1)
    sr = _scan_result(db)
    a = AIAnalysis(symbol="SUSDT", trade_decision="suggest", direction="long",
                   recommendation=80, created_at=datetime.utcnow(),
                   scan_result_id=sr.id)
    db.add(a)
    db.commit()
    db.add(TradeReview(ai_analysis_id=a.id, symbol="SUSDT", outcome="win_tp2"))
    db.commit()
    _mark(db, "cvd_div", sr.id, "diverge_bear")

    rep = build_report(db, window_days=30)[0]
    assert rep["baseline"]["counted"] == 1
    assert rep["sufficient"] is True


def test_build_report_window_and_empty(db):
    """窗口过滤（window 外标记不计）+ 无标记特征返回空桶空态"""
    sr = _scan_result(db)
    old = ShadowMark(feature_key="cvd_div", ref_type="scan_result", ref_id=sr.id,
                     symbol="SUSDT", value_json={"bucket": "confirm"},
                     created_at=datetime.utcnow() - timedelta(days=60))
    db.add(old)
    db.commit()

    rep = {r["feature"]: r for r in build_report(db, window_days=30)}
    assert rep["cvd_div"]["baseline"]["n"] == 0       # 60 天前 → 30 天窗口外
    assert rep["cvd_div"]["buckets"] == []
    assert rep["cvd_div"]["sufficient"] is False
    assert "vol_regime" not in rep  # CARD-03 已删代码 → 不出报告（marks 仍在库）


def test_build_report_rule_version_window(db):
    """跨版本不混算（docs/14 §六.1，2026-10-13）：卡声明 since_rule_version 后，
    历史 NULL 与旧版本标记出窗，仅当前版本（ORM 默认 settings.RULE_VERSION）计入
    ——CARD-01/04 候选池依赖规则集，规则重构后前向窗口据此自动重置"""
    sr_null, sr_old, sr_cur = (_scan_result(db) for _ in range(3))
    # 历史 NULL 行（旧部署存量，列未回填）与旧版本行：均应出窗。
    # 用 Core insert 显式 rule_version=None 绕过 ORM 列默认（ORM 会给 None 补当前版本）
    db.execute(insert(ShadowMark).values(
        feature_key="cvd_div", ref_type="scan_result", ref_id=sr_null.id,
        symbol="SUSDT", value_json={"bucket": "confirm"}, rule_version=None))
    db.execute(insert(ShadowMark).values(
        feature_key="cvd_div", ref_type="scan_result", ref_id=sr_old.id,
        symbol="SUSDT", value_json={"bucket": "confirm"}, rule_version="v2026-10-12-1"))
    db.commit()
    _mark(db, "cvd_div", sr_cur.id, "diverge_bear")  # 当前版本 → 计入

    rep = {r["feature"]: r for r in build_report(db, window_days=30)}
    assert rep["cvd_div"]["baseline"]["n"] == 1
    assert [b["bucket"] for b in rep["cvd_div"]["buckets"]] == ["diverge_bear"]


# ── 6. 日报附录（shadow_report_lines）异常隔离与格式 ──────────────────


def _fake_report():
    return [
        {"feature": "cvd_div", "name": "CVD 背离", "card": "CARD-01", "min_sample": 80,
         "sufficient": True,
         "baseline": {"n": 210, "counted": 120, "win_rate": 0.41},
         "buckets": [
             {"bucket": "confirm", "n": 150, "counted": 90, "win_rate": 0.44},
             {"bucket": "diverge_bear", "n": 34, "counted": 24, "win_rate": 0.29},
             {"bucket": "diverge_bull", "n": 26, "counted": 6, "win_rate": None},
         ]},
        {"feature": "r2_slope", "name": "趋势质量", "card": "CARD-10", "min_sample": 120,
         "sufficient": False,
         "baseline": {"n": 0, "counted": 0, "win_rate": None},
         "buckets": []},
    ]


def test_shadow_report_lines_format(monkeypatch):
    """基线 n=0 特征跳过、对照桶不展开、差值按 pp、达标状态标注"""
    monkeypatch.setattr(shadow_tasks, "build_report_safe",
                        lambda db, window_days=30: _fake_report())
    lines = shadow_tasks.shadow_report_lines()
    assert len(lines) == 1  # r2_slope 无样本不占版面
    line = lines[0]
    assert "CVD 背离" in line and "✅ 达标" in line
    assert "基线 41%（n=210）" in line
    assert "diverge_bear 29% n=34（-12pp）" in line
    assert "diverge_bull" in line and "—" in line  # 无有效归因胜率显示 —
    assert "confirm" not in line  # 对照桶不在日报展开


def test_shadow_report_lines_swallows_errors(monkeypatch):
    """build_report 任何异常 → 附录降级为空，不拖垮日报主流程"""
    def _boom(db, window_days=30):
        raise RuntimeError("db down")

    monkeypatch.setattr(shadow_tasks, "build_report_safe", _boom)
    assert shadow_tasks.shadow_report_lines() == []
