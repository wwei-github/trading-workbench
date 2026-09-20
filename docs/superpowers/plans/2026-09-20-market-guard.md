# Market Guard（市场风险防护）实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 按 docs/15 实现 30 分钟级市场风险快检（2026-09-20 用户拍板）（4 信号 → L0~L3 分级），升级即时执行仓位保护动作，前端横幅展示状态与迁移历史。

**Architecture:** 新增 `risk_guard` 服务（信号采集直连币安 + 纯函数分级状态机）与 `risk_tasks` Celery 任务（每 30 分钟），开仓闸门在 `trade_engine.try_open_for_analysis` 与 `ai_tasks` 批量分发处各加一道；新表 `market_risk_state`（单行状态）+ `market_risk_history`（迁移记录），create_all 自动建表。前端新增 `RiskBanner` 组件轮询展示。

**Tech Stack:** FastAPI + SQLAlchemy 2.0 + Celery beat + httpx（币安合约公开接口）；React 18 + Ant Design 5 + TypeScript。

## Global Constraints

- 设计依据：`docs/15-系统性风险防护：普跌行情预案.md`（信号阈值/防抖规则以文档 §4/§5 为准）
- 新列必须进 `database.py:_run_migrations`；新表靠 `create_all`（本项目惯例）
- 验证命令：后端 `python3 -m compileall backend/app`；前端 `cd frontend && npx tsc --noEmit`
- 仓位动作仅作用于 `settings.TRADING_TESTNET` 对应网（与结算巡检同口径）
- 快检数据**直连**币安，禁走 ExchangePool K线缓存（docs/15 §4 口径）
- 所有阈值走 `config.py` Settings（可被 .env 覆盖），不在代码里写死
- 提交信息用中文，风格沿用 `feat:`/`fix:`/`docs:` 前缀

---

### Task 1: 配置项 + 数据模型

**Files:**

- Modify: `backend/app/config.py`（文件末尾 `TRADING_LEVERAGE_MIN` 之后）
- Create: `backend/app/models/market_risk.py`
- Modify: `backend/app/models/__init__.py`（如存在聚合导出则加一行；无则跳过）

**Interfaces:**

- Produces: `settings.MARKET_GUARD_*` 配置组；`MarketRiskState`（单行，id=1）、`MarketRiskHistory` 模型，字段如下述代码，后续任务按字段名引用

- [ ] **Step 1: config.py 加配置组**

在 `TRADING_LEVERAGE_MIN: int = 8` 块之后追加：

```python
    # 市场风险防护（docs/15，Market Guard）
    MARKET_GUARD_ENABLED: bool = True     # 总开关（false 时快检任务空转、闸门放行）
    GUARD_TOP_N: int = 20                 # 市场广度取样币种数（按 24h 成交额取前 N）
    GUARD_S1_PCT: float = -5.0            # S1：BTC 24h 跌幅阈值（%）
    GUARD_S1_L3_PCT: float = -8.0         # S1 直达 L3 的深跌阈值（%）
    GUARD_S2_PCT: float = -3.0            # S2：单币 24h 跌幅计入广度的阈值（%）
    GUARD_S2_BREADTH: float = 70.0        # S2：广度阈值（前 N 中跌幅达标的占比 %）
    GUARD_S3_MULT: float = 1.8            # S3：1h ATR14 放大倍数阈值
    GUARD_S4_L2_PCT: float = -3.5         # S4：3×15m 累计跌幅触发 L2（%）
    GUARD_S4_L3_PCT: float = -6.0         # S4 触发 L3 的急跌阈值（%，还需 24h 共振）
    GUARD_S4_L3_CONFIRM_24H: float = -3.0 # S4 升 L3 要求的 BTC 24h 共振阈值（%）
    GUARD_S4_RELEASE_PCT: float = -1.5    # S4 迟滞解除阈值（跌幅浅于此视为消失）
    GUARD_DOWNGRADE_ROUNDS: int = 4       # 降级防抖轮数（30 分钟/轮 ×4 ≈ 2 小时，等值于原 5 分钟×24 轮设计）
    GUARD_CONFIRM_ROUNDS: int = 2         # S1/S2/S3 升级确认轮数（防窗口滚动跳变；30 分钟节拍下 ≤1 小时）
    GUARD_STALE_S: int = 2700             # 数据时点陈旧阈值（秒，>45 分钟冻结；30 分钟节拍 ×1.5 余量）
    GUARD_OSC_LOCK_S: int = 3600          # L2 震荡锁时长（秒）
```

- [ ] **Step 2: 新建模型文件**

`backend/app/models/market_risk.py` 全文：

```python
"""市场风险防护状态（docs/15）。状态单行 id=1，历史只增不改。"""
from datetime import datetime
from sqlalchemy import DateTime, Integer, String, JSON
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class MarketRiskState(Base):
    __tablename__ = "market_risk_state"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)  # 恒为 1
    level: Mapped[int] = mapped_column(Integer, default=0)      # 0~3
    cause: Mapped[str] = mapped_column(String(16), default="normal")  # normal / s4
    signals: Mapped[dict] = mapped_column(JSON, default=dict)   # 最近一次信号快照 {s1,s2,s3,s4,btc_24h_pct,...}
    pending_level: Mapped[int] = mapped_column(Integer, default=0)   # 升级确认中的候选等级
    pending_count: Mapped[int] = mapped_column(Integer, default=0)   # 连续命中轮数
    below_count: Mapped[int] = mapped_column(Integer, default=0)     # 低于当前等级的连续轮数（降级防抖）
    s4_last_release_at: Mapped[datetime] = mapped_column(DateTime, nullable=True)
    osc_lock_until: Mapped[datetime] = mapped_column(DateTime, nullable=True)  # L2 震荡锁截止
    entered_level_at: Mapped[datetime] = mapped_column(DateTime, nullable=True)  # 进入当前等级时间
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class MarketRiskHistory(Base):
    __tablename__ = "market_risk_history"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    from_level: Mapped[int] = mapped_column(Integer)
    to_level: Mapped[int] = mapped_column(Integer)
    trigger: Mapped[dict] = mapped_column(JSON, default=dict)  # {reason, s1, s2, s3, s4}
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
```

