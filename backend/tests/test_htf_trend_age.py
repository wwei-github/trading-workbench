"""4h 背景分级测试（2026-10-10 五次拍板建排列强度口径；2026-10-11 分级重构）

分级体系（判定优先级，docs/17 外部 4H 闸门方案第 2/3 层采纳）：
ranging(ADX<20) → mixed(EMA21/55 间距<0.1%) → overheat 过热期（三条件任一命中
当前根：EMA144 斜率 20 根>5% / |close−EMA144|>4×ATR / 加速比>2）→ cooling
冷却期（近 20 根命中过 ∧ 当前未命中，无状态回看实现非状态机）→ 结构 3 档
weak <0.5 / medium <1.0 / strong ≥1.0（extreme 档退役：过度张开语义由过热
三条件接管，div 原始值随 dict["div"] 保留供质量分降档；原 exhausted 档同日
早前退役）。宏观守卫 EMA55 vs EMA144（逆宏观=熊内反弹/牛内回调）不变。

硬约束锁单矩阵：过热/冷却 → 双向禁开（只管开仓，持仓出场不受影响）；
中等/强 ∧ 宏观同侧 → 锁逆势；弱排列/逆宏观 → 放行。

夹具构造说明：稳态线性趋势下「strong 且不过热」数学上近乎互斥
（div≥1 ⟺ ATR≤17d，距离过热豁免 ⟺ ATR≥dist/4≈17.9d）——平底线夹具任何
持续趋势都会落过热期。健康 strong 夹具用「盘整基座后起势」形态（A：
140 根横盘 + 60 根缓涨），逆宏观健康反弹用深跌后的温和反弹（B）。
"""
import numpy as np

