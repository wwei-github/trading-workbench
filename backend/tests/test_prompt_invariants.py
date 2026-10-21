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


# ── 不变量 2（2026-10-11 退役，2026-10-21 删除 _RULE_HTF_HARD）：「动能同向」
# 定义随衰竭期/MACD 动能判定整链删除，技术层不再产出「仍同向」注记 ──


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


# ── 不变量 9：4h 方向职责只在筛选层（2026-10-21 用户拍板）─────────
# 背景：4h 多头排列+做多 / 空头排列+做空 是 scanner 的选币逻辑，不是 AI
# 的开单限制。提示词只注入 4h 数据行（state/macro），AI 结合结构、量能、
# 方向票自主判断；原 _RULE_HTF_HARD（双向禁开/锁逆势/程序强制改判）已删除。


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


def test_ai_prompts_do_not_restrict_direction_by_htf():
    """P0/P1 均须声明 4h 仅作数据参考，且不得再出现方向硬约束文案"""
    for txt in (_p0_system(), _p1_system()):
        assert "仅作趋势强弱与风险参考" in txt
        assert "不限制多空方向" in txt
        for kw in ("状态约束", "方向约束", "双向禁开新仓", "锁逆势",
                   "程序会强制改判"):
            assert kw not in txt, kw
        # 公共检查首项只作数据参考，不得再写成方向判断项
        assert "1. 4h背景（数据参考）" in txt
        assert "1. 4h方向" not in txt
    # 常量整体退役（防复活）
    assert not hasattr(ai_analyzer, "_RULE_HTF_HARD")
    assert not hasattr(ai_agent, "_RULE_HTF_HARD")


def test_render_htf_background_is_data_only():
    """渲染层：任何 4h 状态都只注入数据行，不注入状态/方向约束行"""
    cases = {
        "turn_bull": ((140, 0.0), (40, 0.5), (10, -1.5)),   # 多头排列内拐头
        "aligned_bull": ((140, 0.0), (60, 0.5)),           # 多头排列稳态
        "flat": ((200, 0.002),),                           # 横盘缠绕
    }
    for name, seg in cases.items():
        block = render_indicator_block(
            _kl_1h_stub(), htf_klines=_htf_kl(_htf_shape(seg), 2.0))
        assert "- 4h背景: state=" in block, name
        for kw in ("状态约束", "方向约束", "双向禁开新仓", "锁逆势", "phase="):
            assert kw not in block, (name, kw)
    block = render_indicator_block(
        _kl_1h_stub(), htf_klines=_htf_kl(_htf_shape([(140, 0.0), (60, 0.5)]), 2.5))
    assert "macro=同侧" in block


def test_indicator_title_does_not_direct_by_htf():
    """指标块标题也不得把 4h 写成方向来源（2026-10-21 口径收敛）"""
    kls = _htf_kl(_htf_shape([(140, 0.0), (60, 0.5)]), 2.0)
    for st in ("trigger_pullback_confirm", "trigger_breakout"):
        block = render_indicator_block(_kl_1h_stub(), htf_klines=kls, trigger_type=st)
        assert "方向=4h" not in block, st
        assert "4h 背景=环境数据" in block, st
    pb = render_indicator_block(
        _kl_1h_stub(), htf_klines=kls, trigger_type="trigger_pullback_confirm")
    assert "方向票=确认度参考" in pb


# ── 不变量 10：盈亏比唯一不达标 → 固定 1.5R 放宽（2026-10-14）──────


