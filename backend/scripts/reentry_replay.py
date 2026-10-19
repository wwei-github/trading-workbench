"""#12 再入场规则（海龟）本体预注册回放（docs/26 模式，2026-10-19）。

预注册判定规则（跑数前锁定，与 docs/13 #12 登记同步，锁定后不可回调）：
1. 样本量：正式网 structure_ok 桶 n≥60，否则「样本不足→不拍板」；
2. 主判定：ΔR 均值 >0 且 ΔR Σ >0（ΔR = 0.5×R_leg，处置 − 对照≡0）→ PASS；
   ΔR 均值 ≤0 → FAIL 关闭归档；
3. 敏感性地基：θ{0,0.5,1}R × T{12,24,48}h 九格中 ΔR 均值 >0 的格数 ≥7；
   <7 → 参数敏感无稳健区 → FAIL 关闭归档（docs/26 惯例）。
2/3 全过 → #12 进实施（另行 bump RULE_VERSION）；否则按上失败路径归档。

处置/对照：正式网 CLOSED 且 exit_reason=sl；structure_ok=处置组，同批不复入=对照（ΔR≡0）。
  测试网同级分层仅作环境差异对照，不进主判定。

再入场腿模拟（依据 docs/15 v2 终局恒 ladder 1.5R 止盈）：止损后 T h 内首次触及开仓价
  (signed-R≥θ)触发，同向、1/2 预算；自触发根起逐根按 SL=−1R / TP=+1.5R 判定，同根
  双触按先止损保守处理，窗末(48h)未了结按末根 (h+l)/2 结算 → R_leg ∈ {−1,+1.5,末根mid}；
  ΔR = 0.5 × R_leg。数据 = CARD-07 影子已落库 post_path（1h 根、signed-R），不重拉 K 线。

运行：cwd=backend，docker exec 进容器 python scripts/reentry_replay.py
"""
import logging

logger = logging.getLogger(__name__)


def simulate_leg(post_path, theta=0.0, t_hours=24, sl=-1.0, tp=1.5):
    """再入场腿 R_leg（1/2 预算折算前的自身风险单位）。

    post_path = [[h_r, l_r], ...]，1h 根、signed-R（正=对持仓方向有利），
    起于出场后第 1 根。返回 (triggered, r_leg)；无触发 → (False, 0.0)。
    """
    idx = None
    for i, (h, l) in enumerate(post_path):
        if i >= t_hours:                    # 仅前 T 根内可触发（i=0 ↔ 出场后第 1 根）
            break
        if max(h, l) >= theta:              # 首次触及开仓价（θ=0 即回到 0R）
            idx = i
            break
    if idx is None:
        return False, 0.0
    for h, l in post_path[idx:]:
        if min(h, l) <= sl:                 # 同根双触先止损（保守）
            return True, sl
        if max(h, l) >= tp:
            return True, tp
    h, l = post_path[-1]
    return True, (h + l) / 2.0              # 窗末未了结按末根 mid 结算


def _stats(events, theta=0.0, t_hours=24, sl=-1.0):
    """events = [value_json, ...]（structure_ok 桶，按平仓时间升序）；返回分桶统计。

    max_dd = 累计 ΔR 曲线（按平仓时间序）的最大回撤（≤0）。
    """
    n = len(events)
    triggered = wins = second_sl = 0
    r_sum = delta_sum = cum = peak = 0.0
    max_dd = 0.0
    for v in events:
        ok, r = simulate_leg(v.get("post_path") or [], theta, t_hours, sl)
        triggered += ok
        r_sum += r
        delta_sum += 0.5 * r
        wins += r > 0
        second_sl += r == sl
        cum += 0.5 * r
        peak = max(peak, cum)
        max_dd = min(max_dd, cum - peak)
    return {
        "n": n,
        "triggered": triggered,
        "r_mean": r_sum / n if n else None,
        "delta_mean": delta_sum / n if n else None,
        "delta_sum": delta_sum,
        "win_rate": wins / n if n else None,
        "second_sl_rate": second_sl / n if n else None,
        "max_dd": max_dd,
    }


def _fmt_row(name, s):
    f = lambda x: f"{x:+.3f}" if x is not None else "-"  # noqa: E731
    p = lambda x: f"{x:.0%}" if x is not None else "-"   # noqa: E731
    return (f"| {name} | {s['n']} | {s['triggered']} | {f(s['r_mean'])} | "
            f"{f(s['delta_mean'])} | {f(s['delta_sum'])} | {p(s['win_rate'])} | "
            f"{p(s['second_sl_rate'])} | {f(s['max_dd'])} |")