注意：`from sqlalchemy import` 若项目其他模型统一从 `sqlalchemy.dialects.postgresql import JSON` 则保持一致（看 `app/models/scan.py` 头部写法照抄）。

- [ ] **Step 3: 注册进模型聚合**

查 `backend/app/models/__init__.py`：若其中 `from .xxx import ...` 聚合了其他模型（如 trade、scan），追加 `from .market_risk import MarketRiskState, MarketRiskHistory`；若该文件不存在则跳过（模型经 `app.models.trade` 之类直接导入，create_all 走 `Base.metadata` 需要模型模块被 import 过——确认 `app/main.py` 或 `database.py` 附近有 import 链，没有就在 `app/models/__init__.py` 补）。

- [ ] **Step 4: 验证建表**

```bash
python3 -m compileall backend/app -q && echo OK
```

本地起一次 FastAPI 或跑 `python3 -c "from app.database import Base, engine; import app.models.market_risk; Base.metadata.create_all(engine)"`（在 backend 目录、激活对应环境），确认 `market_risk_state`/`market_risk_history` 表创建无报错。

- [ ] **Step 5: Commit**

```bash
git add backend/app/config.py backend/app/models/market_risk.py backend/app/models/__init__.py
git commit -m "feat: Market Guard 配置项与风险状态/历史模型（docs/15 Task1）"
```

---

### Task 2: 信号采集 + 分级状态机（纯函数）

**Files:**

- Modify: `backend/app/services/binance_client.py`（加一个公开 `get_klines`）
- Create: `backend/app/services/risk_guard.py`

**Interfaces:**

- Consumes: Task 1 的 `settings.GUARD_*`
- Produces:
  - `BinanceClient.get_klines(symbol: str, interval: str, limit: int) -> list[list]`
  - `fetch_signals() -> dict | None`（None=数据失败）
  - `evaluate_level(sig: dict, state: dict) -> dict`（纯函数，返回新 state dict，含 `level`）
  - `GUARD_KEYS` 常量

- [ ] **Step 1: binance_client.py 加 get_klines**

在 `get_24h_tickers` 方法之后追加（照抄 `_request` 复用限流退避）：

```python
    def get_klines(self, symbol: str, interval: str, limit: int) -> list[list]:
        """合约 K线（公开接口，直连不走缓存——Market Guard 快检用）"""
        resp = self._request(self.futures_client, "/fapi/v1/klines", params={
            "symbol": symbol, "interval": interval, "limit": limit,
        })
        return resp.json()
```

- [ ] **Step 2: 写 risk_guard.py 信号采集与状态机**

`backend/app/services/risk_guard.py` 全文（信号采集 + 纯函数分级；动作与持久化在 Task 3）：

