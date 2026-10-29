"""扫描编排器（指标驱动筛选管线，2026-09-17 重设计）

筛选漏斗（指标为主、信号为触发器，EMA 三件套硬门 2026-10-10 二次拍板）：
1. 宇宙：USDT 永续，24h 成交额 ≥ MIN_VOLUME_24H（流动性门槛）
2. 4h 合并入口 htf_context 一票否决（2026-10-20 合并：原 gate_4h 三重否决 +
   4h 状态机）：EMA144 斜率走平/缠绕/ADX 必须过（detect_tangle 统一缠绕判据），
   否决直接不看（不跑票数不触发不进 AI）；震荡闸门 4 项合取任一命中同为硬门
   （technical.py / ema_gate.py，docs/19 §2/§3）。1H 结构门已于 2026-10-12 退役（docs/21
   O7；策略二资格闸随策略二删除），2026-10-15 再由 trigger 层硬门降级为
   质量分重罚（docs/21 O9）——结构判定不否决信号，只在质量分上体现
3. 分组：方向票（MACD/布林带/RSI）同向 ≥2 张 → 多头/空头候选（2026-10-15
   指标拆角色，docs/21 O9）；ADX/OBV/SuperTrend/4h背景退出方向票
4. 触发确认：两段式决策矩阵（docs/19）——结构突破/趋势突破 / 回踩+金K确认；
   回踩触碰无金K 转入 pending 待确认池（pullback_pendings，次根金K才确认）；
   无触发 → 观察池（不落库，下轮重算）
5. 质量分排序 + 重复命中抑制（仅定时）+ 落库 → AI 配额（按质量分取前 N）

旧信号管线（Donchian+形态为主动作、EMA 硬门控）已整体替换：
方向判定权从信号路径移交给方向票多数决（2026-10-15 指标拆角色，docs/21 O9），
与 AI 提示词的方向票规则同一把尺子。
"""
import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from typing import Optional, Tuple
from uuid import NAMESPACE_URL, UUID, uuid5

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.config import settings
from app.database import SessionLocal
from app.models.scan import BreakoutEvent, PullbackPending, ScanRecord, ScanResult
from app.models.shadow import ShadowMark
from app.models.system_config import SystemConfig
from app.models.trade import TradeRecord
from app.services.scan_filters import ScanFilters, resolve_scan_filters
from app.services.exchange_pool import ExchangePool
from app.services.binance_trader import BinanceTrader
from app.services import market_data as md
from app.services.strategy.ema import analyze_ema
from app.services.strategy.indicators import volume_ratio
from app.services.strategy.technical import (
    DIR_VOTE_MIN,
    STATE_LOCK_CONTRA,
    compute_indicator_votes,
    compute_quality_score,
    pullback_stabilize_bonus,
    htf_context,
    HTF_SHORT,
)
from app.services.strategy.trigger import (
    BREAKOUT,
    PULLBACK_CONFIRM,
    TOUCH_RETEST,
    TREND_BREAKOUT,
    detect_trigger,
)
from app.services.strategy.ema_gate import (
    RANGE_SCORE_REJECT,
    range_score,
)

logger = logging.getLogger(__name__)


def _scan_hit_card(new_hits: list[dict]) -> dict:
    """定时扫描命中消息卡片（2026-09-30 由纯文本改卡片；2026-10-02 三列等分）。

    标题计数 + 每命中一行 column_set：币种 / 信号族·金K形态 / 质量分 三列，
    flex_mode=trisect 强制三等分（stretch+权重会被内容宽度挤歪，2026-10-01
    的 34/33/33 加权方案在客户端渲染不齐）；信号族与金K形态中文名沿用复盘
    TRIGGER_FAMILY / PATTERN_LABEL_MAP 映射；超 8 条截断、余量并入 note
    脚注。返回卡片体（外层包装与发送通道由 feishu_bot 负责）。
    """
    from app.services.actual_review import TRIGGER_FAMILY
    from app.services.strategy.candlestick import PATTERN_LABEL_MAP

    def _col(content: str) -> dict:
        return {
            "tag": "column",
            "width": "weighted",
            "weight": 1,
            "elements": [{"tag": "markdown", "content": content}],
        }

    rows = []
    for h in new_hits[:8]:
        sig = TRIGGER_FAMILY.get(h.get("signal_type") or "", "未知信号")
        if h.get("pattern"):
            sig += f"·{PATTERN_LABEL_MAP.get(h['pattern'], h['pattern'])}"
            # 组合确认（2026-10-26，docs/11）：形态在信号根的前一根，卡片 dict 无
            # signal_candle_open，用等价判据 entry_lag_pct 非空（构造上只有形态早于
            # 信号根才算滞后）；不看 golden_combo——双流星 golden_combo=True 但形态
            # 就在信号根，不该标 (前根)
            if h.get("entry_lag_pct") is not None:
                sig += "(前根)"
        rows.append({
            "tag": "column_set",
            "flex_mode": "trisect",
            "columns": [
                _col(f"**{h['symbol']}**"),
                _col(sig),
                _col(f"质量分 {h.get('quality_score', 0)}"),
            ],
        })
    card = {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text",
                      "content": f"🔎 定时扫描命中 {len(new_hits)} 个"},
            "template": "blue",
        },
        "elements": rows,
    }
    if len(new_hits) > 8:
        card["elements"].append({"tag": "note", "elements": [
            {"tag": "plain_text",
             "content": f"仅列前 8 个，共命中 {len(new_hits)} 个"}]})
    return card


def _ai_skip_card(slots_detail: str) -> dict:
    """定时扫描满仓跳过 AI 分析的消息卡片（2026-10-04）。

    此前跳过只在 celery 日志与任务记录 summary 里，命中通知发了、AI 却没动静，
    用户无从归因。卡片橙标题 + 两网在跑/上限计数（slots_detail 由 _slots_full
    生成：「满仓跳过 AI 分析：X网在跑单达上限（测试网 X/Y、正式网 X/Y）」），
    note 脚注说明恢复条件。仅定时扫描推送（手动扫描用户在场不推）。"""
    # slots_detail 形如「满仓跳过 AI 分析：测试网在跑单达上限（测试网 10/10、正式网 4/10）」
    # → 拆出原因与计数行分别渲染
    reason = slots_detail.split("：", 1)[-1] if "：" in slots_detail else slots_detail
    counts = slots_detail[slots_detail.find("（") + 1:slots_detail.rfind("）")]
    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": "⏸️ 满仓跳过本轮 AI 分析"},
            "template": "orange",
        },
        "elements": [
            {"tag": "markdown", "content": f"**{reason}**\n在跑名额：{counts}"},
            {"tag": "note", "elements": [{"tag": "plain_text", "content":
             "定时扫描满仓时跳过批量 AI 分析（省 LLM 成本）；任一网结算释放名额后下一轮自动恢复。命中的币种可在筛选结果页手动单币分析（不受满仓限制）。"}]},
        ],
    }


def classify_volume(klines: list[list]) -> tuple[float, str]:
    """提取最新已收盘 K 线成交量并分类（比率口径复用 indicators.volume_ratio，
    与放量突破门槛 BREAKOUT_VOL_RATIO 同源）。

    klines[-1] 是未收盘 K 线，用 klines[-2]。返回 (volume, volume_type)。
    """
    if len(klines) < 22:
        return 0.0, "平量"
    vol = float(klines[-2][5])
    ratio = volume_ratio(klines)
    if ratio <= 0:  # 近20根均量为 0（volume_ratio 的兜底分支）
        return vol, "平量"
    if ratio < 0.5:
        vt = "地量"
    elif ratio < 0.8:
        vt = "缩量"
    elif ratio <= 1.2:
        vt = "平量"
    elif ratio < 2.0:
        vt = "放量"
    else:
        vt = "倍量"
    return vol, vt


def _get_scan_config(db) -> SystemConfig:
    """从数据库读取扫描策略配置"""
    cfg = db.get(SystemConfig, 1)
    if not cfg:
        # 回退到 env 默认值
        cfg = SystemConfig(
            id=1,
            ai_analysis_enabled=False,
            kline_interval=settings.KLINE_INTERVAL,
            kline_window=settings.KLINE_WINDOW,
            breakout_threshold=settings.BREAKOUT_THRESHOLD,
            r_squared_threshold=settings.R_SQUARED_THRESHOLD,
            repeat_window_hours=settings.REPEAT_WINDOW_HOURS,
            swing_order=settings.SWING_ORDER,
            pullback_tolerance=settings.PULLBACK_TOLERANCE,
        )
    return cfg


def _votes_detail(votes: dict) -> str:
    """漏斗 votes 关票明细渲染（2026-10-11 用户拍板）：全部票逐张展示
    「短名 方向箭头 状态（细节）」，分号连接——前端判定说明列按分号拆行。

    方向箭头：▲=多票 / ▼=空票 / —=弃权或方向 0；state_label/detail 缺失的
    指标降级只出短名+箭头（不因明细渲染再抛异常拒绝流程）"""
    parts: list[str] = []
    for short, r in votes.get("results", []):
        try:
            d = int(r.get("dir") or 0)
        except (TypeError, ValueError):
            d = 0
        arrow = "▲" if d > 0 else ("▼" if d < 0 else "—")
        seg = f"{short} {arrow}"
        if r.get("state_label"):
            seg += f" {r['state_label']}"
        if r.get("detail"):
            seg += f"（{r['detail']}）"
        parts.append(seg)
    return "；".join(parts)


def _htf_snapshot(res: Optional[dict], degraded: bool = False) -> dict:
    """4h 前置门否决证据快照（2026-10-21 第 2 步：漏斗证据 + 被拦信号影子）。

    `gate_4h` 关卡承载两种语义不同的死因——数据降级（拉取失败/空数据/计算
    异常/拒因 data·adx）与市场状态（flat/tangle/undetermined）——关卡代码
    不变（前端关卡表稳定），靠 reject_reason + degraded 区分复盘口径。
    `htf_state_lock`（block_both / 逆势锁）reject_reason 恒空，靠 state 说明
    拦在哪个状态；dir 提供反事实方向（被拦信号若放行的方向依据）。
    res=None（4h 数据不可用）时 reject_reason 记 "missing" 并标降级。
    """
    if res is None:
        return {
            "state": None, "state_label": None, "reject_reason": "missing",
            "direction": None, "dir": 0, "evidence": {}, "degraded": True,
        }
    ev = res.get("evidence") or {}
    return {
        "state": res.get("state"),
        "state_label": res.get("state_label"),
        "reject_reason": res.get("reject_reason"),
        "direction": res.get("direction"),
        "dir": int(res.get("dir") or 0),
        "evidence": {
            "slope_pct": ev.get("slope_pct"),
            "wraps": ev.get("wraps"),
            "adx": ev.get("adx"),
            "close_vs_ema144": ev.get("close_vs_ema144"),
        },
        "degraded": bool(degraded),
    }


def _blocked_probe(klines: list[list], flt, retest=None) -> dict:
    """被 4h 门拦下的候选补「触点/金K」解析（2026-10-27，docs/16 §16.3 D1）。

    只是 `trigger.probe_blocked_context` 的容错包装：任何异常都返回空 dict，
    影子快照绝不因为诊断计算失败而中断（调用方在扫描主循环里）。
    """
    try:
        from app.services.strategy.trigger import probe_blocked_context
        return probe_blocked_context(klines, flt, retest=retest) or {}
    except Exception as e:  # noqa: BLE001 - 诊断数据
        logger.debug("被拦候选触点/金K解析失败 %s", e)
        return {}


