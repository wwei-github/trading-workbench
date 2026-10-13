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


def compute_macd_div(klines: list[list]) -> Optional[dict]:
    """CARD-04 macd_div：MACD 背离（缠论盘整背驰可取子集）。

    口径（technical.analyze_macd_div）：近 24 根已收盘K线前后半段比较——
    - diverge_bear：后半段价格新高而 MACD 柱峰值衰减（多头信号质量差）
    - diverge_bull：后半段价格新低而柱低谷抬升（空头信号质量差）
    - confirm：其余（与 cvd_div 桶语义对齐）
    样本不足（MACD 柱序列不足 24 根）返回 None。
    """
    from app.services.strategy.technical import analyze_macd_div
    return analyze_macd_div(klines)


def compute_oi_conf_scan_stub(klines: list[list]) -> None:
    """oi_conf 扫描侧占位：该特征需要 symbol 维度的 OI 快照环与网络请求，
    无法从单币 klines 现算——扫描循环对本特征恒得 None 不打标；
    实际打标由 tag_oi_conf 在 AI 配额段（tasks/ai_tasks.py）执行。
    注册表占位仅为让评估报告（build_report）自动覆盖该特征元数据。"""
    return None


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
    enabled: bool = True           # 关闭的卡不再打标、不进评估报告（docs/14 §六.1：
                                   # 「对每个 enabled 特征取窗口标记」）；裁决关闭
                                   # 记录见 docs/14 §十一 归档表
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
        # CARD-03 于 2026-10-13 裁决关闭（docs/14 §十一）：假设「压缩期假突破率高
        # → 折价」的动作已由 range_score 布林带收口项落地进主线，且影子读数反向
        # （compress 胜率高于 expand）——卡已无独立信息量，停打标、出报告
        ShadowFeature("vol_regime", "波动率状态（ATR14/ATR100）", "CARD-03", compute_vol_regime, 100,
                      enabled=False),
        # 跨版本不混算（同上，2026-10-13）：候选池重构后前向窗口重置为当前版本
        ShadowFeature("macd_div", "MACD 背离（缠论背驰子集）", "CARD-04", compute_macd_div, 60,
                      since_rule_version="v2026-10-13-8"),
        ShadowFeature("oi_conf", "OI 确认度（24h ΔOI）", "CARD-02", compute_oi_conf_scan_stub, 60),
        # 趋势质量（docs/16 S-10，CARD-10，2026-10-08 立卡）：r²/slope 影子侧
        # 自算（ScanResult.r_squared 历史门槛废弃时置 0 恒空）。min_sample=120
        # 为展示口径（判定桶 r2_high/r2_low 各 ≥60，r2_mid 不进判定——真实
        # 判据在 docs/14 §七 CARD-10 文本）
        ShadowFeature("r2_slope", "趋势质量（R²/slope）", "CARD-10", compute_r2_slope, 120),
        # 早期保护反事实（docs/15 §五）：trade_record 宿主，打标不走扫描侧
        # compute 循环（由 tag_trade_close 在归因任务打标），注册仅为让
        # build_report 输出元数据与反事实统计。min_sample = 预注册 decided 门槛
        ShadowFeature("be_retrace", "BE@0.8R 保本反事实（A 腿）", "CARD-05", lambda k: None, 30),
        # CARD-06 于 2026-10-13 裁决关闭（docs/14 §十一）：TP0 微止盈已被
        # docs/15 v1/v2 两轮预注册回放参数稳健地证伪（期望 -0.107R / -0.096R），
        # 终局方案 B 腿归档——保留打标只是重复已知结论，停打标、出报告
        ShadowFeature("tp0_retrace", "TP0 微止盈反事实（B 腿）", "CARD-06", lambda k: None, 60,
                      enabled=False),
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
    "CARD-01": "2026-10-18",  # 打标上线 10-04 + 14 天
    "CARD-02": "2026-10-19",  # 打标上线 10-05 + 14 天
    "CARD-03": "2026-10-25",  # 打标上线 10-04 + 21 天（regime 切换慢）
    "CARD-04": "2026-10-19",  # 打标上线 10-05 + 14 天
    "CARD-05": None,          # decided ≥30 且 saved:injured ≥4:1，到量即判
    "CARD-06": None,          # decided ≥60 且 ΣΔR>0 且被扫<45%，到量即判
    "CARD-07": None,          # SPRT 早停通道，跨界即终局（落锁后仍走 §八）
    "CARD-08": "2026-10-21",  # 锁定 10-07 + 14 天
    "CARD-09": "2026-11-08",  # REC70_START(10-11) + 28 天（评估仅计 closed_at ≥ REC70_START；
                              # 10-11 随 EMA 状态机重构重置窗口，锁定期顺延 1 天）
    "CARD-10": "2026-10-22",  # 锁定 10-08 + 14 天
}


