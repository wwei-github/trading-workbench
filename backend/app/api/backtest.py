"""合约回测 API（docs/16 P-06，零侵入：独立 router，不触碰合约交易路径）

- POST /api/backtests          提交回测（异步 celery，返回 id 供轮询）
- GET  /api/backtests/{id}     结果（RUNNING 轮询；DONE 返回 summary 全量）
- GET  /api/backtests          历史列表（对比用，不含大 JSON）

参数约束（服务端硬校验，防误配拖垮任务）：
- source: history（历史建议单+真实单）/ scan（扫描候选全链路重放）
- symbols: scan 必填 1~BACKTEST_MAX_SYMBOLS 个；history 可空=全部币
- days: 1~BACKTEST_MAX_DAYS
- variants: 首个强制 baseline（空 overrides），最多 6 个变体；
  overrides 键白名单（引擎 OVERRIDE_KEYS），未知键 400
"""
import uuid as _uuid
from datetime import datetime, timedelta
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, field_validator, model_validator
from sqlalchemy.orm import Session

from app.config import settings
from app.database import get_db

router = APIRouter(prefix="/api/backtests", tags=["backtest"])

# 变体 overrides 白名单（与 backtest_engine.OVERRIDE_KEYS 同源独立定义，
# 避免 API 层 import 引擎（numpy 链路）拖慢启动）
OVERRIDE_KEYS = (
    "be_trigger_r", "be_fee_buffer", "tp0_r", "tp0_qty_pct", "tp0_gate_r",
    "trail_atr_k", "trail_swing_bars", "atr_period", "fee_rate", "fwd_bars",
)
MAX_VARIANTS = 6  # 除 baseline 外最多 6 个变体（单任务成本上限）


class VariantIn(BaseModel):
    name: str
    overrides: dict = {}

    @field_validator("name")
    @classmethod
    def _name(cls, v: str) -> str:
        v = (v or "").strip()
        if not v or len(v) > 32:
            raise ValueError("变体名须 1~32 字符")
        return v

    @field_validator("overrides")
    @classmethod
    def _keys(cls, v: dict) -> dict:
        bad = [k for k in v if k not in OVERRIDE_KEYS]
        if bad:
            raise ValueError(f"非法 overrides 键: {bad}（合法: {OVERRIDE_KEYS}）")
        for k, val in v.items():
            if val is not None and not isinstance(val, (int, float, bool)):
                raise ValueError(f"overrides.{k} 须为数值/布尔/null")
        return v


class BacktestIn(BaseModel):
    source: str = "scan"                # history / scan
    symbols: List[str] = []
    days: int = 30
    variants: List[VariantIn] = []
    risk_usdt: float = 100.0
    max_open_trades: int = 5
    same_direction_cap: int = 0

    @field_validator("source")
    @classmethod
    def _src(cls, v: str) -> str:
        if v not in ("history", "scan"):
            raise ValueError("source 须为 history 或 scan")
        return v

    @field_validator("days")
    @classmethod
    def _days(cls, v: int) -> int:
        if not 1 <= v <= settings.BACKTEST_MAX_DAYS:
            raise ValueError(f"days 须在 1~{settings.BACKTEST_MAX_DAYS}")
        return v

    @field_validator("symbols")
    @classmethod
    def _syms(cls, v: List[str]) -> List[str]:
        out = []
        for s in v:
            s = (s or "").strip().upper()
            if s and s not in out:
                out.append(s)
        if len(out) > settings.BACKTEST_MAX_SYMBOLS:
            raise ValueError(f"币种数上限 {settings.BACKTEST_MAX_SYMBOLS}")
        return out


def _validate_body(body: BacktestIn) -> dict:
    """业务校验（Pydantic 字段级之外的组合约束），返回 params 快照。"""
    if body.source == "scan" and not body.symbols:
        raise HTTPException(status_code=400, detail="scan 源必须指定 symbols")
    if body.variants and len(body.variants) > MAX_VARIANTS + 1:
        raise HTTPException(status_code=400, detail=f"变体数上限 {MAX_VARIANTS}（含基线 {MAX_VARIANTS + 1}）")
    if body.risk_usdt <= 0 or body.risk_usdt > 10_000:
        raise HTTPException(status_code=400, detail="risk_usdt 须在 (0, 10000]")
    if not 1 <= body.max_open_trades <= 50:
        raise HTTPException(status_code=400, detail="max_open_trades 须在 1~50")
    if body.same_direction_cap < 0 or body.same_direction_cap > body.max_open_trades:
        raise HTTPException(status_code=400, detail="same_direction_cap 须在 0~max_open_trades")

    # variants 首个强制 baseline（防用户伪基线带 overrides）
    variants = [{"name": "baseline", "overrides": {}}]
    for v in body.variants:
        if v.name == "baseline":
            continue  # 重复 baseline 名忽略（前端误传防御）
        variants.append({"name": v.name, "overrides": v.overrides})
    if len(variants) == 1 and body.variants:
        raise HTTPException(status_code=400, detail="至少提供一个非 baseline 变体")

    return {
        "source": body.source,
        "symbols": body.symbols,
        "days": body.days,
        "variants": variants,
        "portfolio": {
            "risk_usdt": body.risk_usdt,
            "max_open_trades": body.max_open_trades,
            "same_direction_cap": body.same_direction_cap,
        },
    }


