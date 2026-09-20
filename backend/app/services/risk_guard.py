"""Risk Guard：AI 决策的确定性校验器（docs/04 §4 Stage 4 / §5.6）

校验链已收缩（2026-09-15 拍板：② AI 分析校验链仅保留两条——RR 铁律 + 双评委辩论复核）：
- Schema 强约束（Pydantic）+ suggest 基本项（方向/开单类型/三价为正）
- **RR 铁律**：按 AI 给的三价复算盈亏比 ≥ AI_RR_MIN(1.5)，声称值不信
- 仓位公式化（固定亏损法）：仓位 = 风险预算 ÷ 止损距离，触止损恰好亏
  RISK_BUDGET_PCT（3%），AI 不自报仓位

已移除的校验（方法论降级为 AI 提示词纪律，程序不再打回/改挂）：
- 入场偏离现价 ≤2%（2026-09-15 移除）
- 方向铁律入场贴近结构位 ±0.3%（2026-09-15 早些时候移除）
- 止损锚定（结构锚 vs 影线极值取更远者 + 0.3% 缓冲）、止损宽度 [0.3×ATR, 3×ATR]、
  强平安全上限前置、止盈最近优先强制（docs/09 的锚定设计保留为提示词方法论）

方向-价格一致性（2026-09-16 恢复为硬校验，不属于方法论）：多单 SL<entry<TP1、
空单 TP1<entry<SL（TP2>0 时同侧递延）。2026-09-15 随校验链收缩移除后，
ETHUSDT 空单反向止损（SL=2373 < 入场 2391.89）靠 abs() 口径的 RR 铁律
畅通落库，到挂 SL 才会被交易所拒单——这是可行性铁律而非结构偏好，不可省。

放水后的兜底在开仓闸门（trade_engine._attempt_open）：方向-价格复核、现价偏离
≤1%、RR 按现价复算 ≥1.5、强平闸门（liquidation_gate_pct，本模块提供共用口径）、
保证金可用性——交易所侧最后一道防线。
"""
import logging
from datetime import datetime, timedelta
from typing import Literal, Optional

from pydantic import BaseModel, ValidationError

from app.config import settings

logger = logging.getLogger(__name__)

# 开单类型（结构打法归类，docs/04 §4 Stage 3；skip 时为空）
# 2026-09-12 起三种破位/回踩类打法（123法则·N字结构·2B法则）合并为 structure_break
TRADE_TYPE_LABELS = {
    "trend_follow": "顺势交易",
    "structure_break": "结构破位回踩",
    "range_edge": "区间边缘反转",
}
TRADE_TYPES = tuple(TRADE_TYPE_LABELS)

# 合并前的历史类型（旧分析/旧交易快照仍可能携带，仅作展示，不再接受 AI 新输出）
LEGACY_TRADE_TYPE_LABELS = {
    "rule_123": "123法则（已并入结构破位回踩）",
    "n_structure": "N字结构（已并入结构破位回踩）",
    "rule_2b": "2B法则（已并入结构破位回踩）",
}


class TradeDecision(BaseModel):
    """AI 决策契约（P0：AI 报绝对价格；P1 升级为结构位锚点 level_ref+offset）"""

    trade_decision: Literal["suggest", "skip"]
    skip_reason: str = ""
    direction: Optional[Literal["long", "short"]] = None
    trade_type: Optional[str] = None  # 开单类型：trend_follow / structure_break / range_edge
    entry_price: float = 0.0
    stop_loss: float = 0.0
    take_profit_1: float = 0.0
    take_profit_2: float = 0.0
    risk_reward_ratio: float = 0.0
    position_pct: float = 0.0
    recommendation: float = 0.0
    analysis: str = ""


_NUM_FIELDS = (
    "entry_price", "stop_loss", "take_profit_1",
    "take_profit_2", "risk_reward_ratio", "position_pct", "recommendation",
)


