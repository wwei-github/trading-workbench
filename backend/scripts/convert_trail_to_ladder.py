# -*- coding: utf-8 -*-
"""存量 trail 单转 ladder（策略一）一次性脚本（2026-10-12，设计见
.trae/documents/存量trail单转ladder设计.md）。

背景：策略二（纯跟踪）删除后，存量在跑 trail 单（raw.tp_mode=="trail"）无止盈
挂单，巡检仅原 SL 兜底。本脚本把它们一次性转为 ladder 出场管理：
- Case A（现价未达 1.5R）：挂 1.5R 止盈单（TP1，无 TP2 口径 80%），成交后由
  既有巡检转 TP1_HIT → 保本 → 剩余 20% 跟进止损；
- Case B（现价已越过 1.5R，含边界）：不挂止盈（挂上会立即成交），状态置
  TP1_HIT（语义=已过 1.5R 进入保本+跟进），并立即执行一次保本；跟进由
  下一轮巡检接管。
引擎零改动；默认 dry-run，--apply 才执行。线上执行由人工发起。

用法（cwd=backend）：
  python scripts/convert_trail_to_ladder.py            # dry-run：只打印计划
  python scripts/convert_trail_to_ladder.py --apply    # 执行转换
"""
import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import select  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app.database import SessionLocal  # noqa: E402
from app.models.system_config import SystemConfig  # noqa: E402
from app.models.trade import TradeEvent, TradeRecord  # noqa: E402
from app.services.account_registry import get_account  # noqa: E402
from app.services.binance_trader import BinanceTrader  # noqa: E402
from app.services.trade_engine import (  # noqa: E402
    _add_event, _close_side, _move_sl_breakeven, _tp_ratio_cfg,
)

RR_MULT = 1.5  # 1.5R 止盈档（与 AI_RR_MIN 铁律下限同值）


def _rec_acc_key(rec: TradeRecord) -> str:
    """账号键解析（与 trade_engine._rec_acc_key 同款，旧行回落 testnet）。"""
    return rec.account_key or ("testnet-1" if rec.testnet else "live-1")


def _open_event_detail(db: Session, rec: TradeRecord) -> dict:
    """开单时点的 OPEN 事件 detail（原始止损/开仓价快照来源）；缺失返回 {}。"""
    ev = db.execute(
        select(TradeEvent)
        .where(TradeEvent.trade_record_id == rec.id,
               TradeEvent.event_type == "OPEN")
        .order_by(TradeEvent.created_at)
        .limit(1)
    ).scalars().first()
    return (ev.detail or {}) if ev is not None else {}


def _r_basis(rec: TradeRecord, detail: dict) -> Optional[tuple]:
    """R 基准：返回 (entry, r_dist, source)；不可得返回 None。

    原始止损取 OPEN 事件快照——rec.stop_loss 是现值（trail 武装/跟进后可能已被
    改动），不可用于 R 计算。回退链：OPEN 快照缺失 → risk_amount ÷ qty
    （qty 偏保守：可能已含落袋缩减）→ 仍不可得返回 None（跳过不猜）。"""
    entry = float(rec.entry_price or 0)
    if entry <= 0:
        entry = float(detail.get("fill_price") or detail.get("entry_price") or 0)
    if entry <= 0:
        return None
    sl0 = float(detail.get("stop_loss") or 0)
    if sl0 > 0:
        dist = abs(entry - sl0)
        if dist > 0:
            return entry, dist, "open_event"
    risk = float(rec.risk_amount or 0)
    qty = float(detail.get("qty") or rec.qty or 0)
    if risk > 0 and qty > 0:
        return entry, risk / qty, "risk_amount/qty"
    return None


def _level_1p5r(entry: float, r_dist: float, direction: str) -> float:
    """1.5R 止盈档价（未取整；取整由 trader.round_price 完成）。"""
    return entry + RR_MULT * r_dist if direction == "long" else entry - RR_MULT * r_dist


def _tp_triggered(last: float, level: float, direction: str) -> bool:
    """现价是否已越过 1.5R 档（多单 last ≥ level / 空单 last ≤ level，含边界）——
    Case B 判定与「挂单是否立即成交」复检共用同一口径（与止盈单 CONTRACT_PRICE
    触发一致）。level 取整后判定，故 Case A 不会挂出立即触发的止盈单。"""
    return last >= level if direction == "long" else last <= level


def _fetch_candidates(db: Session) -> list:
    """OPENED 且 raw.tp_mode=="trail" 的记录（Python 侧过滤，避免 JSON 方言差异）。"""
    rows = db.execute(
        select(TradeRecord).where(TradeRecord.status == "OPENED")
    ).scalars().all()
    return [r for r in rows if (r.raw or {}).get("tp_mode") == "trail"]


