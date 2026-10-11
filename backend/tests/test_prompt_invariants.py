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


# ── 不变量 1：跳档禁令恢复纯净（2026-10-11 trail RR 豁免退役）────


def test_ladder_ban_pure_no_trail_exception():
    """策略一 ladder-only：跳档禁令不得再带「trail 例外」括注（防括注复活）"""
    for txt in (_p0_system(), ai_agent.AGENT_SYSTEM):
        for line in txt.splitlines():
            if "凑盈亏比" in line:
                assert "trail" not in line, line


# ── 不变量 1.5：tp_mode 由 entry_strategy 程序推导（AI 不自选）──


def test_tp_mode_derivation_clause_present_p0_p1():
    """P0 字段说明与 P1 规则 12.5 同句式：程序按 entry_strategy 推导并覆盖"""
    for txt in (_p0_system(), ai_agent.AGENT_SYSTEM):
        assert "程序按 entry_strategy 推导并覆盖本字段" in txt
        assert "trail 加入前的原版" in txt  # 策略一重定义（ladder-only）


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
    for txt in (_p0_system(), ai_agent.AGENT_SYSTEM):
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