def normalize_raw(raw: dict) -> TradeDecision:
    """宽松归一化：AI 可能输出字符串数字 / direction 空串等，先转成严格类型"""
    data = dict(raw or {})
    for f in _NUM_FIELDS:
        v = data.get(f)
        if isinstance(v, str):
            try:
                data[f] = float(v.strip())
            except ValueError:
                data[f] = 0.0
        elif v is None:
            data[f] = 0.0
    if data.get("direction") == "":
        data["direction"] = None
    if data.get("trade_decision") not in ("suggest", "skip"):
        raise ValueError(f"trade_decision 非法: {data.get('trade_decision')!r}")
    return TradeDecision(**data)


# 逐仓强平闸门口径（docs/07 §8-A4）：止损距离超过 1/杠杆 − 维持保证金率 时，
# 价格未到止损价保证金就先亏光 → 被强平。
# MMR 必须按币种实际值取（trade_engine 经 binance_trader.bracket_mmr 查
# /fapi/v1/leverageBracket）：BTC 0.4%、主流币 0.5%、小币第 1 档普遍 1.5%-2.5%。
# 写死 0.005 的旧口径放行了 4USDT（MMR=1.5%）止损 4.2% 的单子——实际跌 3.5%
# 即被强平，SL 形同虚设（2026-09-17 案例，实际亏损超风险预算 10%）。
# 缓冲 LIQ_GATE_BUFFER 覆盖：开平 taker 手续费（约 0.1% 名义）+ 持仓资金费
# （小币单笔可达 0.2%+ 名义，swing 持仓多日）+ 强平触发价差。
# trade_engine 开仓闸门（_attempt_open）使用此口径；risk_guard 侧的前置打回
# 已随"仅保留 RR 铁律"移除（2026-09-15），强平保护由开仓闸门承担
LIQ_GATE_MMR = 0.005  # mmr 未提供时的默认（主流币口径）
LIQ_GATE_MMR_FALLBACK = 0.025  # bracket API 拿不到时的保守兜底（覆盖小币高 MMR）
LIQ_GATE_BUFFER = 0.005

# 入场偏离容差（system_config.pullback_market_pct 可页面覆盖，NULL=默认 0.01）：
# 开仓时现价对 AI 入场价的最大偏离。2026-09-18 起一兼两职：
# ① 普通单的开仓漂移上限（原写死 1%）；② 待回踩单的市价入场判定——
# 回踩距离 ≤ 容差时市价进场（RR 按现价复算仍须 ≥AI_RR_MIN，见开仓闸门），
# 超容差才维持「待回踩不下单」。回踩不远时市价进也能满足盈亏比，不必干等
PULLBACK_MARKET_PCT = 0.01

# 开仓前盘口滑点实测上限（system_config.max_slippage_pct 可页面覆盖，NULL=默认 0.003）：
# 按本单数量走订单簿逐层吃的加权均价对最优价的偏离（0.003=0.3%）。GRIFFAIN 案例：
# 簿薄的小币吃单滑点 0.637% → 实际止损距离拉宽 17%、风险超预算。None 返回（簿异常）
# 不阻断，避免接口抖动锁死开单
MAX_SLIPPAGE_PCT = 0.003


def liquidation_gate_pct(
    leverage: Optional[int] = None,
    mmr: Optional[float] = None,
    buffer: Optional[float] = None,
) -> float:
    """逐仓强平闸门：止损距离占价格的最大安全比例（1/杠杆 − MMR − 缓冲）"""
    lev = leverage or settings.TRADING_LEVERAGE
    return 1 / lev - (LIQ_GATE_MMR if mmr is None else mmr) - (LIQ_GATE_BUFFER if buffer is None else buffer)


def is_pullback_wait(ai_result: dict, current_price) -> bool:
    """AI 建议等回踩判定（2026-09-15）：suggest 且入场价在现价的回踩侧——
    多单入场低于现价、空单高于现价（提示词契约：等回踩时 entry 锚定回踩结构位）。

    限价委托已放弃（docs/10），此类建议无法按 AI 计划价位成交——仅落
    "待回踩"标识供展示，程序不为其下单（即时开仓分发与开仓快速过滤都会跳过）。
    """
    if ai_result.get("trade_decision") != "suggest":
        return False
    direction = (ai_result.get("direction") or "").lower()
    try:
        entry, cur = float(ai_result.get("entry_price") or 0), float(current_price or 0)
    except (TypeError, ValueError):
        return False
    if direction not in ("long", "short") or entry <= 0 or cur <= 0:
        return False
    return entry < cur if direction == "long" else entry > cur


