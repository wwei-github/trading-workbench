"""4h EMA 状态机测试（2026-10-11 再重构 11 态，docs/17）

状态机（判定优先级即 _ema_state 代码顺序）：
tangle 缠绕（ADX<20 ∨ 21/55间距<0.1% ∨ 近10根21×55交叉≥2次锯齿）→
macro_cross_* 宏观交叉初期（55×144 交叉后 age<CROSS_FRESH_BARS，趋势级事件
优先定名，深V双叉不误判恢复）→ turn_* 拐头（仅全排列内 EMA21 斜率连续
TURN_CONFIRM_BARS 根反向）→ *_recover 恢复初期（宏观同侧的 21×55 新交叉）
→ aligned_* 排列稳态 → bull_dip/bear_rally 过渡态（宏观逆侧，21×55 关系
定名）。完备分割，无第五种落点。

锁单矩阵：tangle/turn_* 双向禁开；macro_cross_*/recover/aligned_* 锁逆势
（宏观对齐被状态机自然吸收，不再独立分支）；过渡态放行（质量分逆宏观
-4 兜底）。质量分查 STATE_QUALITY（同向加分, 逆向减分）；退役旧档
（weak/medium/strong/extreme/exhausted/overheat/cooling 存量行）走中性兜底。

夹具均经参数扫描定标（11/11 命中后固化为常量）：
- 缠绕：横盘（间距道）/ 正弦锯齿（ADX 道）
- 排列：140 根盘整基座 + 60 根缓涨/缓跌（趋势老到无 fresh 交叉）
- 拐头：排列后 10 根反向（EMA21 斜率连续 2 根反向）
- 宏观交叉：深V/倒V（154 根单边 + 46 根急返，55×144 交叉后 6 根）
- 恢复：160 根慢涨/跌 + 18 根回踩/反抽 + 22 根回升/回落（21×55 fresh 交叉）
- 过渡：170 根单边 + 30 根急回（21×55 fresh 交叉但宏观逆侧 → 位置定语义）
"""
import numpy as np

from app.services.strategy.ema import calc_ema
from app.services.strategy.technical import (
    CROSS_FRESH_BARS,
    HTF_SHORT,
    STATE_BLOCK_BOTH,
    STATE_LABELS,
    STATE_LOCK_CONTRA,
    STATE_QUALITY,
    STATE_TRANSITION,
    _STATE_DIR,
    _STATE_MACRO,
    _ema_state,
    _htf_cross_age,
    analyze_htf_background,
    compute_quality_score,
    enforce_htf_constraint,
    render_indicator_block,
)


def _kl_from_closes(closes) -> list[list]:
    """最小 4h K线序列（o=h=l=c，逐根等距；analyze_adx 以 close 差分作 DM）"""
    return [
        [i * 4 * 3600_000, c, c, c, float(c), 1000.0]
        for i, c in enumerate(closes)
    ]


def _kl(closes, wick: float) -> list[list]:
    """带影线 4h K线（TR=影线范围，供 ATR 计算与价距 ATR 注记用）"""
    return [
        [i * 4 * 3600_000, c, c + wick * (1 + 0.1 * (i % 3)),
         c - wick * (1 + 0.1 * (i % 3)), float(c), 1000.0]
        for i, c in enumerate(closes)
    ]


def _ramp(n: int, drift: float, base: float) -> list[float]:
    return [base + drift * i for i in range(n)]


def _shape(seg) -> list[float]:
    """分段走势构造：[(根数, 每根漂移), ...] 从 100 起，末段补平至 200 根"""
    out, p = [], 100.0
    for n, d in seg:
        out += [p + d * i for i in range(n)]
        p += d * n
    return (out + [out[-1]] * 300)[:200]


