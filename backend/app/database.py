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
        # Feature 7: Agent 管线开关（docs/04 P1）
        conn.execute(text("""
            ALTER TABLE system_config
              ADD COLUMN IF NOT EXISTS ai_pipeline_enabled BOOLEAN NOT NULL DEFAULT FALSE
        """))
        # Feature 8: P2 闭环——AI trace + 双评委开关（docs/04 P2）
        conn.execute(text("""
            ALTER TABLE ai_analyses
              ADD COLUMN IF NOT EXISTS stage_trace JSON
        """))
        conn.execute(text("""
            ALTER TABLE system_config
              ADD COLUMN IF NOT EXISTS dual_judge_enabled BOOLEAN NOT NULL DEFAULT FALSE
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
        # 交易环境/总开关运行时化（2026-09-20）：由 env 静态配置迁移进 DB，页面可改
        # （system_enabled 列曾随"系统总开关"短暂数上线，功能并入 trading_enabled 后移除）
        conn.execute(text(
            f"ALTER TABLE system_config "
            f"ADD COLUMN IF NOT EXISTS trading_testnet BOOLEAN NOT NULL DEFAULT {settings.TRADING_TESTNET}, "
            f"ADD COLUMN IF NOT EXISTS trading_enabled BOOLEAN NOT NULL DEFAULT {settings.TRADING_ENABLED}"
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
        # 现货屯币（docs/18）：spot_settings 配置 JSON 列（新增独立列，不改既有列语义）。
        # 全新表（spot_orders/spot_weekly_reviews/spot_backtests/spot_kline_cache）
        # 由 create_all 建表（init_db 已导入 app.models.spot）
        conn.execute(text(
            "ALTER TABLE system_config "
            "ADD COLUMN IF NOT EXISTS spot_settings JSONB"
        ))
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
        # 确保系统配置表有默认行（初始值从 env 注入）。
        # 注意必须包含所有 NOT NULL 列：全新库由 create_all 建表，模型列只有
        # Python 端 default 没有 server_default，漏列的裸 INSERT 会 NotNullViolation
        # （存量库行已存在走 ON CONFLICT 跳过，因此本地从未暴露）。
        conn.execute(text(
            "INSERT INTO system_config "
            "(id, ai_analysis_enabled, kline_interval, kline_window, "
            " breakout_threshold, r_squared_threshold, repeat_window_hours, "
            " swing_order, pullback_tolerance, notes, "
            " ai_pipeline_enabled, dual_judge_enabled, "
            " strategy_prompt_enabled, strategy_prompt, fib_enabled, "
            " max_open_trades, "
            " strategy_trend_follow_enabled, strategy_structure_break_enabled, "
            " strategy_range_edge_enabled, trading_min_free_pct, "
            " trading_testnet, trading_enabled) "
            "VALUES (1, false, :kline_interval, :kline_window, "
            " :breakout_threshold, :r_squared_threshold, :repeat_window_hours, "
            " :swing_order, :pullback_tolerance, '系统运行时配置', "
            " false, true, "
            " false, '', false, "
            " 5, "
            " true, false, "
            " true, 0.5, "
            " :trading_testnet, :trading_enabled) "
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
                "trading_testnet": settings.TRADING_TESTNET,
                "trading_enabled": settings.TRADING_ENABLED,
            },
        )


def init_db():
    """创建所有表 + 幂等迁移"""
    from app.models import ops, patch, scan, trade, blacklist, spot  # noqa: F401
    Base.metadata.create_all(bind=engine)
    _run_migrations(engine)
