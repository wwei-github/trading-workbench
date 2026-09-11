"""Trader Agent：GLM 工具循环 + submit_decision 校验即工具（docs/04 §5 P1）

- 事实包（信号/关键位/EMA/ATR/量能/市场环境）随首条 user 消息给足，程序能算的不让模型算
- 工具只做"补充查询"：更多K线、资金费率、大盘、恐贪、技能阅读
- 结构位锚点：模型不报绝对价格，报 (ref, offset_pct)，程序换算后过 Risk Guard——消灭幻觉价位
- 校验即工具：submit_decision 内嵌 Risk Guard，违规明细作为工具结果返回，模型看着错处改（Reflection）
- spke 已验证 GLM tool-calling 稳定（3/3，2 轮收敛），故手写循环不引入框架
"""
import json
import logging
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Optional

from app.config import settings
from app.services import market_data
from app.services import skill_library
from app.services.llm_client import get_client
from app.services.risk_guard import (
    TRADE_TYPES,
    TradeDecision,
    build_forced_skip,
    calc_atr,
    validate_decision,
)
from app.services.strategy.ema import analyze_ema
from app.services.strategy.types import POSITION_LABEL_MAP

logger = logging.getLogger(__name__)

MAX_ROUNDS = 5

# 锚点枚举：关键位 kind + market（市价锚，兜底表达力）
LEVEL_REFS = (
    "support", "resistance", "prev_high", "prev_low",
    "range_top", "range_bottom", "market",
)

_REF_DESC = "、".join(LEVEL_REFS)

AGENT_SYSTEM = """你是加密货币合约交易决策 Agent。事实包已随消息给出，规则如下：
1. 程序能算的不让你算——事实包数据直接用；需要补充数据才调用工具，最多 {max_rounds} 轮。
2. **禁止编造绝对价格**：所有价格必须用结构位锚点表达（entry_ref/stop_ref/tp1_ref/tp2_ref + offset_pct），
   ref 取值: {_ref_desc}；offset_pct 为相对锚点的百分比偏移（如支撑下方 0.5×ATR 用负 offset）。
3. 决策必须通过 submit_decision 工具提交，提交后程序会做风控校验：
   校验不通过时工具会返回违规明细，请按明细修正后重新提交。
4. **入场价纪律**：当前价格是分析时刻的最新价。顺势追势时 entry 用 market 锚（offset 0 附近）；
   计划等回踩时 entry 锚定回踩结构位并在 reason 写明"等回踩"；若现价已显著离开信号关键位
   且无合理入场计划 → 直接 skip，不要给出既不贴近现价也不贴近结构位的模糊入场价。
5. 开单类型归类（trade_type，suggest 必填其一）：
   trend_follow 顺势交易 / rule_123 123法则（破前高/低后回踩确认反转）/
   n_structure N字结构（回踩后同向延续）/ rule_2b 2B法则（假突破前高/低后反向）/
   range_edge 区间边缘反转。
6. "可用技能"列表中标注【当前命中】的技能，建议先 load_skill 阅读再决策。
7. **效率（重要）**：事实包已含决策所需核心数据（30根K线/关键位/ATR/资金费率/大盘/恐贪），
   首轮即可直接 submit_decision；确需补充数据时，把所需工具在同一轮一次性批量调用，
   不要为用工具而用工具，也不要每轮只查一个工具。
8. 不要输出不带工具调用的纯文字回复（会浪费一轮）；文字只用于简短说明查询意图，
   决策一律通过 submit_decision 提交。
9. 盈亏比铁律 ≥{rr_min}；止损距离 ≤{stop_max:.0f}%；仓位数不要自己报，程序按风险预算计算。

{skill_index}"""

