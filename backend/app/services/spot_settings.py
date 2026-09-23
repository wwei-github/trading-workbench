"""现货屯币配置读写（docs/18 §5 / §7.1 spot_settings JSON 列）

零侵入约束：只读写 system_config.spot_settings 这一**新增独立列**，
不触碰既有列。NULL / 字段缺失 → SpotDefaults 兜底。
"""
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy.orm import Session

from app.models.system_config import SystemConfig
from app.services.spot_engine import SpotDefaults

# SpotDefaults 字段 → (类型, 合法范围)，PUT 校验用
_FIELD_SPECS = {
    "per_order_usdt": (float, (10.0, 1000.0)),
    "max_total_usdt": (float, (100.0, 100000.0)),
    "dca_enabled": (bool, None),
    "dca_amount": (float, (0.0, 1000.0)),
    "usdt_reserve_pct": (float, (0.0, 0.9)),
    "fee_rate": (float, (0.0, 0.01)),
}


def load_spot_settings(db: Session) -> Dict[str, Any]:
    """读配置并补全默认值（返回完整 dict，供 API/任务直接用）。"""
    cfg = db.get(SystemConfig, 1)
    raw: Dict[str, Any] = dict(cfg.spot_settings or {}) if cfg else {}
    d = SpotDefaults()
    out: Dict[str, Any] = {
        "per_order_usdt": raw.get("per_order_usdt", d.per_order_usdt),
        "max_total_usdt": raw.get("max_total_usdt", d.max_total_usdt),
        "dca_enabled": raw.get("dca_enabled", d.dca_enabled),
        "dca_amount": raw.get("dca_amount", d.dca_amount),
        "usdt_reserve_pct": raw.get("usdt_reserve_pct", d.usdt_reserve_pct),
        "fee_rate": raw.get("fee_rate", d.fee_rate),
    }
    return out


def merge_spot_settings(
    current: Dict[str, Any], patch: Dict[str, Any]
) -> Tuple[Dict[str, Any], List[str]]:
    """PUT 部分更新：仅接受合法字段与范围，非法项拒绝。

    返回 (merged, rejected_keys)。
    """
    merged = dict(current)
    rejected: List[str] = []
    for k, v in (patch or {}).items():
        spec = _FIELD_SPECS.get(k)
        if spec is None:
            rejected.append(k)
            continue
        typ, rng = spec
        try:
            if typ is bool:
                if not isinstance(v, bool):
                    rejected.append(k)
                    continue
                merged[k] = v
            else:
                fv = float(v)
                if rng and not (rng[0] <= fv <= rng[1]):
                    rejected.append(k)
                    continue
                merged[k] = fv
        except (TypeError, ValueError):
            rejected.append(k)
    return merged, rejected