def _htf_funnel(evidence: dict[str, dict]) -> dict:
    """4h 关卡证据聚合（2026-10-21 第 2 步）：关卡 × 状态 / 死因分布。

    htf_states：被 4h 前置门拦下的 4h 状态分布（含 "none"=状态机不可用）；
    gate_4h_reasons：gate_4h 死因分布——degraded=数据降级，其余为市场状态
    （flat/tangle/undetermined/adx/missing），复盘据此分离「数据源问题」与
    「4h 无趋势」。htf_state_lock 无 reject_reason，不计入该分布。

    ⚠ 口径（2026-10-27，docs/16 §2 L0-4 更正）：这里的分母是**被 4h 门拦下**的
    币次，与 `ai_analyses.htf_phase`（分母=已出 suggest 的候选，走同一
    `analyze_htf_background` 状态机）不是同一总体——前者以 tangle/flat 为主，
    后者以 aligned_* 为主，属幸存者差异而非两套算法。对比两者前必须先对齐分母，
    否则会得出「4h 看多 99 个 vs 4 个」的假矛盾。
    """
    states: dict[str, int] = {}
    reasons: dict[str, int] = {}
    for ev in evidence.values():
        st = ev.get("state") or "none"
        states[st] = states.get(st, 0) + 1
        if ev.get("stage") == "gate_4h":
            rr = "degraded" if ev.get("degraded") else (ev.get("reject_reason") or "unknown")
            reasons[rr] = reasons.get(rr, 0) + 1
    return {"htf_states": states, "gate_4h_reasons": reasons}


def _funnel_observability(
    hits_before_floor: list[dict],
    suppressed: int,
    pendings: dict[str, dict],
    pending_ops: dict[str, dict],
) -> dict:
    """漏斗观测补键（2026-10-28，docs/19 §4.3）：raw_candidates / suppressed /
    pending_active。

    - raw_candidates：质量分下限**之前**的候选快照（symbol/score/parts）。被砍
      候选不落 scan_results，缺此快照就无法复盘「差多少分、哪个分项拖的」。
    - suppressed：本轮被重复命中抑制掉、因此未落库的候选数。
    - pending_active：本轮结束时**币池内**活跃 pending 估算=预取快照里未被终态
      处置的 + 本轮新建（upsert 且不在快照）。与 pending_pool（本轮 upsert 条数，
      含刷新）不是同一口径——前端 KPI 曾把后者当池内总数展示（docs/19 §4.2）。
    """
    raw = sorted(
        ({"symbol": h.get("symbol"),
          "score": h.get("quality_score"),
          "parts": h.get("quality_parts")} for h in hits_before_floor),
        key=lambda x: x.get("score") or 0,
        reverse=True,
    )
    active = 0
    for sym in set(pendings) | set(pending_ops):
        op = pending_ops.get(sym)
        if op is not None:
            active += 1 if op.get("op") == "upsert" else 0
        elif sym in pendings:
            active += 1
    return {
        "raw_candidates": raw,
        "suppressed": suppressed,
        "pending_active": active,
    }


