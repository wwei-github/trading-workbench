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
from datetime import datetime
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


def compute_r2_slope(klines: list[list]) -> Optional[dict]:
    """CARD-10 r2_slope：趋势质量（R²/slope，docs/16 S-10，2026-10-08 立卡）。

    口径：近 24 根已收盘 1h 收盘价最小二乘回归（swing.linear_regression，
    与 CARD-07 结构 R² 同引擎）——r² = 拟合优度，slope_pct = slope ÷ 均价
    × 100（每根 %，跨币可比）。桶：
    - r2_high：r² ≥ 0.7（强趋势，回踩/突破延续概率高）
    - r2_mid：0.4 ≤ r² < 0.7（弱趋势，观察列不进判定）
    - r2_low：r² < 0.4（震荡，假信号重灾区）
    slope 方向不进分桶（方向由票数/4h 背景闸门承担），记 value_json 观察。
    样本不足（<24 根）返回 None。背景：ScanResult.r_squared 列历史门槛废弃时
    已置 0 恒空，本特征影子侧自算（零成本，与生产判定零耦合）。
    """
    from app.services.strategy.swing import linear_regression

    closed = klines[:-1][-24:] if klines else []
    if len(closed) < 24:
        return None
    closes = np.array([float(k[4]) for k in closed], dtype=float)
    slope, _icpt, r2 = linear_regression(np.arange(len(closes)), closes)
    mean_px = float(closes.mean())
    slope_pct = (float(slope) / mean_px * 100.0) if mean_px > 0 else 0.0
    r2 = float(r2)
    if r2 >= 0.7:
        bucket = "r2_high"
    elif r2 >= 0.4:
        bucket = "r2_mid"
    else:
        bucket = "r2_low"
    return {"r2": round(r2, 4), "slope_pct": round(slope_pct, 4), "bucket": bucket}


# ── 特征注册表（docs/14 §四）────────────────────────────────────────────

@dataclass
class ShadowFeature:
    key: str
    name: str
    card: str                      # 预注册标准卡（docs/14 §七）
    compute: Callable[[list], Optional[dict]]
    min_sample: int                # 卡的最小样本（有效归因计数），达标前 sufficient=False
                                   # **展示口径·必要非充分**（2026-10-19 明确，docs/14 §十一
                                   # 遗留问题 2）：sufficient 只按**全卡** counted 判定，而各卡
                                   # 真实判据含**分桶**口径（如 CARD-01 n≥80/桶、CARD-10 判定桶
                                   # 各≥60、CARD-08 n≥30/桶）——绿灯只保证够开庭样本，判定仍
                                   # 须人工核对 docs/14 §七 卡文本
    enabled: bool = True           # 停用开关：置 False 后不再打标、不进评估报告
                                   # （docs/14 §六.1「对每个 enabled 特征取窗口标记」；
                                   # §九 特征启停改代码不进配置页）。注意：裁决
                                   # **关闭的卡按纪律直接删除代码**（2026-10-19 新
                                   # 策略，见 docs/14 §十一），本开关仅用于临时止血/
                                   # 停打，不用于「关闭保留」
    since_rule_version: Optional[str] = None
                                   # 跨版本不混算起点（docs/14 §六.1，2026-10-13）：
                                   # 候选池依赖规则集的卡（scan_result 宿主）在规则
                                   # 重构后置本值即可自动重置窗口——评估只计
                                   # rule_version ≥ 本值的标记，旧版本与历史 NULL
                                   # 行出窗，免手工改窗口常量


SHADOW_FEATURES: dict[str, ShadowFeature] = {
    f.key: f for f in (
        # 跨版本不混算（docs/14 §六.1，2026-10-13）：10-09 4h 计算合并 / 10-10
        # gate_4h 硬门 / 10-13 4h 状态机前置门连续重构候选池，存量与新版不同源
        # → 前向窗口重置为 v2026-10-13-8（旧版本/历史 NULL 行出窗）
        ShadowFeature("cvd_div", "CVD 背离（订单流免费子集）", "CARD-01", compute_cvd_div, 80,
                      since_rule_version="v2026-10-13-8"),
        # 趋势质量（docs/16 S-10，CARD-10，2026-10-08 立卡）：r²/slope 影子侧
        # 自算（ScanResult.r_squared 历史门槛废弃时置 0 恒空）。min_sample=120
        # 为展示口径（判定桶 r2_high/r2_low 各 ≥60，r2_mid 不进判定——真实
        # 判据在 docs/14 §七 CARD-10 文本）
        ShadowFeature("r2_slope", "趋势质量（R²/slope）", "CARD-10", compute_r2_slope, 120),
        # 再入场前置（docs/13 #12，CARD-07）：止损后结构×续走条件概率，
        # trade_record 宿主——出场后 48h 路径在平仓时刻不存在，打标由每日
        # 回填（tag_reentry_backfill）执行，compute 占位仅为 build_report 元数据
        ShadowFeature("reentry_precond", "再入场前置（止损后结构×续走）", "CARD-07", lambda k: None, 60),
        # 流动性猎取确认（docs/13 #10 转影子，CARD-08）：回踩触点位的插针×收回，
        # 扫描侧特例接线（需生产原生触点位 + 15m 拉取，compute 占位仅为
        # build_report 元数据）；EMA 触点族不打标（2026-10-07 预注册回放结论）
        ShadowFeature("sweep_reclaim", "流动性猎取确认（sweep+reclaim）", "CARD-08", lambda k: None, 30),
        # 评分门槛前向对照（docs/14 §七 CARD-09）：rec 60-69 vs 70+ 前向实盘
        # 分桶——存量回看（09-14~10-07）判 60-69 段负期望，按纪律提门槛前走
        # 28 天前向影子。trade_record 宿主（tag_trade_close 内打标，不依赖 K 线）
        ShadowFeature("rec70_shadow", "评分门槛前向对照（60~69 vs 70+）", "CARD-09", lambda k: None, 15),
    )
}

# 预注册开庭日（docs/14 §七 时间锁，2026-10-10 汇总成表）：打标上线/标准卡锁定
# 日 + 评估周期。到期前即使样本达标也只算「达标待期」——§八 人工裁决不得提前
# （时间窗防的是单一行情 regime 的自相关样本，与样本量正交，docs/16 S-01「不提前」）。
# None = 到量即判卡（SPRT/decided 门槛，无日历窗，样本达标即可开庭）。
COURT_DATES: dict[str, Optional[str]] = {
    "CARD-01": "2026-10-27",  # 打标上线 10-04 + 14 天；2026-10-19 顺延——10-13 前向
                              # 窗口重置（since_rule_version=v2026-10-13-8），时间锁自
                              # 重置日重算（10-13 + 14 天），同 CARD-09 顺延先例（docs/14 §十一）
    "CARD-07": None,          # SPRT 早停通道，跨界即终局（落锁后仍走 §八）
    "CARD-08": "2026-10-21",  # 锁定 10-07 + 14 天
    "CARD-09": "2026-11-08",  # REC70_START(10-11) + 28 天（评估仅计 closed_at ≥ REC70_START；
                              # 10-11 随 EMA 状态机重构重置窗口，锁定期顺延 1 天）
    "CARD-10": "2026-10-22",  # 锁定 10-08 + 14 天
}


