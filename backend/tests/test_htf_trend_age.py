"""4h 背景排列强度分类测试（2026-10-10 五次拍板改口径）

分类主轴从「EMA21/55 交叉后根数四档」换成「EMA 排列角度（发散度）五档」：
|EMA21-55|/ATR 与 |EMA55-144|/ATR 双线取小者——weak <0.5 / medium <1.0 /
strong <2.0 / extreme ≥2.0；衰竭期=MACD 柱线翻向（凌驾于强度档，但交叉
≤10 根的太新背离不判衰竭）；age_bars 仅作 detail 注记。
宏观守卫 EMA55 vs EMA144（逆宏观=熊内反弹/牛内回调）不变。
硬约束锁单矩阵：衰竭期最先判双向禁开新单；中等/强/极强 ∧ 宏观同侧 →
锁逆势；弱排列/逆宏观 → 放行。
"""
import numpy as np

from app.services.strategy.ema import calc_ema
from app.services.strategy.technical import (
    HTF_SHORT,
    _htf_cross_age,
    _strength_phase,
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


def _ramp(n: int, drift: float, base: float) -> list[float]:
    return [base + drift * i for i in range(n)]


# ── 分类：排列强度五档 ──


def test_extreme_bullish_classification():
    """跌 40 根后涨 160 根：排列角度 ~16.9 ATR ≥ 2.0 → 排列极强多头，宏观同侧"""
    closes = _ramp(40, -1.0, 100.0) + _ramp(160, 2.0, 60.0)
    r = analyze_htf_background(_kl_from_closes(closes))
    assert r is not None
    assert r["state"] == "bullish" and r["dir"] == 1
    assert r["phase"] == "extreme"
    assert r["age_bars"] is not None and r["age_bars"] > 30  # age 仅作注记
    assert r["macro_aligned"] is True
    assert "排列极强多头背景" in r["state_label"]
    assert "金叉" in r["detail"] and "EMA144" in r["detail"]
    assert "排列角度" in r["detail"]  # 强度分档证据注记


def test_strong_bullish_bear_rally():
    """跌 150 根后反弹 18 根：排列角度 ~1.94 ATR → 排列强；EMA55 仍 < EMA144 → 逆宏观熊内反弹
    （该构造下 EMA21/55 交叉发生在反弹第 ~14 根，age = 反弹根数 - 14，仅作注记）"""
    closes = _ramp(150, -0.5, 100.0) + _ramp(18, 3.0, 25.0)
    r = analyze_htf_background(_kl_from_closes(closes))
    assert r is not None
    assert r["state"] == "bullish"
    assert r["phase"] == "strong"
    assert r["age_bars"] == 4
    assert r["macro_aligned"] is False
    assert "熊内反弹" in r["detail"]


def test_strong_bullish_bear_rally_longer():
    """跌 150 根后反弹 28 根：排列角度 ~1.40 ATR 仍落 strong 档（1.0≤div<2.0），逆宏观不变"""
    closes = _ramp(150, -0.5, 100.0) + _ramp(28, 3.0, 25.0)
    r = analyze_htf_background(_kl_from_closes(closes))
    assert r is not None
    assert r["state"] == "bullish" and r["phase"] == "strong"
    assert r["age_bars"] == 14
    assert r["macro_aligned"] is False


def test_bear_exhausted_before_recross():
    """跌 150 根后反弹 12 根：EMA 尚未金叉（仍空头排列）但柱线已翻多 → 衰竭期空头
    （动能先于均线死亡；MACD 数值零保护不误判）"""
    closes = _ramp(150, -0.5, 100.0) + _ramp(12, 3.0, 25.0)
    r = analyze_htf_background(_kl_from_closes(closes))
    assert r is not None
    assert r["state"] == "bearish" and r["phase"] == "exhausted"
    assert r["momentum"] == "opposite"
    assert r["macro_aligned"] is True  # 空头顺势侧 = EMA55<EMA144，长期下跌后成立
    assert "衰竭期空头背景" in r["state_label"]


# ── 三线堆叠位置注记（6 种排列全枚举；行为只读 macro_aligned 二值，注记细化）──


def test_v_recovery_strong_annotation():
    """长跌后急弹：EMA21 已上穿 144、EMA55 未跟上 → V型反转注记（逆宏观，不锁单）。
    排列角度 ~1.18 ATR → strong（1.0≤div<2.0）；金叉 8 根柱线未翻 → 不判衰竭"""
    closes = _ramp(150, -0.5, 100.0) + _ramp(18, 6.0, 25.0)
    r = analyze_htf_background(_kl_from_closes(closes))
    assert r is not None
    assert r["state"] == "bullish" and r["phase"] == "strong"
    assert r["macro_aligned"] is False
    assert "V型反转中期（EMA21已上穿144，EMA55未跟上）" in r["detail"]
    assert "排列角度" in r["detail"]  # 强度分档证据注记


def test_full_stack_annotation():
    """长涨后急杀 10 根内死叉前：三线多头排列（宏观顺势）注记"""
    closes = _ramp(165, 0.5, 100.0) + _ramp(4, -2.0, 182.5)
    r = analyze_htf_background(_kl_from_closes(closes))
    assert r is not None
    assert r["state"] == "bullish"
    assert r["macro_aligned"] is True
    assert "三线多头排列" in r["detail"]


def test_bull_correction_weak_annotation():
    """长涨后急杀：EMA21 下穿 EMA55 但未破 144（144 最下方托底）→ 牛内回调注记。
    排列角度 ~0.41 ATR < 0.5 → weak（排列未证明，锁单矩阵放行侧，质量分兜底）"""
    closes = _ramp(150, 0.5, 100.0) + _ramp(14, -3.0, 175.0)
    r = analyze_htf_background(_kl_from_closes(closes))
    assert r is not None
    assert r["state"] == "bearish" and r["phase"] == "weak"
    assert r["macro_aligned"] is False
    assert "牛内回调（144托底中·下行）" in r["detail"]


def test_constraint_unlocks_v_recovery_counter_short():
    """V型反转中期（逆宏观）开空 → 放行：宏观确认以 EMA55 上穿 144 为准，
    21 单线收复 144 不改变二值行为（注记层给 AI 细分语境）"""
    closes = _ramp(150, -0.5, 100.0) + _ramp(18, 6.0, 25.0)
    d = {"trade_decision": "suggest", "direction": "short"}
    out = enforce_htf_constraint(d, _kl_from_closes(closes))
    assert out["trade_decision"] == "suggest"


def test_exhausted_bullish():
    """长涨后急跌 10 根：EMA 排列仍多头但 MACD 柱线翻负 → 衰竭期（动能先死）"""
    closes = _ramp(165, 1.5, 100.0) + _ramp(10, -8.0, 347.5)
    r = analyze_htf_background(_kl_from_closes(closes))
    assert r is not None
    assert r["state"] == "bullish"
    assert r["phase"] == "exhausted"
    assert r["momentum"] == "opposite"
    assert r["macro_aligned"] is True
    assert "柱线已翻向" in r["detail"]


def test_same_direction_shrinking_note():
    """冲高后降速不翻向：柱线仍同号但连续收窄 → 动能衰减注记带「仍同向」字样
    （BTWUSDT 2026-10-10：AI 把裸「动能衰减」误读成动能不同向而 skip，
    盈亏比取舍条款的「动能同向」以柱线翻向为准，减速不算不同向）"""
    closes = _ramp(150, 0.5, 100.0) + _ramp(6, 2.5, 175.0) + _ramp(8, 1.2, 190.0)
    r = analyze_htf_background(_kl_from_closes(closes))
    assert r is not None
    assert r["state"] == "bullish"
    assert r["momentum"] == "same"  # 未翻向
    assert r["phase"] != "exhausted"
    assert "动能衰减" in r["detail"] and "仍同向" in r["detail"]


# ── 盈亏比取舍提示行注入（BTWUSDT 个案回归）──


def _kl_1h_stub(n: int = 60) -> list[list]:
    """render_indicator_block 的 1h K线替身（平坦，指标全部弃权不影响注入行）"""
    return [
        [i * 3_600_000, 100.0, 101.0, 99.0, 100.0, 10.0, i * 3_600_000 + 3_599_000, 1000.0]
        for i in range(n)
    ]


def test_rr_tradeoff_hint_injected_for_strong_trend():
    """排列极强 + 柱线同向（含动能衰减）→ 程序侧注入「盈亏比取舍（可用）」行，
    不再依赖 LLM 自查散文条款（2026-10-10 21:36 分析仍保守 skip 的修复）"""
    closes = _ramp(150, 0.5, 100.0) + _ramp(6, 2.5, 175.0) + _ramp(8, 1.2, 190.0)
    htf = _kl_from_closes(closes)
    r = analyze_htf_background(htf)
    assert r["phase"] in ("medium", "strong", "extreme") and r["momentum"] == "same"
    block = render_indicator_block(_kl_1h_stub(), htf_klines=htf)
    assert "盈亏比取舍（可用）" in block
    assert "不要直接 skip" in block


def test_rr_tradeoff_hint_not_injected_for_exhausted():
    """柱线已翻向（衰竭期）→ 不注入（维持 skip 路径，且方向约束已禁开单）"""
    closes = _ramp(165, 1.5, 100.0) + _ramp(10, -8.0, 347.5)
    htf = _kl_from_closes(closes)
    assert analyze_htf_background(htf)["momentum"] == "opposite"
    block = render_indicator_block(_kl_1h_stub(), htf_klines=htf)
    assert "盈亏比取舍" not in block


def test_rr_tradeoff_hint_not_injected_for_weak():
    """弱排列（牛内回调）→ 不注入（盈亏比不达标仍应 skip）"""
    closes = _ramp(150, 0.5, 100.0) + _ramp(14, -3.0, 175.0)
    htf = _kl_from_closes(closes)
    assert analyze_htf_background(htf)["phase"] == "weak"
    block = render_indicator_block(_kl_1h_stub(), htf_klines=htf)
    assert "盈亏比取舍" not in block


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


def test_extreme_old_trend_annotation():
    """窗口内无交叉的极老趋势：age=None → 按当前排列强度处理（~17 ATR → extreme），
    注记区分两种 age=None 成因（极老趋势 vs 交叉即回穿），不再共用错注记"""
    r = analyze_htf_background(_kl_from_closes(_ramp(300, 0.5, 100.0)))
    assert r is not None
    assert r["state"] == "bullish" and r["phase"] == "extreme"
    assert r["age_bars"] is None
    assert "窗口内无交叉（极老趋势，按当前排列强度处理）" in r["detail"]


# ── _strength_phase 纯函数边界 ──


def test_strength_phase_boundaries():
    """排列角度 → 强度档边界：None 归 medium（角度不可测兜底）；阈值左开右闭"""
    assert _strength_phase(None) == "medium"
    assert _strength_phase(0.0) == "weak"
    assert _strength_phase(0.49) == "weak"
    assert _strength_phase(0.5) == "medium"
    assert _strength_phase(0.99) == "medium"
    assert _strength_phase(1.0) == "strong"
    assert _strength_phase(1.99) == "strong"
    assert _strength_phase(2.0) == "extreme"


# ── 硬约束锁单矩阵 ──


def test_constraint_locks_extreme_aligned():
    """极强多头（宏观同侧）+ AI 开空 → 强制 skip（过热未衰照样杀逆势）"""
    closes = _ramp(40, -1.0, 100.0) + _ramp(160, 2.0, 60.0)
    d = {"trade_decision": "suggest", "direction": "short"}
    out = enforce_htf_constraint(d, _kl_from_closes(closes))
    assert out["trade_decision"] == "skip"
    assert "排列极强" in out["skip_reason"]
    assert out["veto_original"]["source"] == "htf_constraint"
    assert out["htf_phase"] == "extreme"  # stash 落 AIAnalysis.htf_phase 供复盘分组


def test_constraint_blocks_exhausted_counter():
    """衰竭期多头（柱线已翻）+ AI 开空 → 双向禁开新矩阵（2026-10-09 r2）：
    强制 skip + veto 快照 + htf_phase stash（顺势侧放行被本矩阵反转）"""
    closes = _ramp(165, 1.5, 100.0) + _ramp(10, -8.0, 347.5)
    d = {"trade_decision": "suggest", "direction": "short"}
    out = enforce_htf_constraint(d, _kl_from_closes(closes))
    assert out["trade_decision"] == "skip"
    assert "衰竭期" in out["skip_reason"] and "禁止开新单" in out["skip_reason"]
    assert out["veto_original"]["source"] == "htf_constraint"
    assert out["htf_phase"] == "exhausted"


def test_constraint_blocks_exhausted_aligned():
    """衰竭期空头（柱线已翻多）+ AI 顺势开多 → 同样禁开（双向，非仅锁逆势）"""
    closes = _ramp(150, -0.5, 100.0) + _ramp(12, 3.0, 25.0)
    d = {"trade_decision": "suggest", "direction": "long"}
    out = enforce_htf_constraint(d, _kl_from_closes(closes))
    assert out["trade_decision"] == "skip"
    assert "衰竭期" in out["skip_reason"]
    assert out["htf_phase"] == "exhausted"


def test_constraint_unlocks_strong_bear_rally():
    """强排列多头熊内反弹（逆宏观）+ AI 开空 → 放行（逆宏观任何强度不锁）。
    夹具=18 根反弹（角度 ~1.94 ATR → strong）；12 根版本实为衰竭期空头，
    r2 起衰竭期双向禁开，勿混用"""
    closes = _ramp(150, -0.5, 100.0) + _ramp(18, 3.0, 25.0)
    d = {"trade_decision": "suggest", "direction": "short"}
    out = enforce_htf_constraint(d, _kl_from_closes(closes))
    assert out["trade_decision"] == "suggest"


# ── 质量分按强度档映射 ──


def _votes(phase: str, macro=True, htf_dir=1) -> dict:
    return {
        "net": 3,
        "results": {
            HTF_SHORT: {"dir": htf_dir, "phase": phase, "macro_aligned": macro},
            "ADX": None, "RSI": None, "OBV": None,
        },
    }


def test_quality_score_same_direction_by_phase():
    """同向：弱+5 / 中+8 / 强+10 / 极强+6（过热降档）/ 衰竭-6（基础 71）"""
    base = 50 + 1 * 3 * 7
    cases = {"weak": 5, "medium": 8, "strong": 10, "extreme": 6, "exhausted": -6}
    for phase, mod in cases.items():
        s = compute_quality_score(1, _votes(phase), "trigger_pullback_confirm")
        assert s == base + mod, f"{phase}: {s} != {base + mod}"


def test_quality_score_counter_direction_by_phase():
    """逆向：弱-6 / 中-10 / 强-12 / 极强-8 / 衰竭-4"""
    base = 50 + (-1) * 3 * 7
    cases = {"weak": -6, "medium": -10, "strong": -12, "extreme": -8, "exhausted": -4}
    for phase, mod in cases.items():
        s = compute_quality_score(-1, _votes(phase), "trigger_pullback_confirm")
        assert s == base + mod, f"{phase}: {s} != {base + mod}"


def test_quality_score_macro_misaligned_penalty():
    """逆宏观额外 -4（硬约束不锁，分数兜底）"""
    s_aligned = compute_quality_score(1, _votes("strong"), "trigger_pullback_confirm")
    s_mis = compute_quality_score(
        1, _votes("strong", macro=False), "trigger_pullback_confirm")
    assert s_mis == s_aligned - 4