TOOLS_SCHEMA = [
    {"type": "function", "function": {
        "name": "get_recent_klines",
        "description": "获取该币种最近 N 根已收盘K线（事实包里已有30根摘要，需要更长历史时用）",
        "parameters": {"type": "object", "properties": {
            "n": {"type": "integer", "description": "根数，30~200"},
        }, "required": ["n"]},
    }},
    {"type": "function", "function": {
        "name": "get_funding",
        "description": "获取该币种当前资金费率与持仓量",
        "parameters": {"type": "object", "properties": {}, "required": []},
    }},
    {"type": "function", "function": {
        "name": "get_market_breadth",
        "description": "获取大盘状态（BTC/ETH 的 EMA 形态与 24h 涨跌幅）",
        "parameters": {"type": "object", "properties": {}, "required": []},
    }},
    {"type": "function", "function": {
        "name": "get_fear_greed",
        "description": "获取恐惧贪婪指数",
        "parameters": {"type": "object", "properties": {}, "required": []},
    }},
    {"type": "function", "function": {
        "name": "load_skill",
        "description": "阅读一个打法技能的完整内容（索引中标注【当前命中】的建议先读）",
        "parameters": {"type": "object", "properties": {
            "name": {"type": "string", "description": "技能名，见索引"},
        }, "required": ["name"]},
    }},
    {"type": "function", "function": {
        "name": "submit_decision",
        "description": "提交最终决策（唯一出口，程序风控校验）。价格全部用锚点表达。",
        "parameters": {"type": "object", "properties": {
            "trade_decision": {"type": "string", "enum": ["suggest", "skip"]},
            "skip_reason": {"type": "string", "description": "skip 时必填，1. 2. 3. 序号列原因，最多3条"},
            "direction": {"type": "string", "enum": ["long", "short"], "description": "suggest 时必填"},
            "trade_type": {
                "type": "string",
                "enum": list(TRADE_TYPES),
                "description": "开单类型归类，suggest 时必填：trend_follow 顺势交易 / rule_123 123法则 / "
                               "n_structure N字结构 / rule_2b 2B法则 / range_edge 区间边缘反转",
            },
            "entry_ref": {"type": "string", "enum": list(LEVEL_REFS), "description": "入场锚点"},
            "entry_offset_pct": {"type": "number", "description": "入场相对锚点偏移%，默认0"},
            "stop_ref": {"type": "string", "enum": list(LEVEL_REFS), "description": "止损锚点"},
            "stop_offset_pct": {"type": "number", "description": "止损相对锚点偏移%（通常负/反向）"},
            "tp1_ref": {"type": "string", "enum": list(LEVEL_REFS), "description": "止盈1锚点"},
            "tp1_offset_pct": {"type": "number", "description": "止盈1偏移%"},
            "tp2_ref": {"type": "string", "enum": list(LEVEL_REFS), "description": "止盈2锚点（可选）"},
            "tp2_offset_pct": {"type": "number", "description": "止盈2偏移%"},
            "recommendation": {"type": "integer", "description": "推荐程度 0-100，skip≤30，suggest≥50"},
            "reason": {
                "type": "string",
                "description": (
                    "决策分析，≤300字中文，直接作为分析结果落库展示。必须用\"1. 2. 3.\"序号分点、"
                    "每条一行（JSON 字符串内换行写\\n）：先逐条列满足的条件，再列风险点，"
                    "最后一条写明确结论。"
                ),
            },
        }, "required": ["trade_decision"]},
    }},
]


def _resolve_price(ref: Optional[str], offset_pct, signal: dict) -> Optional[float]:
    """锚点 + 偏移 → 绝对价格；非法锚点返回 None"""
    if not ref:
        return None
    price_map = {
        lv.get("kind"): float(lv.get("price", 0))
        for lv in (signal.get("key_levels") or [])
    }
    if ref == "market":
        base = float(signal.get("current_price") or 0)
    elif ref in price_map and price_map[ref] > 0:
        base = price_map[ref]
    else:
        return None
    try:
        off = float(offset_pct or 0)
    except (TypeError, ValueError):
        off = 0.0
    return base * (1 + off / 100.0)


