"""扫描编排器（指标驱动筛选管线，2026-09-17 重设计）

筛选漏斗（指标为主、信号为触发器，EMA 三件套硬门 2026-10-10 二次拍板）：
1. 宇宙：USDT 永续，24h 成交额 ≥ MIN_VOLUME_24H（流动性门槛）
2. 4h 趋势闸门 gate_4h 一票否决：EMA144 斜率/缠绕/ADX 三重条件必须同时成立，
   否决直接不看（不跑票数不触发不进 AI）；1H 结构门 + 震荡评分卡 ≥3 同为硬门
   （ema_gate.py，docs/19 §2/§3）
3. 分组：单趟 7 票净票 |net|≥3 → 多头/空头候选；-2~+2 中性跳过（震荡市不开单）
4. 触发确认：两段式决策矩阵（docs/19）——结构突破/趋势突破 / 回踩+金K确认；
   回踩触碰无金K 转入 pending 待确认池（pullback_pendings，次根金K才确认）；
   无触发 → 观察池（不落库，下轮重算）
5. 质量分排序 + 重复命中抑制（仅定时）+ 落库 → AI 配额（按质量分取前 N）

旧信号管线（Donchian+形态为主动作、EMA 硬门控）已整体替换：
方向判定权从信号路径移交给指标多数决，与 AI 提示词的多数决规则同一把尺子。
"""
import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from typing import Optional, Tuple
from uuid import UUID

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
from app.services.strategy.ema import analyze_ema
from app.services.strategy.indicators import volume_ratio
from app.services.strategy.technical import (
    compute_indicator_votes,
    compute_quality_score,
    analyze_htf_background,
    HTF_SHORT,
)
from app.services.strategy.trigger import (
    BREAKOUT,
    PULLBACK,
    PULLBACK_CONFIRM,
    TOUCH_RETEST,
    TREND_BREAKOUT,
    detect_trigger,
)
from app.services.strategy.ema_gate import (
    RANGE_SCORE_REJECT,
    gate_4h,
    range_score,
    structure_1h,
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


def detect_indicator_candidate(
    pool: ExchangePool, symbol: str, klines: list[list],
    pending: Optional[dict] = None,
    flt: Optional[ScanFilters] = None,
    retest: Optional[dict] = None,
    event_out: Optional[dict] = None,
) -> Tuple[Optional[dict], Optional[dict]]:
    """指标驱动单币检测（Scanner 与 watchlist.refresh 共用同一套逻辑与阈值）。

    流程（2026-10-10 二次拍板：EMA 三件套硬门化）：4h 趋势闸门 gate_4h 一票否决
    （走平/缠绕/ADX<20 任一命中直接不看，取代原 ranging 过滤+htf_res 方向门）
    → 单趟 7 票投票 → 闸门方向一致 → 1H 结构门 structure_1h（三线排列+发散度）
    → 震荡评分卡 range_score（≥3 不入场）→ 两段式触发确认（trigger.py）
    → 质量分。原 6 票预筛已于同日撤销（4h 前置后失去省请求价值，|net6|<2 被
    终判 |net|<3 完全蕴含），4h 背景复用第一步结果、6 项指标不再双算。

    pending: 该币活跃回踩 pending 快照（{id, direction, stage1_candle_open, touch_ref}）或 None。
    flt: 筛选参数组（2026-09-28 筛选配置上页面）；None=按 settings 解析。
    retest: 活跃破轨事件快照 {price, direction, candle_open}（docs/22 回测触点）或 None。
    event_out: 可选出参 dict——T1 突破触发时写入破轨事件动作
      {"op": "upsert", direction, level_price, candle_open}（scanner 落库
      breakout_events，回测触点事实源）；触发类型白名单过滤不影响事件记录
      （破轨是市场事实，与是否产生突破信号无关）。
    返回 (候选 dict, pending_op) 二元组：
    - 候选 dict（signal_type/touch_ref/quality_score/votes 及展示字段），每币至多 1 个；
      无候选（中性/票数不足/无触发）为 None
    - pending_op: pending 处置动作（upsert/expire/confirm/supersede），可为 None；
      候选为 None 时仍可能携带动作（触碰转 pending、票崩/窗口过期）

    异常向上抛由调用方处理。
    """
    ema = analyze_ema(klines)  # 背景展示用（方向判定不依赖 EMA）
    vol, vol_type = classify_volume(klines)

    # 第一步：4h 趋势闸门 gate_4h（2026-10-10 二次拍板，check_4h_gate 一票否决
    # 语义落地为筛选硬门，取代原 ranging 过滤 + htf_res 方向门）——整个筛选系统
    # 最重要的过滤器：只有 4H 趋势明确（EMA144 斜率 ≥0.5%/20根、近10根缠绕
    # ≤2次、ADX≥20，三者同时成立）才允许 1H 入场，4h 走平或缠绕时 1h 信号再
    # 漂亮也不做。命中否决 → 直接不看：不跑票数、不触发、不进候选（连 AI 分析
    # 都不给），活跃 pending 同轮 expire(ranging)（ranging 词义="4h 无明确趋势"，
    # 涵盖走平与缠绕，死因沿用既有词表）。
    # 4h 每轮对全币池拉取（kline 缓存按 4h 桶对齐，实际新请求 ≈ 池子 ÷4 每小时）；
    # 拉取失败（htf=None）与闸门计算异常降级放行——gate_4h 返回 None=主动否决，
    # 异常=数据/实现问题，二者区分（原 ranging 过滤同口径）。4h 历史 <200 根
    # （新币冷静期默认 45 天≈270 根，正常覆盖不到）一并否决。
    try:
        htf = pool.get_klines(symbol, "4h", 300)  # 300 根：EMA144 SMA 种子后递推 155 步，收敛残留 ~12%
    except Exception as e:
        htf = None
        logger.warning("%s 获取4h K线失败（跳过4h票）: %s", symbol, e)
    htf_res = None
    gate = None
    if htf:
        try:
            htf_res = analyze_htf_background(htf)
        except Exception as e:
            htf_res = None
            logger.warning("%s 4h背景复算失败（过滤降级放行）: %s", symbol, e)
        try:
            gate = gate_4h(htf)
        except Exception as e:
            gate = None
            logger.warning("%s 4h闸门计算异常（降级放行）: %s", symbol, e)
        else:
            # 仅"正常返回 None"= 主动否决；异常路径 gate=None 走下方降级放行，
            # 不死因归类 ranging（异常=数据/实现问题，与市场状态无关）
            if gate is None:
                return None, ({"op": "expire", "reason": "ranging"} if pending else None)

    # 投票单趟计算（2026-10-10 精简）：6 票预筛层撤销——4h 前置拉取后预筛失去
    # 「省 4h 请求」的存在理由，|net6|<2 被终判 |net|<3 完全蕴含（第 7 票至多
    # ±1），有损的 voted<5 分支删除（voted6=4 + 第 7 票 → voted=5 本可通过终判，
    # 不再被预筛误杀）。|net6|<2 的币照样死于终判 |net|<3，pending 过期原因不变；
    # 4h 背景复用第一步的 htf_bg，6 项 1h 指标也不再双算（原两次
    # compute_indicator_votes 各跑一遍全量指标）
    votes = compute_indicator_votes(klines, htf_bg=htf_res)
    if not votes or votes["voted"] < 5 or abs(votes["net"]) < 3:
        # 中性/票数不足 → 活跃 pending 方向失守（docs/19 §4.1 前置过期路径）
        return None, ({"op": "expire", "reason": "direction_flip"} if pending else None)

    direction = 1 if votes["net"] > 0 else -1
    # 闸门方向一致性（2026-10-10 二次拍板，取代原 htf_res dir 门）：gate_4h 给出
    # 的 4h 方向（价格×EMA144 位置+斜率方向）必须与多数决方向一致——gate 在上面
    # 已保证非 None（htf 拉取失败降级时 gate=None，跳过检查与原降级口径一致）。
    # 与 4h 冲突的活跃 pending 不可能被确认（金K 方向跟着多数决走），按方向失守过期
    if gate is not None and gate[0] != ("long" if direction == 1 else "short"):
        return None, ({"op": "expire", "reason": "direction_flip"} if pending else None)

    # 第二步：1H 结构校验（2026-10-10 二次拍板，check_1h_structure 语义硬门化）：
    # 三线排列（EMA21>55>144 多头镜像）+ ATR 归一发散度（|21-55|/ATR≥0.5、
    # |55-144|/ATR≥1.0）必须成立——仅有排列还不够，三线挤在一起即使顺序对也是
    # 震荡；ATR 归一化让同一套阈值跨品种可用。1h 历史 <200 根（新币）一并否决
    st = structure_1h(klines[:-1] if len(klines) >= 2 else klines,
                      "long" if direction == 1 else "short")
    if st is None:
        return None, ({"op": "expire", "reason": "structure_1h"} if pending else None)

    # 第三步：震荡评分卡（2026-10-10 二次拍板，calc_range_score 语义硬门化）：
    # 8 项独立评分 ≥3 不入场——单一指标可能失效，多指标共振才可靠。闸门与结构
    # 门已通过的币，评分实际来自 1h ADX 预警/布林带收口/价格反复穿越 EMA21/
    # 成交量低迷四项（项 1-4 恒 0），≥3 即强震荡共识
    rs, _rs_reasons = range_score(klines, gate_ev=gate[1] if gate else None)
    if rs >= RANGE_SCORE_REJECT:
        return None, ({"op": "expire", "reason": "range_score"} if pending else None)

    # htf 直传触点体系（2026-10-09）：ema21_4h 跨周期触点与第 7 票/闸门共用
    # 同一份 4h 快照，单轮单币不重复拉取
    trig, pending_op = detect_trigger(
        klines, direction, pending, flt=flt, retest=retest, htf=htf,
    )
    if trig is not None:
        # T1 突破 → 破轨事件动作（docs/22 §4）：白名单过滤前记录——破轨是市场
        # 事实，触发类型白名单只决定是否产生突破信号，不影响回测触点事实源
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
    if trig is None:
        # 行5：触碰无金K → upsert pending；补 Stage-1 快照（质量分按回踩口径计）
        if pending_op and pending_op.get("op") == "upsert":
            pending_op["quality_score"] = compute_quality_score(direction, votes, PULLBACK)
            pending_op["votes_json"] = _votes_snapshot(votes)
        return None, pending_op

    quality = compute_quality_score(direction, votes, trig["type"], klines=klines)
    return {
        "signal_type": trig["type"],
        # 现价 = 未收盘K线最新价（仅作入场价差参考）：信号判定（投票/触发/形态/量能）
        # 全部基于已收盘K线（klines[-2]），与扫描时刻在小时内的位置无关，同根K线同结果
        "current_price": float(klines[-1][4]),
        "breakout_pct": trig["breakout_pct"],
        "trend_slope": 0.0,
        "r_squared": 0.0,
        "pattern": trig.get("pattern"),
        "signal_reason": trig["reason"][:120],
        "position": trig["position"],
        "touch_ref": trig.get("touch_ref"),
        "position_ref": trig.get("position_ref"),
        "strength": quality / 100.0,
        "ema_state": ema["state"] if ema else None,
        "volume": vol,
        "volume_type": vol_type,
        "quality_score": quality,
        "votes": _votes_snapshot(votes),
    }, pending_op


def _votes_snapshot(votes: dict) -> dict:
    """指标票数快照（含 4h 背景级别，前端「4h方向」列展示；旧行无此键显示 -）"""
    snap = {
        "bull": votes["bull"], "bear": votes["bear"],
        "bull_n": votes["bull_n"], "bear_n": votes["bear_n"],
        "net": votes["net"], "voted": votes["voted"],
    }
    htf_res = dict(votes["results"]).get(HTF_SHORT)
    if htf_res:
        snap["htf"] = {
            "state": htf_res.get("state"),
            "label": htf_res.get("state_label"),
            "dir": int(htf_res.get("dir") or 0),
            # 趋势年龄四档（2026-10-09 r2）：launch/mid/late/exhausted，
            # 前端「4h方向」列短标签用；旧行无此键回退强/弱 label 解析
            "phase": htf_res.get("phase"),
        }
    return snap


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
        coin_count = 0
        ai_skip_note: Optional[str] = None
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
            # 筛选参数组（scan_filters JSONB > env > 代码默认）
            flt = resolve_scan_filters(cfg)

            logger.info("开始扫描 #%s (interval=%s, window=%d)", scan_record_id, kline_interval, kline_window)

            # 1. 获取合约交易对列表（按 24h 成交额降序）
            symbols_data = self.pool.get_usdt_swap_symbols_with_volume()

            coin_count = len(symbols_data)
            logger.info("共 %d 个合约交易对待扫描（按24h成交额降序）", len(symbols_data))

            # 24h 成交额过滤：流动性差的合约不进扫描（省K线请求，剔除不可交易标的）
            min_vol = flt.min_volume_24h
            before = len(symbols_data)
            symbols_data = [
                s for s in symbols_data
                if float(s.get("volume_24h") or 0) >= min_vol
            ]
            logger.info(
                "24h成交额 ≥ %s USDT 过滤：%d → %d",
                f"{min_vol:,.0f}", before, len(symbols_data),
            )

            # C0 下单环境可交易过滤（2026-09-19，ARX 案例）：数据源永远走正式网
            # （ARXUSDT 有 K线、AI 照常分析），下单环境未上架 → 开单 -1121
            # Invalid symbol 白白烧 AI 配额 + FAILED 记录。2026-10-03 双网同开
            # +删环境切换：按「启用了自动交易的网」（auto_trade_accounts 账号的
            # testnet 属性并集）各自拉 exchangeInfo，任一启用网可交易即保留；
            # 双开关全关（无启用网）放行——不开单无浪费，不阻断。空表放行不阻断
            # （_bt 实例化必须在 C0 之前——此前放在 C 段导致 UnboundLocalError，
            #   04:02/05:02 两轮扫描全崩的根因）
            from app.services.account_registry import auto_trade_accounts
            listed: set = set()
            for net in sorted({a.testnet for a in auto_trade_accounts(db)}):
                listed |= set(BinanceTrader(net).listed_symbols())
            if listed:
                before = len(symbols_data)
                symbols_data = [s for s in symbols_data if s["symbol"] in listed]
                logger.info("下单环境可交易过滤（%d 个合约）：%d → %d", len(listed), before, len(symbols_data))

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
                    symbols_data = [
                        s for s in symbols_data
                        if (od := onboard.get(s["symbol"])) is None or od <= cutoff_ms
                    ]
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
                symbols_data = [
                    s for s in symbols_data
                    if (rt := risk_tiers.get(s["symbol"])) is None or rt[1] >= 10
                ]
                logger.info("风险档位过滤（最大杠杆 <10×）：%d → %d", before, len(symbols_data))

            # D 黑名单过滤（2026-09-18）：手动添加（永久）/ 连续止损自动加入（冷却期），
            # 命中不进扫描。集合为空时跳过查询
            from app.services.blacklist_service import active_symbols
            blacklisted = active_symbols(db)
            if blacklisted:
                before = len(symbols_data)
                symbols_data = [s for s in symbols_data if s["symbol"] not in blacklisted]
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
                """指标多数决定向 → 两段式触发确认 → 质量分。返回 (symbol, [候选...], is_error)"""
                try:
                    # EMA144/指标暖机需要足够历史，至少拉 300 根
                    klines = self.pool.get_klines(
                        symbol,
                        interval=kline_interval,
                        limit=max(kline_window, 300),
                    )
                    ev_out: dict = {}
                    cand, pop = detect_indicator_candidate(
                        self.pool, symbol, klines, pending=pendings.get(symbol),
                        flt=flt, retest=retests.get(symbol), event_out=ev_out,
                    )
                    # 破轨事件动作（T1 触发记录 / 回测触点确认消费）
                    if ev_out.get("op"):
                        event_ops[symbol] = ev_out
                    if pop:
                        if pop.get("op") == "confirm" and cand:
                            pop["confirm_pattern"] = cand.get("pattern")
                        # 次根确认消费：pending 的 Stage-1 是回测触点 → 事件 consumed
                        if (pop.get("op") == "confirm"
                                and (pendings.get(symbol) or {}).get("touch_ref") == TOUCH_RETEST):
                            event_ops[symbol] = {"op": "consume"}
                        pending_ops[symbol] = pop
                    # 当根确认消费：本次回踩金K命中的触点是回测位 → 事件 consumed
                    if (cand and cand.get("signal_type") == PULLBACK_CONFIRM
                            and cand.get("touch_ref") == TOUCH_RETEST):
                        event_ops[symbol] = {"op": "consume"}
                    if cand is None:
                        return symbol, [], False
                    cand["symbol"] = symbol
                    # 影子打标（docs/14）：特征计算挂在此处（klines 在手），
                    # 值随候选透传给 _save_results 落 shadow_marks；计算失败
                    # 影子内部吞掉，不影响扫描管线。不写回任何决策字段
                    try:
                        from app.services import shadow as shadow_mod
                        shadow_vals: dict = {}
                        for f in shadow_mod.SHADOW_FEATURES.values():
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
                    # P-07 虚拟账本三价推导（docs/16）：与源B同源公式+账本口径
                    # TP2=3R，随候选透传给 _save_results 开单。entry=信号K收盘
                    # （klines[-2][4]，current_price 是未收盘价不能用）；失败吞掉
                    try:
                        from app.services import ledger as ledger_mod
                        votes = cand.get("votes") or {}
                        d = 1 if votes.get("net", 0) > 0 else -1
                        if len(klines) >= 6:
                            prices = ledger_mod.derive_prices_for_scan(
                                d, float(klines[-2][4]), klines[-6:-1])
                            if prices:
                                cand["ledger"] = {
                                    **prices,
                                    "direction": "long" if d > 0 else "short",
                                    "entry_bar_open_ms": int(klines[-2][0]),
                                }
                    except Exception as e:
                        logger.debug("虚拟账本三价计算异常（忽略）%s: %s", symbol, e)
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
                    for sig in signals:
                        sig["volume_24h"] = volume_map.get(symbol, 0)
                        hits.append({"symbol": symbol, **sig})

            # 3. 按质量分降序（同分按 24h 成交额）——AI 配额按此顺序取前 N
            hits.sort(
                key=lambda x: (x.get("quality_score", 0), x.get("volume_24h", 0)),
                reverse=True,
            )

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

            # 5.6 破轨事件处置（docs/22 §4）：upsert/consume + 窗口过期批处理
            if event_ops:
                self._apply_breakout_events(
                    event_ops, flt.retest_window_bars, kline_interval,
                )

            # 6. 触发 AI 分析（任务内自检开关；批量按24h成交额取前 AI_MAX_PER_SCAN 个）
            # 满仓跳过（docs/06，2026-10-04 按网判定）：定时扫描时任一网（测试/正式）
            # OPENED 数达 max_open_trades 上限则不做 AI 分析（省 LLM 成本，该网反正
            # 开不了仓）；手动扫描/手动分析不受限。跳过说明返回给任务记录 summary 展示
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
            self._update_scan_status(scan_record_id, status, hit_count, error_count, coin_count)
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
                    votes_json=h.get("votes"),
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
            # P-07 虚拟账本开单（docs/16）：每条新 ScanResult 一行（手动+定时都
            # 记，同根K线靠 uq_ledger_candle 唯一约束天然去重）。独立 try：账本
            # 失败只回滚账本插入，绝不拖累扫描结果本身（同影子铁律）
            try:
                from app.services import ledger as ledger_mod
                opened = 0
                for r in rows:
                    snap = next(
                        (h.get("ledger") for h in hits if h["symbol"] == r.symbol),
                        None)
                    if not snap:
                        continue
                    if ledger_mod.open_entry(sdb, r, snap, scan_type=scan_type):
                        opened += 1
                if opened:
                    sdb.commit()
            except Exception as e:
                sdb.rollback()
                logger.warning("虚拟账本开单失败（忽略）#%s: %s", scan_record_id, e)
            logger.info("写入 %d 条扫描结果 #%s", len(hits), scan_record_id)
        except Exception as e:
            logger.exception("写入扫描结果失败 #%s: %s", scan_record_id, e)
            sdb.rollback()
            raise
        finally:
            sdb.close()
        return result_ids

    @staticmethod
    def _update_scan_status(
        scan_record_id: UUID,
        status: str,
        hit_count: int,
        error_count: int,
        coin_count: int,
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
                record.finished_at = datetime.utcnow()
                sdb.commit()
        except Exception as e:
            logger.exception("更新扫描状态失败 #%s: %s", scan_record_id, e)
            sdb.rollback()
        finally:
            sdb.close()

    @staticmethod
    def _slots_full() -> tuple[bool, str]:
        """定时扫描 AI 分析跳过判定（docs/06）：在跑单按网区分——测试网/正式网
        各自统计 OPENED 数对照 max_open_trades 上限，任一网满仓即跳过本轮
        （2026-10-04 前为两网合计判定，单网额度没用完就整体跳过）。

        只统计 OPENED：已开始移动止损的单子（TP1_HIT 止损已移保本 / TP2_HIT
        仅剩跟进仓）风险已锁定，不占上限名额（2026-09-25）。
        返回 (是否满仓, 明细文案「测试网 X/Y、正式网 X/Y」)；
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
            detail = f"测试网 {tn}/{max_n}、正式网 {live}/{max_n}"
            if tn >= max_n or live >= max_n:
                which = "测试网" if tn >= max_n else "正式网"
                return True, f"满仓跳过 AI 分析：{which}在跑单达上限（{detail}）"
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
