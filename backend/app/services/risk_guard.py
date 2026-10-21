"""Risk Guard：AI 决策的确定性校验器（docs/04 §4 Stage 4 / §5.6）

校验链（2026-09-15 收缩为 RR 铁律 + 双评委辩论复核；2026-10-12 增止损宽度下限与
止盈一距离上限）：
- Schema 强约束（Pydantic）+ suggest 基本项（方向/开单类型/三价为正）
- **RR 铁律**：按 AI 给的三价复算盈亏比 ≥ AI_RR_MIN(1.5) − AI_RR_TOL(0.02)，
  声称值不信（2026-10-14：容差带从开仓现价闸扩到 AI 三价闸，吞 tick 取整噪音）
- **止损宽度下限**（2026-10-12-2）：止损距入场 ≥ SL_MIN_ATR×ATR14（默认 0.3），
  防"止损过近"被噪音扫掉并抑制 R 放大；近锚（突破类 stop_anchor=near）豁免
  （其止损刻意贴金K窗口极值，另有敞口折价与 ±0.5% 一致性校验兜底）
- **止盈一距离上限**（2026-10-12）：第一档止盈距入场 ≤
  min(TP1_MAX_ATR×ATR14, TP1_MAX_R×止损距离)、且不低于 rr_min×止损距离
  （防"第一档"落到 3~4R 的远位；超限打回让 AI 改近档或走兜底，程序不改价）
- **止盈二贴档保护**（2026-10-13-9）：TP2 与 TP1 间距 d < gap_min
  （=max(TP2_MIN_GAP_ATR×ATR14, TP2_MIN_GAP_R×止损距离, 2×TAKER_FEE_RATE×入场价)）
  或反向/重合时**静默撤档**（take_profit_2=0，不进 violations、不触发 Reflection）
- 仓位公式化（固定亏损法）：仓位 = 风险预算 ÷ 止损距离。风险预算两种口径
  （2026-09-27 止损金额模式，resolve 见 risk_budget.py）：pct=触止损恰好亏
  RISK_BUDGET_PCT（3%，默认）；fixed=亏固定 USDT 实额（position_pct 不折算），
  AI 不自报仓位

已移除的校验（方法论降级为 AI 提示词纪律，程序不再打回/改挂）：
- 入场偏离现价 ≤2%（2026-09-15 移除）
- 方向铁律入场贴近结构位 ±0.3%（2026-09-15 早些时候移除）
- 止损锚定（结构锚 vs 影线极值取更远者 + 0.3% 缓冲）、止损宽度 [0.3×ATR, 3×ATR]、
  强平安全上限前置、止盈最近优先强制（docs/09 的锚定设计保留为提示词方法论）
  ——2026-10-12-2 部分回退：恢复止损宽度**下限**（0.3×ATR，近锚豁免），上限仍
  走 signal_gate.max_stop_pct + 动态杠杆
- TP2 须比 TP1 更远（2026-10-13-9 移除硬校验：改由「止盈二贴档保护」静默撤档，
  不再打回浪费 Reflection 重试）

方向-价格一致性（2026-09-16 恢复为硬校验，不属于方法论）：多单 SL<entry<TP1、
空单 TP1<entry<SL（TP2 的方向序不再硬校验，见止盈二贴档保护）。2026-09-15 随校验链收缩移除后，
ETHUSDT 空单反向止损（SL=2373 < 入场 2391.89）靠 abs() 口径的 RR 铁律
畅通落库，到挂 SL 才会被交易所拒单——这是可行性铁律而非结构偏好，不可省。

放水后的兜底在开仓闸门（trade_engine._attempt_open）：方向-价格复核、现价偏离
≤1%、RR 按现价复算 ≥1.5、强平闸门（liquidation_gate_pct，本模块提供共用口径）、
保证金可用性——交易所侧最后一道防线。
"""
import logging
from typing import Literal, Optional

import numpy as np
from pydantic import BaseModel, ValidationError

