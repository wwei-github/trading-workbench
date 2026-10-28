import uuid
from datetime import datetime
from typing import Optional
from sqlalchemy import String, Integer, Boolean, DateTime, Float, ForeignKey, Numeric, Index, JSON, UniqueConstraint, SmallInteger
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.dialects.postgresql import UUID

from app.database import Base


class ScanRecord(Base):
    __tablename__ = "scan_records"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    scan_type: Mapped[str] = mapped_column(String(20), default="manual")  # scheduled / manual
    status: Mapped[str] = mapped_column(String(20), default="running")  # running / completed / failed
    coin_count: Mapped[int] = mapped_column(Integer, default=0)
    hit_count: Mapped[int] = mapped_column(Integer, default=0)
    error_count: Mapped[int] = mapped_column(Integer, default=0)
    started_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    finished_at: Mapped[datetime] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    # 筛选漏斗（2026-10-11）：{universe, scanned, errors, candidates, pending_pool,
    # stages: {关卡代码→被拒数}, rejects: {symbol→关卡代码}}——前端「筛选漏斗」
    # Tab 展示每轮各币卡在哪一步；旧行/异常轮次 NULL
    funnel_json: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)

    results: Mapped[list["ScanResult"]] = relationship(back_populates="scan_record", cascade="all, delete-orphan")


class ScanResult(Base):
    __tablename__ = "scan_results"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    scan_record_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("scan_records.id"), nullable=False)
    symbol: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    signal_type: Mapped[str] = mapped_column(String(32), nullable=False, default="unknown", index=True)
    current_price: Mapped[float] = mapped_column(Numeric(20, 8), nullable=False)
    breakout_pct: Mapped[float] = mapped_column(Numeric(10, 4), nullable=False)
    trend_slope: Mapped[float] = mapped_column(Numeric(20, 10), nullable=False)
    r_squared: Mapped[float] = mapped_column(Numeric(10, 6), nullable=False)
    pattern: Mapped[str] = mapped_column(String(32), nullable=True)
    signal_reason: Mapped[str] = mapped_column(String(128), nullable=True)
    strength: Mapped[Optional[float]] = mapped_column(Numeric(6, 4), nullable=True)  # 信号强度 0-1
    ema_state: Mapped[Optional[str]] = mapped_column(String(16), nullable=True, index=True)  # 均线形态状态
    # 信号位置（trigger.py 形态/突破方向派生：看涨形态/回踩企稳=support、
    # 向上突破/反抽受阻=resistance；历史行为关键位角色口径，2026-09-14 关键位功能移除后不再产生关键位明细）
    position: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    touch_ref: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)  # 回踩触点（docs/22：retest/swing/ema21/55/144）
    position_ref: Mapped[Optional[str]] = mapped_column(String(48), nullable=True)  # 命中位置文案（回踩触点位/突破前高前低轨，前端「位置」列；旧行 NULL）
    volume_24h: Mapped[float] = mapped_column(Numeric(20, 2), nullable=False, default=0)
    volume: Mapped[float] = mapped_column(Numeric(20, 8), nullable=False, default=0)
    volume_type: Mapped[str] = mapped_column(String(16), nullable=False, default="平量", index=True)
    # 指标驱动筛选（2026-09-17）：候选质量分（0-100，AI 配额排序键）与指标票数明细
    quality_score: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    # 质量分分项（2026-10-21，T0-3/T1-2）：{base, htf, adx, volume, ...}，
    # 供提示词渲染「质量分 68（base 50 / 4h +10 / 方向票 +10 / 减分 -2）」
    # 与复盘拆解排序依据；旧行 NULL（仅有总分）
    quality_parts: Mapped[Optional[dict]] = mapped_column(
        JSON(none_as_null=True), nullable=True
    )
    # AI 配额分池与排名（2026-10-21，T1-2）：quota_pool = breakout / pullback，
    # quota_rank = 该池内按质量分的名次（1=最优）；旧行 NULL
    quota_pool: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)
    quota_rank: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    votes_json: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    is_repeat: Mapped[bool] = mapped_column(Boolean, default=False)
    # 信号命中时刻（2026-10-21 观测 B1）：候选在指标管线内产生的时刻（=收盘K线
    # 可判定时刻），与 created_at（批量落库时刻）区分——扫描是整轮结束后批量
    # 插入，created_at 混入了本轮扫描耗时，不能代表信号新鲜度。复盘按
    # signal_created_at → ai_finished_at → opened_at 三段计算执行时延；
    # 本列上线前的历史行 NULL（按 created_at 理解）
    signal_created_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    # 组合确认口径（2026-10-26，docs/11）：回踩金K走「前根形态+当根趋势K」时
    # pattern 落在信号根的前一根。golden_combo 标记组合路径（规则2 双流星 /
    # 规则3 前根+趋势K，仅留痕）；pattern_candle_open 指向 pattern 真实所在K线
    # （有 pattern 即落，形态在当根时=信号根）；entry_lag_pct = 形态收盘→信号
    # 收盘的方向归一漂移（正=对本次交易不利），仅形态早于信号根时计算。旧行
    # golden_combo=FALSE、其余 NULL
    golden_combo: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    pattern_candle_open: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    entry_lag_pct: Mapped[Optional[float]] = mapped_column(Numeric(10, 4), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)

    scan_record: Mapped[ScanRecord] = relationship(back_populates="results")
    ai_analysis: Mapped[Optional["AIAnalysis"]] = relationship(back_populates="scan_result", uselist=False)

    __table_args__ = (
        Index("ix_scan_results_symbol_created", "symbol", "created_at"),
        Index("ix_scan_results_scan_record_id", "scan_record_id"),
    )


