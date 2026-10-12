"""CARD-08 sweep_reclaim 影子打标单测（docs/14 §七 / docs/13 #10，2026-10-07）。

覆盖：纯几何分桶 _sweep_bucket（方向对称/收回边界/窗口过滤/未收盘剔除）、
compute_sweep_reclaim_scan 守卫（EMA 族不打标/方向不明/非回踩）、触点位
解析三路径（swing 当根复算 / swing 次根 pending 锚点 / retest 事件价）、
拉取异常全吞、注册表元数据。
"""
import uuid
from datetime import datetime, timedelta

import pytest

from app.services import shadow as shadow_mod
from app.services.strategy.trigger import TOUCH_RETEST, TOUCH_SWING

BAR = shadow_mod.SWEEP_BAR_MS  # 15m
H = 3_600_000


def _sig_open_ms() -> int:
    """信号 1h bar 开盘：取整到小时并回退一小时，避免贴近当前时间边界。"""
    now = int(datetime.utcnow().timestamp() * 1000)
    return (now // H - 2) * H


def _klines(sig_open: int) -> list:
    """最小 1h klines（4 根，[-2]=信号 bar（开盘=sig_open）、[-1]=未收盘）。"""
    rows = []
    for i in range(-2, 2):
        o = sig_open + i * H
        rows.append([o, 100.0, 101.0, 99.0, 100.5, 1.0] + [0.0] * 6)
    return rows


def _cand(touch_ref=TOUCH_SWING, net=3, signal_type="trigger_pullback_confirm") -> dict:
    return {"symbol": "TESTUSDT", "signal_type": signal_type,
            "touch_ref": touch_ref, "votes": {"net": net}}


def _bars(spec: dict) -> dict:
    """spec: {相对信号bar收盘的15m步偏移(负=已收盘): (h, l, c)}
    −1 = 最后一根已收盘 15m 根（ts+15m == t_close）；0 = 未收盘根被滤。"""
    t_close = _sig_open_ms() + H
    return {t_close + k * BAR: list(v) for k, v in spec.items()}


def _sweep_bars(level=100.0):
    """在窗内造一根「插针+当根收回」的已收盘 15m 根（−1 步）。"""
    return _bars({-1: (level * 0.995, level * 0.995, level * 1.005)})


class _FakePool:
    def __init__(self, bars: dict = None, raise_: bool = False):
        self.bars = bars or {}
        self.raise_ = raise_
        self.calls = []

    def get_klines_range(self, symbol, interval, limit, end_time_ms):
        self.calls.append((symbol, interval, limit, end_time_ms))
        if self.raise_:
            raise RuntimeError("boom")
        return [[ts, 0.0, v[0], v[1], v[2], 1.0] + [0.0] * 6
                for ts, v in sorted(self.bars.items())]


# ── _sweep_bucket 纯几何 ─────────────────────────────────────────────────

def test_bucket_long_reclaim_k0_single_bar_pin():
    """多头单根长脚插针当根收回（k=0）。"""
    t_close = _sig_open_ms() + H
    bars = _bars({-1: (99.5, 99.5, 100.5)})  # low 99.5 击穿 100×(1-0.001)=99.9，收回
    r = shadow_mod._sweep_bucket(bars, 100.0, True, t_close, t_close - 2 * H)
    assert r["bucket"] == "sweep_reclaim" and r["reclaim_bars"] == 0
    assert r["depth_pct"] == pytest.approx(0.5, abs=0.01)


def test_bucket_long_reclaim_k3():
    t_close = _sig_open_ms() + H
    bars = _bars({-4: (99.8, 99.85, 99.7),
                  -1: (100.3, 99.95, 100.2)})  # −3/−2 缺失（gap 容忍），−1 未再插针
    r = shadow_mod._sweep_bucket(bars, 100.0, True, t_close, t_close - 2 * H)
    assert r["bucket"] == "sweep_reclaim" and r["reclaim_bars"] == 3


def test_bucket_long_open_not_reclaimed():
    t_close = _sig_open_ms() + H
    bars = _bars({-8: (99.8, 99.5, 99.7), -4: (99.8, 99.6, 99.75),
                  -1: (99.9, 99.7, 99.85)})  # 窗尾仍未收回
    r = shadow_mod._sweep_bucket(bars, 100.0, True, t_close, t_close - 2 * H)
    assert r["bucket"] == "sweep_open" and r["reclaim_bars"] is None


def test_bucket_no_sweep():
    t_close = _sig_open_ms() + H
    bars = _bars({-1: (100.4, 99.95, 100.2)})  # low 99.95 > 99.9 未及深度
    r = shadow_mod._sweep_bucket(bars, 100.0, True, t_close, t_close - 2 * H)
    assert r["bucket"] == "no_sweep" and r["sweep_bars"] == 0


def test_bucket_short_mirror():
    t_close = _sig_open_ms() + H
    bars = _bars({-1: (100.6, 100.5, 99.6)})  # high 100.6 ≥ 100.1，收回下方
    r = shadow_mod._sweep_bucket(bars, 100.0, False, t_close, t_close - 2 * H)
    assert r["bucket"] == "sweep_reclaim" and r["reclaim_bars"] == 0
    bars_open = _bars({-8: (100.6, 100.5, 100.4), -1: (100.3, 100.1, 100.2)})
    r2 = shadow_mod._sweep_bucket(bars_open, 100.0, False, t_close, t_close - 2 * H)
    assert r2["bucket"] == "sweep_open"


def test_bucket_window_and_unclosed_filter():
    """窗外插针不计、未收盘根（ts+15m > 信号收盘）不计。"""
    t_close = _sig_open_ms() + H
    bars = _bars({-9: (99.5, 99.5, 100.5),   # 窗外（win_start 前，−9 < −8）
                  -1: (100.4, 99.95, 100.2),
                  0: (99.5, 99.5, 100.5)})   # ts+15m = t_close+15m 未收盘
    r = shadow_mod._sweep_bucket(bars, 100.0, True, t_close, t_close - 2 * H)
    assert r["bucket"] == "no_sweep"


def test_bucket_insufficient_data():
    t_close = _sig_open_ms() + H
    assert shadow_mod._sweep_bucket({}, 100.0, True, t_close, t_close - 2 * H) is None
    assert shadow_mod._sweep_bucket(_bars({-1: (100, 99.95, 100)}), 0.0, True,
                                    t_close, t_close - 2 * H) is None


# ── compute_sweep_reclaim_scan 守卫与路径 ────────────────────────────────

def test_compute_guards_ema_family_and_direction():
    pool = _FakePool()
    # EMA 触点族不打标（回放结论）且不发起拉取
    assert shadow_mod.compute_sweep_reclaim_scan(
        _klines(_sig_open_ms()), _cand(touch_ref="ema21"), pool=pool) is None
    assert pool.calls == []
    assert shadow_mod.compute_sweep_reclaim_scan(
        _klines(_sig_open_ms()), _cand(net=0), pool=pool) is None
    assert shadow_mod.compute_sweep_reclaim_scan(
        _klines(_sig_open_ms()), _cand(signal_type="trigger_trend_breakout"),
        pool=pool) is None
    assert shadow_mod.compute_sweep_reclaim_scan(
        _klines(_sig_open_ms()), _cand(), pool=None) is None


def test_compute_swing_next_candle_via_pending_anchor(monkeypatch):
    """次根确认：pending 的 ema21 锚点 = 生产原生触点位。"""
    sig_open = _sig_open_ms()
    stage1 = datetime.utcfromtimestamp((sig_open - 6 * H) / 1000)  # Stage-1 六小时前
    pool = _FakePool(_sweep_bars())
    r = shadow_mod.compute_sweep_reclaim_scan(
        _klines(sig_open), _cand(),
        pending={"touch_ref": TOUCH_SWING, "ema21": 100.0,
                 "stage1_candle_open": stage1},
        pool=pool)
    assert r and r["bucket"] == "sweep_reclaim"
    assert r["level"] == 100.0 and r["touch_path"] == "next_candle"
    assert r["touch_ref"] == "swing" and r["direction"] == "long"
    # 窗口从 Stage-1 起（>2h），未收盘根已滤
    sym, interval, limit, end_ms = pool.calls[0]
    assert (sym, interval, limit) == ("TESTUSDT", "15m", 130)
    assert end_ms == sig_open + H


def test_compute_swing_same_candle_via_detect(monkeypatch):
    """当根确认：_detect_swing_touch 同输入复算（monkeypatch 验证接线）。"""
    sig_open = _sig_open_ms()
    monkeypatch.setattr(
        "app.services.strategy.trigger._detect_swing_touch",
        lambda kl, direction, band, order: 100.0)
    pool = _FakePool(_sweep_bars())
    r = shadow_mod.compute_sweep_reclaim_scan(
        _klines(sig_open), _cand(), pool=pool)  # 无 pending → 当根路径
    assert r and r["touch_path"] == "same_candle" and r["level"] == 100.0


def test_compute_retest_same_candle():
    """retest 当根：破轨事件价即参考位；方向不符的事件不被采用。"""
    sig_open = _sig_open_ms()
    pool = _FakePool(_sweep_bars(100.0))
    r = shadow_mod.compute_sweep_reclaim_scan(
        _klines(sig_open), _cand(touch_ref=TOUCH_RETEST),
        retest={"price": 100.0, "direction": 1}, pool=pool)
    assert r and r["touch_ref"] == "retest" and r["level"] == 100.0
    r2 = shadow_mod.compute_sweep_reclaim_scan(
        _klines(sig_open), _cand(touch_ref=TOUCH_RETEST),
        retest={"price": 100.0, "direction": -1}, pool=pool)  # 方向不符→无锚→不打标
    assert r2 is None


def test_compute_short_direction():
    sig_open = _sig_open_ms()
    # 空头插针：high 击穿上方（100.5 ≥ 100.1），收盘收回下方
    pool = _FakePool(_bars({-1: (100.5, 100.5, 99.5)}))
    r = shadow_mod.compute_sweep_reclaim_scan(
        _klines(sig_open), _cand(net=-3),
        pending={"touch_ref": TOUCH_SWING, "ema21": 100.0,
                 "stage1_candle_open": datetime.utcfromtimestamp(sig_open / 1000)},
        pool=pool)
    assert r and r["direction"] == "short" and r["bucket"] == "sweep_reclaim"


def test_compute_fetch_failure_swallowed():
    """拉取异常全吞（影子铁律）：返回 None 不抛。"""
    pool = _FakePool(raise_=True)
    assert shadow_mod.compute_sweep_reclaim_scan(
        _klines(_sig_open_ms()), _cand(),
        pending={"touch_ref": TOUCH_SWING, "ema21": 100.0,
                 "stage1_candle_open": datetime.utcnow()},
        pool=pool) is None


# ── 注册表与评估兼容 ─────────────────────────────────────────────────────

def test_registry_metadata():
    f = shadow_mod.SHADOW_FEATURES["sweep_reclaim"]
    assert f.card == "CARD-08" and f.min_sample == 30
    assert f.compute([1, 2]) is None  # 占位 compute：build_report 元数据用


def test_marks_written_via_save_results_shape():
    """落库形状：scan_result 宿主 + value_json.bucket（与 CARD-01~04 同链路，
    build_report 通用分支自动覆盖，此处锁关键字段形状防漂移）。"""
    value = shadow_mod.compute_sweep_reclaim_scan(
        _klines(_sig_open_ms()), _cand(),
        pending={"touch_ref": TOUCH_SWING, "ema21": 100.0,
                 "stage1_candle_open": datetime.utcnow()},
        pool=_FakePool(_sweep_bars()))
    assert set(("bucket", "touch_ref", "touch_path", "direction",
                "level", "depth_pct", "sweep_bars", "reclaim_bars",
                "window_h")) <= set(value or {})
    # 幂等约束用键（uq_shadow_mark）：feature_key + ref_type + ref_id
    mark = dict(feature_key="sweep_reclaim", ref_type="scan_result",
                ref_id=uuid.uuid4(), symbol="TESTUSDT",
                value_json=value, decision_json={"stage": "scan_hit"})
    assert mark["value_json"]["bucket"] in ("sweep_reclaim", "sweep_open", "no_sweep")


def test_pending_window_cap_24h():
    """Stage-1 早于 24h 上限时窗口封顶（cap 生效，不报错）。"""
    sig_open = _sig_open_ms()
    stage1 = datetime.utcfromtimestamp((sig_open - 40 * H) / 1000)
    r = shadow_mod.compute_sweep_reclaim_scan(
        _klines(sig_open), _cand(),
        pending={"touch_ref": TOUCH_SWING, "ema21": 100.0,
                 "stage1_candle_open": stage1},
        pool=_FakePool(_sweep_bars()))
    assert r and r["window_h"] <= shadow_mod.SWEEP_WIN_MAX_H