from app.config import settings
from app.services.strategy.technical import atr_series

logger = logging.getLogger(__name__)

# 开单类型（结构打法归类，docs/04 §4 Stage 3；skip 时为空）
# 2026-09-12 起破位/回踩类打法（123法则·N字结构）合并为 structure_break；
# 2026-10-20 下线 range_edge——区间市在筛选层（gate_4h/range_score）已被前置剔除、
# 不产生候选，该类型不再作为合法输出（仅历史快照展示，见 LEGACY_*）
TRADE_TYPE_LABELS = {
    "trend_follow": "顺势交易",
    "structure_break": "结构破位回踩",
}
TRADE_TYPES = tuple(TRADE_TYPE_LABELS)

# 评分档位（2026-10-21 第二阶段 T2，方案 A）：提示词只允许 60/70/85 三档，
# 程序不再接受连续分——归一到最近档位，原始分另存 recommendation_raw 供校准。
RECOMMENDATION_TIERS = (60.0, 70.0, 85.0)
SKIP_RECOMMENDATION_CAP = 30.0
# 归一门槛：≥50 才向 60 档靠拢（58→60 是审计确认的典型场景）；更低分保持原值，
# 避免缺省 0/低分被放大成可开单的 60 档。
# 2026-10-21 补口：suggest 但原始分 < 门槛（30~50 连续分，及 ≤30 的越界低分）
# 在 validate_decision 里显式改判 skip——否则低分会以 suggest 落库，再被静态
# 门槛静默拦掉，成为审计 5.2 所说的「隐藏弃单」。
RECOMMENDATION_SNAP_FLOOR = 50.0


def snap_recommendation(raw) -> tuple[float, float]:
    """连续分 → (最近档位, 原始分)。越界先钳制 0~100；非数值按 0 处理。"""
    try:
        value = float(raw)
    except (TypeError, ValueError):
        value = 0.0
    value = round(min(max(value, 0.0), 100.0), 2)
    # 低于归一门槛不向上归一：避免缺省/低分（0、30 等）被放大成可开单的 60 档
    if value < RECOMMENDATION_SNAP_FLOOR:
        return value, value
    tier = min(RECOMMENDATION_TIERS, key=lambda t: (abs(value - t), t))
    return tier, value


# 合并/下线前的历史类型（旧分析/旧交易快照仍可能携带，仅作展示，不再接受 AI 新输出）
LEGACY_TRADE_TYPE_LABELS = {
    "rule_123": "123法则（已并入结构破位回踩）",
    "n_structure": "N字结构（已并入结构破位回踩）",
    "rule_2b": "2B法则（已并入结构破位回踩）",
    "range_edge": "区间边缘反转（已下线：区间市在筛选层被剔除）",
}


class TradeDecision(BaseModel):
    """AI 决策契约（P0：AI 报绝对价格；P1 升级为结构位锚点 level_ref+offset）"""

    trade_decision: Literal["suggest", "skip"]
    skip_reason: str = ""
    direction: Optional[Literal["long", "short"]] = None
    trade_type: Optional[str] = None  # 开单类型：trend_follow / structure_break
    # 止损锚类型（docs/23，仅突破类信号需要）：structure=结构锚（默认）/ near=金K窗口极值近锚
    stop_anchor: Optional[Literal["structure", "near"]] = None
    # 战法命中标注（2026-10-02，仅借鉴不影响结论）：tactic_name 必须逐字取自
    # 本次注入的战法清单（validate_decision 白名单闸门，清单外清空防幻觉）
    tactic_name: Optional[str] = None
    tactic_reason: Optional[str] = None
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
    # stop_anchor 非法值（含 prompt 教的空串）一律回退 None（suggest 落库时再回退
    # structure；docs/23）。此前白名单误把 "" 当合法值原样放行 → 撞 Literal 校验
    # 「输出格式不合法」白烧一次 Reflection 重试（2026-10-04）
    if data.get("stop_anchor") not in ("structure", "near"):
        data["stop_anchor"] = None
    # 战法字段空串归 None（未命中）；白名单校验在 validate_decision（需注入清单）
    for _tf in ("tactic_name", "tactic_reason"):
        if not str(data.get(_tf) or "").strip():
            data[_tf] = None
        else:
            data[_tf] = str(data[_tf]).strip()[:64 if _tf == "tactic_name" else 255]
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

