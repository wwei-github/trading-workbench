"""Trader Agent：GLM 工具循环 + submit_decision 校验即工具（docs/04 §5 P1）

- 事实包（信号/摆动结构/EMA/ATR/量能）随首条 user 消息给足，程序能算的不让模型算
- 工具只做"补充查询"：更多K线、资金费率、大盘、恐贪、技能阅读
- 结构位锚点：模型不报绝对价格，报 (ref, offset_pct)，程序换算到摆动结构位后过
  Risk Guard——消灭幻觉价位（2026-09-14 起锚点解析自 recent_swings，docs/09）
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

# 锚点枚举：结构位两类（support/resistance，解析到现价下/上方最近的摆动点）
# + 线外锚点（做多止盈锚摆动高点下方、做空锚摆动低点上方，枚举名沿用 zone/关键位
# 时期历史命名）+ market（市价锚）。next_ 前缀取更前一档（止盈二用）
LEVEL_REFS = (
    "support", "resistance", "market",
    "resistance_zone_low", "support_zone_high",
    "next_resistance_zone_low", "next_support_zone_high",
)

_REF_DESC = "、".join(LEVEL_REFS)

AGENT_SYSTEM = """你是加密货币合约交易决策 Agent。事实包已随消息给出，规则如下：
1. 程序能算的不让你算——事实包数据直接用；需要补充数据才调用工具，最多 {max_rounds} 轮。
2. **禁止编造绝对价格**：所有价格必须用结构位锚点表达（entry_ref/stop_ref/tp1_ref/tp2_ref + offset_pct），
   ref 取值: {_ref_desc}；offset_pct 为相对锚点的百分比偏移（如支撑位下方 0.5×ATR 用负 offset）。
   止盈用线外锚点：做多止盈一 tp1_ref=resistance_zone_low（现价上方最近的摆动高点，历史命名）、
   止盈二 tp2_ref=next_resistance_zone_low（更远一档的摆动高点）；做空用 support_zone_high /
   next_support_zone_high（现价下方的摆动低点及其更远一档）。止盈 offset 留余地：
   做多 -0.2~-0.5（位下方），做空 0.2~0.5（位上方）。
3. 决策必须通过 submit_decision 工具提交，提交后程序会做风控校验：
   校验不通过时工具会返回违规明细，请按明细修正后重新提交。
4. **入场价纪律**：当前价格是分析时刻的最新价。顺势追势时 entry 用 market 锚（offset 0 附近）；
   计划等回踩时 entry 锚定回踩结构位（摆动低/高点）并在 reason 写明"等回踩"；
   若现价已显著离开摆动结构位且无合理入场计划 → 直接 skip，
   不要给出既不贴近现价也不贴近结构位的模糊入场价。
5. 开单类型归类（trade_type，suggest 必填其一）：
   trend_follow 顺势交易（EMA 三线严格排列是硬前提，多单 21>55>144、空单 144>55>21）/
   structure_break 结构破位回踩（涵盖 123法则·N字结构·2B法则，见下方打法详解）/
   range_edge 区间边缘反转。
6. "可用技能"列表中标注【当前命中】的技能，建议先 load_skill 阅读再决策。
7. **效率与数据边界（重要）**：事实包只含信号与行情数据（60根K线含每根成交量与成交额/摆动结构/均线形态/ATR），
   首轮即可直接 submit_decision；资金费率、大盘状态、恐贪指数等环境数据**不在**事实包中，
   确需时在同一轮一次性批量调用 get_funding / get_market_breadth / get_fear_greed 自行获取
   （如资金费率极端可 load_skill 阅读 funding-extreme-handling）；不要为用工具而用工具，
   也不要每轮只查一个工具。
8. 不要输出不带工具调用的纯文字回复（会浪费一轮）；文字只用于简短说明查询意图，
   决策一律通过 submit_decision 提交。
9. 盈亏比铁律 ≥{rr_min}；止损宽度由结构位决定，不设固定价格百分比上限；仓位不要自己报，
   程序按固定亏损法计算：仓位 = 3% ÷ 止损距离%，触发止损时恰好亏损账户资金的 3%（止损越远仓位越小）。
10. 止损锚定（结构位优先，极值兜底，两锚取更远者；程序按此规则直接定锚，stop 值仅参考）：
    做多优先锚定入场价下方最近摆动低点外 0.3%~0.5%，做空锚定上方最近摆动高点外同幅度；
    入场贴着结构位时止损还须越过影线极值——做多取摆动低点与 5 根最低点中更低者、
    做空取摆动高点与 5 根最高点中更高者外 0.3%
    （止损在近期波动区间内部必被扫损）；入场价与极值间无摆动结构时用极值锚——
    做多严格低于最近 5 根K线最低点（影线极值非收盘价）至少 0.3%，做空相反。