def _handle_submit(args: dict, signal: dict, klines: list) -> tuple[bool, str, Optional[dict]]:
    """submit_decision：锚点换算 → Risk Guard。返回 (done, tool_result, fixed)"""
    decision = args.get("trade_decision")
    if decision not in ("suggest", "skip"):
        return False, "trade_decision 必须是 suggest 或 skip", None

    if decision == "skip":
        if not (args.get("skip_reason") or "").strip():
            return False, "skip 必须给出 skip_reason（1. 2. 3. 序号原因）", None
        fixed = {
            "trade_decision": "skip",
            "skip_reason": args["skip_reason"].strip(),
            "direction": None,
            "trade_type": None,
            "analysis": "",
            "entry_price": 0, "stop_loss": 0, "take_profit_1": 0,
            "take_profit_2": 0, "risk_reward_ratio": 0,
            "position_pct": 0,
            "recommendation": min(float(args.get("recommendation") or 10), 30),
        }
        return True, "决策已提交：skip", fixed

    # suggest：锚点换算
    entry = _resolve_price(args.get("entry_ref"), args.get("entry_offset_pct"), signal)
    stop = _resolve_price(args.get("stop_ref"), args.get("stop_offset_pct"), signal)
    tp1 = _resolve_price(args.get("tp1_ref"), args.get("tp1_offset_pct"), signal)
    tp2 = _resolve_price(args.get("tp2_ref"), args.get("tp2_offset_pct"), signal)
    errs = []
    if entry is None:
        errs.append(f"entry_ref 非法（{_REF_DESC} 中选择）")
    if stop is None:
        errs.append(f"stop_ref 非法")
    if tp1 is None:
        errs.append("tp1_ref 非法")
    if errs:
        return False, "锚点换算失败：" + "；".join(errs) + "。请改用有效锚点重新提交。", None

    d = TradeDecision(
        trade_decision="suggest",
        direction=args.get("direction"),
        trade_type=args.get("trade_type"),
        entry_price=entry, stop_loss=stop,
        take_profit_1=tp1,
        take_profit_2=tp2 or 0.0,
        recommendation=float(args.get("recommendation") or 60),
        analysis=args.get("reason") or "",
    )
    ok, violations, fixed = validate_decision(d, signal, klines)
    if ok:
        return True, "决策已提交并通过风控校验", fixed
    return False, (
        "风控校验未通过：\n" + "\n".join(f"- {v}" for v in violations)
        + "\n请按违规明细修正（调整锚点/offset/方向）后重新提交。"
    ), None


def _dispatch_tool(name: str, args: dict, ctx: dict) -> tuple[bool, str, Optional[dict]]:
    """执行工具。返回 (done, result_text, fixed_decision)"""
    if name == "get_recent_klines":
        n = max(30, min(int(args.get("n", 60)), 200))
        closed = ctx["klines"][: len(ctx["klines"]) - 1]  # 去掉未收盘
        rows = closed[-n:] if len(closed) > n else closed
        return False, json.dumps([
            {"ts": int(k[0]), "o": k[1], "h": k[2], "l": k[3], "c": k[4], "v": k[5]}
            for k in rows
        ]), None
    if name == "get_funding":
        f = market_data.get_funding(ctx["signal"]["symbol"])
        return False, json.dumps(f or {"error": "不可用"}, ensure_ascii=False), None
    if name == "get_market_breadth":
        b = market_data.get_market_breadth(ctx["pool"])
        return False, json.dumps(b or {"error": "不可用"}, ensure_ascii=False), None
    if name == "get_fear_greed":
        fg = market_data.get_fear_greed()
        return False, json.dumps(fg or {"error": "不可用"}, ensure_ascii=False), None
    if name == "load_skill":
        return False, skill_library.load_skill(str(args.get("name", ""))), None
    if name == "submit_decision":
        return _handle_submit(args, ctx["signal"], ctx["klines"])
    return False, f"unknown tool: {name}", None


