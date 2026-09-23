"""回测引擎冒烟：合成数据端到端跑通 + 定投基准口径验证（无网络/无DB依赖）。"""
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.spot_backtest import run_backtest  # noqa: E402
from app.services.spot_engine import SpotDefaults  # noqa: E402

# ISO 周一锚点：2024-01-01 是周一；逐日生成 ~14 个月的日K
import datetime as dt  # noqa: E402


def _mk_klines(start: dt.date, n: int, seed: int, base: float):
    out = []
    t0 = int(dt.datetime(start.year, start.month, start.day, tzinfo=dt.timezone.utc).timestamp() * 1000)
    for i in range(n):
        day = start + dt.timedelta(days=i)
        t = t0 + i * 86_400_000
        drift = base * (1.0 + 0.0012 * i)
        wave = base * 0.18 * math.sin(i / 11.0)
        noise = base * 0.04 * math.sin(i * 5.31 * seed % 360)
        out.append({"t": t, "c": round(drift + wave + noise, 4)})
    return out


START = dt.date(2024, 1, 1)
daily = {"BTCUSDT": _mk_klines(START, 430, 3, 30000.0)}
weekly = {"BTCUSDT": _mk_klines(START, 62, 5, 28000.0)}
monthly = {"BTCUSDT": _mk_klines(START, 15, 9, 25000.0)}

cfg = SpotDefaults(per_order_usdt=100.0, max_total_usdt=3000.0,
                   dca_enabled=True, dca_amount=50.0, usdt_reserve_pct=0.0)

res = run_backtest(["BTCUSDT"], daily, weekly, monthly, budget=3000.0,
                   settings=cfg, start="2024-02-01", end="2025-02-28")
s = res["summary"]

print("bars:", s["bars"], "window:", s["start"], "→", s["end"])
print("orders:", s["orders_count"], "dist:", s["action_dist"])
print("total_qty:", s["total_qty"], "bench_qty:", s["benchmark"]["total_qty"])
print("excess_qty:", s["excess_qty"])
print("series points:", len(res["daily_series"]),
      "first:", res["daily_series"][0], "\nlast:", res["daily_series"][-1])

# 断言
assert s["bars"] > 300
assert s["orders_count"] > 0
assert len(res["daily_series"]) == s["bars"]
# 定投基准必有投入（周一 14 个月 ≈ 60 周 × 50U）
assert s["benchmark"]["invested"] > 2000, f"基准投入异常: {s['benchmark']['invested']}"
# 现金守恒：预算 − 买入支出 + 卖出回款 = cash_left
bought = sum(o["quote"] + o["fee"] for o in res["orders"] if o["side"] == "BUY")
sold = sum(o["quote"] - o["fee"] for o in res["orders"] if o["side"] == "SELL")
left = 3000.0 - bought + sold
assert abs(left - s["cash_left"]) < 0.5, f"现金守恒失败: {left} vs {s['cash_left']}"
print("BACKTEST_SMOKE_PASSED")