# ── oi_conf（CARD-02，AI 侧，docs/14 §十清单4）──────────────────────────

OI_CONF_ENABLED = True  # 代码开关（§九：特征启停改代码不进配置页；紧急置 False 停打）

# 24h OI 快照环：Redis zset（key=shadow:oi:{symbol}，score=ts，member="ts|oi"）。
# 只在 AI 配额轮追加（每小时 ≤ 配额数个 symbol）；保留 48h / 48 条供跨日 24h 前值
# 比较。必须跨进程共享——worker --concurrency=10 的 prefork 子进程各持独立内存，
# 进程内环会让同一币种 24h 前后的快照几乎必然落进不同子进程 → 永远凑不出前值
# （2026-10-06 修复：上线以来零打标的根因）。broker 同实例，前缀隔离不冲突。
_OI_RING: dict[str, list[tuple[float, float]]] = {}  # 测试替身用 dict 环（测试 monkeypatch 环函数）
_OI_RING_TTL_S = 48 * 3600
_OI_RING_MAXLEN = 48
# 24h 前值容差窗口：定时扫描每小时一轮，取距 now-24h 最近的快照，
# 落在 [18h, 30h] 内才有效（漏扫几轮仍可比，节奏漂移过大不可比）
_OI_PREV_WINDOW_S = (18 * 3600, 30 * 3600)

_oi_redis_client = None


def _oi_redis():
    """惰性单例 Redis 客户端（broker 同实例）。子进程首次打标时才建立连接，
    不存在 fork 继承 socket 问题（对比 engine.dispose 的 fork 教训）。"""
    global _oi_redis_client
    if _oi_redis_client is None:
        import redis

        from app.config import settings

        _oi_redis_client = redis.Redis.from_url(
            settings.CELERY_BROKER_URL, decode_responses=True)
    return _oi_redis_client


def _ring_add(sym: str, ts: float, oi: float) -> None:
    """快照入环：zadd + TTL 清理 + 条数上限，pipeline 原子执行。"""
    rds = _oi_redis()
    key = f"shadow:oi:{sym}"
    pipe = rds.pipeline()
    pipe.zadd(key, {f"{ts:.6f}|{oi:.12g}": ts})
    pipe.zremrangebyscore(key, "-inf", ts - _OI_RING_TTL_S)
    pipe.zremrangebyrank(key, 0, -(_OI_RING_MAXLEN + 1))
    pipe.execute()


def _ring_window(sym: str, lo: float, hi: float) -> list[tuple[float, float]]:
    """取环内 [lo, hi] 时间窗快照 [(ts, oi), ...]，ts 升序。"""
    out = _oi_redis().zrangebyscore(f"shadow:oi:{sym}", lo, hi, withscores=True)
    return [(float(m.split("|")[0]), float(m.split("|")[1])) for m, _s in out]