# ── 评分门槛前向对照（CARD-09，docs/14 §七，2026-10-07 预注册锁定）────────
#
# 存量回看（09-14~10-07，正式网）：rec 60-69 段 16 笔胜率 12.5% / avg −0.659R，
# 70+ 段 29 笔 41.4% / +0.324R——但这是事后分桶，直接提门槛有事后选择偏误。
# 本卡以前向窗口验证「60-69 段真实期望为负」：开仓行为不变（门槛仍 60），
# 平仓时按开仓评分分桶落标，≥28 天后按预注册门槛裁决是否正式提至 70。
# 判定期间不改 prompt 评分锚点定义（防分数分布漂移污染对照）。

REC70_NEW_REC = 70.0          # 候选新门槛（分桶分界：60~69 / 70+）
REC70_MIN_REC = 60.0          # 现行开仓门槛（低于此分不会开仓，不产生样本）
REC70_START = "2026-10-11"    # 前向窗口起点：之前平仓的存量只打标不计入评估。
# 2026-10-11 重置（原 2026-10-10，窗口仅积累 1 天样本成本趋零）：
# RULE_VERSION v2026-10-11 变更评分函数（4h EMA 状态机 11 态重构，
# STATE_QUALITY 分值矩阵全变），原窗口分数分布与新口径不同源——
# 前向对照要求单一总体，重置换 28 天纯净窗口
# （首置 2026-10-10 见 docs/14 §七：v2026-10-09r2 趋势年龄重设计）


def _tag_rec70(db, rec) -> bool:
    """CARD-09 打标（tag_trade_close 内调用）：按开仓时 AI 评分分桶落标。

    评分取 ai_snapshot.recommendation（开仓时刻快照，与复盘评分分桶
    actual_review._rec_bucket 同源）；无快照（手动导入单）或 <60（开仓闸
    下不会出现，出现即异常）不打标。value_json 存原始数值 + win/r——
    打标时刻已平仓，真实盈亏即读数，评估期按 closed_at ≥ REC70_START
    计入前向窗口。幂等：uq_shadow_mark 覆盖。返回是否落标。
    """
    from sqlalchemy import select

    from app.models.shadow import ShadowMark

    snap = rec.ai_snapshot if isinstance(rec.ai_snapshot, dict) else {}
    try:
        rec_val = float(snap.get("recommendation"))
    except (TypeError, ValueError):
        return False
    if rec_val < REC70_MIN_REC:
        return False
    pnl = float(rec.realized_pnl or 0)
    # 1R = 成交后初始风险；测试替身/旧对象无 risk_unit 属性时回落 risk_amount
    risk = getattr(rec, "risk_unit", None) or float(rec.risk_amount or 0)
    value = {
        "recommendation": rec_val,
        "bucket": "rec60_69" if rec_val < REC70_NEW_REC else "rec70plus",
        "r": round(pnl / risk, 4) if risk > 0 else None,
        "win": pnl > 0,
        "closed_at": (rec.closed_at.replace(tzinfo=None).isoformat()
                      if rec.closed_at else None),
    }
    decision = {
        "stage": "trade_close", "testnet": bool(rec.testnet),
        "account_key": rec.account_key, "status": rec.status,
    }
    existing = db.execute(
        select(ShadowMark).where(
            ShadowMark.feature_key == "rec70_shadow",
            ShadowMark.ref_type == "trade_record",
            ShadowMark.ref_id == rec.id,
        )
    ).scalars().first()
    if existing:
        existing.value_json = value
        existing.decision_json = decision
    else:
        db.add(ShadowMark(
            feature_key="rec70_shadow", ref_type="trade_record",
            ref_id=rec.id, symbol=rec.symbol,
            testnet=bool(rec.testnet),
            value_json=value, decision_json=decision,
        ))
    return True


def _rec70_stats(rows: list) -> dict:
    """评分门槛前向对照卡（CARD-09）统计：n / counted / win_rate / avg_r。

    读数为真实盈亏（打标时刻已平仓，counted = n）：win = realized_pnl > 0
    （pnl=0 计分母不计胜，与复盘主口径 actual_review._agg 一致）；
    avg_r = mean(realized_pnl ÷ risk_amount)，risk 缺失的行不计入 avg_r。"""
    n = len(rows)
    if not n:
        return {"n": 0, "counted": 0, "win_rate": None, "avg_r": None}
    wins = sum(1 for r in rows if r.get("win"))
    rs = [r.get("r") for r in rows if r.get("r") is not None]
    return {
        "n": n,
        "counted": n,
        "win_rate": round(wins / n, 4),
        "avg_r": round(sum(rs) / len(rs), 4) if rs else None,
    }


def tag_trade_close(trades: list) -> int:
    """trade_close 宿主打标（docs/14 §七 CARD-09）：对已平仓交易逐笔落评分门槛标记。

    CARD-05/06 早期保护反事实卡已于 2026-10-13 / 2026-10-19 裁决关闭并从代码
    删除（docs/14 §十一）。本函数现仅服务 CARD-09 rec70_shadow（不依赖 K 线）。
    幂等：uq_shadow_mark 同 (ref_type, ref_id, feature_key) 覆盖更新。
    任何异常全吞（影子铁律），返回实际落标行数。
    """
    if not trades:
        return 0
    from app.database import SessionLocal

    tagged = 0
    db = SessionLocal()
    try:
        for rec in trades:
            # ── CARD-09 评分门槛前向对照（不依赖 K 线，独立事务）──────────
            try:
                marked = _tag_rec70(db, rec)
                db.commit()
                if marked:
                    tagged += 1
            except Exception as e:
                db.rollback()
                logger.debug("rec70 影子打标失败（忽略）%s: %s",
                             getattr(rec, "symbol", "?"), e)
        return tagged
    except Exception as e:
        db.rollback()
        logger.debug("trade_close 影子打标批量失败（忽略）: %s", e)
        return 0
    finally:
        db.close()


