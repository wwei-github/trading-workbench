"""AI 分析服务：调用 OpenAI 兼容接口，为命中币种生成开单建议"""
import json
import logging

from openai import OpenAI

from app.config import settings

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """你是加密货币合约交易分析师。基于提供的信号与近期K线数据，给出是否开单的建议。
直接输出JSON，不要思考过程，不要markdown包裹。字段如下：
- trade_decision: 是否建议开单，"suggest"（建议开单）或 "skip"（不建议开单）
- skip_reason: 当 trade_decision="skip" 时必填，说明理由（如：信号强度不足、假突破风险、盈亏比不佳、无明确反转形态等）。suggest 时输出空字符串。
- direction: 交易方向，"long"（做多）或 "short"（做空）。skip 时为空字符串。
- entry_price: 建议入场价（接近当前价）。skip 时为 0。
- stop_loss: 止损价，做多时低于入场价，做空时高于入场价，基于结构位。skip 时为 0。
- take_profit_1: 第一档止盈价，盈亏比≥1.5。skip 时为 0。
- take_profit_2: 第二档止盈价，盈亏比≥3。skip 时为 0。
- risk_reward_ratio: take_profit_1 的盈亏比，2位小数。skip 时为 0。
- position_pct: 建议仓位占总资金百分比，1-10，2位小数。skip 时为 0。
- recommendation: 推荐程度，0-100的整数。skip 时≤30，suggest 时≥50
- analysis: ≤200字中文推理过程，说明判断依据和风险点

所有价格为数值，单位 USDT。"""


def _get_client() -> OpenAI:
    return OpenAI(api_key=settings.AI_API_KEY, base_url=settings.AI_BASE_URL)


def analyze_coin(signal: dict, klines: list) -> dict:
    """对单个币种进行 AI 分析，返回结构化建议 dict。

    signal: 包含 symbol/signal_type/current_price/breakout_pct/pattern/signal_reason/volume_type/volume/volume_24h
    klines: 原始 K 线数据 [[open_time, open, high, low, close, volume, ...], ...]
    """
    # 取最近 30 根已收盘 K 线摘要（klines[-1] 未收盘，用 klines[-31:-1]）
    recent = klines[-31:-1] if len(klines) >= 31 else klines[:-1]
    kline_summary = "\n".join(
        f"{int(k[0]/1000)},{k[1]},{k[2]},{k[3]},{k[4]},{k[5]}" for k in recent
    )

    user_prompt = (
        f"币种: {signal['symbol']}\n"
        f"信号类型: {signal['signal_type']}\n"
        f"当前价格: {signal['current_price']}\n"
        f"突破幅度: {signal['breakout_pct']:.2f}%\n"
        f"K线形态: {signal.get('pattern') or '无'}\n"
        f"信号理由: {signal.get('signal_reason') or ''}\n"
        f"量能分类: {signal.get('volume_type', '未知')} (成交量={signal.get('volume', 0)})\n"
        f"24h成交额: {signal.get('volume_24h', 0)}\n"
        f"近{len(recent)}根已收盘K线(timestamp,open,high,low,close,vol):\n{kline_summary}"
    )

    client = _get_client()
    resp = client.chat.completions.create(
        model=settings.AI_MODEL,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ],
        temperature=0.3,
        max_tokens=2000,
    )

    content = resp.choices[0].message.content
    logger.info("AI 返回原始内容: %s", content)
    # 容错处理：去掉可能的 markdown 包裹
    content = content.strip()
    if content.startswith("```"):
        lines = content.split("\n")
        # 去掉首尾 ``` 行
        lines = [l for l in lines if not l.strip().startswith("```")]
        content = "\n".join(lines)
    return json.loads(content)