# 盈亏比复算容差（system_config.ai_rr_tol 可页面覆盖，NULL=settings.AI_RR_TOL=0.02），
# 同一条容差带覆盖三处口径（2026-10-14 起，此前仅开仓现价闸生效）：
# ① 开仓闸现价复算 RR ≥ rr_min - tol（容忍 AI 报价到成交间漂移，CYBERUSDT
#   1.4998 误杀个案）；② AI 三价 RR 铁律同带（容忍交易所 tick 取整——兜底 1.5R
#   价取整后普遍落 1.49~1.51R，10-14 实测零容差白烧过半 Reflection 重试）；
# ③ TP1 距离上限同带（R 单位绝对容差：tp1_R ≤ cap_R + tol，1.50R 擦边放行、
#   1.55R 真·过远仍拦）。0=严格旧口径
AI_RR_TOL = 0.02

# 近锚一致性校验（docs/23，2026-09-29；2026-10-12-5 起扩展至全信号类型——距离分档
# 降级金K锚后非突破类也可合法落 near，见止损纪律距离分档条款；2026-10-12-8 恢复
# 窗口口径）：AI 声明 stop_anchor=near 时，止损必须贴近金K窗口极值（最近 5 根
# 已收盘K线共同最低/最高价，klines[-6:-1]；klines[-1] 未收盘不参与）——
# 提示词要求极值外 0.3% 缓冲，容差 0.5% 覆盖合理偏移；AI 挂近锚名义报远离止损
# （把结构锚止损伪装成近锚、骗取 ×0.7 风险折价并污染复盘分组）时程序打回。
# 数据不足时放行（防数据异常锁死开单，同 MAX_SLIPPAGE 接口抖动不阻断的哲学），仅记 warning
# 尾窗豁免（2026-10-12）：扫描尾窗（:55 起）信号根是未走完的近收盘根快照，其极值
# 未定型、最后数分钟仍可能扩展——此时「贴近已定型极值」无从校验，按未定型极值
# 打回会误杀且会把止损压在会被影线扫掉的价位，故 signal_bar_provisional 时跳过，
# 仅记 warning（signal 上的标记由扫描/AI 事实包侧在尾窗内打上）
NEAR_ANCHOR_BUFFER = 0.003  # 与提示词口径一致：极值外 0.3%
NEAR_ANCHOR_TOLERANCE = 0.005  # 止损对锚价的最大相对偏离
BREAKOUT_SIGNAL_TYPES = ("trigger_breakout", "trigger_trend_breakout")

# 开仓前盘口滑点实测上限（system_config.max_slippage_pct 可页面覆盖，NULL=默认 0.003）：
# 按本单数量走订单簿逐层吃的加权均价对最优价的偏离（0.003=0.3%）。GRIFFAIN 案例：
# 簿薄的小币吃单滑点 0.637% → 实际止损距离拉宽 17%、风险超预算。None 返回（簿异常）
# 不阻断，避免接口抖动锁死开单
MAX_SLIPPAGE_PCT = 0.003


def liquidation_gate_pct(
    leverage: Optional[int] = None,
    mmr: Optional[float] = None,
    buffer: Optional[float] = None,
    lev_cap: Optional[int] = None,
) -> float:
    """逐仓强平闸门：止损距离占价格的最大安全比例（1/杠杆 − MMR − 缓冲）。
    lev_cap（2026-10-02 上页面）：leverage 缺省时的回退上限（cfg.leverage_max
    ?? settings.TRADING_LEVERAGE，由调用方解析传入）"""
    lev = leverage or lev_cap or settings.TRADING_LEVERAGE
    return 1 / lev - (LIQ_GATE_MMR if mmr is None else mmr) - (LIQ_GATE_BUFFER if buffer is None else buffer)