# ── 再入场前置打标（docs/13 #12，CARD-07，trade_record 宿主，每日回填）────
#
# 回答 docs/13 #12 的硬前置问题：止损出场后价格是否同向续走（回到开仓价），
# 按出场时刻趋势结构是否完好分桶。出场后 48h 路径在平仓时刻不存在，
# 无法挂归因任务即时打标——由每日回填任务（tasks/shadow_tasks）调
# tag_reentry_backfill 对出窗止损单补打标，宿主同为 trade_record。
# 口径预注册（2026-10-06 锁定，事后不可回调）：
# - 总体 = CLOSED 且 exit_reason="sl"（docs/13 #12 语义：止损出场单）
# - 结构完好 structure_ok = 出场时三件全过：① EMA 排列（多头 e21>e55，
#   e144 可用时加 e55>e144，空头对称）② ADX(14,1h) ≥ 20 ③ R² ≥ 0.5
#   （止损前 48 根已收盘收盘价线性回归）；数据不可判不打标
# - 续走（主判据）= 出场后 24h 内 signed-R MFE ≥ 0（价格回到开仓价）；
#   value_json 另存 48h 全窗 post_path/mfe_post_r/hours_to_reentry 供离线
#   敏感性（N=12/24/48h、阈值 0/0.5/1R），不重拉 K 线
# - 裁决门槛：正式网 structure_ok 桶 n≥60 且 P(续走|structure_ok) ≥ 40%
#   → #12 进入预注册回放；< 40% → #12 直接关闭

REENTRY_EXIT_REASON = "sl"   # 预注册总体口径：仅止损出场单
REENTRY_POST_H = 48          # 出场后观察窗（h）
REENTRY_CONT_H = 24          # 主判据窗口（h）
REENTRY_LOOKBACK_H = 350     # 结构窗回看（h；EMA144 需 ~245 根已收盘）
REENTRY_FETCH_LIMIT = 400    # 一次拉取根数（350 结构窗 + 48 续走窗）
REENTRY_R2_BARS = 48         # R² 回归窗（根）
REENTRY_R2_MIN = 0.5         # R² 门槛（= 扫描 R_SQUARED_THRESHOLD 默认）
REENTRY_ADX_MIN = 20.0       # ADX 门槛（analyze_adx 趋势酝酿语义）


def compute_reentry_precond(klines: list[list], rec) -> Optional[dict]:
    """CARD-07 reentry_precond：止损单出场结构快照 + 出场后同向续走重放。

    klines = 截至 closed_at + REENTRY_POST_H 的已收盘 1h K 线（时间升序，
    恰含出场前 ~350h 结构窗与出场后 48h 续走窗）。R 空间为 signed-R
    （正向 = 对持仓有利）。返回 None = 不打标
    （缺止损快照 / 结构数据不可判 / 出场后无路径）。
    """
    from app.services.strategy.ema import calc_ema
    from app.services.strategy.swing import linear_regression
    from app.services.strategy.technical import analyze_adx

    entry = float(rec.entry_price or 0)
    stop0 = float(getattr(rec, "stop_initial", 0) or 0)
    if entry <= 0 or stop0 <= 0 or not klines:
        return None
    d = 1 if (rec.direction or "long") == "long" else -1
    r_unit = abs(entry - stop0)
    if r_unit <= 0:
        return None

    def r_of(price: float) -> float:
        return d * (price - entry) / r_unit

    closed_ms = int(rec.closed_at.replace(tzinfo=None).timestamp() * 1000)
    exit_bar_open = closed_ms // 3_600_000 * 3_600_000  # 平仓所在根（该根不入任何窗口）
    pre = [k for k in klines if int(k[0]) + 3_600_000 <= closed_ms]
    post = [k for k in klines if int(k[0]) > exit_bar_open][:REENTRY_POST_H]
    closes = np.array([float(k[4]) for k in pre], dtype=float)
    # ADX(14) 最少 44 根 + EMA55 余量；无出场后路径则续走不可判
    if len(closes) < 66 or not post:
        return None

    # ① EMA 排列（e144 不可用降级 21/55 双线，ema144_used 记录口径）
    e21 = calc_ema(closes, 21)
    e55 = calc_ema(closes, 55)
    e144 = calc_ema(closes, 144)
    if e21 is None or e55 is None:
        return None
    f, m = float(e21[-1]), float(e55[-1])
    if d > 0:
        ema_ok = f > m and (e144 is None or m > float(e144[-1]))
    else:
        ema_ok = f < m and (e144 is None or m < float(e144[-1]))
    # ② ADX（analyze_adx 内部剔尾，pre 已全为已收盘根，无害）
    adx_v = analyze_adx(pre, period=14)
    adx = float(adx_v["adx"]) if adx_v and adx_v.get("adx") is not None else None
    adx_ok = adx is not None and adx >= REENTRY_ADX_MIN
    # ③ R²（近 48 根收盘线性回归）
    win = closes[-REENTRY_R2_BARS:]
    _slope, _icpt, r2 = linear_regression(np.arange(len(win)), win)
    r2_ok = float(r2) >= REENTRY_R2_MIN
    structure_ok = bool(ema_ok and adx_ok and r2_ok)

    # 出场后路径（signed-R，正值 = 对持仓方向有利）与续走判定
    post_path = [[round(r_of(float(k[2])), 4), round(r_of(float(k[3])), 4)] for k in post]
    mfe_post_r = max(max(h, l) for h, l in post_path)
    cont_end_ms = exit_bar_open + REENTRY_CONT_H * 3_600_000
    hours_to_reentry = None
    continued_24h = False
    for k, (h_r, l_r) in zip(post, post_path):
        if int(k[0]) > cont_end_ms:
            break
        if max(h_r, l_r) >= 0.0:
            continued_24h = True
            hours_to_reentry = round((int(k[0]) - closed_ms) / 3_600_000, 1)
            break
    real_r = None
    risk = getattr(rec, "risk_unit", None) or float(rec.risk_amount or 0)
    if risk > 0:
        real_r = round(float(rec.realized_pnl or 0) / risk, 4)
    return {
        "r_unit": round(r_unit, 8), "stop_initial": stop0,
        "direction": rec.direction, "exit_reason": rec.exit_reason,
        "real_r": real_r,
        "structure_ok": structure_ok,
        "bucket": "structure_ok" if structure_ok else "structure_broken",
        "ema_ok": bool(ema_ok), "ema144_used": e144 is not None,
        "adx": round(adx, 2) if adx is not None else None, "adx_ok": bool(adx_ok),
        "r2": round(float(r2), 4), "r2_ok": bool(r2_ok),
        "post_path": post_path, "mfe_post_r": round(mfe_post_r, 4),
        "continued_24h": continued_24h, "hours_to_reentry": hours_to_reentry,
        "continued_48h": bool(mfe_post_r >= 0.0),
    }


