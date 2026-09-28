"""AI 错误归因（复盘重设计 2026-09-18）

对已平仓交易，把「AI 当时的计划 + 实际结果 + K 线特征」喂给 AI，
输出结构化错误标签与一句话教训，落库到 trade_records
（error_tags / error_lesson / attributed_at）。

- 标签词表固定（ERROR_TAGS），保证分组统计口径一致
- 事实由程序计算（MFE/MAE、入场位置、止损宽度），AI 只做归因判断
- 每笔仅归因一次（attributed_at 幂等），支持定时批量与手动单笔触发
"""
import json
import logging
from datetime import datetime, timezone
from typing import Optional

from app.config import settings
from app.services.llm_client import get_client, parse_llm_json

logger = logging.getLogger(__name__)

# 错误标签词表（固定口径，分组统计按标签字符串聚合）
ERROR_TAGS: dict[str, str] = {
    "追高追空": "入场位置差：价格已远离结构位后追入，回撤空间大",
    "止损过宽": "止损距离过大，风险占用过高甚至触发强平风险",
    "止损过窄": "止损太贴近入场价，被正常波动扫掉后行情才走出",
    "逆指标多数": "开仓方向与指标多数决相悖（净票数反向或接近平衡）",
    "逆4h背景": "开仓方向与 4h 大级别趋势/均线排列相反",
    "区间中段开单": "价格处于盘整区间中部，无边缘或突破确认",
    "执行偏差": "滑点/手续费/资金费侵蚀超出计划（计划外亏损来源）",
    "行情异常": "突发消息或极端波动，计划内无法预见",
    "无明显错误": "计划内亏损：入场/止损/仓位均符合纪律，行情未走出",
}
ERROR_TAG_OTHER = "其他"

ATTRIBUTION_SYSTEM = """你是期货交易系统的复盘教练。根据给出的一笔已平仓交易的事实\
（AI 开单计划、实际结果、K 线特征），诊断这笔交易的主要错误并给出教训。

要求：
1. tags 从以下词表中选 1~3 个（只能用词表内的标签，不多不少按相关度排序）：
%s
2. lesson 是一句话教训（≤80字），具体、可执行、针对该笔交易的根因，禁止空话套话。
3. 事实优先于推测：给定的 K 线特征（入场位置/MFE/MAE/止损宽度）是硬数据。
只输出 JSON：{"tags": ["标签"], "lesson": "..."}，不要输出其他内容。
""" % "\n".join(f"- {k}：{v}" for k, v in ERROR_TAGS.items())


def _interval_ms(interval: str) -> int:
    unit = interval[-1]
    n = int(interval[:-1] or 1)
    return n * {"m": 60_000, "h": 3_600_000, "d": 86_400_000, "w": 604_800_000}[unit]


