# -*- coding: utf-8 -*-
"""扫描套（scan profile）注册表单测（2026-10-14 双周期套，docs/22）

覆盖：
- 注册表键集与两套周期/窗口定义（s1=信号1h/背景4h 现状链路；s2=信号4h/背景1d 新增套）
- 触点白名单按套内置：s1=None（走页面配置）、s2=内置（ema21/ema21_1d/ema55）
- get_profile 未知/None 回落 s1（防拼错静默改周期）；is_valid_profile 白名单
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services import scan_profiles as sp  # noqa: E402
from app.services.scan_filters import (  # noqa: E402
    DEFAULT_TOUCH_REFS,
    TOUCH_EMA21,
    TOUCH_EMA21_1D,
    TOUCH_EMA21_4H,
    TOUCH_EMA55,
)


def test_registry_keys():
    assert set(sp.SCAN_PROFILES) == {"s1", "s2"}
    assert sp.DEFAULT_PROFILE_KEY == "s1"
    assert sp.SCAN_PROFILES["s1"] is sp.S1
    assert sp.SCAN_PROFILES["s2"] is sp.S2


def test_s1_defines_legacy_chain():
    """s1 = 现状链路，逐字保持（默认 profile 零行为变化的前提）。"""
    assert sp.S1.key == "s1"
    assert sp.S1.label == "1h·4h"
    assert sp.S1.signal_interval == "1h"
    assert sp.S1.htf_interval == "4h"
    assert sp.S1.signal_window == 240
    assert sp.S1.htf_window == 300
    assert sp.S1.htf_label == "4h"
    assert sp.S1.htf_touch_ref == TOUCH_EMA21_4H
    # s1 触点白名单走页面配置（不内置覆盖 → 用 resolve_scan_filters 结果）
    assert sp.S1.default_touch_refs is None


def test_s2_defines_dual_chain():
    """s2 = 新增 4h·1d 套：信号 4h / 背景 1d，触点内置。"""
    assert sp.S2.key == "s2"
    assert sp.S2.label == "4h·1d"
    assert sp.S2.signal_interval == "4h"
    assert sp.S2.htf_interval == "1d"
    assert sp.S2.signal_window == 240
    assert sp.S2.htf_window == 300
    assert sp.S2.htf_label == "1d"
    assert sp.S2.htf_touch_ref == TOUCH_EMA21_1D
    assert sp.S2.default_touch_refs == (TOUCH_EMA21, TOUCH_EMA21_1D, TOUCH_EMA55)


def test_default_profile_touch_refs_alias():
    """DEFAULT_PROFILE_TOUCH_REFS 与 scan_filters.DEFAULT_TOUCH_REFS 同源。"""
    assert sp.DEFAULT_PROFILE_TOUCH_REFS == DEFAULT_TOUCH_REFS


def test_get_profile_known():
    assert sp.get_profile("s1") is sp.S1
    assert sp.get_profile("s2") is sp.S2


@pytest.mark.parametrize("bad", [None, "", "s3", "S1", "  s1  "])
def test_get_profile_unknown_falls_back_s1(bad):
    assert sp.get_profile(bad) is sp.S1


def test_is_valid_profile():
    assert sp.is_valid_profile("s1")
    assert sp.is_valid_profile("s2")
    assert not sp.is_valid_profile("s3")
    assert not sp.is_valid_profile(None)
    assert not sp.is_valid_profile("")
