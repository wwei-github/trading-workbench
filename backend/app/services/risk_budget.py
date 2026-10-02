# -*- coding: utf-8 -*-
"""单笔固定亏损预算统一解析（2026-09-27 止损金额模式）。

两管线（P0 ai_analyzer / P1 ai_agent）与开仓引擎（trade_engine）共用同一
system_config 口径，防止提示词/校验/下单三处预算分叉：
- pct（默认）：风险基数（钱包首位取整，见 trade_engine._capital_base）×
  RISK_BUDGET_PCT%（3%）——「风险预算占基数百分比」口径
- fixed：risk_fixed_amount 固定 USDT 实额——「预算金额」口径

调用方拿到的元组语义统一：mode=pct 时 amount 是「占基数百分比」数字
（3.0），mode=fixed 时 amount 是 USDT 实额（3.0）。数值恰好同形（都叫
risk_pct 传进提示词槽位、都叫 risk_budget 传进校验），展示文案由 label 区分。
"""
from __future__ import annotations

from typing import Optional

from app.config import settings


def resolve_risk_budget(cfg=None, wallet: Optional[float] = None) -> tuple[str, float, str]:
    """解析止损金额口径。

    cfg: SystemConfig 行（None 时按默认 pct 模式）；wallet: 当前钱包余额
    （仅 pct 模式展示基数用，可 None）。返回 (mode, amount, label)：
    - mode="pct"：amount=RISK_BUDGET_PCT，label 如 "账户风险基数 × 3%"
    - mode="fixed"：amount=risk_fixed_amount，label 如 "固定 3 USDT"
    """
    mode = (getattr(cfg, "risk_budget_mode", None) or "pct") if cfg is not None else "pct"
    if mode == "fixed":
        raw = getattr(cfg, "risk_fixed_amount", None) if cfg is not None else None
        amount = float(raw) if raw is not None else 3.0
        return "fixed", amount, f"固定 {amount:g} USDT"
    # pct 模式比例（2026-10-02 上页面）：system_config.risk_pct 可页面覆盖，NULL=env 默认
    raw_pct = getattr(cfg, "risk_pct", None) if cfg is not None else None
    pct = float(raw_pct) if raw_pct is not None else float(settings.RISK_BUDGET_PCT)
    return "pct", pct, (
        f"账户风险基数 × {pct:g}%"
        + (f"（当前基数 {wallet:g} USDT 口径）" if wallet else "")
    )