# ── 夹具（参数扫描定标，11/11 命中）──────────────────────────────
_TANGLE_FLAT = _kl(_shape([(200, 0.002)]), 2.0)
_TANGLE_WHIPSAW = _kl([100 + 4 * np.sin(i / 2.5) for i in range(200)], 2.0)
_ALIGNED_BULL = _kl(_shape([(140, 0.0), (60, 0.5)]), 2.5)
_ALIGNED_BEAR = _kl(_shape([(140, 0.0), (60, -0.5)]), 2.5)
_TURN_BULL = _kl(_shape([(140, 0.3), (40, 0.5), (10, -1.5)]), 2.0)
_TURN_BEAR = _kl(_shape([(140, -0.3), (40, -0.5), (10, 1.5)]), 2.0)
_MACRO_CROSS_BULL = _kl(_shape([(154, -0.5), (46, 2.0)]), 2.0)
_MACRO_CROSS_BEAR = _kl(_shape([(154, 0.5), (46, -2.0)]), 2.0)
_BULL_RECOVER = _kl(_shape([(160, 0.4), (18, -2.0), (22, 2.5)]), 2.0)
_BEAR_RECOVER = _kl(_shape([(160, -0.4), (18, 2.0), (22, -2.5)]), 2.0)
_BULL_DIP = _kl(_shape([(170, 0.8), (30, -2.0)]), 2.0)
_BEAR_RALLY = _kl(_shape([(170, -0.8), (30, 2.0)]), 2.0)

_STATE_FIXTURES = {
    "tangle": _TANGLE_FLAT,
    "turn_bull": _TURN_BULL, "turn_bear": _TURN_BEAR,
    "macro_cross_bull": _MACRO_CROSS_BULL, "macro_cross_bear": _MACRO_CROSS_BEAR,
    "bull_recover": _BULL_RECOVER, "bear_recover": _BEAR_RECOVER,
    "aligned_bull": _ALIGNED_BULL, "aligned_bear": _ALIGNED_BEAR,
    "bull_dip": _BULL_DIP, "bear_rally": _BEAR_RALLY,
}


def _kl_1h_flat() -> list[list]:
    """1h 侧最小序列（结构化行渲染仅要求非空指标结果）"""
    return _kl_from_closes(_ramp(60, 0.5, 100.0))


# ── 分类：缠绕 tangle（三道判据）────────────────────────────────


def test_tangle_by_spread():
    """横盘微涨：21/55 间距 <0.1% → 缠绕（第二道）；dir=0 不投票、
    macro_aligned=None 未评估（禁开态不评宏观）"""
    r = analyze_htf_background(_TANGLE_FLAT)
    assert r is not None
    assert r["state"] == "tangle"
    assert r["state_label"] == STATE_LABELS["tangle"]
    assert r["dir"] == 0 and r["macro_aligned"] is None
    assert "21/55间距<0.1% 纠缠" in r["detail"]


def test_tangle_by_adx():
    """正弦锯齿：ADX<20 → 缠绕（第一道），交叉年龄照常作注记"""
    r = analyze_htf_background(_TANGLE_WHIPSAW)
    assert r is not None
    assert r["state"] == "tangle"
    assert "无趋势" in r["detail"]           # ADX 道
    assert "55×144金叉后" in r["detail"]     # 注记仍补交叉年龄


def test_tangle_whipsaw_third_gate():
    """锯齿道（第三道）纯函数直测：ADX≥30 绕过第一道、间距 4% 绕过第二道，
    近 10 根 21×55 交叉 9 次 ≥2 → tangle"""
    n = 40
    s21 = np.where(np.arange(n) % 2 == 0, 104.0, 96.0)   # 隔根穿越
    state, notes, _ = _ema_state(
        np.full(n, 100.0), s21, np.full(n, 100.0), np.full(n, 60.0), 30.0, 2.0)
    assert state == "tangle"
    assert "近10根21×55交叉9次（锯齿）" in notes[0]


# ── 分类：排列稳态 aligned_* ────────────────────────────────────


