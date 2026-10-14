# -*- coding: utf-8 -*-
"""双周期扫描套启用开关配置校验单测（2026-10-14 双周期套，docs/22）

规则：两开关相互独立、可同时开启，但**合并后不得两套皆关**（否则扫描链路整体停摆）。
前端已做同款跨字段校验，后端 PUT /config 需兜底拒绝（防直接调 API 绕过）。

直接调用端点函数（update_system_config），sqlite 内存库建表，不触网。
"""
import sys
from pathlib import Path

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import app.models  # noqa: E402,F401
from app.api.scan import update_system_config  # noqa: E402
from app.database import Base  # noqa: E402
from app.models.system_config import SystemConfig  # noqa: E402
from app.schemas.scan import SystemConfigUpdate  # noqa: E402


@pytest.fixture()
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = Session(bind=engine)
    session.add(SystemConfig(id=1, scan_profile_s1_enabled=True, scan_profile_s2_enabled=False))
    session.commit()
    yield session
    session.close()


def _cfg(db):
    return db.get(SystemConfig, 1)


def test_only_one_switch_merges_with_existing(db):
    """只提交 s1 开关时按合并后的生效值判定：s2 仍开 → 合法。"""
    _cfg(db).scan_profile_s2_enabled = True
    db.commit()

    resp = update_system_config(body=SystemConfigUpdate(scan_profile_s1_enabled=False), db=db)
    assert _cfg(db).scan_profile_s1_enabled is False
    assert _cfg(db).scan_profile_s2_enabled is True
    assert resp.scan_profile_s1_enabled is False
    assert resp.scan_profile_s2_enabled is True


def test_both_off_via_single_switch_rejected(db):
    """库内 s2 已关，只提交 s1=False → 合并后两套皆关 → 400，且不落库。"""
    with pytest.raises(HTTPException) as ei:
        update_system_config(body=SystemConfigUpdate(scan_profile_s1_enabled=False), db=db)
    assert ei.value.status_code == 400
    # 校验先于赋值：session 未改动
    assert _cfg(db).scan_profile_s1_enabled is True


def test_both_off_in_one_request_rejected(db):
    """一次请求同时提交两开关皆 False → 400。"""
    with pytest.raises(HTTPException) as ei:
        update_system_config(
            body=SystemConfigUpdate(
                scan_profile_s1_enabled=False, scan_profile_s2_enabled=False,
            ),
            db=db,
        )
    assert ei.value.status_code == 400
    assert _cfg(db).scan_profile_s1_enabled is True
    assert _cfg(db).scan_profile_s2_enabled is False


def test_both_on_accepted(db):
    """两开关同时开启合法。"""
    resp = update_system_config(
        body=SystemConfigUpdate(
            scan_profile_s1_enabled=True, scan_profile_s2_enabled=True,
        ),
        db=db,
    )
    assert _cfg(db).scan_profile_s1_enabled is True
    assert _cfg(db).scan_profile_s2_enabled is True
    assert resp.scan_profile_s1_enabled is True
    assert resp.scan_profile_s2_enabled is True


def test_get_config_exposes_switches(db):
    """GET 口径（_config_response）暴露两开关供前端回填。"""
    from app.api.scan import _config_response

    out = _config_response(_cfg(db))
    assert out.scan_profile_s1_enabled is True
    assert out.scan_profile_s2_enabled is False
