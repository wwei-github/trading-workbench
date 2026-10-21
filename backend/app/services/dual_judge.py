"""双评委辩论（docs/04 P2，移植 TradingAgents 多空辩论思想）

对 Trader 已产出的 suggest 决策做一次多空对抗复核：
- 多头评委（Bull）：为该决策找最有力的支持论据
- 空头评委（Bear）：找最有力的反驳论据
- 裁判（Judge）：基于辩论与事实包裁决 keep / veto

约束（安全边界）：
- 只对 suggest 决策生效，且只能 keep 或 veto（veto=转 forced skip），不能改价格/方向——
  裁判无权生成新交易参数，杜绝"评委幻觉价位"绕过 Risk Guard
- 开关 system_config.dual_judge_enabled（默认开）：辩论多花 3 次 LLM 调用
"""
import logging
from concurrent.futures import ThreadPoolExecutor

from app.services.llm_client import get_client, json_mode_kwargs, parse_llm_json, stream_completion
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
用指标块数据核对决策三价是否与结构位矛盾（如止损落在摆动区间内部、止盈近于争夺位）。
输出 JSON（不要 markdown）：
{{"verdict": "keep" 或 "veto", "reason": "≤80字裁决理由"}}

原始决策:
{decision}

多头论据: {bull}
空头论据: {bear}"""


def _fmt_facts(signal: dict) -> str:
    lines = [
        f"币种: {signal.get('symbol')}",
        f"信号类型: {signal.get('signal_type')}",
        f"当前价格: {signal.get('current_price')}",
        f"信号理由: {signal.get('signal_reason') or ''}",
    ]
    # 事实包增厚（docs/24 D3）：调用方可选注入指标块与摆动结构摘要，
    # 评委才能用数据核对三价与结构位矛盾；未传字段时保持原有 4 行（向后兼容）
    if signal.get("indicator_block"):
        lines.append("【指标数据】")
        lines.append(str(signal["indicator_block"]).strip())
    if signal.get("structure_summary"):
        lines.append("【摆动结构】")
        lines.append(str(signal["structure_summary"]).strip())
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
    decision: dict, signal: dict,
) -> dict:
    """多空辩论复核。返回原决策（keep）或 forced skip（veto，skip_reason 附裁决理由）。

    裁判 JSON 解析失败先反馈回炉重试 ≤2 次（docs/24 D2），3 次仍不可解析降级 keep；
    其余任何异常（LLM 调用失败等）直接降级为 keep——辩论是增强层，不能阻塞主流程。
    """
    if decision.get("trade_decision") != "suggest":
        return decision

    client = get_client()
    d_text = _fmt_decision(decision)
    facts = _fmt_facts(signal)

    def _ask(messages: list, json_mode: bool = False) -> str:
        # 流式（2026-09-30）：评委/裁判与主分析同网关同模型，非流式长推理挂死
        # 风险相同（见 llm_client.stream_completion），一并切换
        r = stream_completion(
            client, messages,
            temperature=0.3,
            # 32000（2026-10-16）：与 ai_analyzer/ai_agent/position_advisor 等同一口径。
            # GLM 思考型模型 reasoning tokens 计入配额，原 2000 常被思考整体耗尽 →
            # 正文为空 → 裁判 JSON 解析抛 "Expecting value: line 1 column 1" →
            # 双评委静默降级 keep，否决能力实效失效
            max_tokens=32000,
            # 仅裁判输出 JSON（多空评委输出论据正文），JSON 模式只挂在裁判调用上
            **(json_mode_kwargs() if json_mode else {}),
        )
        return r.content

    def _strip_fence(s: str) -> str:
        """剥掉 markdown 代码围栏残留（网关偶发用 ```json 包裹）"""
        if s.startswith("```"):
            return "\n".join(
                l for l in s.split("\n") if not l.strip().startswith("```")
            ).strip()
        return s

    try:
        # 多空评委互不依赖，并行取论据（原串行 = 两次完整调用时长相加）
        with ThreadPoolExecutor(max_workers=2) as ex:
            fut_bull = ex.submit(_ask, [{
                "role": "user", "content": BULL_PROMPT.format(decision=d_text, facts=facts),
            }])
            fut_bear = ex.submit(_ask, [{
                "role": "user", "content": BEAR_PROMPT.format(decision=d_text, facts=facts),
            }])
        bull = fut_bull.result()
        bear = fut_bear.result()
        judge_msgs = [{
            "role": "user",
            "content": JUDGE_PROMPT.format(decision=d_text, bull=bull, bear=bear),
        }]
        verdict = None
        last_err = ""
        for attempt in range(3):  # 1 次初始 + 2 次解析失败反馈重试（docs/24 D2）
            # 调用异常（网关超时等）直接抛外层 → 降级 keep，不在此重试
            raw = _ask(judge_msgs, json_mode=True)
            try:
                parsed = parse_llm_json(_strip_fence(raw))
                if not isinstance(parsed, dict):
                    raise ValueError(f"裁判输出非 JSON 对象（{type(parsed).__name__}）")
                verdict = parsed
                break
            except Exception as e:
                last_err = str(e)[:150]
                logger.warning(
                    "双评委裁判解析失败（第%d次）: %s", attempt + 1, last_err,
                )
                # 反馈回炉（仿 ai_analyzer，docs/24 D2）：上次输出非空时以 assistant
                # 回放进上下文，模型看着原文改错；空内容无原文可回放，仅附错误说明
                if raw.strip():
                    judge_msgs.append({"role": "assistant", "content": raw})
                judge_msgs.append({
                    "role": "user",
                    "content": (
                        f"上一次输出 JSON 解析失败：{last_err}。"
                        '请严格只输出 JSON 对象：{"verdict": "keep" 或 "veto", '
                        '"reason": "裁决理由"}，不要任何其他文本或 markdown 包裹'
                    ),
                })
        if verdict is None:
            # 重试耗尽仍未拿到可解析裁决 → 降级 keep（辩论是增强层，不比 forced skip）
            logger.warning("双评委裁判 3 次输出均不可解析，降级 keep: %s", last_err)
            return decision
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
            # veto 闭环（docs/24 A5a）：原始建议三价随 skip 快照落库，
            # 复盘任务据此回放"被否决建议的假想表现"，对比通过单验证 veto 贡献；
            # source 标记否决来源（当前仅 dual_judge；历史行另有 htf_constraint，
            # 该来源 2026-10-21 随 AI 层方向兜底退役），便于分组对比
            skipped["veto_original"] = {
                "source": "dual_judge",
                "direction": decision.get("direction"),
                "entry_price": decision.get("entry_price"),
                "stop_loss": decision.get("stop_loss"),
                "take_profit_1": decision.get("take_profit_1"),
                "take_profit_2": decision.get("take_profit_2"),
                "risk_reward_ratio": decision.get("risk_reward_ratio"),
                "recommendation": decision.get("recommendation"),
                "reason": reason,
            }
            return skipped
    except Exception as e:
        logger.warning("双评委失败（降级 keep）: %s", e)
    return decision
