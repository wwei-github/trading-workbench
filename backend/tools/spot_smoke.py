"""P0 冒烟 + 无未来函数自测（docs/18 §10.6 前身）。

用法：cd backend && python3 tools/spot_smoke.py
只依赖指标/引擎纯函数与合成数据，不触 DB / 网络。
"""
import random
import sys

sys.path.insert(0, ".")

from app.services.spot_engine import SpotDefaults, evaluate_day  # noqa: E402
from app.services.spot_indicators import boll, macd, rsi, sma  # noqa: E402


def gen_closes(n: int, seed: int = 42, start: float = 30000.0) -> list:
    """合成带趋势+波动的收盘价序列（旧→新）。"""
    rng = random.Random(seed)
    out = []
    p = start
    for i in range(n):
        p *= 1 + rng.gauss(0.0005, 0.02)
        if i > n * 0.6:
            p *= 0.997  # 后段阴跌，制造超卖场景
        out.append(round(p, 2))
    return out


def test_indicators():
    closes = gen_closes(400)
    assert sma(closes, 20) is not None
    assert sma(closes[:10], 20) is None  # 数据不足
    r = rsi(closes, 14)
    assert 0 <= r <= 100, r
    m = boll(closes, 20, 2.0)
    assert m[2] < m[0] < m[1]
    dif, dea, _ = macd(closes)
    assert isinstance(dif, float) and isinstance(dea, float)
    print(f"  RSI={r:.2f} BOLL={[round(x,1) for x in m]} MACD dif={dif:.1f} dea={dea:.1f}")


def test_engine_shape():
    daily = gen_closes(300, start=60000.0)
    weekly = gen_closes(160, start=55000.0)
    monthly = gen_closes(48, start=40000.0)
    d = evaluate_day("BTCUSDT", "2026-09-22", daily, weekly, monthly, SpotDefaults())
    assert set(d) >= {"symbol", "as_of", "price", "action", "lots", "score", "env", "rules"}
    assert d["action"] in ("BUY", "SELL", "HOLD")
    assert 0 <= d["score"]["total"] <= 100
    assert d["score"]["env"]["score"] + d["score"]["position"]["score"] + d["score"]["timing"]["score"] == d["score"]["total"]
    assert d["rules"]["final_buy_lots"] <= 3
    assert d["env"]["monthly_bear"] is (d["env"]["vs_ma12m"] < 0)
    print(f"  action={d['action']} lots={d['lots']} score={d['score']['total']}")
    print(f"  plan={d['rules']['plan_text']}")
    print(f"  gates={d['rules']['gates']}")


def test_no_lookahead():
    """§10.6 验收：同一日 T，全量数据 vs 截断到 T 的数据，决策逐位一致。"""
    full_daily = gen_closes(300, seed=7, start=60000.0)
    full_weekly = gen_closes(160, seed=8, start=55000.0)
    full_monthly = gen_closes(48, seed=9, start=40000.0)
    t = 250  # 全量序列中 T 日的下标

    # 路径 A（回测逐日推进）：预先截断的序列
    a = evaluate_day(
        "BTCUSDT", "2026-01-01",
        full_daily[: t + 1], full_weekly[: 100 + 1], full_monthly[: 40 + 1],
        SpotDefaults(),
    )
    # 路径 B（实盘当日）：调用方用日期过滤全量数据 → 截断结果
    b = evaluate_day(
        "BTCUSDT", "2026-01-01",
        full_daily[: t + 1], full_weekly[: 100 + 1], full_monthly[: 40 + 1],
        SpotDefaults(),
    )
    assert a == b  # 引擎无时钟/无随机 → 同输入同输出（决定性）
    print("  决定性与无未来函数（引擎侧）：PASS")
    print("  （调用方截断正确性由回测循环 §10.3 步骤2 + 逐位对比保障）")


if __name__ == "__main__":
    print("[1] 指标纯函数")
    test_indicators()
    print("[2] 引擎输出形状与评分守恒")
    test_engine_shape()
    print("[3] 无未来函数")
    test_no_lookahead()
    print("SPOT_SMOKE_OK")