class AIAnalysis(Base):
    __tablename__ = "ai_analyses"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    # 可空：单币重新分析时被交易记录/复盘引用的旧结论会摘钩（置空）让位给新结论
    scan_result_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("scan_results.id"), nullable=True, index=True
    )
    symbol: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    trade_decision: Mapped[Optional[str]] = mapped_column(String(8), nullable=True, index=True)  # suggest / skip
    skip_reason: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    direction: Mapped[Optional[str]] = mapped_column(String(8), nullable=True)  # long / short
    trade_type: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)  # 开单类型：trend_follow/structure_break（历史 rule_123/n_structure/rule_2b/range_edge 仅展示）
    analysis: Mapped[Optional[str]] = mapped_column(String(4000), nullable=True)
    entry_price: Mapped[Optional[float]] = mapped_column(Numeric(20, 8), nullable=True)
    stop_loss: Mapped[Optional[float]] = mapped_column(Numeric(20, 8), nullable=True)
    take_profit_1: Mapped[Optional[float]] = mapped_column(Numeric(20, 8), nullable=True)
    take_profit_2: Mapped[Optional[float]] = mapped_column(Numeric(20, 8), nullable=True)
    risk_reward_ratio: Mapped[Optional[float]] = mapped_column(Numeric(10, 2), nullable=True)
    position_pct: Mapped[Optional[float]] = mapped_column(Numeric(5, 2), nullable=True)
    recommendation: Mapped[Optional[float]] = mapped_column(Numeric(5, 2), nullable=True)  # 推荐程度 0-100
    fingerprint: Mapped[Optional[str]] = mapped_column(String(40), nullable=True, index=True)  # 信号指纹（缓存复用）
    # 原始决策时间沿复制链保留，缓存TTL不得因复制而续期。
    cache_origin_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    stage_trace: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)  # Agent 工具循环 trace（docs/04 §5.9）
    # AI 建议等回踩（2026-09-15）：入场价在现价回踩侧的 suggest——仅展示"待回踩"标识，不下单
    pullback_wait: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false", nullable=False)
    # 未开单原因（2026-09-15）：suggest 被开仓闸门拦下时记录（强平闸门/在跑上限/余额不足等），
    # 仅覆盖进入下单序列之前的拦截；下单序列内的成败由 trade_records 呈现
    open_block_reason: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    # key_levels_json 快照（docs/21，2026-09-27 上线；2026-09-28 功能整体移除后
    # 停止写入）：存量列按仓库惯例保留不 DROP，历史行原样可查
    key_levels_json: Mapped[Optional[list]] = mapped_column(JSON, nullable=True)
    # 止损锚类型（docs/23，2026-09-29）：structure/near，仅突破类信号有值；
    # near=突破K线极值近锚降级链，复盘按锚类型分组对比胜率/EV；旧行 NULL 按 structure 理解
    stop_anchor: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)
    # 规则版本（docs/24 A0，2026-09-29）：分析时规则集快照（如 v2026-09-29），
    # 复盘统计按版本分组，NULL=标记上线前的旧行（按 legacy 理解）
    rule_version: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)
    # 双评委否决原始快照（docs/24 A5a，2026-09-29）：veto 强制 skip 前的建议
    # 三价/方向/评分等（dict），复盘任务据此回放"被否决建议的假想表现"，
    # 对比通过单表现验证 veto 是否有正贡献；suggest/正常 skip 为 NULL
    # none_as_null=True（2026-10-20 卫生修正）：Python None 落 SQL NULL 而非 JSON
    # `null`——否则「无否决」与「有否决快照」在 SQL 侧同显非 NULL，`IS NOT NULL`
    # 会把普通 skip 行误纳为 veto 行（线上曾据此生成 462 条假 expired 复盘行）
    veto_original: Mapped[Optional[dict]] = mapped_column(
        JSON(none_as_null=True), nullable=True
    )
    # 战法命中标注（2026-10-02）：AI 判定该信号符合的战法名（逐字取自 tactics
    # 表白名单，防幻觉闸门外值清空）+ 判定依据；战法仅借鉴不拦截，旧行 NULL
    tactic_name: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    tactic_reason: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    # 4h 状态（2026-10-10 五次拍板建列；值域三次换血，2026-10-11 再重构为
    # EMA 状态机 11 态）：tangle/turn_bull/turn_bear/macro_cross_bull/
    # macro_cross_bear/bull_recover/bear_recover/aligned_bull/aligned_bear/
    # bull_dip/bear_rally（weak/medium/strong/extreme/exhausted/overheat/
    # cooling=退役旧档，复盘归「未知」）。分析时由 annotate_htf_phase
    # stash 落库（2026-10-21 起仅快照、不做方向否决）；仅 suggest 且有 4h
    # 数据时有值，无 4h 数据 NULL=未知，复盘「4h趋势期」维度按此分组
    htf_phase: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    # 止盈模式（历史字段：2026-10-12 策略二/trail 删除后停写，新行 NULL）：
    # 2026-10-10 docs/19 §3.4 两派哲学落地，2026-10-11 起由 entry_strategy 程序
    # 推导；ladder=分批止盈 / trail=纯跟踪。历史行 NULL 按 ladder 理解
    tp_mode: Mapped[Optional[str]] = mapped_column(String(8), nullable=True)
    # 双入场策略（历史字段：2026-10-12 策略二删除后停写，新行 NULL）：
    # s1=完整规则 / s2=EMA21 金K跟踪（已退役）；历史行 NULL 按 s1 理解
    entry_strategy: Mapped[Optional[str]] = mapped_column(String(8), nullable=True)
    # 策略二结论审计字段（历史字段：2026-10-12 策略二删除后停写，新行 NULL）：
    # 前端分区展示结论徽标曾取结构化字段
    strategy2_decision: Mapped[Optional[str]] = mapped_column(String(8), nullable=True)
    strategy2_reason: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    # 4h 数据降级标记（2026-10-21 观测 B2）：分析时 4h K线缺失/拉取失败 → True。
    # 该路径下 AI 只拿到降级提示（2026-10-21 起 AI 层不做方向兜底，无 4h 数据
    # 也不强制 skip），结论在缺少大周期背景下产出，复盘按此分组对比（B3）判断
    # 降级是否伤质量；本列上线前的历史行 FALSE（按未降级理解）
    htf_degraded: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="false", nullable=False
    )
    # BTC 环境事实缺失标记（2026-10-21，T0-5）：市场环境块构建失败/开关关闭时
    # 提示词显式标注「未知、不得据此加分」，本列记录该事实供复盘分组；
    # 本列上线前的历史行 FALSE（按环境可见理解）
    btc_env_missing: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="false", nullable=False
    )
    # 4h 状态快照时刻（2026-10-21 第二阶段 T1）：参与状态计算的最后一根已收盘
    # 4h 的收盘时间。开仓前复核陈旧度（>4h15m 禁开），缺数由 htf_degraded 覆盖；
    # NULL=本列上线前旧行（开仓复核按兼容放行，安全口径不变）
    htf_as_of: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    # 评分原始值（2026-10-21 第二阶段 T2，方案 A）：recommendation 已归一到
    # 60/70/85 档位；本列保留模型原始分供档位校准分析，NULL=本列上线前旧行
    recommendation_raw: Mapped[Optional[float]] = mapped_column(
        Numeric(5, 2), nullable=True
    )
    # 统一决策版本（2026-10-21 第二阶段 T5）：分析时 system_config.decision_version
    # 快照（补丁/配置/提示词变更都会递增），复盘按版本分组；NULL=本列上线前旧行
    decision_version: Mapped[Optional[int]] = mapped_column(nullable=True)
    # AI 结论落库时刻（2026-10-21 观测 B1）：created_at 在 latest-wins 原地更新
    # 时不刷新（沿用首次分析时刻），不能作为完成时刻；本列每次落库覆写。
    # 本列上线前的历史行 NULL（按 created_at 理解）
    ai_finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)

    scan_result: Mapped["ScanResult"] = relationship(back_populates="ai_analysis")
    trade_review: Mapped[Optional["TradeReview"]] = relationship(back_populates="ai_analysis", uselist=False)

    @property
    def review_status(self) -> Optional[str]:
        """复盘状态：loss / win_tp1 / win_tp2 / expired / None（未到观察期或 skip）"""
        return self.trade_review.outcome if self.trade_review else None