11. 止盈锚定（程序强制最近优先，偏离会直接改挂）：结构位=摆动高低点（事实包"近期摆动结构"）。
    距现价特别近（≤1×ATR）的位是价格正在测试/突破的"争夺位"，只回答"是否得到支撑/压力/突破"，
    不算止盈目标；做多止盈一必须挂在更远的第一档结构位（摆动高点）下方 0.2%~0.5%，
    做空挂在下方第一档结构位（摆动低点）上方 0.2%~0.5%
    ——不得贴死位或挂在越位一侧（价格常在位附近反弹，越位才触发的止盈大概率落空）。
    禁止把止盈一挂到更远结构位凑盈亏比：到第一结构位的盈亏比不足 {rr_min} 说明空间不足，
    应直接 skip；程序会把偏离的止盈一强制改挂回第一结构位外侧再按铁律打回。
    止盈二取止盈一锚定档的下一档同类结构位（做多=更高一档摆动高点下方，做空=更低一档
    摆动低点上方）且必须比止盈一更远。价格创新高/新低、前方无摆动结构位时
    （如创新高突破），止盈一 = 入场 ± {rr_min}×止损距离、止盈二不设（仅一档）；
    程序同样会自动回退。盈亏比 ≥{rr_min} 是开单硬性要求。
12. 方向纪律（不再程序强制，无 ±0.3% 贴近要求）：优先只在支撑结构做多（摆动低点/
    近期低点附近）、只在压力结构做空（摆动高点/近期高点附近）；现价已离开结构位时
    不再要求贴近，按动能与盈亏比自行判断入场（breakout 顺势追突破仍用 market 锚）。