def analyze_coin_agent(
    signal: dict, klines: list,
    strategy_prompt: Optional[str] = None, user_input: Optional[str] = None,
    market_facts: Optional[dict] = None, pool=None,
    progress_cb=None,
) -> dict:
    """Agent 循环入口：返回与 Risk Guard 相同结构的决策 dict（可直接落库）。

    返回值额外带 "stage_trace" 键（工具循环 trace，docs/04 §5.9），落库时一并存储。
    progress_cb: 可选回调，接收 {"t": round/tool, ...} 进度事件供前端流式展示。
    """
    client = get_client()

    # 布尔事实（技能 use_when 判定）
    ema = analyze_ema(klines)
    atr = calc_atr(klines) or 0.0
    facts = _build_facts(signal, klines, atr, market_facts)

    skill_index = skill_library.render_index(facts)
    system = AGENT_SYSTEM.format(
        max_rounds=MAX_ROUNDS, _ref_desc=_REF_DESC,
        rr_min=settings.AI_RR_MIN, stop_max=settings.RISK_STOP_MAX_PCT * 100,
        skill_index=skill_index,
    )
    if strategy_prompt:
        system += (
            "\n\n用户自定义交易策略（冲突时以自定义策略为准）：\n"
            "===== 自定义策略开始 =====\n"
            f"{strategy_prompt}\n"
            "===== 自定义策略结束 ====="
        )

    user_msg = _build_user_msg(signal, klines, ema, atr, market_facts, user_input)
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": user_msg},
    ]

    ctx = {"signal": signal, "klines": klines, "pool": pool}
    fixed: Optional[dict] = None
    tool_rounds = 0
    trace: list[dict] = []
    t0 = time.time()

    for rnd in range(1, MAX_ROUNDS + 1):
        rt = time.time()
        resp = client.chat.completions.create(
            model=settings.AI_MODEL,
            messages=messages,
            tools=TOOLS_SCHEMA,
            temperature=0.3,
            max_tokens=12000,
        )
        msg = resp.choices[0].message
        tool_calls = msg.tool_calls or []
        trace.append({
            "round": rnd,
            "llm_ms": int((time.time() - rt) * 1000),
            "tools": [tc.function.name for tc in tool_calls],
        })
        logger.info(
            "Agent %s round%d: tools=%d content=%dB",
            signal.get("symbol"), rnd, len(tool_calls), len(msg.content or ""),
        )

        if not tool_calls:
            messages.append({"role": "assistant", "content": msg.content or ""})
            messages.append({"role": "user", "content": "请调用 submit_decision 提交决策。"})
            continue

        messages.append({
            "role": "assistant",
            "content": msg.content or "",
            "tool_calls": [tc.model_dump() for tc in tool_calls],
        })

        def _run_tool(tc):
            try:
                args = json.loads(tc.function.arguments or "{}")
            except json.JSONDecodeError:
                args = {}
            tt = time.time()
            done, result, decision = _dispatch_tool(tc.function.name, args, ctx)
            return tc, args, done, result, decision, int((time.time() - tt) * 1000)

        # 轮内多工具并行执行（GLM 常在一轮里同时要数据工具+技能），结果按 tool_call 顺序回填
        with ThreadPoolExecutor(max_workers=min(4, len(tool_calls))) as ex:
            outcomes = list(ex.map(_run_tool, tool_calls))

        for tc, args, done, result, decision, ms in outcomes:
            tool_rounds += 1
            trace[-1].setdefault("calls", []).append({
                "tool": tc.function.name,
                "args": {k: (str(v)[:80]) for k, v in args.items()},
                "result_len": len(result),
                "ms": ms,
            })
            if progress_cb:
                progress_cb({
                    "t": "tool", "tool": tc.function.name,
                    "args": "; ".join(f"{k}={v}" for k, v in args.items())[:60],
                })
            messages.append({
                "role": "tool",
                "tool_call_id": tc.id,
                "content": result[:6000],
            })
            if done:
                fixed = decision
        if fixed:
            break

    if not fixed:
        fixed = build_forced_skip(f"Agent {MAX_ROUNDS} 轮内未提交有效决策")

    fixed["stage_trace"] = {
        "rounds": len(trace),
        "tool_calls": tool_rounds,
        "elapsed_ms": int((time.time() - t0) * 1000),
        "steps": trace,
    }
    logger.info(
        "Agent 完成 %s: %s rr=%s rounds=%d %.1fs",
        signal.get("symbol"), fixed.get("trade_decision"),
        fixed.get("risk_reward_ratio"), tool_rounds, time.time() - t0,
    )
    return fixed