```python
"""市场风险防护（docs/15）：信号采集 + 分级状态机。

快检直连币安合约公开接口（3 个请求），不走 ExchangePool K线缓存——
缓存为小时级扫描设计，复用会让快检拿到最长 1 小时前的旧数据（docs/15 §4）。
分级规则（§4/§5）：
  命中数 = S1(BTC 24h ≤ -5%) + S2(广度 ≥70%) + S3(ATR 放大 ≥1.8x)
  L3 = 命中数≥3 或 S1≤-8% 或 (S4≤-6% 且 BTC 24h≤-3%)
  L2 = 命中数≥2 或 S4≤-3.5%
  L1 = 命中数≥1
防抖：S1/S2/S3 升级需连续 GUARD_CONFIRM_ROUNDS 轮；S4 即时；
  降级需连续 GUARD_DOWNGRADE_ROUNDS 轮低于当前等级且逐级；
  S4 迟滞：触发 -3.5% / 解除 -1.5%；L2 震荡锁 1 小时。
"""
import logging
from datetime import datetime, timedelta

from app.config import settings

logger = logging.getLogger(__name__)

_EXCLUDE = ("UP", "DOWN", "BEAR", "BULL", "HALF", "SHORT", "LONG")


# ── 信号采集（直连，无缓存）──

def fetch_signals() -> dict | None:
    """拉取四信号快照。任一请求失败返回 None（调用方据此冻结等级）。

    返回 {s1, s2, s3, s4, s1_hit, s2_hit, s3_hit, s4_hit, s4_l3_ok, ts}
    """
    from app.services.binance_client import BinanceClient
    client = BinanceClient()
    try:
        tickers = client.get_24h_tickers()
        btc_1h = client.get_klines("BTCUSDT", "1h", 40)
        btc_15m = client.get_klines("BTCUSDT", "15m", 10)
    except Exception as e:
        logger.warning("风险快检行情获取失败（等级冻结）: %s", e)
        return None
    finally:
        client.close()

    btc = next((t for t in tickers if t.get("symbol") == "BTCUSDT"), None)
    if not btc or not btc_1h or not btc_15m:
        logger.warning("风险快检数据不完整（等级冻结）")
        return None

    s1 = float(btc.get("priceChangePercent") or 0)

    # S2 广度：24h 成交额前 N 的 USDT 合约中跌幅达标占比
    pool = [
        t for t in tickers
        if t.get("symbol", "").endswith("USDT")
        and not any(k in t["symbol"] for k in _EXCLUDE)
    ]
    pool.sort(key=lambda t: float(t.get("quoteVolume") or 0), reverse=True)
    top = pool[: settings.GUARD_TOP_N]
    s2 = (
        sum(1 for t in top if float(t.get("priceChangePercent") or 0) <= settings.GUARD_S2_PCT)
        / len(top) * 100
    ) if top else 0.0

    # S3：已收盘 1h K线的 ATR14（近 14 根 vs 24h 前的 14 根）
    trs = _closed_trs(btc_1h)
    s3 = _atr(trs[-14:]) / _atr(trs[-38:-24]) if len(trs) >= 38 else 0.0

    # S4：最近 3 根已收盘 15m 累计涨跌幅（%）——只用已收盘K线，免疫插针
    closed15 = btc_15m[:-1]  # 最后一根未收盘，剔除
    if len(closed15) >= 3:
        open_px = float(closed15[-3][1])
        close_px = float(closed15[-1][4])
        s4 = (close_px / open_px - 1) * 100 if open_px > 0 else 0.0
    else:
        s4 = 0.0

    s4_hit = s4 <= settings.GUARD_S4_L2_PCT
    return {
        "s1": round(s1, 2), "s2": round(s2, 1), "s3": round(s3, 2), "s4": round(s4, 2),
        "s1_hit": s1 <= settings.GUARD_S1_PCT,
        "s2_hit": s2 >= settings.GUARD_S2_BREADTH,
        "s3_hit": s3 >= settings.GUARD_S3_MULT,
        "s4_hit": s4_hit,
        "s4_l3_ok": s4 <= settings.GUARD_S4_L3_PCT and s1 <= settings.GUARD_S4_L3_CONFIRM_24H,
        "ts": datetime.utcnow().isoformat(),
    }


def _closed_trs(klines: list[list]) -> list[float]:
    """已收盘K线的真实波幅 TR 序列（剔除首尾：首根缺前收盘、末根未收盘）"""
    trs = []
    for i in range(1, len(klines) - 1):
        h, l = float(klines[i][2]), float(klines[i][3])
        pc = float(klines[i - 1][4])
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    return trs


def _atr(trs: list[float]) -> float:
    return sum(trs) / len(trs) if trs else 0.0


# ── 分级状态机（纯函数，可离线验证）──

def evaluate_level(sig: dict, st: dict) -> dict:
    """按信号与上一状态计算新状态。st 为 MarketRiskState 行的 dict 化快照。

    st 键：level, cause, pending_level, pending_count, below_count,
           s4_last_release_at(iso|None), osc_lock_until(iso|None)
    返回新 st（含 level/cause/各计数），只算不存。
    """
    now = datetime.utcnow()
    st = {**st}
    level = int(st.get("level") or 0)
    hits = sum(1 for k in ("s1_hit", "s2_hit", "s3_hit") if sig.get(k))
    s4_raw = float(sig.get("s4") or 0.0)
    lock_active = bool(
        st.get("osc_lock_until")
        and datetime.fromisoformat(st["osc_lock_until"]) > now
    )

    # S4 迟滞：s4 驱动期间，跌幅浅于解除阈值才视为消失
    s4_active = s4_raw <= settings.GUARD_S4_L2_PCT or (
        st.get("cause") == "s4" and s4_raw <= settings.GUARD_S4_RELEASE_PCT
    )

    # 目标等级（原始口径）
    l3 = (
        hits >= 3
        or (sig.get("s1") is not None and sig["s1"] <= settings.GUARD_S1_L3_PCT)
        or (s4_raw <= settings.GUARD_S4_L3_PCT
            and (sig.get("s1") or 0) <= settings.GUARD_S4_L3_CONFIRM_24H)
    )
    raw = 3 if l3 else 2 if (hits >= 2 or s4_active) else 1 if hits >= 1 else 0

    if raw > level:
        immediate = raw == 3 or s4_active  # S4/L3 即时；纯 S1/S2/S3 需确认
        if immediate:
            # 震荡锁内同因再触发：锁 L2、走常规降级（cause=normal，不再快速衰减）
            if raw == 2 and s4_active and lock_active:
                st.update(level=2, cause="normal", below_count=0, pending_level=0,
                          pending_count=0, entered_level_at=now.isoformat(),
                          osc_lock_until=(now + timedelta(seconds=settings.GUARD_OSC_LOCK_S)).isoformat())
                return st
            st.update(level=raw, cause="s4" if raw == 2 else "normal",
                      below_count=0, pending_level=0, pending_count=0,
                      entered_level_at=now.isoformat())
            return st
        # S1/S2/S3 升级确认（防窗口滚动/成分变化单轮跳变）
        if int(st.get("pending_level") or 0) == raw:
            cnt = int(st.get("pending_count") or 0) + 1
            if cnt >= settings.GUARD_CONFIRM_ROUNDS:
                st.update(level=raw, cause="normal", below_count=0,
                          pending_level=0, pending_count=0, entered_level_at=now.isoformat())
            else:
                st.update(pending_level=raw, pending_count=cnt)
        else:
            st.update(pending_level=raw, pending_count=1)
        return st

    if raw < level:
        # s4 单驱等级（未锁定）：信号消失即按常规命中重算（快速衰减），并设震荡锁窗口
        if st.get("cause") == "s4" and not s4_active and not lock_active:
            st.update(level=raw, cause="normal", below_count=0, pending_level=0,
                      pending_count=0, entered_level_at=now.isoformat(),
                      osc_lock_until=(now + timedelta(seconds=settings.GUARD_OSC_LOCK_S)).isoformat())
            return st
        # 常规降级：连续 N 轮、逐级（棘轮，max 保证一次最多降一级）
        cnt = int(st.get("below_count") or 0) + 1
        st["below_count"] = cnt
        if cnt >= settings.GUARD_DOWNGRADE_ROUNDS:
            st.update(level=max(raw, level - 1), cause="normal", below_count=0,
                      pending_level=0, pending_count=0, entered_level_at=now.isoformat())
        return st

    # 持平：清计数
    st.update(pending_level=0, pending_count=0, below_count=0)
    return st


def downgrade_one_level(st: dict) -> dict:
    """外置降级入口（预留）：降一级并清 cause。"""
    now = datetime.utcnow()
    return {**st, "level": max(0, int(st.get("level") or 0) - 1), "cause": "normal",
            "below_count": 0, "pending_level": 0, "pending_count": 0,
            "entered_level_at": now.isoformat()}
```