def validate_decision(
    d: TradeDecision, signal: dict, klines: list,
) -> tuple[bool, list[str], dict]:
    """校验并修正决策（2026-09-15 拍板收缩：仅保留 RR 铁律 + 仓位复算）。

    返回 (ok, violations, fixed_dict)；ok=False 时 fixed_dict 为空。
    ok=True 时 fixed_dict 中 rr / position_pct 为程序复算值，直接落库。
    signal / klines 保留入参以稳定调用方接口（当前校验不再使用）。
    """
    violations: list[str] = []

    # ── skip：只要求理由非空（recommendation 超标仅修正，不浪费重试）──
    if d.trade_decision == "skip":
        if not d.skip_reason.strip():
            violations.append("skip 时必须给出 skip_reason 理由")
            return False, violations, {}
        return True, [], {
            "trade_decision": "skip",
            "skip_reason": d.skip_reason.strip(),
            "direction": None,
            "trade_type": None,
            "analysis": d.analysis or "",
            "entry_price": 0, "stop_loss": 0, "take_profit_1": 0,
            "take_profit_2": 0, "risk_reward_ratio": 0,
            "position_pct": 0,
            "recommendation": min(d.recommendation, 30),
        }

    # ── suggest 基本项 ──
    if not d.direction:
        violations.append("suggest 时必须给出 direction (long/short)")
    if d.trade_type not in TRADE_TYPES:
        violations.append(
            f"suggest 时必须归类开单类型 trade_type（{'/'.join(TRADE_TYPES)} 之一）"
        )
    if d.entry_price <= 0 or d.stop_loss <= 0 or d.take_profit_1 <= 0:
        violations.append("suggest 时 entry/stop_loss/tp1 必须为正数价格")
    if violations:
        return False, violations, {}

    # ── 方向-价格一致性硬校验（2026-09-16 恢复为程序强制；ETHUSDT 案例）──
    # 多单 SL<entry<TP1、空单 TP1<entry<SL 是交易所可挂性铁律：反向止损的
    # 条件单会被拒单（-2021 would immediately trigger），走到开仓→挂SL失败→
    # 紧急平仓空转。RR 铁律全程 abs() 对方向盲，反向止损照样达标
    # （ETHUSDT 空单 SL=2373 低于入场 2391.89，RR 复算 1.58 畅通通过）。
    # TP2>0 时须与方向同侧且比 TP1 更远
    if d.direction == "long" and not d.stop_loss < d.entry_price < d.take_profit_1:
        violations.append(
            f"多单价格序矛盾：须 SL < 入场 < TP1"
            f"（实得 SL={d.stop_loss}，入场={d.entry_price}，TP1={d.take_profit_1}）"
        )
    if d.direction == "short" and not d.take_profit_1 < d.entry_price < d.stop_loss:
        violations.append(
            f"空单价格序矛盾：须 TP1 < 入场 < SL"
            f"（实得 TP1={d.take_profit_1}，入场={d.entry_price}，SL={d.stop_loss}）"
        )
    if d.take_profit_2 > 0:
        if d.direction == "long" and d.take_profit_2 <= d.take_profit_1:
            violations.append(
                f"多单 TP2 须比 TP1 更远（TP2={d.take_profit_2} ≤ TP1={d.take_profit_1}）"
            )
        if d.direction == "short" and d.take_profit_2 >= d.take_profit_1:
            violations.append(
                f"空单 TP2 须比 TP1 更远（TP2={d.take_profit_2} ≥ TP1={d.take_profit_1}）"
            )
    if violations:
        return False, violations, {}

    stop_dist = abs(d.entry_price - d.stop_loss)
    stop_pct = stop_dist / d.entry_price if d.entry_price else 0

    # ── RR 铁律（唯一保留的价格校验）：按 AI 三价复算，声称值不信 ──
    rr = 0.0
    if stop_dist > 0:
        rr = abs(d.take_profit_1 - d.entry_price) / stop_dist
        if rr < settings.AI_RR_MIN:
            violations.append(
                f"盈亏比复算 {rr:.2f} < {settings.AI_RR_MIN}（交易系统铁律）"
            )
    if violations:
        return False, violations, {}

    # ── 修正：程序复算值落库，AI 声称值不信 ──
    # 固定亏损仓位法：仓位（名义价值占账户%）= 风险预算 ÷ 止损距离，
    # 触发止损时账户恰好亏损 RISK_BUDGET_PCT（3%）。止损越远仓位越小，不设 clamp。
    # 例：止损距离 2% → 仓位 150%（10×杠杆下占用保证金 15%）
    position_pct = 0.0
    if stop_pct > 0:
        position_pct = round(settings.RISK_BUDGET_PCT / stop_pct, 2)
    fixed = {
        "trade_decision": "suggest",
        "skip_reason": "",
        "direction": d.direction,
        "trade_type": d.trade_type,
        "analysis": d.analysis or "",
        "entry_price": round(d.entry_price, 8),
        "stop_loss": round(d.stop_loss, 8),
        "take_profit_1": round(d.take_profit_1, 8),
        "take_profit_2": round(d.take_profit_2, 8),
        "risk_reward_ratio": round(rr, 2),
        "position_pct": position_pct,
        "recommendation": float(min(max(d.recommendation, 50), 100)),
    }
    return True, [], fixed


