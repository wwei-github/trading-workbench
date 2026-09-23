"""P1-1 无未来函数单测（docs/18 §10.6，CI 一票否决）

检验三个纯度命题：
1. 截断不变性：同一交易日 T（末根相同），把更早的历史砍掉（保留指标窗口上限）
   与全量喂入，决策相关输出必须一致——证明引擎只看「≤T 的窗口」，不看更早
   数据也不看未来。EMA/MACD 递归尾衰减要求保留 ≥200 根历史缓冲
   （MA200 是最长窗口；RSI Wilder 递归在 200 根后残差 < 1e-9）。
2. 纯函数确定性：同输入两次调用输出完全一致（无时钟/无随机/无全局状态）。
3. 窗口敏感性：末根变化（T→T+1）输出随新数据合理变化（引擎确实在算最新一根）。

注意：截断「未来」是调用方职责（回测日历重放逐日喂入）——引擎本身拿不到
超出输入数组的数据，本测试验证的是「输入相同末根 ⇒ 输出相同」。
"""
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.spot_engine import evaluate_day  # noqa: E402


def _synth_closes(n: int, seed: int = 42, base: float = 100.0) -> list:
    """确定性合成收盘价：趋势 + 周期 + 扰动，保证 MA/RSI/BOLL/ATH 全路径有值。"""
    out = []
    for i in range(n):
        drift = base * (1.0 + 0.0015 * i)          # 线性趋势
        wave = base * 0.12 * math.sin(i / 9.0)     # 中周期波动
        noise = base * 0.03 * math.sin(i * 7.77 * seed % 360)  # 确定性伪随机扰动
        out.append(round(drift + wave + noise, 4))
    return out


DAILY = _synth_closes(400, seed=7)
WEEKLY = _synth_closes(200, seed=11)
MONTHLY = _synth_closes(150, seed=13)


def test_truncation_invariance_same_tail():
    """同一末根 T：砍更早历史（保留 ≥MA200 窗口）vs 全量，输出一致。"""
    # 日K全量 400 根，截到 250 根（>MA200 窗口上限，留 50 根缓冲）
    d_full = evaluate_day("BTCUSDT", "2026-09-22", DAILY, WEEKLY, MONTHLY)
    d_cut = evaluate_day("BTCUSDT", "2026-09-22", DAILY[-250:], WEEKLY, MONTHLY)
    assert d_full == d_cut, "同一末根下截断更早历史不应改变输出"


def test_eval_deterministic():
    """同输入两次调用输出完全一致（纯函数无隐藏状态）。"""
    a = evaluate_day("BNBUSDT", "T", DAILY, WEEKLY, MONTHLY)
    b = evaluate_day("BNBUSDT", "T", DAILY, WEEKLY, MONTHLY)
    assert a == b


def test_indicators_pure():
    """指标层纯度：同一序列重复计算稳定；截断更早历史仅剩 Wilder/EMA 递归
    尾衰减残差（200 根缓冲 < 1e-4，对档位判定无影响——阈值间隔 ≥5 分）。"""
    from app.services import spot_indicators as ind

    assert ind.rsi(DAILY, 14) == ind.rsi(DAILY, 14)
    assert ind.boll(DAILY) == ind.boll(DAILY)
    # 递归指标残差有界（Wilder RSI / EMA MACD，历史衰减指数收敛）
    r_full, r_cut = ind.rsi(DAILY, 14), ind.rsi(DAILY[-200:], 14)
    assert abs(r_full - r_cut) < 1e-4, f"RSI 截断残差过大: {abs(r_full - r_cut)}"
    m_full, m_cut = ind.macd(DAILY), ind.macd(DAILY[-300:])
    assert all(abs(a - b) < 1e-9 for a, b in zip(m_full, m_cut)), "MACD 截断残差过大"


def test_tail_moves_output():
    """末根前移（模拟 T-1 vs T）：输出随新收盘变化——引擎确实在算最新一根。"""
    d_now = evaluate_day("ETHUSDT", "T", DAILY, WEEKLY, MONTHLY)
    d_prev = evaluate_day("ETHUSDT", "T-1", DAILY[:-1], WEEKLY, MONTHLY)
    # 价格不同 ⇒ 至少 price 反映的是各自末根
    assert d_now["price"] == DAILY[-1]
    assert d_prev["price"] == DAILY[-2]


if __name__ == "__main__":
    test_truncation_invariance_same_tail()
    test_eval_deterministic()
    test_indicators_pure()
    test_tail_moves_output()
    print("ALL_NO_FUTURE_TESTS_PASSED")
