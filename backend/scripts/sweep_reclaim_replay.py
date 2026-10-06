"""#10 流动性猎取确认档（sweep+reclaim）预注册回放（docs/26 模式，2026-10-07）。

预注册判定规则（跑数前锁定，与 docs/13 #10 登记同步，防 p-hacking）：
1. 样本量：有效 n≥100 且 sweep_reclaim≥30 且 no_sweep≥30，否则「样本不足→转影子」
2. 主判定：win_rate(sweep_reclaim) − win_rate(no_sweep) ≥ +3pp（docs/13 #10 预期下限）
3. 方向合理性：win_rate(sweep_reclaim) ≥ win_rate(sweep_pending)
4. 敏感性：d{0.08,0.10,0.12}% × N{6,8,10} 九格中 Δ≥+3pp 格数 ≥7，否则 FAIL 关闭
5. 集中度：sweep_reclaim 桶单币种占比 <40%
2/3/4/5 全过 → #10 进影子（docs/14 §十行5）；样本足但任一不过 → FAIL 关闭归档。

口径：15m K 线；t = created_at 前最后一根已收盘 15m bar；多头参考位 =
[t−96, t−8) 根最低 low（空头镜像 swing_high）；插针窗 = [t−7, t] 触破
参考位×(1−d)；收回 = 末次 sweep bar 起 N 根内 close 回参考位内侧；
桶 = sweep_reclaim / sweep_pending / no_sweep。
总体：scheduled + trigger_pullback_confirm + is_repeat=false + AIAnalysis +
TradeReview(is_veto=false) + outcome∈{win_tp1,win_tp2,loss}（expired 不入
分母，docs/24 惯例）；方向 votes.net 符号（0/缺失剔除）。

运行：cwd=backend，docker exec 进容器 python scripts/sweep_reclaim_replay.py
"""
import time
from collections import defaultdict

from sqlalchemy import select

BAR_MS = 900_000          # 15m
LOOKBACK_BARS = 96        # 参考位回看 24h
TOUCH_BARS = 8            # 插针窗 [t-7, t]（2h）
SWEEP_DEPTH = 0.001       # 主参数 d = 0.1%
RECLAIM_BARS = 8          # 主参数 N = 8（2h）


def _bucket(bars: dict, t_open: int, long_dir: bool, d: float, n_reclaim: int):
    """按预注册口径分桶；参考位窗数据不全（新上市）返回 None（剔除）。"""
    pre = [t_open - i * BAR_MS for i in range(TOUCH_BARS, LOOKBACK_BARS)]
    if not all(b in bars for b in pre):
        return None
    touch = [t_open - i * BAR_MS for i in range(TOUCH_BARS - 1, -1, -1)]
    if long_dir:
        ref = min(bars[b][1] for b in pre)  # swing low
        swept = [b for b in touch if b in bars and bars[b][1] <= ref * (1 - d)]
        back = lambda c: c in bars and bars[c][2] > ref  # noqa: E731
    else:
        ref = max(bars[b][0] for b in pre)  # swing high
        swept = [b for b in touch if b in bars and bars[b][0] >= ref * (1 + d)]
        back = lambda c: c in bars and bars[c][2] < ref  # noqa: E731
    if not swept:
        return "no_sweep"
    b = swept[-1]
    for k in range(n_reclaim + 1):
        if back(b + k * BAR_MS):
            return "sweep_reclaim"
    return "sweep_pending"