def build_forced_skip(reason: str, violations: Optional[list[str]] = None) -> dict:
    """校验/重试耗尽后的强制 skip 结果（保证任何路径都有确定结论落库）"""
    detail = ""
    if violations:
        detail = "；".join(f"{i+1}. {v}" for i, v in enumerate(violations[:3]))
    skip_reason = f"{reason}" + (f"（{detail}）" if detail else "")
    return {
        "trade_decision": "skip",
        "skip_reason": skip_reason[:500],
        "direction": None,
        "analysis": "1. 该信号未通过程序风控校验，已自动跳过，未消耗人工判断成本。",
        "entry_price": 0, "stop_loss": 0, "take_profit_1": 0,
        "take_profit_2": 0, "risk_reward_ratio": 0,
        "position_pct": 0, "recommendation": 10,
    }


def parse_and_validate(raw: dict, signal: dict, klines: list) -> tuple[bool, list[str], dict]:
    """normalize + pydantic 校验 + 业务校验 的一步入口"""
    try:
        d = normalize_raw(raw)
    except (ValidationError, ValueError) as e:
        return False, [f"输出格式不合法: {str(e)[:200]}"], {}
    return validate_decision(d, signal, klines)


# ═════════════════════════════════════════════════════════════════════
# 市场风险防护（docs/15，Market Guard）：信号采集 + 分级状态机
#
# 快检直连币安合约公开接口（3 个请求），不走 ExchangePool K线缓存——
# 缓存为小时级扫描设计，复用会让快检拿到最长 1 小时前的旧数据（docs/15 §4）。
# 分级规则（§4/§5）：
#   命中数 = S1(BTC 24h ≤ -5%) + S2(广度 ≥70%) + S3(ATR 放大 ≥1.8x)
#   L3 = 命中数≥3 或 S1≤-8% 或 (S4≤-6% 且 BTC 24h≤-3%)
#   L2 = 命中数≥2 或 S4≤-3.5%
#   L1 = 命中数≥1
# 防抖：S1/S2/S3 升级需连续 GUARD_CONFIRM_ROUNDS 轮；S4 即时；
#   降级需连续 GUARD_DOWNGRADE_ROUNDS 轮低于当前等级且逐级；
#   S4 迟滞：触发 -3.5% / 解除 -1.5%；L2 震荡锁 1 小时。
# ═════════════════════════════════════════════════════════════════════

