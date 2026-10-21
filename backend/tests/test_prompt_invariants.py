"""提示词不变量（docs/20 §六）回归：改提示词前逐条过，防条款分叉复发

个案修复的模式是「个案→补条款→发现新漏」——P0 补了括注 P1 漏、注记改了
定义没同步。本文件把条款间的**并存关系**固化为断言：
任何一处改动导致不变量破裂，测试即刻红。
"""
import json as _json

from app.services import ai_agent, ai_analyzer
from app.services.strategy import technical
from app.services.strategy.candlestick import (
    GOLDEN_PATTERNS, PATTERN_LABEL_MAP, golden_whitelist_labels,
)
from app.services.strategy.technical import render_indicator_block


# ── fixtures（与 test_htf_trend_age 同构造）──────────────────────


def _kl_from_closes(closes) -> list[list]:
    return [
        [i * 4 * 3600_000, c, c, c, float(c), 1000.0]
        for i, c in enumerate(closes)
    ]


def _ramp(n: int, drift: float, base: float) -> list[float]:
    return [base + drift * i for i in range(n)]


def _kl_1h_stub(n: int = 60) -> list[list]:
    return [
        [i * 3_600_000, 100.0, 101.0, 99.0, 100.0, 10.0, i * 3_600_000 + 3_599_000, 1000.0]
        for i in range(n)
    ]


def _htf_strong_same():
    """冲高降速不翻向：strong/same（取舍资格盘面）"""
    return _kl_from_closes(_ramp(150, 0.5, 100.0) + _ramp(6, 2.5, 175.0) + _ramp(8, 1.2, 190.0))


def _p0_system() -> str:
    return ai_analyzer.render_system_prompt()


# ── 不变量 1：跳档禁令纯净（2026-10-11 trail RR 豁免退役；2026-10-12
# 策略二整体退役后口径固化）────────────────────────────────────


def test_ladder_ban_pure_no_deprecated_exception():
    """跳档禁令不得再带已退役口径的括注（trail RR 豁免/策略二，防复活）"""
    for txt in (_p0_system(), ai_agent.AGENT_SYSTEM):
        for line in txt.splitlines():
            if "凑盈亏比" in line:
                assert "trail" not in line, line
                assert "策略二" not in line, line


# ── 不变量 1.5：策略二退役（2026-10-12 删除策略二/trail，出场恒
# ladder；AI 不自选出场模式）────────────────────────────────


def test_strategy2_retired_invariants():
    """P0/P1 渲染提示词与 submit_decision 工具 schema 均不含
    「策略二」/entry_strategy/strategy2/tp_mode（防条款分叉复发）"""
    schema_txt = _json.dumps(
        ai_agent._tools_schema(tp2_enabled=True), ensure_ascii=False,
    )
    for txt in (_p0_system(), ai_agent.AGENT_SYSTEM, schema_txt):
        for kw in ("策略二", "entry_strategy", "strategy2", "tp_mode"):
            assert kw not in txt, f"{kw} 残留"


# ── 不变量 1.6：取舍提示行退役（technical 不再注入第三条路）────


def test_tradeoff_hint_retired():
    """盈亏比取舍提示行随 trail RR 豁免一并退役：任何盘面都不再注入"""
    block = render_indicator_block(_kl_1h_stub(), htf_klines=_htf_strong_same())
    assert "盈亏比取舍" not in block


# ── 不变量 2（2026-10-11 退役）：「动能同向」定义随衰竭期/MACD 动能判定
# 整链删除，_RULE_HTF_HARD 不再含该段，技术层不再产出「仍同向」注记 ──


# ── 不变量 3：金K白名单单一来源（B4）────────────────────────────


def test_golden_whitelist_labels_cover_all_patterns():
    labels = golden_whitelist_labels()
    for p in GOLDEN_PATTERNS:
        assert PATTERN_LABEL_MAP[p] in labels, p


def test_signal_type_labels_rendered_from_constants():
    """SIGNAL_TYPE_LABELS 不再手写金K清单——常量渲染（改白名单自动同步）"""
    txt = ai_analyzer.SIGNAL_TYPE_LABELS["trigger_pullback_confirm"]
    assert "锤形线" in txt and "看涨吞没" in txt and "乌云盖顶" in txt
    # 移出白名单的形态只在尾部排除句出现一次（清单段由常量渲染，不含它们）
    assert txt.count("孕线突破") == 1


# ── 不变量 5：止损宽度纪律（A3，P0/P1 同句）─────────────────────


def test_stop_width_discipline_present():
    # P1 止损纪律由共享渲染器注入（T3-3），查渲染后实文而非模板
    for txt in (_p0_system(), _p1_system()):
        assert "止损过宽" in txt
        assert "缩仓" in txt


