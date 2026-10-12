"""Risk Guard：AI 决策的确定性校验器（docs/04 §4 Stage 4 / §5.6）

校验链（2026-09-15 收缩为 RR 铁律 + 双评委辩论复核；2026-10-12 增止损宽度下限与
止盈一距离上限）：
- Schema 强约束（Pydantic）+ suggest 基本项（方向/开单类型/三价为正）
- **RR 铁律**：按 AI 给的三价复算盈亏比 ≥ AI_RR_MIN(1.5)，声称值不信
- **止损宽度下限**（2026-10-12-2）：止损距入场 ≥ SL_MIN_ATR×ATR14（默认 0.3），
  防"止损过近"被噪音扫掉并抑制 R 放大；近锚（突破类 stop_anchor=near）豁免
  （其止损刻意贴金K窗口极值，另有敞口折价与 ±0.5% 一致性校验兜底）
- **止盈一距离上限**（2026-10-12）：第一档止盈距入场 ≤
  min(TP1_MAX_ATR×ATR14, TP1_MAX_R×止损距离)、且不低于 rr_min×止损距离
  （防"第一档"落到 3~4R 的远位；超限打回让 AI 改近档或走兜底，程序不改价）
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

方向-价格一致性（2026-09-16 恢复为硬校验，不属于方法论）：多单 SL<entry<TP1、
空单 TP1<entry<SL（TP2>0 时同侧递延）。2026-09-15 随校验链收缩移除后，
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
    # 止盈模式（2026-10-11 策略一重定义，docs/19 §3.4）：由 entry_strategy 程序
    # 推导——ladder=分批止盈（s1，缺省）/ trail=纯跟踪（s2，TP1 仅作盈亏比校验
    # 参考价，程序不挂止盈档）；AI 输出值无条件被覆盖（原 system_config.tp_mode
    # 强制配置已退役）
    tp_mode: Optional[Literal["ladder", "trail"]] = None
    # 止损锚类型（docs/23，仅突破类信号需要）：structure=结构锚（默认）/ near=金K窗口极值近锚
    stop_anchor: Optional[Literal["structure", "near"]] = None
    # 战法命中标注（2026-10-02，仅借鉴不影响结论）：tactic_name 必须逐字取自
    # 本次注入的战法清单（validate_decision 白名单闸门，清单外清空防幻觉）
    tactic_name: Optional[str] = None
    tactic_reason: Optional[str] = None
    # 双入场策略（2026-10-10-5，docs/20）：entry_strategy=胜出策略——s1=完整规则
    # （RR 铁律+止盈规划，缺省按 s1）/ s2=EMA21 金K跟踪（无 RR 铁律、不规划止盈，
    # 程序强制 tp_mode=trail）；strategy2_*=策略二判定审计字段（suggest/skip+依据），
    # 两策略结论分条见 analysis。资格程序闸：s2 仅当 signal.touch_ref == "ema21"
    entry_strategy: Optional[str] = None
    strategy2_decision: Optional[str] = None
    strategy2_reason: Optional[str] = None
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
    # 止盈模式（docs/19 §3.4）：非法值（含空串/prompt 教的 null）一律回退 None
    # （suggest 落库时回退 ladder）
    if data.get("tp_mode") not in ("ladder", "trail"):
        data["tp_mode"] = None
    # 战法字段空串归 None（未命中）；白名单校验在 validate_decision（需注入清单）
    for _tf in ("tactic_name", "tactic_reason"):
        if not str(data.get(_tf) or "").strip():
            data[_tf] = None
        else:
            data[_tf] = str(data[_tf]).strip()[:64 if _tf == "tactic_name" else 255]
    # 双入场策略（2026-10-10-5）：非法值归 None（按 s1 理解，安全默认=存量行为不变）；
    # strategy2_decision 同理；strategy2_reason 仅截断
    if data.get("entry_strategy") not in ("s1", "s2"):
        data["entry_strategy"] = None
    if data.get("strategy2_decision") not in ("suggest", "skip"):
        data["strategy2_decision"] = None
    _s2r = str(data.get("strategy2_reason") or "").strip()
    data["strategy2_reason"] = _s2r[:255] or None
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

# 盈亏比现价复算容差（system_config.ai_rr_tol 可页面覆盖，NULL=默认 0.02）：
# 开仓闸按现价复算 RR ≥ ai_rr_min - tol 即放行（AI 三价铁律不加容差）。
# 0.0002 级价格漂移误杀案例：CYBERUSDT RR 1.4998 被 1.5 拦
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
) -> tuple[bool, list[str], dict]:
    """校验并修正决策（2026-09-15 拍板收缩：仅保留 RR 铁律 + 仓位复算）。

    tp_mode 由程序按 entry_strategy 推导（2026-10-11 策略一重定义，docs/20 §五）：
    s1→ladder、s2→trail，AI 输出值被覆盖不信任。原拍板 B 的 trail 条件豁免
    （htf_bg/_trail_rr_exempt）随策略一 ladder-only 退役——trail 归策略二独有，
    RR 铁律仅策略二豁免。

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
    """
    violations: list[str] = []
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
            "tp_mode": None,
            "analysis": d.analysis or "",
            "tactic_name": tactic_name,
            "tactic_reason": tactic_reason,
            # 双策略审计字段透传（2026-10-10-5）：skip=两策略均不满足
            "entry_strategy": None,
            "strategy2_decision": d.strategy2_decision,
            "strategy2_reason": d.strategy2_reason,
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

    # ── 双入场策略（2026-10-10-5，docs/20）：s2=EMA21 金K跟踪 ──
    # 资格程序闸：s2 仅当信号触点为 ema21（程序事实，AI 谎报打回重试）；
    # 2026-10-12 拍板（docs/21 O7）收紧为「触点=ema21 ∧ trend_gated」——
    # trend_pullback 升级标注 = 4H 闸门+1H 结构门全过（trigger 层判定），
    # 结构门未过的 ema21 信号 s2 不可用（s1 照常评估，信号池不再被整体杀）；
    # 语义强制：tp_mode 由策略推导（2026-10-11 策略一重定义）——s2 即 trail
    # （不挂止盈档）、s1 即 ladder（trail 加入前原版），AI 输出值被程序覆盖
    _is_s2 = d.entry_strategy == "s2"
    _trend_gated = (signal.get("signal_type") or "") == "trigger_trend_pullback"
    if _is_s2 and (signal.get("touch_ref") or "") != "ema21":
        violations.append(
            f"策略二仅适用于回踩触点=ema21 的信号（程序校验，当前触点="
            f"{signal.get('touch_ref') or '无'}）：改按策略一评估，或如实 skip"
        )
        return False, violations, {}
    if _is_s2 and not _trend_gated:
        violations.append(
            "策略二仅适用于 1H 结构门通过的信号（trigger_trend_pullback 标注，"
            f"当前 signal_type={signal.get('signal_type') or '未知'}）："
            "改按策略一评估，或如实 skip"
        )
        return False, violations, {}
    # 审计交叉一致性（2026-10-10-5b）：strategy2_* 是复盘分组键，矛盾组合打回防脏
    if d.strategy2_decision == "suggest" and (signal.get("touch_ref") or "") != "ema21":
        violations.append(
            f"strategy2_decision=suggest 与触点（{signal.get('touch_ref') or '无'}）矛盾"
            f"——策略二仅触点=ema21 可用，改 skip 并注明依据，或如实修正"
        )
        return False, violations, {}
    if d.strategy2_decision == "suggest" and not _trend_gated:
        violations.append(
            f"strategy2_decision=suggest 与 signal_type（{signal.get('signal_type') or '未知'}）"
            "矛盾——策略二仅 1H 结构门通过（trigger_trend_pullback）可用，"
            "改 skip 并注明「1H结构门未过」，或如实修正"
        )
        return False, violations, {}
    if _is_s2 and d.strategy2_decision != "suggest":
        violations.append(
            f"entry_strategy=s2 须以策略二建议为前提（当前 strategy2_decision="
            f"{d.strategy2_decision or '未填'}）——s2 胜出则 strategy2_decision=suggest"
        )
        return False, violations, {}
    if _is_s2:
        d.tp_mode = "trail"
        d.take_profit_2 = 0.0
    else:
        d.tp_mode = "ladder"

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
    # 策略二豁免（2026-10-10-5）：s2 定义即无 RR 铁律（EMA21 金K跟踪，不规划止盈）。
    # 优先级闸（2026-10-10-5b 拍板：打回改自动改标）——s2 的 TP1 是趋势目标参考价，
    # 可能比 s1 的第一档结构位更远，"s2 复算达标⟹s1 达标"并不成立，打回会诱发
    # 跳档凑 RR 或把仅 s2 满足的单误杀成 skip；同三价在 s1 全部程序闸（RR/方向
    # 价格序/近锚）均已通过，直接改标 s1 落库即完成"优先策略一"。改标痕迹：
    # entry_strategy=s1 + strategy2_decision=suggest，复盘可过滤。tp_mode 同步
    # 翻 ladder（2026-10-11：trail 归策略二独有，s1=ladder-only）
    _rr_floor = float(rr_min) if rr_min is not None else float(settings.AI_RR_MIN)
    rr = 0.0
    if stop_dist > 0:
        rr = abs(d.take_profit_1 - d.entry_price) / stop_dist
        if _is_s2 and rr >= _rr_floor:
            # 优先级闸（改标式）：同三价按 s1 规则同样合法 → 静默改标，不打回不重试
            d.entry_strategy = "s1"
            d.tp_mode = "ladder"
            _is_s2 = False
        elif rr < _rr_floor and not _is_s2:
            violations.append(f"盈亏比复算 {rr:.2f} < {_rr_floor:g}（交易系统铁律）")

    # ── 止盈一距离上限（2026-10-12 拍板）：第一档止盈须"够近"──
    # 摆动点位稀疏/强趋势下"最近结构位"常落到 3~4R，第一档命中率被拉低。上限
    # cap = max(rr_min×止损距离, min(TP1_MAX_ATR×ATR14, TP1_MAX_R×止损距离))——
    # 取 max 与 rr_min×止损距离 保证"入场±rr_min×止损距离"兜底永远可用、不会
    # 打回死循环；无 K 线（ATR 不可算）时只用 R 腿；某腿配置 0 即关闭该腿。
    # 超限 → 打回让 AI 改挂更近结构位或用兜底价（程序不改价，同止损锚定哲学）。
    # trail 模式（s2，TP1 仅趋势参考价、不挂单）豁免；数据异常不锁死开单。
    if not _is_s2 and stop_dist > 0 and d.take_profit_1 > 0:
        _caps = []
        if float(settings.TP1_MAX_R) > 0:
            _caps.append(float(settings.TP1_MAX_R) * stop_dist)
        if _atr14 and _atr14 > 0 and float(settings.TP1_MAX_ATR) > 0:
            _caps.append(float(settings.TP1_MAX_ATR) * _atr14)
        if _caps:
            _cap = max(min(_caps), _rr_floor * stop_dist)
            _tp1_dist = abs(d.take_profit_1 - d.entry_price)
            if _tp1_dist > _cap * (1 + 1e-9):
                violations.append(
                    f"止盈一距离过远：距入场 {_tp1_dist:.6g}（≈{_tp1_dist / stop_dist:.2f}R），"
                    f"超过上限 {_cap:.6g}——请改挂更近的结构位，"
                    f"或改用 入场±{_rr_floor:g}×止损距离 作为止盈一（程序只复算不改价）"
                )
    if violations:
        return False, violations, {}

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
    fixed = {
        "trade_decision": "suggest",
        "skip_reason": "",
        "direction": d.direction,
        "trade_type": d.trade_type,
        # 止盈模式落库（2026-10-11 策略一重定义）：由 entry_strategy 推导——
        # s1→ladder、s2→trail，已在上方程序强制写入（AI 输出值不信任）；
        # 原 system_config.tp_mode 强制覆盖随 AI 自选 trail 一并退役
        "tp_mode": d.tp_mode if d.tp_mode in ("ladder", "trail") else "ladder",
        # 双入场策略（2026-10-10-5）：胜出策略落库（None=旧行/未声明按 s1）+
        # 策略二判定审计字段；复盘按 entry_strategy 分组对比两策略表现
        "entry_strategy": "s2" if _is_s2 else "s1",
        "strategy2_decision": d.strategy2_decision,
        "strategy2_reason": d.strategy2_reason,
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
        # 双策略审计字段（2026-10-10-5）：强制 skip 无策略上下文，置 None
        "entry_strategy": None,
        "strategy2_decision": None,
        "strategy2_reason": None,
        "entry_price": 0, "stop_loss": 0, "take_profit_1": 0,
        "take_profit_2": 0, "risk_reward_ratio": 0,
        "position_pct": 0, "recommendation": 10,
    }


def parse_and_validate(
    raw: dict, signal: dict, klines: list,
    risk_budget: Optional[float] = None,
    rr_min: Optional[float] = None,
    risk_pct: Optional[float] = None,
) -> tuple[bool, list[str], dict]:
    """normalize + pydantic 校验 + 业务校验 的一步入口（预算参数透传 validate_decision）"""
    try:
        d = normalize_raw(raw)
    except (ValidationError, ValueError) as e:
        return False, [f"输出格式不合法: {str(e)[:200]}"], {}
    return validate_decision(d, signal, klines, risk_budget=risk_budget,
                             rr_min=rr_min, risk_pct=risk_pct)