_EXCLUDE = ("UP", "DOWN", "BEAR", "BULL", "HALF", "SHORT", "LONG")


# ── 信号采集（直连，无缓存）──

def fetch_signals() -> Optional[dict]:
    """拉取四信号快照。任一请求失败返回 None（调用方据此冻结等级）。

    返回 {s1, s2, s3, s4, s1_hit, s2_hit, s3_hit, s4_hit, s4_l3_ok, ts}
    """
    from app.services.binance_client import BinanceClient
    client = BinanceClient()
    try:
        tickers = client.get_24h_tickers()
        btc_1h = client.get_klines("BTCUSDT", "1h", 40)
        btc_15m = client.get_klines("BTCUSDT", "15m", 10)
    except Exception as e:
        logger.warning("风险快检行情获取失败（等级冻结）: %s", e)
        return None
    finally:
        client.close()

    btc = next((t for t in tickers if t.get("symbol") == "BTCUSDT"), None)
    if not btc or not btc_1h or not btc_15m:
        logger.warning("风险快检数据不完整（等级冻结）")
        return None

    s1 = float(btc.get("priceChangePercent") or 0)

    # S2 广度：24h 成交额前 N 的 USDT 合约中跌幅达标占比
    pool = [
        t for t in tickers
        if t.get("symbol", "").endswith("USDT")
        and not any(k in t["symbol"] for k in _EXCLUDE)
    ]
    pool.sort(key=lambda t: float(t.get("quoteVolume") or 0), reverse=True)
    top = pool[: settings.GUARD_TOP_N]
    s2 = (
        sum(1 for t in top if float(t.get("priceChangePercent") or 0) <= settings.GUARD_S2_PCT)
        / len(top) * 100
    ) if top else 0.0

    # S3：已收盘 1h K线的 ATR14（近 14 根 vs 24h 前的 14 根）
    trs = _closed_trs(btc_1h)
    s3 = _atr(trs[-14:]) / _atr(trs[-38:-24]) if len(trs) >= 38 else 0.0

    # S4：最近 3 根已收盘 15m 累计涨跌幅（%）——只用已收盘K线，免疫插针
    closed15 = btc_15m[:-1]  # 最后一根未收盘，剔除
    if len(closed15) >= 3:
        open_px = float(closed15[-3][1])
        close_px = float(closed15[-1][4])
        s4 = (close_px / open_px - 1) * 100 if open_px > 0 else 0.0
    else:
        s4 = 0.0

    s4_hit = s4 <= settings.GUARD_S4_L2_PCT
    return {
        "s1": round(s1, 2), "s2": round(s2, 1), "s3": round(s3, 2), "s4": round(s4, 2),
        "s1_hit": s1 <= settings.GUARD_S1_PCT,
        "s2_hit": s2 >= settings.GUARD_S2_BREADTH,
        "s3_hit": s3 >= settings.GUARD_S3_MULT,
        "s4_hit": s4_hit,
        "s4_l3_ok": s4 <= settings.GUARD_S4_L3_PCT and s1 <= settings.GUARD_S4_L3_CONFIRM_24H,
        "ts": datetime.utcnow().isoformat(),
    }


def _closed_trs(klines: list[list]) -> list[float]:
    """已收盘K线的真实波幅 TR 序列（剔除首尾：首根缺前收盘、末根未收盘）"""
    trs = []
    for i in range(1, len(klines) - 1):
        h, l = float(klines[i][2]), float(klines[i][3])
        pc = float(klines[i - 1][4])
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    return trs


def _atr(trs: list[float]) -> float:
    return sum(trs) / len(trs) if trs else 0.0


# ── 分级状态机（纯函数，可离线验证）──

