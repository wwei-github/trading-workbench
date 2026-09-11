"""双评委辩论（docs/04 P2，移植 TradingAgents 多空辩论思想）

对 Trader 已产出的 suggest 决策做一次多空对抗复核：
- 多头评委（Bull）：为该决策找最有力的支持论据
- 空头评委（Bear）：找最有力的反驳论据
- 裁判（Judge）：基于辩论与事实包裁决 keep / veto

约束（安全边界）：
- 只对 suggest 决策生效，且只能 keep 或 veto（veto=转 forced skip），不能改价格/方向——
  裁判无权生成新交易参数，杜绝"评委幻觉价位"绕过 Risk Guard
- 开关 system_config.dual_judge_enabled（默认关）：辩论多花 3 次 LLM 调用，
  建议复盘数据校准后再开启
"""
import json
import logging
from typing import Optional

from openai import OpenAI

from app.config import settings
from app.services.risk_guard import build_forced_skip

logger = logging.getLogger(__name__)

BULL_PROMPT = """你是多头评委。以下是交易决策与市场事实，请用不超过120字给出最有力的支持论据（为什么这笔值得做）。
只输出论据正文，不要客套。

{decision}

{facts}"""

BEAR_PROMPT = """你是空头评委。以下是交易决策与市场事实，请用不超过120字给出最有力的反驳论据（为什么这笔不该做/风险在哪）。
只输出论据正文，不要客套。

{decision}

{facts}"""

JUDGE_PROMPT = """你是交易裁判。一笔已通过风控校验的建议如下，多空评委辩论如下。
你只有两个选择：
- "keep"：维持建议
- "veto"：否决该建议（整体风险/逻辑不成立）

注意：你没有修改价格或方向的权限，只能整体否决与否。摇摆不定时倾向 keep（决策已通过硬校验）。
输出 JSON（不要 markdown）：
{{"verdict": "keep" 或 "veto", "reason": "≤80字裁决理由"}}

原始决策:
{decision}

多头论据: {bull}
空头论据: {bear}"""


def _fmt_facts(signal: dict, market_facts: Optional[dict]) -> str:
    lines = [
        f"币种: {signal.get('symbol')}",
        f"信号类型: {signal.get('signal_type')}",
        f"当前价格: {signal.get('current_price')}",
        f"信号理由: {signal.get('signal_reason') or ''}",
    ]
    if market_facts:
        f = market_facts.get("funding")
        if f:
            lines.append(f"资金费率: {f.get('funding_rate_pct')}%")
        fg = market_facts.get("fear_greed")
        if fg:
            lines.append(f"恐贪指数: {fg.get('value')}({fg.get('label')})")
        b = market_facts.get("market_breadth")
        if b:
            lines.append("大盘: " + " | ".join(
                f"{s} 24h{v.get('change_24h_pct', 0):+.1f}%" for s, v in b.items()
            ))
    return "\n".join(lines)


def _fmt_decision(d: dict) -> str:
    if d.get("trade_decision") == "skip":
        return f"决策: skip（{d.get('skip_reason', '')}）"
    return (
        f"决策: suggest {d.get('direction')} {d.get('symbol', '')}\n"
        f"入场 {d.get('entry_price')} 止损 {d.get('stop_loss')} "
        f"止盈1 {d.get('take_profit_1')} 止盈2 {d.get('take_profit_2')}\n"
        f"盈亏比 {d.get('risk_reward_ratio')} 仓位 {d.get('position_pct')}% "
        f"推荐度 {d.get('recommendation')}\n"
        f"依据: {d.get('analysis', '')}"
    )


def run_dual_judge(
    decision: dict, signal: dict, market_facts: Optional[dict] = None,
) -> dict:
    """多空辩论复核。返回原决策（keep）或 forced skip（veto，skip_reason 附裁决理由）。

    任何异常（LLM 失败/JSON 解析失败）都降级为 keep——辩论是增强层，不能阻塞主流程。
    """
    if decision.get("trade_decision") != "suggest":
        return decision

    client = OpenAI(api_key=settings.AI_API_KEY, base_url=settings.AI_BASE_URL)
    d_text = _fmt_decision(decision)
    facts = _fmt_facts(signal, market_facts)

    def _ask(prompt: str) -> str:
        resp = client.chat.completions.create(
            model=settings.AI_MODEL,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.3,
            max_tokens=2000,
        )
        return (resp.choices[0].message.content or "").strip()

    try:
        bull = _ask(BULL_PROMPT.format(decision=d_text, facts=facts))
        bear = _ask(BEAR_PROMPT.format(decision=d_text, facts=facts))
        raw = _ask(JUDGE_PROMPT.format(decision=d_text, bull=bull, bear=bear))
        if raw.startswith("```"):
            raw = "\n".join(
                l for l in raw.split("\n") if not l.strip().startswith("```")
            ).strip()
        verdict = json.loads(raw)
        v = verdict.get("verdict")
        reason = str(verdict.get("reason", "")).strip()
        logger.info(
            "双评委 %s: %s — %s | 多头:%s | 空头:%s",
            signal.get("symbol"), v, reason, bull[:60], bear[:60],
        )
        if v == "veto":
            skipped = build_forced_skip(
                f"双评委否决：{reason or '辩论未通过'}"
            )
            skipped["recommendation"] = min(
                float(decision.get("recommendation") or 0), 30
            )
            return skipped
    except Exception as e:
        logger.warning("双评委失败（降级 keep）: %s", e)
    return decision