def build_facts(rec, klines: list) -> dict:
    """归因事实包：AI 计划 + 实际结果 + 程序计算的 K 线特征。klines 为历史区间
    （开仓前 ~100 根 + 持仓全程，升序已收盘），与交易复盘K线接口同源。"""
    snap = rec.ai_snapshot or {}
    opened = rec.opened_at
    closed = rec.closed_at or datetime.utcnow()
    opened_ms = int(opened.replace(tzinfo=timezone.utc).timestamp() * 1000) if opened else 0

    entry = float(rec.entry_price or snap.get("entry_price") or 0)  # 实际成交（MFE/MAE 基准）
    plan_entry = float(snap.get("entry_price") or rec.entry_price or 0)  # AI 计划价（风控校验口径）
    plan_sl = float(snap.get("stop_loss") or 0)
    plan_tp1 = float(snap.get("take_profit_1") or 0)
    direction = rec.direction or ""

    pre = [k for k in klines if int(k[0]) < opened_ms][-20:]
    held = [k for k in klines if int(k[0]) >= opened_ms]

    mfe = mae = None
    entry_pos = None
    if held and entry > 0:
        if direction == "long":
            mfe = round((max(float(k[2]) for k in held) / entry - 1) * 100, 2)
            mae = round((1 - min(float(k[3]) for k in held) / entry) * 100, 2)
        else:
            mfe = round((1 - min(float(k[3]) for k in held) / entry) * 100, 2)
            mae = round((max(float(k[2]) for k in held) / entry - 1) * 100, 2)
    if pre and entry > 0:
        hi, lo = max(float(k[2]) for k in pre), min(float(k[3]) for k in pre)
        if hi > lo:
            # 入场在开仓前 20 根区间中的位置：1=区间顶（多头追高）、0=区间底（空头追空）。
            # 不做 [0,1] 截断：>1 或 <0 表示入场已越出近期区间，追高/追空信号更强
            entry_pos = round((entry - lo) / (hi - lo), 2)

    stop_pct = round(abs(plan_entry - plan_sl) / plan_entry * 100, 2) \
        if plan_entry > 0 and plan_sl > 0 else None
    rr = round(abs(plan_tp1 - plan_entry) / abs(plan_entry - plan_sl), 2) \
        if plan_entry and plan_sl and plan_tp1 and abs(plan_entry - plan_sl) > 0 else None
    hold_hours = round((closed - opened).total_seconds() / 3600, 1) if opened else None

    return {
        "symbol": rec.symbol,
        "direction": direction,
        "trade_type": snap.get("trade_type"),
        "recommendation": snap.get("recommendation"),
        "position_pct": snap.get("position_pct"),
        "plan": {
            "entry": snap.get("entry_price"), "stop_loss": plan_sl or None,
            "take_profit_1": plan_tp1 or None, "take_profit_2": snap.get("take_profit_2"),
            "stop_distance_pct": stop_pct, "risk_reward": rr,
        },
        "actual": {
            "entry_fill": float(rec.entry_price) if rec.entry_price else None,
            "exit_reason": rec.exit_reason,
            "realized_pnl": float(rec.realized_pnl) if rec.realized_pnl is not None else None,
            "pnl_pct": float(rec.pnl_pct) if rec.pnl_pct is not None else None,
            "fee_total": float(rec.fee_total) if rec.fee_total is not None else None,
            "funding_total": float(rec.funding_total) if rec.funding_total is not None else None,
            "hold_hours": hold_hours,
        },
        "kline_facts": {
            "entry_pos_in_20bar_range": entry_pos,  # 1=区间顶 0=区间底
            "mfe_pct": mfe,  # 持仓期间最大有利波动%
            "mae_pct": mae,  # 持仓期间最大不利波动%
            "held_bars": len(held),
        },
        "ai_analysis": (snap.get("analysis") or "")[:1500],
    }


def attribute_trade(rec, klines: list) -> dict:
    """单笔归因：调 LLM 输出 {tags, lesson}；词表校验后返回。"""
    from openai import OpenAI  # 局部导入避免无谓依赖

    client: OpenAI = get_client()
    facts = build_facts(rec, klines)
    resp = client.chat.completions.create(
        model=settings.AI_MODEL,
        messages=[
            {"role": "system", "content": ATTRIBUTION_SYSTEM},
            {"role": "user", "content": json.dumps(facts, ensure_ascii=False)},
        ],
        temperature=0.2,
        max_tokens=32000,  # GLM 思考型：reasoning 计入配额，12000 被思考耗尽过（同 ai_analyzer 口径）
    )
    content = (resp.choices[0].message.content or "").strip()
    if content.startswith("```"):
        lines = [l for l in content.split("\n") if not l.strip().startswith("```")]
        content = "\n".join(lines).strip()
    data = parse_llm_json(content)

    raw_tags = data.get("tags") or []
    tags = [t for t in raw_tags if t in ERROR_TAGS][:3]
    if not tags:
        tags = [ERROR_TAG_OTHER]
    lesson = str(data.get("lesson") or "").strip()[:120]
    return {"tags": tags, "lesson": lesson}