def _build_traders(records: list) -> dict:
    """按账号键构建 trader（镜像 trade_engine._rec_trader 的回退口径）。"""
    traders: dict = {}
    for rec in records:
        key = _rec_acc_key(rec)
        if key in traders:
            continue
        traders[key] = (BinanceTrader(key) if get_account(key)
                        else BinanceTrader(rec.testnet))
    return traders


def _bookmark_now(trader, rec: TradeRecord) -> float:
    """当前价差盈亏累计（_tranche_pnl 书签口径，起点同 _record_realized 回拨 5s）。

    转换时推进书签到此刻：O8 浮盈落袋等已实现的价差不会被日后 TP1_FILL 档差
    重复计入（书签只增不减）。"""
    since_ms = int(rec.opened_at.replace(tzinfo=timezone.utc).timestamp() * 1000) - 5_000
    return round(trader.realized_price_pnl_since(rec.symbol, since_ms), 6)


def convert_one(db: Session, trader, rec: TradeRecord, cfg,
                fee_rate: Optional[float], apply: bool) -> dict:
    """单条转换；返回报告行（result: dry-A/dry-B/converted_A/converted_B/skip/failed）。
    apply 分支在函数内自行 commit（Case B 保本二次 commit，失败不撤转换）。"""
    row = {
        "symbol": rec.symbol, "account": _rec_acc_key(rec),
        "net": "testnet" if rec.testnet else "live",
        "mode": None, "entry": None, "sl0": None, "r_dist": None,
        "level": None, "last": None, "qty": None, "qty_tp1": None,
        "result": "skip", "reason": "",
    }
    raw = rec.raw or {}
    if raw.get("trail_convert"):
        row["reason"] = "已转换过（trail_convert 标记）"
        return row
    basis = _r_basis(rec, _open_event_detail(db, rec))
    if basis is None:
        row["reason"] = "R 基准不可得（OPEN 事件与 risk_amount 均缺失）"
        return row
    entry, r_dist, source = basis
    level = trader.round_price(rec.symbol, _level_1p5r(entry, r_dist, rec.direction))
    row.update({
        "entry": entry,
        "sl0": (entry - r_dist) if rec.direction == "long" else (entry + r_dist),
        "r_dist": round(r_dist, 8), "level": level,
    })
    pos = trader.positions().get(rec.symbol)
    amt = abs(float((pos or {}).get("amt") or 0))
    if amt <= 0:
        row["reason"] = "交易所有仓为空（等待巡检结算）"
        return row
    last = trader.price(rec.symbol)
    mode = "B" if _tp_triggered(last, level, rec.direction) else "A"
    row.update({"mode": mode, "last": last, "qty": amt})
    # 记录数量与持仓偏差 >1%：以交易所持仓为准同步（防挂错量）
    prev_qty = None
    if rec.qty and abs(amt - float(rec.qty)) > float(rec.qty) * 0.01:
        prev_qty = float(rec.qty)
        row["reason"] = f"qty 与持仓偏差（记录 {prev_qty} → 持仓 {amt}），以持仓为准"
    at = datetime.utcnow().isoformat(timespec="seconds")
    if mode == "A":
        _p1, _p2, p1s = _tp_ratio_cfg(cfg)          # 无 TP2 口径 = p1s（默认 80）
        qty_tp1 = trader.round_qty(rec.symbol, amt * p1s / 100)
        f = trader.filters(rec.symbol)
        if qty_tp1 < f["min_qty"]:
            row["reason"] = f"TP1 数量 {qty_tp1} 低于 min_qty {f['min_qty']}"
            return row
        row["qty_tp1"] = qty_tp1
        if not apply:
            row["result"] = "dry-A"
            return row
        bookmark = _bookmark_now(trader, rec)       # 挂单前取书签（防瞬时成交误计）
        order = trader.take_profit_reduce(
            rec.symbol, _close_side(rec.direction), qty_tp1, level)
        order_id = order.get("algoId") or order.get("orderId")
        rec.tp1 = level
        if prev_qty is not None:
            rec.qty = amt
        rec.raw = {
            **raw,
            "tp1_order_id": order_id, "qty_tp1": qty_tp1, "qty_tp2": 0,
            "tp_mode": "ladder", "realized_bookmark": bookmark,
            "trail_convert": {
                "mode": "A", "at": at, "r_dist": round(r_dist, 8),
                "level": level, "qty_tp1": qty_tp1,
                **({"prev_qty": prev_qty} if prev_qty is not None else {}),
            },
        }
        _add_event(db, rec, "CONVERT", {
            "mode": "A", "level": level, "last": last,
            "r_dist": round(r_dist, 8), "qty_tp1": qty_tp1,
            "tp1_order_id": order_id, "r_source": source,
            **({"prev_qty": prev_qty} if prev_qty is not None else {}),
            "note": "存量 trail 单转 ladder：挂 1.5R 止盈（TP1）",
        })
        try:
            db.commit()
        except Exception:
            db.rollback()
            # 落库失败撤销刚挂的止盈单（尽力而为；原 SL 不受影响）
            try:
                trader.cancel_order(rec.symbol, int(order_id))
            except Exception as cancel_err:
                row["reason"] = f"掉库失败且撤 TP 单失败: {cancel_err}"
                row["result"] = "failed"
                return row
            raise
        row["result"] = "converted_A"
        return row
    # ── Case B：置 TP1_HIT（已过 1.5R 进入保本+跟进）+ 立即保本 ──
    if not apply:
        row["result"] = "dry-B"
        return row
    rec.status = "TP1_HIT"
    if prev_qty is not None:
        rec.qty = amt
    rec.raw = {
        **raw, "qty_tp1": 0, "tp_mode": "ladder",
        "realized_bookmark": _bookmark_now(trader, rec),
        "trail_convert": {
            "mode": "B", "at": at, "r_dist": round(r_dist, 8), "level": level,
            **({"prev_qty": prev_qty} if prev_qty is not None else {}),
        },
    }
    _add_event(db, rec, "CONVERT", {
        "mode": "B", "level": level, "last": last, "r_dist": round(r_dist, 8),
        "r_source": source,
        **({"prev_qty": prev_qty} if prev_qty is not None else {}),
        "note": "存量 trail 单转 ladder：已越过 1.5R，进入保本+跟进（不挂止盈）",
    })
    db.commit()
    # 立即保本（幂等；异常不撤转换——状态已落库，巡检下一轮会重试保本）
    moved = None
    try:
        open_ids = trader.open_order_ids(rec.symbol)
        moved = _move_sl_breakeven(db, trader, rec, open_ids, fee_rate)
        db.commit()
    except Exception as e:
        db.rollback()
        row["reason"] = f"保本移动失败（巡检将重试）: {e}"
    row["breakeven"] = moved
    row["result"] = "converted_B"
    return row