def test_aligned_bull():
    """140 根盘整基座 + 60 根缓涨：全多头排列稳态；21×55 金叉已老（57 根）
    → 非 recover；div（排列角度）退役为纯注记证据"""
    r = analyze_htf_background(_ALIGNED_BULL)
    assert r is not None
    assert r["state"] == "aligned_bull"
    assert r["state_label"] == "多头排列"
    assert r["dir"] == 1 and r["macro_aligned"] is True
    assert r["age_bars"] == 57
    assert "21×55金叉后57根" in r["detail"]
    assert "排列角度" in r["detail"] and "仅注记" in r["detail"]


def test_aligned_bear():
    """镜像空头排列：21×55 全程无交叉（极老趋势）"""
    r = analyze_htf_background(_ALIGNED_BEAR)
    assert r is not None
    assert r["state"] == "aligned_bear" and r["dir"] == -1
    assert r["macro_aligned"] is True
    assert "21×55窗口内无交叉（极老趋势）" in r["detail"]


# ── 分类：拐头 turn_*（连续 2 根斜率反向确认）────────────────────


def test_turn_bull():
    """多头排列内 EMA21 斜率连续 2 根下行 → turn_bull（警戒禁开）；
    macro_aligned=None（拐头态不评宏观）"""
    r = analyze_htf_background(_TURN_BULL)
    assert r is not None
    assert r["state"] == "turn_bull"
    assert r["dir"] == 1 and r["macro_aligned"] is None
    assert "EMA21斜率连续2根下行（排列内拐头）" in r["detail"]


def test_turn_bear():
    """镜像：空头排列内连续 2 根上行 → turn_bear"""
    r = analyze_htf_background(_TURN_BEAR)
    assert r is not None
    assert r["state"] == "turn_bear" and r["dir"] == -1
    assert "EMA21斜率连续2根上行（排列内拐头）" in r["detail"]


def test_turn_needs_2_consecutive_slope_bars():
    """拐头确认边界（纯函数直测）：全多头排列内 EMA21 仅末 1 根斜率反向
    → 未达 TURN_CONFIRM_BARS，仍落 aligned_bull（单根翻向是噪声，同日
    衰竭期退役教训）；补足第 2 根反向 → turn_bull"""
    n = 200
    s144 = np.full(n, 100.0)
    s55 = np.full(n, 110.0)
    s21 = np.concatenate([np.linspace(120, 122, n - 3), [124.0, 125.0, 124.5]])
    # 末三根 124→125→124.5（倒V）：slope_now=-0.5<0、slope_prev=+1.0>0，仅 1 根反向
    state, _, _ = _ema_state(np.full(n, 130.0), s21, s55, s144, 30.0, 2.0)
    assert state == "aligned_bull"
    s21[-2:] = [123.5, 123.0]   # 末三根 124→123.5→123 递减：连续 2 根下行
    state2, notes2, _ = _ema_state(np.full(n, 130.0), s21, s55, s144, 30.0, 2.0)
    assert state2 == "turn_bull"
    assert "连续2根下行" in notes2[0]


# ── 分类：宏观交叉初期 macro_cross_* ────────────────────────────


def test_macro_cross_bull():
    """深V：154 根缓跌 + 46 根急拉 → 55×144 金叉初期（6 根）；
    趋势级事件独占判据位（detail 不重复补 55×144 年龄），21×55 年龄照常补注"""
    r = analyze_htf_background(_MACRO_CROSS_BULL)
    assert r is not None
    assert r["state"] == "macro_cross_bull"
    assert r["state_label"] == "宏观金叉初期"
    assert r["dir"] == 1 and r["macro_aligned"] is True
    assert r["macro_cross_age"] == 6
    assert "55×144金叉后6根，价距144约" in r["detail"]
    assert "21×55金叉后30根" in r["detail"]
    assert r["detail"].count("55×144") == 1   # 判据年龄不重复


