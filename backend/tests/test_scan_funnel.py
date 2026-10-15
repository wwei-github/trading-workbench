# -*- coding: utf-8 -*-
"""筛选漏斗回归（2026-10-11）：被拒币种 + 卡住关卡的结构化收集与落库。

detect_indicator_candidate 各关卡经 reject_out 旁路写 {"stage": <关卡代码>}
（与 event_out 同模式，零破坏旧调用点）；主扫描 run 收口组装 funnel_json
随 ScanRecord 落库，前端「筛选漏斗」Tab 展示。关卡代码表：
gate_4h / votes / direction_flip / range_score / no_trigger /
trigger_whitelist / htf_state_lock（进池准入闸，2026-10-13）+
预过滤 liquidity/new_coin/risk_tier/blacklist（预过滤在 run_scan 内联）。
（structure_1h 关卡 2026-10-15 随 1H 结构门降级重罚退役：不过门照常出候选，
不再进漏斗被拒，见 docs/21 O9。）

判定链测试打法：monkeypatch 打桩各闸门/票数/触发（test_scan_filters 白名单
测试同款），使命中点唯一落在被测关卡；另用真实夹具（ranging 4h）抽查
gate_4h 全管线路径。进待确认池（pending upsert）不算被拒——reject_out 不写。
"""
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

from app.database import Base  # noqa: E402
from app.models.scan import ScanRecord  # noqa: E402
from app.services import scanner as scanner_mod  # noqa: E402
from app.services.scanner import Scanner, detect_indicator_candidate  # noqa: E402

_PASS_VOTES = {"bull": ["MACD", "RSI"], "bear": [], "bull_n": 2, "bear_n": 0,
               "net": 2, "voted": 2, "results": {}}
_FAIL_VOTES = {"bull": ["MACD"], "bear": ["RSI"], "bull_n": 1, "bear_n": 1,
               "net": 0, "voted": 2, "results": {}}
_SHORT_VOTES = {"bull": [], "bear": ["MACD", "RSI"], "bull_n": 0, "bear_n": 2,
                "net": -2, "voted": 2, "results": {}}


class FakePool:
    """4h 返回非空占位（闸门块执行）或空（跳过闸门块），1h 忽略"""

    def __init__(self, htf_nonempty=True):
        self.htf = [[0, 100.0, 101.0, 99.0, 100.0, 10.0, 10.0]] if htf_nonempty else []

    def get_klines(self, symbol, interval, limit, **kwargs):
        return self.htf[:limit]


def _detect(reject_out, klines=None, *, votes=_PASS_VOTES, gate=("long", {}), htf_nonempty=True,
            rs=0, trigger=None, flt=None, pending=None, real_trigger=False, htf_state=None):
    """打桩装配一次 detect：各桩默认全放行，命中点由参数控制。
    real_trigger=True 时不打桩 detect_trigger（跑真实触发链，测 no_trigger 明细）；
    htf_state 非 None 时打桩 analyze_htf_background 返回该 4h 状态机态（4h 状态机
    前置门用：tangle/turn_* 一票否决全部 1h 候选）"""
    import app.services.scanner as sm

    orig = {
        "analyze_ema": sm.analyze_ema, "classify_volume": sm.classify_volume,
        "compute_indicator_votes": sm.compute_indicator_votes,
        "gate_4h": sm.gate_4h, "analyze_htf_background": sm.analyze_htf_background,
        "htf_core": sm.htf_core,
        "range_score": sm.range_score, "detect_trigger": sm.detect_trigger,
    }
    sm.analyze_ema = lambda k: None
    sm.classify_volume = lambda k: (1.0, "平量")
    sm.compute_indicator_votes = lambda k, h=None, htf_bg=None: votes
    # 4h 共享计算核心（2026-10-13）：scanner 先调 htf_core 再透传 core；此处打桩返回
    # None（等价样本不足），各桩忽略 core 参数即可
    sm.htf_core = lambda k, fast=21, slow=55: None
    sm.gate_4h = lambda k, cfg=None, core=None: gate
    if htf_state is not None:
        _labels = {"tangle": "缠绕", "turn_bull": "多头拐头", "turn_bear": "空头拐头",
                   "aligned_bull": "多头排列", "macro_cross_bull": "55×144金叉"}
        sm.analyze_htf_background = lambda k, fast=21, slow=55, core=None: {
            "state": htf_state, "state_label": _labels.get(htf_state, htf_state)}
    sm.range_score = lambda k, gate_ev=None: (rs, [])
    if not real_trigger:
        sm.detect_trigger = lambda *a, **k: trigger
    try:
        return detect_indicator_candidate(
            FakePool(htf_nonempty), "XUSDT", klines if klines is not None else [],
            flt=flt, pending=pending, reject_out=reject_out)
    finally:
        for k, v in orig.items():
            setattr(sm, k, v)