def _print_row(row: dict) -> None:
    head = (f"[{row.get('result', '?')}] {row.get('symbol', '?')} "
            f"({row.get('net', '?')}/{row.get('account', '?')})")
    bits = []
    if row.get("mode"):
        bits.append(f"mode={row['mode']}")
    if row.get("entry") is not None:
        bits.append(f"entry={row['entry']}")
    if row.get("sl0") is not None:
        bits.append(f"原始SL={row['sl0']}")
    if row.get("r_dist") is not None:
        bits.append(f"R={row['r_dist']}")
    if row.get("level") is not None:
        bits.append(f"1.5R档={row['level']}")
    if row.get("last") is not None:
        bits.append(f"last={row['last']}")
    if row.get("qty_tp1"):
        bits.append(f"TP1数量={row['qty_tp1']}")
    if row.get("breakeven") is not None:
        bits.append(f"保本移动={row['breakeven']}")
    line = head + ((" " + " ".join(bits)) if bits else "")
    if row.get("reason"):
        line += f"  # {row['reason']}"
    print(line)


def _print_summary(rows: list) -> None:
    from collections import Counter
    cnt = Counter(r.get("result", "?") for r in rows)
    print("汇总：" + "，".join(f"{k}={v}" for k, v in sorted(cnt.items())))


def main() -> None:
    parser = argparse.ArgumentParser(
        description="存量 trail 单转 ladder（策略一）一次性脚本（默认 dry-run）")
    parser.add_argument("--apply", action="store_true",
                        help="真正执行转换（默认只打印计划，不动交易所/数据库）")
    args = parser.parse_args()

    db = SessionLocal()
    try:
        cfg = db.get(SystemConfig, 1)
        fee_rate = (float(cfg.taker_fee_rate)
                    if cfg is not None and cfg.taker_fee_rate is not None else None)
        records = _fetch_candidates(db)
        traders = _build_traders(records)
        print(f"候选（OPENED + tp_mode=trail）：{len(records)} 条"
              f"{'（APPLY 模式）' if args.apply else '（DRY-RUN，不会改动任何东西）'}")
        rows = []
        for rec in records:
            trader = traders[_rec_acc_key(rec)]
            try:
                row = convert_one(db, trader, rec, cfg, fee_rate, args.apply)
            except Exception as e:
                db.rollback()
                row = {"symbol": rec.symbol, "account": _rec_acc_key(rec),
                       "net": "testnet" if rec.testnet else "live",
                       "result": "failed", "reason": str(e)}
            rows.append(row)
            _print_row(row)
        _print_summary(rows)
    finally:
        db.close()


if __name__ == "__main__":
    main()