def test_macro_cross_bear():
    """倒V镜像：55×144 死叉初期 → macro_cross_bear"""
    r = analyze_htf_background(_MACRO_CROSS_BEAR)
    assert r is not None
    assert r["state"] == "macro_cross_bear" and r["dir"] == -1
    assert r["macro_aligned"] is True
    assert "55×144死叉后6根" in r["detail"]


def test_macro_cross_fresh_boundary_age10():
    """初期窗口严格小于边界：55×144 金叉后恰好 CROSS_FRESH_BARS 根
    → 不再算初期，落排列稳态（年龄降级为注记）"""
    r = analyze_htf_background(_kl(_shape([(150, -0.5), (50, 2.0)]), 2.0))
    assert r is not None
    assert r["state"] == "aligned_bull"
    assert r["macro_cross_age"] == CROSS_FRESH_BARS
    assert "55×144金叉后10根" in r["detail"]


def test_macro_cross_priority_over_recover():
    """深V双叉同步（纯函数直测）：21×55 与 55×144 同时 fresh 金叉
    → 55×144 趋势级事件优先，不误判 bull_recover"""
    n = 200
    s144 = np.full(n, 100.0)
    s55 = np.array([98.0] * 195 + [98.5, 99.0, 99.8, 100.2, 101.0])
    s21 = np.array([96.0] * 195 + [98.0, 100.0, 101.5, 102.5, 103.0])
    # 55×144 金叉龄 1、21×55 金叉龄 3，均在初期窗口内
    state, notes, meta = _ema_state(np.full(n, 105.0), s21, s55, s144, 30.0, 2.0)
    assert state == "macro_cross_bull"
    assert "55×144金叉后1根" in notes[0]
    assert meta["age_m"] == 1 and meta["age_c"] == 3


# ── 分类：恢复初期 *_recover（位置语义：宏观同侧的新交叉）────────


def test_bull_recover():
    """160 根慢涨 + 18 根回踩 + 22 根回升：21×55 金叉 fresh（6 根）∧
    宏观多头（55>144）→ bull_recover（锁逆势档）"""
    r = analyze_htf_background(_BULL_RECOVER)
    assert r is not None
    assert r["state"] == "bull_recover"
    assert r["state_label"] == "多头恢复初期"
    assert r["dir"] == 1 and r["macro_aligned"] is True
    assert r["age_bars"] == 6
    assert "21×55金叉后6根（宏观多头侧恢复），价距144约" in r["detail"]
    assert r["detail"].count("21×55") == 1   # 判据年龄不重复


def test_bear_recover():
    """镜像：21×55 死叉 fresh ∧ 宏观空头 → bear_recover"""
    r = analyze_htf_background(_BEAR_RECOVER)
    assert r is not None
    assert r["state"] == "bear_recover" and r["dir"] == -1
    assert r["macro_aligned"] is True
    assert "21×55死叉后6根（宏观空头侧恢复）" in r["detail"]


# ── 分类：过渡态 bull_dip / bear_rally（位置语义：宏观逆侧）──────


def test_bull_dip():
    """170 根急涨 + 30 根急跌：21×55 死叉 fresh（9 根）但宏观多头
    （55>144）→ 同一 fresh 死叉在宏观多头侧=牛内回调（过渡态放行），
    不是 bear_recover——状态名必须编码位置"""
    r = analyze_htf_background(_BULL_DIP)
    assert r is not None
    assert r["state"] == "bull_dip"
    assert r["state_label"] == "牛内回调"
    assert r["dir"] == -1 and r["macro_aligned"] is False
    assert "21×55死叉后9根" in r["detail"]


def test_bear_rally():
    """镜像：21×55 金叉 fresh ∧ 宏观空头 → 熊内反弹（过渡态放行）"""
    r = analyze_htf_background(_BEAR_RALLY)
    assert r is not None
    assert r["state"] == "bear_rally" and r["dir"] == 1
    assert r["macro_aligned"] is False
    assert "21×55金叉后9根" in r["detail"]