- [ ] **Step 3: 离线验证状态机（不联网）**

在 backend 目录执行内联脚本：

```bash
cd backend && python3 - <<'EOF'
from app.services.risk_guard import evaluate_level
from app.config import settings

st = {"level": 0, "cause": "normal", "pending_level": 0, "pending_count": 0,
      "below_count": 0, "s4_last_release_at": None, "osc_lock_until": None}

# 1) S1 单命中 → 需 2 轮确认
s = {"s1": -5.2, "s2": 10, "s3": 1.0, "s4": 0.0, "s1_hit": True, "s2_hit": False,
     "s3_hit": False, "s4_hit": False, "s4_l3_ok": False}
st2 = evaluate_level(s, st); assert st2["level"] == 0 and st2["pending_count"] == 1, st2
st3 = evaluate_level(s, st2); assert st3["level"] == 1, st3
print("S1 两轮确认升级 OK")

# 2) S4 即时升 L2
s4 = {"s1": -1.0, "s2": 10, "s3": 1.0, "s4": -4.0, "s1_hit": False, "s2_hit": False,
      "s3_hit": False, "s4_hit": True, "s4_l3_ok": False}
st4 = evaluate_level(s4, st); assert st4["level"] == 2 and st4["cause"] == "s4", st4
print("S4 即时 L2 OK")

# 3) S4≤-6 且 24h≤-3 直达 L3
s4l3 = {**s4, "s4": -6.5, "s1": -3.5, "s4_l3_ok": True}
st5 = evaluate_level(s4l3, st); assert st5["level"] == 3, st5
print("S4+24h 共振 L3 OK")

# 4) S4≤-6 但 24h 浅 → 只 L2
s4deep = {**s4, "s4": -6.5, "s4_l3_ok": False}
st6 = evaluate_level(s4deep, st); assert st6["level"] == 2, st6
print("S4 深 24h 浅仅 L2 OK")

# 5) S4 消失快速回落 + 震荡锁 + 24 轮棘轮
down = {**s4, "s4": 0.5, "s4_hit": False}
st7 = evaluate_level(down, st6)          # s4 消失（0.5 > -1.5）→ 快速回落 L0 并设震荡锁
assert st7["level"] == 0 and st7["osc_lock_until"], st7
st8 = evaluate_level(s4, st7)            # 1h 内同因再触发 → 锁 L2，cause=normal
assert st8["level"] == 2 and st8["cause"] == "normal", st8
st9 = evaluate_level(down, st8)          # 再消失 → 不再快速回落，走 24 轮棘轮
assert st9["level"] == 2 and st9["below_count"] == 1, st9
for _ in range(settings.GUARD_DOWNGRADE_ROUNDS - 1):
    st9 = evaluate_level(down, st9)
assert st9["level"] == 1, st9            # 一次最多降一级（棘轮）
print("快速衰减/震荡锁/降级棘轮 OK")

# 6) 三信号共振 → L3
res = {"s1": -6.0, "s2": 80, "s3": 2.0, "s4": -1.0, "s1_hit": True, "s2_hit": True,
       "s3_hit": True, "s4_hit": False, "s4_l3_ok": False}
st8 = evaluate_level(res, st); assert st8["level"] == 3, st8
print("三信号共振 L3 OK")
print("全部通过")
EOF
```

预期输出 `全部通过`。若 evaluate_level 分支有 bug 就地修到通过。

- [ ] **Step 4: Commit**

```bash
git add backend/app/services/binance_client.py backend/app/services/risk_guard.py
git commit -m "feat: 风险信号采集（直连币安）与分级状态机（迟滞/确认/棘轮）"
```

---

### Task 3: 状态持久化 + 升级动作 + 通知

**Files:**

- Modify: `backend/app/services/risk_guard.py`（追加执行层函数）
- Modify: `backend/app/services/trade_engine.py:924` 附近（settle 零仓位分支识别 guard 平仓）

**Interfaces:**

- Consumes: Task 2 的 `fetch_signals`/`evaluate_level`；`trade_engine._replace_sl`；`BinanceTrader.market_order/cancel_all_algo`；`notify.send_feishu_text`
- Produces:
  - `run_risk_check() -> None`（快检任务主入口）
  - `get_current_level(db) -> int`（闸门读取）
  - `execute_level_actions(db, trader, level: int) -> dict`（返回 {sl_moved, closed}）
  - settle 识别 `raw.guard_closed` → `exit_reason="market_guard"`

