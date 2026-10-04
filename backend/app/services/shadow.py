"""影子模式（docs/14，2026-10-04）：候选信号特征「只计算+打标+记录，不参与决策」。

四环：打标（scanner scan_one 计算 + _save_results 落 shadow_marks）
→ 归因（复用 TradeReview/TradeRecord，§五）→ 评估（tasks/shadow_tasks.py 每日
分桶统计，本模块 build_report）→ 裁决（docs/14 §七 预注册标准卡）。

铁律（docs/14 §九）：
- 打标代码任何异常必须吞掉（记 debug 日志），绝不影响主管线；
- 打标结果不写回候选 dict、不进 AI 上下文——影子数据对决策路径不可见；
- 特征启停改代码（SHADOW_FEATURES.enabled），不进配置页。
"""
import logging
from dataclasses import dataclass
from typing import Callable, Optional

import numpy as np

logger = logging.getLogger(__name__)


# ── 特征计算（口径 = docs/14 §七 预注册标准卡）────────────────────────────

def compute_cvd_div(klines: list[list]) -> Optional[dict]:
    """CARD-01 cvd_div：CVD（累计主动买卖差）与价格背离（订单流免费子集）。

    口径：近 24 根已收盘K线，CVD_t = Σ(2×takerBuy − volume)（index 9 taker 买量）；
    price_chg = 收盘价首尾涨幅%，cvd_chg = CVD 首尾涨幅%（相对 |CVD 起点|，
    起点为 0 时用绝对差归一为成交量比）。桶：
    - diverge_bear：价升 >1% 且 CVD 未跟随（多头信号质量差）
    - diverge_bull：价跌 <-1% 且 CVD 未跟随（空头信号质量差）
    - confirm：价量同向
    样本不足（<26 根）返回 None（不打标，不产生缺失组伪统计）。
    """
    if not klines or len(klines) < 26:
        return None
    closed = klines[:-1][-24:]
    if len(closed) < 24:
        return None
    c = np.array([float(k[4]) for k in closed], dtype=float)
    v = np.array([float(k[5]) for k in closed], dtype=float)
    tb = np.array([float(k[9]) if len(k) > 9 else k[5] for k in closed], dtype=float)
    price_chg = (c[-1] / c[0] - 1) * 100
    cvd = 2 * tb - v  # 每根净主动买量
    cvd_start = float(cvd[0])
    cvd_abs_start = float(np.abs(cvd[:6]).sum()) or float(v[:6].sum()) or 1.0
    cvd_chg_pct = (float(cvd[-1]) - cvd_start) / cvd_abs_start * 100
    if price_chg > 1.0 and cvd_chg_pct < 0:
        bucket = "diverge_bear"
    elif price_chg < -1.0 and cvd_chg_pct > 0:
        bucket = "diverge_bull"
    else:
        bucket = "confirm"
    return {
        "price_chg_pct": round(price_chg, 3),
        "cvd_chg_pct": round(cvd_chg_pct, 3),
        "bucket": bucket,
    }


def compute_vol_regime(klines: list[list]) -> Optional[dict]:
    """CARD-03 vol_regime：波动率状态（ATR14/ATR100，宏观/CTA regime 过滤）。

    口径：1h 已收盘K线 Wilder ATR（atr_series，strategy/technical.py），
    ratio = ATR14 最新值 ÷ ATR100 最新值。桶：
    - compress：ratio < 0.6（波动率收缩，突破缺燃料，假突破重灾区）
    - expand：ratio > 1.4（波动扩张）
    - normal：其余
    样本不足（ATR100 未成形）返回 None。
    """
    from app.services.strategy.technical import atr_series

    a14 = atr_series(klines, period=14)
    a100 = atr_series(klines, period=100)
    if a14 is None or a100 is None:
        return None
    a14_last, a100_last = float(a14[-1]), float(a100[-1])
    if not np.isfinite(a14_last) or not np.isfinite(a100_last) or a100_last <= 0:
        return None
    ratio = a14_last / a100_last
    if ratio < 0.6:
        bucket = "compress"
    elif ratio > 1.4:
        bucket = "expand"
    else:
        bucket = "normal"
    return {"atr_ratio": round(ratio, 4), "bucket": bucket}


# ── 特征注册表（docs/14 §四）────────────────────────────────────────────

@dataclass
class ShadowFeature:
    key: str
    name: str
    card: str                      # 预注册标准卡（docs/14 §七）
    compute: Callable[[list], Optional[dict]]
    min_sample: int                # 卡的最小样本（有效归因计数），达标前 sufficient=False