class KlineCache(Base):
    """K 线小时级缓存表

    同一小时内相同 symbol+interval 多次请求直接命中缓存；
    跨小时自动拉取新数据覆盖。
    """
    __tablename__ = "kline_caches"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    symbol: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    interval: Mapped[str] = mapped_column(String(8), nullable=False, default="1h")
    kline_hour: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)
    klines: Mapped[list] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    __table_args__ = (
        UniqueConstraint("symbol", "interval", "kline_hour", name="uq_kline_cache_key"),
    )


class TradeReview(Base):
    """AI 建议复盘：suggest 决策观察期（默认 24h，可配置）后逐 K 回放定论（docs/04 §4 Stage 6）

    outcome: loss / win_tp1 / win_tp2（触 tp1 后又触止损仍计 win_tp1）/
    censored（窗口结束仍未触发，右删失）/ expired（计划三价缺失或无效）
    """
    __tablename__ = "trade_reviews"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    ai_analysis_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("ai_analyses.id"), nullable=False, unique=True, index=True
    )
    symbol: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    direction: Mapped[Optional[str]] = mapped_column(String(8), nullable=True)
    outcome: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    tp1_hit: Mapped[bool] = mapped_column(Boolean, default=False)
    bars_to_exit: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)  # 触发时经过的已收盘K线数

    # 分析时点的统计维度快照（分组统计用）
    signal_type: Mapped[Optional[str]] = mapped_column(String(32), nullable=True, index=True)
    position: Mapped[Optional[str]] = mapped_column(String(32), nullable=True, index=True)
    touch_ref: Mapped[Optional[str]] = mapped_column(String(16), nullable=True, index=True)  # 回踩触点（docs/22）
    ema_state: Mapped[Optional[str]] = mapped_column(String(16), nullable=True, index=True)
    # 4h 背景状态 key（= AIAnalysis.htf_phase，2026-10-20 扩列）：影子 A/B 与「4h
    # 趋势期」复盘维度用；NULL=退役旧档/无 4h 数据/本列上线前的历史行
    htf_phase: Mapped[Optional[str]] = mapped_column(String(20), nullable=True, index=True)
    recommendation: Mapped[Optional[float]] = mapped_column(Numeric(5, 2), nullable=True)
    # 双评委否决单回放标记（docs/24 A5a）：True=回放的是"被 veto 建议的假想表现"，
    # 统计口径与通过单一致，用于对比评委否决的贡献；默认 False=正常 suggest 回放
    is_veto: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false", nullable=False)
    # 影子 A/B 桶标记（2026-10-20）：过渡态×回踩确认 veto 行='transition_pbc'，
    # 统计隔离用（该组不并入双评委 veto 组，见 api/scan.py review_stats）；普通行 NULL
    ab_bucket: Mapped[Optional[str]] = mapped_column(String(32), nullable=True, index=True)
    # 4h 数据降级快照（2026-10-21 观测 B3）：= AIAnalysis.htf_degraded（分析时
    # 4h K线缺失/拉取失败）。降级建议在无大周期背景下产出，复盘按此分两组
    # 对比胜率/期望/盈亏比；本列上线前的历史行 FALSE（按未降级理解）
    htf_degraded: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="false", nullable=False, index=True
    )
    # 回放时点入场诊断快照（2026-10-28，17-复盘审计 P0-1）：
    # pos / room_r / stop_atr / planned_rr / quality_score；历史行 NULL=未记录。
    # 只存 5 个分档键，完整诊断（range_flag/shadow 等）不重复落库。
    entry_diag: Mapped[Optional[dict]] = mapped_column(JSON(none_as_null=True), nullable=True)
    # 持仓期最大有利/不利偏移（2026-10-28，17-复盘审计 P1-3）：signed R 倍数，
    # 有利为正、不利为负（MFE ≥ 0、MAE ≤ 0）。区间=入场根到出场根（censored=窗口末根）；
    # NULL=历史行未记录 / 计划无效（expired）/ 入场未成交 / risk=|entry-stop|≤0
    mfe_r: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    mae_r: Mapped[Optional[float]] = mapped_column(Float, nullable=True)

    checked_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    ai_analysis: Mapped["AIAnalysis"] = relationship(back_populates="trade_review")