- [ ] **Step 1: risk_guard.py 追加持久化与动作**

文件末尾追加：

```python
# ── 持久化与执行（Task 3）──

def _load_state(db) -> tuple["MarketRiskState", dict]:
    from app.models.market_risk import MarketRiskState
    row = db.get(MarketRiskState, 1)
    if row is None:
        row = MarketRiskState(id=1, level=0, cause="normal")
        db.add(row)
        db.flush()
    st = {
        "level": row.level, "cause": row.cause,
        "pending_level": row.pending_level, "pending_count": row.pending_count,
        "below_count": row.below_count,
        "s4_last_release_at": row.s4_last_release_at.isoformat() if row.s4_last_release_at else None,
        "osc_lock_until": row.osc_lock_until.isoformat() if row.osc_lock_until else None,
        "entered_level_at": row.entered_level_at.isoformat() if row.entered_level_at else None,
    }
    return row, st


def _save_state(db, row, st: dict, sig: dict | None, old_level: int) -> None:
    from app.models.market_risk import MarketRiskHistory
    row.level = st["level"]; row.cause = st["cause"]
    row.pending_level = st["pending_level"]; row.pending_count = st["pending_count"]
    row.below_count = st["below_count"]
    row.s4_last_release_at = datetime.fromisoformat(st["s4_last_release_at"]) if st.get("s4_last_release_at") else None
    row.osc_lock_until = datetime.fromisoformat(st["osc_lock_until"]) if st.get("osc_lock_until") else None
    row.entered_level_at = datetime.fromisoformat(st["entered_level_at"]) if st.get("entered_level_at") else None
    if sig:
        row.signals = {k: sig[k] for k in ("s1", "s2", "s3", "s4") if k in sig}
        row.updated_at = datetime.utcnow()
    if st["level"] != old_level:
        db.add(MarketRiskHistory(from_level=old_level, to_level=st["level"], trigger={
            "reason": f"{'升级' if st['level'] > old_level else '降级'} L{old_level}→L{st['level']}",
            **(row.signals or {}),
        }))


def get_current_level(db) -> int:
    """开仓闸门读取（数据失败冻结即：表里是什么就是什么）"""
    from app.models.market_risk import MarketRiskState
    row = db.get(MarketRiskState, 1)
    return int(row.level) if row else 0


def execute_level_actions(db, trader, level: int) -> dict:
    """L2/L3 存量仓位动作（仅本网）。L2：盈利方向 SL 收成本；L3：深亏(>0.5R)多单市价离场、
    盈利空单锁利润。返回动作计数供通知。"""
    from datetime import timezone as _tz
    from sqlalchemy import select as _select
    from app.models.trade import TradeRecord
    recs = db.execute(_select(TradeRecord).where(
        TradeRecord.status.in_(("OPENED", "TP1_HIT", "TP2_HIT")),
        TradeRecord.testnet == settings.TRADING_TESTNET,
    )).scalars().all()
    if not recs:
        return {"sl_moved": 0, "closed": 0}
    # 现价一次拉全（BinanceTrader 无公开行情方法，走 BinanceClient 24hr ticker）
    from app.services.binance_client import BinanceClient
    _c = BinanceClient()
    try:
        px = {t["symbol"]: float(t.get("lastPrice") or 0) for t in _c.get_24h_tickers()}
    finally:
        _c.close()
    moved = closed = 0
    for rec in recs:
        try:
            price = px.get(rec.symbol, 0.0)
            entry = float(rec.entry_price or 0)
            if entry <= 0:
                continue
            qty = abs(float(rec.qty or 0))
            risk = float(rec.risk_amount or 0)
            raw = dict(rec.raw or {})
            if level >= 2:
                # 盈利方向：多单 price>entry / 空单 price<entry → SL 收成本
                in_profit = price > entry if rec.direction == "long" else price < entry
                if in_profit:
                    from app.services.trade_engine import _replace_sl
                    open_ids = trader.open_order_ids(rec.symbol)
                    if float(rec.stop_loss or 0) != entry:
                        _replace_sl(db, trader, rec, open_ids, entry, reason="market_guard_l2")
                        moved += 1
                # L3 深亏多单市价离场（空单不平，docs/15 §3）
                if level >= 3 and rec.direction == "long" and price < entry and risk > 0:
                    loss_r = (entry - price) * qty / risk
                    if loss_r > 0.5:
                        trader.cancel_all_algo(rec.symbol)
                        trader.market_order(rec.symbol, "sell", qty, reduce_only=True)
                        raw["guard_closed"] = True
                        rec.raw = raw  # 新 dict，确保 SQLAlchemy 落库
                        _add_guard_event(db, rec, {"loss_r": round(loss_r, 2), "price": price})
                        closed += 1
            db.commit()
        except Exception as e:
            db.rollback()
            logger.exception("防护动作失败 %s: %s", rec.symbol, e)
    return {"sl_moved": moved, "closed": closed}


def _add_guard_event(db, rec, detail: dict) -> None:
    from app.services.trade_engine import _add_event
    _add_event(db, rec, "MARKET_GUARD_CLOSE", detail)


def run_risk_check() -> dict:
    """快检主入口：采集→评估→持久化→升级即时执行→通知。数据失败冻结。"""
    from app.database import SessionLocal
    from app.services.binance_trader import BinanceTrader
    from app.services.notify import send_feishu_text
    sig = fetch_signals()
    result = {"level": 0, "changed": False, "actions": None, "frozen": sig is None}
    if sig is None:
        logger.info("快检数据失败，等级冻结")
        return result
    db = SessionLocal()
    try:
        row, st = _load_state(db)
        old_level = st["level"]
        st = evaluate_level(sig, st)
        _save_state(db, row, st, sig, old_level)
        db.commit()
        result["level"] = st["level"]
        if st["level"] > old_level:
            result["changed"] = True
            trader = BinanceTrader()
            if trader.configured:
                result["actions"] = execute_level_actions(db, trader, st["level"])
            send_feishu_text(
                f"⚠️ 市场风险升级 L{old_level}→L{st['level']}\n"
                f"BTC 24h {sig['s1']}% | 广度 {sig['s2']}% | ATR {sig['s3']}x | 3×15m {sig['s4']}%\n"
                f"动作：{'开仓暂停' + (str(result['actions']) if result['actions'] else '')}"
            )
        elif st["level"] < old_level:
            result["changed"] = True
            send_feishu_text(f"✅ 市场风险降级 L{old_level}→L{st['level']}（防抖确认后）")
        logger.info("风险快检：L%s（s1=%s s2=%s s3=%s s4=%s）",
                    st["level"], sig["s1"], sig["s2"], sig["s3"], sig["s4"])
        return result
    finally:
        db.close()
```