# ── _htf_cross_age 纯函数 ───────────────────────────────────────


def test_cross_age_helper():
    """_htf_cross_age：最后一次交叉的根数与方向"""
    f = np.array([1.0, 2.0, 3.0, 2.0, 1.0])  # 在 idx1→2 间上穿、3→4 下穿
    m = np.array([2.0, 2.0, 2.0, 2.0, 2.0])
    age, d = _htf_cross_age(f, m)
    assert age == 1 and d == -1  # 最后一次是下穿，距今 1 根


def test_no_cross_fallback():
    """序列内无交叉：age=None、按当前排列定方向"""
    f = np.array([3.0, 4.0, 5.0])
    m = np.array([2.0, 2.0, 2.0])
    age, d = _htf_cross_age(f, m)
    assert age is None and d == 1


def test_cross_age_ignores_nan_seed_boundary():
    """慢线 EMA SMA 种子位之前 diff 为 NaN：NaN→有效值边界不得误判为交叉
    （300 根匀速涨全程 EMA21>EMA55，旧实现会在种子位记一次幻影金叉）"""
    closes = _ramp(300, 0.5, 100.0)
    kls = _kl_from_closes(closes)
    arr = np.array([float(k[4]) for k in kls[:-1]], dtype=float)
    f = calc_ema(arr, 21)
    m = calc_ema(arr, 55)
    age, d = _htf_cross_age(f, m)
    assert age is None and d == 1


# ── 硬约束锁单矩阵（常量驱动全枚举）─────────────────────────────


def test_enforce_block_both_matrix():
    """缠绕/拐头（STATE_BLOCK_BOTH 3 态）× 开多/开空 → 一律 skip：
    skip_reason 带状态约束语义与原因、htf_phase stash 状态 key、veto 快照"""
    for st in STATE_BLOCK_BOTH:
        _why = "缠绕无趋势" if st == "tangle" else "排列内拐头，趋势衰竭警戒"
        for direction in ("long", "short"):
            d = {"trade_decision": "suggest", "direction": direction}
            out = enforce_htf_constraint(d, _STATE_FIXTURES[st])
            assert out["trade_decision"] == "skip", (st, direction)
            assert "状态约束：4h" in out["skip_reason"], (st, direction)
            assert _why in out["skip_reason"], (st, direction)
            assert "双向禁开新仓" in out["skip_reason"]
            assert out["htf_phase"] == st
            assert out["veto_original"]["source"] == "htf_constraint"


def test_enforce_lock_contra_matrix():
    """宏观同侧态（STATE_LOCK_CONTRA 6 态）：逆势 skip（方向约束）、
    顺势放行；两种结果都 stash htf_phase 供复盘分组"""
    for st in STATE_LOCK_CONTRA:
        htf_dir = _STATE_DIR[st]
        contra = "short" if htf_dir == 1 else "long"
        along = "long" if htf_dir == 1 else "short"
        out = enforce_htf_constraint(
            {"trade_decision": "suggest", "direction": contra},
            _STATE_FIXTURES[st])
        assert out["trade_decision"] == "skip", st
        assert "方向约束：4h" in out["skip_reason"], st
        assert f"禁止开{'空' if contra == 'short' else '多'}" in out["skip_reason"]
        assert out["htf_phase"] == st
        assert out["veto_original"]["source"] == "htf_constraint"
        out2 = enforce_htf_constraint(
            {"trade_decision": "suggest", "direction": along},
            _STATE_FIXTURES[st])
        assert out2["trade_decision"] == "suggest", st
        assert "veto_original" not in out2
        assert out2["htf_phase"] == st   # 放行同样 stash