def test_reject_gate_4h():
    """gate_4h 主动否决（返回 None）→ stage=gate_4h"""
    rv: dict = {}
    cand, _ = _detect(rv, gate=None)
    assert cand is None and rv == {"stage": "gate_4h"}


def test_reject_votes():
    """方向票同向不足（同向 <2 张，2026-10-15 拆角色后门槛）→ stage=votes"""
    rv: dict = {}
    cand, _ = _detect(rv, votes=_FAIL_VOTES, htf_nonempty=False)
    assert cand is None and rv == {"stage": "votes"}


def test_reject_votes_detail_attached():
    """votes 拒绝且有票 → reject_out 附 detail（2026-10-11 拍板：判定说明展示全部票）"""
    rv: dict = {}
    votes = {"bull": ["MACD"], "bear": ["KDJ"], "bull_n": 1, "bear_n": 1,
             "net": 0, "voted": 3,
             "results": [
                 ("MACD", {"dir": 1, "state_label": "多头", "detail": "金叉"}),
                 ("KDJ", {"dir": -1, "state_label": "空头", "detail": "死叉"}),
                 ("4h背景", {"dir": 0, "state_label": "缠绕"}),
                 ("BBW", {"dir": None}),
             ]}
    cand, _ = _detect(rv, votes=votes, htf_nonempty=False)
    assert cand is None and rv["stage"] == "votes"
    assert rv["detail"] == ("MACD ▲ 多头（金叉）；KDJ ▼ 空头（死叉）；"
                            "4h背景 — 缠绕；BBW —")


def test_votes_detail_render():
    """_votes_detail 渲染：dir 缺失/异常降级箭头 —，无 label/detail 只出短名"""
    from app.services.scanner import _votes_detail
    votes = {"results": [
        ("EMA排列", {"dir": 1, "state_label": "多头排列", "detail": "21>55>144"}),
        ("RSI", {"dir": "bad"}),
        ("X", {}),
    ]}
    assert _votes_detail(votes) == "EMA排列 ▲ 多头排列（21>55>144）；RSI —；X —"


def test_reject_direction_flip():
    """方向票空头 + 闸门多头 → stage=direction_flip（4h 方向硬门）"""
    rv: dict = {}
    cand, _ = _detect(rv, votes=_SHORT_VOTES, gate=("long", {}))
    assert cand is None and rv == {"stage": "direction_flip"}


def test_structure_1h_gate_retired_signal_survives():
    """筛选链 1H 结构门退役（2026-10-12 O7；2026-10-15 O8 再降级重罚）：scanner
    不再整体否决信号——触发链照常返回候选（structure_gate=False 也照常出候选，
    仅质量分重罚）；pending 处置仅由 trigger 返回的动作驱动，scanner 自身不再因
    结构门 expire（1H 结构门现为 trigger 层 ema21 触点软校验，不过门不否决）"""
    from app.services.strategy.trigger import PULLBACK_CONFIRM

    kl = [[1_700_000_000_000 + i * 3_600_000, 100.0, 101.0, 99.0, 100.0, 10.0, 10.0]
          for i in range(12)]
    trig = {"type": PULLBACK_CONFIRM, "direction": 1, "position": "support",
            "pattern": "hammer", "breakout_pct": 0.5, "reason": "r",
            "touch_ref": "ema21", "structure_gate": False}
    rv: dict = {}
    cand, _ = _detect(rv, kl, trigger=(trig, None), htf_nonempty=False)
    assert cand is not None and rv == {}
    # pending 也不再因结构门 expire（此前结构门否决会顺带过期活跃 pending）
    rv2: dict = {}
    pend = {"id": 1, "direction": 1, "stage1_candle_open": 1_700_000_000_000,
            "touch_ref": "ema21"}
    cand2, op2 = _detect(rv2, kl, trigger=(None, None), pending=pend,
                         htf_nonempty=False)
    assert cand2 is None and op2 is None and rv2 == {"stage": "no_trigger"}