# ── 不变量 6：skip_reason 类别前缀契约（C9，P0/P1 同）───────────


def test_skip_reason_prefix_contract():
    """P0 在系统提示词输出契约；P1 在 submit_decision 工具 schema（各自注入点）"""
    import json as _json

    assert "[RR不足]" in _p0_system() and "[其他]" in _p0_system()
    schema_txt = _json.dumps(ai_agent._tools_schema(tp2_enabled=True), ensure_ascii=False)
    assert "[RR不足]" in schema_txt and "[环境逆风]" in schema_txt


# ── 不变量 7：旧类型标签压缩（B5）───────────────────────────────


def test_legacy_signal_labels_compressed():
    for k in ("trigger_candlestick", "trigger_pullback"):
        assert len(ai_analyzer.SIGNAL_TYPE_LABELS[k]) <= 30, k


# ── 不变量 8：4h 结构化行随 4h 数据注入（C7）────────────────────


def test_htf_structured_line_injected():
    block = render_indicator_block(_kl_1h_stub(), htf_klines=_htf_strong_same())
    assert "- 4h背景: state=" in block   # 2026-10-11 再重构：phase 键退役改 state
    assert "momentum=" not in block  # momentum 键已随衰竭期退役删除（2026-10-11）


def test_htf_structured_line_absent_without_htf():
    block = render_indicator_block(_kl_1h_stub())
    assert "- 4h背景: state=" not in block


# ── 不变量 9：4h 状态机硬约束文案三要素（2026-10-11 再重构）──────


def _htf_kl(closes, wick):
    return [
        [i * 4 * 3600_000, c, c + wick * (1 + 0.1 * (i % 3)),
         c - wick * (1 + 0.1 * (i % 3)), float(c), 1000.0]
        for i, c in enumerate(closes)
    ]


def _htf_shape(seg):
    out, p = [], 100.0
    for n, d in seg:
        out += [p + d * i for i in range(n)]
        p += d * n
    return (out + [out[-1]] * 300)[:200]


def test_htf_hard_rule_covers_state_and_direction():
    """_RULE_HTF_HARD 必须同时覆盖：缠绕/拐头双向禁开（状态约束）与宏观同侧
    态锁逆势（方向约束），且声明程序会强制改判（LLM 无视注入行时程序侧
    兜底语义不缺失）。P0 系统提示词为填充后实文；
    P1 AGENT_SYSTEM 为模板（常量经 render 填充），查常量文本+注入占位符"""
    rule = ai_agent._RULE_HTF_HARD
    assert "「状态约束（硬性）」" in rule
    assert "「方向约束（硬性）」" in rule
    assert "双向禁开新仓" in rule
    assert "程序会强制改判" in rule
    assert "{_RULE_HTF_HARD}" in ai_agent.AGENT_SYSTEM  # P1 注入点不缺失
    for txt in (_p0_system(),):
        assert "「状态约束（硬性）」" in txt
        assert "双向禁开新仓" in txt


def test_state_constraint_line_rendered():
    """渲染层：禁开态（缠绕/拐头）4h 背景必须注入「状态约束（硬性）」行
    （双向禁开、优先于一切信号），且不再渲染「方向约束」行"""
    out, p = [], 100.0
    for n, d in ((140, 0.0), (40, 0.5), (10, -1.5)):  # 多头排列内拐头
        out += [p + d * i for i in range(n)]
        p += d * n
    block = render_indicator_block(_kl_1h_stub(), htf_klines=_htf_kl(out, 2.0))
    assert "状态约束（硬性）" in block
    assert "双向禁开新仓" in block
    assert "方向约束（硬性）" not in block


def test_direction_constraint_line_rendered_for_strong_aligned():
    """渲染层：宏观同侧排列（多头排列锁逆势态）注入「方向约束（硬性）」
    锁逆势行——夹具用盘整基座起势形态（趋势老到无 fresh 交叉，落排列
    稳态而非宏观交叉/拐头）"""
    closes = _htf_shape([(140, 0.0), (60, 0.5)])
    block = render_indicator_block(_kl_1h_stub(), htf_klines=_htf_kl(closes, 2.5))
    assert "方向约束（硬性）" in block
    assert "状态约束（硬性）" not in block


# ── 不变量 10：盈亏比唯一不达标 → 固定 1.5R 放宽（2026-10-14）──────