def test_rr_only_shortfall_fixed_rr_fallback_present():
    """P0/P1 提示词必须含『唯一放宽』条款：盈亏比为唯一不达标项 → 改用
    『入场价 ± rr_min×止损距离』固定 1.5R 兜底。2026-10-21 起该放宽不再
    附加 4h 宏观同侧条件（方向限制只保留在筛选层）。兜底情形 (c)（近端
    结构位太近）在 TP2 开启版与关闭版均须齐备。"""
    # P1 的止损/止盈段改由共享渲染器注入（T3-3），渲染后实文才含条款
    for txt in (_p0_system(), _p1_system()):
        assert "唯一放宽" in txt
    p0 = _p0_system()
    assert "唯一不达标项" in p0
    assert "止盈一兜底" in p0               # 三情形兜底段已注入渲染后实文
    assert "×止损距离" in p0                 # 渲染后 {rr_min} 已填为 1.5
    assert "(c)" in ai_analyzer._RULE_TP_TAIL_ON
    assert "唯一放宽" in ai_analyzer._RULE_TP_BASE  # 唯一放宽条款在公共段（TP2 开关无关）
    assert "入场价 ± {rr_min}×止损距离" in ai_analyzer._RULE_TP_BASE
    # 2026-10-21 文案清理：放宽前提用「触发起效」，不得再提「趋势状态」（防 4h 回流）
    assert "触发起效" in ai_analyzer._RULE_TP_BASE
    assert "趋势状态" not in ai_analyzer._RULE_TP_BASE
    assert "唯一不达标项" in ai_analyzer._RULE_TP_TAIL_OFF


# ── 不变量 11：评分分档纪律 + 门槛注入（2026-10-21 止血批）──────────
# 背景：AI 曾以「1h 三指标偏空 + BTC 逆风 + RR 仅达标」为由给出 58 分，
# 58 < 分发门槛 60 等于隐性弃单，但提示词未注入门槛、且允许线性内插——
# 分数退化成语气量表。以下断言锁定：分档不内插、减分项不单独跨档、
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
        # 渲染实文须同时含两张表的关键行（预览页无触发类型时两表都列出）；
        # 2026-10-21 起 4h 同向降为 70 档加分项，60 档不再要求 4h 同侧
        assert "60 = 触发有效 + 方向票同向 ≥1" in txt
        assert "60 = 触点有效 + 金K确认" in txt
        assert "4h 同向（升档加分条件）" in txt


def test_tier_table_by_trigger_type():
    """T3-1：突破/回踩档位表按 signal_type 选择，互不串味"""
    bo = ai_analyzer.render_system_prompt(trigger_type="trigger_breakout")
    pb = ai_analyzer.render_system_prompt(trigger_type="trigger_pullback_confirm")
    assert "触发有效" in bo and "触点有效" not in bo
    assert "触点有效" in pb and "触发有效" not in pb


def test_deduction_items_never_shift_tier():
    """1h 反向/RR 恰达标 只作档内风险说明，不得单独跨档（环境逆风按环境规则
    最多降一档）；若足以否决则直接 skip（不得用中间分表达犹豫）"""
    for txt in (_p0_system(), _p1_system()):
        assert "减分项不单独跨档" in txt
        assert "若减分项本身足以否决" in txt
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
    """回踩确认单的 1h 反向属回撤形态本身：方向票反向/同向不足只作确认度
    参考，不得据此否决或降分（2026-10-21 起方向由 AI 结合结构与 4h 背景
    自主判断，不再以 4h 为准）"""
    p0 = _p0_system()
    assert "回踩确认单（trigger_pullback_confirm）：1h 方向票反向或同向不足" in p0
    assert "属回撤常态" in p0
    assert "仅作确认度参考（影响质量分），不得据此否决或降分" in p0
    assert "多空方向结合" in p0
    assert "4h 背景与结构自主判断" in p0
    assert "定向以 4h 为准" not in p0
    # P1 为精简版：同一口径（确认度参考、不计票）
    assert "回踩单方向票仅作确认度参考" in _p1_system()


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
    # 2026-10-21：技能文档也不得再出现 4h 方向硬约束
    assert "方向约束（硬性）" not in txt
    assert "缠绕/拐头双向禁开" not in txt
    assert "4h 背景（state/macro）仅作趋势强弱与风险参考" in txt
    assert "4h EMA 死叉/拐头仅作风险点记录" in txt
    assert "不单独降档" in txt