def detect_indicator_candidate(
    pool: ExchangePool, symbol: str, klines: list[list],
    pending: Optional[dict] = None,
    flt: Optional[ScanFilters] = None,
    retest: Optional[dict] = None,
    event_out: Optional[dict] = None,
    reject_out: Optional[dict] = None,
    htf_out: Optional[dict] = None,
    now: Optional[datetime] = None,
) -> Tuple[Optional[dict], Optional[dict]]:
    """指标驱动单币检测（Scanner 与 watchlist.refresh 共用同一套逻辑与阈值）。

    流程：4h 合并入口 htf_context 一票否决（走平/缠绕/ADX 任一命中直接不看，
    取代原 gate_4h + 状态机前置门）→ 单趟指标计算 → 方向票（MACD/布林带/
    RSI）同向 ≥2 张（2026-10-15 指标拆角色，docs/21 O9）→ 闸门方向一致 →
    震荡闸门 range_score（4 项合取，任一命中不入场）→ 两段式触发确认（trigger.py）→ 质量分。
    4h 背景复用第一步结果、指标不再双算。
    1H 结构门：2026-10-12 退役筛选链硬门（docs/21 O7），2026-10-15 再由
    trigger 层硬门「降级不否决」（docs/21 O9）——ema21 触点不过门照常出信号，
    仅质量分重罚（trigger 证据 structure_gate=False → -15），scanner 不再记
    结构门死因。

    pending: 该币活跃回踩 pending 快照（{id, direction, stage1_candle_open, touch_ref}）或 None。
    flt: 筛选参数组（2026-09-28 筛选配置上页面）；None=按 settings 解析。
    retest: 活跃破轨事件快照 {price, direction, candle_open}（docs/22 回测触点）或 None。
    event_out: 可选出参 dict——T1 突破触发时写入破轨事件动作
      {"op": "upsert", direction, level_price, candle_open}（scanner 落库
      breakout_events，回测触点事实源）；触发类型白名单过滤不影响事件记录
      （破轨是市场事实，与是否产生突破信号无关）。
    reject_out: 可选出参 dict（与 event_out 同旁路模式，2026-10-11 筛选漏斗）——
      币种被关卡拒绝时写入 {"stage": <关卡代码>}，供主扫描漏斗落库展示
      「卡在哪一步」；候选产生/进待确认池（pending upsert）时不写。关卡代码：
      gate_4h / htf_state_lock（4h 状态机双向禁开前置门，2026-10-13）/
      votes（方向票同向不足）/ direction_flip / range_score /
      no_trigger / trigger_whitelist。
      votes 关附 "detail"（全票明细）、htf_state_lock / no_trigger /
      trigger_whitelist 关附 "detail"（2026-10-13）——前端判定说明列渲染，
      其余关卡不写。gate_4h / htf_state_lock 另附 "htf"（4h 证据快照，
      2026-10-21 第 2 步：state / reject_reason / 斜率·缠绕·ADX 等），
      供漏斗聚合与被拦信号影子落库。
    htf_out: 可选出参 dict（与 event_out 同旁路模式，2026-10-29）——通过
      gate_4h 与 htf_state_lock 两关时写入 {"state", "state_label"}，供漏斗
      抽屉「通过」按 4h 放行状态（排列 / 恢复初期）分组；数据降级
      （htf_res=None）不写，前端归入「未记录」。
    now: 本轮数据基准时刻（生产传 scan_now）；仅用于 4h 末根已收盘竞态兜底
      （整点取数时新 4h 根可能尚未出现、末根实为刚收盘根）。None 时不做该
      兜底——测试/回放的历史切片保持「末根=未收盘根」语义。
    返回 (候选 dict, pending_op) 二元组：
    - 候选 dict（signal_type/touch_ref/quality_score/votes 及展示字段），每币至多 1 个；
      无候选（方向票不足/无触发）为 None
    - pending_op: pending 处置动作（upsert/expire/confirm/supersede），可为 None；
      候选为 None 时仍可能携带动作（触碰转 pending、票崩/窗口过期）

    异常向上抛由调用方处理。
    """

    def _reject(stage: str) -> None:
        # 漏斗旁路（单币一个新 dict，clear 仅为防御性安全）
        if reject_out is not None:
            reject_out.clear()
            reject_out["stage"] = stage

    # flt=None 按 settings 解析（docstring 约定）：结构门退役前 flt=None 路径
    # 到不了白名单行（多数夹具被结构门拦下），O7 后触发信号直达白名单检查，
    # 必须在此解析兜底
    if flt is None:
        flt = resolve_scan_filters(None)

    ema = analyze_ema(klines)  # 背景展示用（方向判定不依赖 EMA）
    vol, vol_type = classify_volume(klines)

    # 第一步：4h 合并入口 htf_context（2026-10-20 用户拍板：原 gate_4h 三重否决
    # 与 4h 状态机合并为单入口）——整个筛选系统最重要的过滤器：只有 4H 趋势明确
    # （EMA144 斜率 ≥0.5%/20根、非缠绕〔detect_tangle：ADX<20 ∨ 21/55间距<0.1% ∨
    # 21×55锯齿 ∨ 价格×144 穿越>2〕、ADX 可用、方向确定）才允许 1H 入场，4h 走平
    # 或缠绕时 1h 信号再漂亮也不做。命中否决（reject_reason 非空或 core 不足返回
    # None）→ 直接不看：不跑票数、不触发、不进候选（连 AI 分析都不给），活跃
    # pending 同轮 expire(ranging)（ranging 词义="4h 无明确趋势"，涵盖走平与缠绕）。
    # 4h 每轮对全币池拉取（kline 缓存按 4h 桶对齐，实际新请求 ≈ 池子 ÷4 每小时）；
    # 拉取失败/计算异常/数据不足一律 fail-closed（方向关拒绝），不再降级放行——
    # 「数据缺失」与「市场状态否决」通过 degraded 标记区分，不混淆复盘口径。
    # 4h 数据降级标记（2026-10-21 观测 B2）：把「数据缺失」类拒绝（拉取失败/空数据/
    # 计算异常/拒因为 data·adx）与「市场状态」类拒绝（flat/tangle/undetermined）
    # 分开。方向职责拆分后 4h 无方向一律 fail-closed（不再降级放行），标记随
    # 漏斗落 funnel_json，供按天统计数据源降级占比（spec §5.3，>1% 需排查数据源）
    htf_degraded = False
    try:
        # 600 根：EMA144 的 SMA 种子在 300 根时仍残留约 11% 权重（20 根斜率误差
        # 可达走平阈值 0.5% 的 1/3），600 根后残留 <0.2%，斜率判定才稳定
        htf = pool.get_klines(symbol, "4h", 600)
    except Exception as e:
        htf = None
        htf_degraded = True
        logger.warning("%s 获取4h K线失败（跳过4h票）: %s", symbol, e)
    htf_res = None
    if htf:
        # 4h 末根竞态兜底（与 1h 同口径）：整点取数时新 4h 根可能尚未出现在响应
        # 里、末根实为刚收盘根——补幽灵尾根，避免 htf_core 剔掉它导致 4h 状态
        # 整体滞后一根（4 小时）。now 由调用方传入；None 时不处理，保持测试/回放
        # 历史切片的「末根=未收盘」语义不变。
        if now is not None:
            htf = md.screen_klines(htf, "4h", now)
        # 合并入口一次算 core（EMA21/55/144 + ADX + ATR）并产出状态机视图 +
        # 闸门方向 + 行为矩阵，替代原「core 透传 → analyze + gate 双算」。
        try:
            htf_res = htf_context(htf)
        except Exception as e:
            htf_res = None
            htf_degraded = True
            logger.warning("%s 4h 上下文计算异常（按数据降级处理）: %s", symbol, e)
        else:
            # 仅"正常返回"时判定主动否决：reject_reason 非空（走平/缠绕/数据/
            # ADX/方向歧义）或 core 不足返回 None → gate_4h 关卡；其中 data/adx
            # 与 core 不足属数据降级（计入 htf_degraded），走平/缠绕/方向歧义属
            # 市场状态（不计降级）
            if htf_res is None or htf_res.get("reject_reason"):
                htf_degraded = (
                    htf_res is None
                    or htf_res.get("reject_reason") in ("data", "adx")
                )
                _reject("gate_4h")
                if reject_out is not None:
                    if htf_degraded:
                        reject_out["degraded"] = True
                    reject_out["htf"] = _htf_snapshot(htf_res, htf_degraded)
                return None, ({"op": "expire", "reason": "ranging"} if pending else None)
        # 4h 状态机双向禁开（2026-10-20 收紧为放行排列；2026-10-29 放宽加入恢复
        # 初期）：block_both（不在放行白名单）时，回踩·突破候选均一票否决——与
        # 闸门同属「前置筛选」层（4h 决定做不做，1h 负责命中与待确认）。
        # tangle 已由上方 reject_reason 先行否决（统一判据），此处实际拦 turn_*
        # 拐头、macro_cross_* 交叉初期与 bull_dip/bear_rally 过渡态；命中即直接
        # 不看：不跑票数、不触发、不进候选，活跃 pending 同轮 expire(htf_state_lock)。
        # 异常/数据不足（htf_res=None）不在此判，由下方方向关 fail-closed 收口。
        if htf_res is not None and htf_res.get("block_both"):
            _reject("htf_state_lock")
            if reject_out is not None:
                # 只留状态名：口径说明由前端关卡 hint 统一承载，避免抽屉里同一句话出现两遍
                reject_out["detail"] = (
                    htf_res.get("state_label") or htf_res.get("state") or ""
                )
                reject_out["htf"] = _htf_snapshot(htf_res)
            return None, ({"op": "expire", "reason": "htf_state_lock"} if pending else None)
        # 4h 状态机放行（2026-10-29）：闸门与状态锁都通过的币种记下状态名，
        # 供漏斗抽屉「通过」按放行状态分组（排列 / 恢复初期各一组）；后续关卡
        # （票数/触发/质量分）是否拦掉不影响这里——它记的是 4h 这一关的口径。
        if htf_out is not None and htf_res is not None:
            htf_out["state"] = htf_res.get("state")
            htf_out["state_label"] = htf_res.get("state_label")
    else:
        # 4h K线为空（数据源返回空/不足）：与拉取失败同口径，计入数据降级
        htf_degraded = True

    # 4h 方向权威（原 gate_4h 的 long/short；合并后即 htf_context.direction）。
    # htf_res=None（数据降级）时无方向 → 方向职责拆分后 fail-closed，不回落到
    # 1h 定向（见下方 gate_dir 分支）
    gate_dir = htf_res.get("direction") if htf_res is not None else None

    # 投票单趟计算（2026-10-10 精简；2026-10-15 指标拆角色，docs/21 O9）：
    # 方向票仅 MACD/布林带/RSI 三张（technical.DIR_VOTE_SHORTS），门槛改为
    # 「方向票同向 ≥DIR_VOTE_MIN 张」——不再要求 7 票里净差 ≥3（滞后指标
    # ADX/OBV/SuperTrend 与 4h背景退出方向票，改由 range_score/质量分承担），
    # 消除大量有效回踩死在这一关的问题。net 仍为方向票净差（符号=方向）。
    # 4h 背景复用第一步的 htf_bg，指标不再双算
    votes = compute_indicator_votes(klines, htf_bg=htf_res)

    # 定向（2026-10-21 方向职责拆分，docs/需求文档/06.../回踩命中改造方案 §3.2）：
    # 4h 定方向，1h 三票只作「确认度」。回踩是趋势内回调，1h 动量在此期间必然反向；
    # 若仍以净差定向，回踩永远与 4h 方向相反 → 深回撤触点被结构性消灭
    # （反事实重放：EMA144 924 次几何命中仅存活 1 次）。因此回踩与突破统一按 4h
    # 方向定向，突破的动量确认后移到「触发之后」按类型分流校验。
    # 4h 无方向（拉取/计算异常降级）→ fail-closed 直接拒绝，不回落到 1h 定向。
    if gate_dir is None:
        _reject("gate_4h")
        if reject_out is not None:
            reject_out["detail"] = "4h 方向缺失（数据降级），无趋势依据不出信号"
            if htf_degraded:
                reject_out["degraded"] = True
            reject_out["htf"] = _htf_snapshot(htf_res, htf_degraded)
        return None, ({"op": "expire", "reason": "direction_flip"} if pending else None)

    direction = 1 if gate_dir == "long" else -1
    same_n = (votes["bull_n"] if direction == 1 else votes["bear_n"]) if votes else 0
    opp_n = (votes["bear_n"] if direction == 1 else votes["bull_n"]) if votes else 0
    # 突破用的动量确认：同向票足够=通过；反向票足够=方向对撞（回踩不消费这两个标志）
    votes_ok = same_n >= DIR_VOTE_MIN
    votes_flip = opp_n >= DIR_VOTE_MIN

    # 4h 状态机逆势锁（2026-10-19 用户拍板）：STATE_LOCK_CONTRA（宏观同侧六态：
    # macro_cross_* / *_recover / aligned_*）时，逆状态的候选一票否决。2026-10-20
    # 前置门收紧到「仅排列放行」后，macro_cross_* 在 block_both 层已被拦；2026-10-29
    # 恢复初期（*_recover）放行后重新到达本关，与 aligned_* 一样把通过集锁到其自身
    # 方向。与 STATE_BLOCK_BOTH 同属「前置筛选」层，但需先定向才能判逆势，故放在方向派生
    # 之后。置于方向冲突关「之后」：gate_4h 与定向已一致时，再核对 4h 排列结构
    # （EMA21/55/144 状态机）——二者不一致（如宏观死叉但价格仍在 EMA144 上方）
    # 是方向冲突关的盲区，本关补齐。命中即直接不看：不触发、不进候选，活跃
    # pending 同轮 expire(htf_state_lock)。
    # 目的：4h 死叉/空头排列时不再产出看涨金K（看涨吞没/刺透等）候选——
    # 金K方向只跟 1h 方向票走，此前 4h 排列不参与，逆势金K会落到命中列表。
    # 2026-10-21 起 AI 层不再做方向兜底，本前置门是唯一 4h 方向限制。
    # 异常降级（htf_res=None）放行，与
    # gate_4h / STATE_BLOCK_BOTH 同口径。过渡态 bull_dip/bear_rally 已在 block_both
    # 层被拦（2026-10-20 前置门收紧），不再到达本关。
    if (htf_res is not None and htf_res.get("state") in STATE_LOCK_CONTRA):
        st_dir = htf_res.get("dir") or 0
        if st_dir and st_dir != direction:
            _reject("htf_state_lock")
            if reject_out is not None:
                reject_out["detail"] = (
                    f"{htf_res.get('state_label') or htf_res.get('state')}"
                    f"（宏观同侧）锁逆势——仅可{'多' if st_dir > 0 else '空'}头，"
                    f"当前定向={'多' if direction == 1 else '空'}头")
                reject_out["htf"] = _htf_snapshot(htf_res)
            return None, ({"op": "expire", "reason": "htf_state_lock"} if pending else None)

    # 第二步：1H 结构门——不在筛选链（2026-10-12，docs/21 O7 退役筛选链硬门）：
    # 不再整体否决信号（s1 可见信号池恢复），结构判定收敛为 trigger 层 1h EMA21
    # 触点的软校验；2026-10-15（docs/21 O9）再降级为「不过门不否决、仅质量分重罚」
    # ——trigger 带 structure_gate 证据，scanner 透传进 compute_quality_score。
    # flt.structure_1h_gate 开关随之退役（scan_filters 字段保留向后兼容，不再消费）。

    # 第三步：震荡闸门（2026-10-20 重构，原 8 项评分卡「≥3」退役）：
    # 4 项合取判据任一命中即不入场——1h 三线交错 / 55-144 分离史过近 /
    # ADX<18 / 波动过度扩张（Donchian 或布林带宽分位 ≥0.8）。判据来自方向感知
    # 近锚止损 PNL 回放：原评分卡放行池净期望 −0.02R（打平偏亏），新判据
    # 全样本 +0.10R 且 train/test 双段为正。详见 ema_gate.range_score
    # 2026-10-21：只计算不即刻拒绝——回踩确认改为「降权不拒」（深回撤本身就会让
    # 1h 三线收敛，硬拒等于把深回撤触点二次消灭，重放中 EMA144 通过方向门的 161
    # 次里 160 次死在本关）；突破类保持硬拒，判定在触发后分流执行。
    rs, _rs_reasons = range_score(
        klines, gate_ev=htf_res.get("evidence") if htf_res else None)

    # htf 直传触点体系（2026-10-09）：ema21_4h 跨周期触点与第 7 票/闸门共用
    # 同一份 4h 快照，单轮单币不重复拉取
    whitelist_reject = False   # 白名单外触发标记（漏斗区分 trigger_whitelist/no_trigger）
    # 未触发原因采集（2026-10-13，漏斗「无触发」判定说明）：reject_out 在场才开，
    # trigger 侧向 diag["reasons"] 追加卡点文本（watchlist 等无漏斗调用零开销）
    trig_diag: Optional[dict] = {} if reject_out is not None else None
    trig, pending_op = detect_trigger(
        klines, direction, pending, flt=flt, retest=retest, htf=htf,
        # 4h 方向传给触点层：ema144 深回撤门靠真实 4h 趋势方向判定；闸门降级
        # （htf 拉取失败/异常 → gate_dir=None）时该触点 fail-closed 不出（2026-10-16 加固）
        htf_dir=gate_dir,
        diag=trig_diag,
    )
    # 触发后分流校验（2026-10-21 方向职责拆分）：突破是动量事件，仍要求 1h 三票
    # 同向（原 votes 关 + 方向冲突关原义保留）；回踩确认不否决——回撤期间票反向
    # 是形态本身，反向票只进质量分与仓位折扣。
    range_penalty = False
    if trig is not None and trig["type"] in (BREAKOUT, TREND_BREAKOUT):
        if flt.direction_vote_gate_enabled and not votes_ok:
            _reject("votes")
            if reject_out is not None and votes:
                detail = _votes_detail(votes)
                if detail:
                    reject_out["detail"] = detail
            return None, pending_op
        if flt.direction_conflict_gate_enabled and votes_flip:
            _reject("direction_flip")
            if reject_out is not None:
                reject_out["detail"] = (
                    f"突破方向={'多' if direction == 1 else '空'}头，但1h方向票"
                    f"{opp_n}张反向——突破需动量同向")
            return None, pending_op

    # 震荡闸门分流：突破硬拒；回踩降权不拒（质量分 -8，见 compute_quality_score）。
    # 回踩三条路径都必须放行：① 当根/次根金K确认（trig=PULLBACK_CONFIRM）；
    # ② 触碰当根尚无金K、正要进待确认池（trig=None + pending upsert）——
    #    若在 ② 硬拒，两段式回踩根本进不了池，① 的降权永远不可达；
    # ③ 次根确认的等待小时（trig=None + 活跃 pending + trigger 无处置动作）——
    #    深回撤常伴随 ADX 走低，若在等待根硬杀 pending，T+2 金K 将因无 pending
    #    无法次根确认（2026-10-21 审查补漏；触发侧 expire〔4h 翻转/窗口耗尽〕
    #    是趋势/窗口死亡，不属此列，照常终态）。
    if rs >= RANGE_SCORE_REJECT:
        pullback_path = (
            (trig is not None and trig["type"] == PULLBACK_CONFIRM)
            or (trig is None and (
                bool(pending_op and pending_op.get("op") == "upsert")
                or (pending_op is None and bool(pending))
            ))
        )
        if pullback_path:
            range_penalty = True
        else:
            _reject("range_score")
            # 死因保留：触发侧已有处置动作（expire(direction_flip/window_expired)/
            # supersede）时原样返回，不用 range_score 覆盖审计死因
            return None, (pending_op or
                          ({"op": "expire", "reason": "range_score"} if pending else None))

    if trig is not None:
        # T1 突破 → 破轨事件动作（docs/22 §4）：白名单过滤前、类型校验之后记录——
        # 破轨是市场事实，触发类型白名单只决定是否产生突破信号，不影响回测触点事实源
        if trig["type"] in (BREAKOUT, TREND_BREAKOUT) and event_out is not None:
            event_out.update({
                "op": "upsert",
                "direction": direction,
                "level_price": trig.get("breakout_level"),
                "candle_open": trig.get("candle_open"),
            })
        if trig["type"] not in flt.allowed_triggers:
            # 触发类型白名单（2026-09-28 筛选配置）：白名单外类型不产生候选（连 AI
            # 分析都不进）；pending 处置照常执行——如突破抢先 supersede、pending
            # 窗口过期 expire，让回踩池状态机保持自洽
            trig = None
            whitelist_reject = True
    if trig is None:
        # 进待确认池准入闸（2026-10-13 用户拍板）：待确认池的产物类型恒为回踩确认
        # （trigger_pullback_confirm），故该类型须在触发类型白名单内——否则次根确认
        # 成立也产生不了候选，进池只是挂空账。命中即挡在池外：漏斗记关卡 + 判定
        # 说明、丢弃 upsert；仅挡新进池，已有 pending 的 confirm/expire/supersede
        # 生命周期不受影响。（4h 状态机双向禁开已由上方前置门对全部候选一票否决，
        # 无需在此单列。）
        if pending_op and pending_op.get("op") == "upsert":
            if PULLBACK_CONFIRM not in flt.allowed_triggers:
                _reject("trigger_whitelist")
                if reject_out is not None:
                    reject_out["detail"] = (
                        "回踩确认类型未在触发类型白名单内（筛选配置）——不进待确认池")
                return None, None
        # 行5：触碰无金K → upsert pending；补 Stage-1 快照（质量分按回踩确认口径计，
        # 触碰根即当前信号根 klines[-2]：量能走「放量 +5」加分，跳过量能语义——
        # 与当根确认同规则，避免同一根上放量加分与放量/缩量回踩语义冲突）。
        # structure_gate：ema21 触点未过 1H 结构门（2026-10-15 降级为重罚）随
        # pending 快照进质量分，让待确认池也按结构合规度排序
        if pending_op and pending_op.get("op") == "upsert":
            _stab = pullback_stabilize_bonus(klines, direction, -2, volume_semantics=False)
            pending_op["quality_score"] = compute_quality_score(
                direction, votes, PULLBACK_CONFIRM, klines=klines, stabilize=_stab,
                structure_gate=bool(pending_op.get("structure_gate", True)),
                range_penalty=range_penalty,
            )
            pending_op["votes_json"] = _votes_snapshot(votes, stabilize=_stab)
        elif reject_out is not None:
            # 无触发且未进待确认池 → 漏斗记录（upsert 进池不算被拒）。
            # 1H 结构门 2026-10-15 起不再否决（降级为质量分重罚），
            # 故触发层不再产生 expire(structure_1h)，此分支无需单列：
            # trigger.py 已无 structure_1h 死因，故此处只区分白名单/无触发
            if whitelist_reject:
                _reject("trigger_whitelist")
            else:
                # 无触发明细（2026-10-13）：trigger 侧采集的卡点文本段，分号连接
                # 供前端判定说明列拆行；无采集内容（旧路径）不写 detail
                # 关卡代码细分（2026-10-27，docs/16 §6.3 P1-3）：trigger 侧把
                # 未触发原因归一到 diag["stage"]（no_touch / leg_gate /
                # golden_no_context / pending_waiting / window_expired /
                # pending_flip / insufficient_bars / no_golden），这里直接作为
                # 关卡代码落库——漏斗按类计数，不再全部塌进 no_trigger。
                _stage = (trig_diag or {}).get("stage") or "no_trigger"
                _reject(str(_stage))
                reasons = trig_diag.get("reasons") if trig_diag else None
                if reasons:
                    reject_out["detail"] = "；".join(reasons)
        return None, pending_op

    # 企稳加分项证据（docs/19 §3.3，trigger 回踩路径产出）：进质量分 + votes 快照；
    # pattern 透传供看涨吞没上影短加分（2026-10-13 硬门改加分项）；
    # structure_gate 透传 1H 结构门结果（2026-10-15 降级为重罚，不过门照常出信号）
    stabilize = trig.get("stabilize")
    # 质量分分项（2026-10-21，T0-3/T1-2）：随候选落库，提示词渲染与复盘拆解
    q_parts: dict = {}
    quality = compute_quality_score(direction, votes, trig["type"], klines=klines,
                                    stabilize=stabilize, pattern=trig.get("pattern"),
                                    golden_combo=bool(trig.get("golden_combo")),
                                    swing_order=flt.swing_order,
                                    structure_gate=bool(trig.get("structure_gate", True)),
                                    range_penalty=range_penalty,
                                    parts_out=q_parts)
    # 反向腿深度观测（2026-10-28，docs/16 §23）：腿门有门槛（min_bars/min_atr），
    # 但"过门"不区分"深回踩"与"贴位几乎不动的浅回踩"（TAUSDT 0.94ATR 即过线）。
    # 把实际深度写进分项，成交后即可按 leg_atr 分档回看哪一档该被拒。
    # 仅在 compute_quality_score 之后写入，不参与 total 计算。
    _leg = trig.get("leg_evidence")
    if isinstance(_leg, dict) and _leg.get("bars", 0) > 0:
        try:
            q_parts["leg_atr"] = round(float(_leg.get("atr_mult") or 0.0), 2)
            q_parts["leg_bars"] = int(_leg.get("bars") or 0)
        except (TypeError, ValueError):
            pass
    return {
        "signal_type": trig["type"],
        # 候选产生时刻（2026-10-21 观测 B1）：信号新鲜度起点。扫描是整轮结束后
        # 批量落库（ScanResult.created_at 混入整轮耗时），故逐币在此取时刻
        "detected_at": datetime.utcnow(),
        # 现价 = 未收盘K线最新价（仅作入场价差参考）：信号判定（投票/触发/形态/量能）
        # 全部基于「信号K线」= klines[-2]。非尾窗下它是最后一根已收盘K线（与扫描
        # 时刻在小时内的位置无关，同根K线同结果）；尾窗幽灵视图下它是近收盘未走完
        # 根的快照（OHLCV 为扫描时刻值，收盘后可能继续变化）——见 screen_klines 与
        # risk_guard 的 signal_bar_provisional 处理（2026-10-12）
        "current_price": float(klines[-1][4]),
        "breakout_pct": trig["breakout_pct"],
        "trend_slope": 0.0,
        "r_squared": 0.0,
        "pattern": trig.get("pattern"),
        # 组合确认口径（2026-10-26，docs/11）：一律按 trigger 字段透传，不按
        # 规则号/note 猜测——规则2 双流星形态在 -2（无 entry_lag_pct），规则3
        # 前根形态才在 -3，而 -3 也可能来自「当根触碰确认」
        "golden_combo": bool(trig.get("golden_combo")),
        "pattern_candle_open": trig.get("pattern_candle_open"),
        "entry_lag_pct": trig.get("entry_lag_pct"),
        "signal_reason": trig["reason"][:120],
        "position": trig["position"],
        "touch_ref": trig.get("touch_ref"),
        "position_ref": trig.get("position_ref"),
        "strength": quality / 100.0,
        "ema_state": ema["state"] if ema else None,
        "volume": vol,
        "volume_type": vol_type,
        "quality_score": quality,
        "quality_parts": q_parts,
        "votes": _votes_snapshot(votes, stabilize=stabilize),
    }, pending_op