def test_rr_only_shortfall_fixed_rr_fallback_present():
    """P0/P1 提示词必须含『唯一放宽』条款：盈亏比为唯一不达标项 ∧ 4h 宏观同侧六态
    （渲染层出现「方向约束（硬性）」行）→ 改用『入场价 ± rr_min×止损距离』固定
    1.5R 兜底；并声明缠绕/拐头与过渡态仍 skip（防放宽面被误读为无条件放行）。
    兜底情形 (c)（近端结构位太近）在 TP2 开启版与关闭版均须齐备。"""
    # P1 的止损/止盈段改由共享渲染器注入（T3-3），渲染后实文才含条款
    for txt in (_p0_system(), _p1_system()):
        assert "唯一放宽" in txt
        assert "方向约束（硬性）" in txt
        assert "缠绕/拐头" in txt
    p0 = _p0_system()
    assert "止盈一兜底" in p0               # 三情形兜底段已注入渲染后实文
    assert "×止损距离" in p0                 # 渲染后 {rr_min} 已填为 1.5
    assert "(c)" in ai_analyzer._RULE_TP_TAIL_ON
    assert "唯一放宽" in ai_analyzer._RULE_TP_BASE  # 唯一放宽条款在公共段（TP2 开关无关）
    assert "入场价 ± {rr_min}×止损距离" in ai_analyzer._RULE_TP_BASE
    assert "入场价 ± {rr_min}×止损距离" in ai_analyzer._RULE_TP_BASE
    assert "唯一不达标项" in ai_analyzer._RULE_TP_TAIL_OFF


# ── 不变量 11：评分分档纪律 + 门槛注入（2026-10-21 止血批）──────────
# 背景：AI 曾以「1h 三指标偏空 + BTC 逆风 + RR 仅达标」为由给出 58 分，
# 58 < 分发门槛 60 等于隐性弃单，但提示词未注入门槛、且允许线性内插——
# 分数退化成语气量表。以下断言锁定：分档不内插、减分项不跨档、
# 门槛必须注入（AI 才知道 58 等于程序不下单）、仓位不承载意见。


def _p1_system(min_rec=None) -> str:
    return ai_agent.render_system_prompt(min_recommendation=min_rec)


def test_recommendation_tiered_no_interpolation():
    """P0/P1 均须按档给分（60/70/85），禁止介于档位之间的分数；
    T3-1 起档位定义按触发类型分表（突破/回踩各三档）"""
    for txt in (_p0_system(), _p1_system()):
        assert "只取 60/70/85 三档" in txt
        assert "禁止输出 58/62 这类中间分" in txt
        assert "档位定义（按触发类型）" in txt
        # 渲染实文须同时含两张表的关键行（预览页无触发类型时两表都列出）
        assert "60 = 4h 同侧 + 触发有效" in txt
        assert "60 = 4h 同侧 + 触点有效" in txt


def test_tier_table_by_trigger_type():
    """T3-1：突破/回踩档位表按 signal_type 选择，互不串味"""
    bo = ai_analyzer.render_system_prompt(trigger_type="trigger_breakout")
    pb = ai_analyzer.render_system_prompt(trigger_type="trigger_pullback_confirm")
    assert "触发有效" in bo and "触点有效" not in bo
    assert "触点有效" in pb and "触发有效" not in pb


def test_deduction_items_never_shift_tier():
    """环境风险/1h 反向/RR 恰达标 只在同档内决定上下限，不得跨档；
    若足以否决则直接 skip（不得用中间分表达犹豫）"""
    for txt in (_p0_system(), _p1_system()):
        assert "减分项不跨档" in txt
        assert "若它本身足以否决，直接 skip" in txt
        assert "禁止输出 58/62 这类中间分" in txt


def test_min_recommendation_injected_into_both_pipelines():
    """提示词静态门槛 P0/P1 一致；连损动态门槛仅在可确定时注入"""
    assert "AI 分析阶段静态门槛 = 75" in ai_analyzer.render_system_prompt(min_recommendation=75)
    assert "AI 分析阶段静态门槛 = 75" in ai_agent.render_system_prompt(min_recommendation=75)
    from app.config import settings
    expect = f"AI 分析阶段静态门槛 = {float(settings.TRADING_MIN_RECOMMENDATION):g}"
    assert expect in _p0_system()
    assert expect in _p1_system()


def test_consecutive_loss_threshold_does_not_apply_when_derate_disabled(monkeypatch):
    """关闭执行层连损降速时，提示词不可继续要求按 75/85 门槛跳过"""
    from app.config import settings
    monkeypatch.setattr(settings, "CONSECUTIVE_LOSS_DERATE", False)
    prompt = ai_analyzer.render_system_prompt(
        min_recommendation=60, consecutive_losses=3,
    )
    assert "AI 分析阶段静态门槛 = 60" in prompt
    assert "连损动态门槛由执行层按账号在开仓时单独校验" not in prompt
    assert "程序执行门槛提升至 85" not in prompt