- [ ] **Step 2: settle 识别防护平仓**

`trade_engine.py` 零仓位结算分支（约 L953 `if insurance_clear != 0:` 之前）改为三分支——把：

```python
                if insurance_clear != 0:
```

改为：

```python
                if (rec.raw or {}).get("guard_closed"):
                    exit_reason = "market_guard"
                elif insurance_clear != 0:
```

（后续 `else:` 分支保持原样；`rec.status`/`closed_at`/SETTLE 事件等逻辑不动。）

- [ ] **Step 3: 离线验证**

```bash
python3 -m compileall backend/app -q && echo OK
```

再跑一次 Task 2 的内联验证脚本确认状态机未被破坏。

- [ ] **Step 4: Commit**

```bash
git add backend/app/services/risk_guard.py backend/app/services/trade_engine.py
git commit -m "feat: 风险状态持久化/升级动作（SL收成本/深亏多单离场）/飞书通知与防护平仓结算口径"
```

---

### Task 4: Celery 任务 + 开仓闸门 + AI 批量跳过

**Files:**

- Create: `backend/app/tasks/risk_tasks.py`
- Modify: `backend/app/celery_app.py`（includes + beat 表）
- Modify: `backend/app/services/trade_engine.py:587` 附近（开仓资格闸门）
- Modify: `backend/app/tasks/ai_tasks.py:104` 附近（批量分发跳过）

**Interfaces:**

- Consumes: Task 3 `run_risk_check`/`get_current_level`；`trade_tasks._acquire_open_lock/_release_open_lock`
- Produces: beat 任务 `app.tasks.risk_tasks.market_risk_check`（每 30 分钟）；闸门在 L≥2 时 `_mark_open_block(db, a, ...)`

- [ ] **Step 1: risk_tasks.py**

```python
"""市场风险快检任务（docs/15）：每 30 分钟，升级即时执行仓位动作。"""
import logging
from uuid import uuid4

from app.celery_app import celery_app
from app.config import settings

logger = logging.getLogger(__name__)


@celery_app.task(name="app.tasks.risk_tasks.market_risk_check", max_retries=0)
def market_risk_check():
    if not settings.MARKET_GUARD_ENABLED:
        return
    # 与结算巡检共用开仓锁：升级动作要动 SL/市价平仓，禁止与 settle 并发
    from redis import Redis
    from app.tasks.trade_tasks import _acquire_open_lock, _release_open_lock, _OPEN_LOCK_TTL_S
    from app.database import SessionLocal
    from app.services.risk_guard import run_risk_check
    from app.tasks.task_utils import open_task, close_task  # 若 open_task 在别处，照 trade_tasks 的 import 路径

    task_id = open_task(task_type="guard", task_name="风险快检", trigger="scheduled")
    r = Redis.from_url(settings.REDIS_URL, decode_responses=True)
    ident = uuid4().hex
    try:
        if not _acquire_open_lock(r, ident, wait_s=30, ttl_s=_OPEN_LOCK_TTL_S):
            close_task(task_id, "skipped", summary="开仓锁被占用，本轮快检跳过")
            return
        try:
            result = run_risk_check()
        finally:
            _release_open_lock(r, ident)
        summary = (
            f"数据失败，等级冻结" if result.get("frozen")
            else f"L{result['level']}" + ("（升级，已执行动作）" if result.get("changed") and result["level"] > 0 else "")
        )
        close_task(task_id, "completed", summary=summary, detail=result)
    except Exception as e:
        logger.exception("风险快检任务失败: %s", e)
        close_task(task_id, "failed", error=e)
    finally:
        r.close()
```

实现时注意：`open_task/close_task` 的真实 import 路径以 `trade_tasks.py` 头部为准照抄；`task_type` 若有枚举约束改用 `"trade"` 同款。

- [ ] **Step 2: celery_app.py 注册**

在 includes 列表加 `"app.tasks.risk_tasks"`；`beat_schedule` 加（与日报条目同格式）：