def test_enforce_transition_always_allows():
    """过渡态（bull_dip/bear_rally）双向放行：宏观逆侧不锁，质量分兜底"""
    for st in ("bull_dip", "bear_rally"):
        for direction in ("long", "short"):
            out = enforce_htf_constraint(
                {"trade_decision": "suggest", "direction": direction},
                _STATE_FIXTURES[st])
            assert out["trade_decision"] == "suggest", (st, direction)
            assert "veto_original" not in out
            assert out["htf_phase"] == st


def test_enforce_transition_pbc_shadow_veto():
    """影子 A/B（2026-10-20）：过渡态 × 回踩确认 → 程序 veto 不进真单；
    veto_original 三价快照保留 + ab_bucket 标记；非回踩确认仍放行。"""
    for st in STATE_TRANSITION:
        d = {"trade_decision": "suggest", "direction": "long", "entry_price": 100,
             "stop_loss": 98, "take_profit_1": 104, "take_profit_2": 108,
             "risk_reward_ratio": 2.0, "recommendation": 70}
        out = enforce_htf_constraint(
            d, _STATE_FIXTURES[st], "trigger_pullback_confirm")
        assert out["trade_decision"] == "skip", st
        assert out["htf_phase"] == st
        assert "回踩确认" in out["skip_reason"] and "影子 A/B" in out["skip_reason"]
        v = out["veto_original"]
        assert v["source"] == "htf_constraint" and v["ab_bucket"] == "transition_pbc"
        assert v["entry_price"] == 100 and v["stop_loss"] == 98
        assert v["take_profit_1"] == 104 and v["recommendation"] == 70


def test_enforce_transition_non_pbc_still_allows():
    """过渡态非回踩确认信号（突破型）仍放行；缺 signal_type 亦放行
    （向后兼容：不透传时退回原「过渡态一律放行」口径）"""
    for st in STATE_TRANSITION:
        for sig in ("trigger_breakout", "trigger_trend_breakout", None):
            out = enforce_htf_constraint(
                {"trade_decision": "suggest", "direction": "long"},
                _STATE_FIXTURES[st], sig)
            assert out["trade_decision"] == "suggest", (st, sig)
            assert "veto_original" not in out
            assert out["htf_phase"] == st


def test_enforce_passthrough_no_htf_or_not_suggest():
    """非 suggest / 无 4h 数据原样返回，不 stash htf_phase"""
    d = {"trade_decision": "skip", "direction": "long"}
    assert enforce_htf_constraint(d, _ALIGNED_BULL) is d
    assert "htf_phase" not in d
    d2 = {"trade_decision": "suggest", "direction": "long"}
    assert enforce_htf_constraint(d2, None) is d2
    assert enforce_htf_constraint(d2, []) is d2
    assert "htf_phase" not in d2


# ── 质量分：STATE_QUALITY 矩阵 ──────────────────────────────────


def _votes(state=None, phase=None, htf_dir=None, macro=None) -> dict:
    """构造 votes 快照（与 scanner._votes_snapshot 的 4h 票同构）；
    phase 非 None 时模拟退役旧档存量行（无 state 键）"""
    if phase is not None:
        htf = {"phase": phase, "dir": htf_dir if htf_dir is not None else 1,
               "macro_aligned": macro if macro is not None else True}
    else:
        htf = {"state": state, "dir": _STATE_DIR[state],
               "macro_aligned": _STATE_MACRO[state]}
    return {
        "net": 3,
        "results": {HTF_SHORT: htf, "ADX": None, "RSI": None, "OBV": None},
    }