def tag_reentry_backfill(batch: int = 40, days: int = 90) -> int:
    """CARD-07 每日回填（docs/13 #12 前置卡）：出场 48h 观察窗已过的止损单补打标。

    入选（预注册总体）：CLOSED + exit_reason="sl" + closed_at ∈
    [now-days, now-48h] + 尚无 reentry_precond 标记；每轮 ≤ batch 笔
    （每笔 1 次 K 线请求预算）。K 线 = 截至 closed_at+48h 的最近
    REENTRY_FETCH_LIMIT 根 1h（一次请求覆盖结构窗 + 续走窗）。
    已标记单永不重打（出场后路径以首个 48h 窗为准）。铁律不变：
    任何异常吞掉；结果只进 shadow_marks，不回写 TradeRecord。
    """
    from datetime import datetime, timedelta

    from sqlalchemy import select

    from app.database import SessionLocal
    from app.models.shadow import ShadowMark
    from app.models.trade import TradeEvent, TradeRecord
    from app.services.exchange_pool import ExchangePool

    db = SessionLocal()
    try:
        now = datetime.utcnow()
        done = set(db.execute(
            select(ShadowMark.ref_id).where(
                ShadowMark.feature_key == "reentry_precond")
        ).scalars().all())
        rows = db.execute(
            select(TradeRecord).where(
                TradeRecord.status == "CLOSED",
                TradeRecord.excluded.is_(False),  # 异常单剔除（2026-10-18）
                TradeRecord.exit_reason == REENTRY_EXIT_REASON,
                TradeRecord.closed_at.isnot(None),
                TradeRecord.closed_at >= now - timedelta(days=days),
                TradeRecord.closed_at <= now - timedelta(hours=REENTRY_POST_H),
            ).order_by(TradeRecord.closed_at.desc())
        ).scalars().all()
        pending = [t for t in rows if t.id not in done][:batch]
        if not pending:
            return 0
        # 初始止损快照（同 tag_trade_close 口径：OPEN 事件 detail.stop_loss）
        events = db.execute(
            select(TradeEvent.trade_record_id, TradeEvent.detail).where(
                TradeEvent.trade_record_id.in_([t.id for t in pending]),
                TradeEvent.event_type == "OPEN",
            )
        ).all()
        stop_map: dict = {}
        for tr_id, detail in events:
            try:
                sl = float((detail or {}).get("stop_loss") or 0)
            except (TypeError, ValueError):
                sl = 0.0
            if sl > 0:
                stop_map[tr_id] = sl
        pool = ExchangePool()
        tagged = 0
        for rec in pending:
            try:
                si = stop_map.get(rec.id)
                if not si:
                    continue  # 无初始止损快照：R 空间不可算，跳过
                rec.stop_initial = si  # compute 读此属性
                closed_ms = int(rec.closed_at.replace(tzinfo=None).timestamp() * 1000)
                kls = pool.get_klines_range(
                    rec.symbol, "1h", REENTRY_FETCH_LIMIT,
                    closed_ms + REENTRY_POST_H * 3_600_000,
                ) or []
                value = compute_reentry_precond(kls, rec)
                if value is None:
                    continue
                decision = {
                    "stage": "reentry_backfill",
                    "testnet": bool(rec.testnet),
                    "account_key": rec.account_key,
                    "status": rec.status,
                }
                existing = db.execute(
                    select(ShadowMark).where(
                        ShadowMark.feature_key == "reentry_precond",
                        ShadowMark.ref_type == "trade_record",
                        ShadowMark.ref_id == rec.id,
                    )
                ).scalars().first()
                if existing:
                    existing.value_json = value
                    existing.decision_json = decision
                else:
                    db.add(ShadowMark(
                        feature_key="reentry_precond", ref_type="trade_record",
                        ref_id=rec.id, symbol=rec.symbol,
                        testnet=bool(rec.testnet),
value_json=value, decision_json=decision,
                    ))
                db.commit()  # 逐笔提交：单笔失败回滚不拖累已打标行
                tagged += 1
            except Exception as e:
                db.rollback()
                logger.debug("reentry_backfill 影子打标失败（忽略）%s: %s",
                             getattr(rec, "symbol", "?"), e)
        return tagged
    except Exception as e:
        db.rollback()
        logger.debug("reentry_backfill 影子回填批量失败（忽略）: %s", e)
        return 0
    finally:
        db.close()


# ── 归因与评估（docs/14 §五/§六）────────────────────────────────────────
def _scan_prices(direction, entry, stop, tp1, tp2, veto_original,
                 is_veto) -> Optional[tuple]:
    """scan_result 宿主卡回放三价（与 api/scan.py::_prices_of 同口径）。

    veto 否决单的三价取自 `ai_analysis.veto_original` 快照（自身三价已被
    forced skip 清空）；suggest 单取分析行自身字段。缺三价返回 None（该笔
    不计 R 读数）。"""
    if is_veto and veto_original:
        v = veto_original or {}
        return (v.get("direction"), float(v.get("entry_price") or 0),
                float(v.get("stop_loss") or 0),
                float(v.get("take_profit_1") or 0),
                float(v.get("take_profit_2") or 0))
    if not entry or not stop or not tp1:
        return None
    return (direction, float(entry), float(stop), float(tp1),
            float(tp2) if tp2 else 0.0)


