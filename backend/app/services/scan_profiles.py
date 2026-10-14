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


S1 = ScanProfile(
    key="s1", label="1h·4h", signal_interval="1h", htf_interval="4h",
    signal_window=240, htf_window=300, htf_label="4h", htf_touch_ref=TOUCH_EMA21_4H,
    default_touch_refs=None,  # 走页面配置（缺省即 DEFAULT_TOUCH_REFS）
)
S2 = ScanProfile(
    key="s2", label="4h·1d", signal_interval="4h", htf_interval="1d",
    signal_window=240, htf_window=300, htf_label="1d", htf_touch_ref=TOUCH_EMA21_1D,
    default_touch_refs=(TOUCH_EMA21, TOUCH_EMA21_1D, TOUCH_EMA55),
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