def test_reject_range_score():
    """震荡评分 ≥3 → stage=range_score"""
    rv: dict = {}
    cand, _ = _detect(rv, rs=3, htf_nonempty=False)
    assert cand is None and rv == {"stage": "range_score"}


def test_structure_1h_reason_no_longer_a_stage():
    """structure_1h 关卡 2026-10-15 退役（1H 结构门降级重罚）：trigger 历史遗留的
    expire/structure_1h 死因在 scanner 侧不再单列，落 no_trigger 兜底"""
    rv: dict = {}
    cand, _ = _detect(rv, trigger=(None, {"op": "expire", "reason": "structure_1h"}),
                      htf_nonempty=False)
    assert cand is None and rv == {"stage": "no_trigger"}


def test_reject_no_trigger():
    """全门通过但 detect_trigger 无触发且无 pending 处置 → stage=no_trigger"""
    rv: dict = {}
    cand, _ = _detect(rv, trigger=(None, None), htf_nonempty=False)
    assert cand is None and rv == {"stage": "no_trigger"}


# ── 无触发原因明细（2026-10-13 漏斗「无触发」判定说明）──────────────────


def _diag_flt(**over):
    """诊断测试用筛选参数组：显式口径 + 仅 1h EMA21 触点"""
    from app.services.scan_filters import ScanFilters as SF
    kw = dict(
        min_volume_24h=1, breakout_channel_bars=20, breakout_threshold=0.002,
        breakout_vol_ratio=1.2, breakout_max_pct=1.5, candle_max_ema21_dist_pct=0,
        ai_max_per_scan=10, pullback_touch_refs=("ema21",),
    )
    kw.update(over)
    return SF(**kw)


def _rising_klines(n=60):
    """缓涨小实体序列：实体 0.1 / 全长 1.1 ≈ 9.1% 不过长实体门、低点始终高于
    EMA21（无触点）、无金K——用于真实 detect_trigger 的「无触发」路径"""
    out = []
    ts = 1_700_000_000_000
    px = 100.0
    for i in range(n):
        c = px + 0.1
        out.append([ts + i * 3_600_000, px, c + 0.9, px - 0.1, c, 10.0, 10.0])
        px = c
    return out


def test_no_trigger_detail_real_trigger():
    """真实触发链无触发（2026-10-13）：no_trigger 关 reject_out 附 detail——
    突破实体门 / 回踩未命中触点 / 本根无金K，分号连接（前端判定说明列拆行）"""
    rv: dict = {}
    cand, op = _detect(rv, _rising_klines(), flt=_diag_flt(), htf_nonempty=False,
                       real_trigger=True)
    assert cand is None and op is None
    assert rv["stage"] == "no_trigger"
    assert "实体占比9.1%<70%" in rv["detail"]
    assert "未命中任何启用触点（EMA21）" in rv["detail"]
    assert rv["detail"].endswith("本根无同向金K")


def test_no_trigger_detail_pending_expired():
    """活跃 pending（Stage-1 已到确认窗口最后一根 elapsed=N）+ 本根无金K无触点
    → expire/window_expired，detail 附待确认失效原因（2026-10-13；窗口放宽为 N 根）"""
    from datetime import datetime as dt
    kl = _rising_klines()
    pending = {
        "id": 1, "direction": 1, "touch_ref": "swing",
        "stage1_candle_open": dt.utcfromtimestamp(kl[-6][0] / 1000),  # elapsed=N=4（2026-10-14 窗口默认 3→4）
    }
    rv: dict = {}
    cand, op = _detect(rv, kl, flt=_diag_flt(), pending=pending,
                       htf_nonempty=False, real_trigger=True)
    assert cand is None
    assert op == {"op": "expire", "reason": "window_expired"}
    assert rv["stage"] == "no_trigger"
    assert "待确认失效：回踩摆动低点后确认窗口内未出现同向金K" in rv["detail"]