def _scan_replay_r(prices: Optional[tuple], outcome: Optional[str],
                   bars_to_exit: Optional[int]) -> Optional[float]:
    """scan_result 宿主卡的 R 读数（**预注册 2026-10-19**，口径锁定不可回调）。

    scan_result 宿主卡的 outcome 来自同一条 `TradeReview` 建议回放，R 口径
    逐字复用 `GET /api/scans/review/stats` 的 `ev_r`（api/scan.py::_ev_r）：
    loss = −1.0R / win_tp1 = (TP1−entry)/stop_dist / win_tp2 = (TP2−entry)/stop_dist，
    减往返费用（taker 0.1% + 资金费 0.03%/8h，按 bars_to_exit 折算成 R）。
    expired / 缺三价 → None（不计入 avg_r 分母）。与 CARD-01/CARD-10 标准卡的
    avg_r 判定项配套（此前 avg_r 恒 null 致该条件不可判定，见 docs/14 §十一）。"""
    if not prices:
        return None
    _direction, entry, stop, tp1, tp2 = prices
    stop_dist = abs(entry - stop)
    if stop_dist <= 0 or entry <= 0:
        return None
    if outcome == "loss":
        r_mult = -1.0
    elif outcome == "win_tp1":
        r_mult = abs(tp1 - entry) / stop_dist
    elif outcome == "win_tp2" and tp2:
        r_mult = abs(tp2 - entry) / stop_dist
    else:
        return None
    cost_pct = 0.001 + 0.0003 * max(1, (bars_to_exit or 1) // 8)
    return round(r_mult - cost_pct / (stop_dist / entry), 3)


def _bucket_stats(rows: list) -> dict:
    """一批已归因标记 → 统计。outcome ∈ win_tp1/win_tp2/loss/expired/no_analysis/
    pending；win = win_tp1/win_tp2；avg_r = 该桶内有效 R 读数均值（scan_result
    宿主的 R 口径见 _scan_replay_r，预注册 2026-10-19；缺 R 的行不计分母，
    口径同 _rec70_stats）。"""
    n = len(rows)
    wins = 0
    counted = 0
    rs: list = []
    for _b, outcome, r in rows:
        if outcome in ("win_tp1", "win_tp2", "loss"):
            counted += 1
            wins += 1 if outcome.startswith("win") else 0
            if r is not None:
                rs.append(r)
    return {
        "n": n,
        "counted": counted,
        "win_rate": round(wins / counted, 4) if counted else None,
        "avg_r": round(sum(rs) / len(rs), 4) if rs else None,
    }


def _grouped_bucket_out(groups: dict[str, list]) -> dict:
    """分组读数（触点族 / 信号类型，2026-10-27）：每组 baseline + 分桶统计。

    形状同主报告 buckets；n<30 的桶标 insufficient。调用方保留既有混合
    buckets 原样（向后兼容），本函数只产附加读数、不影响 sufficient。
    """
    out: dict = {}
    for name, rows in sorted(groups.items()):
        sub: dict[str, list] = {}
        for row in rows:
            sub.setdefault(row[0], []).append(row)
        items = [
            {"bucket": b, **_bucket_stats(rs)}
            for b, rs in sorted(sub.items())
        ]
        for item in items:
            if item["n"] < 30:
                item["insufficient"] = True
        out[name] = {"baseline": _bucket_stats(rows), "buckets": items}
    return out


def _reentry_stats(rows: list) -> dict:
    """再入场前置卡（CARD-07，docs/13 #12）统计：n / continued / cont_rate。

    continued = 出场后 24h 内价格回到开仓价（signed-R MFE ≥ 0）的笔数；
    cont_rate = continued / n（止损后同向续走条件概率的主读数）。
    structure_ok_n 为第二前置读数（结构完好占比，docs/13 #12 预估 30-40%）；
    bucket 本身即 structure_ok/broken 语义，胜率与反事实口径均不适用。"""
    n = len(rows)
    if not n:
        return {"n": 0, "continued": 0, "cont_rate": None, "structure_ok_n": 0}
    cont = sum(1 for r in rows if r.get("continued_24h"))
    ok = sum(1 for r in rows if r.get("bucket") == "structure_ok")
    return {
        "n": n,
        "continued": cont,
        "cont_rate": round(cont / n, 4),
        "structure_ok_n": ok,
    }


def _early_stop_verdict(db, card: str, compute: Callable[[], dict]) -> dict:
    """早停宣告落锁（docs/14 §七，2026-10-09）：SPRT 三态判读的持久化封装。

    - 已有宣告（该卡曾跨界）→ 直接返回宣告快照 + locked/declared_at，
      不再现算——宣告后不随滚动窗口新样本回摆（跨界=检验终止，预注册语义）
    - 无宣告 → 现算；跨界（h1/h0）时首次落宣告（card 唯一）；
      continue 不落锁继续现算
    - 落锁任何异常只记 warning 不影响评估读数（吞异常铁律延伸）
    仅限预注册含早停通道的卡（CARD-07）；CARD-09 固定窗口协议不接本函数。
    """
    from app.models.shadow import ShadowEarlyStop
    try:
        declared = db.query(ShadowEarlyStop).filter(
            ShadowEarlyStop.card == card).first()
    except Exception as e:
        logger.warning("早停宣告查询失败（card=%s，降级现算）: %s", card, e)
        try:
            db.rollback()
        except Exception:
            pass
        declared = None
    if declared is not None:
        out = dict(declared.detail or {})
        out.setdefault("verdict", declared.verdict)
        out["locked"] = True
        out["declared_at"] = declared.created_at.isoformat()
        return out

    out = compute()
    if out.get("verdict") not in ("h1", "h0"):
        return out
    try:
        row = ShadowEarlyStop(card=card, verdict=out["verdict"], detail=dict(out))
        db.add(row)
        db.commit()
        out["locked"] = True
        # 时间源统一用行 created_at（与宣告读取路径同源，避免双钟微秒漂移）
        out["declared_at"] = row.created_at.isoformat()
    except Exception as e:
        logger.warning("早停宣告落锁失败（card=%s，本次现算展示）: %s", card, e)
        try:
            db.rollback()
        except Exception:
            pass
    return out


def _rule_version_key(v: Optional[str]) -> tuple:
    """版本序键（2026-10-13）：把 v2026-10-13-10 这类版本号按 `-` 分段转整数元组，
    修正字符串字典序在双位后缀下的错排（`-10` 字典序 < `-8`，语义却更大）。空/NULL
    视作最小（历史行出窗）；非法段按 -1 兜底，不因脏数据抛错。"""
    if not v:
        return (-1,)
    parts = []
    for seg in v.lstrip("v").split("-"):
        try:
            parts.append(int(seg))
        except ValueError:
            parts.append(-1)
    return tuple(parts)


def build_report(db, window_days: int = 30) -> list[dict]:
    """影子评估报告（docs/14 §六）：按特征分桶 join 复盘结果统计。

    归因链（scan_result 宿主）：ScanResult → AIAnalysis → TradeReview.outcome；
    无 AI 分析的标记记 no_analysis（配额未进），未复盘的记 pending 下轮再看。
    trade_record 宿主的卡（CARD-07 再入场前置 / CARD-09 评分门槛）各自特判。
    评估不回填 marks 行（避免双写漂移）。
    """
    from datetime import datetime, timedelta
    from sqlalchemy import select
    from app.models.scan import AIAnalysis, ScanResult, TradeReview
    from app.models.shadow import ShadowMark

    since = datetime.utcnow() - timedelta(days=window_days)
    out: list[dict] = []
    for key, feat in SHADOW_FEATURES.items():
        # 停用的卡不进报告（docs/14 §六.1「对每个 enabled 特征」）；裁决关闭的卡
        # 按新纪律直接删除代码（2026-10-19，见 docs/14 §十一），注册表已无条目
        if not feat.enabled:
            continue
        marks = db.execute(
            select(ShadowMark).where(
                ShadowMark.feature_key == key,
                ShadowMark.created_at >= since,
            ).order_by(ShadowMark.created_at)
        ).scalars().all()
        # 跨版本不混算过滤（docs/14 §六.1，2026-10-13）：卡声明起点后自动只计
        # rule_version ≥ 起点的标记——规则重构改变候选池，旧版本与历史 NULL 行
        # 出窗（免手工改窗口常量）；未声明起点的卡不受影响。版本序按数值段比较
        # （_rule_version_key），修正 -10/-8 双位后缀的字典序错排
        if feat.since_rule_version:
            since_key = _rule_version_key(feat.since_rule_version)
            marks = [m for m in marks
                     if _rule_version_key(m.rule_version) >= since_key]
        if not marks:
            out.append({"feature": key, "name": feat.name, "card": feat.card,
                        "min_sample": feat.min_sample,
                        "window_days": window_days, "sufficient": False,
                        "buckets": [], "baseline": {"n": 0, "counted": 0,
                                                    "win_rate": None, "avg_r": None}})
            continue

        # ── 再入场前置（CARD-07，docs/13 #12）：结构桶 × 续走率 ───────────
        # 第三种宿主语义（scan_result 归因链之外）：bucket = structure_ok/broken，
        # 读数 = 止损后 24h 同向续走条件概率。分层：正式网主判定组，测试网对照组
        # 不进判定。
        if feat.card == "CARD-07":
            prod_values: list = []
            testnet_values: list = []
            for m in marks:
                v = m.value_json or {}
                if (m.decision_json or {}).get("testnet"):
                    testnet_values.append(v)
                else:
                    prod_values.append(v)
            baseline = _reentry_stats(prod_values)
            buckets: dict[str, list] = {}
            for v in prod_values:
                buckets.setdefault(v.get("bucket", "unknown"), []).append(v)
            bucket_out = []
            for b, rows in sorted(buckets.items()):
                st = _reentry_stats(rows)
                if st["n"] < 30:
                    st["insufficient"] = True
                bucket_out.append({"bucket": b, **st})
            t_buckets: dict[str, int] = {}
            for v in testnet_values:
                b = v.get("bucket", "unknown")
                t_buckets[b] = t_buckets.get(b, 0) + 1
            # ── P-08 序贯判读（docs/14 §七 CARD-07 预注册 2026-10-08）────
            # structure_ok 桶：H0 续走率=40%（裁决门槛）vs H1=50%；
            # 达标即早停 h1（升格方向），显著低于门槛早停 h0（卡关闭）。
            # 早停落锁（2026-10-09）：首次跨界写宣告（shadow_early_stops），
            # 此后固定用宣告快照——不再随窗口新样本回摆（昨天 h1 今天
            # continue 的状态抖动）。
            # 预注册时点已积累 decided≈7（远未跨边界，判定不受影响）
            seq = None
            for item in bucket_out:
                if item["bucket"] == "structure_ok":
                    from app.services.sequential import sequential_verdict
                    seq = _early_stop_verdict(
                        db, "CARD-07",
                        lambda it=item: sequential_verdict(
                            it.get("continued", 0), it.get("n", 0),
                            p0=0.40, p1=0.50))
                    break
            out.append({
                "feature": key, "name": feat.name, "card": feat.card,
                "min_sample": feat.min_sample,
                "window_days": window_days,
                "baseline": baseline,
                "buckets": bucket_out,
                # P-08 序贯判读（structure_ok 桶，SPRT α=0.05/β=0.20 预注册）
                "sequential": seq,
                # 对照组：测试网读数（同 _reentry_stats 形状）+ 桶分布计数
                "testnet_baseline": {
                    **_reentry_stats(testnet_values),
                    "buckets": dict(sorted(t_buckets.items())),
                },
                # 达标判定：正式网打标笔数（两桶都计入——分桶本身是读数）
                "sufficient": baseline.get("n", 0) >= feat.min_sample,
            })
            continue

        # ── 评分门槛前向对照（CARD-09，docs/14 §七）：评分桶 × 真实盈亏 ──
        # 前向窗口过滤：REC70_START 之前平仓的存量不计入——门槛候选 70 正是
        # 从这些存量事后分桶看出来的，混入会污染前向判定（自我实现的回看）。
        # 分层同 CARD-07：正式网主判定组，测试网对照组不进判定。
        if feat.card == "CARD-09":
            prod_values: list = []
            testnet_values: list = []
            for m in marks:
                v = m.value_json or {}
                if ((v.get("closed_at") or "")[:10]) < REC70_START:
                    continue
                if (m.decision_json or {}).get("testnet"):
                    testnet_values.append(v)
                else:
                    prod_values.append(v)
            baseline = _rec70_stats(prod_values)
            buckets: dict[str, list] = {}
            for v in prod_values:
                buckets.setdefault(v.get("bucket", "unknown"), []).append(v)
            bucket_out = []
            for b, rows in sorted(buckets.items()):
                st = _rec70_stats(rows)
                if st["n"] < 30:
                    st["insufficient"] = True
                bucket_out.append({"bucket": b, **st})
            t_buckets: dict[str, int] = {}
            for v in testnet_values:
                b = v.get("bucket", "unknown")
                t_buckets[b] = t_buckets.get(b, 0) + 1
            # ── P-08 序贯判读（docs/14 §七 CARD-09 预注册 2026-10-08）────
            # rec60_69 桶每笔 R：H0 均值=0 vs H1 均值=−0.30R（效应量取存量
            # −0.659R 的约半——「显著负」的操作性定义，预注册后不可回调）；
            # 早停 h1 = 支持提门槛方向，早停 h0 = 存量 16 笔判运气差、卡关闭。
            # sd 用桶内已决定样本的样本标准差（n<2 → continue，引擎内置）
            # 注意：CARD-09 不走 _early_stop_verdict 落锁——协议为固定
            # 28 天窗口 + 三条件联合判定，无早停通道（docs/16 §0.4 S-01
            # 「不提前」纪律）；此 SPRT 是观察读数，跨界≠裁决，随数据
            # 滚动回摆属预期
            seq = None
            for item in bucket_out:
                if item["bucket"] == "rec60_69":
                    rows_69 = buckets.get("rec60_69") or []
                    rs = [r.get("r") for r in rows_69 if r.get("r") is not None]
                    if len(rs) >= 2:
                        m = sum(rs) / len(rs)
                        sd = (sum((v - m) ** 2 for v in rs) / (len(rs) - 1)) ** 0.5
                        from app.services.sequential import sprt_normal_verdict
                        seq = sprt_normal_verdict(m, len(rs), sd, delta=-0.30)
                    break
            out.append({
                "feature": key, "name": feat.name, "card": feat.card,
                "min_sample": feat.min_sample,
                "window_days": window_days,
                "baseline": baseline,
                "buckets": bucket_out,
                # P-08 序贯判读（rec60_69 桶 R 均值 SPRT，δ=−0.30R 预注册）
                "sequential": seq,
                # 对照组：测试网读数（同 _rec70_stats 形状）+ 桶分布计数
                "testnet_baseline": {
                    **_rec70_stats(testnet_values),
                    "buckets": dict(sorted(t_buckets.items())),
                },
                # 达标判定：正式网前向窗口打标笔数 ≥ 卡最小样本
                "sufficient": baseline.get("n", 0) >= feat.min_sample,
            })
            continue

        # ── 常规特征（scan_result 宿主）：outcome 归因链统计 ─────────────
        # 批量取宿主链结果：ref_id → (outcome, 回放 R 读数)。R 与 outcome 同源
        # （同一 TradeReview 建议回放），口径 _scan_replay_r（预注册 2026-10-19）
        ref_ids = [m.ref_id for m in marks]
        sr_rows = db.execute(
            select(ScanResult.id, AIAnalysis.id, TradeReview.outcome,
                   AIAnalysis.direction, AIAnalysis.entry_price,
                   AIAnalysis.stop_loss, AIAnalysis.take_profit_1,
                   AIAnalysis.take_profit_2, AIAnalysis.veto_original,
                   TradeReview.is_veto, TradeReview.bars_to_exit,
                   ScanResult.signal_type)
            .outerjoin(AIAnalysis, AIAnalysis.scan_result_id == ScanResult.id)
            .outerjoin(TradeReview, TradeReview.ai_analysis_id == AIAnalysis.id)
            .where(ScanResult.id.in_(ref_ids))
        ).all()
        outcome_map: dict = {}
        r_map: dict = {}
        # CARD-10 分信号类型读数（2026-10-27 风险复核 R1-A）：宿主 ScanResult
        # 的 signal_type 取自已落库结果，历史 marks 同样适用（不依赖打标快照）
        sig_map: dict = {}
        for (sr_id, _ai_id, outcome, direction, entry, stop, tp1, tp2,
             veto_original, is_veto, bars, sr_signal_type) in sr_rows:
            # 同一 scan_result 理论一条 AIAnalysis（唯一约束），outerjoin 重复行取首个非空
            if sr_id not in sig_map:
                sig_map[sr_id] = sr_signal_type
            if sr_id not in outcome_map or outcome_map[sr_id] is None:
                outcome_map[sr_id] = outcome
                r_map[sr_id] = _scan_replay_r(
                    _scan_prices(direction, entry, stop, tp1, tp2,
                                 veto_original, is_veto), outcome, bars)
        buckets: dict[str, list] = {}
        # CARD-08 分族读数（2026-10-27 开庭延期，docs/14 §七）：标准卡主判定为
        # swing 族，retest 仅观察列——报告按 value_json.touch_ref 另出 family_buckets，
        # 既有混合 buckets 保持不变（向后兼容）。
        family_rows: dict[str, list] = {}
        # CARD-10 分信号类型读数（2026-10-27 风险复核 R1-A）：折价是否应限定
        # 突破类需先看回踩/突破各自读数——仅报告层附加，不改生产判定。
        sig_rows: dict[str, list] = {}
        for m in marks:
            # 归一：宿主缺失（ScanResult 被清）→ no_analysis；有 AIAnalysis
            # 无 TradeReview → pending（未到 24h，下轮再看）
            outcome = outcome_map.get(m.ref_id)
            label = "no_analysis" if m.ref_id not in outcome_map else (outcome or "pending")
            b = (m.value_json or {}).get("bucket", "unknown")
            row = (b, label, r_map.get(m.ref_id))
            buckets.setdefault(b, []).append(row)
            if feat.card == "CARD-08":
                fam = (m.value_json or {}).get("touch_ref")
                if fam in ("swing", "retest"):
                    family_rows.setdefault(fam, []).append(row)
            elif feat.card == "CARD-10":
                # 宿主被清时无法取 signal_type → unknown 单列，不混入判定组
                sig_rows.setdefault(sig_map.get(m.ref_id) or "unknown", []).append(row)
        bucket_out = [
            {"bucket": b, **_bucket_stats(rows)}
            for b, rows in sorted(buckets.items())
        ]
        for item in bucket_out:
            if item["n"] < 30:
                item["insufficient"] = True
        family_out = _grouped_bucket_out(family_rows)
        report = {
            "feature": key, "name": feat.name, "card": feat.card,
            "min_sample": feat.min_sample,
            "window_days": window_days,
            "baseline": _bucket_stats([r for rows in buckets.values() for r in rows]),
            "buckets": bucket_out,
            # 达标判定：有效归因样本 ≥ 卡最小样本（docs/14 §七 预注册后不可回调）。
            # **展示口径·必要非充分**（2026-10-19，§十一 遗留问题 2）：此处为**全卡**
            # counted 口径，各卡真实判据含**分桶**门槛（CARD-01 n≥80/桶、CARD-10 判定桶
            # 各≥60）——绿灯只保证够开庭样本，判定仍须人工核对 §七 卡文本
            "sufficient": _bucket_stats([r for rows in buckets.values() for r in rows])["counted"] >= feat.min_sample,
        }
        if feat.card == "CARD-08":
            report["family_buckets"] = family_out
        elif feat.card == "CARD-10":
            report["signal_type_buckets"] = _grouped_bucket_out(sig_rows)
        out.append(report)
    # 开庭日期感知（2026-10-10）：每卡带预注册开庭日（docs/14 §七 时间锁），
    # 前端据此三态——样本不足=观察中 / 达标未到期=达标·开庭待期 / 到期=可开庭。
    # 统一在出口注入，五条组装分支免逐个接线
    for card in out:
        card["court_date"] = COURT_DATES.get(card.get("card"))
    return out


# ── sweep_reclaim（CARD-08，docs/13 #10 转影子，2026-10-07）──────────────
#
# 流动性猎取确认档：回踩确认候选触点位的插针×收回形态。docs/26 模式预注册
# 回放两轮判「转影子」后实施（口径与门槛见 docs/14 §七 CARD-08，锁定不可
# 回调）。参考位 = 生产原生触点位（零漂移，这是回放两轮的核心教训）：
# - swing 当根确认：trigger._detect_swing_touch 同输入复算（同 klines 同 band 同阶数）
# - swing 次根确认：PullbackPending.ema21（Stage-1 触碰锚点，upsert 时存）
# - retest：破轨事件 price（当根）/ pending 锚点（次根）
# EMA 触点族不打标：回放证明该族插针收回是常态（~91%），无选择性。
# 请求预算：每个 swing/retest 族回踩候选 +1 次 15m 只读拉取（130 根）。
# 铁律不变：异常全吞、None 不打标、不写回决策字段。

SWEEP_DEPTH = 0.001        # 插针判定深度 d：击穿参考位 0.1%（回放主参数）
SWEEP_RECLAIM_BARS = 8     # 收回时限 N：末次插针起 8 根 15m（2h，回放主参数）
SWEEP_BAR_MS = 900_000     # 15m
SWEEP_WIN_MIN_H = 2        # 无 pending（当根）时插针窗起点 = 信号bar开盘前 2h
SWEEP_WIN_MAX_H = 24       # 插针窗上限（pending Stage-1 最长 24h）


def _sweep_bucket(bars: dict, level: float, long_dir: bool,
                  t_close_ms: int, win_start_ms: int,
                  d: float = SWEEP_DEPTH, n_reclaim: int = SWEEP_RECLAIM_BARS) -> Optional[dict]:
    """纯几何分桶（docs/14 §七 CARD-08 口径）。

    bars: {open_ms: (high, low, close)}，已滤未收盘根。窗 = [win_start, t_close]
    （信号 bar 收盘 = 打标时刻，未来不可知，收回判定截至此处）。
    - sweep_reclaim：末次插针起 ≤N 根内 15m 收盘回参考位内侧（k=0 含单根
      长脚插针当根收回）
    - sweep_open：有插针、截至打标时刻未收回（升格后门语义关键观察桶）
    - no_sweep：窗内未击穿 d 深度
    数据不足（窗内无 bar / level 非法）返回 None（不打标）。
    """
    win = {ts: v for ts, v in bars.items()
           if win_start_ms <= ts and ts + SWEEP_BAR_MS <= t_close_ms}
    if not win or level <= 0:
        return None
    if long_dir:
        swept = sorted(ts for ts, v in win.items() if v[1] <= level * (1 - d))
        depth = min((level - win[ts][1]) / level for ts in swept) if swept else 0.0
        back = lambda c: c > level  # noqa: E731
    else:
        swept = sorted(ts for ts, v in win.items() if v[0] >= level * (1 + d))
        depth = min((win[ts][0] - level) / level for ts in swept) if swept else 0.0
        back = lambda c: c < level  # noqa: E731
    facts = {"sweep_bars": len(swept), "depth_pct": round(depth * 100, 3),
             "reclaim_bars": None}
    if not swept:
        return {"bucket": "no_sweep", **facts}
    last = swept[-1]
    for k in range(n_reclaim + 1):
        ts = last + k * SWEEP_BAR_MS
        if ts in win and back(win[ts][2]):
            # k 必须在 **facts 之后展开：否则被 facts.reclaim_bars=None 覆盖
            return {"bucket": "sweep_reclaim", **facts, "reclaim_bars": k}
    return {"bucket": "sweep_open", **facts}


def compute_sweep_reclaim_scan(klines: list, cand: dict, pending: Optional[dict] = None,
                               retest: Optional[dict] = None, flt=None, pool=None) -> Optional[dict]:
    """CARD-08 打标入口（scan_one 特例接线，非注册表 compute 签名）。

    生产原生触点位解析（零漂移）→ 15m 拉取（+1 只读请求）→ _sweep_bucket。
    任何一步失败返回 None（不打标，影子铁律）。
    """
    if not klines or len(klines) < 3 or not cand or pool is None:
        return None
    if cand.get("signal_type") != "trigger_pullback_confirm":
        return None
    touch_ref = cand.get("touch_ref")
    if touch_ref not in ("swing", "retest"):
        return None  # EMA 族/None 不打标（2026-10-07 回放结论：常态噪声）
    net = (cand.get("votes") or {}).get("net") or 0
    if net == 0:
        return None
    long_dir = net > 0
    direction = 1 if long_dir else -1
    try:
        from app.services.strategy import trigger as trigger_mod

        lvl = None
        path = None
        if touch_ref == "swing":
            band = float(getattr(flt, "structure_touch_band_pct", 0.0) or 0.0)
            # 阶数跟随配置（2026-10-12）；flt 缺省（直接调用）回落 env，与 trigger 同源
            from app.config import settings
            order = int(getattr(flt, "swing_order", 0) or settings.SWING_ORDER)
            lvl = trigger_mod._detect_swing_touch(klines, direction, band, order)  # 当根
            if lvl is not None:
                path = "same_candle"
        elif (touch_ref == "retest" and retest
              and retest.get("direction") == direction):
            lvl = float(retest.get("price") or 0) or None  # 当根：破轨事件价
            if lvl is not None:
                path = "same_candle"
        if lvl is None and pending and pending.get("touch_ref") == touch_ref:
            anchor = pending.get("ema21")
            if anchor:
                lvl = float(anchor)
                path = "next_candle"  # 次根确认：Stage-1 触碰锚点
        if not lvl or lvl <= 0:
            return None

        sig_open_ms = int(float(klines[-2][0]))  # 信号 bar = 最后一根已收盘 1h
        t_close_ms = sig_open_ms + 3_600_000
        win_start_ms = sig_open_ms - SWEEP_WIN_MIN_H * 3_600_000
        if pending and pending.get("stage1_candle_open") is not None:
            s1_ms = pending["stage1_candle_open"].replace(
                tzinfo=None).timestamp() * 1000
            win_start_ms = min(win_start_ms, s1_ms)
        win_start_ms = max(win_start_ms, t_close_ms - SWEEP_WIN_MAX_H * 3_600_000)

        kl = pool.get_klines_range(cand["symbol"], "15m", 130, t_close_ms)
        bars = {int(k[0]): (float(k[2]), float(k[3]), float(k[4]))
                for k in kl if int(k[0]) + SWEEP_BAR_MS <= t_close_ms}
        res = _sweep_bucket(bars, float(lvl), long_dir, t_close_ms, win_start_ms)
        if res is None:
            return None
        res.update({
            "touch_ref": touch_ref, "touch_path": path,
            "direction": "long" if long_dir else "short",
            "level": round(float(lvl), 8),
            "window_h": round((t_close_ms - win_start_ms) / 3_600_000, 1),
        })
        return res
    except Exception as e:
        logger.debug("sweep_reclaim 打标失败（忽略）%s: %s", cand.get("symbol"), e)
        return None