def main() -> None:
    from app.database import SessionLocal
    from app.models.scan import AIAnalysis, ScanRecord, ScanResult, TradeReview
    from app.services.exchange_pool import ExchangePool

    db = SessionLocal()
    rows = db.execute(
        select(ScanResult.symbol, ScanResult.created_at, ScanResult.votes_json,
               TradeReview.outcome)
        .join(ScanRecord, ScanRecord.id == ScanResult.scan_record_id)
        .join(AIAnalysis, AIAnalysis.scan_result_id == ScanResult.id)
        .join(TradeReview, TradeReview.ai_analysis_id == AIAnalysis.id)
        .where(
            ScanResult.signal_type == "trigger_pullback_confirm",
            ScanRecord.scan_type == "scheduled",
            ScanResult.is_repeat.is_(False),
            TradeReview.is_veto.is_(False),
            TradeReview.outcome.in_(("win_tp1", "win_tp2", "loss")),
        )
    ).all()
    db.close()

    # 方向预筛（预注册剔除项，网格无关）
    cands = []
    skip_dir = 0
    for sym, created, votes, outcome in rows:
        net = (votes or {}).get("net") or 0
        if net == 0:
            skip_dir += 1
            continue
        created_ms = created.replace(tzinfo=None).timestamp() * 1000
        t_open = created_ms // BAR_MS * BAR_MS - BAR_MS  # 前最后一根已收盘 15m bar
        cands.append((sym, t_open, net > 0, outcome))

    pool = ExchangePool()
    cache: dict = {}

    def bars_for(sym: str, t_open: int) -> dict:
        end_ms = t_open + 16 * BAR_MS
        key = (sym, end_ms)
        if key not in cache:
            try:
                kl = pool.get_klines_range(sym, "15m", 130, end_ms)
            except Exception:
                cache[key] = {}
            else:
                now_ms = time.time() * 1000
                cache[key] = {
                    int(k[0]): (float(k[2]), float(k[3]), float(k[4]))
                    for k in kl if int(k[0]) + BAR_MS <= now_ms
                }
        return cache[key]

    def run_cell(d: float, n_reclaim: int):
        buckets = defaultdict(lambda: [0, 0])            # n, win
        by_sym: dict = defaultdict(lambda: defaultdict(int))
        skipped_data = 0
        for sym, t_open, long_dir, outcome in cands:
            b = _bucket(bars_for(sym, t_open), t_open, long_dir, d, n_reclaim)
            if b is None:
                skipped_data += 1
                continue
            buckets[b][0] += 1
            if outcome in ("win_tp1", "win_tp2"):
                buckets[b][1] += 1
            by_sym[b][sym] += 1
        return buckets, by_sym, skipped_data

    def wr(b):
        n, w = b
        return w / n if n else None

    # 主参数格
    buckets, by_sym, skipped_data = run_cell(SWEEP_DEPTH, RECLAIM_BARS)
    n_total = sum(v[0] for v in buckets.values())
    print("# sweep+reclaim 预注册回放（docs/26 模式，2026-10-07）\n")
    print(f"总体流程：复盘定论 {len(rows)} → 方向不明剔除 {skip_dir} → "
          f"数据不全剔除 {skipped_data} → 有效 {n_total}")
    print("\n## 主参数（d=0.1%, N=8）分桶\n")
    print("| 桶 | n | win | win_rate |")
    print("| --- | --- | --- | --- |")
    for b in ("no_sweep", "sweep_reclaim", "sweep_pending"):
        n, w = buckets[b]
        r = wr(buckets[b])
        print(f"| {b} | {n} | {w} | {f'{r:.1%}' if r is not None else '-'} |")

    delta = None
    if buckets["no_sweep"][0] and buckets["sweep_reclaim"][0]:
        delta = wr(buckets["sweep_reclaim"]) - wr(buckets["no_sweep"])
    print(f"\n主判定 Δ = win_rate(sweep_reclaim) − win_rate(no_sweep) = "
          f"{f'{delta:+.1%}' if delta is not None else 'n/a'}（门槛 ≥ +3pp）")

    # 方向细分（补充，不参与判定）
    print("\n## 方向细分（主参数，补充读数）\n")
    print("| 方向 | no_sweep | sweep_reclaim | sweep_pending |")
    print("| --- | --- | --- | --- |")
    for name, long_dir in (("long", True), ("short", False)):
        cells = []
        for b in ("no_sweep", "sweep_reclaim", "sweep_pending"):
            sub = [(s, t, ld, o) for s, t, ld, o in cands if ld == long_dir]
            n = w = 0
            for sym, t_open, _ld, outcome in sub:
                bb = _bucket(bars_for(sym, t_open), t_open, long_dir,
                             SWEEP_DEPTH, RECLAIM_BARS)
                if bb == b:
                    n += 1
                    w += outcome in ("win_tp1", "win_tp2")
            cells.append(f"{w}/{n}={f'{w/n:.0%}' if n else '-'}")
        print(f"| {name} | {cells[0]} | {cells[1]} | {cells[2]} |")

    # 规则 1 样本量
    ok_n = (buckets["sweep_reclaim"][0] >= 30 and buckets["no_sweep"][0] >= 30
            and n_total >= 100)
    print(f"\n规则1 样本量：sweep_reclaim={buckets['sweep_reclaim'][0]}（≥30）"
          f" no_sweep={buckets['no_sweep'][0]}（≥30） 总体={n_total}（≥100）→ "
          f"{'PASS' if ok_n else 'FAIL（样本不足→不拍板，转影子打标积累）'}")

    # 规则 3 方向合理性
    wr_sr = wr(buckets["sweep_reclaim"])
    wr_sp = wr(buckets["sweep_pending"])
    ok_dir = wr_sr is not None and wr_sp is not None and wr_sr >= wr_sp
    f_sr = f"{wr_sr:.1%}" if wr_sr is not None else "-"
    f_sp = f"{wr_sp:.1%}" if wr_sp is not None else "-"
    print(f"规则3 方向合理性：sweep_reclaim {f_sr} vs sweep_pending {f_sp} → "
          f"{'PASS' if ok_dir else 'FAIL'}")

    # 规则 5 集中度
    sr_syms = by_sym.get("sweep_reclaim", {})
    total_sr = sum(sr_syms.values())
    top_sym, top_n = max(sr_syms.items(), key=lambda kv: kv[1]) if sr_syms else ("-", 0)
    conc = top_n / total_sr if total_sr else 0.0
    ok_conc = total_sr > 0 and conc < 0.40
    print(f"规则5 集中度：sweep_reclaim 单币最多 {top_sym} {top_n}/{total_sr}"
          f" = {conc:.0%}（<40%）→ {'PASS' if ok_conc else 'FAIL'}")

    # 规则 4 敏感性网格
    print("\n## 敏感性网格（Δ = sweep_reclaim − no_sweep，门槛 ≥+3pp）\n")
    print("| d \\ N | 6 | 8 | 10 |")
    print("| --- | --- | --- | --- |")
    grid_pass = 0
    for d in (0.0008, 0.001, 0.0012):
        cells = []
        for n_reclaim in (6, 8, 10):
            bk, _, _ = run_cell(d, n_reclaim)
            if bk["no_sweep"][0] and bk["sweep_reclaim"][0]:
                dd = wr(bk["sweep_reclaim"]) - wr(bk["no_sweep"])
                mark = "✓" if dd >= 0.03 else "✗"
                grid_pass += dd >= 0.03
                cells.append(f"{dd:+.1%}{mark}")
            else:
                cells.append("n/a")
        print(f"| {d:.2%} | {cells[0]} | {cells[1]} | {cells[2]} |")
    ok_grid = grid_pass >= 7
    print(f"\n规则4 敏感性：{grid_pass}/9 格 Δ≥+3pp（≥7）→ "
          f"{'PASS' if ok_grid else 'FAIL（参数敏感，docs/26 惯例判 FAIL 关闭）'}")

    # 终判
    print("\n## 预注册终判\n")
    if not ok_n:
        print("**样本不足** → 不拍板，转影子打标（docs/14 §十行5）积累有效样本。")
    else:
        verdict = ok_dir and ok_grid and ok_conc and (delta is not None and delta >= 0.03)
        print(("**PASS** → #10 进影子（扫描侧打标，docs/14 §十行5）"
               if verdict else
               "**FAIL** → #10 关闭归档（docs/26 刺透/乌云先例）。"))