@router.post("")
def create_backtest(body: BacktestIn, db: Session = Depends(get_db)):
    """提交回测任务（异步）。返回 {id, status} 供前端轮询。"""
    from app.models.backtest import FuturesBacktest
    from app.tasks.backtest_tasks import run_futures_backtest_task

    params = _validate_body(body)
    row = FuturesBacktest(params=params)
    db.add(row)
    db.commit()
    db.refresh(row)
    run_futures_backtest_task.delay(str(row.id))
    return {"id": str(row.id), "status": "RUNNING"}


# ── P-12 参数稳健性扫描（注册在 /{bt_id} 之前，防路径吞并）──────────────


class RobustnessIn(BaseModel):
    source: str = "scan"                # history / scan
    symbols: List[str] = []
    days: int = 30                      # start/end 缺省时的回看天数
    start: Optional[str] = None         # YYYY-MM-DD（UTC）；与 end 成对，优先于 days
    end: Optional[str] = None           # YYYY-MM-DD（UTC，含当日）
    n_blocks: int = 8                   # CSCV 块数（4~16 偶数）
    grid: Optional[dict] = None         # None=默认出厂网格（12 变体）

    @field_validator("source")
    @classmethod
    def _src(cls, v: str) -> str:
        if v not in ("history", "scan"):
            raise ValueError("source 须为 history 或 scan")
        return v

    @field_validator("days")
    @classmethod
    def _days(cls, v: int) -> int:
        if not 1 <= v <= settings.BACKTEST_MAX_DAYS:
            raise ValueError(f"days 须在 1~{settings.BACKTEST_MAX_DAYS}")
        return v

    @field_validator("symbols")
    @classmethod
    def _syms(cls, v: List[str]) -> List[str]:
        out = []
        for s in v:
            s = (s or "").strip().upper()
            if s and s not in out:
                out.append(s)
        if len(out) > settings.BACKTEST_MAX_SYMBOLS:
            raise ValueError(f"币种数上限 {settings.BACKTEST_MAX_SYMBOLS}")
        return out

    @field_validator("n_blocks")
    @classmethod
    def _blocks(cls, v: int) -> int:
        if v % 2 != 0 or not 4 <= v <= 16:
            raise ValueError("n_blocks 须为 4~16 的偶数")
        return v

    @field_validator("start", "end")
    @classmethod
    def _date_fmt(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return v
        try:
            datetime.strptime(v, "%Y-%m-%d")
        except ValueError:
            raise ValueError("start/end 须为 YYYY-MM-DD")
        return v

    @model_validator(mode="after")
    def _window_pair(self) -> "RobustnessIn":
        if (self.start is None) != (self.end is None):
            raise ValueError("start/end 须成对提供（或都缺省走 days）")
        if self.start and self.end:
            s = datetime.strptime(self.start, "%Y-%m-%d")
            e = datetime.strptime(self.end, "%Y-%m-%d")
            if e <= s:
                raise ValueError("end 须晚于 start")
            if (e - s).days > settings.BACKTEST_MAX_DAYS:
                raise ValueError(f"窗口跨度上限 {settings.BACKTEST_MAX_DAYS} 天")
            # 允许到 UTC 明天（东八区当天早上尚未到 UTC 当日）
            if e.date() > datetime.utcnow().date() + timedelta(days=1):
                raise ValueError("end 不能是未来日期")
        return self


@router.post("/robustness")
def create_robustness_scan(body: RobustnessIn, db: Session = Depends(get_db)):
    """提交稳健性扫描（异步）。409=2h 内已有在跑的扫描（防连点）。"""
    from datetime import datetime, timedelta

    from app.models.backtest import RobustnessScan
    from app.services import robustness as rb
    from app.tasks.backtest_tasks import run_robustness_scan_task

    if body.source == "scan" and not body.symbols:
        raise HTTPException(status_code=400, detail="scan 源必须指定 symbols")
    try:
        rb.expand_grid(body.grid)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    running = (
        db.query(RobustnessScan)
        .filter(RobustnessScan.status == "RUNNING",
                RobustnessScan.created_at >= datetime.utcnow() - timedelta(hours=2))
        .first()
    )
    if running is not None:
        raise HTTPException(status_code=409, detail=f"已有稳健性扫描在跑（{running.id}），请稍候")
    row = RobustnessScan(params={
        "source": body.source, "symbols": body.symbols, "days": body.days,
        "start": body.start, "end": body.end,
        "n_blocks": body.n_blocks, "grid": body.grid,
    })
    db.add(row)
    db.commit()
    db.refresh(row)
    run_robustness_scan_task.delay(str(row.id))
    return {"id": str(row.id), "status": "RUNNING"}


@router.get("/robustness")
def list_robustness_scans(limit: int = 20, db: Session = Depends(get_db)):
    """稳健性扫描历史列表（不含大 JSON）。"""
    from app.models.backtest import RobustnessScan

    rows = (
        db.query(RobustnessScan)
        .order_by(RobustnessScan.created_at.desc())
        .limit(min(limit, 100))
        .all()
    )
    return [
        {
            "id": str(r.id),
            "params": r.params,
            "status": r.status,
            "verdict": ((r.summary or {}).get("conclusion") or {}).get("verdict") if r.summary else None,
            "pbo": ((r.summary or {}).get("pbo") or {}).get("pbo") if r.summary else None,
            "n_signals": (r.summary or {}).get("n_signals") if r.summary else None,
            "error": r.error,
            "created_at": r.created_at.isoformat() if r.created_at else None,
            "finished_at": r.finished_at.isoformat() if r.finished_at else None,
        }
        for r in rows
    ]


@router.get("/robustness/{scan_id}")
def get_robustness_scan(scan_id: str, db: Session = Depends(get_db)):
    """稳健性扫描结果：RUNNING 时前端轮询；DONE 返回 summary 全量。"""
    from app.models.backtest import RobustnessScan

    try:
        row = db.get(RobustnessScan, _uuid.UUID(scan_id))
    except ValueError:
        raise HTTPException(status_code=400, detail="非法 id")
    if row is None:
        raise HTTPException(status_code=404, detail="扫描不存在")
    return {
        "id": str(row.id),
        "status": row.status,
        "params": row.params,
        "summary": row.summary,
        "error": row.error,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "finished_at": row.finished_at.isoformat() if row.finished_at else None,
    }


@router.get("/{bt_id}")
def get_backtest(bt_id: str, db: Session = Depends(get_db)):
    """回测结果：RUNNING 时前端轮询；DONE 返回 summary + daily_series + 订单明细。"""
    from app.models.backtest import FuturesBacktest

    try:
        row = db.get(FuturesBacktest, _uuid.UUID(bt_id))
    except ValueError:
        raise HTTPException(status_code=400, detail="非法 id")
    if row is None:
        raise HTTPException(status_code=404, detail="回测不存在")
    return {
        "id": str(row.id),
        "status": row.status,
        "params": row.params,
        "summary": row.summary,
        "daily_series": row.daily_series,
        "orders": (row.orders_detail or {}).get("items") if row.orders_detail else None,
        "truncated": (row.orders_detail or {}).get("truncated", False) if row.orders_detail else False,
        "error": row.error,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "finished_at": row.finished_at.isoformat() if row.finished_at else None,
    }


@router.get("")
def list_backtests(limit: int = 20, db: Session = Depends(get_db)):
    """历史回测列表（对比用，不含大 JSON）。"""
    from app.models.backtest import FuturesBacktest

    rows = (
        db.query(
            FuturesBacktest.id, FuturesBacktest.params, FuturesBacktest.status,
            FuturesBacktest.summary, FuturesBacktest.error,
            FuturesBacktest.created_at, FuturesBacktest.finished_at,
        )
        .order_by(FuturesBacktest.created_at.desc())
        .limit(min(limit, 100))
        .all()
    )
    return [
        {
            "id": str(r.id),
            "params": r.params,
            "status": r.status,
            "signals": ((r.summary or {}).get("signals") or {}).get("n") if r.summary else None,
            "variants_n": len((r.params or {}).get("variants") or []) if r.params else 0,
            "error": r.error,
            "created_at": r.created_at.isoformat() if r.created_at else None,
            "finished_at": r.finished_at.isoformat() if r.finished_at else None,
        }
        for r in rows
    ]