def _votes_snapshot(votes: Optional[dict], stabilize: Optional[dict] = None) -> dict:
    """指标票数快照（含 4h 背景级别，前端「4h方向」列展示；旧行无此键显示 -）。
    stabilize：回踩企稳加分项证据（docs/19 §3.3 条件2/3，可 None）——
    落 votes_json["stabilize"] 供复盘审计（加分名目/触碰根量比/MACD 零轴态）。
    votes=None（2026-10-18 方向票关关闭后指标全不可用、4h 闸门兜底定向的罕见
    路径可达）：返回中性空票快照，避免下游 KeyError/TypeError"""
    if not votes:
        snap = {"bull": [], "bear": [], "bull_n": 0, "bear_n": 0, "net": 0, "voted": 0}
        if stabilize:
            snap["stabilize"] = stabilize
        return snap
    snap = {
        "bull": votes["bull"], "bear": votes["bear"],
        "bull_n": votes["bull_n"], "bear_n": votes["bear_n"],
        "net": votes["net"], "voted": votes["voted"],
    }
    if stabilize:
        snap["stabilize"] = stabilize
    htf_res = dict(votes["results"]).get(HTF_SHORT)
    if htf_res:
        snap["htf"] = {
            "state": htf_res.get("state"),
            "label": htf_res.get("state_label"),
            "dir": int(htf_res.get("dir") or 0),
            "macro_aligned": htf_res.get("macro_aligned"),
            # EMA 状态机 11 态（2026-10-11 再重构，docs/17）：tangle/turn_*/
            # macro_cross_*/*_recover/aligned_*/bull_dip/bear_rally，
            # 前端「4h方向」列短标签用；旧行无此键回退 label 解析
        }
    return snap


def apply_quality_floor(hits: list[dict], floor: float) -> tuple[list[dict], list[str]]:
    """质量分下限过滤（2026-10-27，docs/16 §5.1 P0-1）。

    只做"是否成为候选"这一步：被剔除的命中不再进 AI 配额排序与开单池，
    但 pending 的 upsert 与命中历史展示不受影响（它们是触点事实，不是候选）。
    `quality_score` 缺失按 0 处理（旧数据无分=最低分，与排序口径一致）。
    返回 (保留列表, 被剔除的 symbol 列表)。
    """
    if floor <= 0 or not hits:
        return hits, []
    kept: list[dict] = []
    dropped: list[str] = []
    for h in hits:
        if float(h.get("quality_score") or 0) < floor:
            dropped.append(str(h.get("symbol") or ""))
        else:
            kept.append(h)
    return kept, dropped