def evaluate_level(sig: dict, st: dict) -> dict:
    """按信号与上一状态计算新状态。st 为 MarketRiskState 行的 dict 化快照。

    st 键：level, cause, pending_level, pending_count, below_count,
           s4_last_release_at(iso|None), osc_lock_until(iso|None)
    返回新 st（含 level/cause/各计数），只算不存。
    """
    now = datetime.utcnow()
    st = {**st}
    level = int(st.get("level") or 0)
    hits = sum(1 for k in ("s1_hit", "s2_hit", "s3_hit") if sig.get(k))
    s4_raw = float(sig.get("s4") or 0.0)
    lock_active = bool(
        st.get("osc_lock_until")
        and datetime.fromisoformat(st["osc_lock_until"]) > now
    )

    # S4 迟滞：s4 驱动期间，跌幅浅于解除阈值才视为消失
    s4_active = s4_raw <= settings.GUARD_S4_L2_PCT or (
        st.get("cause") == "s4" and s4_raw <= settings.GUARD_S4_RELEASE_PCT
    )

    # 目标等级（原始口径）
    l3 = (
        hits >= 3
        or (sig.get("s1") is not None and sig["s1"] <= settings.GUARD_S1_L3_PCT)
        or (s4_raw <= settings.GUARD_S4_L3_PCT
            and (sig.get("s1") or 0) <= settings.GUARD_S4_L3_CONFIRM_24H)
    )
    raw = 3 if l3 else 2 if (hits >= 2 or s4_active) else 1 if hits >= 1 else 0

    if raw > level:
        immediate = raw == 3 or s4_active  # S4/L3 即时；纯 S1/S2/S3 需确认
        if immediate:
            # 震荡锁内同因再触发：锁 L2、走常规降级（cause=normal，不再快速衰减）
            if raw == 2 and s4_active and lock_active:
                st.update(level=2, cause="normal", below_count=0, pending_level=0,
                          pending_count=0, entered_level_at=now.isoformat(),
                          osc_lock_until=(now + timedelta(seconds=settings.GUARD_OSC_LOCK_S)).isoformat())
                return st
            st.update(level=raw, cause="s4" if raw == 2 else "normal",
                      below_count=0, pending_level=0, pending_count=0,
                      entered_level_at=now.isoformat())
            return st
        # S1/S2/S3 升级确认（防窗口滚动/成分变化单轮跳变）
        if int(st.get("pending_level") or 0) == raw:
            cnt = int(st.get("pending_count") or 0) + 1
            if cnt >= settings.GUARD_CONFIRM_ROUNDS:
                st.update(level=raw, cause="normal", below_count=0,
                          pending_level=0, pending_count=0, entered_level_at=now.isoformat())
            else:
                st.update(pending_level=raw, pending_count=cnt)
        else:
            st.update(pending_level=raw, pending_count=1)
        return st

    if raw < level:
        # s4 单驱等级（未锁定）：信号消失即按常规命中重算（快速衰减），并设震荡锁窗口
        if st.get("cause") == "s4" and not s4_active and not lock_active:
            st.update(level=raw, cause="normal", below_count=0, pending_level=0,
                      pending_count=0, entered_level_at=now.isoformat(),
                      osc_lock_until=(now + timedelta(seconds=settings.GUARD_OSC_LOCK_S)).isoformat())
            return st
        # 常规降级：连续 N 轮、逐级（棘轮，max 保证一次最多降一级）
        cnt = int(st.get("below_count") or 0) + 1
        st["below_count"] = cnt
        if cnt >= settings.GUARD_DOWNGRADE_ROUNDS:
            st.update(level=max(raw, level - 1), cause="normal", below_count=0,
                      pending_level=0, pending_count=0, entered_level_at=now.isoformat())
        return st

    # 持平：清计数
    st.update(pending_level=0, pending_count=0, below_count=0)
    return st


def downgrade_one_level(st: dict) -> dict:
    """外置降级入口（预留）：降一级并清 cause。"""
    now = datetime.utcnow()
    return {**st, "level": max(0, int(st.get("level") or 0) - 1), "cause": "normal",
            "below_count": 0, "pending_level": 0, "pending_count": 0,
            "entered_level_at": now.isoformat()}


# ── 持久化与执行（docs/15 Task 3）──