from app.services.strategy.ema import calc_ema
from app.services.strategy.technical import (
    HTF_SHORT,
    _htf_cross_age,
    _overheat_reason,
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


def _kl(closes, wick: float) -> list[list]:
    """带影线 4h K线（TR=影线范围，供 ATR 计算与距离过热口径用）"""
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


# 健康夹具（2026-10-11 分级重构定标，均不过热）：
# A：strong·宏观同侧（140 根盘整基座 + 60 根缓涨，排列角度 1.25 ATR）
_STRONG_ALIGNED = _kl(_shape([(140, 0.0), (60, 0.5)]), 2.5)
# B：strong·逆宏观（深跌 140 根后温和反弹 36 根，熊内反弹·144 压制中）
_STRONG_BEAR_RALLY = _kl(_shape([(140, -0.5), (36, 0.7)]), 1.5)
# C：medium·宏观同侧（慢涨后更缓，排列角度 0.59 ATR）
_MEDIUM_ALIGNED = _kl(_shape([(150, 0.25), (30, 0.2)]), 2.0)
# D：斜率过热（先缓后急 20 根 @2.0，EMA144 20 根变化 >5%）
_OVERHEAT_SLOPE = _kl(_shape([(140, 0.3), (20, 2.0)]), 3.0)
# 冷却：过热命中后小步回落消化（距离过热命中于 2 根前）
_COOLING = _kl(_shape([(140, 0.3), (20, 2.0), (8, -1.0)]), 3.0)


# ── 分类：过热期/冷却期（2026-10-11 分级重构，凌驾结构档）──────────


def test_overheat_slope_classification():
    """跌 40 根后涨 160 根 @2.0：EMA144 斜率 20 根变化 >5% → 过热期（斜率过热）。
    原判 extreme（排列角度 16.9 ATR）——extreme 档退役后由过热三条件接管"""
    closes = _ramp(40, -1.0, 100.0) + _ramp(160, 2.0, 60.0)
    r = analyze_htf_background(_kl_from_closes(closes))
    assert r is not None
    assert r["state"] == "bullish" and r["dir"] == 1
    assert r["phase"] == "overheat"
    assert r["age_bars"] is not None and r["age_bars"] > 30  # age 仅作注记
    assert r["macro_aligned"] is True
    assert "过热期多头背景" in r["state_label"]
    assert "金叉" in r["detail"] and "EMA144" in r["detail"]
    assert "排列角度" in r["detail"]  # 结构档证据注记仍在（过热凌驾其上）
    assert "过热命中：斜率过热" in r["detail"]


def test_overheat_distance_classification():
    """300 根平底 ramp @0.5：div=17 ATR 但命中距离过热（|close−EMA144|>4×ATR）
    → 过热期。原判 extreme——价格脱离趋势结构（距离过热）取代过度张开分档"""
    r = analyze_htf_background(_kl_from_closes(_ramp(300, 0.5, 100.0)))
    assert r is not None
    assert r["state"] == "bullish" and r["phase"] == "overheat"
    assert r["age_bars"] is None
    assert "窗口内无交叉（极老趋势，按当前排列强度处理）" in r["detail"]
    assert "过热命中：距离过热" in r["detail"]


def test_overheat_accel_reason_pure_function():
    """加速过热（第三条件）纯函数单测：EMA144 口径下管线级构造几乎总被
    斜率/距离先命中（极慢均线的加速必伴随 20 根斜率扩张），合成序列直验。
    i=199：s_20=100.1 → s_5=100.475 爬 0.375（15 根），s_5 → s_now=104.2 爬
    3.725（5 根）→ accel≈9.9>2；斜率 3.94%<5%、价格贴合 EMA 距离 0 均不命中"""
    closes = np.full(200, 104.0)
    s144 = np.full(200, 100.0)
    s144[175:196] = np.linspace(100.0, 100.5, 21)   # 前 15+5 根缓爬
    s144[196:] = [101.1, 101.9, 102.9, 104.2]        # 末 5 根加速
    tr = [0.5] * 199
    assert _overheat_reason(closes, s144, tr, 199) == "加速过热"
    # 条件优先级：斜率命中时先返回斜率（同组数据抬尾至 20 根变化 6.7%>5%）
    s144_slope = s144.copy()
    s144_slope[199] = 107.0
    assert _overheat_reason(closes, s144_slope, tr, 199) == "斜率过热"
    # EMA144 有限区（SMA 种子 143 根）：i-20<143 不可判 → None
    assert _overheat_reason(closes, s144, tr, 150) is None


def test_cooling_state_lookback():
    """冷却期（外部 4H 闸门第 3 层，无状态回看实现）：过热命中后小步回落消化，
    当前根未命中 ∧ 窗口内（近 20 根）命中过 → cooling，证据注记带命中条件与
    距今根数；冷却期双向禁开：开多/开空一律 skip（持仓出场不受影响）"""
    r = analyze_htf_background(_COOLING)
    assert r is not None
    assert r["phase"] == "cooling"
    assert "冷却中（距离过热命中于2根前，消化窗口内）" in r["detail"]
    for direction in ("long", "short"):
        d = {"trade_decision": "suggest", "direction": direction}
        out = enforce_htf_constraint(d, _COOLING)
        assert out["trade_decision"] == "skip"
        assert "过热约束：4h冷却期（过热后获利盘消化中）双向禁开新仓" in out["skip_reason"]
        assert out["veto_original"]["source"] == "htf_constraint"


def test_overheat_gate_boundary_164_165():
    """EMA144 有限区（SMA 种子 143）+ 斜率 20 根回看 → len(closed)≥164 才可判
    过热：164 根输入（163 已收盘）不评估 → div=17 仍落 strong（extreme 退役的
    显式佐证：div≥2 未过热 = strong，降档语义只在质量分）；165 根起评估 →
    匀速陡涨命中斜率过热"""
    r164 = analyze_htf_background(_kl_from_closes(_ramp(164, 2.0, 100.0)))
    assert r164["phase"] == "strong" and (r164["div"] or 0) > 2.0
    r165 = analyze_htf_background(_kl_from_closes(_ramp(165, 2.0, 100.0)))
    assert r165["phase"] == "overheat" and "斜率过热" in r165["detail"]


# ── 分类：排列强度 3 档 ──────────────────────────────────────────


def test_strong_bullish_bear_rally():
    """深跌 140 根后温和反弹 36 根：排列角度 1.07 ATR → strong；EMA55 仍 < EMA144
    → 逆宏观熊内反弹（不过热：反弹温和，斜率/距离/加速三条件均未命中）。
    （原 18 根 @3.0 夹具触发斜率过热，healthy 形态改用 B 夹具；「更长反弹仍
    strong」的档位带宽由 _strength_phase 纯函数边界用例覆盖）"""
    r = analyze_htf_background(_STRONG_BEAR_RALLY)
    assert r is not None
    assert r["state"] == "bullish"
    assert r["phase"] == "strong"
    assert r["age_bars"] == 32
    assert r["macro_aligned"] is False
    assert "熊内反弹" in r["detail"]


def test_bear_histogram_flip_keeps_structural_phase():
    """跌 150 根后反弹 12 根：EMA 尚未金叉（仍空头排列）但柱线已翻多——
    衰竭期退役后（2026-10-11）动能翻向不再改变 phase，按排列角度分结构档，
    detail 无动能注记、label 无「衰竭期」"""
    closes = _ramp(150, -0.5, 100.0) + _ramp(12, 3.0, 25.0)
    r = analyze_htf_background(_kl_from_closes(closes))
    assert r is not None
    assert r["state"] == "bearish"
    assert r["phase"] in ("weak", "medium", "strong")
    assert r["macro_aligned"] is True  # 空头顺势侧 = EMA55<EMA144，长期下跌后成立
    assert "衰竭期" not in r["state_label"]
    assert "动能背离" not in r["detail"] and "柱线已翻向" not in r["detail"]


# ── 三线堆叠位置注记（6 种排列全枚举；行为只读 macro_aligned 二值，注记细化）──


def test_v_recovery_overheat_annotation():
    """长跌后急弹（18 根 @6.0）：EMA21 已上穿 144、EMA55 未跟上 → V型反转中期
    注记保留；急弹命中距离过热 → 过热期（原判 strong/排列角度 1.18 ATR 注记
    仍在 detail，但 phase 被过热凌驾）"""
    closes = _ramp(150, -0.5, 100.0) + _ramp(18, 6.0, 25.0)
    r = analyze_htf_background(_kl_from_closes(closes))
    assert r is not None
    assert r["state"] == "bullish" and r["phase"] == "overheat"
    assert r["macro_aligned"] is False
    assert "V型反转中期（EMA21已上穿144，EMA55未跟上）" in r["detail"]
    assert "排列角度" in r["detail"]
    assert "过热命中：距离过热" in r["detail"]


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


def test_bull_histogram_flip_no_exhausted():
    """盘整基座起势后急杀 10 根 @-3.0：EMA 排列仍多头（落 weak 结构档）——
    衰竭期退役后（2026-10-11）柱线翻负不再判衰竭/不改 label，phase 按排列
    角度落结构档（夹具构造避免触发过热，与「翻向不改档」语义解耦），detail
    无动能注记"""
    closes = _shape([(140, 0.0), (50, 0.5), (10, -3.0)])
    r = analyze_htf_background(_kl(closes, 2.5))
    assert r is not None
    assert r["state"] == "bullish"
    assert r["phase"] == "weak"
    assert r["macro_aligned"] is True
    assert "衰竭期" not in r["state_label"]
    assert "动能背离" not in r["detail"] and "柱线已翻向" not in r["detail"]


def test_no_momentum_annotation():
    """冲高后降速不翻向：动能衰减注记已随衰竭期退役删除（2026-10-11），
    detail 不再出现任何动能措辞，phase 恒为结构档。
    （夹具 164 根 → len(closed)=163<164 不评过热——落在可判边界内恰为
    纯结构档判定样本，边界行为另见 test_overheat_gate_boundary_164_165）"""
    closes = _ramp(150, 0.5, 100.0) + _ramp(6, 2.5, 175.0) + _ramp(8, 1.2, 190.0)
    r = analyze_htf_background(_kl_from_closes(closes))
    assert r is not None
    assert r["state"] == "bullish"
    assert r["phase"] in ("weak", "medium", "strong")
    assert "动能衰减" not in r["detail"] and "仍同向" not in r["detail"]


def test_structured_line_ranging_no_false_macro():
    """4h 震荡（ADX<20）结构化行不得渲染 phase=None / macro=逆侧——
    宏观守卫未评估，None 渲染成「逆侧」会向 LLM 注入错误事实
    （bug 修复 2026-10-11）；改输出 state 标记"""
    closes = [100.0 + 0.05 * ((-1) ** i) + 0.002 * i for i in range(200)]  # 横盘低 ADX
    block = render_indicator_block(_kl_1h_flat(), htf_klines=_kl_from_closes(closes))
    assert "phase=None" not in block
    assert "macro=逆侧" not in block
    assert "- 4h背景: state=ranging" in block


def _kl_1h_flat() -> list[list]:
    """1h 侧最小序列（结构化行渲染仅要求非空指标结果）"""
    return _kl_from_closes(_ramp(60, 0.5, 100.0))


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


# ── _strength_phase 纯函数边界 ──


def test_strength_phase_boundaries():
    """排列角度 → 强度档边界（2026-10-11 分级重构：3 档，extreme 退役——
    ≥1.0 一律 strong，div≥2 的过度张开由过热三条件/质量分降档接管）：
    None 归 medium（角度不可测兜底）；阈值左开右闭"""
    assert _strength_phase(None) == "medium"
    assert _strength_phase(0.0) == "weak"
    assert _strength_phase(0.49) == "weak"
    assert _strength_phase(0.5) == "medium"
    assert _strength_phase(0.99) == "medium"
    assert _strength_phase(1.0) == "strong"
    assert _strength_phase(1.99) == "strong"
    assert _strength_phase(2.0) == "strong"  # extreme 退役：2.0 不再独立成档
    assert _strength_phase(17.0) == "strong"


# ── 硬约束锁单矩阵 ──


def test_constraint_locks_strong_aligned():
    """强多头排列（宏观同侧·健康形态不过热）+ AI 开空 → 强制 skip。
    extreme 退役后锁逆势档为 medium/strong；健康夹具用 A（盘整基座起势）——
    稳态线性趋势会触发距离过热走双向禁开分支而非锁逆势"""
    d = {"trade_decision": "suggest", "direction": "short"}
    out = enforce_htf_constraint(d, _STRONG_ALIGNED)
    assert out["trade_decision"] == "skip"
    assert "排列强多头背景禁止开空" in out["skip_reason"]
    assert out["veto_original"]["source"] == "htf_constraint"
    assert out["htf_phase"] == "strong"  # stash 落 AIAnalysis.htf_phase 供复盘分组


def test_constraint_locks_medium_aligned():
    """中等多头排列（宏观同侧）+ AI 开空 → 强制 skip（medium 档同样锁逆势）"""
    d = {"trade_decision": "suggest", "direction": "short"}
    out = enforce_htf_constraint(d, _MEDIUM_ALIGNED)
    assert out["trade_decision"] == "skip"
    assert "排列中等多头背景禁止开空" in out["skip_reason"]
    assert out["htf_phase"] == "medium"


def test_constraint_overheat_blocks_both_directions():
    """斜率过热（D 夹具）双向禁开：开多（追抛物线）与开空（接飞刀）一律 skip，
    skip_reason/veto reason 带过热语义（凌驾锁逆势分支）"""
    for direction in ("long", "short"):
        d = {"trade_decision": "suggest", "direction": direction}
        out = enforce_htf_constraint(d, _OVERHEAT_SLOPE)
        assert out["trade_decision"] == "skip"
        assert "过热约束：4h过热期（抛物线加速，暴跌前兆）双向禁开新仓" in out["skip_reason"]
        assert out["veto_original"]["source"] == "htf_constraint"
        assert "4h过热期双向禁开闸" in out["veto_original"]["reason"]
        assert out["htf_phase"] == "overheat"


def test_constraint_blocks_v_recovery_overheat():
    """V型反转中期（逆宏观）急弹命中距离过热 → 双向禁开（原逆宏观放行语义
    被过热凌驾：外部方案「过热不进」优先于宏观守卫）"""
    closes = _ramp(150, -0.5, 100.0) + _ramp(18, 6.0, 25.0)
    d = {"trade_decision": "suggest", "direction": "short"}
    out = enforce_htf_constraint(d, _kl_from_closes(closes))
    assert out["trade_decision"] == "skip"
    assert "过热约束：4h过热期" in out["skip_reason"]
    assert out["htf_phase"] == "overheat"


def test_constraint_allows_histogram_flip_with_trend():
    """原衰竭期构造（空头排列宏观同侧 + 柱线已翻多）+ AI 顺势开空 → 放行：
    衰竭期双向禁开规则退役（2026-10-11），矩阵只剩锁逆势——顺势侧不受
    动能翻向影响（htf_phase stash 为结构档，无「衰竭期」措辞）"""
    closes = _ramp(150, -0.5, 100.0) + _ramp(12, 3.0, 25.0)
    d = {"trade_decision": "suggest", "direction": "short"}
    out = enforce_htf_constraint(d, _kl_from_closes(closes))
    assert out["trade_decision"] == "suggest"
    assert out["htf_phase"] in ("weak", "medium", "strong")


def test_constraint_unlocks_strong_bear_rally():
    """强排列多头熊内反弹（逆宏观·健康形态不过热）+ AI 开空 → 放行：
    逆宏观任何强度不锁（质量分惩罚兜底）"""
    d = {"trade_decision": "suggest", "direction": "short"}
    out = enforce_htf_constraint(d, _STRONG_BEAR_RALLY)
    assert out["trade_decision"] == "suggest"
    assert out["htf_phase"] == "strong"


# ── 质量分按强度档映射 ──


def _votes(phase: str, macro=True, htf_dir=1, div=None) -> dict:
    return {
        "net": 3,
        "results": {
            HTF_SHORT: {
                "dir": htf_dir, "phase": phase,
                "macro_aligned": macro, "div": div,
            },
            "ADX": None, "RSI": None, "OBV": None,
        },
    }


def test_quality_score_same_direction_by_phase():
    """同向（2026-10-11 分级重构矩阵）：弱+5 / 中+8 / 强+10；过热期-8 /
    冷却期-4（追抛物线/消化期追多压分）；强∧div≥2.0 降为 +6（extreme 退役后
    降档语义由 div 原始值承载）（基础 71）"""
    base = 50 + 1 * 3 * 7
    cases = {"weak": 5, "medium": 8, "strong": 10, "overheat": -8, "cooling": -4}
    for phase, mod in cases.items():
        s = compute_quality_score(1, _votes(phase), "trigger_pullback_confirm")
        assert s == base + mod, f"{phase}: {s} != {base + mod}"


def test_quality_score_strong_div2_downgrade():
    """strong ∧ div≥2.0 同向降档 +10→+6（原 extreme 档语义）；div<2.0 不降档；
    div 键缺省按 0 处理（不降档）；降档只作用于同向（逆向不叠加）"""
    base = 50 + 1 * 3 * 7
    s_ge = compute_quality_score(1, _votes("strong", div=2.0), "trigger_pullback_confirm")
    assert s_ge == base + 6
    s_lt = compute_quality_score(1, _votes("strong", div=1.99), "trigger_pullback_confirm")
    assert s_lt == base + 10
    s_nokey = compute_quality_score(1, _votes("strong"), "trigger_pullback_confirm")
    assert s_nokey == base + 10
    s_counter = compute_quality_score(-1, _votes("strong", div=3.0), "trigger_pullback_confirm")
    assert s_counter == 50 + (-1) * 3 * 7 - 12  # 逆向走 -12，不触发降档


def test_quality_score_counter_direction_by_phase():
    """逆向（2026-10-11 分级重构矩阵）：弱-6 / 中-10 / 强-12；过热期-4 /
    冷却期-2（接飞刀同样险但程序已双向禁开，此处仅扫描侧排序兜底）；
    exhausted/extreme 旧行值走默认中性兜底（另用例覆盖）"""
    base = 50 + (-1) * 3 * 7
    cases = {"weak": -6, "medium": -10, "strong": -12, "overheat": -4, "cooling": -2}
    for phase, mod in cases.items():
        s = compute_quality_score(-1, _votes(phase), "trigger_pullback_confirm")
        assert s == base + mod, f"{phase}: {s} != {base + mod}"


def test_quality_score_legacy_exhausted_neutral_fallback():
    """存量旧行 htf_phase="exhausted"/"extreme"（退役旧档）不再有专属分值，
    走默认中性兜底（同向 +5 / 逆向 -8）"""
    base = 50 + 1 * 3 * 7
    for legacy in ("exhausted", "extreme"):
        s = compute_quality_score(1, _votes(legacy), "trigger_pullback_confirm")
        assert s == base + 5, legacy


def test_quality_score_macro_misaligned_penalty():
    """逆宏观额外 -4（硬约束不锁，分数兜底）"""
    s_aligned = compute_quality_score(1, _votes("strong"), "trigger_pullback_confirm")
    s_mis = compute_quality_score(
        1, _votes("strong", macro=False), "trigger_pullback_confirm")
    assert s_mis == s_aligned - 4
