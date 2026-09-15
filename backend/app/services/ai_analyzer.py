"""AI 分析服务：调用 OpenAI 兼容接口，为命中币种生成开单建议"""
import json
import logging
from typing import Optional

from openai import OpenAI

from app.config import settings
from app.services import skill_library
from app.services.llm_client import get_client
from app.services.risk_guard import build_forced_skip, parse_and_validate
from app.services.strategy.ema import analyze_ema
from app.services.strategy.types import POSITION_LABEL_MAP

logger = logging.getLogger(__name__)

# 信号类型中文对照（结构分类）
SIGNAL_TYPE_LABELS = {
    "uptrend": "上涨趋势（HH+HL）",
    "downtrend": "下跌趋势（LH+LL）",
    "trend_reversal": "趋势反转（收盘价破前高/前低）",
    "range_bound": "震荡区间",
    "breakout": "放量突破（收盘破 Donchian 通道 + 放量）",
    "unknown": "未分类",
    "manual_search": "手动搜索",
}

SYSTEM_PROMPT = """你是加密货币合约交易分析师。基于提供的信号与近期K线数据，给出是否开单的建议。
直接输出JSON，不要思考过程，不要markdown包裹。字段如下：
- trade_decision: 是否建议开单，"suggest"（建议开单）或 "skip"（不建议开单）
- skip_reason: 当 trade_decision="skip" 时必填，用"1. 2. 3."序号逐条列出不建议开单的主要原因（如：1. 信号强度不足 2. 盈亏比不佳），最多3条，每条一行，JSON内换行写\\n。suggest 时输出空字符串。
- direction: 交易方向，"long"（做多）或 "short"（做空）。skip 时为空字符串。
- trade_type: 开单类型（结构打法归类），suggest 时必填其一：trend_follow（顺势交易，EMA 三线严格排列是硬前提：多单 21>55>144、空单 144>55>21）/ structure_break（结构破位回踩，涵盖 123法则·N字结构·2B法则，见下方打法详解）/ range_edge（区间边缘反转）。skip 时为空字符串。
- entry_price: 建议入场价。必须以"当前价格"（分析时刻最新价）为基准：顺势追势时入场贴近现价；
  计划等回踩时给回踩锚定价（摆动低/高点）并在 analysis 中写明"等回踩"（程序会据此标记
  "待回踩"，仅作展示、不会为其下单）；若现价已显著离开
  摆动结构位且无合理入场计划，应 skip。skip 时为 0。
- 方向纪律（不再程序强制，2026-09-15 起无 ±0.3% 贴近要求）：优先只在支撑结构做多、
  只在压力结构做空；现价已离开结构位时不再要求贴近，可按动能顺势给入场价（顺势追势
  入场贴近现价即可），入场是否合理由盈亏比与止损结构把关。
- stop_loss: 止损价，做多时低于入场价，做空时高于入场价，基于结构位。skip 时为 0。
- take_profit_1: 第一档止盈价，做多=上方摆动高点下方留 0.2%~0.5% 余地，
  做空=下方摆动低点上方留 0.2%~0.5%，盈亏比≥1.5。距现价特别近（≤1×ATR）的位
  是当前争夺位、不算止盈目标，止盈一从更远的第一档结构位起算。skip 时为 0。
- take_profit_2: 第二档止盈价，锚定更前方一档摆动结构位（做多=更高一档摆动高点下方，
  做空=更低一档摆动低点上方，同样留余地）且必须比止盈一更远。skip 时为 0。
- risk_reward_ratio: take_profit_1 的盈亏比，2位小数。skip 时为 0。
- position_pct: 建议名义仓位占账户资金百分比，2位小数。程序会按固定亏损复算覆盖（仓位=3%÷止损距离%），此处报估算值即可。skip 时为 0。
- recommendation: 推荐程度，0-100的整数。skip 时≤30，suggest 时≥50
- analysis: ≤300字中文推理，用"1. 2. 3."序号逐条展示，每条独占一行（JSON内换行写\\n），格式如下：
  先逐条列出满足的条件（如：1. EMA多头排列，趋势向上 2. 回踩支撑位企稳，触及2次）；
  再列出不满足的条件或风险点；最后一条写明确结论（如：4. 结论：建议开单，盈亏比与趋势背景共振）。

结构破位回踩打法（structure_break）详解——三种子形态按行情阶段择一，判断依据写进 analysis：
1. 123法则（趋势反转）：跌破支撑位(1) → 反弹不创新低(2) → 再突破反弹高点（上方压力位）时入场(3，决定性确认)；止损放 123 转折点外侧。确认稳但滞后，入场偏晚。
2. N字结构（趋势延续）：回调不破支撑位 → 突破压力位 → 回踩不破支撑位入场；止损放 N 字中间低点，破即结构失效。
3. 2B法则（假突破反手）：创阶段新高后迅速收回压力位下方（或阶段新低后收回支撑位上方）→ 反向入场；止损放假突破极值外侧小缓冲。盈亏比最佳但胜率偏低，须严守止损。
共同缺点：震荡市表现最差（假突破连发）；归为震荡区间的信号应归 range_edge 而非本打法。优缺点与子形态选择理由须在 analysis 中说明。

止损纪律（结构位优先，影线极值兜底，两锚取更远者；程序不再改写止损价，合理性自行把关）：
- 优先：做多锚定入场价下方最近摆动低点外 0.3%（建议 0.3%~0.5%），做空锚定入场价上方最近摆动高点外同幅度。
- 两锚取更远者：入场贴着结构位是区间边缘类开单的常态，此时位在 5 根影线范围内，只锚结构位会把止损放进近期波动区间内部、极易被扫损。止损必须同时在结构位外与影线极值外——做多取摆动低点与最近 5 根最低点中更低者外 0.3%，做空取摆动高点与最近 5 根最高点中更高者外 0.3%。
- 兜底：入场价与极值之间没有任何摆动结构位时，用极值锚——做多严格低于最近 5 根最低点（影线极值即最低价，非收盘价）至少 0.3%，做空严格高于最高点至少 0.3%。
- 止损宽度不设固定百分比上限；若盈亏比不达标，优先调整入场价，仍不行则 skip。

止盈锚定（多空方向不同，必须遵守；程序不再改挂止盈价）：
- 结构位=摆动高低点（用户消息中"近期摆动结构"）。
  **近位不算止盈目标**：距现价特别近（≤1×ATR）的位是价格正在测试/突破的
  "争夺位"，它回答的是"价格是否得到支撑/压力/突破"，不是盈利空间；
  止盈一必须挂在更远的第一档结构位外侧：
  做多 = 上方摆动高点下方 0.2%~0.5%，不得挂在位上方或贴死位；
  做空 = 下方摆动低点上方 0.2%~0.5%，不得挂在位下方或贴死位。
  价格常在位附近反弹，深入越位才触发的止盈大概率落空。
- 禁止把止盈一挂到更远结构位凑盈亏比：到第一结构位的盈亏比不足 1.5 说明这笔交易
  空间不足（第一结构位都够不着 1.5R），应直接 skip，不要跳档（程序按盈亏比铁律
  复算打回）。
- 止盈二：挂在止盈一锚定档的下一档结构位外侧（做多=更高一档摆动高点下方，
  做空=更低一档摆动低点上方），同样留余地；止盈二必须比止盈一更远。
- 若入场方向前方没有任何结构位可锚定（如创新高突破）：止盈一 = 入场价 ± 1.5×止损距离（盈亏比恰达标），
  止盈二设 0（仅设一档）。

所有价格为数值，单位 USDT。
均线形态是重要趋势背景：多头排列支撑做多逻辑，空头排列支撑做空逻辑；
金叉/死叉与拐头是动能转换信号；若均线形态与摆动结构信号方向矛盾，
需谨慎评估并在 analysis 中说明理由。
资金费率/持仓量/大盘状态/恐贪指数等市场环境数据未随消息提供，请勿臆测：
仅基于给定的 K 线、均线、量能与摆动结构数据进行分析。"""