def test_reject_trigger_whitelist():
    """触发类型在白名单外且无 pending upsert → stage=trigger_whitelist"""
    from app.services.scan_filters import ALL_TRIGGERS
    from app.services.strategy.trigger import PULLBACK_CONFIRM, ScanFilters

    # 非空 1h 序列（白名单内分支会构造候选，current_price 取 klines[-1][4]）
    kl = [[1_700_000_000_000 + i * 3_600_000, 100.0, 101.0, 99.0, 100.0, 10.0, 10.0]
          for i in range(12)]
    flt = ScanFilters(
        min_volume_24h=1, breakout_channel_bars=20, breakout_threshold=0.002,
        breakout_vol_ratio=1.2, breakout_max_pct=0, candle_max_ema21_dist_pct=0,
        ai_max_per_scan=10, allowed_triggers=(PULLBACK_CONFIRM,),
    )
    breakout = {"type": "trigger_breakout", "direction": 1, "position": "resistance",
                "pattern": None, "breakout_pct": 0.5, "reason": "r"}
    rv: dict = {}
    cand, _ = _detect(rv, kl, trigger=(breakout, None), flt=flt, htf_nonempty=False)
    assert cand is None and rv == {"stage": "trigger_whitelist"}
    # 白名单含突破 → 候选照常产生，reject_out 不写
    flt2 = ScanFilters(
        min_volume_24h=1, breakout_channel_bars=20, breakout_threshold=0.002,
        breakout_vol_ratio=1.2, breakout_max_pct=0, candle_max_ema21_dist_pct=0,
        ai_max_per_scan=10, allowed_triggers=ALL_TRIGGERS,
    )
    rv2: dict = {}
    cand2, _ = _detect(rv2, kl, trigger=(breakout, None), flt=flt2, htf_nonempty=False)
    assert cand2 is not None and rv2 == {}


def test_pending_upsert_not_rejected():
    """触碰无金K进待确认池（upsert）不算被拒：reject_out 不写、pending_op 照常返回"""
    rv: dict = {}
    cand, op = _detect(rv, trigger=(None, {"op": "upsert", "direction": 1}),
                       htf_nonempty=False)
    assert cand is None and op["op"] == "upsert" and rv == {}


# ── 4h 状态机前置门 + 进待确认池白名单准入闸（2026-10-13）────────────────────
# 前置门：4h 状态机双向禁开（tangle/turn_*）→ 对全部 1h 候选（回踩·突破）一票
# 否决，活跃 pending 同轮 expire(htf_state_lock)；准入闸：仅「回踩确认不在触发
# 类型白名单」时挡新进池（状态机分支已由前置门接管）


def _adm_flt(allowed_triggers):
    """准入测试用筛选参数组：显式触发类型白名单"""
    from app.services.scan_filters import ScanFilters as SF
    return SF(
        min_volume_24h=1, breakout_channel_bars=20, breakout_threshold=0.002,
        breakout_vol_ratio=1.2, breakout_max_pct=1.5, candle_max_ema21_dist_pct=0,
        ai_max_per_scan=10, allowed_triggers=allowed_triggers,
    )


def test_pending_upsert_blocked_by_trigger_whitelist():
    """回踩确认不在触发类型白名单 → 不进待确认池：stage=trigger_whitelist、
    pending_op 丢弃（不落库）、附判定说明"""
    from app.services.scan_filters import TRIGGER_BREAKOUT
    rv: dict = {}
    cand, op = _detect(rv, trigger=(None, {"op": "upsert", "direction": 1}),
                       flt=_adm_flt((TRIGGER_BREAKOUT,)), htf_nonempty=False)
    assert cand is None and op is None
    assert rv["stage"] == "trigger_whitelist"
    assert "未在触发类型白名单内" in rv["detail"]


def test_htf_state_front_gate_blocks_upsert():
    """4h 状态机双向禁开（tangle/turn_*）→ 前置门一票否决：stage=htf_state_lock、
    无候选、不产生 pending upsert、附判定说明（回踩/突破候选均不做）"""
    from app.services.scan_filters import ALL_TRIGGERS
    for st, label in (("tangle", "缠绕"), ("turn_bear", "空头拐头")):
        rv: dict = {}
        cand, op = _detect(rv, trigger=(None, {"op": "upsert", "direction": 1}),
                           flt=_adm_flt(ALL_TRIGGERS), htf_state=st)
        assert cand is None and op is None
        assert rv["stage"] == "htf_state_lock"
        assert label in rv["detail"] and "回踩/突破候选均不做" in rv["detail"]


