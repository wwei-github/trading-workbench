"""扫描套（scan profile）注册表（2026-10-14 双周期套，docs/22）

把「周期」从全局单例（settings.KLINE_INTERVAL / SystemConfig.kline_interval）提升为
**每套一份**的参数，让两套链路并存：

- s1 = 信号 1h / 背景 4h（现状链路）——默认启用，长期保留
- s2 = 信号 4h / 背景 1d（新增套）——默认关闭，页面开关开启后参与实盘

设计约束：本项目已退役「多策略可插拔 + 影子层」，本注册表**不复活**
strategy_registry / 插件协议——只用轻量常量注册表（Python 常量），形态对应
archive/20「共享底盘 + 双大脑」：信号链各自独立，执行引擎/全局风控/复盘归因共享。

两套**共用同一份筛选阈值**（scan_filters JSONB 的所有键），**仅触点白名单按套内置**
（触点语义与周期强绑定，共用会错配）。s1 的触点白名单走页面配置（`default_touch_refs=None`
→ 用 resolve_scan_filters 解析出的 pullback_touch_refs）；s2 用内置触点集覆盖。

风控 ATR 倍数（2026-10-14-4）同样按套标定：`sl_min_atr`/`tp1_max_atr`/`tp2_min_gap_atr`
三个常数直接乘**本套信号周期**的 ATR14，而 settings 里的默认值是按 1h ATR 校准的——
s2 信号周期 4h 的 ATR ≈1.5~2× 1h ATR，沿用同一倍率会把实际距离放宽 ~2×。故 s2 给出
**1h 等效倍率**（÷2：0.15/1.0/0.25）；s1 全 None = 走 settings 默认，逐字不变。
"""
import logging
from dataclasses import dataclass
from typing import Optional

from app.services.scan_filters import (
    DEFAULT_TOUCH_REFS,
    TOUCH_EMA21,
    TOUCH_EMA21_1D,
    TOUCH_EMA21_4H,
    TOUCH_EMA55,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ScanProfile:
    """一套扫描链路的周期参数（只读常量）。"""

    key: str                    # 套标识："s1" / "s2"
    label: str                  # 展示名："1h·4h" / "4h·1d"
    signal_interval: str        # 扫描/信号周期："1h" / "4h"
    htf_interval: str           # 趋势背景周期："4h" / "1d"
    signal_window: int          # 信号K线根数（两套同 240）
    htf_window: int             # 背景K线根数（根数口径，与周期无关；300 根足够 EMA144 收敛）
    htf_label: str              # 提示词/展示用背景周期名："4h" / "1d"
    htf_touch_ref: str          # 跨周期 EMA21 触点 ref："ema21_4h" / "ema21_1d"
    # 触点半名单：None=用页面配置（s1）；tuple=内置覆盖（s2，页面触点勾选只作用于 s1）
    default_touch_refs: Optional[tuple] = None
    # 风控 ATR 倍数（按套标定，2026-10-14-4）：直接乘本套信号周期 ATR14 的三个常数。
    # None = 走 settings 全局默认（该默认按 1h ATR 校准）；s1 全 None → 行为逐字不变。
    # 周期无关的 R 腿（TP1_MAX_R / TP2_MIN_GAP_R）不在此列。
    sl_min_atr: Optional[float] = None        # 止损宽度下限倍数（settings.SL_MIN_ATR）
    tp1_max_atr: Optional[float] = None       # 止盈一距离上限 ATR 腿（settings.TP1_MAX_ATR）
    tp2_min_gap_atr: Optional[float] = None   # 止盈二间距下限 ATR 腿（settings.TP2_MIN_GAP_ATR）


S1 = ScanProfile(
    key="s1", label="1h·4h", signal_interval="1h", htf_interval="4h",
    signal_window=240, htf_window=300, htf_label="4h", htf_touch_ref=TOUCH_EMA21_4H,
    default_touch_refs=None,  # 走页面配置（缺省即 DEFAULT_TOUCH_REFS）
)
S2 = ScanProfile(
    key="s2", label="4h·1d", signal_interval="4h", htf_interval="1d",
    signal_window=240, htf_window=300, htf_label="1d", htf_touch_ref=TOUCH_EMA21_1D,
    default_touch_refs=(TOUCH_EMA21, TOUCH_EMA21_1D, TOUCH_EMA55),
    # 1h 等效倍率（4h ATR ≈2× 1h ATR → ÷2）：与 settings 默认 0.3/2.0/0.5 同价位距离
    sl_min_atr=0.15, tp1_max_atr=1.0, tp2_min_gap_atr=0.25,
)

SCAN_PROFILES: dict[str, ScanProfile] = {"s1": S1, "s2": S2}
DEFAULT_PROFILE_KEY = "s1"

# 默认触点集（s1 页面未配置时回落；与 scan_filters.DEFAULT_TOUCH_REFS 同源，此处
# 仅为便于前端/文档引用，不改变解析口径）
DEFAULT_PROFILE_TOUCH_REFS = DEFAULT_TOUCH_REFS


def get_profile(key: Optional[str]) -> ScanProfile:
    """按 key 取扫描套；未知/None 回落 s1 并告警（防拼错静默改了周期）。"""
    if key in SCAN_PROFILES:
        return SCAN_PROFILES[key]
    if key:
        logger.warning("未知扫描套 profile_key=%s，回落 %s", key, DEFAULT_PROFILE_KEY)
    return SCAN_PROFILES[DEFAULT_PROFILE_KEY]


def is_valid_profile(key: Optional[str]) -> bool:
    return key in SCAN_PROFILES


def resolve_risk_atr(profile_key: Optional[str]) -> tuple[float, float, float]:
    """按套解析三个 ATR 倍数，返回 (止损宽度下限, 止盈一距离上限, 止盈二间距下限)。

    这三个常数在 risk_guard 与两管线提示词中直接乘 ATR14——即乘的是**本套信号周期**
    的 ATR。profile 未显式标定（None，含 s1 与未知 key）时回落 settings 全局默认
    （按 1h ATR 校准），保证 s1/旧调用路径逐字不变。R 腿（TP1_MAX_R/TP2_MIN_GAP_R）
    是止损距离口径、与周期无关，不经此处。"""
    from app.config import settings  # 局部导入：避免 config ↔ services 顶层循环
    p = get_profile(profile_key)
    return (
        float(settings.SL_MIN_ATR if p.sl_min_atr is None else p.sl_min_atr),
        float(settings.TP1_MAX_ATR if p.tp1_max_atr is None else p.tp1_max_atr),
        float(settings.TP2_MIN_GAP_ATR if p.tp2_min_gap_atr is None else p.tp2_min_gap_atr),
    )