def _load_state(db) -> tuple["MarketRiskState", dict]:
    from app.models.market_risk import MarketRiskState
    row = db.get(MarketRiskState, 1)
    if row is None:
        row = MarketRiskState(id=1, level=0, cause="normal")
        db.add(row)
        db.flush()
    st = {
        "level": row.level, "cause": row.cause,
        "pending_level": row.pending_level, "pending_count": row.pending_count,
        "below_count": row.below_count,
        "s4_last_release_at": row.s4_last_release_at.isoformat() if row.s4_last_release_at else None,
        "osc_lock_until": row.osc_lock_until.isoformat() if row.osc_lock_until else None,
        "entered_level_at": row.entered_level_at.isoformat() if row.entered_level_at else None,
        "actions_pending": bool(row.actions_pending),
    }
    return row, st


def _save_state(db, row, st: dict, sig: Optional[dict], old_level: int) -> None:
    from app.models.market_risk import MarketRiskHistory
    row.level = st["level"]
    row.cause = st["cause"]
    row.pending_level = st["pending_level"]
    row.pending_count = st["pending_count"]
    row.below_count = st["below_count"]
    row.s4_last_release_at = datetime.fromisoformat(st["s4_last_release_at"]) if st.get("s4_last_release_at") else None
    row.osc_lock_until = datetime.fromisoformat(st["osc_lock_until"]) if st.get("osc_lock_until") else None
    row.entered_level_at = datetime.fromisoformat(st["entered_level_at"]) if st.get("entered_level_at") else None
    row.actions_pending = bool(st.get("actions_pending"))
    if sig:
        row.signals = {k: sig[k] for k in ("s1", "s2", "s3", "s4") if k in sig}
        row.updated_at = datetime.utcnow()
    if st["level"] != old_level:
        db.add(MarketRiskHistory(from_level=old_level, to_level=st["level"], trigger={
            "reason": f"{'升级' if st['level'] > old_level else '降级'} L{old_level}→L{st['level']}",
            **(row.signals or {}),
        }))


def get_current_level(db) -> int:
    """开仓闸门读取（数据失败冻结即：表里是什么就是什么）"""
    from app.models.market_risk import MarketRiskState
    row = db.get(MarketRiskState, 1)
    return int(row.level) if row else 0


def execute_level_actions(db, trader, level: int) -> dict:
    """L2/L3 存量仓位动作（仅本网）。L2：盈利方向 SL 收成本；L3：深亏(>0.5R)多单市价离场、
    盈利单 SL 再收紧一档（entry 与现价中点，锁约一半浮盈）。返回动作计数供通知，
    failed>0 时由调用方置 actions_pending 下轮重试。"""
    from sqlalchemy import select as _select
    from app.models.trade import TradeRecord
    recs = db.execute(_select(TradeRecord).where(
        TradeRecord.status.in_(("OPENED", "TP1_HIT", "TP2_HIT")),
        TradeRecord.testnet == settings.TRADING_TESTNET,
    )).scalars().all()
    if not recs:
        return {"sl_moved": 0, "closed": 0, "failed": 0}
    # 现价一次拉全（BinanceTrader 无公开行情方法，走 BinanceClient 24hr ticker）
    from app.services.binance_client import BinanceClient
    _c = BinanceClient()
    try:
        px = {t["symbol"]: float(t.get("lastPrice") or 0) for t in _c.get_24h_tickers()}
    finally:
        _c.close()
    # 交易所实际剩余仓位一次拉全：TP1/TP2 分批后 rec.qty（开仓原始量）大于剩余量，
    # 按原始量发 reduceOnly 市价单会被币安 -2022 拒单
    positions = trader.positions()
    moved = closed = failed = 0
    for rec in recs:
        try:
            price = px.get(rec.symbol, 0.0)
            entry = float(rec.entry_price or 0)
            if entry <= 0:
                continue
            amt = abs(positions[rec.symbol]["amt"]) if rec.symbol in positions else 0.0
            if amt < 1e-9:  # 已无实际持仓（结算/手动平仓未同步等），跳过
                continue
            risk = float(rec.risk_amount or 0)
            raw = dict(rec.raw or {})
            if level >= 2:
                # 盈利方向：多单 price>entry / 空单 price<entry → SL 收紧
                in_profit = price > entry if rec.direction == "long" else price < entry
                if in_profit:
                    from app.services.trade_engine import _replace_sl
                    # 只收紧不放宽：L2 收至成本；L3 再收紧一档至 entry 与现价中点。
                    # 多单 SL 已被跟进止损移到目标上方（空单反之）时不回退
                    target = entry if level == 2 else (entry + price) / 2
                    cur_sl = float(rec.stop_loss or 0)
                    need_tighten = cur_sl < target if rec.direction == "long" \
                        else (cur_sl == 0 or cur_sl > target)
                    if need_tighten:
                        open_ids = trader.open_order_ids(rec.symbol)
                        _replace_sl(db, trader, rec, open_ids, target,
                                    reason=f"market_guard_l{level}")
                        moved += 1
                # L3 深亏多单市价离场（空单不平，docs/15 §3）
                if level >= 3 and rec.direction == "long" and price < entry and risk > 0:
                    loss_r = (entry - price) * amt / risk
                    if loss_r > 0.5:
                        trader.cancel_all_algo(rec.symbol)
                        trader.market_order(rec.symbol, "SELL", amt, reduce_only=True)
                        raw["guard_closed"] = True
                        rec.raw = raw  # 新 dict，确保 SQLAlchemy 落库
                        _add_guard_event(db, rec, {"loss_r": round(loss_r, 2), "price": price})
                        closed += 1
            db.commit()
        except Exception as e:
            db.rollback()
            failed += 1
            logger.exception("防护动作失败 %s: %s", rec.symbol, e)
    return {"sl_moved": moved, "closed": closed, "failed": failed}