SHADOW_FEATURES: dict[str, ShadowFeature] = {
    f.key: f for f in (
        ShadowFeature("cvd_div", "CVD 背离（订单流免费子集）", "CARD-01", compute_cvd_div, 80),
        ShadowFeature("vol_regime", "波动率状态（ATR14/ATR100）", "CARD-03", compute_vol_regime, 100),
    )
}


# ── 归因与评估（docs/14 §五/§六）────────────────────────────────────────

def _bucket_stats(rows: list) -> dict:
    """一批已归因标记 → 统计。outcome ∈ win_tp1/win_tp2/loss/expired/no_analysis/
    pending；win = win_tp1/win_tp2；R 仅 trade 宿主有。"""
    n = len(rows)
    wins = 0
    counted = 0
    for _b, outcome, _r in rows:
        if outcome in ("win_tp1", "win_tp2", "loss"):
            counted += 1
            wins += 1 if outcome.startswith("win") else 0
    return {"n": n, "counted": counted, "win_rate": round(wins / counted, 4) if counted else None}


def build_report(db, window_days: int = 30) -> list[dict]:
    """影子评估报告（docs/14 §六）：按特征分桶 join 复盘结果统计。

    归因链（scan_result 宿主）：ScanResult → AIAnalysis → TradeReview.outcome；
    无 AI 分析的标记记 no_analysis（配额未进），未复盘的记 pending 下轮再看。
    评估不回填 marks 行（避免双写漂移）。
    """
    from datetime import datetime, timedelta
    from sqlalchemy import select
    from app.models.scan import AIAnalysis, ScanResult, TradeReview
    from app.models.shadow import ShadowMark

    since = datetime.utcnow() - timedelta(days=window_days)
    out: list[dict] = []
    for key, feat in SHADOW_FEATURES.items():
        marks = db.execute(
            select(ShadowMark).where(
                ShadowMark.feature_key == key,
                ShadowMark.created_at >= since,
            ).order_by(ShadowMark.created_at)
        ).scalars().all()
        if not marks:
            out.append({"feature": key, "name": feat.name, "card": feat.card,
                        "min_sample": feat.min_sample,
                        "window_days": window_days, "sufficient": False,
                        "buckets": [], "baseline": {"n": 0, "counted": 0, "win_rate": None}})
            continue
        # 批量取宿主链结果：ref_id → (outcome)
        ref_ids = [m.ref_id for m in marks]
        sr_rows = db.execute(
            select(ScanResult.id, AIAnalysis.id, TradeReview.outcome)
            .outerjoin(AIAnalysis, AIAnalysis.scan_result_id == ScanResult.id)
            .outerjoin(TradeReview, TradeReview.ai_analysis_id == AIAnalysis.id)
            .where(ScanResult.id.in_(ref_ids))
        ).all()
        outcome_map: dict = {}
        for sr_id, _ai_id, outcome in sr_rows:
            # 同一 scan_result 理论一条 AIAnalysis（唯一约束），outerjoin 重复行取首个非空
            if sr_id not in outcome_map or outcome_map[sr_id] is None:
                outcome_map[sr_id] = outcome
        buckets: dict[str, list] = {}
        for m in marks:
            # 归一：宿主缺失（ScanResult 被清）→ no_analysis；有 AIAnalysis
            # 无 TradeReview → pending（未到 24h，下轮再看）
            outcome = outcome_map.get(m.ref_id)
            label = "no_analysis" if m.ref_id not in outcome_map else (outcome or "pending")
            b = (m.value_json or {}).get("bucket", "unknown")
            buckets.setdefault(b, []).append((b, label, None))
        bucket_out = [
            {"bucket": b, **_bucket_stats(rows)}
            for b, rows in sorted(buckets.items())
        ]
        for item in bucket_out:
            if item["n"] < 30:
                item["insufficient"] = True
        out.append({
            "feature": key, "name": feat.name, "card": feat.card,
            "min_sample": feat.min_sample,
            "window_days": window_days,
            "baseline": _bucket_stats([r for rows in buckets.values() for r in rows]),
            "buckets": bucket_out,
            # 达标判定：有效归因样本 ≥ 卡最小样本（docs/14 §七 预注册后不可回调）
            "sufficient": _bucket_stats([r for rows in buckets.values() for r in rows])["counted"] >= feat.min_sample,
        })
    return out