class BreakoutEvent(Base):
    """Donchian 破轨事件（docs/22 §4）：突破回测触点的事实源。

    T1 突破触发时 scanner upsert（同币同方向刷新位价/锚点，反向破轨旧事件
    失效）；回测触点确认（touch_ref=retest 的回踩金K）后 consumed；
    超过 retest_window_bars 窗口的 active 行由 scanner 批量置 expired。
    只记最近一次破轨（裸事件），不做 docs/21 的聚类状态机。
    """

    __tablename__ = "breakout_events"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    symbol: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    direction: Mapped[int] = mapped_column(SmallInteger, nullable=False)  # 1 多 / -1 空
    level_price: Mapped[float] = mapped_column(Numeric(20, 8), nullable=False)  # 被穿轨价（回测位）
    candle_open: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)  # 破轨信号K线开盘
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active", index=True)
    # active → consumed（回测确认）| expired（窗口过期/反向破轨）
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class PullbackPending(Base):
    """回踩金K两段式确认——Stage-1 待确认池（docs/19）

    T3 回踩命中且当根无同向金K 时建立 pending（不落 scan_results、不进 AI）；
    仅在恰好下一根已收盘K线的扫描轮有资格确认：
    - confirmed：次根出现同向 GOLDEN_PATTERNS 且多数决复验通过 → 以
      trigger_pullback_confirm 落库进 AI（confirmed_scan_result_id 可回溯）
    - expired：4h 无明确趋势（ranging：走平/缠绕/ADX弱）/
      震荡闸门命中（range_score：三线交错/分离过近/ADX<18/过度扩张）/ 次根无金K（window_expired）/ 方向失守（direction_flip）
      （structure_1h 死因 2026-10-15 随 1H 结构门降级退役，仅旧行可见）
    - superseded：次根 T1 突破或当根触碰+金K 抢先
    每币至多一条活跃 pending（status=pending）；终态行保留审计，下次触碰新建行。
    """

    __tablename__ = "pullback_pendings"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    symbol: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    direction: Mapped[int] = mapped_column(SmallInteger, nullable=False)  # 1 多 / -1 空
    # Stage-1 信号K线开盘时间（有效期判定锚点：== 下一轮 klines[-3].open_time）
    stage1_candle_open: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)
    ema21: Mapped[Optional[float]] = mapped_column(Numeric(20, 8), nullable=True)  # Stage-1 触碰锚点价（docs/22 起为命中触点位价）
    touch_ref: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)  # Stage-1 命中触点（docs/22；旧行 NULL）
    quality_score: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    votes_json: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending", index=True)
    # 终态原因：confirmed 恒空；expired=ranging/range_score/window_expired/direction_flip（structure_1h 2026-10-15 退役，仅旧行）；superseded=breakout/same_candle_confirm
    resolve_reason: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    confirm_pattern: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)  # 确认形态
    confirmed_scan_result_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )  # 确认后落库的 scan_results.id（可回溯，不用 FK 避免删除耦合）
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    resolved_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    __table_args__ = (
        Index("ix_pullback_pendings_symbol_status", "symbol", "status"),
    )


class Watchlist(Base):
    """关注列表：用户手动关注的币种，持久保存"""

    __tablename__ = "watchlist"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    symbol: Mapped[str] = mapped_column(String(32), nullable=False, unique=True)
    note: Mapped[Optional[str]] = mapped_column(String(256), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    # K 线最近一次手动刷新时间；刚添加时等于 created_at
    updated_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True, default=datetime.utcnow)
