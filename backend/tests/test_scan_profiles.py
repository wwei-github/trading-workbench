# -*- coding: utf-8 -*-
"""扫描套（scan profile）注册表单测（2026-10-14 双周期套，docs/22）

覆盖：
- 注册表键集与两套周期/窗口定义（s1=信号1h/背景4h 现状链路；s2=信号4h/背景1d 新增套）
- 触点白名单按套内置：s1=None（走页面配置）、s2=内置（ema21/ema21_1d/ema55）
- get_profile 未知/None 回落 s1（防拼错静默改周期）；is_valid_profile 白名单
- /scans/latest/results 支持 profile_key（切套兜底拉取不串套）
"""
import sys
from datetime import datetime, timedelta
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


# ===== /scans/latest/results 按套隔离：切套兜底拉取不得串套 =====

@pytest.fixture()
def db():
    """sqlite 内存库（仅建表），供 scan.py API 函数直调。"""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    import app.models  # noqa: E402,F401
    from app.database import Base  # noqa: E402

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()


def test_latest_results_scoped_by_profile(db, monkeypatch):
    """latest/results 传 profile_key 时只取该套最近一轮；缺该套轮次即空。

    前端切套后 currentScanId 为空走该接口兜底，不过滤会把另一套的最近
    一轮当成当前套显示（用户反馈「切套没有显示空列表」）。
    """
    import app.api.scan as scan_api
    from app.models.scan import ScanRecord
    from app.schemas.scan import ScanResultListResponse

    t0 = datetime.utcnow()
    s1_rec = ScanRecord(
        scan_type="scheduled", status="completed",
        profile_key="s1", finished_at=t0 - timedelta(hours=2),
    )
    s2_rec = ScanRecord(
        scan_type="scheduled", status="completed",
        profile_key="s2", finished_at=t0 - timedelta(hours=1),
    )
    db.add_all([s1_rec, s2_rec])
    db.commit()

    # 记下 _get_results 实际收到的 scan_id（不构造 ScanResult 行）
    seen: list = []
    monkeypatch.setattr(
        scan_api, "_get_results",
        lambda scan_id, *a, **kw: (
            seen.append(str(scan_id)), ScanResultListResponse(items=[], total=0)
        )[1],
    )

    scan_api.latest_scan_results(profile_key="s1", db=db)
    assert seen == [str(s1_rec.id)]

    seen.clear()
    scan_api.latest_scan_results(profile_key="s2", db=db)
    assert seen == [str(s2_rec.id)]

    # 不带 profile_key（FastAPI 解析缺省为 None，向后兼容）：仍取全局最近一轮
    seen.clear()
    scan_api.latest_scan_results(profile_key=None, db=db)
    assert seen == [str(s2_rec.id)]