class Scanner:
    """扫描编排器（同步 + 线程池并发）"""

    def __init__(self):
        self.pool = ExchangePool()
        self.concurrency = settings.BINANCE_CONCURRENCY

    def run(self, scan_record_id: UUID) -> Optional[str]:
        """执行扫描。返回 AI 分析跳过说明（None=正常分发分析/无需说明），
        供任务记录 summary 展示「满仓跳过 AI 分析」（2026-10-04）。"""
        db = SessionLocal()
        status = "failed"
        hit_count = 0
        error_count = 0
        error_symbols: list[str] = []
        coin_count = 0
        ai_skip_note: Optional[str] = None
        funnel: Optional[dict] = None   # 筛选漏斗（正常完成才组装落库）
        try:
            record = db.get(ScanRecord, scan_record_id)
            if not record:
                logger.error("扫描记录不存在: %s", scan_record_id)
                return

            # 从数据库读取扫描策略配置
            cfg = _get_scan_config(db)
            kline_interval = cfg.kline_interval
            kline_window = cfg.kline_window
            repeat_window_hours = cfg.repeat_window_hours
            # 扫描尾窗（2026-10-11-4）：beat 定在每小时第 SCAN_BEAT_MINUTE 分，
            # 尾窗内把未收盘根视作已收盘参与筛选（信号根前移一根，提前数分钟
            # 捕捉）；尾窗内 K线缓存可能是小时内早期快照（图表浏览/大盘预取
            # 写入），必须强刷保数据新鲜。2026-10-12 beat 改整点 :00 后尾窗
            # 关闭（FORMING_TAIL_MINUTES=0，恒 False）——信号根 = 上一根已收盘
            # 根，不清缓存快照；整点取数竞态（新周期根尚未出现在响应里、末根
            # 实为刚收盘根）由 screen_klines「末根已收盘」检查兜底（2026-10-13），
            # 不清缓存也能判对信号根
            #
            # 基准时刻整轮取一次（2026-10-12）：scan_one 内若各自重新读时钟，
            # 跨整点的长轮次会前半批用幽灵快照、后半批用最终收盘根——同一轮
            # 内同一根K线两种基准、结果不可复现。scan_now 透传给 screen_klines
            scan_now = datetime.now(timezone.utc)
            tail_window = md.in_forming_tail(kline_interval, scan_now)
            # 筛选参数组（scan_filters JSONB > env > 代码默认）
            flt = resolve_scan_filters(cfg)

            # 尾窗状态落日志（2026-10-12）：beat 分钟数与 FORMING_TAIL_MINUTES
            # 漂移、或扫描落在非预期分钟时，只看这行即可判定筛选口径是否生效
            logger.info(
                "开始扫描 #%s (interval=%s, window=%d, 尾窗=%s, 基准=%s)",
                scan_record_id, kline_interval, kline_window,
                "是" if tail_window else "否",
                (scan_now + timedelta(hours=8)).strftime("%H:%M:%S"),  # 北京时间
            )

            # 1. 获取合约交易对列表（按 24h 成交额降序）
            symbols_data = self.pool.get_usdt_swap_symbols_with_volume()

            coin_count = len(symbols_data)
            # 通过者反推（2026-10-29）：只落全量币池符号（≈500 个字符串），前端按
            # 「币池 − 本关及之前被拒 − 异常」还原每关通过名单；逐关存通过集合会把
            # funnel_json 撑大一个量级，且与 rejects 重复。
            universe_symbols = sorted(s["symbol"] for s in symbols_data)
            logger.info("共 %d 个合约交易对待扫描（按24h成交额降序）", len(symbols_data))

            # 筛选漏斗明细（2026-10-11）：symbol → 卡住的关卡代码。预过滤 5 关 +
            # 指标管线各关（reject_out 旁路）统一收口，扫描结束组装 funnel_json。
            # funnel_details：判定说明文本（votes 全票明细 / no_trigger 未触发
            # 原因，2026-10-13；其余关卡无）
            funnel_rejects: dict[str, str] = {}
            funnel_details: dict[str, str] = {}
            # 4h 数据降级币种（2026-10-21 观测 B2）：指标管线旁路标记，扫描结束
            # 汇总进 funnel_json，按天统计降级占比（spec §5.3）
            htf_degraded_symbols: set[str] = set()
            # 4h 前置门证据（2026-10-21 第 2 步）：symbol → {stage, state, ...}，
            # 扫描结束聚合成状态分布/死因分布进 funnel_json
            htf_evidence: dict[str, dict] = {}
            # 4h 放行状态（2026-10-29）：symbol → 状态名（多头排列/空头恢复初期…），
            # 只收通过 gate_4h + htf_state_lock 的币，供抽屉「通过」按状态分组
            htf_pass_states: dict[str, str] = {}
            # 被 4h 前置门拦下的候选快照（第 2 步）：仅定时扫描落 shadow_marks，
            # 供第 3 步反事实回放评估「仅放行 aligned」是否划算
            blocked_shadows: dict[str, dict] = {}

            def _drop(rows: list[dict], keep, stage: str) -> list[dict]:
                """预过滤关卡拆分：保留侧照常流转，剔除侧记入漏斗"""
                kept = [s for s in rows if keep(s)]
                for s in rows:
                    if not keep(s):
                        funnel_rejects[s["symbol"]] = stage
                return kept

            # 24h 成交额过滤：流动性差的合约不进扫描（省K线请求，剔除不可交易标的）
            min_vol = flt.min_volume_24h
            before = len(symbols_data)
            symbols_data = _drop(
                symbols_data, lambda s: float(s.get("volume_24h") or 0) >= min_vol,
                "liquidity")
            logger.info(
                "24h成交额 ≥ %s USDT 过滤：%d → %d",
                f"{min_vol:,.0f}", before, len(symbols_data),
            )

            # C0 下单环境可交易过滤已撤（2026-10-11 用户拍板）：未上架币照常
            # 进扫描/AI 分析（数据源走正式网，K 线齐全），AI 配额换取全市场
            # 可见性；可交易校验收敛到开单时逐网进行（trade_engine._attempt_open
            # listed 兜底 + 未上架飞书提示，正式网有则正常开单）。原实现按
            # 「启用自动交易的网」拉 exchangeInfo 并集预过滤（2026-09-19 ARX
            # 案例：开单 -1121 白烧配额——该防线保留在开单层）

            # C0' 新币冷静期（2026-10-04 NIGHT 案例，用户拍板默认 14 天可配）：
            # 上市未满 N 天的合约不进扫描。新币盘口深度极不稳定——开仓时刻滑点
            # 实测达标 ≠ 止损出场（触发=快速砸盘时刻）不滑爆，NIGHT 出场滑点
            # 1.98% 致实亏 234% R；近两天超预算自动拉黑的单子全部是上市 1-2 天
            # 的新合约。onboardDate 与 listed 同源 exchangeInfo（零额外请求语义），
            # 数据源 live 优先（与 C 段 risk_tier 同口径）；缺表/缺该键放行不误拦；
            # 冷静期 0=关闭；scan_filters.new_coin_cooldown_days 可覆盖（DB>env>默认）
            cooldown_days = flt.new_coin_cooldown_days
            if cooldown_days > 0:
                onboard = BinanceTrader(False).onboard_dates()
                if onboard:
                    cutoff_ms = (time.time() - cooldown_days * 86400) * 1000
                    before = len(symbols_data)
                    symbols_data = _drop(
                        symbols_data,
                        lambda s: (od := onboard.get(s["symbol"])) is None or od <= cutoff_ms,
                        "new_coin",
                    )
                    logger.info(
                        "新币冷静期过滤（上市 <%d 天）：%d → %d",
                        cooldown_days, before, len(symbols_data),
                    )

            # C 交易所风险档位过滤（2026-09-18）：最大允许杠杆 <10× 的币不进候选
            # （交易所对其流动性/风险的终极标注；4USDT 案例）。注意不能按首档 MMR
            # 过滤——币安 2026 已把多数山寨首档 MMR 统一调到 1.5%~2%（首档名义
            # 5k~10k 内 MMR 均为此值，BTC 0.4% 是孤例），按 MMR 过滤会误拦 58%。
            # 档位属市场全局配置，数据源按正式网优先（2026-10-03 删环境切换：
            # BinanceTrader(False)=live 优先回落测试网）；一次拉取缓存 24h，
            # 失败（空表）放行不阻断
            risk_tiers = BinanceTrader(False).risk_tier_all()
            if risk_tiers:
                before = len(symbols_data)
                symbols_data = _drop(
                    symbols_data,
                    lambda s: (rt := risk_tiers.get(s["symbol"])) is None or rt[1] >= 10,
                    "risk_tier",
                )
                logger.info("风险档位过滤（最大杠杆 <10×）：%d → %d", before, len(symbols_data))

            # D 黑名单过滤（2026-09-18）：手动添加（永久）/ 连续止损自动加入（冷却期），
            # 命中不进扫描。集合为空时跳过查询
            from app.services.blacklist_service import active_symbols
            blacklisted = active_symbols(db)
            if blacklisted:
                before = len(symbols_data)
                symbols_data = _drop(
                    symbols_data, lambda s: s["symbol"] not in blacklisted, "blacklist")
                logger.info("黑名单过滤（%d 个生效）：%d → %d", len(blacklisted), before, len(symbols_data))

            # 构建 symbol -> volume 映射
            volume_map = {s["symbol"]: s["volume_24h"] for s in symbols_data}
            symbols = [s["symbol"] for s in symbols_data]

            # 2. 线程池并发扫描每个币种（指标驱动筛选，见模块 docstring）
            hits: list[dict] = []
            # 两段式回踩确认（docs/19）：批量预取活跃 pending（每轮一次查询，勿逐币查），
            # 扫描线程只读快照，处置动作（upsert/expire/confirm/supersede）收集后统一落库
            pendings = self._prefetch_pendings(symbols)
            pending_ops: dict[str, dict] = {}
            # 破轨事件（docs/22 §4）：回测触点事实源，预取/动作收集后统一落库
            retests = self._prefetch_retest_events(symbols)
            event_ops: dict[str, dict] = {}

            def scan_one(symbol: str) -> tuple[str, list[dict], bool]:
                """方向票定向 → 两段式触发确认 → 质量分。返回 (symbol, [候选...], is_error)"""
                try:
                    # EMA144/指标暖机需要足够历史，至少拉 300 根；
                    # 尾窗内强刷（缓存可能是小时内早期快照，信号根必须新鲜）
                    klines = self.pool.get_klines(
                        symbol,
                        interval=kline_interval,
                        limit=max(kline_window, 300),
                        force_refresh=tail_window,
                    )
                    # 幽灵尾根视图（2026-10-11-4；2026-10-13 扩整点竞态）：尾窗内
                    # 近收盘根，或整点取数竞态下末根实为刚收盘根（新周期根尚未出
                    # 现在响应里）→ 末根视作信号根；均不满足原样返回（信号根=上
                    # 一根已收盘）。now 用轮次基准 scan_now（2026-10-12）：不重读
                    # 时钟，避免跨整点时同一轮内两种数据基准
                    klines = md.screen_klines(klines, kline_interval, scan_now)
                    ev_out: dict = {}
                    rv_out: dict = {}   # 漏斗旁路：单币拒绝关卡（cand=None 时读）
                    htf_out: dict = {}  # 漏斗旁路：4h 放行状态（2026-10-29）
                    cand, pop = detect_indicator_candidate(
                        self.pool, symbol, klines, pending=pendings.get(symbol),
                        flt=flt, retest=retests.get(symbol), event_out=ev_out,
                        reject_out=rv_out, htf_out=htf_out, now=scan_now,
                    )
                    # 破轨事件动作（T1 触发记录 / 回测触点确认消费）
                    if ev_out.get("op"):
                        event_ops[symbol] = ev_out
                    # 4h 状态机放行币种 → 状态名（抽屉「通过」按状态分组）
                    if htf_out.get("state_label"):
                        htf_pass_states[symbol] = htf_out["state_label"]
                    # 4h 数据降级标记（B2）：4h K线缺失/计算异常/数据类拒因
                    if rv_out.get("degraded"):
                        htf_degraded_symbols.add(symbol)
                    if pop:
                        if pop.get("op") == "confirm" and cand:
                            pop["confirm_pattern"] = cand.get("pattern")
                        # 次根确认消费：pending 的 Stage-1 是回测触点 → 事件 consumed
                        if (pop.get("op") == "confirm"
                                and (pendings.get(symbol) or {}).get("touch_ref") == TOUCH_RETEST):
                            event_ops[symbol] = {"op": "consume"}
                        # 无活跃 pending 时的 expire 仅为漏斗归因，不落 pending
                        # 处置——避免对无 pending 币种逐币空查询
                        if not (pop.get("op") == "expire" and pendings.get(symbol) is None):
                            pending_ops[symbol] = pop
                    # 当根确认消费：本次回踩金K命中的触点是回测位 → 事件 consumed
                    if (cand and cand.get("signal_type") == PULLBACK_CONFIRM
                            and cand.get("touch_ref") == TOUCH_RETEST):
                        event_ops[symbol] = {"op": "consume"}
                    if cand is None:
                        # 进待确认池（upsert）不算被拒，漏斗只记真拒绝关卡
                        if rv_out.get("stage"):
                            funnel_rejects[symbol] = rv_out["stage"]
                            if rv_out.get("detail"):
                                funnel_details[symbol] = rv_out["detail"]
                            # 4h 前置门证据 + 被拦快照（第 2 步）：只收
                            # gate_4h / htf_state_lock 两关（其余关卡与 4h 无关）。
                            # 快照记信号根（klines[-2]，已收盘）开盘时间与收盘价；
                            # scanner 层无入场/止损/TP，收益估值留待第 3 步按
                            # 前瞻收益口径回放
                            htf_ev = rv_out.get("htf")
                            if htf_ev and rv_out["stage"] in (
                                    "gate_4h", "htf_state_lock"):
                                htf_evidence[symbol] = {
                                    "stage": rv_out["stage"], **htf_ev}
                                if len(klines) >= 2:
                                    blocked_shadows[symbol] = {
                                        "stage": rv_out["stage"],
                                        "htf": htf_ev,
                                        "detail": rv_out.get("detail"),
                                        "signal_bar_open_ms": int(klines[-2][0]),
                                        "close": float(klines[-2][4]),
                                        # 触点/金K解析（2026-10-27，docs/16 §16.3 D1）：
                                        # 4h 门先于触点探测，不补这一步，反事实回放
                                        # 只能事后重算链路（实测复现率 2.4%）。这里用
                                        # 与真实信号同一套探测口径当场算，随快照落库。
                                        "probe": _blocked_probe(
                                            klines, flt, retests.get(symbol)),
                                    }
                        return symbol, [], False
                    cand["symbol"] = symbol
                    # 影子打标（docs/14）：特征计算挂在此处（klines 在手），
                    # 值随候选透传给 _save_results 落 shadow_marks；计算失败
                    # 影子内部吞掉，不影响扫描管线。不写回任何决策字段
                    try:
                        from app.services import shadow as shadow_mod
                        shadow_vals: dict = {}
                        for f in shadow_mod.SHADOW_FEATURES.values():
                            # 关闭的卡不再打标（docs/14 §四 enabled，2026-10-13）
                            if not f.enabled:
                                continue
                            if f.key == "sweep_reclaim":
                                # CARD-08 特例：需生产原生触点位（pending 锚点/
                                # retest 事件）与 15m 拉取，非 compute(klines) 签名
                                v = shadow_mod.compute_sweep_reclaim_scan(
                                    klines, cand, pending=pendings.get(symbol),
                                    retest=retests.get(symbol), flt=flt,
                                    pool=self.pool)
                            else:
                                v = f.compute(klines)
                            if v is not None:
                                shadow_vals[f.key] = v
                        cand["shadow"] = shadow_vals
                    except Exception as e:
                        logger.debug("影子特征计算异常（忽略）%s: %s", symbol, e)
                    return symbol, [cand], False
                except Exception as e:
                    logger.warning("扫描 %s 失败: %s", symbol, e)
                    return symbol, [], True

            with ThreadPoolExecutor(max_workers=self.concurrency) as executor:
                futures = {executor.submit(scan_one, s): s for s in symbols}
                for future in as_completed(futures):
                    symbol, signals, is_error = future.result()
                    if is_error:
                        error_count += 1
                        error_symbols.append(symbol)
                    for sig in signals:
                        sig["volume_24h"] = volume_map.get(symbol, 0)
                        hits.append({"symbol": symbol, **sig})

            # 2.9 质量分下限（2026-10-27，docs/16 §5.1 P0-1）：低于下限的命中不进
            #     候选池。只影响「能否成为候选（进 AI / 开单池）」，不影响 pending
            #     的 upsert 与命中历史展示。计数写 funnel_rejects，前端「筛选漏斗」
            #     可直接看到被拦数量；0=关闭（默认，零行为变化）。
            _before_floor_hits = hits
            _before_floor = len(hits)
            hits, _floor_dropped = apply_quality_floor(hits, flt.quality_floor)
            if _floor_dropped:
                for _sym in _floor_dropped:
                    funnel_rejects.setdefault(_sym, "quality_floor")
                logger.info("质量分下限 %.0f 拦截 #%s：%d/%d 条候选被剔除",
                            flt.quality_floor, scan_record_id, len(_floor_dropped),
                            _before_floor)

            # 3. 按质量分降序（同分按 24h 成交额）——AI 配额按此顺序取前 N
            hits.sort(
                key=lambda x: (x.get("quality_score", 0), x.get("volume_24h", 0)),
                reverse=True,
            )
            # 3.1 配额分池与池内排名（2026-10-21，T1-2/T2-4 观测）：回踩与突破
            # 在同一配额里竞争，回踩因质量分基数低被结构性挤出（spec F4）。
            # 这里只记录「若按类型分池，每个候选在本池的名次」，不改变实际
            # 分发顺序（旧分配仍是质量分全局前 N）；复盘据此看回踩分布。
            _pool_rank: dict[str, int] = {}
            for _h in hits:
                _p = ("pullback"
                      if _h.get("signal_type") == "trigger_pullback_confirm"
                      else "breakout")
                _pool_rank[_p] = _pool_rank.get(_p, 0) + 1
                _h["quota_pool"] = _p
                _h["quota_rank"] = _pool_rank[_p]

            # 4. 重复命中抑制（仅定时扫描）：同 (symbol, signal_type) 在
            #    repeat_window_hours 内已有命中的本轮不落库——持续趋势里 Donchian 突破
            #    每根收盘K线都在创新高，不抑制会同币同信号每轮重复命中。
            #    手动扫描不做抑制（2026-09-20）：手动 = 对最近已收盘K线的完整评估，
            #    与 :02 定时扫描同一根K线应得同样结果；抑制会让手动结果
            #    「看起来完全不同」（实为定时已落库的命中被滤掉）。
            #    AI 成本由指纹缓存（FINGERPRINT_TTL_MIN）兜底，同币同信号不重复调 LLM
            if record.scan_type == "scheduled":
                new_hits, suppressed = self._suppress_repeat_hits(hits, repeat_window_hours)
                if suppressed:
                    logger.info("重复命中抑制 #%s：%d 条已在 %dh 内命中过，不再落库", scan_record_id, suppressed, repeat_window_hours)
            else:
                new_hits, suppressed = hits, 0

            # 5. 写入结果（用独立 session，避免长时间扫描后主 session 连接失效）
            result_ids = self._save_results(scan_record_id, new_hits, scan_type=record.scan_type)

            # 5.1 被 4h 前置门拦下的候选快照（2026-10-21 第 2 步）：仅定时扫描，
            # 供第 3 步反事实回放；失败只记日志不影响扫描与 AI 链路
            self._save_blocked_shadows(
                scan_record_id, blocked_shadows, scan_type=record.scan_type)

            # 5.2 筛选命中飞书通知（2026-09-28，仅定时扫描——手动扫描用户在场，
            # 不推送）。new_hits 已过重复命中抑制，只在真新命中时提醒；
            # 失败只记日志不影响扫描与 AI 分析链路。
            # 2026-09-30 纯文本改消息卡片（_scan_hit_card）
            if record.scan_type == "scheduled" and new_hits:
                try:
                    from app.services.feishu_bot import send_app_card
                    send_app_card(_scan_hit_card(new_hits))
                except Exception:
                    logger.exception("扫描命中飞书通知失败 #%s", scan_record_id)

            # 5.5 两段式 pending 处置（docs/19 §4.2）：确认回写 scan_result_id、
            # upsert/终态落库。放在 _save_results 之后以便 confirm 关联新记录 id
            if pending_ops:
                self._apply_pending_ops(pending_ops, result_ids)

            # 5.5.1 孤儿行清扫（2026-10-10）：跌出本轮宇宙的币（缩量/黑名单/风险
            # 档位/拉K线持续失败等）无人再评估其 pending，行冻结在 pending 永不
            # 终态。设计等待窗 ≤N 根（N=pending_confirm_window_bars，默认 4），
            # 阈值取 N+1 根给「窗口最后一根仍未裁决」留一轮余量，超 N+1 根仍未
            # 终态即为孤儿，直接 expired（window_expired）。全表一条 UPDATE，
            # 每轮都跑（幂等，命中的轮次才写行）
            self._sweep_orphan_pendings(
                kline_interval, flt.pending_confirm_window_bars + 1)

            # 5.6 破轨事件处置（docs/22 §4）：upsert/consume + 窗口过期批处理
            if event_ops:
                self._apply_breakout_events(
                    event_ops, flt.retest_window_bars, kline_interval,
                )

            # 6. 触发 AI 分析（任务内自检开关；批量按24h成交额取前 AI_MAX_PER_SCAN 个）
            # 满仓跳过（docs/06，2026-10-04 按网判定）：定时扫描时正式网 OPENED 数
            # 达 max_open_trades 上限则不做 AI 分析（省 LLM 成本，正式网反正
            # 开不了仓）；测试网不限持仓数（2026-10-15 用户拍板）不参与满仓判定。
            # 手动扫描/手动分析不受限。跳过说明返回给任务记录 summary 展示
            from app.tasks.ai_tasks import run_ai_analysis_task
            try:
                if record.scan_type == "scheduled":
                    full, slots_detail = self._slots_full()
                    if full:
                        ai_skip_note = slots_detail
                        logger.info("定时扫描后跳过本轮 AI 分析 #%s：%s", scan_record_id, slots_detail)
                        # 满仓跳过飞书卡片（2026-10-04）：命中通知发了、AI 没动静
                        # 的静默路径补上推送（仅定时扫描，同命中通知口径）
                        try:
                            from app.services.feishu_bot import send_app_card
                            send_app_card(_ai_skip_card(slots_detail))
                        except Exception:
                            logger.exception("满仓跳过飞书通知失败 #%s", scan_record_id)
                    else:
                        run_ai_analysis_task.delay(str(scan_record_id))
                else:
                    run_ai_analysis_task.delay(str(scan_record_id))
            except Exception as e:
                logger.warning("AI 分析分发失败（忽略）: %s", e)

            hit_count = len(new_hits)
            status = "completed"

            # 筛选漏斗组装（2026-10-11）：各关卡计数 + 被拒明细，随
            # ScanRecord.funnel_json 落库（前端「筛选漏斗」Tab 展示卡在哪一步）。
            # 进待确认池的币不算被拒（pending_pool 单列）；异常轮次不组装（数据不完整）
            pending_pool_n = sum(
                1 for op in pending_ops.values() if op.get("op") == "upsert")
            stages: dict[str, int] = {}
            for st in funnel_rejects.values():
                stages[st] = stages.get(st, 0) + 1
            funnel = {
                "universe": coin_count,        # 全市场合约数（预过滤前）
                "scanned": len(symbols),       # 预过滤后实际进指标管线的币数
                "errors": error_count,
                "candidates": len(new_hits),   # 产生候选（进 AI 分析）数
                "pending_pool": pending_pool_n,
                # 复合金K确认数（docs/16 §12.4/§13，2026-10-27）：14 天 3,330 条
                # 命中里 golden_combo=True 只有 1 条，离线复算却证明规则可达
                # （30 币 × 10,200 根 → 24 次复合、直通 3,927 次，占 0.6%）。
                # 落计数是为了分清"规则太窄"和"写库漏了"，不再靠猜。
                "combo_confirm": sum(1 for h in new_hits if h.get("golden_combo")),
                "stages": stages,              # 关卡代码 → 被拒数
                "rejects": funnel_rejects,     # symbol → 关卡代码（全量明细）
                # 通过者反推（2026-10-29）：全量币池符号 + 异常币。前端按关卡序
                # 「universe − 前序及本关被拒 − error」得到该关通过名单；旧轮次无这两个
                # 键，只保留计数。
                "universe_symbols": universe_symbols,
                "error_symbols": sorted(error_symbols),
                # 4h 放行状态（2026-10-29）：symbol → 状态名。只收通过 gate_4h +
                # htf_state_lock 的币（≈进管线币的放行子集），供抽屉「通过」按
                # 多头/空头排列、恢复初期分组；旧轮次无此键 → 前端显示「未记录」
                "htf_pass_states": htf_pass_states,
                "details": funnel_details,     # symbol → 判定说明（votes 票明细/no_trigger 未触发原因）
                # 观测补键（2026-10-28，docs/19 §4.3）：质量分下限之前的候选快照
                # （被砍者不落 scan_results，没有它无法复盘差多少分）、重复抑制数、
                # 活跃 pending 总量（与 pending_pool 的"本轮 upsert 条数"区分）
                **_funnel_observability(
                    _before_floor_hits, suppressed, pendings, pending_ops),
                # 4h 数据降级（2026-10-21 观测 B2）：本币池/命中数可比出占比，
                # 按天汇总（spec §5.3）>1% 需排查数据源
                "htf_degraded": len(htf_degraded_symbols),
                "htf_degraded_symbols": sorted(htf_degraded_symbols),
                # 4h 前置门证据与被拦快照（2026-10-21 第 2 步）：拦在哪个 4h
                # 状态/什么死因（data 降级 vs 市场无趋势），复盘分离两类口径
                "htf_evidence": htf_evidence,
                **_htf_funnel(htf_evidence),
            }

            logger.info(
                "扫描完成 #%s: 扫描%d个, 命中%d个（抑制重复%d个）",
                scan_record_id,
                len(symbols),
                len(new_hits),
                suppressed,
            )
        except Exception as e:
            logger.exception("扫描异常 #%s: %s", scan_record_id, e)
        finally:
            # 确保扫描状态总是更新（用独立 session 避免事务已中止）
            try:
                db.rollback()
            except Exception:
                pass
            self._update_scan_status(
                scan_record_id, status, hit_count, error_count, coin_count,
                funnel=funnel)
            db.close()
        return ai_skip_note

    @staticmethod
    def _save_results(
        scan_record_id: UUID,
        hits: list[dict],
        scan_type: str = "scheduled",
    ) -> dict[str, UUID]:
        """用独立 session 写入扫描结果，避免主 session 连接失效或死锁。

        返回 {symbol: 新写入 ScanResult.id}（供 pending confirm 回写关联）。
        """
        if not hits:
            return {}
        sdb = SessionLocal()
        result_ids: dict[str, UUID] = {}
        rows: list[ScanResult] = []
        try:
            for h in hits:
                row = ScanResult(
                    scan_record_id=scan_record_id,
                    symbol=h["symbol"],
                    signal_type=h.get("signal_type", "unknown"),
                    current_price=h["current_price"],
                    breakout_pct=h["breakout_pct"],
                    trend_slope=h.get("trend_slope", 0),
                    r_squared=h.get("r_squared", 0),
                    pattern=h.get("pattern"),
                    # 组合确认三项（2026-10-26，docs/11）：字段透传落库，形态
                    # 所在K线与漂移供 T3 的展示口径判定（旧行/无该键默认 False/None）
                    golden_combo=bool(h.get("golden_combo")),
                    pattern_candle_open=h.get("pattern_candle_open"),
                    entry_lag_pct=h.get("entry_lag_pct"),
                    signal_reason=h.get("signal_reason"),
                    strength=h.get("strength"),
                    ema_state=h.get("ema_state"),
                    position=h.get("position"),
                    touch_ref=h.get("touch_ref"),
                    position_ref=h.get("position_ref"),
                    volume_24h=h.get("volume_24h", 0),
                    volume=h.get("volume", 0),
                    volume_type=h.get("volume_type", "平量"),
                    quality_score=h.get("quality_score"),
                    # 质量分分项（T0-3/T1-2）：提示词与复盘共用同一份分项
                    quality_parts=h.get("quality_parts"),
                    # 配额分池/池内名次（T1-2）：观测回踩是否被结构性挤出
                    quota_pool=h.get("quota_pool"),
                    quota_rank=h.get("quota_rank"),
                    votes_json=h.get("votes"),
                    # 信号命中时刻（2026-10-21 观测 B1）：候选产生时点，非本批
                    # 落库时刻；旧命中无该键（历史/手工构造）留 NULL
                    signal_created_at=h.get("detected_at"),
                )
                sdb.add(row)
                rows.append(row)
            sdb.flush()  # 应用列默认值（uuid 主键），供 confirm 关联回写
            result_ids.update({r.symbol: r.id for r in rows})
            # 先提交扫描结果，再打影子标（docs/14 铁律）：影子若与结果同事务，
            # 影子 commit 失败的 rollback 会把已 flush 的 ScanResult 一并回滚
            # （rollback 会丢弃事务开始时的 pending 插入）——影子失败绝不拖累结果本身
            sdb.commit()
            # 影子打标落库（docs/14）：仅定时扫描（手动扫描用户在场非观察总体）。
            # 独立事务独立 try：影子失败只回滚影子写入，不影响扫描结果本身
            if scan_type == "scheduled":
                for r in rows:
                    shadow_data = next(
                        (h.get("shadow") for h in hits if h["symbol"] == r.symbol), None)
                    if not shadow_data:
                        continue
                    try:
                        for key, value in shadow_data.items():
                            sdb.add(ShadowMark(
                                feature_key=key, ref_type="scan_result", ref_id=r.id,
                                symbol=r.symbol,
                                value_json=value,
                                decision_json={"stage": "scan_hit", "scan_type": scan_type},
                            ))
                        sdb.commit()
                    except Exception as e:
                        sdb.rollback()
                        logger.debug("影子打标落库失败 %s: %s", r.symbol, e)
            logger.info("写入 %d 条扫描结果 #%s", len(hits), scan_record_id)
        except Exception as e:
            logger.exception("写入扫描结果失败 #%s: %s", scan_record_id, e)
            sdb.rollback()
            raise
        finally:
            sdb.close()
        return result_ids

    @staticmethod
    def _save_blocked_shadows(
        scan_record_id: UUID,
        blocked: dict[str, dict],
        scan_type: str = "scheduled",
    ) -> int:
        """被 4h 前置门拦下的候选快照落 shadow_marks（2026-10-21 第 2 步）。

        与 _save_results 的影子打标同口径：仅定时扫描（手动扫描用户在场，非
        观察总体）；独立 session、逐笔 commit，异常全吞——影子绝不拖累扫描结果。
        不注册进 shadow.SHADOW_FEATURES：该表是第 3 步反事实回放的数据集，
        不参与 build_report 的 scan_result 归因链（未注册的 key 不会出现在报告，
        避免被误记为 no_analysis）。

        ref_id 用确定性 uuid5(scan_record_id, symbol)：同一轮同一币唯一，重跑
        幂等（重复插入撞唯一约束后 rollback 忽略）。返回成功写入笔数。
        """
        if not blocked or scan_type != "scheduled":
            return 0
        sdb = SessionLocal()
        n = 0
        try:
            for symbol, snap in blocked.items():
                try:
                    sdb.add(ShadowMark(
                        feature_key="htf_blocked",
                        ref_type="blocked_scan",
                        ref_id=uuid5(
                            NAMESPACE_URL, f"htf_blocked:{scan_record_id}:{symbol}"),
                        symbol=symbol,
                        value_json=snap,
                        decision_json={"stage": snap.get("stage"), "scan_type": scan_type},
                    ))
                    sdb.commit()
                    n += 1
                except Exception as e:
                    sdb.rollback()
                    logger.debug("被拦信号影子落库失败 %s: %s", symbol, e)
            if n:
                logger.info("写入 %d 条被拦信号影子 #%s", n, scan_record_id)
        finally:
            sdb.close()
        return n

    @staticmethod
    def _update_scan_status(
        scan_record_id: UUID,
        status: str,
        hit_count: int,
        error_count: int,
        coin_count: int,
        funnel: Optional[dict] = None,
    ) -> None:
        """独立 session 更新扫描状态，确保即使主事务失败也能写入"""
        sdb = SessionLocal()
        try:
            record = sdb.get(ScanRecord, scan_record_id)
            if record:
                record.status = status
                record.hit_count = hit_count
                record.error_count = error_count
                if coin_count:
                    record.coin_count = coin_count
                if funnel is not None:
                    record.funnel_json = funnel   # 筛选漏斗（2026-10-11）
                record.finished_at = datetime.utcnow()
                sdb.commit()
        except Exception as e:
            logger.exception("更新扫描状态失败 #%s: %s", scan_record_id, e)
            sdb.rollback()
        finally:
            sdb.close()

    @staticmethod
    def _slots_full() -> tuple[bool, str]:
        """定时扫描 AI 分析跳过判定（docs/06）：正式网 OPENED 数对照
        max_open_trades 上限，达上限即跳过本轮；测试网不限持仓数（2026-10-15
        用户拍板，模拟环境多开观察）——OPENED 数不参与满仓判定，明细仍展示
        两网计数供观测。

        只统计 OPENED：已开始移动止损的单子（TP1_HIT 止损已移保本 / TP2_HIT
        仅剩跟进仓）风险已锁定，不占上限名额（2026-09-25）。
        返回 (是否满仓, 明细文案「测试网 X（不限）、正式网 X/Y」)；
        上限 0=不限单数；查询失败按未满仓处理（宁可多花一次 AI 分析，
        不因统计故障漏掉开仓机会）。
        """
        sdb = None
        try:
            sdb = SessionLocal()
            cfg = sdb.get(SystemConfig, 1)
            if not cfg:
                return False, ""
            max_n = cfg.max_open_trades or 0
            if max_n <= 0:
                return False, ""
            rows = sdb.execute(
                select(
                    func.coalesce(TradeRecord.testnet, False).label("net"),
                    func.count(TradeRecord.id),
                )
                .where(TradeRecord.status == "OPENED")
                .group_by("net")
            ).all()
            counts = {net: n for net, n in rows}
            tn = counts.get(True, 0)
            live = counts.get(False, 0)
            detail = f"测试网 {tn}（不限）、正式网 {live}/{max_n}"
            if live >= max_n:
                return True, f"满仓跳过 AI 分析：正式网在跑单达上限（{detail}）"
            return False, detail
        except Exception:
            logger.exception("查询在跑单子数失败，按未满仓处理")
            return False, ""
        finally:
            if sdb is not None:
                sdb.close()

    @staticmethod
    def _suppress_repeat_hits(hits: list[dict], repeat_window_hours: int) -> tuple[list[dict], int]:
        """重复命中抑制：同 (symbol, signal_type) 在 repeat_window_hours 内已有命中
        记录的，本轮不再落库——持续趋势里 Donchian 突破每根收盘K线都在创新高，
        不抑制会同币同信号每轮重复命中、重复触发 AI 分析。

        用独立 session 避免与主事务死锁；查询失败按未重复处理（不拦新信号）。
        返回 (新命中列表, 抑制条数)。
        """
        if not hits:
            return [], 0
        seen: set[tuple[str, str]] = set()
        sdb = SessionLocal()
        try:
            since = datetime.utcnow() - timedelta(hours=repeat_window_hours)
            symbols = list({h["symbol"] for h in hits})
            rows = sdb.execute(
                select(ScanResult.symbol, ScanResult.signal_type).where(
                    ScanResult.symbol.in_(symbols), ScanResult.created_at >= since
                )
            ).all()
            seen = {(r[0], r[1] or "unknown") for r in rows}
        except Exception as e:
            logger.warning("查询重复命中失败（按未重复处理）: %s", e)
            sdb.rollback()
        finally:
            sdb.close()
        new_hits = [
            h for h in hits
            if (h["symbol"], h.get("signal_type", "unknown")) not in seen
        ]
        return new_hits, len(hits) - len(new_hits)

    # ── 两段式回踩确认：pending 待确认池（docs/19）─────────────────────────

    @staticmethod
    def _prefetch_pendings(symbols: list[str]) -> dict[str, dict]:
        """批量预取活跃回踩 pending 快照（每轮一次查询，勿逐币查）。

        返回 {symbol: {id, direction, stage1_candle_open}}；查询失败按无 pending
        处理（不阻断扫描）。同 symbol 取最新一条（正常至多一条；防御手动/定时
        并发双行的极端情况）。
        """
        if not symbols:
            return {}
        sdb = SessionLocal()
        try:
            rows = sdb.execute(
                select(PullbackPending).where(
                    PullbackPending.symbol.in_(symbols),
                    PullbackPending.status == "pending",
                ).order_by(PullbackPending.created_at.desc())
            ).scalars().all()
            out: dict[str, dict] = {}
            for r in rows:
                if r.symbol not in out:
                    out[r.symbol] = {
                        "id": r.id,
                        "direction": r.direction,
                        "stage1_candle_open": r.stage1_candle_open,
                        "touch_ref": r.touch_ref,
                        # Stage-1 触碰锚点价（命中触点位）：CARD-08 影子打标用
                        # 生产原生触点位（零漂移），对决策路径不可见
                        "ema21": float(r.ema21) if r.ema21 is not None else None,
                    }
            return out
        except Exception as e:
            logger.warning("预取回踩 pending 失败（按无 pending 处理）: %s", e)
            return {}
        finally:
            sdb.close()

    # ── 破轨事件（docs/22 §4）：突破回测触点的事实源 ─────────────────────────

    @staticmethod
    def _interval_hours(interval: str) -> float:
        """K线周期字符串 → 小时数（"1h"/"4h"/"15m"/"1d"）；解析失败按 1h"""
        try:
            unit, n = interval[-1], int(interval[:-1])
            return {"m": n / 60.0, "h": float(n), "d": float(n * 24)}.get(unit, 1.0)
        except (ValueError, TypeError, IndexError):
            return 1.0

    @staticmethod
    def _prefetch_retest_events(symbols: list[str]) -> dict[str, dict]:
        """批量预取活跃破轨事件快照（docs/22 回测触点；每轮一次查询，勿逐币查）。

        返回 {symbol: {price, direction, candle_open}}；查询失败按无事件处理
        （不阻断扫描）。同 symbol 取最新一条 active（正常至多一条；防御并发双行）。
        """
        if not symbols:
            return {}
        sdb = SessionLocal()
        try:
            rows = sdb.execute(
                select(BreakoutEvent).where(
                    BreakoutEvent.symbol.in_(symbols),
                    BreakoutEvent.status == "active",
                ).order_by(BreakoutEvent.created_at.desc())
            ).scalars().all()
            out: dict[str, dict] = {}
            for r in rows:
                if r.symbol not in out:
                    out[r.symbol] = {
                        "price": float(r.level_price),
                        "direction": r.direction,
                        "candle_open": r.candle_open,
                    }
            return out
        except Exception as e:
            logger.warning("预取破轨事件失败（按无事件处理）: %s", e)
            return {}
        finally:
            sdb.close()

    @staticmethod
    def _apply_breakout_events(
        ops: dict[str, dict], window_bars: int, kline_interval: str,
    ) -> None:
        """破轨事件落库（独立 session，扫描线程只收集不写库）。

        - upsert：T1 突破触发记录/刷新（同方向换新位价与锚点K线；反向破轨旧行
          expired 后新建——旧方向回测位随趋势反转失效）
        - consume：回测触点确认（touch_ref=retest 的回踩金K落库）→ consumed
        - 窗口过期：active 且破轨K线早于 retest_window_bars 根之前 → 批量 expired
        失败整体回滚忽略——事件是派生事实，下轮 T1 触发会重新记录。
        """
        sdb = SessionLocal()
        try:
            now = datetime.utcnow()
            if window_bars and window_bars > 0:
                cutoff = now - timedelta(
                    hours=Scanner._interval_hours(kline_interval) * window_bars
                )
                sdb.execute(
                    update(BreakoutEvent)
                    .where(BreakoutEvent.status == "active",
                           BreakoutEvent.candle_open < cutoff)
                    .values(status="expired")
                )
            for symbol, op in ops.items():
                kind = op.get("op")
                row = sdb.execute(
                    select(BreakoutEvent).where(
                        BreakoutEvent.symbol == symbol,
                        BreakoutEvent.status == "active",
                    ).order_by(BreakoutEvent.created_at.desc()).limit(1)
                ).scalars().first()
                if kind == "upsert":
                    direction = op["direction"]
                    if row and row.direction != direction:
                        row.status = "expired"  # 反向破轨：旧方向回测位失效
                        row = None
                    if row:
                        row.level_price = op["level_price"]
                        row.candle_open = op["candle_open"]
                    else:
                        sdb.add(BreakoutEvent(
                            symbol=symbol,
                            direction=direction,
                            level_price=op["level_price"],
                            candle_open=op["candle_open"],
                        ))
                elif kind == "consume" and row is not None:
                    row.status = "consumed"
            sdb.commit()
        except Exception as e:
            logger.exception("破轨事件落库失败（忽略，下轮重评估）: %s", e)
            sdb.rollback()
        finally:
            sdb.close()

    @staticmethod
    def _sweep_orphan_pendings(kline_interval: str, max_bars: int = 3) -> None:
        """孤儿 pending 清扫（2026-10-10）：跌出扫描宇宙的币无人再评估其 pending，
        行冻结在 pending 永不终态（_prefetch/_apply 均按本轮 universe 过滤）。

        设计等待窗 ≤N 根（N=pending_confirm_window_bars）；调用方取 N+1 根作阈值，
        给「窗口最后一根仍未裁决」留一轮余量；给同向触碰续命的 upsert 也会刷新
        stage1_candle_open，故超 max_bars 根仍未终态 = 既无本轮处置也无续命，属孤儿。
        批量置 expired/window_expired（终态保留审计链，前端 blocked_by 可见死因）。
        失败只记日志——下轮清扫幂等重跑。默认 3 供旧调用方兼容（N=2 口径）。
        """
        sdb = SessionLocal()
        try:
            cutoff = datetime.utcnow() - timedelta(
                hours=Scanner._interval_hours(kline_interval) * max_bars
            )
            result = sdb.execute(
                update(PullbackPending)
                .where(PullbackPending.status == "pending",
                       PullbackPending.stage1_candle_open < cutoff)
                .values(status="expired", resolve_reason="window_expired",
                        resolved_at=datetime.utcnow())
            )
            sdb.commit()
            if result.rowcount:
                logger.info("孤儿 pending 清扫：%d 行超 %d 根未裁决 → expired", result.rowcount, max_bars)
        except Exception as e:
            logger.exception("孤儿 pending 清扫失败（忽略，下轮重跑）: %s", e)
            sdb.rollback()
        finally:
            sdb.close()

    @staticmethod
    def _apply_pending_ops(ops: dict[str, dict], result_ids: dict[str, UUID]) -> None:
        """统一落库 pending 处置动作（独立 session，扫描线程只收集不写库）。

        - upsert：同方向刷新锚点续命；无活跃行则新建；方向反转旧行 expired
          （direction_flip）后同事务新建（保留审计链，不原地改写方向）
        - confirm：终态 confirmed，回填确认形态与 confirmed_scan_result_id
        - supersede：终态 superseded（T1 突破 / 当根确认抢先）
        - expire：终态 expired（ranging=4h 震荡第一步过滤 / direction_flip / window_expired）
        失败整体回滚忽略——下轮扫描会按快照重新评估，不产生不可恢复状态。
        """
        sdb = SessionLocal()
        try:
            now = datetime.utcnow()
            for symbol, op in ops.items():
                kind = op.get("op")
                row = sdb.execute(
                    select(PullbackPending).where(
                        PullbackPending.symbol == symbol,
                        PullbackPending.status == "pending",
                    ).order_by(PullbackPending.created_at.desc()).limit(1)
                ).scalars().first()

                if kind == "upsert":
                    direction = op["direction"]
                    if row and row.direction != direction:
                        row.status = "expired"
                        row.resolve_reason = "direction_flip"
                        row.resolved_at = now
                        row = None  # 方向反转：旧行终态后新建（不原地改写方向）
                    if row:
                        # 同方向触碰刷新：锚点换新触碰根，等待窗口重新计时
                        row.stage1_candle_open = op["stage1_candle_open"]
                        row.ema21 = op.get("ema21")
                        row.touch_ref = op.get("touch_ref")
                        row.quality_score = op.get("quality_score")
                        row.votes_json = op.get("votes_json")
                        row.updated_at = now
                    else:
                        sdb.add(PullbackPending(
                            symbol=symbol,
                            direction=direction,
                            stage1_candle_open=op["stage1_candle_open"],
                            ema21=op.get("ema21"),
                            touch_ref=op.get("touch_ref"),
                            quality_score=op.get("quality_score"),
                            votes_json=op.get("votes_json"),
                        ))
                elif row is None:
                    continue
                elif kind == "confirm":
                    row.status = "confirmed"
                    row.confirm_pattern = op.get("confirm_pattern")
                    row.confirmed_scan_result_id = result_ids.get(symbol)
                    row.resolved_at = now
                elif kind == "supersede":
                    row.status = "superseded"
                    row.resolve_reason = op.get("reason") or "superseded"
                    row.resolved_at = now
                elif kind == "expire":
                    row.status = "expired"
                    row.resolve_reason = op.get("reason") or "window_expired"
                    row.resolved_at = now
            sdb.commit()
        except Exception as e:
            logger.exception("回踩 pending 处置落库失败（忽略，下轮重评估）: %s", e)
            sdb.rollback()
        finally:
            sdb.close()