def test_quality_matrix_all_states():
    """11 态质量分矩阵全枚举：同向加 same_mod / 逆向加 counter_mod；
    tangle dir=0 不进方向分支（无状态分、无宏观罚）"""
    for st, (same_mod, counter_mod) in STATE_QUALITY.items():
        d = _STATE_DIR[st]
        if d == 0:
            s = compute_quality_score(1, _votes(st), "trigger_pullback_confirm")
            assert s == 50 + 1 * 3 * 10, st
            continue
        base = 50 + d * 3 * 10
        # 过渡态 macro_aligned=False：同/逆都带逆宏观 -4（排列/交叉态恒 True 不触发）
        extra = -4 if _STATE_MACRO[st] is False else 0
        s_same = compute_quality_score(d, _votes(st), "trigger_pullback_confirm")
        assert s_same == base + same_mod + extra, st
        # 逆向：direction=-d，基础分按逆向重算（同 -d 方向的票数乘积）
        s_counter = compute_quality_score(-d, _votes(st), "trigger_pullback_confirm")
        assert s_counter == 50 - d * 3 * 10 + counter_mod + extra, st


def test_quality_transition_macro_penalty():
    """逆宏观 -4 天然只作用过渡态（macro_aligned=False）：牛内回调做空 /
    熊内反弹做多压分；排列态恒同侧不触发"""
    s_dip = compute_quality_score(-1, _votes("bull_dip"), "trigger_pullback_confirm")
    assert s_dip == 50 - 1 * 3 * 10 + 5 - 4
    s_rally = compute_quality_score(1, _votes("bear_rally"), "trigger_pullback_confirm")
    assert s_rally == 50 + 1 * 3 * 10 + 5 - 4
    s_aligned = compute_quality_score(1, _votes("aligned_bull"), "trigger_pullback_confirm")
    assert s_aligned == 50 + 1 * 3 * 10 + 10


def test_quality_legacy_phase_fallback():
    """存量旧行 votes 快照无 state 键（phase=退役旧档）：走中性兜底
    （同向 +5 / 逆向 -8）；旧 phase 键兼容读取（state or phase）"""
    s = compute_quality_score(1, _votes(phase="strong"), "trigger_pullback_confirm")
    assert s == 50 + 1 * 3 * 10 + 5
    s2 = compute_quality_score(-1, _votes(phase="strong"), "trigger_pullback_confirm")
    assert s2 == 50 - 1 * 3 * 10 - 8
    s3 = compute_quality_score(
        1, _votes(phase="overheat", macro=False), "trigger_pullback_confirm")
    assert s3 == 50 + 1 * 3 * 10 + 5 - 4   # 旧逆宏观行罚分照旧


# ── 渲染行（状态约束 / 方向约束 / 结构化行）──────────────────────


def test_render_state_constraint_line():
    """禁开态（tangle/turn_*）渲染「状态约束（硬性）」双向禁开行 +
    结构化行标注宏观未评估；不渲染方向约束行"""
    for st in ("tangle", "turn_bull"):
        block = render_indicator_block(
            _kl_1h_flat(), htf_klines=_STATE_FIXTURES[st])
        assert "状态约束（硬性）" in block, st
        assert "双向禁开新仓" in block, st
        assert "方向约束（硬性）" not in block, st
        assert f"- 4h背景: state={st}（双向禁开，宏观对齐未评估）" in block, st
        assert "phase=" not in block   # phase 键已退役


def test_render_direction_constraint_line():
    """宏观同侧态（aligned_bull）渲染「方向约束（硬性）」仅可 long 行 +
    结构化行 macro=同侧；不渲染状态约束行"""
    block = render_indicator_block(_kl_1h_flat(), htf_klines=_ALIGNED_BULL)
    assert "方向约束（硬性）" in block
    assert "仅可 long，开空一律 skip" in block
    assert "状态约束（硬性）" not in block
    assert "- 4h背景: state=aligned_bull macro=同侧" in block


def test_render_transition_no_constraint_line():
    """过渡态（bull_dip）无任何硬约束行，结构化行 macro=逆侧"""
    block = render_indicator_block(_kl_1h_flat(), htf_klines=_BULL_DIP)
    assert "约束（硬性）" not in block
    assert "- 4h背景: state=bull_dip macro=逆侧" in block