def is_pullback_wait(ai_result: dict, current_price) -> bool:
    """AI 建议等回踩判定（2026-09-15）：suggest 且入场价在现价的回踩侧——
    多单入场低于现价、空单高于现价（2026-09-30 提示词已禁回踩侧入场价，
    本判定保留作兜底：模型未遵守时照样标记、不下单）。

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


def _tactic_gate(d: TradeDecision, signal: dict) -> tuple[Optional[str], Optional[str]]:
    """战法白名单闸门（2026-10-02）：tactic_name 不在本次注入清单（signal 携带的
    tactic_names，调用方从活动战法装载）→ 清空——LLM 编造/改写战法名直接丢弃，
    不落库（复盘分组按真实清单聚合，防脏键）。reason 与 name 同进同出。"""
    allowed = signal.get("tactic_names") or []
    if d.tactic_name and d.tactic_name in allowed:
        return d.tactic_name, d.tactic_reason
    if d.tactic_name:
        logger.info("战法名未命中白名单，已清空: %r（清单 %s）", d.tactic_name, allowed)
    return None, None


def validate_decision(
    d: TradeDecision, signal: dict, klines: list,
    risk_budget: Optional[float] = None,
    rr_min: Optional[float] = None,
    risk_pct: Optional[float] = None,
    rr_tol: Optional[float] = None,
) -> tuple[bool, list[str], dict]:
    """校验并修正决策（2026-09-15 拍板收缩：仅保留 RR 铁律 + 仓位复算）。

    返回 (ok, violations, fixed_dict)；ok=False 时 fixed_dict 为空。
    ok=True 时 fixed_dict 中 rr / position_pct 为程序复算值，直接落库。
    signal / klines 保留入参以稳定调用方接口；klines 自 2026-10-12 起用于
    止盈一距离上限（据 ATR14 算 ATR 腿）。
    risk_budget（2026-09-27 止损金额模式）：fixed 模式下为单笔固定亏损 USDT
    实额（调用方从 system_config.risk_fixed_amount 传入），此时 position_pct
    置 None（仓位%依赖钱包，分析期不可得）；None 时按 risk_pct 百分比口径
    复算（pct 模式，现行默认）。
    rr_min / risk_pct（2026-10-02 上页面）：盈亏比铁律下限与单笔风险比例，
    调用方从 system_config 解析传入（cfg 列 ?? env 默认）；None 回落 settings。
    rr_tol（2026-10-14）：RR 铁律与 TP1 上限共用的 R 单位容差（cfg.ai_rr_tol
    ?? settings.AI_RR_TOL，默认 0.02；0=严格），吞 tick 取整擦边、不放真过远。
    2026-10-12：策略二（EMA21 金K跟踪）与 trail 纯跟踪出场链整体删除，本校验器
    不再有 s2 资格闸/优先级改标——出场恒为 ladder，RR 铁律无豁免。
    """
    violations: list[str] = []

    # ── 评分硬校验（2026-10-21 第二阶段补口）：suggest 的原始分低于归一门槛时
    # 显式改判 skip，而不是保留连续低分让下游静态门槛静默拦截。覆盖 30~50 的
    # 中间分与 ≤30 的越界低分；原始分保留在 recommendation_raw 供校准统计。
    if d.trade_decision == "suggest":
        _raw_score = float(d.recommendation or 0)
        if _raw_score < RECOMMENDATION_SNAP_FLOOR:
            return True, [], {
                "trade_decision": "skip",
                "skip_reason": (
                    f"[评分不足] 原始分 {_raw_score:g} 低于归一门槛 "
                    f"{RECOMMENDATION_SNAP_FLOOR:g}，不足以进入分数档位"
                ),
                "direction": None,
                "trade_type": None,
                "analysis": d.analysis or "",
                "tactic_name": None,
                "tactic_reason": None,
                "entry_price": 0, "stop_loss": 0, "take_profit_1": 0,
                "take_profit_2": 0, "risk_reward_ratio": 0,
                "position_pct": 0,
                "recommendation": min(_raw_score, SKIP_RECOMMENDATION_CAP),
                "recommendation_raw": _raw_score,
            }

    tactic_name, tactic_reason = _tactic_gate(d, signal)

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
            "tactic_name": tactic_name,
            "tactic_reason": tactic_reason,
            "entry_price": 0, "stop_loss": 0, "take_profit_1": 0,
            "take_profit_2": 0, "risk_reward_ratio": 0,
            "position_pct": 0,
            "recommendation": min(d.recommendation, SKIP_RECOMMENDATION_CAP),
            "recommendation_raw": float(d.recommendation or 0),
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
    # TP2 的方向序/贴近问题不再打回（2026-10-13-9）：改由下方「止盈二贴档保护」
    # 静默撤档（tp2<=tp1 或间距过近一律 take_profit_2=0），不浪费 Reflection 重试
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
    if violations:
        return False, violations, {}

    # ── 近锚一致性校验（docs/23；2026-10-12-5 扩展至全信号类型）：AI 声明
    # stop_anchor=near 时，止损必须贴近金K窗口极值 ±0.5%——防 AI 挂近锚名义用
    # 远止损（骗 ×0.7 风险折价、污染复盘 structure/near 分组）。
    # 尾窗内信号根未走完（signal_bar_provisional）→ 极值未定型，该校验无基准，
    # 跳过并记 warning（详见 NEAR_ANCHOR_* 常量处的豁免说明，2026-10-12）──
    if signal.get("signal_bar_provisional"):
        logger.warning(
            "近锚校验跳过：信号根为扫描尾窗未走完根（极值未定型）%s",
            signal.get("symbol"),
        )
    elif d.stop_anchor == "near" and len(klines) >= 2:
        # 金K窗口（2026-10-12-8 恢复窗口口径）：最近 5 根已收盘K线的共同极值——
        # 做多=5 根最低点、做空=5 根最高点（klines[-6:-1]，含信号K线 klines[-2]；
        # klines[-1] 未收盘不参与），与 ai_agent._resolve_price 的
        # signal_low/signal_high 解析同口径
        window = klines[max(0, len(klines) - 6):len(klines) - 1]
        lows: list = []
        highs: list = []
        for c in window:
            try:
                c_high, c_low = float(c[2]), float(c[3])
            except (IndexError, TypeError, ValueError):
                continue
            if c_high > 0 and c_low > 0:
                highs.append(c_high)
                lows.append(c_low)
        if lows and highs:
            if d.direction == "long":
                anchor = min(lows) * (1 - NEAR_ANCHOR_BUFFER)
            else:
                anchor = max(highs) * (1 + NEAR_ANCHOR_BUFFER)
            if abs(d.stop_loss - anchor) / anchor > NEAR_ANCHOR_TOLERANCE:
                violations.append(
                    f"stop_anchor=near 但止损偏离金K窗口极值锚过远"
                    f"（锚={anchor:.8g}，SL={d.stop_loss}）：近锚仅允许最近5根已收盘"
                    f"K线{'最低' if d.direction == 'long' else '最高'}价外 "
                    f"{NEAR_ANCHOR_BUFFER:.1%} 缓冲"
                )
                return False, violations, {}
        else:
            logger.warning("near 校验跳过：金K窗口数据异常 %s", signal.get("symbol"))

    stop_dist = abs(d.entry_price - d.stop_loss)
    stop_pct = stop_dist / d.entry_price if d.entry_price else 0

    # ATR14：止损宽度下限（ATR 腿）与止盈一距离上限（ATR 腿）共用，只算一次
    _atr14 = None
    try:
        _arr = atr_series(klines, period=14) if klines else None
        if _arr is not None and len(_arr) and np.isfinite(_arr[-1]):
            _atr14 = float(_arr[-1])
    except Exception:
        _atr14 = None

    # ── 止损宽度下限（2026-10-12-2 拍板）：防"止损过近"被噪音扫掉、并抑制 R 放大 ──
    # 止损宽度须 ≥ SL_MIN_ATR×ATR14（默认 0.3）。近锚（突破类 stop_anchor=near）
    # 豁免——其止损刻意贴金K窗口极值，另有 NEAR_ANCHOR_RISK_FACTOR 敞口折价与
    # ±0.5% 一致性校验兜底，套全局下限会误杀整个近锚特性。ATR 不可算（K线不足）
    # 放行；SL_MIN_ATR=0 关闭。超限 → 打回让 AI 外扩锚点或直接 skip（程序不改价）。
    # 尾窗例外（2026-10-12）：信号根未走完时近锚的两个兜底都失效——极值未定型
    # （一致性校验已跳过）、且最后数分钟的影线扩展正是"过近被扫"的高发场景，
    # 故此时取消豁免、照套下限（宁宽勿紧），换取向上的安全偏移
    _near_anchor = (
        d.stop_anchor == "near"
        and (signal.get("signal_type") or "") in BREAKOUT_SIGNAL_TYPES
        and not signal.get("signal_bar_provisional")
    )
    if (
        not _near_anchor and stop_dist > 0 and _atr14 and _atr14 > 0
        and float(settings.SL_MIN_ATR) > 0
    ):
        _sl_floor = float(settings.SL_MIN_ATR) * _atr14
        if stop_dist < _sl_floor * (1 - 1e-9):
            violations.append(
                f"止损宽度过近：距入场 {stop_dist:.6g}（≈{stop_dist / _atr14:.2f}×ATR），"
                f"低于下限 {_sl_floor:.6g}（{settings.SL_MIN_ATR:g}×ATR）——"
                f"把锚点外扩到至少该宽度再报，或直接 skip（过近必被噪音扫）"
            )

    # ── RR 铁律（唯一保留的价格校验）：按 AI 三价复算，声称值不信 ──
    # 容差带（2026-10-14 起覆盖三价闸，此前仅开仓现价闸）：交易所 tick 取整后
    # 「入场±rr_min×止损距离」兜底价普遍落 1.49~1.51R，零容差把擦边单全部打回、
    # 白烧 Reflection 重试；tol 是 R 单位绝对带宽（默认 0.02），0=页面显式严格
    _rr_floor = float(rr_min) if rr_min is not None else float(settings.AI_RR_MIN)
    _rr_tol = max(0.0, float(rr_tol)) if rr_tol is not None else float(settings.AI_RR_TOL)
    _rr_floor_eff = max(0.0, _rr_floor - _rr_tol)
    rr = 0.0
    if stop_dist > 0:
        rr = abs(d.take_profit_1 - d.entry_price) / stop_dist
        if rr < _rr_floor_eff:
            if _rr_tol > 0:
                violations.append(
                    f"盈亏比复算 {rr:.2f} < {_rr_floor_eff:g}"
                    f"（交易系统铁律 {_rr_floor:g}，容差 {_rr_tol:g}）"
                )
            else:
                violations.append(f"盈亏比复算 {rr:.2f} < {_rr_floor:g}（交易系统铁律）")

    # ── 止盈一距离上限（2026-10-12 拍板）：第一档止盈须"够近"──
    # 摆动点位稀疏/强趋势下"最近结构位"常落到 3~4R，第一档命中率被拉低。上限
    # cap = max(rr_min×止损距离, min(TP1_MAX_ATR×ATR14, TP1_MAX_R×止损距离))——
    # 取 max 与 rr_min×止损距离 保证"入场±rr_min×止损距离"兜底永远可用、不会
    # 打回死循环；无 K 线（ATR 不可算）时只用 R 腿；某腿配置 0 即关闭该腿。
    # 超限 → 打回让 AI 改挂更近结构位或用兜底价（程序不改价，同止损锚定哲学）。
    # 数据异常不锁死开单。容差（2026-10-14）：同一条 rr_tol 按 R 单位绝对加宽
    # （允许带 = cap + tol×止损距离）——tick 取整后的 1.50~1.51R 兜底价放行，
    # 1.55R 这类真·越界仍拦，与 RR 铁律容差带同源
    if stop_dist > 0 and d.take_profit_1 > 0:
        _caps = []
        if float(settings.TP1_MAX_R) > 0:
            _caps.append(float(settings.TP1_MAX_R) * stop_dist)
        if _atr14 and _atr14 > 0 and float(settings.TP1_MAX_ATR) > 0:
            _caps.append(float(settings.TP1_MAX_ATR) * _atr14)
        if _caps:
            _cap = max(min(_caps), _rr_floor * stop_dist)
            _tp1_dist = abs(d.take_profit_1 - d.entry_price)
            _cap_eff = _cap + _rr_tol * stop_dist
            if _tp1_dist > _cap_eff * (1 + 1e-9):
                _cap_desc = f"{_cap_eff:.6g}"
                if _rr_tol > 0:
                    # 告知 AI 容差后的真实边界，避免按旧 cap 自纠仍擦边、多耗一次重试
                    _cap_desc = (
                        f"{_cap_eff:.6g}（基础上限 {_cap / stop_dist:.4g}R"
                        f"＋容差 {_rr_tol:g}R）"
                    )
                violations.append(
                    f"止盈一距离过远：距入场 {_tp1_dist:.6g}（≈{_tp1_dist / stop_dist:.2f}R），"
                    f"超过上限 {_cap_desc}——请改挂更近的结构位，"
                    f"或改用 入场±{_rr_floor:g}×止损距离 作为止盈一（程序只复算不改价）"
                )
    if violations:
        return False, violations, {}

    # ── 止盈二贴档保护（2026-10-13-9，程序侧静默撤档）──
    # AI 常把第二档挂到第一档近旁：两档几乎同时触发，分批意义消失（留半仓吃不到
    # 额外空间、还多付一份手续费），甚至反向/重合（TP2 ≤ TP1 多、TP2 ≥ TP1 空）。
    # 旧口径对这类决策打回（violations）走 Reflection 重试，但 AI 自纠成本高且不稳定
    # → 改为程序侧静默把 take_profit_2 置 0（不进 violations、不消耗重试），出场引擎
    # 按 80%/20% 分批（trade_engine._tp_quantities 已有逻辑）自动退化为单档全平。
    # 间距下限 gap_min = max(TP2_MIN_GAP_ATR×ATR14, TP2_MIN_GAP_R×止损距离,
    # 2×TAKER_FEE_RATE×入场价)；方向距离 d =（多）TP2−TP1 /（空）TP1−TP2，
    # d ≤ 0（反向/重合）或 d < gap_min 都撤档。ATR 不可算时 ATR 腿置 0；
    # TP2_MIN_GAP_ATR/R 都为 0 时整条保护失效（含手续费地板——开关口径）。
    _tp2 = float(d.take_profit_2) if d.take_profit_2 > 0 else 0.0
    _gap_cfg_on = float(settings.TP2_MIN_GAP_ATR) > 0 or float(settings.TP2_MIN_GAP_R) > 0
    if _tp2 > 0 and _gap_cfg_on:
        _gaps = [2 * float(settings.TAKER_FEE_RATE) * d.entry_price]
        if float(settings.TP2_MIN_GAP_R) > 0:
            _gaps.append(float(settings.TP2_MIN_GAP_R) * stop_dist)
        if _atr14 and _atr14 > 0 and float(settings.TP2_MIN_GAP_ATR) > 0:
            _gaps.append(float(settings.TP2_MIN_GAP_ATR) * _atr14)
        _gap_min = max(_gaps)
        _tp2_gap = (
            _tp2 - d.take_profit_1 if d.direction == "long"
            else d.take_profit_1 - _tp2
        )
        if _tp2_gap < _gap_min * (1 - 1e-9):
            logger.info(
                "止盈二贴档撤档 %s：TP1=%s TP2=%s（方向=%s，间距 %s < 下限 %s）",
                signal.get("symbol"), round(d.take_profit_1, 8), round(_tp2, 8),
                d.direction, round(_tp2_gap, 8), round(_gap_min, 8),
            )
            _tp2 = 0.0

    # ── 修正：程序复算值落库，AI 声称值不信 ──
    # 固定亏损仓位法：仓位（名义价值占账户%）= 风险预算 ÷ 止损距离。
    # 风险预算两种口径（2026-09-27 止损金额模式，与开仓引擎 trade_engine 同源）：
    # pct（默认）= 风险比例%（3%，system_config.risk_pct 可页面覆盖）× 账户资金
    # → 触止损恰好亏账户 3%；fixed = risk_budget 实额 USDT（由调用方从
    # system_config 传入）。止损越远仓位越小，不设 clamp。
    # 例：止损距离 2% + 3% 口径 → 仓位 150%（10×杠杆下占用保证金 15%）
    position_pct = 0.0
    if stop_pct > 0:
        if risk_budget is not None and risk_budget > 0:
            # fixed 模式（2026-09-27）：仓位%依赖钱包余额，分析期（多账号扇出前）
            # 无账号上下文，不做折算——置 None 让卡片「仓位建议」显示 -，
            # 真实仓位由开仓引擎按 固定金额÷止损距离 实算（trade_engine）
            position_pct = None
        else:
            _rp = float(risk_pct) if risk_pct is not None else float(settings.RISK_BUDGET_PCT)
            position_pct = round(_rp / stop_pct, 2)
    rec_tier, rec_raw = snap_recommendation(d.recommendation)
    fixed = {
        "trade_decision": "suggest",
        "skip_reason": "",
        "direction": d.direction,
        "trade_type": d.trade_type,
        "tactic_name": tactic_name,
        "tactic_reason": tactic_reason,
        # 止损锚类型落库（docs/23；2026-10-12-5 起 near 全信号类型有效——
        # 距离分档降级金K锚后非突破类也可合法声明 near，一致性校验已在上方
        # 把关贴极值）；None（未声明/非法值）按 structure 理解
        "stop_anchor": "near" if d.stop_anchor == "near" else "structure",
        "analysis": d.analysis or "",
        "entry_price": round(d.entry_price, 8),
        "stop_loss": round(d.stop_loss, 8),
        "take_profit_1": round(d.take_profit_1, 8),
        "take_profit_2": round(_tp2, 8),
        "risk_reward_ratio": round(rr, 2),
        "position_pct": position_pct,
        # 档位硬校验（T2）：执行链只出现 60/70/85；原始连续分保留供校准
        "recommendation": rec_tier,
        "recommendation_raw": rec_raw,
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
        "position_pct": 0, "recommendation": 10, "recommendation_raw": 10,
    }


def parse_and_validate(
    raw: dict, signal: dict, klines: list,
    risk_budget: Optional[float] = None,
    rr_min: Optional[float] = None,
    risk_pct: Optional[float] = None,
    rr_tol: Optional[float] = None,
) -> tuple[bool, list[str], dict]:
    """normalize + pydantic 校验 + 业务校验 的一步入口（预算参数透传 validate_decision）"""
    try:
        d = normalize_raw(raw)
    except (ValidationError, ValueError) as e:
        return False, [f"输出格式不合法: {str(e)[:200]}"], {}
    return validate_decision(d, signal, klines, risk_budget=risk_budget,
                             rr_min=rr_min, risk_pct=risk_pct, rr_tol=rr_tol)