def _get_client() -> OpenAI:
    # 共享单例：复用连接池，统一超时与重试策略（见 llm_client）
    return get_client()


def analyze_coin(
    signal: dict, klines: list,
    strategy_prompt: Optional[str] = None, user_input: Optional[str] = None,
) -> dict:
    """对单个币种进行 AI 分析（单次调用，返回原始 JSON dict）。

    signal: 包含 symbol/signal_type/current_price/breakout_pct/pattern/signal_reason/
            position/volume_type/volume/volume_24h（recent_swings 可选注入）
    klines: 原始 K 线数据 [[open_time, open, high, low, close, volume, ...], ...]
    strategy_prompt: 用户自定义交易策略（MD 格式），提供时附加到系统提示词供 AI 参考
    user_input: 本次分析的用户补充说明（要求/持仓计划/个人观点），附加到用户提示词
    """
    client = _get_client()
    return _call_llm(
        client,
        _build_messages(signal, klines, strategy_prompt, user_input),
    )


def _build_messages(
    signal: dict, klines: list,
    strategy_prompt: Optional[str] = None, user_input: Optional[str] = None,
) -> list:
    """构造 LLM messages（analyze_coin / analyze_with_guard 共用）"""
    # 取最近 60 根已收盘 K 线摘要（klines[-1] 未收盘，用 klines[-61:-1]）
    recent = klines[-61:-1] if len(klines) >= 61 else klines[:-1]
    # 每根K线自带成交量与成交额（quote_vol，USDT），与 Agent 管线同格式
    kline_summary = "\n".join(
        f"{int(k[0]/1000)},{k[1]},{k[2]},{k[3]},{k[4]},{k[5]},{k[7]}" for k in recent
    )

    # 信号形态出现位置标签
    position_label = POSITION_LABEL_MAP.get(signal.get("position") or "", "")

    # 均线形态（EMA21/55/144 状态判定，基于已收盘 K 线实时计算）
    ema = analyze_ema(klines)
    ema_summary = ""
    if ema:
        ema_summary = f"均线形态: {ema['state_label']}（{ema['detail']}）\n"

    # 近期摆动结构（HH/LH/HL/LL，收盘价摆动点，与 Agent 管线同源）
    sw = signal.get("recent_swings") or {}
    swings_summary = ""
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
        swings_summary = "\n".join(sw_lines) + "\n"

    # 量能（放量突破判定依据，volume_ratio 口径：相对近20根已收盘均量的倍数）
    vol_desc = signal.get("volume_type") or "—"
    if signal.get("volume"):
        vol_desc += f"（{float(signal['volume']):.1f}×20根均量）"

    # 市场环境（资金费率/恐贪/大盘）不再注入——单次调用管线无工具，模型仅凭给定数据分析
    user_prompt = (
        f"币种: {signal['symbol']}\n"
        f"信号类型: {SIGNAL_TYPE_LABELS.get(signal['signal_type'], signal['signal_type'])}（{signal['signal_type']}）\n"
        f"当前价格: {signal['current_price']}\n"
        f"突破幅度: {signal['breakout_pct']:.2f}%\n"
        f"K线形态: {signal.get('pattern') or '无'}\n"
        f"信号理由: {signal.get('signal_reason') or ''}\n"
        f"形态出现位置: {position_label or signal.get('position') or '未知'}\n"
        f"量能: {vol_desc}\n"
        f"{ema_summary}"
        f"{swings_summary}"
        f"近{len(recent)}根已收盘K线(timestamp,open,high,low,close,vol,quote_vol_USDT):\n{kline_summary}"
    )

    if user_input:
        user_prompt += (
            "\n\n用户补充说明（用户基于自身判断提供的信息或要求，请结合其内容进行分析）：\n"
            f"{user_input.strip()}"
        )

    system_prompt = SYSTEM_PROMPT
    # 技能注入（P0 管线无工具循环，命中技能正文直接进系统提示词；与 Agent 管线共用同一事实判定）
    skill_block = skill_library.render_matched(skill_library.build_facts(signal, klines))
    if skill_block:
        system_prompt += (
            "\n\n以下技能与当前信号命中相关（use_when 条件已程序判定），请遵循其规则：\n"
            f"{skill_block}"
        )
    if strategy_prompt:
        system_prompt += (
            "\n\n以下是用户自定义交易策略，请优先遵循其规则进行分析，"
            "与默认规则冲突时以自定义策略为准：\n"
            "===== 自定义策略开始 =====\n"
            f"{strategy_prompt}\n"
            "===== 自定义策略结束 ====="
        )

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]
    return messages