def tag_oi_conf(scan_results: list) -> int:
    """AI 配额段 oi_conf 打标（CARD-02）：候选逐币拉当前 OI 快照入环，
    环内有 18~30h 前值才计算 ΔOI 分桶落 shadow_marks，否则只存快照不打标。

    分桶（CARD-02 预注册）：ΔOI 24h 符号——增=新钱入场 confirm、
    减=回补/去杠杆 diverge；信号方向（votes net 符号）只进 value_json，
    供评估期四象限细分，不参与分桶。幂等：uq_shadow_mark 重复打标覆盖。
    请求预算：每轮 ≤ len(候选)（配额默认 10）个只读请求（docs/14 §四）。
    任何异常全吞（影子铁律），返回实际落标数。
    """
    if not OI_CONF_ENABLED or not scan_results:
        return 0
    import time

    from sqlalchemy import select

    from app.database import SessionLocal
    from app.models.shadow import ShadowMark
    from app.services.exchange_pool import ExchangePool

    pool = ExchangePool()
    now = time.time()
    oi_now_map: dict[str, float] = {}
    for r in scan_results:
        if r.symbol in oi_now_map:
            continue
        try:
            oi_now_map[r.symbol] = pool.get_open_interest(r.symbol)
        except Exception as e:
            logger.debug("oi_conf OI 拉取失败（不打标）%s: %s", r.symbol, e)
    if not oi_now_map:
        return 0

    tagged = 0
    db = SessionLocal()
    try:
        for r in scan_results:
            sym = r.symbol
            if sym not in oi_now_map:
                continue
            oi_now = oi_now_map[sym]
            lo, hi = now - _OI_PREV_WINDOW_S[1], now - _OI_PREV_WINDOW_S[0]
            window = _ring_window(sym, lo, hi)
            prev = (
                min(window, key=lambda p: abs(p[0] - (now - 86400)))
                if window else None
            )
            # 快照入环（无论是否打标），TTL 清理与条数上限在环函数内消化
            _ring_add(sym, now, oi_now)

            if prev is None or prev[1] <= 0:
                continue  # 无 24h 前值：先存快照，本轮不打标
            oi_chg_pct = (oi_now - prev[1]) / prev[1] * 100
            votes = r.votes_json if isinstance(r.votes_json, dict) else {}
            net = votes.get("net") or 0
            value = {
                "oi_now": round(oi_now, 8), "oi_prev": round(prev[1], 8),
                "oi_chg_pct": round(oi_chg_pct, 3),
                "direction": "long" if net > 0 else "short" if net < 0 else None,
                "bucket": "confirm" if oi_chg_pct > 0 else "diverge",
            }
            decision = {
                "stage": "ai_quota", "signal_type": r.signal_type,
                "breakout_pct": float(r.breakout_pct or 0),
            }
            existing = db.execute(
                select(ShadowMark).where(
                    ShadowMark.feature_key == "oi_conf",
                    ShadowMark.ref_type == "scan_result",
                    ShadowMark.ref_id == r.id,
                )
            ).scalars().first()
            if existing:
                existing.value_json = value
                existing.decision_json = decision
            else:
                db.add(ShadowMark(
                    feature_key="oi_conf", ref_type="scan_result", ref_id=r.id,
                    symbol=sym, value_json=value, decision_json=decision,
                ))
            tagged += 1
        db.commit()
        return tagged
    except Exception as e:
        db.rollback()
        logger.debug("oi_conf 影子打标落库失败（忽略）: %s", e)
        return 0
    finally:
        db.close()


# ── 早期保护反事实打标（docs/15 §五，CARD-05/06，trade_close 宿主）────────
#
# 与上面四特征（开仓前打标）不同：be_retrace / tp0_retrace 是「出场管理规则」
# 的影子——规则效果取决于持仓期价格路径，必须等平仓后对整段路径做反事实重放。
# 宿主 = trade_record（ref_type="trade_record"），打标点挂归因任务开头
# （tasks/review_tasks.run_ai_attribution_task，AI-Key 门之前，不依赖 LLM）。
# 铁律不变：任何异常吞掉；结果只进 shadow_marks，不回写 TradeRecord、不进 AI。