```python
        "market-risk-check": {
            "task": "app.tasks.risk_tasks.market_risk_check",
            "schedule": crontab(minute="*/30"),  # 每 30 分钟快检（用户拍板 2026-09-20，原设计 5 分钟）
        },
```

- [ ] **Step 3: 开仓闸门（trade_engine.try_open_for_analysis）**

在单数上限检查（约 L587 `if cfg.max_open_trades > 0 ...`）**之前**插入：

```python
    # 市场风险防护（docs/15）：L2/L3 暂停全部开仓
    if settings.MARKET_GUARD_ENABLED:
        from app.services.risk_guard import get_current_level
        lvl = get_current_level(db)
        if lvl >= 2:
            logger.info("即时开仓跳过 %s：市场防护 L%d 暂停开仓", a.symbol, lvl)
            _mark_open_block(db, a, f"市场防护 L{lvl} 暂停开仓")
            return False
```

- [ ] **Step 4: AI 批量分析跳过（ai_tasks.run_ai_analysis_task）**

在 `force = only_scan_result_id is not None`（约 L123）之前插入：

```python
        # 市场风险防护（docs/15 §5）：L2/L3 开仓已暂停，批量 AI 分析不再分发（省 LLM 成本）。
        # 手动单币重分析（only_scan_result_id 非空）不受限，结果仅展示
        if only_scan_result_id is None and settings.MARKET_GUARD_ENABLED:
            from app.services.risk_guard import get_current_level
            lvl = get_current_level(db)
            if lvl >= 2:
                logger.info("市场防护 L%d 生效，扫描 #%s 命中 %d 币跳过 AI 分析", lvl, scan_record_id, len(results))
                return
```

- [ ] **Step 5: 验证 + Commit**

```bash
python3 -m compileall backend/app -q && echo OK
git add backend/app/tasks/risk_tasks.py backend/app/celery_app.py backend/app/services/trade_engine.py backend/app/tasks/ai_tasks.py
git commit -m "feat: 30分钟风险快检任务接入beat；L2/L3开仓闸门与批量AI分析跳过"
```

---

### Task 5: API + 前端横幅 + 出场原因映射

**Files:**

- Modify: `backend/app/api/trades.py`（加 GET /market-risk）
- Modify: `frontend/src/types/index.ts`（MarketRisk 类型）
- Modify: `frontend/src/api/scan.ts`（tradesApi.marketRisk）
- Create: `frontend/src/components/RiskBanner.tsx`
- Modify: `frontend/src/pages/ScanResult.tsx`（页面顶部挂横幅）
- Modify: `frontend/src/components/TradesPanel.tsx`（EXIT_REASON_MAP 加 market_guard）

**Interfaces:**

- Consumes: Task 3 的 `get_current_level`、`MarketRiskState/History`
- Produces: `GET /api/trades/market-risk?history_limit=30`；`RiskBanner` 组件（零 props，自行轮询）

- [ ] **Step 1: 后端接口**

`app/api/trades.py` 加（db 获取方式照该文件现有路由的写法）：

```python
@router.get("/market-risk")
def get_market_risk(history_limit: int = 30):
    """市场风险防护状态 + 最近等级迁移历史（docs/15 §6 展示）"""
    from app.models.market_risk import MarketRiskState, MarketRiskHistory
    from sqlalchemy import select
    db = SessionLocal()
    try:
        row = db.get(MarketRiskState, 1)
        hist = db.execute(
            select(MarketRiskHistory)
            .order_by(MarketRiskHistory.created_at.desc())
            .limit(min(history_limit, 100))
        ).scalars().all()
        return {
            "level": row.level if row else 0,
            "cause": row.cause if row else "normal",
            "signals": row.signals if row else {},
            "entered_level_at": row.entered_level_at.isoformat() if row and row.entered_level_at else None,
            "updated_at": row.updated_at.isoformat() if row and row.updated_at else None,
            "history": [
                {
                    "id": str(h.id), "from_level": h.from_level, "to_level": h.to_level,
                    "trigger": h.trigger, "created_at": h.created_at.isoformat(),
                } for h in hist
            ],
        }
    finally:
        db.close()
```

注意：若 `trades.py` 里已有 `/{trade_id}/...` 单段动态路由在本路由之前注册，FastAPI 字面量优先级按注册顺序——`/market-risk` 无冲突（单段动态路由不存在于该文件则无需处理；有 `@router.get("/{trade_id}")` 之类就把本路由放到它前面）。验证：`curl localhost:8000/api/trades/market-risk`。

- [ ] **Step 2: 前端类型与 API**

`types/index.ts` 追加：

```typescript
export interface MarketRiskSignals {
  s1: number;
  s2: number;
  s3: number;
  s4: number;
}
export interface MarketRiskHistoryItem {
  id: string;
  from_level: number;
  to_level: number;
  trigger: {
    reason?: string;
    s1?: number;
    s2?: number;
    s3?: number;
    s4?: number;
  };
  created_at: string;
}
export interface MarketRisk {
  level: 0 | 1 | 2 | 3;
  cause: string;
  signals: MarketRiskSignals;
  entered_level_at: string | null;
  updated_at: string | null;
  history: MarketRiskHistoryItem[];
}
```

`api/scan.ts` 的 tradesApi 对象内（transfers 旁）加：

```typescript
    marketRisk: (historyLimit = 30) =>
      api.get<MarketRisk>('/trades/market-risk', { params: { history_limit: historyLimit } }).then((r) => r.data),
```

（`MarketRisk` 加入该文件已有的 types import。）

- [ ] **Step 3: RiskBanner.tsx**