def _call_llm(client: OpenAI, messages: list) -> dict:
    """单次 LLM 调用 → 原始 JSON dict；空内容/解析失败抛异常"""
    # GLM 系列为思考型模型，reasoning tokens 也计入 max_tokens，
    # 配额太小会导致正文为空或被截断（finish_reason=length），这里放大配额
    resp = client.chat.completions.create(
        model=settings.AI_MODEL,
        messages=messages,
        temperature=0.3,
        max_tokens=12000,
    )
    finish = resp.choices[0].finish_reason
    content = (resp.choices[0].message.content or "").strip()
    logger.info("AI 返回原始内容(finish=%s): %s", finish, content[:300])
    if not content:
        raise ValueError(f"AI 返回空内容 (finish_reason={finish})")
    # 容错处理：去掉可能的 markdown 包裹
    if content.startswith("```"):
        lines = [l for l in content.split("\n") if not l.strip().startswith("```")]
        content = "\n".join(lines).strip()
    return json.loads(content)


def analyze_with_guard(
    signal: dict, klines: list,
    strategy_prompt: Optional[str] = None, user_input: Optional[str] = None,
) -> dict:
    """带 Risk Guard 的分析入口：校验失败把违规明细反馈给模型重试（≤2 次），耗尽后强制 skip。

    返回值为校验修正后的决策 dict（rr/position_pct 为程序复算值），可直接落库。
    """
    prompt_messages = _build_messages(
        signal, klines, strategy_prompt=strategy_prompt, user_input=user_input,
    )
    client = _get_client()

    last_violations: list[str] = []
    for attempt in range(3):  # 1 次初始 + 2 次反馈重试
        try:
            raw = _call_llm(client, prompt_messages)
        except (ValueError, json.JSONDecodeError) as e:
            last_violations = [f"输出无法解析: {str(e)[:150]}"]
            logger.warning("AI 第%d次输出解析失败: %s", attempt + 1, e)
            continue  # messages 不变直接重试

        ok, violations, fixed = parse_and_validate(raw, signal, klines)
        if ok:
            logger.info(
                "AI 决策通过校验: %s %s rr=%s",
                signal.get("symbol"), fixed.get("trade_decision"),
                fixed.get("risk_reward_ratio"),
            )
            return fixed
        last_violations = violations
        logger.warning(
            "AI 决策未过校验(第%d次): %s", attempt + 1, violations,
        )
        # 违规明细作为反馈消息进入上下文，模型看着错处修改（Reflection）
        prompt_messages.append({
            "role": "assistant",
            "content": json.dumps(raw, ensure_ascii=False),
        })
        prompt_messages.append({
            "role": "user",
            "content": (
                "你的决策未通过程序风控校验，违规项如下：\n"
                + "\n".join(f"- {v}" for v in violations)
                + "\n请逐条修正后，重新输出完整 JSON（字段要求与最初一致）。"
            ),
        })

    return build_forced_skip("AI 输出多次未通过风控校验", last_violations)
