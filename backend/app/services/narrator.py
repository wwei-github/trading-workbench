"""Narrator：决策定稿后的叙述生成（docs/04 §5 P1 叙述分离）

- 决策 JSON 由 Agent 循环产出（不含叙述，token 省、结构稳）
- suggest 决策由本模块生成 ≤300 字中文叙述（轻量调用）；失败回退模板拼接
- skip 决策不调 LLM（skip_reason 已是结构化理由，前端直接展示）
"""
import logging
from typing import Optional

from openai import OpenAI

from app.config import settings
from app.services.strategy.types import POSITION_LABEL_MAP

logger = logging.getLogger(__name__)

_NARRATOR_SYSTEM = """你是交易叙述员。根据给定的决策与事实，输出 ≤300 字中文推理，格式：
先逐条列出满足的条件（如：1. EMA多头排列，趋势向上 2. 回踩支撑位企稳，触及2次）；
再列出不满足的条件或风险点；最后一条写明确结论。
每条独占一行（JSON 内换行写\\n），只输出纯文本，不要 JSON、不要 markdown。"""


def _template(signal: dict, decision: dict, reason: str) -> str:
    """LLM 失败时的模板兜底"""
    ref_label = POSITION_LABEL_MAP.get(signal.get("position") or "", "")
    return (
        f"1. 决策方向: {decision.get('direction')}，锚点 {ref_label or '市价'}，"
        f"入场 {decision.get('entry_price')}，止损 {decision.get('stop_loss')}，"
        f"盈亏比 {decision.get('risk_reward_ratio')}\n"
        f"2. 决策依据: {reason or '结构位信号 + 形态确认'}\n"
        f"3. 仓位 {decision.get('position_pct')}%（按风险预算公式计算），"
        f"推荐程度 {decision.get('recommendation')}"
    )


def generate_narrative(
    signal: dict, decision: dict, agent_reason: str = "",
    market_facts: Optional[dict] = None,
) -> str:
    """为 suggest 决策生成叙述。任何失败回退模板。"""
    try:
        client = OpenAI(api_key=settings.AI_API_KEY, base_url=settings.AI_BASE_URL)
        facts = [
            f"币种 {signal['symbol']}，当前价 {signal['current_price']}",
            f"信号: {signal.get('signal_type')} + {signal.get('pattern') or '无'}"
            f"（位置 {POSITION_LABEL_MAP.get(signal.get('position') or '', '未知')}）",
            f"决策: {decision.get('direction')}，入场 {decision.get('entry_price')}，"
            f"止损 {decision.get('stop_loss')}，止盈1 {decision.get('take_profit_1')}，"
            f"盈亏比 {decision.get('risk_reward_ratio')}，仓位 {decision.get('position_pct')}%",
        ]
        if agent_reason:
            facts.append(f"Agent 核心依据: {agent_reason}")
        f = (market_facts or {}).get("funding")
        if f:
            facts.append(f"资金费率 {f['funding_rate_pct']}%")
        b = (market_facts or {}).get("market_breadth")
        if b:
            facts.append("大盘 " + "；".join(
                f"{s} {v.get('ema_label') or '—'}" for s, v in b.items()
            ))
        resp = client.chat.completions.create(
            model=settings.AI_MODEL,
            messages=[
                {"role": "system", "content": _NARRATOR_SYSTEM},
                {"role": "user", "content": "\n".join(facts)},
            ],
            temperature=0.3,
            max_tokens=6000,
        )
        text = (resp.choices[0].message.content or "").strip()
        if not text:
            raise ValueError("叙述为空")
        # 截断防护：300 字上限
        return text[:600]
    except Exception as e:
        logger.warning("Narrator 生成失败（回退模板）: %s", e)
        return _template(signal, decision, agent_reason)
