"""AI 分析服务：调用 OpenAI 兼容接口，为命中币种生成开单建议"""
import json
import logging
from typing import Optional

from openai import OpenAI

from app.config import settings
from app.services.strategy.types import POSITION_LABEL_MAP

logger = logging.getLogger(__name__)

# 信号类型中文对照（关键位重构后的结构分类）
SIGNAL_TYPE_LABELS = {
    "uptrend": "上涨趋势（HH+HL）",
    "downtrend": "下跌趋势（LH+LL）",
    "trend_reversal": "趋势反转（收盘价破前高/前低）",
    "range_bound": "震荡区间",
    "unknown": "未分类",
    "manual_search": "手动搜索",
}

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


def analyze_coin(signal: dict, klines: list, strategy_prompt: Optional[str] = None) -> dict:
    """对单个币种进行 AI 分析，返回结构化建议 dict。

    signal: 包含 symbol/signal_type/current_price/breakout_pct/pattern/signal_reason/
            position/key_levels/volume_type/volume/volume_24h
    klines: 原始 K 线数据 [[open_time, open, high, low, close, volume, ...], ...]
    strategy_prompt: 用户自定义交易策略（MD 格式），提供时附加到系统提示词供 AI 参考
    """
    # 取最近 30 根已收盘 K 线摘要（klines[-1] 未收盘，用 klines[-31:-1]）
    recent = klines[-31:-1] if len(klines) >= 31 else klines[:-1]
    kline_summary = "\n".join(
        f"{int(k[0]/1000)},{k[1]},{k[2]},{k[3]},{k[4]},{k[5]}" for k in recent
    )

    # 关键位明细（12金K出现的位置 + 全部关键位）
    position_label = POSITION_LABEL_MAP.get(signal.get("position") or "", "")
    levels_summary = ""
    if signal.get("key_levels"):
        lines = []
        for lv in signal["key_levels"]:
            kind = POSITION_LABEL_MAP.get(lv.get("kind"), lv.get("kind", "?"))
            role = "支撑" if lv.get("role") == "support" else "压力"
            lines.append(
                f"  {kind}: {lv['price']:.6g} ({role}, 触及{lv.get('touches', 1)}次, "
                f"区域{lv['zone_low']:.6g}~{lv['zone_high']:.6g})"
            )
        levels_summary = "关键位列表:\n" + "\n".join(lines) + "\n"

    user_prompt = (
        f"币种: {signal['symbol']}\n"
        f"信号类型: {SIGNAL_TYPE_LABELS.get(signal['signal_type'], signal['signal_type'])}（{signal['signal_type']}）\n"
        f"当前价格: {signal['current_price']}\n"
        f"突破幅度: {signal['breakout_pct']:.2f}%\n"
        f"K线形态: {signal.get('pattern') or '无'}\n"
        f"信号理由: {signal.get('signal_reason') or ''}\n"
        f"形态出现位置: {position_label or signal.get('position') or '未知'}\n"
        f"{levels_summary}"
        f"量能分类: {signal.get('volume_type', '未知')} (成交量={signal.get('volume', 0)})\n"
        f"24h成交额: {signal.get('volume_24h', 0)}\n"
        f"近{len(recent)}根已收盘K线(timestamp,open,high,low,close,vol):\n{kline_summary}"
    )

    system_prompt = SYSTEM_PROMPT
    if strategy_prompt:
        system_prompt += (
            "\n\n以下是用户自定义交易策略，请优先遵循其规则进行分析，"
            "与默认规则冲突时以自定义策略为准：\n"
            "===== 自定义策略开始 =====\n"
            f"{strategy_prompt}\n"
            "===== 自定义策略结束 ====="
        )

    client = _get_client()
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]

    # GLM 系列为思考型模型，reasoning tokens 也计入 max_tokens，
    # 配额太小会导致正文为空或被截断（finish_reason=length），这里放大配额并失败重试一次
    last_err: Exception = ValueError("AI 分析未执行")
    for attempt in range(2):
        resp = client.chat.completions.create(
            model=settings.AI_MODEL,
            messages=messages,
            temperature=0.3,
            max_tokens=12000,
        )
        finish = resp.choices[0].finish_reason
        content = (resp.choices[0].message.content or "").strip()
        logger.info(
            "AI 返回原始内容(第%d次, finish=%s): %s",
            attempt + 1, finish, content[:300],
        )
        if not content:
            last_err = ValueError(f"AI 返回空内容 (finish_reason={finish})")
            continue
        # 容错处理：去掉可能的 markdown 包裹
        if content.startswith("```"):
            lines = [l for l in content.split("\n") if not l.strip().startswith("```")]
            content = "\n".join(lines).strip()
        try:
            return json.loads(content)
        except json.JSONDecodeError as e:
            last_err = e
    raise last_err