def test_htf_state_front_gate_blocks_breakout_candidate():
    """前置门对突破候选同样一票否决（1h 命中照拦）：即便触发链会给出突破候选，
    也在 4h 状态机层被挡下——stage=htf_state_lock、无候选"""
    from app.services.scan_filters import ALL_TRIGGERS
    breakout = {"type": "trigger_breakout", "direction": 1, "position": "resistance",
                "pattern": None, "breakout_pct": 0.5, "reason": "r"}
    rv: dict = {}
    cand, op = _detect(rv, _confirm_kl(), trigger=(breakout, None),
                       flt=_adm_flt(ALL_TRIGGERS), htf_state="turn_bull")
    assert cand is None and op is None
    assert rv["stage"] == "htf_state_lock"
    assert "多头拐头" in rv["detail"]


def test_htf_state_front_gate_expires_active_pending():
    """前置门命中时活跃 pending 同轮失效（expire/htf_state_lock）"""
    rv: dict = {}
    pend = {"id": 1, "direction": 1, "touch_ref": "ema21",
            "stage1_candle_open": 1_700_000_000_000}
    cand, op = _detect(rv, trigger=(None, None), pending=pend, htf_state="tangle")
    assert cand is None and op == {"op": "expire", "reason": "htf_state_lock"}
    assert rv["stage"] == "htf_state_lock"


def test_pending_upsert_allowed_when_gates_pass():
    """触发类型白名单含回踩确认 + 4h 状态非禁开 → 正常 upsert（reject_out 不写）"""
    from app.services.scan_filters import ALL_TRIGGERS
    rv: dict = {}
    cand, op = _detect(rv, trigger=(None, {"op": "upsert", "direction": 1}),
                       flt=_adm_flt(ALL_TRIGGERS), htf_state="aligned_bull")
    assert cand is None and op["op"] == "upsert" and "quality_score" in op
    assert rv == {}


def test_existing_pending_expire_unaffected_by_admission_gate():
    """准入闸只挡新进池：已有 pending 的 expire 生命周期不受白名单/状态机影响"""
    from app.services.scan_filters import TRIGGER_BREAKOUT
    rv: dict = {}
    pend = {"id": 1, "direction": 1, "touch_ref": "ema21",
            "stage1_candle_open": 1_700_000_000_000}
    cand, op = _detect(rv, trigger=(None, {"op": "expire", "reason": "window_expired"}),
                       flt=_adm_flt((TRIGGER_BREAKOUT,)), pending=pend,
                       htf_state="tangle", htf_nonempty=False)
    assert cand is None and op == {"op": "expire", "reason": "window_expired"}
    assert rv["stage"] == "no_trigger"


def _confirm_trig():
    from app.services.strategy.trigger import PULLBACK_CONFIRM
    return {"type": PULLBACK_CONFIRM, "direction": 1, "position": "support",
            "pattern": "hammer", "breakout_pct": 0.5, "reason": "r",
            "touch_ref": "ema21"}


def _confirm_kl():
    return [[1_700_000_000_000 + i * 3_600_000, 100.0, 101.0, 99.0, 100.0, 10.0, 10.0]
            for i in range(12)]


def _active_pending():
    return {"id": 1, "direction": 1, "touch_ref": "ema21",
            "stage1_candle_open": 1_700_000_000_000}


def test_pending_confirm_blocked_by_htf_state_front_gate():
    """次根确认路径被 4h 状态机前置门挡下：双向禁开 → 不产生候选、活跃 pending
    同轮失效（expire/htf_state_lock）、漏斗记 htf_state_lock + 判定说明"""
    from app.services.scan_filters import ALL_TRIGGERS
    rv: dict = {}
    cand, op = _detect(rv, _confirm_kl(), trigger=(_confirm_trig(), {"op": "confirm"}),
                       pending=_active_pending(), flt=_adm_flt(ALL_TRIGGERS),
                       htf_state="tangle")
    assert cand is None
    assert op == {"op": "expire", "reason": "htf_state_lock"}
    assert rv["stage"] == "htf_state_lock"
    assert "缠绕" in rv["detail"] and "回踩/突破候选均不做" in rv["detail"]