# BE@1R 巡检保本反事实参数（docs/15 §4.5 定稿 BE@0.8R）
BE_AT_R = 0.8
# TP0 前置微止盈反事实参数（docs/15 §4.2 定稿 0.5R/25%/gate1.2；B 腿已归档，
# 此处按 v0 定稿参数重放——变体比较靠 value_json.path 离线重算，不重拉 K 线）
TP0_R = 0.5
TP0_QTY = 0.25
TP0_GATE_R = 1.2
_PATH_MAX_POINTS = 60      # 路径数组上限（长持仓降采样）

# 前向窗口起点（2026-10-13 重置，同 CARD-09 的 closed_at 口径）：评估仅计
# closed_at ≥ 本值。2026-10-12 出场链改「恒 ladder」（1.5R 止盈/保本+跟进，
# 删策略二与 trail 纯跟踪），反事实对比的「真实 R」出处随之改变——
# 之前平仓的存量与新口径不同源，不可比 → 只打标不计入评估。
EARLY_PROTECT_START = "2026-10-12"


def compute_early_protect(klines: list[list], rec) -> Optional[dict]:
    """CARD-05/06 共享路径基元：对一笔已平仓交易做持仓期逐根反事实重放。

    口径（docs/15 §二/§4.5）：R = |entry − 初始止损|，初始止损由调用方解包后
    以 rec.stop_initial 传入（TradeRecord.stop_loss 列是跟踪止损后的现值，
    不可用；正确来源 = OPEN 事件 detail.stop_loss 快照，见 tag_trade_close）。
    klines 为持仓期已收盘 1h K 线（时间升序，含开仓时刻所在的根）。

    返回 dict：共享基元（r_unit/stop_initial/path/mfe_r/mae_r/tp1_r/duration_h/
    direction/exit_reason/real_r）+ A 腿（be_retrace）+ B 腿（tp0_retrace）两组；
    无反事实意义（缺止损快照/风险额/无K线）返回 None 不打标。
    """
    entry = float(rec.entry_price or 0)
    stop0 = float(getattr(rec, "stop_initial", 0) or 0)
    risk = float(rec.risk_amount or 0)
    qty = float(rec.qty or 0)
    if entry <= 0 or stop0 <= 0 or risk <= 0 or qty <= 0 or not klines:
        return None
    d = 1 if (rec.direction or "long") == "long" else -1
    r_unit = abs(entry - stop0)
    if r_unit <= 0:
        return None

    def r_of(price: float) -> float:
        return d * (price - entry) / r_unit

    # 路径基元：逐根 [high_r, low_r]（对 R 空间），>48h 降采样保 ≤60 点
    path = [[round(r_of(float(k[2])), 4), round(r_of(float(k[3])), 4)] for k in klines]
    if len(klines) > _PATH_MAX_POINTS:
        step = -(-len(path) // _PATH_MAX_POINTS)
        path = path[::step]

    mfe_r = max(max(h, l) for h, l in path)
    mae_r = min(min(h, l) for h, l in path)
    tp1 = float(rec.tp1 or 0)
    tp1_r = r_of(tp1) if tp1 > 0 else None
    duration_h = None
    if rec.opened_at and rec.closed_at:
        duration_h = round((rec.closed_at - rec.opened_at).total_seconds() / 3600, 1)
    real_r = float(rec.realized_pnl or 0) / risk

    # ── A 腿反事实（BE@BE_AT_R 巡检保本，严格口径：触线K收盘后下一根生效）──
    armed = False
    a_r = real_r
    a_out = "carry"           # carry=窗口结束未触发，沿用真实出场
    hours_to_arm = None
    post_arm_below_entry = False
    for i, (hi_r, lo_r) in enumerate(path):
        hit_r = hi_r >= BE_AT_R if d > 0 else lo_r <= -BE_AT_R
        hit_sl = lo_r <= -1.0 if d > 0 else hi_r >= 1.0
        hit_be = lo_r <= 0.0 if d > 0 else hi_r >= 0.0
        armed_prev = armed
        if hit_r:
            armed = True      # 本根收盘后巡检武装，下一根起生效
            if hours_to_arm is None:
                hours_to_arm = round(((i + 1) * (duration_h / len(path)) if duration_h
                                      else (i + 1)), 1)
        if hit_sl and not armed_prev:
            a_r, a_out = -1.0, "loss_keep"
            break
        if armed_prev and hit_be:
            a_r, a_out = 0.0, "be_exit"
            post_arm_below_entry = True
            break

    # ── B 腿反事实（TP0 限价单挂上即守，成交即武装保本；docs/15 §4.2）──────
    gate = tp1_r is not None and tp1_r >= TP0_GATE_R
    tp0_r = d * TP0_R
    tp0_hit = False
    tp0_hit_i = -1
    if gate:
        for i, (hi_r, lo_r) in enumerate(path):
            if (hi_r >= tp0_r if d > 0 else lo_r <= tp0_r):
                tp0_hit, tp0_hit_i = True, i
                break
    b_r = real_r
    b_out = "carry"
    survived_pullback = None
    if gate and tp0_hit:
        # TP0 成交后：TP0 腿落袋 0.25×0.5R；剩余仓 SL=保本（entry±0，保守），
        # 之后先触保本（be_after_tp0）或 TP1（win_after_tp0 = TP1 份额的全程收益）
        b_r = TP0_QTY * TP0_R
        b_out = "tp0_only"
        for hi_r, lo_r in path[tp0_hit_i + 1:]:
            hit_be = lo_r <= 0.0 if d > 0 else hi_r >= 0.0
            hit_tp1 = hi_r >= tp1_r if d > 0 else lo_r <= tp1_r
            if hit_be:
                b_out = "be_after_tp0"  # 剩余仓 ≈0R
                break
            if hit_tp1:
                b_r += (1 - TP0_QTY) * tp1_r
                b_out = "win_after_tp0"
                survived_pullback = True
                break
        if b_out == "tp0_only":
            survived_pullback = True  # 撑到真实出场时刻未被保本扫出
        if b_out == "be_after_tp0":
            survived_pullback = False
    elif gate:
        b_out = "tp0_miss"        # 挂了但价格未到 0.5R：按真实出场（与 carry 同值）
    else:
        b_out = "below_gate"      # TP1 距离不足门控：不挂 TP0

    # 桶判据（docs/15 §五 评估层）
    if a_out in ("loss_keep", "be_exit") and real_r <= -0.5 and a_r >= -0.2:
        be_bucket = "saved"
    elif real_r > 0.05 and a_r <= 0.05:
        be_bucket = "injured"
    elif not armed:
        be_bucket = "no_arm"
    else:
        be_bucket = "carry"

    if b_out == "be_after_tp0" and real_r > 0.05:
        tp0_bucket = "injured"    # 真实赢利被保本扫出（误伤）
    elif b_out == "be_after_tp0" and real_r <= -0.5:
        tp0_bucket = "protected"  # 真实亏损被保护
    elif b_out == "win_after_tp0":
        tp0_bucket = "survived"   # 回踩后仍走到原出场
    elif b_out == "below_gate":
        tp0_bucket = "below_gate"
    else:
        tp0_bucket = "untouched"  # tp0_only / tp0_miss / carry（无反事实变化）

    _closed_at = getattr(rec, "closed_at", None)
    return {
        # 共享路径基元
        "r_unit": round(r_unit, 8), "stop_initial": stop0,
        "mfe_r": round(mfe_r, 4), "mae_r": round(mae_r, 4),
        "tp1_r": round(tp1_r, 4) if tp1_r is not None else None,
        "duration_h": duration_h, "direction": rec.direction,
        "exit_reason": rec.exit_reason, "real_r": round(real_r, 4),
        "closed_at": (_closed_at.replace(tzinfo=None).isoformat()
                      if _closed_at else None),
        "path": path,
        # A 腿（be_retrace）
        "a_r": round(a_r, 4), "a_out": a_out, "armed": armed,
        "hours_to_arm": hours_to_arm,
        "post_arm_below_entry": post_arm_below_entry,
        "a_delta_r": round(a_r - real_r, 4), "bucket": be_bucket,
        # B 腿（tp0_retrace）
        "b_r": round(b_r, 4), "b_out": b_out,
        "tp0_hit": tp0_hit, "gate": gate,
        "survived_pullback": survived_pullback,
        "b_delta_r": round(b_r - real_r, 4), "bucket_tp0": tp0_bucket,
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
    risk = float(rec.risk_amount or 0)
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
    """trade_close 宿主打标（docs/15 §五）：对已平仓交易逐笔做早期保护反事实。

    每笔交易产生两行影子标记：be_retrace（CARD-05）+ tp0_retrace（CARD-06）；
    CARD-06 于 2026-10-13 裁决关闭后仅打 be_retrace 一行（docs/14 §十一）。
    value_json 同为 compute_early_protect 输出（path 基元共用，≈2KB/笔）。
    K 线由调用方（归因任务）按持仓区间拉取传入；无 K 线/缺快照的交易静默跳过。
    幂等：uq_shadow_mark 同 (feature_key, trade_record, ref_id) 覆盖更新。
    任何异常全吞（影子铁律），返回实际落标行数。
    """
    if not trades:
        return 0
    from sqlalchemy import select

    from app.database import SessionLocal
    from app.models.shadow import ShadowMark
    from app.services.exchange_pool import ExchangePool

    pool = ExchangePool()
    tagged = 0
    db = SessionLocal()
    try:
        # 初始止损快照（docs/15 口径）：OPEN 事件 detail.stop_loss——
        # stop_loss 列是跟踪止损后的现值不可用；批量一次查出避免逐笔查事件表
        trade_ids = [t.id for t in trades]
        from app.models.trade import TradeEvent
        open_events = db.execute(
            select(TradeEvent.trade_record_id, TradeEvent.detail)
            .where(
                TradeEvent.trade_record_id.in_(trade_ids),
                TradeEvent.event_type == "OPEN",
            )
        ).all()
        stop_map: dict = {}
        for tr_id, detail in open_events:
            try:
                sl = float((detail or {}).get("stop_loss") or 0)
            except (TypeError, ValueError):
                sl = 0.0
            if sl > 0:
                stop_map[tr_id] = sl
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
            try:
                si = stop_map.get(rec.id)
                if si:
                    rec.stop_initial = si  # compute 读此属性（仅快照有值时挂）
                # 持仓期已收盘 1h K 线（开仓时刻起，到平仓为止）
                opened_ms = int(rec.opened_at.replace(tzinfo=None).timestamp() * 1000) \
                    if rec.opened_at else 0
                closed_ms = int(rec.closed_at.replace(tzinfo=None).timestamp() * 1000) - 1 \
                    if rec.closed_at else 0
                if opened_ms <= 0 or closed_ms <= opened_ms:
                    continue
                kls = pool.get_klines_range(rec.symbol, "1h", 500, closed_ms) or []
                held = [k for k in kls if int(k[0]) >= opened_ms - 3600_000]
                if not held:
                    continue
                value = compute_early_protect(held, rec)
                if value is None:
                    continue
                decision = {
                    "stage": "trade_close",
                    "testnet": bool(rec.testnet),
                    "account_key": rec.account_key,
                    "status": rec.status,
                }
                for feat_key in ("be_retrace", "tp0_retrace"):
                    # 关闭的卡不再打标（2026-10-13：CARD-06 已裁决关闭）
                    if not SHADOW_FEATURES[feat_key].enabled:
                        continue
                    existing = db.execute(
                        select(ShadowMark).where(
                            ShadowMark.feature_key == feat_key,
                            ShadowMark.ref_type == "trade_record",
                            ShadowMark.ref_id == rec.id,
                        )
                    ).scalars().first()
                    if existing:
                        existing.value_json = value
                        existing.decision_json = decision
                    else:
                        db.add(ShadowMark(
                            feature_key=feat_key, ref_type="trade_record",
                            ref_id=rec.id, symbol=rec.symbol,
                            testnet=bool(rec.testnet),
                            value_json=value, decision_json=decision,
                        ))
                    tagged += 1
                db.commit()
            except Exception as e:
                db.rollback()
                logger.debug("trade_close 影子打标失败（忽略）%s: %s",
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
    恰含出场前 ~350h 结构窗与出场后 48h 续走窗）。R 空间同 CARD-05/06
    （signed-R：正向 = 对持仓有利）。返回 None = 不打标
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
    risk = float(rec.risk_amount or 0)
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


def _trade_protect_stats(rows: list, delta_key: str,
                         bucket_key: Optional[str] = None,
                         judged_buckets: Optional[tuple] = None) -> dict:
    """早期保护特征（trade_record 宿主）分桶统计：反事实读数为主。

    rows = value_json dict 列表（compute_early_protect 输出，A/B 两组字段同构）；
    delta_key 区分特征视角（be_retrace→a_delta_r / tp0_retrace→b_delta_r）。
    核心读数：n / decided / sum_delta_r（净改善）/ avg_real_r / avg_cf_r；
    胜率口径在此不适用（bucket 已是 saved/injured 语义而非 outcome）。

    bucket_key + judged_buckets 同时传入时启用判定白名单（docs/16 P-04）：
    decided / decided_sum_delta_r 只按白名单桶计——CARD-05 判定桶 = saved/injured，
    CARD-06 = protected/injured。no_arm/carry/untouched 的 ΔR 是记账口径 artifact
    （反事实按干净 -1.0R 记账 vs 真实含滑点费 -1.26R，线上首日 no_arm 每笔
    +0.264R 且 |Δ|>0.05 被 |delta| 阈值误计入 decided）或出场方式差异，
    不是规则效果；sum_delta_r 保持全量行合计（向后兼容/诊断用）。"""
    n = len(rows)
    if not n:
        return {"n": 0, "decided": 0, "sum_delta_r": 0.0,
                "decided_sum_delta_r": 0.0,
                "avg_real_r": None, "avg_cf_r": None}
    deltas = [float(r.get(delta_key, 0)) for r in rows]
    real = [float(r.get("real_r", 0)) for r in rows]
    cf = [real[i] + deltas[i] for i in range(n)]
    if judged_buckets is not None:
        decided_deltas = [
            d for d, r in zip(deltas, rows)
            if (r.get(bucket_key) or "unknown") in judged_buckets
        ]
    else:
        # decided = 反事实改变了结局的笔数（|delta|>0.05R，即 saved+injured 合计）
        decided_deltas = [d for d in deltas if abs(d) > 0.05]
    return {
        "n": n,
        "decided": len(decided_deltas),
        "sum_delta_r": round(sum(deltas), 3),
        # 净改善只按 decided 计（口径同上，phantom 残差不进判定读数）
        "decided_sum_delta_r": round(sum(decided_deltas), 3),
        "avg_real_r": round(sum(real) / n, 4),
        "avg_cf_r": round(sum(cf) / n, 4),
    }


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
    早期保护特征（be_retrace/tp0_retrace，trade_record 宿主，docs/15 §五）：
    跳过 outcome 归因链（其 bucket 本身即 saved/injured 语义），按反事实 R 统计。
    评估不回填 marks 行（避免双写漂移）。
    """
    from datetime import datetime, timedelta
    from sqlalchemy import select
    from app.models.scan import AIAnalysis, ScanResult, TradeReview
    from app.models.shadow import ShadowMark

    since = datetime.utcnow() - timedelta(days=window_days)
    out: list[dict] = []
    for key, feat in SHADOW_FEATURES.items():
        # 关闭的卡不进报告（docs/14 §六.1「对每个 enabled 特征」，2026-10-13）：
        # CARD-03/CARD-06 已裁决关闭，面板不再列出；裁决记录见 docs/14 §十一
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
                        "buckets": [], "baseline": {"n": 0, "counted": 0, "win_rate": None}})
            continue

        # ── 早期保护特征（trade_record 宿主）：反事实统计分支 ────────────
        if feat.card in ("CARD-05", "CARD-06"):
            # 分层口径（docs/15 §5.3）：正式网为主判定组（decided/sufficient 基数），
            # 测试网单独成组——两网人群结构差异大（测试网首轮 77 笔以赢家为主，
            # 全量混算会把赢家误伤成本错记进正式网口径），测试网只作环境差异证据
            prod_values: list = []
            testnet_values: list = []
            for m in marks:
                v = m.value_json or {}
                # 前向窗口过滤（2026-10-13 重置，同 CARD-09 的 closed_at 口径）：
                # 出场链 2026-10-12 起改恒 ladder，反事实的「真实 R」出处随之改变，
                # 之前平仓的存量只打标不计入评估（docs/14 §十一）
                if ((v.get("closed_at") or "")[:10]) < EARLY_PROTECT_START:
                    continue
                if (m.decision_json or {}).get("testnet"):
                    testnet_values.append(v)
                else:
                    prod_values.append(v)
            # 非机械出场剔除（docs/16 P-04）：manual/liquidation 的单，反事实
            # 重放按「机械原计划出场」（如 TP1 价）补全剩余腿，与真实出场方式
            # 无关——survived 桶 cf>real（+4.88 vs +3.57）即此类污染，正 ΔR
            # 不是规则效果。从判定集（正式网+测试网）剔除，计数单列供核对
            def _mechanical(v: dict) -> bool:
                return (v.get("exit_reason") or "") not in ("manual", "liquidation")
            excluded_n = sum(1 for v in prod_values if not _mechanical(v))
            prod_values = [v for v in prod_values if _mechanical(v)]
            testnet_values = [v for v in testnet_values if _mechanical(v)]
            delta_key = "a_delta_r" if key == "be_retrace" else "b_delta_r"
            bucket_key = "bucket" if key == "be_retrace" else "bucket_tp0"
            # 判定桶白名单（docs/16 P-04，预注册语义）：decided/净改善只数
            # 反事实真正改变结局的桶——saved/injured（A）/ protected/injured（B）；
            # no_arm/carry/untouched/below_gate 的 ΔR 是记账 artifact 或无变化
            judged = (("saved", "injured") if key == "be_retrace"
                      else ("protected", "injured"))
            baseline = _trade_protect_stats(
                prod_values, delta_key, bucket_key, judged)
            buckets: dict[str, list] = {}
            for v in prod_values:
                buckets.setdefault(v.get(bucket_key, "unknown"), []).append(v)
            bucket_out = []
            for b, rows in sorted(buckets.items()):
                st = _trade_protect_stats(rows, delta_key, bucket_key, judged)
                if st["n"] < 30:
                    st["insufficient"] = True
                bucket_out.append({"bucket": b, **st})
            t_buckets: dict[str, int] = {}
            for v in testnet_values:
                b = v.get(bucket_key, "unknown")
                t_buckets[b] = t_buckets.get(b, 0) + 1
            out.append({
                "feature": key, "name": feat.name, "card": feat.card,
                "min_sample": feat.min_sample,
                "window_days": window_days,
                "baseline": baseline,
                "buckets": bucket_out,
                # 非机械出场剔除笔数（manual/liquidation，反事实不可比）
                "excluded_n": excluded_n,
                # 对照组：测试网读数 + 桶分布计数，不进判定
                "testnet_baseline": {
                    **_trade_protect_stats(
                        testnet_values, delta_key, bucket_key, judged),
                    "buckets": dict(sorted(t_buckets.items())),
                },
                # 达标判定：预注册卡按正式网 decided 数（saved+injured），
                # carry/no_arm 与测试网样本不计入
                "decided": baseline.get("decided", 0),
                "sufficient": baseline.get("decided", 0) >= feat.min_sample,
            })
            continue

        # ── 再入场前置（CARD-07，docs/13 #12）：结构桶 × 续走率 ───────────
        # 第三种宿主语义（scan_result 归因链 / CARD-05/06 反事实之外）：
        # bucket = structure_ok/broken，读数 = 止损后 24h 同向续走条件概率。
        # 分层同 CARD-05/06：正式网主判定组，测试网对照组不进判定。
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