def _build_facts(signal: dict, klines: list, atr: float, market_facts: Optional[dict]) -> dict:
    """技能 use_when 判定用的布尔事实（程序算，不让模型猜）"""
    facts: dict = {
        "signal_type": signal.get("signal_type") or "unknown",
        "pin_bar": False,
        "funding_extreme": False,
        "narrow_range": False,
        "role": None,
    }
    # 信号命中的关键位角色
    hit_kind = signal.get("position")
    for lv in signal.get("key_levels") or []:
        if lv.get("kind") == hit_kind:
            facts["role"] = lv.get("role")
            break
    # pin_bar：已收盘最后一根 影线 > 2×实体
    if len(klines) >= 2:
        k = klines[-2]
        h, l, o, c = float(k[2]), float(k[3]), float(k[1]), float(k[4])
        body = abs(c - o)
        shadow = max(h - o, h - c) + max(o - l, c - l)  # 上下影线之和
        facts["pin_bar"] = body > 0 and shadow > 2 * body
    # funding 极端
    f = (market_facts or {}).get("funding")
    if f:
        facts["funding_extreme"] = abs(f.get("funding_rate_pct", 0)) > 0.1
    return facts


def _build_user_msg(
    signal: dict, klines: list, ema: Optional[dict], atr: float,
    market_facts: Optional[dict], user_input: Optional[str],
) -> str:
    closed = klines[:-1] if len(klines) >= 2 else klines
    recent = closed[-30:]
    kline_summary = "\n".join(
        f"{int(k[0]/1000)},{k[1]},{k[2]},{k[3]},{k[4]},{k[5]}" for k in recent
    )
    levels = []
    for lv in signal.get("key_levels") or []:
        role = "支撑" if lv.get("role") == "support" else "压力"
        levels.append(
            f"  {POSITION_LABEL_MAP.get(lv.get('kind'), lv.get('kind'))}: {lv['price']:.6g} "
            f"({role}, 触及{lv.get('touches', 1)}次)"
        )
    msg = (
        f"币种: {signal['symbol']}\n"
        f"信号类型: {signal['signal_type']}\n"
        f"当前价格: {signal['current_price']}\n"
        f"K线形态: {signal.get('pattern') or '无'}（位置: "
        f"{POSITION_LABEL_MAP.get(signal.get('position') or '', signal.get('position') or '未知')}）\n"
        f"信号理由: {signal.get('signal_reason') or ''}\n"
        f"信号强度: {signal.get('strength', '—')}\n"
        f"ATR(14): {atr:.6g}\n"
        + (f"均线形态: {ema['state_label']}（{ema['detail']}）\n" if ema else "")
        + "关键位（锚点参考，价格程序可换算）:\n" + "\n".join(levels) + "\n"
    )
    if market_facts:
        f = market_facts.get("funding")
        if f:
            msg += f"资金费率: {f['funding_rate_pct']}%\n"
        fg = market_facts.get("fear_greed")
        if fg:
            msg += f"恐贪指数: {fg['value']}({fg['label']})\n"
        b = market_facts.get("market_breadth")
        if b:
            seg = [f"{s} {v.get('ema_label') or '—'} 24h{v['change_24h_pct']:+.1f}%" for s, v in b.items()]
            msg += "大盘: " + " | ".join(seg) + "\n"
    msg += f"近{len(recent)}根已收盘K线(timestamp,open,high,low,close,vol):\n{kline_summary}"
    if user_input:
        msg += (
            "\n\n用户补充说明（请结合其内容进行分析）：\n"
            f"{user_input.strip()}"
        )
    return msg