def test_pending_confirm_allowed_when_state_pass():
    """次根确认 + 4h 状态放行 → 候选照常产生、pending confirm（不写 reject_out）"""
    from app.services.scan_filters import ALL_TRIGGERS
    from app.services.strategy.trigger import PULLBACK_CONFIRM
    rv: dict = {}
    cand, op = _detect(rv, _confirm_kl(), trigger=(_confirm_trig(), {"op": "confirm"}),
                       pending=_active_pending(), flt=_adm_flt(ALL_TRIGGERS),
                       htf_state="aligned_bull")
    assert cand is not None and cand["signal_type"] == PULLBACK_CONFIRM
    assert op == {"op": "confirm"} and rv == {}


def test_reject_real_pipeline_gate_4h():
    """真实管线抽查（非打桩）：4h 走平 → gate_4h 否决 + reject 旁路一致
    （test_htf_ranging_filter 同款夹具形态：220 根窄幅 4h + 300 根上行 1h）"""
    kl_1h = []
    ts = 1_700_000_000_000
    price = 100.0
    for i in range(300):
        o = price
        price += 0.35
        kl_1h.append([ts + i * 3_600_000, o, price + 0.15, price - 0.25, price, 100.0, 100.0])
    kl_4h = []
    for i in range(220):
        o = 100.0 + 0.3 * ((i % 8) - 4) / 4
        c = 100.0 + 0.3 * (((i + 4) % 8) - 4) / 4
        kl_4h.append([ts + i * 4 * 3_600_000, o, max(o, c) + 0.1, min(o, c) - 0.1, c, 80.0, 80.0])

    class Pool:
        def __init__(self, m):
            self._m = m

        def get_klines(self, symbol, interval, limit, **kwargs):
            return self._m[interval][:limit]

    rv: dict = {}
    cand, _ = detect_indicator_candidate(
        Pool({"1h": kl_1h, "4h": kl_4h}), "TEST/USDT", kl_1h, reject_out=rv)
    assert cand is None and rv == {"stage": "gate_4h"}


# ── 落库：_update_scan_status 挂 funnel_json ─────────────────────────


@pytest.fixture()
def db(monkeypatch):
    # StaticPool + check_same_thread=False：_update_scan_status 用独立
    # SessionLocal 连接，需共享同一内存库（test_slots_full_by_net 同款惯例）
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine, tables=[ScanRecord.__table__])
    monkeypatch.setattr(scanner_mod, "SessionLocal", lambda: Session(bind=engine))
    session = Session(bind=engine)
    yield session
    session.close()


def test_update_scan_status_stores_funnel(db):
    """扫描完成时 funnel_json 落库；funnel=None（异常轮次）不动既有值"""
    rec = ScanRecord(id=uuid.uuid4(), scan_type="scheduled", status="running")
    db.add(rec)
    db.commit()

    funnel = {
        "universe": 480, "scanned": 185, "errors": 2, "candidates": 5,
        "pending_pool": 12,
        "stages": {"gate_4h": 120, "votes": 30, "no_trigger": 8},
        "rejects": {"BTCUSDT": "gate_4h", "ETHUSDT": "votes"},
    }
    Scanner._update_scan_status(
        rec.id, "completed", 5, 2, 480, funnel=funnel)
    db.expire_all()
    rec2 = db.get(ScanRecord, rec.id)
    assert rec2.status == "completed"
    assert rec2.funnel_json == funnel
    assert rec2.funnel_json["rejects"]["BTCUSDT"] == "gate_4h"

    # funnel=None（异常路径）不覆盖已存漏斗
    Scanner._update_scan_status(rec.id, "failed", 0, 480, 480, funnel=None)
    db.expire_all()
    assert db.get(ScanRecord, rec.id).funnel_json == funnel


def test_update_scan_status_funnel_absent_param(db):
    """不传 funnel（旧调用点兼容）：行为与原来一致，funnel_json 保持 NULL"""
    rec = ScanRecord(id=uuid.uuid4(), scan_type="manual", status="running")
    db.add(rec)
    db.commit()
    Scanner._update_scan_status(rec.id, "completed", 0, 0, 100)
    db.expire_all()
    rec2 = db.get(ScanRecord, rec.id)
    assert rec2.status == "completed" and rec2.funnel_json is None