def main():
    from sqlalchemy import select

    from app.database import SessionLocal
    from app.models.shadow import ShadowMark
    from app.models.trade import TradeRecord

    db = SessionLocal()
    rows = db.execute(
        select(ShadowMark.testnet, ShadowMark.decision_json,
               ShadowMark.value_json, TradeRecord.closed_at)
        .join(TradeRecord, TradeRecord.id == ShadowMark.ref_id)
        .where(ShadowMark.feature_key == "reentry_precond")
    ).all()
    db.close()

    def split(testnet):
        """取该分层 structure_ok 且含 48h post_path 的 value_json 列表（按平仓时间升序）。"""
        out = []
        for net_col, decision, value, closed_at in rows:
            v = value or {}
            # 分层权威口径 decision_json.testnet（cb572ee 前 testnet 列曾错标）
            is_testnet = bool((decision or {}).get("testnet", net_col))
            if is_testnet != testnet:
                continue
            if v.get("bucket") != "structure_ok" or not v.get("post_path"):
                continue
            out.append((v, closed_at))
        out.sort(key=lambda e: (e[1] is None, e[1]))
        return [v for v, _ in out]

    net = split(False)          # 正式网处置组（主判定）
    tnet = split(True)          # 测试网对照

    print("# #12 再入场 本体预注册回放（docs/26 模式，2026-10-19）\n")
    print(f"库内 reentry_precond 标记 {len(rows)} 条 → "
          f"正式网 structure_ok（含 post_path）{len(net)} / 测试网 {len(tnet)}\n")

    m = _stats(net, 0.0, 24)
    print("## 主参数（θ=0R, T=24h）\n")
    print("| 组 | n | 触发 | R_leg 均值 | ΔR 均值 | ΔR Σ | 胜率 | 二次止损率 | 最大回撤 |")
    print("| --- | --- | --- | --- | --- | --- | --- | --- | --- |")
    print(_fmt_row("正式网(主)", m))
    print(_fmt_row("测试网(对照)", _stats(tnet, 0.0, 24)))

    # 与 CARD-07 已落库 continued_24h 交叉校验（同 T=24h、θ=0 口径应一致）
    cont_field = sum(1 for v in net if v.get("continued_24h"))
    print(f"\n交叉校验：主参数触发 {m['triggered']} vs 已落库 continued_24h "
          f"{cont_field} → {'一致 ✓' if m['triggered'] == cont_field else '不一致 ✗（检查口径）'}")

    ok_n = m["n"] >= 60
    print(f"\n规则1 样本量：正式网 structure_ok n={m['n']}（≥60）→ "
          f"{'PASS' if ok_n else 'FAIL（样本不足→不拍板）'}")

    print("\n## 敏感性网格（ΔR 均值，门槛 >0）\n")
    print("| θ \\ T | 12h | 24h | 48h |")
    print("| --- | --- | --- | --- |")
    grid_pass = 0
    for theta in (0.0, 0.5, 1.0):
        cells = []
        for t in (12, 24, 48):
            dm = _stats(net, theta, t)["delta_mean"]
            if dm is None:
                cells.append("n/a")
                continue
            good = dm > 0
            grid_pass += good
            cells.append(f"{dm:+.3f}{'✓' if good else '✗'}")
        print(f"| {theta:.1f}R | {cells[0]} | {cells[1]} | {cells[2]} |")
    ok_grid = grid_pass >= 7
    if ok_n:
        print(f"\n规则3 敏感性：{grid_pass}/9 格 ΔR 均值 >0（≥7）→ "
              f"{'PASS' if ok_grid else 'FAIL（参数敏感→关闭归档）'}")
    else:
        print(f"\n规则3 敏感性：{grid_pass}/9 格 ΔR 均值 >0（参考读数；n<60 样本不足，不作判定）")

    print("\n## 预注册终判\n")
    if not ok_n:
        print("**样本不足** → 不拍板，样本继续积累（CARD-07 每日回填）。")
    else:
        verdict = (ok_grid and m["delta_mean"] is not None and m["delta_mean"] > 0
                   and m["delta_sum"] > 0)
        print("**PASS** → #12 进实施（加 `repeat_window` 结构完好豁免 + bump RULE_VERSION）"
              if verdict else
              "**FAIL** → #12 关闭归档（docs/26 刺透/乌云先例）。")


if __name__ == "__main__":
    main()