{skill_index}"""

TOOLS_SCHEMA = [
    {"type": "function", "function": {
        "name": "get_recent_klines",
        "description": "获取该币种最近 N 根已收盘K线（事实包里已有60根摘要，需要更长历史时用）。列与事实包相同：timestamp,open,high,low,close,vol,quote_vol(USDT)",
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
                "description": "开单类型归类，suggest 时必填：trend_follow 顺势交易 / "
                               "structure_break 结构破位回踩（123·N字·2B 合并）/ range_edge 区间边缘反转",
            },
            "entry_ref": {"type": "string", "enum": list(LEVEL_REFS), "description": "入场锚点"},
            "entry_offset_pct": {"type": "number", "description": "入场相对锚点偏移%，默认0"},
            "stop_ref": {"type": "string", "enum": list(LEVEL_REFS), "description": "止损锚点"},
            "stop_offset_pct": {"type": "number", "description": "止损相对锚点偏移%（通常负/反向）"},
            "tp1_ref": {"type": "string", "enum": list(LEVEL_REFS),
                        "description": "止盈1锚点：做多=resistance_zone_low（更远的第一条压力位价格线，距现价≤1×ATR的争夺位线不算），做空=support_zone_high（更远的第一条支撑位价格线）"},
            "tp1_offset_pct": {"type": "number",
                               "description": "止盈1相对锚点偏移%，留余地：做多 -0.2~-0.5（线下方），做空 0.2~0.5（线上方）"},
            "tp2_ref": {"type": "string", "enum": list(LEVEL_REFS),
                        "description": "止盈2锚点（可选）：做多=next_resistance_zone_low（下一档压力线），做空=next_support_zone_high（下一档支撑线）"},
            "tp2_offset_pct": {"type": "number", "description": "止盈2相对锚点偏移%，同止盈1留余地"},
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


# 线外锚点：ref -> (swings 侧, 第几档)；第几档 0=现价侧最近、1=下一档（止盈二用）。
# 锚点解析到摆动结构位价格本身（docs/09），枚举名沿用 zone/关键位时期历史命名。
_ZONE_REF_MAP = {
    "resistance_zone_low": ("highs", 0),
    "support_zone_high": ("lows", 0),
    "next_resistance_zone_low": ("highs", 1),
    "next_support_zone_high": ("lows", 1),
}


def _nth_swing(signal: dict, side: str, nth: int) -> Optional[float]:
    """现价侧第 nth 近的摆动结构位价格：highs 取现价上方升序（最近为 0），
    lows 取现价下方降序（最近为 0）。数据缺失返回 None。"""
    sw = signal.get("recent_swings") or {}
    try:
        cur = float(signal.get("current_price") or 0)
    except (TypeError, ValueError):
        cur = 0.0
    if cur <= 0:
        return None
    prices = sorted(
        (float(s["price"]) for s in (sw.get(side) or []) if float(s.get("price", 0) or 0) > 0),
        reverse=(side == "lows"),
    )
    prices = [p for p in prices if (p > cur if side == "highs" else p < cur)]
    if len(prices) <= nth:
        return None
    return prices[nth]


def _resolve_price(ref: Optional[str], offset_pct, signal: dict) -> Optional[float]:
    """锚点 + 偏移 → 绝对价格；非法锚点返回 None。

    support/resistance 解析到现价下方/上方最近的摆动结构位（多位时：
    support 取价下方最近即价格最大者，resistance 取价上方最近即价格最小者）；
    线外锚点解析到对应侧第 nth 近的摆动结构位价格本身。
    """
    if not ref:
        return None
    if ref == "market":
        base = float(signal.get("current_price") or 0)
    elif ref in _ZONE_REF_MAP:
        side, nth = _ZONE_REF_MAP[ref]
        base = _nth_swing(signal, side, nth)
        if base is None:
            return None
    else:
        cur = float(signal.get("current_price") or 0)
        sw = signal.get("recent_swings") or {}
        side = "lows" if ref == "support" else "highs"
        prices = [
            float(s.get("price", 0))
            for s in (sw.get(side) or [])
            if float(s.get("price", 0)) > 0
        ]
        if cur > 0:
            prices = [p for p in prices if (p < cur if ref == "support" else p > cur)]
        if not prices:
            return None
        base = max(prices) if ref == "support" else min(prices)
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
        n = max(30, min(int(args.get("n", 100)), 200))
        closed = ctx["klines"][: len(ctx["klines"]) - 1]  # 去掉未收盘
        rows = closed[-n:] if len(closed) > n else closed
        # 与事实包同列的 CSV（紧凑，200根 ≈ 11k 字符，在工具结果截断上限内）
        body = "\n".join(
            f"{int(k[0]/1000)},{k[1]},{k[2]},{k[3]},{k[4]},{k[5]},{k[7]}"
            for k in rows
        )
        return False, "ts,open,high,low,close,vol,quote_vol(USDT)\n" + body, None
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
    pool=None,
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
    facts = _build_facts(signal, klines, atr)

    skill_index = skill_library.render_index(facts)
    system = AGENT_SYSTEM.format(
        max_rounds=MAX_ROUNDS, _ref_desc=_REF_DESC,
        rr_min=settings.AI_RR_MIN,
        skill_index=skill_index,
    )
    if strategy_prompt:
        system += (
            "\n\n用户自定义交易策略（冲突时以自定义策略为准）：\n"
            "===== 自定义策略开始 =====\n"
            f"{strategy_prompt}\n"
            "===== 自定义策略结束 ====="
        )

    user_msg = _build_user_msg(signal, klines, ema, atr, user_input)
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
                "content": result[:12000],
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


def _build_facts(signal: dict, klines: list, atr: float) -> dict:
    """技能 use_when 判定用的布尔事实（委托 skill_library，与 P0 管线共用）"""
    return skill_library.build_facts(signal, klines)


def _build_user_msg(
    signal: dict, klines: list, ema: Optional[dict], atr: float,
    user_input: Optional[str],
) -> str:
    closed = klines[:-1] if len(klines) >= 2 else klines
    recent = closed[-60:]
    # 每根K线自带成交量与成交额（quote_vol，USDT），不再给汇总口径
    kline_summary = "\n".join(
        f"{int(k[0]/1000)},{k[1]},{k[2]},{k[3]},{k[4]},{k[5]},{k[7]}" for k in recent
    )
    # 近期摆动结构（HH/LH/HL/LL，收盘价摆动点）——锚点解析的数据源
    sw = signal.get("recent_swings") or {}
    swings_section = ""
    if sw.get("highs") or sw.get("lows"):
        sw_lines = ["近期摆动结构(收盘价摆动点,新→旧):"]
        if sw.get("highs"):
            sw_lines.append(
                "  高点: " + ", ".join(
                    f"{h['label']} {h['price']:.6g}（{h['bars_ago']}根前）" for h in sw["highs"]
                )
            )
        if sw.get("lows"):
            sw_lines.append(
                "  低点: " + ", ".join(
                    f"{lv['label']} {lv['price']:.6g}（{lv['bars_ago']}根前）" for lv in sw["lows"]
                )
            )
        swings_section = "\n".join(sw_lines) + "\n"
    # 量能（放量突破判定依据，classify_volume 口径：相对近20根已收盘均量的倍数）
    vol_desc = signal.get("volume_type") or "—"
    if signal.get("volume"):
        vol_desc += f"（{float(signal['volume']):.1f}×20根均量）"
    msg = (
        f"币种: {signal['symbol']}\n"
        f"信号类型: {signal['signal_type']}\n"
        f"当前价格: {signal['current_price']}\n"
        f"K线形态: {signal.get('pattern') or '无'}（位置: "
        f"{POSITION_LABEL_MAP.get(signal.get('position') or '', signal.get('position') or '未知')}）\n"
        f"信号理由: {signal.get('signal_reason') or ''}\n"
        f"信号强度: {signal.get('strength', '—')}\n"
        f"量能: {vol_desc}\n"
        f"ATR(14): {atr:.6g}\n"
        + (f"均线形态: {ema['state_label']}（{ema['detail']}）\n" if ema else "")
        + swings_section
        + f"近{len(recent)}根已收盘K线(timestamp,open,high,low,close,vol,quote_vol_USDT):\n{kline_summary}"
    )
    if user_input:
        msg += (
            "\n\n用户补充说明（请结合其内容进行分析）：\n"
            f"{user_input.strip()}"
        )
    return msg