```typescript
import { useEffect, useState } from 'react'
import { Popover, Typography } from 'antd'
import { tradesApi } from '../api/scan'
import type { MarketRisk } from '../types'

const LEVELS = [
  { label: '市场正常', color: '#52c41a', bg: '#f6ffed', border: '#b7eb8f' },
  { label: '警戒', color: '#faad14', bg: '#fffbe6', border: '#ffe58f' },
  { label: '限制开仓', color: '#fa8c16', bg: '#fff7e6', border: '#ffd591' },
  { label: '防御模式', color: '#f5222d', bg: '#fff1f0', border: '#ffa39e' },
]

const SIGNAL_DEFS = [
  { key: 's1', name: 'BTC 24h', unit: '%', thr: '≤ -5' },
  { key: 's2', name: '广度(前20)', unit: '%', thr: '≥ 70' },
  { key: 's3', name: 'ATR 放大', unit: 'x', thr: '≥ 1.8' },
  { key: 's4', name: '3×15m 急跌', unit: '%', thr: '≤ -3.5' },
] as const

/** 市场风险横幅（docs/15 §6）：30 秒轮询，L0 单行低调展示，升级高亮 + 信号详情 */
export default function RiskBanner() {
  const [risk, setRisk] = useState<MarketRisk | null>(null)

  useEffect(() => {
    let alive = true
    const load = () => tradesApi.marketRisk().then((d) => { if (alive) setRisk(d) }).catch(() => {})
    load()
    const t = setInterval(load, 30_000)
    return () => { alive = false; clearInterval(t) }
  }, [])

  if (!risk) return null
  const lv = LEVELS[risk.level] ?? LEVELS[0]
  const sigText = SIGNAL_DEFS.map((d) => `${d.name} ${risk.signals[d.key] ?? '-'}${d.unit}`).join('  ')

  return (
    <Popover
      placement="bottomLeft"
      title={`市场风险 L${risk.level} · ${lv.label}`}
      content={
        <div style={{ maxWidth: 360 }}>
          <table style={{ width: '100%', fontSize: 12, marginBottom: 8 }}>
            <tbody>
              {SIGNAL_DEFS.map((d) => (
                <tr key={d.key}>
                  <td>{d.name}</td>
                  <td>{risk.signals[d.key] ?? '-'}{d.unit}</td>
                  <td style={{ color: '#999' }}>阈值 {d.thr}{d.unit}</td>
                </tr>
              ))}
            </tbody>
          </table>
          <Typography.Text type="secondary" style={{ fontSize: 12 }}>
            {risk.history.slice(0, 8).map((h) => (
              <div key={h.id}>
                {h.created_at.slice(5, 16).replace('T', ' ')} L{h.from_level}→L{h.to_level}（{h.trigger?.reason ?? ''}）
              </div>
            ))}
            {risk.history.length === 0 && <div>近 48h 无等级迁移</div>}
          </Typography.Text>
        </div>
      }
    >
      <div style={{
        display: 'inline-flex', alignItems: 'center', gap: 8,
        padding: '2px 12px', borderRadius: 4, marginBottom: 8,
        background: lv.bg, border: `1px solid ${lv.border}`, color: lv.color,
        cursor: 'pointer', fontSize: 13,
      }}>
        <span>{['🟢', '🟡', '🟠', '🔴'][risk.level]}</span>
        <strong>{lv.label}</strong>
        {risk.level > 0 && <span style={{ color: '#666' }}>{sigText}</span>}
      </div>
    </Popover>
  )
}
```

- [ ] **Step 4: 挂载 + 出场原因**

`ScanResult.tsx`：import RiskBanner，在 `<Tabs` 之前（页面容器顶部）加 `<RiskBanner />`。
`TradesPanel.tsx`：EXIT_REASON_MAP（若为常量映射对象）加 `market_guard: '防护平仓'`；该值显示为红色系 tag（沿用 liquidation 的展示样式）。

- [ ] **Step 5: 验证 + Commit**

```bash
cd frontend && npx tsc --noEmit && echo TS-OK
git add backend/app/api/trades.py frontend/src/types/index.ts frontend/src/api/scan.ts frontend/src/components/RiskBanner.tsx frontend/src/pages/ScanResult.tsx frontend/src/components/TradesPanel.tsx
git commit -m "feat: 市场风险API与前端横幅（状态/信号/迁移时间线），防护平仓出场原因"
```

---

### Task 6: 全量回归 + 手动验证 + 推送

- [ ] **Step 1: 全量验证**

```bash
python3 -m compileall backend/app -q && echo PY-OK
cd frontend && npx tsc --noEmit && echo TS-OK
```

- [ ] **Step 2: 本地手动冒烟（可选，本地起服务时）**

```bash
# 触发一次快检（本地 worker）：celery -A app.celery_app worker/beat 起来后观察日志
# 或直接 python3 -c "from app.services.risk_guard import run_risk_check; print(run_risk_check())"
# 预期：打印当前 L 等级与四信号值；再 curl /api/trades/market-risk 有数据
```

- [ ] **Step 3: 推送（CICD 自动部署）**

```bash
git push origin main
```

部署成功（飞书卡片确认）后，等下一整点半（:00/:30 后几分钟）观察服务器日志 `docker compose logs backend | grep 风险快检 | tail -3`，横幅在页面出现即全链路通。

- [ ] **Step 4: 文档验收**

按 `docs/15` §7 勾选可实现项（S4 注入/窗口滚动/震荡锁模拟属后续灰度观察项，标注"上线后验证"）。