def test_volume_facts_render_ratio_and_raw_volume_separately():
    """原始成交量不得被错误标成量比；两者按独立字段渲染"""
    from app.services.ai_analyzer import build_signal_facts
    signal = {
        "symbol": "TESTUSDT", "signal_type": "trigger_breakout",
        "current_price": 10, "breakout_pct": 0.5, "volume_type": "放量",
        "volume": 1234567, "volume_ratio": 1.23,
    }
    facts = build_signal_facts(signal, _kl_1h_stub())
    assert "量能: 放量（量比 1.23×近20根中位量）" in facts
    assert "原始量: 1,234,567（币/张数，仅记录，不参与判断）" in facts
    assert "1234567.0×20根均量" not in facts


def test_quality_facts_render_score_and_breakdown():
    """质量分应明确是排序分，且只呈现实际记录到的分项"""
    from app.services.ai_analyzer import build_signal_facts
    signal = {
        "symbol": "TESTUSDT", "signal_type": "trigger_breakout",
        "current_price": 10, "breakout_pct": 0.5, "quality_score": 68,
        "quality_parts": {"base": 50, "htf": 10, "volume": 5},
    }
    facts = build_signal_facts(signal, _kl_1h_stub())
    assert "质量分: 68/100（基础 50 / 4h +10 / 量能 +5）（程序排序用，不替代你的判断）" in facts


def test_static_prompt_threshold_is_not_mislabeled_as_per_account_dynamic_gate(monkeypatch):
    """提示词能说明静态门槛，但不能把跨账号连损状态说成所有账号的实效门槛"""
    prompt = ai_analyzer.render_system_prompt(
        min_recommendation=60, consecutive_losses=3,
    )
    from app.services.ai_analyzer import build_signal_facts
    from app.config import settings
    monkeypatch.setattr(settings, "CONSECUTIVE_LOSS_DERATE", True)
    facts = build_signal_facts(
        {"symbol": "TESTUSDT", "signal_type": "trigger_breakout",
         "current_price": 10, "breakout_pct": 0.5},
        _kl_1h_stub(), min_recommendation=60, consecutive_losses=3,
    )
    assert "连损动态门槛: 部分账号可能提高" in facts
    assert "AI 分析阶段静态门槛: 60" in facts
    monkeypatch.setattr(settings, "CONSECUTIVE_LOSS_DERATE", False)
    disabled_facts = build_signal_facts(
        {"symbol": "TESTUSDT", "signal_type": "trigger_breakout",
         "current_price": 10, "breakout_pct": 0.5},
        _kl_1h_stub(), min_recommendation=60, consecutive_losses=3,
    )
    assert "连损动态门槛: 部分账号可能提高" not in disabled_facts
    assert "AI 分析阶段静态门槛 = 85" not in prompt


def test_position_field_not_carrying_opinion():
    """仓位由程序按固定亏损法复算，评分不参与——P0 字段说明不得再暗示
    可用缩仓表达保守"""
    p0 = _p0_system()
    assert "不承载保守/激进语义" in p0
    assert "仓位恒由程序按固定亏损法计算" in p0


def test_pullback_1h_reversal_exempt_listed():
    """回踩确认单的 1h 反向属回撤形态本身：MACD/BOLL/RSI、1h EMA、
    SuperTrend/OBV、缩量 四类全部豁免（不进减分项/不改档/不作 skip 理由）"""
    p0 = _p0_system()
    assert "回踩确认单（trigger_pullback_confirm）定向以 4h 为准" in p0
    for kw in ("MACD/布林带/RSI", "1h EMA 排列/拐头", "SuperTrend/OBV", "回撤缩量"):
        assert kw in p0, kw
    assert "只作确认度参考，不进减分项、不改变评分档位、不作 skip 理由" in p0
    assert "只作确认度参考，不进减分项、不改变评分档位、不作 skip 理由" \
        in ai_agent._RULE_HTF_HARD


def test_btc_env_not_a_gate_leveled():
    """BTC 状态行不是闸门：无「急跌」不得降分降档；有「急跌」最多降一档"""
    for txt in (_p0_system(), _p1_system()):
        assert "BTC 状态行不是闸门" in txt
        assert "不降分、不降档、不作 skip 理由" in txt
        assert "最多降一档" in txt


def test_skill_pullback_no_conflicting_scoring_text():
    """技能文档不得再教「1h EMA 矛盾 → 评分保守」，与主提示词冲突"""
    from pathlib import Path
    p = Path(ai_analyzer.__file__).resolve().parents[2] / "skills" / "trend-pullback" / "SKILL.md"
    txt = p.read_text(encoding="utf-8")
    assert "只在评分上适度保守" not in txt
    assert "不影响评分档位" in txt
    assert "**4h** EMA 死叉/拐头" in txt
    assert "1h EMA 死叉/拐头属回撤常态" in txt
