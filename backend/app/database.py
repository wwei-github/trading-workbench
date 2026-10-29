from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker, DeclarativeBase

from app.config import settings

engine = create_engine(settings.DATABASE_URL, pool_pre_ping=True, echo=False)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


class Base(DeclarativeBase):
    pass


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def _run_migrations(engine):
    """幂等迁移：create_all 不会给已存在表加列，用 ALTER TABLE 补充。"""
    from app.config import settings

    with engine.begin() as conn:
        # trade_records 加触发信号类型列（2026-09-25）：开单时自 AIAnalysis.signal_type 落快照
        conn.execute(text(
            "ALTER TABLE trade_records "
            "ADD COLUMN IF NOT EXISTS signal_type VARCHAR(32)"
        ))
        # trade_records 加触发K线形态列（2026-09-25）：开单时自 ScanResult.pattern 落快照
        conn.execute(text(
            "ALTER TABLE trade_records "
            "ADD COLUMN IF NOT EXISTS pattern VARCHAR(32)"
        ))
        # Feature 1: scan_results 加 volume / volume_type
        conn.execute(text("""
            ALTER TABLE scan_results
              ADD COLUMN IF NOT EXISTS volume NUMERIC(20,8) NOT NULL DEFAULT 0,
              ADD COLUMN IF NOT EXISTS volume_type VARCHAR(16) NOT NULL DEFAULT '平量'
        """))
        conn.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_scan_results_volume_type "
            "ON scan_results (volume_type)"
        ))
        # scan_results 加命中位置文案列（2026-10-01）：回踩确认=触点位/突破=前高前低轨价，
        # 前端「位置」列展示（旧行 NULL 回退 touch_ref 渲染）
        conn.execute(text(
            "ALTER TABLE scan_results "
            "ADD COLUMN IF NOT EXISTS position_ref VARCHAR(48)"
        ))
        # Feature 2: system_config 加扫描策略列（若表已存在但缺列）
        conn.execute(text("""
            ALTER TABLE system_config
              ADD COLUMN IF NOT EXISTS kline_interval VARCHAR(8) NOT NULL DEFAULT '1h',
              ADD COLUMN IF NOT EXISTS kline_window INTEGER NOT NULL DEFAULT 240,
              ADD COLUMN IF NOT EXISTS breakout_threshold NUMERIC(10,6) NOT NULL DEFAULT 0.005,
              ADD COLUMN IF NOT EXISTS r_squared_threshold NUMERIC(10,6) NOT NULL DEFAULT 0.5,
              ADD COLUMN IF NOT EXISTS repeat_window_hours INTEGER NOT NULL DEFAULT 24,
              ADD COLUMN IF NOT EXISTS swing_order INTEGER NOT NULL DEFAULT 3,
              ADD COLUMN IF NOT EXISTS pullback_tolerance NUMERIC(10,6) NOT NULL DEFAULT 0.03,
              ADD COLUMN IF NOT EXISTS strategy_prompt_enabled BOOLEAN NOT NULL DEFAULT FALSE,
              ADD COLUMN IF NOT EXISTS strategy_prompt TEXT NOT NULL DEFAULT '',
              ADD COLUMN IF NOT EXISTS fib_enabled BOOLEAN NOT NULL DEFAULT FALSE
        """))
        # Feature 3: scan_results 加信号位置列（形态/突破方向派生）。
        # 2026-09-14 关键位功能移除（docs/09）：key_level_tolerance / level_merge_threshold /
        # key_levels 列不再新建与映射；存量库中的历史列按仓库惯例保留不 DROP（历史行不动）
        conn.execute(text("""
            ALTER TABLE scan_results
              ADD COLUMN IF NOT EXISTS position VARCHAR(32)
        """))
        # Feature 4: EMA 均线形态状态
        conn.execute(text("""
            ALTER TABLE scan_results
              ADD COLUMN IF NOT EXISTS ema_state VARCHAR(16)
        """))
        conn.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_scan_results_ema_state "
            "ON scan_results (ema_state)"
        ))
        # Feature 5: 关注列表 K 线刷新时间（历史行回填为添加时间）
        conn.execute(text("""
            ALTER TABLE watchlist
              ADD COLUMN IF NOT EXISTS updated_at TIMESTAMP
        """))
        conn.execute(text(
            "UPDATE watchlist SET updated_at = created_at WHERE updated_at IS NULL"
        ))
        # Feature 6: AI 管线 P0——扫描结果强度 + AI 决策指纹（docs/04）
        conn.execute(text("""
            ALTER TABLE scan_results
              ADD COLUMN IF NOT EXISTS strength NUMERIC(6, 4)
        """))
        conn.execute(text("""
            ALTER TABLE ai_analyses
              ADD COLUMN IF NOT EXISTS fingerprint VARCHAR(40)
        """))
        conn.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_ai_analyses_fingerprint "
            "ON ai_analyses (fingerprint)"
        ))
        # Feature 8: AI trace（docs/04 P2）
        conn.execute(text("""
            ALTER TABLE ai_analyses
              ADD COLUMN IF NOT EXISTS stage_trace JSON
        """))
        # Feature 11: 移除复盘记忆注入功能，清理残留列
        conn.execute(text("""
            ALTER TABLE system_config
              DROP COLUMN IF EXISTS memory_injection_enabled
        """))
        # Feature 9: 开单类型（结构打法归类：顺势/123法则/N字/2B/区间边缘）
        conn.execute(text("""
            ALTER TABLE ai_analyses
              ADD COLUMN IF NOT EXISTS trade_type VARCHAR(16)
        """))
        # Feature 12: 自动交易（docs/06）——在跑单子上限
        conn.execute(text("""
            ALTER TABLE system_config
              ADD COLUMN IF NOT EXISTS max_open_trades INTEGER NOT NULL DEFAULT 5
        """))
        # 开单策略开关（默认：顺势/区间边缘开，结构破位关）
        # 2026-09-15 拍板：开关不再拦截开仓（trade_type 仅作记录），DB 列保留不删
        conn.execute(text("""
            ALTER TABLE system_config
              ADD COLUMN IF NOT EXISTS strategy_trend_follow_enabled BOOLEAN NOT NULL DEFAULT TRUE,
              ADD COLUMN IF NOT EXISTS strategy_structure_break_enabled BOOLEAN NOT NULL DEFAULT FALSE,
              ADD COLUMN IF NOT EXISTS strategy_range_edge_enabled BOOLEAN NOT NULL DEFAULT TRUE
        """))
        # 余额风控可配置（2026-09-15）：开仓最低可用余额占比，默认 50%（原固定 70% 规则）
        conn.execute(text("""
            ALTER TABLE system_config
              ADD COLUMN IF NOT EXISTS trading_min_free_pct NUMERIC(10,6) NOT NULL DEFAULT 0.5
        """))
        # 待回踩标记（2026-09-15）：AI 建议等回踩的 suggest 结论（入场价在现价回踩侧）
        # 仅展示"待回踩"标识、不参与下单（限价委托已放弃，docs/10）
        conn.execute(text("""
            ALTER TABLE ai_analyses
              ADD COLUMN IF NOT EXISTS pullback_wait BOOLEAN NOT NULL DEFAULT FALSE
        """))
        # 未开单原因（2026-09-15）：suggest 结论被开仓闸门拦下时记录具体原因，
        # 前端 AI 分析卡展示（下单序列内的成败由 trade_records 呈现，不走此列）
        conn.execute(text("""
            ALTER TABLE ai_analyses
              ADD COLUMN IF NOT EXISTS open_block_reason VARCHAR(255)
        """))
        # Feature 12: 自动交易——环境标识（测试网/正式网）与开单时 AI 结论快照
        conn.execute(text("""
            ALTER TABLE trade_records
              ADD COLUMN IF NOT EXISTS testnet BOOLEAN NOT NULL DEFAULT TRUE,
              ADD COLUMN IF NOT EXISTS ai_snapshot JSON
        """))
        # 资金分析环境口径（2026-09-20）：划转记录标记来源环境，存量回填 TRUE = 测试网账户流水
        conn.execute(text("""
            ALTER TABLE account_transfers
              ADD COLUMN IF NOT EXISTS testnet BOOLEAN NOT NULL DEFAULT TRUE
        """))
        # 总开关运行时化（2026-09-20）：由 env 静态配置迁移进 DB，页面可改。
        # trading_testnet 列已废弃（2026-10-03 删环境切换，代码不读写；旧库
        # 列保留不 DROP 防回滚）——新部署不再创建该列
        conn.execute(text(
            f"ALTER TABLE system_config "
            f"ADD COLUMN IF NOT EXISTS trading_enabled BOOLEAN NOT NULL DEFAULT {settings.TRADING_ENABLED}"
        ))
        # 双网自动交易开关拆分（2026-10-03）：trading_enabled →
        # trading_enabled_testnet（默认开）/ trading_enabled_live（默认关）。
        # 存量回填一次性（列刚新增时）：testnet 继承旧总开关现值、live 恒 FALSE
        # （实盘安全基线，需页面显式开启）——守卫判断防重启把用户已改的开关重置
        _has_tn_switch = conn.execute(text(
            "SELECT 1 FROM information_schema.columns "
            "WHERE table_name = 'system_config' AND column_name = 'trading_enabled_testnet'"
        )).first()
        if not _has_tn_switch:
            conn.execute(text(
                "ALTER TABLE system_config "
                f"ADD COLUMN trading_enabled_testnet BOOLEAN NOT NULL DEFAULT {settings.TRADING_ENABLED_TESTNET}, "
                f"ADD COLUMN trading_enabled_live BOOLEAN NOT NULL DEFAULT {settings.TRADING_ENABLED_LIVE}"
            ))
            conn.execute(text(
                "UPDATE system_config SET trading_enabled_testnet = trading_enabled"
            ))
        # 测试网飞书通知开关（2026-10-15，默认开）：关=发送层拦截标注 testnet
        # 的合约单据卡片/文本。必须在下方裸 INSERT 之前执行（老库先补列，
        # INSERT 引用该列才不炸）；存量行默认 TRUE（向后兼容，通知不丢）
        conn.execute(text(
            "ALTER TABLE system_config "
            "ADD COLUMN IF NOT EXISTS feishu_testnet_notify_enabled "
            "BOOLEAN NOT NULL DEFAULT TRUE"
        ))
        # 双网同开的 DB 防重（2026-10-03）：ai_analysis_id 全局 unique 会挡住
        # 同一 AI 分析在两网各开一单（应用层防重本就按账号，docs/17 P1）——
        # 改复合唯一 (ai_analysis_id, account_key, testnet)。旧行 account_key
        # NULL 在 PG 唯一索引中互不冲突（多 NULL 合法），不受影响
        conn.execute(text(
            "ALTER TABLE trade_records "
            "DROP CONSTRAINT IF EXISTS trade_records_ai_analysis_id_key"
        ))
        # 旧唯一在线上实际是 UNIQUE INDEX 形态（ix_trade_records_ai_analysis_id，
        # 建库早期对 unique 列同时声明 index 的产物），DROP CONSTRAINT 摸不到它——
        # 与复合唯一并存导致双网镜像单第二网插入必撞 UniqueViolation，并触发
        # 开仓保险丝（撤 SL/TP + reduceOnly 市价平仓）：2026-10-03 线上事故
        # UBUSDT/MANAUSDT live 开仓即被平。幂等补删 INDEX 形态
        conn.execute(text(
            "DROP INDEX IF EXISTS ix_trade_records_ai_analysis_id"
        ))
        conn.execute(text(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_trade_analysis_account "
            "ON trade_records (ai_analysis_id, account_key, testnet)"
        ))
        # 指标驱动筛选（2026-09-17）：扫描候选质量分（AI 配额排序键）与指标票数明细
        conn.execute(text("""
            ALTER TABLE scan_results
              ADD COLUMN IF NOT EXISTS quality_score INTEGER,
              ADD COLUMN IF NOT EXISTS votes_json JSON
        """))
        # 导入持仓（2026-09-24）：手动单接管无 AI 分析行，ai_analysis_id 放开非空
        # （unique 约束保留，PG 允许多个 NULL）
        conn.execute(text(
            "ALTER TABLE trade_records ALTER COLUMN ai_analysis_id DROP NOT NULL"
        ))
        # 复盘重设计（2026-09-18）：实盘费用拆分 + AI 错误归因（trade_records）
        conn.execute(text("""
            ALTER TABLE trade_records
              ADD COLUMN IF NOT EXISTS fee_total NUMERIC(20, 6),
              ADD COLUMN IF NOT EXISTS funding_total NUMERIC(20, 6),
              ADD COLUMN IF NOT EXISTS error_tags JSON,
              ADD COLUMN IF NOT EXISTS error_lesson TEXT,
              ADD COLUMN IF NOT EXISTS attributed_at TIMESTAMP
        """))
        # 在跑单浮动收益 + AI 持仓建议（docs/16，2026-09-20）
        conn.execute(text("""
            ALTER TABLE trade_records
              ADD COLUMN IF NOT EXISTS unrealized_pnl NUMERIC(20, 6),
              ADD COLUMN IF NOT EXISTS ai_advice JSON,
              ADD COLUMN IF NOT EXISTS ai_advice_at TIMESTAMP,
              ADD COLUMN IF NOT EXISTS ai_advice_pnl NUMERIC(20, 6)
        """))
        # 规则补丁闭环（docs/12 §10，2026-09-18）：prompt_patches 全新表由
        # create_all 建表（init_db 已导入 app.models.patch），此处仅补索引与参数列
        conn.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_prompt_patches_status "
            "ON prompt_patches (status)"
        ))
        conn.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_prompt_patches_target_tag "
            "ON prompt_patches (target_tag)"
        ))
        conn.execute(text("""
            ALTER TABLE system_config
              ADD COLUMN IF NOT EXISTS max_stop_pct NUMERIC(10, 6),
              ADD COLUMN IF NOT EXISTS liq_gate_buffer NUMERIC(10, 6),
              ADD COLUMN IF NOT EXISTS min_adapt_leverage INTEGER,
              ADD COLUMN IF NOT EXISTS pullback_market_pct NUMERIC(10, 6),
              ADD COLUMN IF NOT EXISTS max_slippage_pct NUMERIC(10, 6),
              ADD COLUMN IF NOT EXISTS testnet_transfer_purge_at TIMESTAMP
        """))
        # 风控参数上页面（2026-10-02）：评分门槛/熔断/盈亏比/风险比例/杠杆上限/
        # 费率/近锚折价/组合热度/同向上限/BTC 急跌——全部 nullable（NULL=回落 env）
        conn.execute(text("""
            ALTER TABLE system_config
              ADD COLUMN IF NOT EXISTS min_recommendation INTEGER,
              ADD COLUMN IF NOT EXISTS daily_loss_stop_pct NUMERIC(10, 6),
              ADD COLUMN IF NOT EXISTS daily_loss_stop_enabled BOOLEAN,
              ADD COLUMN IF NOT EXISTS ai_rr_min NUMERIC(10, 6),
              ADD COLUMN IF NOT EXISTS risk_pct NUMERIC(10, 6),
              ADD COLUMN IF NOT EXISTS leverage_max INTEGER,
              ADD COLUMN IF NOT EXISTS taker_fee_rate NUMERIC(10, 6),
              ADD COLUMN IF NOT EXISTS near_anchor_factor NUMERIC(10, 6),
              ADD COLUMN IF NOT EXISTS portfolio_heat_cap NUMERIC(10, 6),
              ADD COLUMN IF NOT EXISTS same_direction_cap INTEGER,
              ADD COLUMN IF NOT EXISTS btc_crash_pause_pct NUMERIC(10, 6),
              ADD COLUMN IF NOT EXISTS ai_rr_tol NUMERIC(10, 6)
        """))
        # AI 运行时参数上页面（2026-10-14，策略配置弹窗）：模型名/端点/并发/超时
        # 全部 nullable（NULL=回落 config.py env 默认）；
        # ai_api_key（2026-10-26）：与 ai_base_url 成对配置才生效，缺一整体回落 env
        conn.execute(text("""
            ALTER TABLE system_config
              ADD COLUMN IF NOT EXISTS ai_model VARCHAR(64),
              ADD COLUMN IF NOT EXISTS ai_api_key VARCHAR(255),
              ADD COLUMN IF NOT EXISTS ai_base_url VARCHAR(255),
              ADD COLUMN IF NOT EXISTS llm_concurrency INTEGER,
              ADD COLUMN IF NOT EXISTS ai_call_timeout_s INTEGER
        """))
        # 多账号支持（docs/17 P0，2026-09-23）：账号配置走 .env 注册表，存量表
        # 补 account_key 归属列并按 testnet 布尔回填（testnet→testnet-1，正式→live-1）
        conn.execute(text(
            "ALTER TABLE system_config "
            "ADD COLUMN IF NOT EXISTS current_account_key VARCHAR(20)"
        ))
        # P1 扇出（docs/17 §3）：启用自动交易的正式网账号集合（逗号分隔）；
        # 回填 = 当前选中账号（P0 行为等价迁移：单账号启用集合）
        conn.execute(text(
            "ALTER TABLE system_config "
            "ADD COLUMN IF NOT EXISTS live_trading_keys TEXT"
        ))
        conn.execute(text(
            "UPDATE system_config SET live_trading_keys = current_account_key "
            "WHERE live_trading_keys IS NULL AND current_account_key IS NOT NULL"
        ))
        conn.execute(text(
            "ALTER TABLE trade_records "
            "ADD COLUMN IF NOT EXISTS account_key VARCHAR(20)"
        ))
        conn.execute(text(
            "ALTER TABLE account_transfers "
            "ADD COLUMN IF NOT EXISTS account_key VARCHAR(20)"
        ))
        # 初始资金（2026-09-26）：划转同步首次接入只回看 7 天，早于接入的入金
        # 不进 account_transfers，资金划转面板用本字段展示初始资金
        conn.execute(text(
            "ALTER TABLE system_config "
            "ADD COLUMN IF NOT EXISTS live_initial_capital NUMERIC(12, 2) NOT NULL DEFAULT 0"
        ))
        # TP2 分批止盈开关（2026-10-08）：默认关——出场走「仅 TP1 80% + 剩余 20% 跟进」。
        # 必须在下方裸 INSERT 之前执行：老库先补列（INSERT 引用该列才不炸）
        conn.execute(text(
            "ALTER TABLE system_config "
            "ADD COLUMN IF NOT EXISTS tp2_enabled BOOLEAN NOT NULL DEFAULT FALSE"
        ))
        # 止盈全平模式（2026-10-28）：默认关——TP 档后剩余由跟进止损退出；
        # 开启后有 TP2 由 TP2 平掉剩余全部、无 TP2 由 TP1 平掉全部仓位
        conn.execute(text(
            "ALTER TABLE system_config "
            "ADD COLUMN IF NOT EXISTS tp_full_exit_enabled BOOLEAN NOT NULL DEFAULT FALSE"
        ))
        # TP 分批比例（2026-10-09，% 值可配）：NULL=env 默认 60/20/80
        conn.execute(text(
            "ALTER TABLE system_config "
            "ADD COLUMN IF NOT EXISTS tp1_qty_pct_with_tp2 NUMERIC(5, 2)"
        ))
        conn.execute(text(
            "ALTER TABLE system_config "
            "ADD COLUMN IF NOT EXISTS tp2_qty_pct NUMERIC(5, 2)"
        ))
        conn.execute(text(
            "ALTER TABLE system_config "
            "ADD COLUMN IF NOT EXISTS tp1_qty_pct_no_tp2 NUMERIC(5, 2)"
        ))
        # 止盈模式（2026-10-10，docs/19 §3.4；2026-10-11 起由 entry_strategy 程序
        # 推导落库，不再有 system_config.tp_mode 配置列）：ai_analyses.tp_mode 存
        # 推导值，旧行 NULL 按 ladder 理解
        conn.execute(text(
            "ALTER TABLE ai_analyses "
            "ADD COLUMN IF NOT EXISTS tp_mode VARCHAR(8)"
        ))
        # 盈亏峰值列（2026-09-26）：持仓期间全程盈亏峰值，浮盈回撤 ≥0.6R 触发 AI 诊断
        conn.execute(text(
            "ALTER TABLE trade_records "
            "ADD COLUMN IF NOT EXISTS peak_total_pnl NUMERIC(20, 6)"
        ))
        # 止损金额模式（2026-09-27）：pct=风险基数分档×3%（默认）/ fixed=固定 USDT 金额
        conn.execute(text(
            "ALTER TABLE system_config "
            "ADD COLUMN IF NOT EXISTS risk_budget_mode VARCHAR(8) NOT NULL DEFAULT 'pct'"
        ))
        conn.execute(text(
            "ALTER TABLE system_config "
            "ADD COLUMN IF NOT EXISTS risk_fixed_amount NUMERIC(12, 2) NOT NULL DEFAULT 3"
        ))
        # 交易记录模式快照列（2026-09-27）：开仓时止损金额模式（pct/fixed），
        # 旧行 NULL 按 pct 理解，无需回填
        conn.execute(text(
            "ALTER TABLE trade_records "
            "ADD COLUMN IF NOT EXISTS risk_budget_mode VARCHAR(8)"
        ))
        # key_levels_json 快照列（docs/21，2026-09-27 上线；2026-09-28 功能整体
        # 移除后停止写入）：存量列按仓库惯例保留不 DROP，历史行原样可查
        conn.execute(text(
            "ALTER TABLE ai_analyses "
            "ADD COLUMN IF NOT EXISTS key_levels_json JSON"
        ))
        # 持仓时长警告级别（2026-09-27）：0=未告警/1=已发 12h 黄卡/2=已发 24h 红卡，
        # 巡检每 30 分钟一轮的幂等去重标记
        conn.execute(text(
            "ALTER TABLE trade_records "
            "ADD COLUMN IF NOT EXISTS hold_warn_level INTEGER NOT NULL DEFAULT 0"
        ))
        # 异常单标记（2026-10-18）：测试网模拟撮合曾产出伪盈亏单（HYPEUSDT 一笔
        # -2330.51 伪巨亏），污染资金分析曲线/复盘统计，甚至误触发测试网当日亏损
        # 熔断/周月回撤闸门。人工标记（excluded=True）后全链路剔除该单——统计/复盘/
        # 熔断闸门/日报口径一致剔除，行本身保留在库中可审计，可在页面取消标记。
        conn.execute(text(
            "ALTER TABLE trade_records "
            "ADD COLUMN IF NOT EXISTS excluded BOOLEAN NOT NULL DEFAULT FALSE"
        ))
        conn.execute(text(
            "ALTER TABLE trade_records "
            "ADD COLUMN IF NOT EXISTS excluded_at TIMESTAMP"
        ))
        # 止损锚类型（docs/23，2026-09-29）：structure/near，仅突破类信号有值，
        # 旧行 NULL 按 structure 理解
        conn.execute(text(
            "ALTER TABLE ai_analyses "
            "ADD COLUMN IF NOT EXISTS stop_anchor VARCHAR(16)"
        ))
        # 规则版本标记（docs/24 A0，2026-09-29）：trade_records/ai_analyses 快照
        # 开仓/分析时点规则集（settings.RULE_VERSION，如 v2026-09-29），
        # 复盘/归因按版本分组；NULL=标记上线前旧行，统计口径归 legacy
        conn.execute(text(
            "ALTER TABLE trade_records "
            "ADD COLUMN IF NOT EXISTS rule_version VARCHAR(16)"
        ))
        conn.execute(text(
            "ALTER TABLE ai_analyses "
            "ADD COLUMN IF NOT EXISTS rule_version VARCHAR(16)"
        ))
        # 双评委 veto 闭环（docs/24 A5a，2026-09-29）：veto 原始建议快照 +
        # 复盘否决单回放标记，对比"被否决建议假想表现 vs 通过单表现"
        conn.execute(text(
            "ALTER TABLE ai_analyses "
            "ADD COLUMN IF NOT EXISTS veto_original JSONB"
        ))
        # veto 快照卫生（2026-10-20）：本列 none_as_null 生效前，Python None 被写成
        # JSON `null`（非 SQL NULL），`veto_original IS NOT NULL` 因此对普通 skip 行
        # 也为真 → 复盘任务误把无否决快照的 skip 单当 veto 单回放。此处把存量 JSON
        # `null` 归一回 SQL NULL（幂等：归一后无行匹配）；列非空且文本为 'null' 才动。
        conn.execute(text(
            "UPDATE ai_analyses SET veto_original = NULL "
            "WHERE veto_original IS NOT NULL AND veto_original::text = 'null'"
        ))
        conn.execute(text(
            "ALTER TABLE trade_reviews "
            "ADD COLUMN IF NOT EXISTS is_veto BOOLEAN NOT NULL DEFAULT FALSE"
        ))
        # 影子 A/B（2026-10-20）：复盘行携带 4h 背景状态 key（与 AIAnalysis.htf_phase
        # 同源）与影子桶标记（过渡态×回踩确认 veto 行='transition_pbc'），使
        # /review/stats 可按 (ab_bucket, direction) 出分层对比，无需回查 ai_analyses
        conn.execute(text(
            "ALTER TABLE trade_reviews "
            "ADD COLUMN IF NOT EXISTS htf_phase VARCHAR(20)"
        ))
        conn.execute(text(
            "ALTER TABLE trade_reviews "
            "ADD COLUMN IF NOT EXISTS ab_bucket VARCHAR(32)"
        ))
        conn.execute(text(
            "UPDATE trade_records SET account_key = 'testnet-1' "
            "WHERE account_key IS NULL AND testnet"
        ))
        conn.execute(text(
            "UPDATE trade_records SET account_key = 'live-1' "
            "WHERE account_key IS NULL AND NOT testnet"
        ))
        conn.execute(text(
            "UPDATE account_transfers SET account_key = 'testnet-1' "
            "WHERE account_key IS NULL AND testnet"
        ))
        conn.execute(text(
            "UPDATE account_transfers SET account_key = 'live-1' "
            "WHERE account_key IS NULL AND NOT testnet"
        ))
        # 币种黑名单（2026-09-18）：全新表由 create_all 建表（init_db 已导入
        # app.models.blacklist）。symbol 带 unique=True 自带唯一索引，不再建
        # ix_symbol_blacklist_symbol（模型同名额外索引曾致 beat DuplicateTable）
        conn.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_symbol_blacklist_origin "
            "ON symbol_blacklist (origin)"
        ))
        # 战法模块（2026-10-02）：全新表 tactics 由 create_all 建表（init_db 已
        # 导入 app.models.tactic，name 带 unique=True 自带唯一索引）；命中标注
        # 快照列随 AIAnalysis 落库，旧行 NULL=战法模块上线前/未命中
        conn.execute(text(
            "ALTER TABLE ai_analyses "
            "ADD COLUMN IF NOT EXISTS tactic_name VARCHAR(64)"
        ))
        conn.execute(text(
            "ALTER TABLE ai_analyses "
            "ADD COLUMN IF NOT EXISTS tactic_reason VARCHAR(255)"
        ))
        # 现货屯币（docs/18）：spot_settings 配置 JSON 列（新增独立列，不改既有列语义）。
        # 全新表（spot_orders/spot_weekly_reviews/spot_kline_cache）
        # 由 create_all 建表（init_db 已导入 app.models.spot）
        conn.execute(text(
            "ALTER TABLE system_config "
            "ADD COLUMN IF NOT EXISTS spot_settings JSONB"
        ))
        # 筛选参数组（2026-09-28 筛选配置上页面）：JSON 列，NULL=回落 env/代码默认；
        # 键集与解析见 services/scan_filters.py，旧 breakout_threshold 等散列废弃不删
        conn.execute(text(
            "ALTER TABLE system_config "
            "ADD COLUMN IF NOT EXISTS scan_filters JSONB"
        ))
        # 类型对齐（2026-10-22）：全新库由 create_all 按模型 JSON 建成 json，
        # 上面的 ADD COLUMN IF NOT EXISTS 对已存在列 no-op，不会改成 jsonb；
        # 下方 jsonb 运算符迁移（@> / <@ / jsonb_set）在 json 列上直接报
        # UndefinedFunction，init_db 失败 → backend/celery 全部重启。显式对齐。
        _scan_filters_type = conn.execute(text(
            "SELECT data_type FROM information_schema.columns "
            "WHERE table_name = 'system_config' AND column_name = 'scan_filters'"
        )).scalar()
        if _scan_filters_type == "json":
            conn.execute(text(
                "ALTER TABLE system_config "
                "ALTER COLUMN scan_filters TYPE JSONB USING scan_filters::jsonb"
            ))
        # 筛选漏斗（2026-10-11）：每轮扫描的关卡计数+被拒币种明细（symbol→关卡），
        # 前端「筛选漏斗」Tab 展示卡在哪一步；旧行/异常轮次 NULL
        conn.execute(text(
            "ALTER TABLE scan_records "
            "ADD COLUMN IF NOT EXISTS funnel_json JSONB"
        ))
        # ema144 触点复出一致化（2026-10-16）：退役期（2026-10-10~10-16）前端不给该
        # 选项、解析层强制过滤，故存量「未定制」的默认白名单必然是退役期默认
        # ["swing","ema21","ema21_4h","ema55"]（缺 ema144）。此处对「恰好等于退役期
        # 默认」的存量行一次性补入 ema144；补后数组变为 5 元 → 集合相等条件自动失效，
        # 迁移天然幂等、且不触碰操作者已定制的白名单（增删任一项即不匹配，自行在
        # 配置页勾选）。集合相等用 jsonb 双向包含（忽略元素顺序）。
        conn.execute(text(
            "UPDATE system_config SET scan_filters = jsonb_set("
            "  scan_filters, '{pullback_touch_refs}',"
            "  scan_filters->'pullback_touch_refs' || '\"ema144\"'::jsonb) "
            "WHERE scan_filters->'pullback_touch_refs' "
            "        @> '[\"swing\",\"ema21\",\"ema21_4h\",\"ema55\"]'::jsonb "
            "  AND scan_filters->'pullback_touch_refs' "
            "        <@ '[\"swing\",\"ema21\",\"ema21_4h\",\"ema55\"]'::jsonb"
        ))
        # retest 触点默认启用（2026-10-17 docs/23 方案 A1，用户拍板直接上线不做影子）：
        # 与上方 ema144 迁移同款「存量未定制一致化」——凡「恰好等于现行默认 5 类」
        # （swing/ema21/ema21_4h/ema55/ema144，即 ema144 迁移后的默认集）的存量行，
        # 一次性补入 retest（前插，与 DEFAULT_TOUCH_REFS 顺序一致）。集合相等用
        # jsonb 双向包含（忽略顺序），补后数组变 6 元 → 条件自动失效，天然幂等；
        # 不触碰操作者已定制的白名单（增删任一项即不匹配，自行在配置页勾选）。
        # 上方 ema144 迁移先执行，故本次匹配的是「已补过 ema144」的 5 元集。
        # 注：前插必须显式括号——PG 中 `->`/`||` 同为「其他运算符」级、左结合，
        # `'[\"retest\"]'::jsonb || col->'k'` 会解析为 `('[\"retest\"]' || col) -> 'k'`
        # → 取键失败得 NULL，jsonb_set(new_value=NULL) 会把整个 scan_filters 抹成 NULL
        # （2026-10-17 首版即踩此坑，已用本机 PG15 复现并验证修复）。
        conn.execute(text(
            "UPDATE system_config SET scan_filters = jsonb_set("
            "  scan_filters, '{pullback_touch_refs}',"
            "  ('[\"retest\"]'::jsonb) || (scan_filters->'pullback_touch_refs')) "
            "WHERE scan_filters->'pullback_touch_refs' "
            "        @> '[\"swing\",\"ema21\",\"ema21_4h\",\"ema55\",\"ema144\"]'::jsonb "
            "  AND scan_filters->'pullback_touch_refs' "
            "        <@ '[\"swing\",\"ema21\",\"ema21_4h\",\"ema55\",\"ema144\"]'::jsonb"
        ))
        # 黑名单永久档（2026-10-11）：origin 加 stop_hard 值（9 字符），列 8→16 扩宽
        conn.execute(text(
            "ALTER TABLE symbol_blacklist "
            "ALTER COLUMN origin TYPE VARCHAR(16)"
        ))
        # 多策略可插拔（docs/20 S0/S1）：
        # trade_records.strategy_id 策略归因列——带 DEFAULT，存量单自动归 golden_k_ai
        conn.execute(text(
            "ALTER TABLE trade_records "
            "ADD COLUMN IF NOT EXISTS strategy_id VARCHAR(16) "
            "NOT NULL DEFAULT 'golden_k_ai'"
        ))
        # 2026-10-10 融合拍板：影子层（strategy_shadow_signals/observations/
        # shadow_pendings 三表与 strategy_registry 注册表）整体退役——EMA 趋势
        # 逻辑并入原生触发层（新触发类型 trend_pullback），一套系统。
        # 历史表按项目惯例不 DROP：存量数据保留审计，代码不再读写。
        # 现货环境与合约完全独立（docs/18，2026-09-23）：默认现货=测试网；
        # 启用账号集合空回落 live-1。共用 docs/17 的 .env 账号注册表
        conn.execute(text(
            "ALTER TABLE system_config "
            "ADD COLUMN IF NOT EXISTS spot_trading_testnet BOOLEAN DEFAULT TRUE"
        ))
        conn.execute(text(
            "ALTER TABLE system_config "
            "ADD COLUMN IF NOT EXISTS spot_live_trading_keys TEXT"
        ))
        # spot_orders 归属账号列（P1 执行闭环，对齐 trade_records 惯例）：
        # NULL 旧行按 testnet 布尔回填（与 docs/17 迁移同款双保险）
        conn.execute(text(
            "ALTER TABLE spot_orders "
            "ADD COLUMN IF NOT EXISTS account_key VARCHAR(20)"
        ))
        conn.execute(text(
            "UPDATE spot_orders SET account_key = 'testnet-1' "
            "WHERE account_key IS NULL AND testnet"
        ))
        conn.execute(text(
            "UPDATE spot_orders SET account_key = 'live-1' "
            "WHERE account_key IS NULL AND NOT testnet"
        ))
        # 回踩触点体系（2026-09-28 docs/22）：触点维度三列 + 破轨事件表。
        # breakout_events 全新表由 create_all 建表，此处只为存量库兜底
        # （create_all 不改既有表）；列语义与消费方见 models/scan.py
        conn.execute(text(
            "ALTER TABLE scan_results "
            "ADD COLUMN IF NOT EXISTS touch_ref VARCHAR(16)"
        ))
        conn.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_scan_results_touch_ref "
            "ON scan_results (touch_ref)"
        ))
        conn.execute(text(
            "ALTER TABLE pullback_pendings "
            "ADD COLUMN IF NOT EXISTS touch_ref VARCHAR(16)"
        ))
        conn.execute(text(
            "ALTER TABLE trade_reviews "
            "ADD COLUMN IF NOT EXISTS touch_ref VARCHAR(16)"
        ))
        conn.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_trade_reviews_touch_ref "
            "ON trade_reviews (touch_ref)"
        ))
        # 下线列默认值补齐（2026-10-22 生产事故）：ai_pipeline_enabled /
        # dual_judge_enabled 已停用，种子 INSERT 不再写入（96a0beb）；但生产库
        # 建库时 create_all 按旧模型已建出这两列（NOT NULL 且无 server_default，
        # 当时的 ADD COLUMN IF NOT EXISTS 因列已存在而 no-op）。PG 的 NOT NULL
        # 校验先于 ON CONFLICT 判定——行已存在也会 NotNullViolation，init_db 整体
        # 失败 → backend(uvicorn exit 3)/worker/beat(celery exit 2) 全部重启。
        # 给存量列补默认值后裸 INSERT 才可正常走冲突跳过；列保留不 DROP（项目惯例）。
        _legacy_bool_cols = conn.execute(text(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'system_config' "
            "  AND is_nullable = 'NO' AND column_default IS NULL "
            "  AND column_name IN ('ai_pipeline_enabled', 'dual_judge_enabled')"
        )).scalars().all()
        for _col in _legacy_bool_cols:
            conn.execute(text(
                f"ALTER TABLE system_config ALTER COLUMN {_col} SET DEFAULT FALSE"
            ))
        # 确保系统配置表有默认行（初始值从 env 注入）。
        # 注意必须包含所有 NOT NULL 列：全新库由 create_all 建表，模型列只有
        # Python 端 default 没有 server_default，漏列的裸 INSERT 会 NotNullViolation
        # （存量库行已存在走 ON CONFLICT 跳过，因此本地从未暴露）。
        # 2026-10-08 补上历史漏掉的 4 个 NOT NULL 列（risk_budget_mode/
        # risk_fixed_amount/spot_trading_testnet/live_initial_capital——2026-09-27
        # 起新库 init_db 必炸的潜伏 bug）+ 新增的 tp2_enabled
        conn.execute(text(
            "INSERT INTO system_config "
            "(id, ai_analysis_enabled, kline_interval, kline_window, "
            " breakout_threshold, r_squared_threshold, repeat_window_hours, "
            " swing_order, pullback_tolerance, notes, "
            " strategy_prompt_enabled, strategy_prompt, fib_enabled, "
            " max_open_trades, "
            " strategy_trend_follow_enabled, strategy_structure_break_enabled, "
            " strategy_range_edge_enabled, trading_min_free_pct, "
            " trading_enabled, "
            " trading_enabled_testnet, trading_enabled_live, "
            " risk_budget_mode, risk_fixed_amount, spot_trading_testnet, "
            " live_initial_capital, tp2_enabled, tp_full_exit_enabled, "
            " feishu_testnet_notify_enabled) "
            "VALUES (1, false, :kline_interval, :kline_window, "
            " :breakout_threshold, :r_squared_threshold, :repeat_window_hours, "
            " :swing_order, :pullback_tolerance, '系统运行时配置', "
            " false, '', false, "
            " 5, "
            " true, false, "
            " true, 0.5, "
            " :trading_enabled, "
            " :trading_enabled_testnet, :trading_enabled_live, "
            " 'pct', 3, true, "
            " 0, false, false, true) "
            "ON CONFLICT (id) DO NOTHING"
        ),
            {
                "kline_interval": settings.KLINE_INTERVAL,
                "kline_window": settings.KLINE_WINDOW,
                "breakout_threshold": settings.BREAKOUT_THRESHOLD,
                "r_squared_threshold": settings.R_SQUARED_THRESHOLD,
                "repeat_window_hours": settings.REPEAT_WINDOW_HOURS,
                "swing_order": settings.SWING_ORDER,
                "pullback_tolerance": settings.PULLBACK_TOLERANCE,
                "trading_enabled": settings.TRADING_ENABLED,
                "trading_enabled_testnet": settings.TRADING_ENABLED_TESTNET,
                "trading_enabled_live": settings.TRADING_ENABLED_LIVE,
            },
        )
        # trade_records 加 β 快照列（2026-10-04，docs/13 #1 组合热度 β 加权；
        # NULL=旧行/β 不可得，闸门按 1.0 保守计）
        conn.execute(text(
            "ALTER TABLE trade_records ADD COLUMN IF NOT EXISTS beta NUMERIC(10, 4)"
        ))
        # 移动止损方式（2026-10-20 引入；2026-10-21 起新开单默认改回 three_line，
        # hl 保留可切换）：hl=HL/LH 结构跟踪；three_line=三线止损。
        # NULL=旧行，运行期按 three_line 理解
        conn.execute(text(
            "ALTER TABLE trade_records ADD COLUMN IF NOT EXISTS trailing_mode VARCHAR(16)"
        ))
        # 周/月回撤分层熔断 + ATR 跟进倍数（2026-10-04，docs/13 #2/#5）：
        # 阈值列 NULL=回落 env（页面留空恢复默认）、0=关闭该层；
        # ack 时刻 = 月熔断人工复盘确认，确认在当前 UTC 月内即放行至月底
        conn.execute(text("""
            ALTER TABLE system_config
              ADD COLUMN IF NOT EXISTS weekly_dd_stop_pct NUMERIC(10, 6),
              ADD COLUMN IF NOT EXISTS monthly_dd_stop_pct NUMERIC(10, 6),
              ADD COLUMN IF NOT EXISTS monthly_breaker_ack_at TIMESTAMP,
              ADD COLUMN IF NOT EXISTS weekly_breaker_ack_at TIMESTAMP,
              ADD COLUMN IF NOT EXISTS atr_trail_k NUMERIC(10, 6)
        """))
        # 影子模式打标表（2026-10-04，docs/14）：候选信号特征「只计算+打标+记录，
        # 不参与决策」，事后按 标记×结果 分组统计评估升格/关闭
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS shadow_marks (
                id UUID PRIMARY KEY,
                created_at TIMESTAMP NOT NULL DEFAULT now(),
                feature_key VARCHAR(32) NOT NULL,
                ref_type VARCHAR(16) NOT NULL,
                ref_id UUID NOT NULL,
                symbol VARCHAR(32) NOT NULL,
                testnet BOOLEAN NOT NULL DEFAULT TRUE,
                value_json JSON,
                decision_json JSON,
                rule_version VARCHAR(32)
            )
        """))
        conn.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_shadow_marks_feature_time "
            "ON shadow_marks (feature_key, created_at)"
        ))
        conn.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_shadow_marks_ref "
            "ON shadow_marks (ref_type, ref_id)"
        ))
        conn.execute(text(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_shadow_mark "
            "ON shadow_marks (feature_key, ref_type, ref_id)"
        ))
        # 影子标记规则版本列（2026-10-13，docs/14 §六.1）：规则集变更改变候选池/
        # 出场链，评估按卡声明的 since_rule_version 过滤以防「跨版本不混算」；
        # 仅新行有值（ORM 默认取 settings.RULE_VERSION），历史行 NULL 自动出窗
        conn.execute(text(
            "ALTER TABLE shadow_marks "
            "ADD COLUMN IF NOT EXISTS rule_version VARCHAR(32)"
        ))
        # ai_analyses 加 4h 状态列（2026-10-09 r2 建，值域三次换血：五次拍板 →
        # 过热/冷却 → 2026-10-11 再重构 EMA 状态机 11 态 tangle/turn_*/
        # macro_cross_*/*_recover/aligned_*/bull_dip/bear_rally）：
        # enforce 阶段 stash 落库；旧行（退役旧枚举）/无 4h 数据 NULL=未知，
        # 复盘维度分组用。列宽 12→20（macro_cross_bull 16 字符超旧宽，
        # 2026-10-11 用户拍板扩列存全名；ALTER TYPE 幂等，已是 20 时无害）
        conn.execute(text(
            "ALTER TABLE ai_analyses ADD COLUMN IF NOT EXISTS htf_phase VARCHAR(20)"
        ))
        conn.execute(text(
            "ALTER TABLE ai_analyses ALTER COLUMN htf_phase TYPE VARCHAR(20)"
        ))
        # 双入场策略列（历史列：2026-10-12 策略二删除后停写，新行 NULL，仅历史行透出）
        conn.execute(text(
            "ALTER TABLE ai_analyses "
            "ADD COLUMN IF NOT EXISTS entry_strategy VARCHAR(8)"
        ))
        # 策略二结论审计列（历史列：2026-10-12 策略二删除后停写，新行 NULL）
        conn.execute(text(
            "ALTER TABLE ai_analyses "
            "ADD COLUMN IF NOT EXISTS strategy2_decision VARCHAR(8)"
        ))
        conn.execute(text(
            "ALTER TABLE ai_analyses "
            "ADD COLUMN IF NOT EXISTS strategy2_reason VARCHAR(255)"
        ))
        # 程序止损参考价（2026-10-11 上线，2026-10-13 功能整体删除）：仅前端对比
        # 展示的历史列，随功能一并物理删除（存量库摘除；本仓库「列保留不删」惯例的
        # 显式例外，用户拍板）
        conn.execute(text(
            "ALTER TABLE ai_analyses "
            "DROP COLUMN IF EXISTS calc_stop_loss"
        ))
        # 退役表清理（2026-10-12）：P-12 参数稳健性扫描功能已删除，
        # 模型 RobustnessScan 下线后 create_all 不再建表；存量库用 DROP 摘除孤儿表
        # robustness_scans（幂等，表不存在时无害）。本仓库惯例是「列保留不删」，
        # 此处为显式拍板的整表清理
        conn.execute(text("DROP TABLE IF EXISTS robustness_scans"))
        # 回测与虚拟账本整体退役（2026-10-12）：合约回测（backtest_engine /
        # backtest_tasks / api/backtest）、现货回测（spot_backtest /
        # run_backtest_task / api/spot 回测端点）与虚拟账本（ledger 全链路，
        # 含 N-16 度量）功能与模型一并删除，create_all 不再建表；存量库 DROP
        # 孤儿表（幂等，表不存在时无害）。spot_kline_cache 被现货屯币回填复用，
        # 严禁 DROP
        conn.execute(text("DROP TABLE IF EXISTS virtual_ledger"))
        conn.execute(text("DROP TABLE IF EXISTS futures_backtests"))
        conn.execute(text("DROP TABLE IF EXISTS backtest_kline_cache"))
        conn.execute(text("DROP TABLE IF EXISTS spot_backtests"))
        # 双周期扫描套（1h·4h / 4h·1d）整体回退后的残留列清理（2026-10-14，见 revert
        # 提交）：该特性曾给存量库补过 4 列 + 1 索引（scan_records/trade_records 的
        # profile_key、system_config 的两个启用开关）。回退后模型不再映射、代码不再
        # 引用这些列，留着虽无害（均为可空或带 DEFAULT），但按用户拍板做显式清理。
        # 本仓库默认「列保留不删」，此处为显式例外（同上方 calc_stop_loss 先例）。
        # 幂等：全新库由 create_all 建表本就不含这些列，IF EXISTS 下四句全部无害。
        conn.execute(text("DROP INDEX IF EXISTS ix_scan_records_profile_key"))
        conn.execute(text("ALTER TABLE scan_records DROP COLUMN IF EXISTS profile_key"))
        conn.execute(text("ALTER TABLE trade_records DROP COLUMN IF EXISTS profile_key"))
        conn.execute(text(
            "ALTER TABLE system_config DROP COLUMN IF EXISTS scan_profile_s1_enabled"))
        conn.execute(text(
            "ALTER TABLE system_config DROP COLUMN IF EXISTS scan_profile_s2_enabled"))
        # 链路观测三段时间戳（2026-10-21 批次 B1，docs/需求文档/06 §5.1）：
        # signal_created_at=候选产生时刻（批量落库前）、ai_finished_at=AI 结论
        # 落库时刻（latest-wins 原地更新也刷新）、opened_at 已在 trade_records；
        # 三者相减得 signal_age_s / ai→open，复盘按 P50/P90 输出执行时延
        conn.execute(text(
            "ALTER TABLE scan_results "
            "ADD COLUMN IF NOT EXISTS signal_created_at TIMESTAMP"
        ))
        conn.execute(text(
            "ALTER TABLE ai_analyses "
            "ADD COLUMN IF NOT EXISTS ai_finished_at TIMESTAMP"
        ))
        # 4h 数据降级标记（2026-10-21 批次 B2/B3，docs/需求文档/06 §5.3）：
        # AI 层 4h K线缺失/拉取失败时结论仍会产出（fail-open 点），打标供复盘
        # 分组对比；默认 FALSE 使存量行按「未降级」理解
        conn.execute(text(
            "ALTER TABLE ai_analyses "
            "ADD COLUMN IF NOT EXISTS htf_degraded BOOLEAN NOT NULL DEFAULT FALSE"
        ))
        conn.execute(text(
            "ALTER TABLE trade_reviews "
            "ADD COLUMN IF NOT EXISTS htf_degraded BOOLEAN NOT NULL DEFAULT FALSE"
        ))
        conn.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_trade_reviews_htf_degraded "
            "ON trade_reviews (htf_degraded)"
        ))
        # 回放入场诊断快照（2026-10-28，17-复盘审计 P0-1）：pos/room_r/stop_atr/
        # planned_rr/quality_score 五键 JSON；历史行 NULL，不回填（回放已完成，
        # 补算需重新拉 K 线，按「新账记清」只影响新回放行）
        conn.execute(text(
            "ALTER TABLE trade_reviews "
            "ADD COLUMN IF NOT EXISTS entry_diag JSONB"
        ))
        # 持仓期最大有利/不利偏移（2026-10-28，17-复盘审计 P1-3）：signed R 倍数
        # （有利为正/不利为负）；历史行 NULL，不回填（补算需重新拉 K 线）
        conn.execute(text(
            "ALTER TABLE trade_reviews "
            "ADD COLUMN IF NOT EXISTS mfe_r DOUBLE PRECISION, "
            "ADD COLUMN IF NOT EXISTS mae_r DOUBLE PRECISION"
        ))
        # BTC 环境事实缺失标记（2026-10-21，T0-5）：市场环境块缺失时提示词显式
        # 标注「未知」；打标供复盘剔除/分组，默认 FALSE 使存量行按环境可见理解
        conn.execute(text(
            "ALTER TABLE ai_analyses "
            "ADD COLUMN IF NOT EXISTS btc_env_missing BOOLEAN NOT NULL DEFAULT FALSE"
        ))
        # 排序可观测（2026-10-21，T1-2）：质量分分项 + AI 配额分池/名次。
        # 旧行 NULL（只有总分，无法回溯分项）
        conn.execute(text(
            "ALTER TABLE scan_results "
            "ADD COLUMN IF NOT EXISTS quality_parts JSONB"
        ))
        conn.execute(text(
            "ALTER TABLE scan_results "
            "ADD COLUMN IF NOT EXISTS quota_pool VARCHAR(16)"
        ))
        conn.execute(text(
            "ALTER TABLE scan_results "
            "ADD COLUMN IF NOT EXISTS quota_rank INTEGER"
        ))
        conn.execute(text(
            "ALTER TABLE ai_analyses "
            "ADD COLUMN IF NOT EXISTS cache_origin_at TIMESTAMP"
        ))
        # 4h 状态快照时刻 + 评分原始值（2026-10-21 第二阶段 T1/T2）：
        # 前者供开仓前时效复核（>4h15m 禁开），后者保留模型连续分供档位校准
        conn.execute(text(
            "ALTER TABLE ai_analyses "
            "ADD COLUMN IF NOT EXISTS htf_as_of TIMESTAMP"
        ))
        conn.execute(text(
            "ALTER TABLE ai_analyses "
            "ADD COLUMN IF NOT EXISTS recommendation_raw NUMERIC(5, 2)"
        ))
        # 风险字段口径拆分（2026-10-21 第二阶段 T3）：planned/initial/remaining 三列。
        # 旧行按 risk_amount 回填（无更早口径可还原）；终态单剩余风险=0，在跑单
        # 暂按原风险，下一轮结算巡检按实际剩余量与当前止损重算。risk_amount 保留
        # 读取兼容期，新行继续写 initial_risk 等值防旧消费方读到空值。
        conn.execute(text(
            "ALTER TABLE trade_records "
            "ADD COLUMN IF NOT EXISTS planned_risk NUMERIC(20, 4)"
        ))
        conn.execute(text(
            "ALTER TABLE trade_records "
            "ADD COLUMN IF NOT EXISTS initial_risk NUMERIC(20, 4)"
        ))
        conn.execute(text(
            "ALTER TABLE trade_records "
            "ADD COLUMN IF NOT EXISTS remaining_risk NUMERIC(20, 4)"
        ))
        conn.execute(text(
            "UPDATE trade_records SET planned_risk = risk_amount "
            "WHERE planned_risk IS NULL AND risk_amount IS NOT NULL"
        ))
        conn.execute(text(
            "UPDATE trade_records SET initial_risk = risk_amount "
            "WHERE initial_risk IS NULL AND risk_amount IS NOT NULL"
        ))
        conn.execute(text(
            "UPDATE trade_records SET remaining_risk = CASE "
            "WHEN status IN ('OPENED', 'TP1_HIT', 'TP2_HIT') THEN risk_amount ELSE 0 "
            "END WHERE remaining_risk IS NULL"
        ))
        # 统一决策版本（2026-10-21 第二阶段 T5，审计 7.1）：配置/补丁/提示词变更
        # 在同一事务内递增；AI 分析与交易快照携带版本，复盘与缓存按版本分组。
        # 存量行 decision_version=0（上线前原始版本）、ai_analyses 旧行 NULL
        conn.execute(text(
            "ALTER TABLE system_config "
            "ADD COLUMN IF NOT EXISTS decision_version INTEGER NOT NULL DEFAULT 0"
        ))
        conn.execute(text(
            "ALTER TABLE system_config "
            "ADD COLUMN IF NOT EXISTS decision_version_at TIMESTAMP"
        ))
        conn.execute(text(
            "ALTER TABLE ai_analyses "
            "ADD COLUMN IF NOT EXISTS decision_version INTEGER"
        ))
        # 补丁合入前来源值（T5）：回滚恢复到合入前自定义值，而非一律回落默认
        conn.execute(text(
            "ALTER TABLE prompt_patches "
            "ADD COLUMN IF NOT EXISTS before_value_json JSONB"
        ))
        # AI 分析暂停时段（2026-10-26）：每天固定整点区间内跳过筛选后的自动 AI
        # 分析（北京时间）。三列均可空：NULL=未配置（不暂停），老库加列无需回填；
        # 语义与消费方见 services/ai_pause.py
        conn.execute(text("""
            ALTER TABLE system_config
              ADD COLUMN IF NOT EXISTS ai_pause_enabled BOOLEAN,
              ADD COLUMN IF NOT EXISTS ai_pause_start_hour INTEGER,
              ADD COLUMN IF NOT EXISTS ai_pause_end_hour INTEGER
        """))
        # 配置最后修改时间（docs/23 T14a，2026-10-29）：旧库补列，NULL=从未记录
        conn.execute(text(
            "ALTER TABLE system_config ADD COLUMN IF NOT EXISTS updated_at TIMESTAMP"))
        # 交易事件类型放宽 16→32（docs/23 T14e，2026-10-29）：FULL_EXIT_RESIDUAL(18)
        # 与 FULL_EXIT_RESIDUAL_FAILED(25) 超长会让 PG 抛 StringDataRightTruncation，
        # 整轮巡检回滚 → 补平已成交但幂等标记丢失，下轮重复补平。重复执行无害
        conn.execute(text(
            "ALTER TABLE trade_events ALTER COLUMN event_type TYPE VARCHAR(32)"))
        # scan_results 组合确认三列（2026-10-26，docs/11）：旧行 golden_combo=FALSE、
        # pattern_candle_open/entry_lag_pct NULL，前端据此回退到「单形态、锚信号根」
        conn.execute(text(
            "ALTER TABLE scan_results "
            "ADD COLUMN IF NOT EXISTS golden_combo BOOLEAN NOT NULL DEFAULT FALSE"))
        conn.execute(text(
            "ALTER TABLE scan_results "
            "ADD COLUMN IF NOT EXISTS pattern_candle_open TIMESTAMP"))
        conn.execute(text(
            "ALTER TABLE scan_results "
            "ADD COLUMN IF NOT EXISTS entry_lag_pct NUMERIC(10,4)"))


def init_db():
    """创建所有表 + 幂等迁移"""
    from app.models import (  # noqa: F401
        ops, patch, scan, trade, blacklist, spot, tactic, shadow, observation,
    )
    Base.metadata.create_all(bind=engine)
    _run_migrations(engine)
