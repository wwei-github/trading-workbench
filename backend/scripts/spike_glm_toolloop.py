"""P1 前置 spike：GLM 思考模型 tool-calling 循环验证（docs/04 §8.2 第 2 条）

验证点：
1. OpenAI 兼容网关下 GLM 的 tool_calls 是否稳定返回
2. 多轮循环中 content/reasoning 是否异常（空 content、混排）
3. 每轮 token 消耗（含 reasoning）与总轮次、耗时

用法：cd backend && python3 scripts/spike_glm_toolloop.py [样本数]
"""
import json
import sys
import time

sys.path.insert(0, ".")

from openai import OpenAI  # noqa: E402

from app.config import settings  # noqa: E402

# ── 伪工具实现（协议验证用，数据为预置样例）──

MOCK_BREADTH = {
    "BTCUSDT": {"ema_state": "bullish", "ema_label": "多头排列", "change_24h_pct": 1.2},
    "ETHUSDT": {"ema_state": "bullish", "ema_label": "多头排列", "change_24h_pct": 0.8},
}
MOCK_KLINES = [
    {"ts": 1789000000000 + i * 3600000, "o": 100 + i * 0.1, "h": 101 + i * 0.1,
     "l": 99 + i * 0.1, "c": 100.5 + i * 0.1, "v": 1000 + i}
    for i in range(30)
]

TOOLS = [
    {"type": "function", "function": {
        "name": "get_market_breadth",
        "description": "获取大盘状态（BTC/ETH 的 EMA 形态与 24h 涨跌幅）",
        "parameters": {"type": "object", "properties": {}, "required": []},
    }},
    {"type": "function", "function": {
        "name": "get_recent_klines",
        "description": "获取该币种最近 N 根已收盘 K 线",
        "parameters": {"type": "object", "properties": {
            "n": {"type": "integer", "description": "根数，最大 60"},
        }, "required": ["n"]},
    }},
    {"type": "function", "function": {
        "name": "get_funding",
        "description": "获取该币种当前资金费率与持仓量",
        "parameters": {"type": "object", "properties": {}, "required": []},
    }},
    {"type": "function", "function": {
        "name": "submit_decision",
        "description": "提交最终交易决策（提交后结束分析）",
        "parameters": {"type": "object", "properties": {
            "trade_decision": {"type": "string", "enum": ["suggest", "skip"]},
            "direction": {"type": "string", "enum": ["long", "short", ""]},
            "entry_price": {"type": "number"},
            "stop_loss": {"type": "number"},
            "take_profit_1": {"type": "number"},
            "reason": {"type": "string"},
        }, "required": ["trade_decision", "reason"]},
    }},
]


def mock_tool(name: str, args: dict) -> str:
    if name == "get_market_breadth":
        return json.dumps(MOCK_BREADTH, ensure_ascii=False)
    if name == "get_recent_klines":
        return json.dumps(MOCK_KLINES[-min(int(args.get("n", 30)), 60):])
    if name == "get_funding":
        return json.dumps({"funding_rate_pct": 0.0101, "open_interest": 12345678})
    if name == "submit_decision":
        return "OK（已收到决策，分析结束）"
    return f"unknown tool: {name}"


SYSTEM = """你是交易分析师。可用工具查询数据，分析完成后必须调用 submit_decision 提交决策。
最多 8 轮工具调用。不要在文本中输出决策，只通过 submit_decision 提交。"""


def run_one(symbol: str, max_rounds: int = 8) -> dict:
    client = OpenAI(api_key=settings.AI_API_KEY, base_url=settings.AI_BASE_URL)
    messages = [
        {"role": "system", "content": SYSTEM},
        {"role": "user", "content": f"分析 {symbol}（信号: 支撑位+看涨吞没），决定 suggest/skip。"},
    ]
    usage_total = {"prompt": 0, "completion": 0}
    t0 = time.time()
    submitted = None
    anomalies: list[str] = []

    for rnd in range(1, max_rounds + 1):
        resp = client.chat.completions.create(
            model=settings.AI_MODEL, messages=messages, tools=TOOLS,
            temperature=0.3, max_tokens=12000,
        )
        msg = resp.choices[0].message
        u = resp.usage
        reasoning = getattr(u.completion_tokens_details, "reasoning_tokens", None) if u else None
        usage_total["prompt"] += u.prompt_tokens if u else 0
        usage_total["completion"] += u.completion_tokens if u else 0
        content_empty = not (msg.content or "").strip()
        if content_empty:
            anomalies.append(f"round{rnd}: content 为空")
        print(f"  round{rnd}: tool_calls={len(msg.tool_calls or [])} content={len(msg.content or '')}B "
              f"tokens(p{u.prompt_tokens}/c{u.completion_tokens}/r{reasoning}) finish={resp.choices[0].finish_reason}")

        if msg.tool_calls:
            messages.append({
                "role": "assistant",
                "content": msg.content or "",
                "tool_calls": [tc.model_dump() for tc in msg.tool_calls],
            })
            for tc in msg.tool_calls:
                args = json.loads(tc.function.arguments or "{}")
                result = mock_tool(tc.function.name, args)
                print(f"    -> {tc.function.name}({args}) => {result[:80]}")
                messages.append({
                    "role": "tool", "tool_call_id": tc.id,
                    "content": result,
                })
                if tc.function.name == "submit_decision":
                    submitted = args
        else:
            # 无 tool_calls：文本回复，追加以引导继续或结束
            if submitted:
                break
            anomalies.append(f"round{rnd}: 无 tool_calls 纯文本回复")
            messages.append({"role": "assistant", "content": msg.content or ""})
            messages.append({"role": "user", "content": "请调用 submit_decision 提交决策。"})
            continue

        if submitted:
            break

    dt = time.time() - t0
    return {
        "symbol": symbol, "rounds": rnd, "seconds": round(dt, 1),
        "tokens": usage_total, "submitted": submitted,
        "anomalies": anomalies,
    }


def main():
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 3
    symbols = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT"][:n]
    print(f"GLM tool-calling spike: model={settings.AI_MODEL} 样本={symbols}\n")
    results = [run_one(s) for s in symbols]

    print("\n===== 汇总 =====")
    ok = sum(1 for r in results if r["submitted"])
    rounds = [r["rounds"] for r in results]
    secs = [r["seconds"] for r in results]
    tok = [r["tokens"]["prompt"] + r["tokens"]["completion"] for r in results]
    for r in results:
        print(f"{r['symbol']}: rounds={r['rounds']} {r['seconds']}s "
              f"tokens={r['tokens']} submitted={'是' if r['submitted'] else '否'} "
              f"anomalies={r['anomalies'] or '无'}")
    print(f"\n成功率(通过 submit_decision 提交): {ok}/{len(results)}")
    print(f"轮次 avg={sum(rounds)/len(rounds):.1f} max={max(rounds)} | "
          f"耗时 avg={sum(secs)/len(secs):.1f}s max={max(secs)}s | "
          f"token avg={sum(tok)/len(tok):.0f} max={max(tok)}")


if __name__ == "__main__":
    main()
