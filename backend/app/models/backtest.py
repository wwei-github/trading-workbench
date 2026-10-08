"""合约回测模型（docs/16 P-06，全部全新表——零侵入约束：不动既有表语义）

- FuturesBacktest：合约回测任务与结果（镜像 SpotBacktest：params/status/summary/
  daily_series/orders_detail/error，任务异步执行、前端轮询）
- BacktestKlineCache：回测历史 K 线缓存（symbol+interval 唯一；生产 KlineCache
  只存「最新」窗口，历史区间无复用价值——回测/重放用本表避免重复拉取限速）

注意：JSON 列一律用可移植 JSON（严禁 PG 专用 JSONB——sqlite 内存库测试套件
create_all 直接编译失败，2026-10-07 system_config/spot 前车之鉴）。
"""
import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, Index, JSON, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class FuturesBacktest(Base):
    __tablename__ = "futures_backtests"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    # 参数快照（可复现）：{source, start/end | top_n/days, symbols, rule_version,
    # signal_types, rr_tp1, use_tp2, variants:[{name, overrides}], portfolio:{...},
    # engine_version}——引擎版本随 params 落库，结果可追溯
    params: Mapped[dict] = mapped_column(JSON, nullable=False)

    # RUNNING / DONE / FAILED
    status: Mapped[str] = mapped_column(String(8), nullable=False, default="RUNNING", index=True)
    # 汇总：{engine_version, data_range, signals:{n,veto_n}, variants:[...],
    #         deltas:[...], portfolio:[...]}
    summary: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    # 逐日权益序列（曲线用）：{items:[{date, equity_r, equity_usdt, closed, open}]}
    daily_series: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    # 模拟订单明细（32KB 截断惯例）：{items:[...], truncated:bool}
    orders_detail: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)

    error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)


class BacktestKlineCache(Base):
    __tablename__ = "backtest_kline_cache"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    interval: Mapped[str] = mapped_column(String(8), nullable=False)
    # 币安原始 K 线数组精简版 [[ts,o,h,l,c,v]]（ts=ms open_time，时间升序）
    klines: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )

    __table_args__ = (
        Index("ix_backtest_kline_cache_symbol_interval", "symbol", "interval", unique=True),
    )


class RobustnessScan(Base):
    """参数稳健性扫描（docs/16 P-12）：网格变体 × 同一信号集重放 → PBO/DSR 折扣判据

    params: {source, symbols, days, grid:{override_key:[...]}, n_blocks}
    summary: {n_trials, n_signals, matrix_rows, excluded_n, pbo:{...},
              variants:[{name, overrides, decided, win_rate, exp_r, sum_r,
                         sr, skew, kurt, dsr}], best_is, best_oos,
              conclusion:{verdict, text}, engine_version, data_range}
    """
    __tablename__ = "robustness_scans"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    params: Mapped[dict] = mapped_column(JSON, nullable=False)
    status: Mapped[str] = mapped_column(String(8), nullable=False, default="RUNNING", index=True)
    summary: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