def _add_guard_event(db, rec, detail: dict) -> None:
    from app.services.trade_engine import _add_event
    _add_event(db, rec, "GUARD_CLOSE", detail)


def run_risk_check() -> dict:
    """快检主入口：采集→评估→持久化→升级即时执行→通知。数据失败冻结。

    仓位动作在 _save_state/commit 之前完成（execute 内部自会 commit 其行变更）；
    动作失败（failed>0）置 actions_pending，下轮快检即便等级未变也重试；
    trader 未配置时同样置位，等配置后补执行。
    """
    from app.database import SessionLocal
    from app.services.binance_trader import BinanceTrader
    from app.services.notify import send_feishu_text
    sig = fetch_signals()
    result = {"level": 0, "changed": False, "upgraded": False, "actions": None,
              "frozen": sig is None}
    if sig is None:
        logger.info("快检数据失败，等级冻结")
        return result
    db = SessionLocal()
    try:
        row, st = _load_state(db)
        old_level = st["level"]
        st = evaluate_level(sig, st)
        upgraded = st["level"] > old_level
        result["upgraded"] = upgraded
        need = st["level"] >= 2 and (upgraded or st.get("actions_pending"))
        if need:
            trader = BinanceTrader()
            if trader.configured:
                result["actions"] = execute_level_actions(db, trader, st["level"])
                st["actions_pending"] = result["actions"].get("failed", 0) > 0
            else:
                st["actions_pending"] = True  # 等配置后下轮重试
        elif st["level"] < 2:
            st["actions_pending"] = False
        _save_state(db, row, st, sig, old_level)
        db.commit()
        result["level"] = st["level"]
        if upgraded:
            result["changed"] = True
            send_feishu_text(
                f"⚠️ 市场风险升级 L{old_level}→L{st['level']}\n"
                f"BTC 24h {sig['s1']}% | 广度 {sig['s2']}% | ATR {sig['s3']}x | 3×15m {sig['s4']}%\n"
                f"动作：{'开仓暂停' + (str(result['actions']) if result['actions'] else '')}"
            )
        elif st["level"] < old_level:
            result["changed"] = True
            send_feishu_text(f"✅ 市场风险降级 L{old_level}→L{st['level']}（防抖确认后）")
        logger.info("风险快检：L%s（s1=%s s2=%s s3=%s s4=%s）",
                    st["level"], sig["s1"], sig["s2"], sig["s3"], sig["s4"])
        return result
    finally:
        db.close()
