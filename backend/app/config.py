import os
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # 应用
    APP_NAME: str = "Trading Workbench API"
    DEBUG: bool = True

    # 数据库
    DATABASE_URL: str = os.getenv(
        "DATABASE_URL",
        "postgresql://postgres:postgres@localhost:5432/trading_workbench",
    )

    # Redis / Celery
    REDIS_URL: str = os.getenv("REDIS_URL", "redis://localhost:6379/0")
    CELERY_BROKER_URL: str = os.getenv("CELERY_BROKER_URL", "redis://localhost:6379/1")
    CELERY_RESULT_BACKEND: str = os.getenv("CELERY_RESULT_BACKEND", "redis://localhost:6379/2")

    # 币安
    BINANCE_BASE_URL: str = os.getenv("BINANCE_BASE_URL", "https://api.binance.com")
    BINANCE_FUTURES_URL: str = os.getenv("BINANCE_FUTURES_URL", "https://fapi.binance.com")
    BINANCE_TIMEOUT: int = 15
    BINANCE_CONCURRENCY: int = 8

    # 扫描策略
    SCAN_INTERVAL_HOURS: int = 1
    # 定时扫描调度分钟数（single source of truth，2026-10-12）：celery beat
    # hourly-scan 的 crontab 分钟数与 market_data 尾窗宽度（60-该值）均由此派生。
    # 二者曾仅靠注释耦合：改 beat 忘改尾窗时 in_forming_tail 静默返回 False，
    # 筛选悄悄退化为「看上一根已收盘根」（不报错、不告警，只表现为信号变旧）
    # 0 = 每小时整点触发（2026-10-12 拍板，回退 v2026-10-11-4 的 :55 提前捕捉）：
    # 整点新周期刚开、没有「近收盘未收盘根」可言，故尾窗同步关闭（派生宽度 0），
    # 筛选回到「看上一根已收盘根」。信号根与 :55 口径是同一根（都是刚走完的那根），
    # 但取到的是最终收盘价而非扫描时刻快照，且时点晚 5 分钟。
    # 整点取数竞态（2026-10-13）：整点触发时新周期根可能尚未出现在取数响应里、
    # 末根实为刚收盘根——由 screen_klines「末根已收盘即视作信号根」兜底，不再当
    # 未收盘剔除致信号根错位到前一根且该根漏评（STXUSDT 09:00 吞没漏评个案）
    SCAN_BEAT_MINUTE: int = 0
    KLINE_INTERVAL: str = "1h"
    KLINE_WINDOW: int = 240
    BREAKOUT_THRESHOLD: float = 0.002  # 最小突破幅度：收盘须越过 Donchian 轨 0.2%（毛边突破不算）
    R_SQUARED_THRESHOLD: float = 0.5
    REPEAT_WINDOW_HOURS: int = 3
    SWING_ORDER: int = 3
    BREAKOUT_CHANNEL_BARS: int = 20  # Donchian 通道突破的回看根数（收盘破 N 根高低轨 + 长实体）
    BREAKOUT_VOL_RATIO: float = 1.2  # 放量加分门槛：信号K线量 ≥ 近20根中位量的1.2倍（加分项不卡死）
    BREAKOUT_MAX_PCT: float = 1.5  # 突破幅度上限：越过轨 >1.5% 视为追高，不产生突破触发（0=关闭）
    CANDLE_MAX_EMA21_DIST_PCT: float = 1.5  # 悬空金K门槛（已随纯金K下线失效，键保留向后兼容）
    RETEST_WINDOW_BARS: int = 24  # 突破回测触点有效窗口（已收盘根数，0=关闭 retest 触点）
    # 回踩待确认窗口（2026-10-13-10）：触碰根治不落金K后进入 pending 待确认池，
    # 同向金K允许出现在触碰根之后 1..N 根已收盘K线内（支持启明星/黄昏星等三根形态；
    # N=1 等价旧「恰好下一根」行为）。≥1，由 trigger 层消费。
    # 2026-10-14 默认 3→4：实测金K迟到 1 根是待确认池最大卡点，放宽约多覆盖 30% 迟到确认
    PENDING_CONFIRM_WINDOW_BARS: int = 4
    STRUCTURE_TOUCH_BAND_PCT: float = 0.3  # retest/swing 横向位触碰带宽（%，影线可入带收盘须守住）
    EMA_TOUCH_MIN_BARS: int = 200  # EMA 触碰最小已收盘根数（数据门，EMA144 收敛兜底）
    # 1h 结构门开关：已于 2026-10-12 退役（docs/21 O7）。字段保留向后兼容，
    # scanner 不再消费，scan_filters.structure_1h_gate 同理（同退役先例）
    STRUCTURE_1H_GATE: bool = True
    # 方向过滤开关（2026-10-18，配置页可调）：扫描链上两道方向硬过滤的环境默认值
    # （DB system_config.scan_filters 同名键优先，见 scan_filters.resolve_scan_filters）。
    # 默认 True=保持现网门槛行为（上线零差异），关闭=跳过该检测、放开信号池。
    # ① 方向票关：方向票同向张数 < DIR_VOTE_MIN(2) 是否否决候选
    DIRECTION_VOTE_GATE_ENABLED: bool = True
    # ② 方向冲突关：定向方向与 4h 闸门 gate_4h 方向相反时是否否决候选
    DIRECTION_CONFLICT_GATE_ENABLED: bool = True
    DAILY_LOSS_STOP_PCT: float = 9.0  # 当日亏损熔断：UTC 日已平仓净亏 ≤ -风险基数×9%（≈3笔满损）停开仓（0=关闭）
    # 当日亏损熔断总开关（2026-09-30 用户拍板开关化）：True=启用（默认，保持现行为），
    # False=关闭；阈值仍由 DAILY_LOSS_STOP_PCT 控制（9% ≈ 3 笔满损，随账户金额缩放）。
    # 注意与软熔断（CONSECUTIVE_LOSS_DERATE 提门槛）区别：本开关管的是停开仓的硬闸门
    DAILY_LOSS_STOP_ENABLED: bool = True
    # 周/月回撤分层熔断（docs/13 #2，2026-10-04）：日级熔断之上的二三级减速——
    # 周回撤（UTC 周一 0 点起已平仓净亏）≥ 风险基数×15% → 本周剩余时间单笔预算减半；
    # 月回撤 ≥ 基数×25% → 停开新仓，人工复盘确认（monthly_breaker_ack_at）后恢复。
    # 均只收紧亏损侧，盈利月份零影响；0=关闭该层
    WEEKLY_DD_STOP_PCT: float = 15.0
    MONTHLY_DD_STOP_PCT: float = 25.0
    # ATR 跟进止损倍数（docs/13 #5 Chandelier Exit，2026-10-04）：跟进 SL =
    # 持仓期极值 ∓ k×ATR(22,D1)，与摆动低点跟进取更紧者（只收紧不放松）。
    # 0=关闭 ATR 腿（退化为现行摆动跟进）；k 默认 3（海龟系标准）
    ATR_TRAIL_K: float = 3.0
    # TP 分批止盈比例（2026-10-09 配置化，% 值；system_config 页面值优先，NULL 回落此处）：
    # 有 TP2 → TP1 60% / TP2 20% / 剩余 20% 跟进；无 TP2 → TP1 80% / 剩余 20% 跟进
    TP1_QTY_PCT_WITH_TP2: float = 60.0
    TP2_QTY_PCT: float = 20.0
    TP1_QTY_PCT_NO_TP2: float = 80.0
    # P-09 事件驱动执行开关（2026-10-07，docs/16）：user-data-stream 成交事件
    # 秒级触发现有结算链路 + mark price 盈亏快照；False=纯定时巡检（现为 30min）
    # 定时巡检永久保留兜底，本开关只控制事件流是否叠加；默认关——部署后
    # 先在测试网账号验证（系统日志看「事件触发结算」）再放开正式网
    EVENT_EXEC_ENABLED: bool = False
    # 结算巡检节拍（docs/06 A2，2026-10-21）：恢复 30min（:01/:31）——移保本/HL
    # 移损是程序侧动作、只随巡检频率执行，每小时节奏（2026-10-20 改回）把
    # TP1 成交→移保本的保护时延拉到最长 60min，等于回退 P-09 保护效果
    # （归因：浮盈回撤占亏损 42%）。事件驱动（EVENT_EXEC_ENABLED）打开后
    # 本节拍降级为兜底。crontab minute 表达式：默认 "1,31"=每半小时；改回每小时用 "1"
    AUTO_TRADE_BEAT_MINUTES: str = "1,31"
    # 挂单入场流程（2026-10-17，docs/27）：现价盈亏比复算不足时，不再拦下不开单，
    # 改为在 AI 入场价挂 GTC 限价单等待成交（1 小时内未成交自动撤销）。
    # False=回退旧行为（现价 RR 不足即拦下）；TTL 为挂单存活分钟数
    PENDING_ENTRY_ENABLED: bool = True
    PENDING_ENTRY_TTL_MINUTES: int = 60
    PULLBACK_TOLERANCE: float = 0.03  # 回调容差 3%
    # 规则版本标记（docs/24 A0，2026-09-29）：随规则集变更递增（改 trigger/risk_guard/
    # 提示词纪律/分批比例均算改版），开仓与分析落库时快照到 trade_records/ai_analyses；
    # 复盘/归因按版本分组对比，跨版本混谈从机制上杜绝（NULL=上线前旧行按 legacy 理解）。
    # v2026-09-29-2：回踩金K复合确认（用户拍板）——单影线锤/流星单根不再确认，
    # 须双锤或锤+趋势K（candlestick.resolve_golden_evidence）
    # v2026-09-30：决断K线影线质量门（docs/25 §4，用户拍板）——吞没第二根、启明星/
    # 黄昏星第三根总影线 > 全长 40% 不判金K（已随 v2026-10-16 梯度加分口径整体废止，
    # 原 40% 门由本版起见 candlestick.DECISION_SHADOW_BONUS_GRADES）；
    # 同日撤销 docs/25 提示词注入
    # v2026-09-30-2：突破K线/趋势K线长实体门 0.75→0.70（用户拍板）——实体 ≥70% 全长
    # （影线合计 ≤30%）即算长实体；trigger.BREAKOUT_BODY_RANGE_MIN 与
    # candlestick.TREND_CANDLE_BODY_RANGE_MIN 两常量同步
    # v2026-10-01：金K规则三项（用户拍板）——①单锤形线/倒锤形线恢复单根确认
    # （倒锤加入 GOLDEN_12，复合确认仅剩流星线：双流星/流星+趋势K）；
    # ②看涨吞没影线门不对称化（上影 ≤15% 全长、下影不限；看跌维持总影线 ≤40%）；
    # ③启明星中K实体上限收紧 0.5→0.3（防退化为看涨吞没观感）
    # v2026-10-01-2：倒锤形线退出看涨白名单（KSMUSDT 回踩 EMA21 个案拍板）——
    # 看涨回踩语境长上影=上方抛压，不是企稳证据；看涨金K回到锤/吞没/启明星 3 种
    # v2026-10-01-3：恢复完整 12 金K（看涨 6 + 看跌 6 对称）——2026-09-12 精简掉的
    # 刺透线/孕线突破/蜻蜓/乌云盖顶/墓碑回归白名单参与信号判定；倒锤维持退出、
    # 上吊线维持被流星线替代；白名单内仅流星线走单影线复合确认
    # v2026-10-02：战法模块上线（仅借鉴标注，不影响开单结论）——AI 输出
    # tactic_name/tactic_reason 经白名单闸门防幻觉，复盘按战法分组
    # v2026-10-09：孕线突破（两向）与蜻蜓/墓碑十字移出金K白名单（用户拍板，
    # 同 2026-09-12 精简口径）——金K清单 12→8（看涨/看跌各 4）；检测器保留
    # 仅历史展示。GOLDEN_12 更名 GOLDEN_PATTERNS（数量随拍板浮动，不再入名）
    # v2026-10-10：trail RR 铁律条件豁免（拍板 B，docs/20 §四）+ 4h 结构化行/
    # 取舍提示行 signal_type 互斥/止损宽度纪律句/skip_reason 类别前缀
    # v2026-10-10-4：趋势K线实体门 0.70→0.60（用户拍板，复合确认证据层）——回踩
    # 反转根常带探底下影（KAIAUSDT 13:00 复合路径 61.8% 被 70% 门拒）；突破K线
    # 维持 0.70，"两常量同步"约定解除，此后各自独立改版
    # v2026-10-11：4h 分级再重构为 EMA 状态机 11 态（用户三轮拍板，docs/17）——
    # 锁单矩阵（缠绕/拐头双向禁开、宏观同侧态锁逆势、过渡放行）+ 质量分
    # STATE_QUALITY 全变；REC70 前向窗口同日重置（见 shadow.REC70_START）
    # v2026-10-11-2：看涨吞没上影门 15%→25%（用户拍板）——EDUUSDT 11:00 吞没
    # 上影 17.3% 被 15% 门拒、边缘拒绝个案出现；门语义（冲高遭抛压打回=犹豫）
    # 保留，容忍度放宽。看跌吞没总影线 40% 门不变
    # v2026-10-11-3：开仓现价 RR 复算加容差带 ai_rr_tol（默认 0.02），rr_now ≥
    # rr_min-tol 放行——CYBERUSDT 案例：AI 三价 RR 1.57 达标，3 分钟漂移后现价
    # 复算 1.4998 被 1.5 拦，0.0002 级漂移误杀信号
    # v2026-10-11-4：定时扫描 :02→:55，尾窗（距收盘≤5min）内未收盘根视作已收盘
    # 参与筛选（market_data.screen_klines 幽灵尾根视图，信号根前移一根）——信号
    # 提前 ~7 分钟捕捉；AI 分析/手动分析同口径对齐
    # v2026-10-11-5：止损锚纪律改版（P0/P1 提示词+程序参考+回测推导三面同源）——
    # 极值锚由「最近 5 根影线极值」收窄为「信号K线（金K/突破K线=最后一根已收盘
    # K线）最低/最高点」，两锚取更远者与摆动锚结构不变
    # v2026-10-12：止盈一距离上限（用户拍板）——第一档止盈须"够近"：
    # 距入场超过 min(TP1_MAX_ATR×ATR14, TP1_MAX_R×止损距离) 且不低于 rr_min×止损
    # 距离（保证兜底可用）判违规，打回让 AI 改挂近档或用"入场±rr_min×止损距离"兜底；
    # 同版修正 P1 止盈锚语义（_nth_swing 排除 ≤1×ATR 争夺位，与 schema 描述对齐）。
    # 背景：摆动点位稀疏/强趋势下"最近结构位"常落 3~4R，第一档命中率被拉低
    # v2026-10-12-2：止损宽度下限（用户拍板，与止盈一上限配对）——止损距入场须
    # ≥ SL_MIN_ATR×ATR14（默认 0.3），防"止损过近"被噪音扫掉、抑制 R 放大；
    # 近锚（突破类 stop_anchor=near）豁免（其止损刻意贴突破K极值）。部分回退
    # 2026-09-15 移除的"止损宽度 [0.3×ATR, 3×ATR]"，仅恢复下限
    # v2026-10-12-3：止盈一兜底拆两种情形（用户拍板）——(a) 前方无任何结构位
    # （创新高突破）仍仅设一档；(b) 结构位存在但越过距离上限时，止盈一走兜底价、
    # 止盈二保留锚定该远端结构位作趋势跑手档（原行为是一并置 0）
    # v2026-10-12-4：尾窗内近锚口径修正（用户拍板）——信号根为扫描尾窗未走完根
    # （signal_bar_provisional，极值未定型）时：① 近锚一致性校验跳过（仅告警，
    # 拿未定型极值硬比对会误伤）；② 同步取消近锚对止损宽度下限的豁免——兜底
    # 没了，改由「止损不许过近」补上，宁宽勿紧
    # v2026-10-12-5：止损锚定距离分档（用户拍板）——最近摆动结构位距入场超过
    # TP1_MAX_ATR÷rr_min（≈1.33×ATR，超过此线 RR 数学上不可能达标）时降级
    # 金K极值锚（stop_anchor=near 放开全信号类型，一致性校验同步扩展）；
    # 突破类近锚降级链保留，处理"锚不远但 RR 仍不达标"场景
    # v2026-10-12-6（用户拍板，docs/21 批次1）：策略二专项——O7 结构门降格为
    # s2 资格闸（触点=ema21 ∧ trend_gated，scanner 不再整体否决）；O1 4h 状态机
    # 下车态接入 trail 出场（fail-closed：连续 2 个 4h 桶数据缺失保守全平）；
    # O2 EMA21 减半腿双确认（连续 2 根逆侧收盘 + 末根破线 >0.1×ATR(14,1h)）
    # v2026-10-12-7（批次1 缺陷修复三处）：① close_market_now 增 exit_reason
    # 参数——4h 状态机全平落 htf_state，不再被硬编码误标 manual（飞书按钮
    # 默认 manual 不变）；② 4h 全平不预撤挂单——撤单交 close_market_now 内部
    # 「先平仓后撤单」，预撤会致平仓失败时无 SL 裸奔；③ 全平失败清理
    # exit_override（仓位仍开放时）防后续真实 SL 被 settle 误标 htf_state，
    # 仓位已归零则保留待下轮落账；EXIT_HTF_STATE 事件幂等（重试轮不重复落）
    # v2026-10-12-8（用户拍板）：金K极值锚口径恢复为「最近5根已收盘K线共同极值」
    # （v2026-10-11-5 单根口径的定点回退，C98USDT.P 案例：信号K单根低点高于
    # 前一根低点时，止损落在近期波动区间内部必被扫损）。三面同源：P0/P1 提示词 +
    # 程序参考 calc_stop_reference + 回测/虚拟账本 derive_signal_prices；
    # risk_guard 近锚一致性校验锚点同步改窗口极值（±0.5% 容差口径不变）
    # 注：上述 calc_stop_reference（程序止损参考价，2026-10-13 删除）与回测/账本
    # derive_signal_prices（2026-10-12 退役）后续均已移除，本行仅存历史语境
    # v2026-10-12-9（用户拍板，docs/21 O8）：策略二 trail 新增中途止盈腿——
    # 武装后 1h 已收盘 K 收盘价全程浮盈（已实现+浮动）≥2×ATR(22,1d)×当前持仓
    # → 市价 reduceOnly 减 1/3 落袋（一次性旗标 trail_tp_done；收盘口径剔尾
    # 抗插针；仅武装后启用；entry_atr 缺失退化 2R；与 EMA21 减半腿独立叠加，
    # 事件 TP_TRAIL）
    # v2026-10-12-10（用户拍板）：删除策略二（EMA21 金K跟踪）与 trail 纯跟踪
    # 出场链——出场恒为 ladder（TP1/TP2 分批挂单）；AI 输出退役 tp_mode/
    # entry_strategy/strategy2_* 四字段（DB 列与 API schema 保留历史透出，
    # 新行 NULL）；-6~-9 的 trail 专属出场（O1 4h 状态机全平/O2 EMA21 减半/
    # O8 浮盈落袋/开仓 RR 现价豁免）随之下线；手动接管必须填止盈位
    # v2026-10-12-11（用户拍板）：移除 trigger_trend_pullback（它并非独立信号，而是
    # 同一回踩路径的质量分层标注，已并入 trigger_pullback_confirm）；1H 结构门
    # （三线排列+快中线发散史+EMA144方向）恢复为 ema21 回踩触点的硬门（不过则否决，
    # 不再升级标注）；全局 4H 闸门（scanner gate_4h 三重否决）不变
    # v2026-10-12-12（用户拍板）：定时扫描 :55 → 整点 :00，尾窗随之关闭
    # （FORMING_TAIL_MINUTES 派生为 0，in_forming_tail 恒 False）——回退
    # v2026-10-11-4 的「未收盘根视作已收盘」提前捕捉；信号根仍是刚走完的那根，
    # 但取最终收盘价而非扫描时刻快照，时点晚 5 分钟
    # v2026-10-12-13（用户拍板）：摆动点阶数纳入触发层——swing 触点与结构突破
    # pivot 取材不再硬编码 3 阶，改为跟随「摆动点阶数」配置（flt.swing_order，
    # 读 SystemConfig 顶层列，默认 3 与 AI 事实包/K线标注同源）；默认值下信号
    # 结果不变
    # v2026-10-12-14：AI 提示词门槛块接线真实筛选配置——render_signal_gate_block
    # 不再固定 resolve(None)（只读 env 默认），由调用方（ai_tasks 双管线 /
    # api 手动分析）透传 resolve_scan_filters(cfg)，突破幅度/量比/通道根数/
    # 触碰带宽与页面筛选配置同源（此前页面改配置后 AI 口径与程序闸门分叉）；
    # 触点语义行补 ema21_4h（2026-10-09 上线）、移除 ema144 退役引用，并新增
    # 「当前启用触点」行渲染页面白名单生效值；默认配置下提示词数字不变
    # v2026-10-13（用户拍板）：看涨吞没上影门改加分项（不再卡死）——上影 ≤ 全长
    # 25% 时质量分 +3（technical.compute_quality_score），上影长只失加分、
    # 不再拒判形态（冲高遭抛压打回=犹豫的语义降为排序信号）；看跌吞没总影线 40% 门
    # 当时不变（该二值口径与 40% 硬门均已随 v2026-10-16 并入统一梯度表）
    # v2026-10-13-2（用户拍板）：整点取数竞态修复——扫描取数末根已是已收盘根
    # （开盘时刻+周期 ≤ 扫描时刻）时视作信号根（market_data.screen_klines 幽灵
    # 尾根条件由「尾窗」扩为「尾窗 ∨ 末根已收盘」；scanner/api analyze/ai_tasks
    # 三处调用点统一恒过 screen_klines），不再当未收盘剔除、信号根错位到前一根
    # 且该根被漏评（STXUSDT 09:00 吞没漏评个案）；非竞态时行为不变
    # v2026-10-13-3（用户拍板）：进待确认池准入收紧——待确认池产物恒为回踩确认
    # （trigger_pullback_confirm），故须在触发类型白名单内；且 4h 状态机双向禁开
    # （tangle/turn_*，technical.STATE_BLOCK_BOTH）时确认也必被 AI 层强制 skip，
    # 二者命中即挡在池外（scanner 漏斗新增 htf_state_lock 关卡；仅挡新进池，
    # 已有 pending 生命周期不变）
    # v2026-10-13-4（用户拍板）：状态机禁开约束延伸到「次根确认」——已有 pending
    # 被确认成候选时若 4h 状态机双向禁开（tangle/turn_*），候选必被 AI 层强制
    # skip，故不产生候选、pending 直接失效（expire/htf_state_lock；漏斗同上关）；
    # 新增/进池与确认两条路径口径一致：禁开态下回踩既不进池也不确认
    # v2026-10-13-5（用户拍板）：① 合并 4h 计算——gate_4h 与 analyze_htf_background
    # 双算的 EMA21/55/144+ADX+ATR 收敛为 technical.htf_core 一次算、core 透传
    # （去重不改行为）；② 4h 状态机双向禁开从「散落的临时子闸」提为筛选链前置门
    # （gate_4h 之后、票数之前），对全部 1h 候选（回踩·突破）一票否决、活跃 pending
    # 同轮 expire(htf_state_lock)；③ 移除上一版 upsert/confirm 的状态机子闸（仅保留
    # 正交的触发类型白名单子闸）
    # v2026-10-13-6（用户拍板）：金K确认（单个/组合）后，按金K组合自身 [min低, max高]
    # 价格带内、近 200 根 1h 落入的结构摆动高低点数加分——每点 +3、封顶 +12，计入
    # 候选质量分（只影响 AI 排序，不改方向/不拦截）。带内点越多 = 金K一口吞掉越多
    # 结构转折点、决断力越强。仅回踩金K确认候选适用（突破/无金K upsert 不加分）
    # v2026-10-13-7（用户拍板）：上条结构点入计口径由「摆动极值点本身落带内」改为
    # 「摆动K线实体（open~close）与价格带重叠即计」——长影线探带而实体在外不再计、
    # 极值在外但实体压带内则计；每点 +3 封顶 +12、仅回踩金K确认等其余不变
    # v2026-10-13-8（用户拍板）：分析文本输出格式纪律收紧——「决策结论：」节由可
    # 在一行内合写改为**按条分行**（以 1. 2. 3. 序号逐条列满足/不满足什么，最后
    # 单独一行"结论：建议开单/不建议开单"收尾，禁止写成一行长句）；P0/P1 提示词
    # 同步（ai_analyzer.SYSTEM_PROMPT 字段说明 / ai_agent 规则 12.5 与 reason schema）。
    # 前端展示配套：公共检查由双列改单列（纯展示，非规则）
    # v2026-10-13-9（用户拍板）：止盈二贴档保护——TP2 与 TP1 间距过近时程序侧
    # 静默撤档（take_profit_2=0，不进 violations、不触发 Reflection），间距下限见
    # TP2_MIN_GAP_* 常量；原「TP2<TP1 打回」硬校验同步移除。P0/P1 提示词补间距纪律
    # （不足则主动输出 take_profit_2=0）
    # v2026-10-13-10（用户拍板）：回踩待确认窗口由「恰好下一根」放宽为 N 根——
    # 触碰根进 pending 后，同向金K出现在其后 1..N 根已收盘K线内即确认（支持三根
    # 形态等待：INJUSDT 触碰根→十字星→启明星决断根个案）；窗口未满且无金K时保持
    # 等待不再当轮 expire；窗口耗尽才 window_expired。N 默认 3，见
    # PENDING_CONFIRM_WINDOW_BARS；N=1 与旧行为完全等价。
    # v2026-10-14（用户拍板，LIGHTUSDT 个案）：止盈一兜底扩第三种情形——当盈亏比不达标
    # 是唯一不达标项、且 4h 为宏观同侧六态（technical.STATE_LOCK_CONTRA，渲染层出现
    # 「方向约束（硬性）」行）时，允许改用"入场价 ± rr_min×止损距离"固定 1.5R 价作
    # 止盈一，不再 skip（缠绕/拐头与过渡态仍 skip）；程序侧零改动（TP1 距离上限的
    # max(..., rr_min×止损距离) 下限天然放行）。P0 _RULE_TP_BASE/_RULE_TP_TAIL_* +
    # P1 rule 12 同步。
    # v2026-10-14-2（用户拍板，DUSKUSDT 个案）：黑名单自动拉黑分档收紧——冷却档门槛
    # 由「实亏 ≥1.2R」提高到「≥1.5R」、冷却期 30 天缩短为 14 天；永久档门槛由 1.5R
    # 提高到 1.8R（见 blacklist_service.AUTO_RISK_OVERSHOOT/AUTO_COOLDOWN_DAYS/
    # PERMANENT_R_OVERSHOOT）。动机：测试网孪生单的超预算亏损会把币种全局拉黑
    # （register_settle 不区分网络），1.2R 门槛过松、连坐 30 天。
    # v2026-10-14-3（程序校验口径修复，非拍板）：ai_rr_tol 容差带从开仓现价闸
    # 扩到 AI 三价 RR 铁律与 TP1 距离上限——tick 取整后兜底 1.5R 价普遍复算为
    # 1.49~1.51R，零容差白烧 Reflection 重试后强制 skip（10-14 实测过半）；
    # 0.02R 带宽吞取整噪音，1.47R/1.55R 真·越界仍拦，页面置 0 回严格旧口径。
    # v2026-10-16（用户拍板，两项口径改造）：①retest 触点新增结构推导来源——新 HH
    # 收盘越位后，被它越过的前一个 HH 即突破回测位（空头镜像 LL），不再依赖 scanner
    # breakout_events 先手记录（trigger._structural_retest_level + _detect_retest 双来源）；
    # 同日边界放宽（未部署前合并，不单独 bump）：B 判定在 HH/LL 之外接受摆动序列末位
    # 的 "H"/"L"（该点无前参照，标签不足以否定其「前高/前低」身份）——放宽前整个回看窗
    # 只有 2 个摆动高点（老=H、新=HH）时推导不出 B；A 仍须为 HH/LL；
    # ②决断K线影线三处硬门（看跌吞没第二根/启明星第三根/黄昏星第三根，原
    # DECISION_SHADOW_MAX_PCT=0.40）全部删除，统一为梯度加分
    # （candlestick.DECISION_SHADOW_BONUS_GRADES：影线比 ≤10% +5、≤20% +3、≤30% +1、
    # >30% 不加分）；看涨吞没上影由二值（≤25% +3）并入同一梯度表，口径不变
    # （看涨看上影 / 看跌看下影 / 星看第三根总影线）
    # v2026-10-17（用户拍板，docs/23 方案 A 全做、不做影子直接上线）：①retest 触点
    # 默认启用——DEFAULT_TOUCH_REFS 加入 retest、前端默认同步，存量「未定制」白名单由
    # database._run_migrations 一次性补入（2026-10-16 起它已有结构推导来源，语义可靠性
    # 达标）；②方向派生修复（scanner A2）：net=0 不再静默偏置为空头，改由 4h 闸门方向
    # 兜底、无闸门方向则不出信号（现网 net=0 不可达，属防御性修复）；③深回撤语义显性化
    # （A3）：EMA55/EMA144 慢速深回撤必被 1h 方向票/方向冲突关挡下、仅插针式可过——写入
    # 代码注释 + 漏斗 direction_flip/votes 关判定说明。
    # v2026-10-18（用户拍板）：扫描链两道方向过滤开关化——新增 scan_filters 布尔键
    # direction_vote_gate_enabled（方向票关）/ direction_conflict_gate_enabled（方向冲突关），
    # 默认 True=保持现网门槛行为（上线零差异）、关闭=跳过该检测；配置页可分别启停，
    # 用于按需放开信号池观察影响（scanner 判定链条件化，见 docs/01 §4 键表）。
    # v2026-10-19（用户拍板）：止盈一距离上限提示词口径对齐程序——此前 P0/P1 提示词写成
    # "≤TP1_MAX_ATR×ATR 且 ≤TP1_MAX_R×止损距离"的绝对硬上限（TP1_MAX_R=0 时渲染成
    # "≤0×止损距离"），AI 遂把"入场±rr_min×止损距离"兜底价判为越 2×ATR 上限并误 skip；
    # 程序实为 cap=max(min(TP1_MAX_ATR×ATR, TP1_MAX_R×止损距离), rr_min×止损距离)，
    # 兜底价永远可用。改为按程序口径渲染上限描述（render_tp1_cap_desc）并显式禁止
    # "兜底超 2×ATR"式 skip（2026-10-19 线上排查：当日高置信误 skip 9 笔）。
    # v2026-10-20（用户拍板）：止损宽度纪律提示词自相矛盾修复——P0 _RULE_SL 与 P1 规则 9
    # 原写"止损宽度不设固定百分比上限；不得以「止损过宽」为由 skip"，与信号门槛块注入的
    # "止损宽度硬上限 max_stop_pct（程序硬闸，超限必弃单）"及 skip_reason 枚举 [止损过宽]
    # 直接冲突（2026-10-20 线上排查：当日 104 笔 skip 中 29 笔引用 5.5% 硬闸）。合并为一
    # 条条件句：内部纪律不设百分比上限、不得以其为由 skip，但门槛块给出程序硬闸时以硬闸
    # 为准、超限如实 skip（与 trade_engine 开仓闸门一致）。仅改提示词、程序零改动。
    # v2026-10-20-2（用户拍板）：打法口径收窄 + trade_type 白名单收敛——_RULE_STRUCT 删 2B
    # 假突破反手（逆势反手与顺势定位/4h 硬约束冲突）、123 法则收窄为"顺势方向的结构反转"；
    # 下线 range_edge（区间市在筛选层 gate_4h/range_score 已前置剔除、不产生候选），
    # risk_guard.TRADE_TYPES 由三选一收敛为 trend_follow/structure_break（range_edge 移入
    # LEGACY_TRADE_TYPE_LABELS 仅历史展示，validate_decision 判违规回炉）；退役
    # structure-break/range-bound-fade 技能（use_when 指向遗留类型永不命中、正文已内联）。
    # v2026-10-20-3（用户拍板）：trend-pullback 死技能复出——use_when 由遗留
    # signal_type（uptrend/downtrend）修正为生产类型 trigger_pullback_confirm，
    # 该技能此前永不命中、P0/P1 均不注入（render_matched/render_index 皆空）；复出后
    # P0 对回踩金K确认候选重新注入"顺势回踩"正文（改变 AI 输入=判定变化，故本版单独
    # bump）；同步清理正文退役语境（删 uptrend=HH+HL 旧分类前提、删已下线的策略二/
    # trail 盈亏比取舍句）并把止损 offset 口径对齐提示词 _RULE_SL（0.3%~0.5% 结构锚
    # + 金K窗口极值兜底两锚取更远者，原写"锚点外 0.5~1×ATR"）。
    # v2026-10-20-4（用户拍板）：过渡态 × 回踩确认影子 A/B——enforce_htf_constraint
    # 新增分支：4h 背景为 bull_dip/bear_rally（宏观逆侧过渡态）且 signal_type=
    # trigger_pullback_confirm 时程序 veto（skip + veto_original 三价快照，ab_bucket=
    # transition_pbc），不进真单；由复盘任务回放假想表现与同侧通过单对比。
    # 决策假设（2026-10-20 正式网复盘，仅正式网、剔除测试网异常单）：逆宏观回踩缺
    # 趋势跟随赖以盈利的右尾、期望为负，同侧背景同信号盈利。
    # 入库快照口径复核（trade_records.ai_snapshot->>'htf_phase'，该列 2026-10-09 起
    # 写入）：该组合 n=3（均值 −0.334R、最佳 +1.07R）、同侧对照 n=186（long 81 均
    # −0.165R / short 105 均 +0.128R，方向差异 t≈1.4、p≈0.16 不显著）——样本远不足以
    # 单独判读，仅作影子观察。原注释所引「74 笔均值 −0.29R / 最佳 +2.4R，同侧 +0.16R /
    # 最佳 +6.3R」无留存脚本、无法按上述快照列复现（疑为对历史 K 线回溯重算 4h 状态），
    # 判读前应补可复现口径。
    # 属程序判定变化（新增否决），故单独 bump。
    # v2026-10-21-1：4h 取数窗口 300→600 根（EMA144 种子残留 11%→<0.2%，斜率/
    # 走平判定稳定）+ 4h 末根已收盘竞态兜底（状态不再滞后一根），判定输入变化故 bump。
    # v2026-10-21-2（用户拍板）：4h 方向配对职责收敛到筛选层——「多头排列+做多 /
    # 空头排列+做空」只作 scanner 选币逻辑；P0/P1 提示词删除 _RULE_HTF_HARD 硬约束
    # （状态约束/方向约束），改为只注入 state/macro 数据行，AI 结合结构·量能·方向票
    # 自主判断方向；enforce_htf_constraint 退役为 annotate_htf_phase（仅落 htf_phase
    # 快照，不再双向禁开/锁逆势/过渡态影子 veto）；评分档位 60 不再要求 4h 同侧，
    # 4h 同向降为 70/85 档加分项；_RULE_TP_BASE 唯一放宽不再附加 4h 宏观同侧条件。
    RULE_VERSION: str = "v2026-10-21-2"

    # 排除的币种关键字（杠杆代币等）
    EXCLUDE_KEYWORDS: list = ["UP", "DOWN", "BEAR", "BULL", "HALF", "SHORT", "LONG"]
    QUOTE_ASSET: str = "USDT"

    # AI 分析（OpenAI 兼容接口）
    AI_ENABLED: bool = False
    AI_API_KEY: str = ""
    AI_BASE_URL: str = "https://api.openai.com/v1"
    AI_MODEL: str = "gpt-4o-mini"
    # 飞书通知（业务日报/持仓建议/平仓结果）统一走自建应用机器人；
    # 旧 FEISHU_NOTIFY_WEBHOOK 自定义机器人已弃用（收不到卡片按钮回调）
    # 飞书卡片操作（市价平仓按钮）：FEISHU_ACTION_TOKEN=操作令牌（按钮 value 携带校验）。
    # 自建应用三件套（APP_ID/APP_SECRET/TARGET_ID）未配置时通知静默跳过
    FEISHU_ACTION_TOKEN: str = ""
    # 飞书自建应用机器人（卡片按钮回调走长连接，点击平仓 → 会话内新卡片回报结果）
    FEISHU_APP_ID: str = ""
    FEISHU_APP_SECRET: str = ""
    # 卡片发送目标：群 chat_id（oc_ 开头）或个人 open_id（ou_ 开头），发送时按前缀自动识别
    FEISHU_TARGET_ID: str = ""

    # AI 管线（P0，docs/04 §10）
    LLM_CONCURRENCY: int = 4          # LLM 全局并发上限（Redis 信号量；2026-09-20 4→8，
                                      # 2026-09-24 8→4：降低网关拥塞，单次调用回落 85~130s 区间，
                                      # 10 币批量需 3 轮 ≈5~7min）
    AI_CALL_TIMEOUT_S: int = 300      # 单次 LLM 调用超时（防网关挂起拖垮整轮分析；
                                      # 实测单轮 85~130s，拥塞尾延迟随机破 240s →
                                      # 2026-09-28 240→300 覆盖尾部，留足余量）
    # JSON 输出模式（docs/24 D2，2026-09-29）：开启时 chat/completions 请求带
    # response_format={"type":"json_object"}（OpenAI 兼容参数），促使模型直接产出
    # 合法 JSON、降低解析失败重试率；默认关——部分兼容网关不支持该参数（会 400）
    LLM_JSON_MODE: bool = False
    AI_RR_MIN: float = 1.5            # 盈亏比复算下限（交易系统铁律）
    AI_RR_TOL: float = 0.02           # RR 复算容差（R 单位绝对带宽，三处共用，2026-10-14-3 起）：
                                      # ① 开仓现价闸（容忍 AI 报价到成交间漂移）；
                                      # ② AI 三价 RR 铁律；③ TP1 距离上限 R 带。
                                      # 吞 tick 取整擦边（1.49~1.51R），0=严格旧口径
    # 止损宽度下限（2026-10-12-2）：止损距入场须 ≥ SL_MIN_ATR×ATR14，防"止损过近"
    # 被噪音扫掉并抑制 R 放大。近锚（突破类 stop_anchor=near）豁免；ATR 不可算
    # （K线不足）放行；0 = 关闭。宽度上限仍走 signal_gate.max_stop_pct + 动态杠杆
    SL_MIN_ATR: float = 0.3            # 止损宽度下限倍数（约 0.3 根 1h K 线全幅）
    # 止盈一距离上限（2026-10-12）：第一档止盈须"够近"——距入场不超过
    # min(TP1_MAX_ATR×ATR14, TP1_MAX_R×止损距离)，且不低于 rr_min×止损距离
    # （下限保证"入场±rr_min×止损距离"兜底永远可用、不会打回死循环）。
    # 超限判违规、走 Reflection 打回让 AI 改挂更近结构位或用兜底价（程序不改价）。
    # 策略二 trail 豁免已随退役移除（2026-10-12，出场恒 ladder）；ATR 不可算
    # 时只用 R 腿。
    # 0 = 关闭该腿（两腿都为 0 则整条上限失效）
    TP1_MAX_ATR: float = 2.0          # ATR 腿上限倍数（价格波动口径：约 2 根 1h K 线全幅）
    # R 腿默认关（0）：现有测试/既有单允许到 ~4R，硬性 3R 上限是更宽的策略变更，
    # 需用户显式拍板再开（置 3.0 即"距入场 ≤3 倍止损距离"，窄止损单会被强制走兜底）
    TP1_MAX_R: float = 0.0            # R 腿上限倍数（风险口径），0=关闭
    # 止盈二贴档保护（2026-10-13）：TP2 与 TP1 间距过近时程序侧静默撤档
    # （take_profit_2 置 0，不进 violations、不触发 Reflection 重试）——两档几乎
    # 同时触发则分批意义消失（留半仓吃不到额外空间、还多一份手续费）。间距下限
    # gap_min = max(TP2_MIN_GAP_ATR×ATR14, TP2_MIN_GAP_R×止损距离,
    # 2×TAKER_FEE_RATE×入场价)；方向距离 d=（多）TP2−TP1 /（空）TP1−TP2，
    # d≤0 或 d<gap_min 即撤档。ATR 不可算时 ATR 腿置 0。
    # 两腿都为 0 则整条保护失效（含手续费地板）；默认 0.5/0.5（待 TUTUSDT 实测校准）
    TP2_MIN_GAP_ATR: float = 0.5      # 间距下限 ATR 腿倍数（0=关闭该腿）
    TP2_MIN_GAP_R: float = 0.5        # 间距下限 R 腿倍数（0=关闭该腿）
    RISK_BUDGET_PCT: float = 3.0      # 单笔固定亏损预算（%账户资金）：仓位 = 预算÷止损距离%，触止损恰好亏3%
    FINGERPRINT_TTL_MIN: int = 60     # 指纹缓存复用窗口（分钟，0.3% 价格分桶）
    AI_MAX_PER_SCAN: int = 10         # 单次扫描批量 AI 分析上限（按24h成交额取前N）
    MIN_VOLUME_24H: float = 3_000_000  # 扫描候选池 24h 成交额下限（USDT），低于此不进扫描
    NEW_COIN_COOLDOWN_DAYS: int = 14   # 新币冷静期（天）：上市未满 N 天不进扫描（0=关闭），
                                       # scan_filters.new_coin_cooldown_days 可覆盖

    # 合约回测（docs/16 P-06）：单任务规模上限（防误配拖垮 worker 与交易所限速）
    BACKTEST_MAX_SYMBOLS: int = 50     # 单任务币种数上限（源B 每币一次全段拉取）
    BACKTEST_MAX_DAYS: int = 90        # 单任务回看天数上限

    # 市场环境注入（docs/24 A4①）：AI 事实包（P0 单次调用管线）追加本币资金费率
    # 与 BTC 状态摘要——系统提示词要求"勿臆测"，普跌日无环境数据 AI 只能凭本币
    # 指标判断；任一数据源失败静默降级为不注入
    MARKET_CONTEXT_ENABLED: bool = True   # 环境注入总开关（False=不注入，测试/降级用）
    BTC_CRASH_PAUSE_PCT: float = 1.0      # BTC 1h 近3根累计急跌阈值（%，跌破注入
                                          # 「BTC 短线急跌，山寨开单需谨慎」警示；0=关闭警示）
                                          # 2026-10-13 起仅作 AI 提示词警示，不再拦截开单

    # 自动交易（docs/06，币安 USDT-M 合约）
    # 账号凭据（docs/17 注册表在 account_registry.py 读取）：正式网统一命名
    # BINANCE_LIVE_{N}_API_KEY/SECRET_KEY（N≥1，注册表直接扫 os.environ，不走本字段）；
    # 此处仅保留旧变量 BINANCE_API_KEY/SECRET_KEY 作 live-1 兼容回落与测试网兜底
    # （测试网未单独配置时回落正式网 key，兼容只有一组 key 的部署）
    BINANCE_API_KEY: str = ""
    BINANCE_SECRET_KEY: str = ""
    BINANCE_TESTNET_API_KEY: str = ""
    BINANCE_TESTNET_SECRET_KEY: str = ""
    # 现货测试网专用 key（可选）：现货测试网（testnet.binance.vision）与合约测试网
    # （testnet.binancefuture.com）是两套独立系统，key 不互通（2026-09-24 实证）。
    # 未配置时回落 BINANCE_TESTNET_*，保持旧行为
    BINANCE_SPOT_TESTNET_API_KEY: str = ""
    BINANCE_SPOT_TESTNET_SECRET_KEY: str = ""
    TRADING_ENABLED: bool = False     # （废弃 2026-10-03：拆分为下方双网开关；仅旧列首次注入用）
    # 双网自动交易开关初始值（运行时以 system_config.trading_enabled_testnet/live 为准，
    # 此值仅首次建行注入）：测试网默认开 / 正式网默认关（实盘安全基线，需页面显式开启）。
    # TRADING_TESTNET 已废弃（同日删环境切换：自动开单按双网开关、手动开单显式选账号）
    TRADING_ENABLED_TESTNET: bool = True
    TRADING_ENABLED_LIVE: bool = False
    TRADING_MIN_RECOMMENDATION: float = 60.0  # 开单推荐度门槛
    TRADING_RISK_PCT: float = 3.0     # 止损金额占风险基数比例（%）
    # 近锚单风险折价（docs/23，2026-09-29）：突破类 stop_anchor=near 的单，
    # 风险预算 ×此系数——止损贴突破K极值被扫率未知（观察期敞口折价），
    # 且止损窄使名义仓位放大、手续费/滑点磨损绝对额相应放大（30→21 USDT 例）。
    # 不改变 EV/R 符号只缩敞口；复盘胜率验证后可调回 1.0 放开
    NEAR_ANCHOR_RISK_FACTOR: float = 0.7
    # 连损降速软熔断（docs/24 A3）：当日连续亏损时提升开仓评分门槛
    # （连损 2 → 评分 ≥75，连损 ≥3 → ≥85，绝对档位；低于现行门槛值时不生效），
    # 且定时批量分析配额减半——连损趋势日降频观望，但不像当日亏损熔断那样停摆；
    # False=关闭。2026-09-30 用户拍板默认关闭（False）：评分门槛已由
    # TRADING_MIN_RECOMMENDATION 统一把关，连损日额外提分档属重复设限，
    # 且拦截理由「连损3笔降速」对用户不可预期
    CONSECUTIVE_LOSS_DERATE: bool = False
    # 组合热度上限（docs/24 A3'）：在跑单（OPENED/TP1_HIT/TP2_HIT）止损金额合计
    # / 钱包余额的上限（0.15=15%），本单预估止损计入后超限则不开新单；
    # 0=关闭。2026-09-29 用户拍板默认 0（关闭）：单数控制（max_open_trades +
    # 同向上限）已足够，金额维度不重复设限
    PORTFOLIO_HEAT_CAP: float = 0.0
    # 同向持仓上限（docs/24 A3'）：同方向（long/short）在跑单数上限，
    # 防趋势同震时单边敞口过度集中；0=不限。2026-09-30 用户拍板默认 0（关闭）：
    # 单数总量控制（max_open_trades=10）已足够，方向维度也不设限；
    # 代码保留，env 置正数可随时启用
    SAME_DIRECTION_CAP: int = 0
    # 盘口价差预检（docs/24 A4'）：开仓前 bookTicker 的 bid-ask 价差/中间价上限
    # （0.003=0.3%），价差过宽说明流动性枯竭/行情剧烈，市价成交即承担点差成本，
    # 放弃开单（查询异常不拦截，走滑点实测兜底）；0=关闭
    SPREAD_MAX_PCT: float = 0.003
    # Taker 手续费率（docs/24 A1''）：保本止损价 = 成本价 ± 往返费用缓冲
    # （开+平两腿各收一次）——窄止损下费用/R 被名义杠杆放大，
    # 纯成本价保本实际仍亏往返手续费
    TAKER_FEE_RATE: float = 0.0005
    # 风险基数取整（2026-09-21 由分档表改首位数取整：4800→4000、5200→5000，
    # 低于 10不开仓；字段保留供 .env 兼容，代码已不再读取）
    TRADING_CAPITAL_TIERS: str = "100,200,500,1000,2000,5000,10000"
    TRADING_MIN_FREE_PCT: float = 0.5  # 可用余额/总资金 下限（2026-09-15 由 70% 改 50%；系统配置表 trading_min_free_pct 可改，此值仅作缺列/缺行兜底）
    TRADING_LEVERAGE: int = 20        # 杠杆上限（逐仓 ISOLATED；止损距离超闸门时动态下调）
    TRADING_LEVERAGE_MIN: int = 8     # 动态杠杆下限默认值（system_config.min_adapt_leverage 可页面覆盖）：
                                      # 限制层——安全杠杆低于下限的宽止损单放弃，不是所有单都算完就开

    class Config:
        env_file = ".env"
        # 多账号 .env 变量（BINANCE_LIVE_{N}_*）由 account_registry 直接扫
        # os.environ，Settings 不逐个声明；ignore 避免 extra_forbidden 误杀
        extra = "ignore"


settings = Settings()