def main_r2() -> None:
    """第二轮：EMA21 触点家族（2026-10-07 补充预注册）。

    第一轮几何诊断证明回踩触点悬于 24h 低点上方（中位 +4.8%，166 条零击穿）
    ——24h 滚动低点不是回踩的流动性池。第二轮参考位改**被触位**：仅限
    touch_ref='ema21'（EMA 可无歧义重构）；swing/retest 家族摆动位重构歧义
    大（docs/13 预警形态边界风险），不在回放里猜，留影子用生产原生摆动点。
    预注册：sweep_reclaim≥20 且 no_sweep≥15 否则样本不足→转影子；Δ≥+3pp；
    reclaim≥pending；九格敏感性≥7 格；单币<40%。
    """
    EMA_PERIOD = 21

    def ema(closes, period):
        if len(closes) < period:
            return None
        e = sum(closes[:period]) / period
        k = 2 / (period + 1)
        for c in closes[period:]:
            e = c * k + e * (1 - k)
        return e

    def _bucket2(bars, t_open, long_dir, d, n_reclaim):
        closes = [bars[ts][2] for ts in sorted(bars) if ts <= t_open]
        ref = ema(closes, EMA_PERIOD)
        if ref is None:
            return None
        touch = [t_open - i * BAR_MS for i in range(TOUCH_BARS - 1, -1, -1)]
        if long_dir:
            swept = [b for b in touch if b in bars and bars[b][1] <= ref * (1 - d)]
            back = lambda c: c in bars and bars[c][2] > ref  # noqa: E731
        else:
            swept = [b for b in touch if b in bars and bars[b][0] >= ref * (1 + d)]
            back = lambda c: c in bars and bars[c][2] < ref  # noqa: E731
        if not swept:
            return "no_sweep"
        b = swept[-1]
        for k in range(n_reclaim + 1):
            if back(b + k * BAR_MS):
                return "sweep_reclaim"
        return "sweep_pending"

    from app.database import SessionLocal
    from app.models.scan import AIAnalysis, ScanRecord, ScanResult, TradeReview
    from app.services.exchange_pool import ExchangePool

    db = SessionLocal()
    rows = db.execute(
        select(ScanResult.symbol, ScanResult.created_at, ScanResult.votes_json,
               TradeReview.outcome)
        .join(ScanRecord, ScanRecord.id == ScanResult.scan_record_id)
        .join(AIAnalysis, AIAnalysis.scan_result_id == ScanResult.id)
        .join(TradeReview, TradeReview.ai_analysis_id == AIAnalysis.id)
        .where(
            ScanResult.signal_type == "trigger_pullback_confirm",
            ScanRecord.scan_type == "scheduled",
            ScanResult.is_repeat.is_(False),
            ScanResult.touch_ref == "ema21",
            TradeReview.is_veto.is_(False),
            TradeReview.outcome.in_(("win_tp1", "win_tp2", "loss")),
        )
    ).all()
    db.close()

    pool = ExchangePool()
    _cache: dict = {}

    def bars_for(sym, t_open):
        end_ms = t_open + 16 * BAR_MS
        key = (sym, end_ms)
        if key not in _cache:
            try:
                kl = pool.get_klines_range(sym, "15m", 130, end_ms)
            except Exception:
                _cache[key] = {}
            else:
                now_ms = time.time() * 1000
                _cache[key] = {int(k[0]): (float(k[2]), float(k[3]), float(k[4]))
                               for k in kl if int(k[0]) + BAR_MS <= now_ms}
        return _cache[key]

    def wr(b):
        n, w = b
        return w / n if n else None

    def run_cell(cands, bucket_fn, d, n_reclaim):
        buckets = defaultdict(lambda: [0, 0])
        by_sym = defaultdict(lambda: defaultdict(int))
        for sym, t_open, long_dir, outcome in cands:
            b = bucket_fn(bars_for(sym, t_open), t_open, long_dir, d, n_reclaim)
            if b is None:
                continue
            buckets[b][0] += 1
            if outcome in ("win_tp1", "win_tp2"):
                buckets[b][1] += 1
            by_sym[b][sym] += 1
        return buckets, by_sym

    cands = []
    for sym, created, votes, outcome in rows:
        net = (votes or {}).get("net") or 0
        if net == 0:
            continue
        created_ms = created.replace(tzinfo=None).timestamp() * 1000
        t_open = created_ms // BAR_MS * BAR_MS - BAR_MS
        cands.append((sym, t_open, net > 0, outcome))

    buckets, by_sym = run_cell(cands, _bucket2, 0.001, 8)
    n_total = sum(v[0] for v in buckets.values())
    print("\n# sweep+reclaim 第二轮回放（EMA21 触点家族）\n")
    print(f"总体：ema21 触点定论 {len(rows)} → 有效 {n_total}\n")
    print("| 桶 | n | win | win_rate |")
    print("| --- | --- | --- | --- |")
    for b in ("no_sweep", "sweep_reclaim", "sweep_pending"):
        n, w = buckets[b]
        r = wr(buckets[b])
        print(f"| {b} | {n} | {w} | {f'{r:.1%}' if r is not None else '-'} |")
    delta = None
    if buckets["no_sweep"][0] and buckets["sweep_reclaim"][0]:
        delta = wr(buckets["sweep_reclaim"]) - wr(buckets["no_sweep"])
    print(f"\n主判定 Δ = {f'{delta:+.1%}' if delta is not None else 'n/a'}（门槛 ≥+3pp）")
    wr_sr, wr_sp = wr(buckets["sweep_reclaim"]), wr(buckets["sweep_pending"])
    ok_dir = wr_sr is not None and wr_sp is not None and wr_sr >= wr_sp
    print(f"规则3 reclaim {f'{wr_sr:.1%}' if wr_sr is not None else '-'} vs "
          f"pending {f'{wr_sp:.1%}' if wr_sp is not None else '-'} → "
          f"{'PASS' if ok_dir else 'FAIL'}")
    sr = by_sym.get("sweep_reclaim", {})
    tot = sum(sr.values())
    top, tn = max(sr.items(), key=lambda kv: kv[1]) if sr else ("-", 0)
    conc = tn / tot if tot else 0.0
    ok_conc = tot > 0 and conc < 0.40
    print(f"规则5 集中度：{top} {tn}/{tot}={conc:.0%}（<40%）→ "
          f"{'PASS' if ok_conc else 'FAIL'}")
    gp = 0
    for d in (0.0008, 0.001, 0.0012):
        cells = []
        for n_re in (6, 8, 10):
            bk, _ = run_cell(cands, _bucket2, d, n_re)
            if bk["no_sweep"][0] and bk["sweep_reclaim"][0]:
                dd = wr(bk["sweep_reclaim"]) - wr(bk["no_sweep"])
                gp += dd >= 0.03
                cells.append(f"{dd:+.1%}{'✓' if dd >= 0.03 else '✗'}")
            else:
                cells.append("n/a")
        print(f"  d={d:.2%}: {cells[0]} | {cells[1]} | {cells[2]}")
    ok_grid = gp >= 7
    print(f"规则4：{gp}/9 格 ≥+3pp（≥7）→ {'PASS' if ok_grid else 'FAIL（参数敏感→关闭）'}")
    ok_n = buckets["sweep_reclaim"][0] >= 20 and buckets["no_sweep"][0] >= 15
    print("\n## 第二轮预注册终判\n")
    if not ok_n:
        print("**样本不足/无对照基线** → #10 转影子打标（docs/14 §十行5）。")
    else:
        print("**PASS → #10 进影子**" if (ok_dir and ok_grid and ok_conc
              and delta is not None and delta >= 0.03) else
              "**FAIL → #10 关闭归档**。")


if __name__ == "__main__":
    main()
    main_r2()
