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


def test_seed_insert_referenced_columns_added_before_insert():
    """迁移列序不变量（2026-10-14 线上部署事故回归）

    _run_migrations 的 system_config 种子 INSERT 引用了新列；若某新列的
    `ADD COLUMN IF NOT EXISTS` 出现在该 INSERT 之后，存量库（create_all 对既有
    表不建列）执行到 INSERT 时列尚不存在 → PG 解析期即报 does not exist →
    迁移事务整体回滚 → init_db() 抛异常 → 后端启动崩溃、探活失败。
    本测试静态校验「被 INSERT 引用的 ADD COLUMN 必须位于 INSERT 之前」。
    """
    import inspect
    import re

    from app import database

    src = inspect.getsource(database._run_migrations)
    insert_pos = src.index("INSERT INTO system_config")
    col_block = src[insert_pos:src.index("VALUES", insert_pos)]
    insert_cols = set(re.findall(r"[a-z_][a-z0-9_]*", col_block))
    for m in re.finditer(r"ADD COLUMN IF NOT EXISTS (\w+)", src):
        if m.group(1) in insert_cols:
            assert m.start() < insert_pos, (
                f"{m.group(1)} 被种子 INSERT 引用，但其 ADD COLUMN 位于 INSERT 之后"
                "——存量库会因该列不存在导致迁移失败（须移到 INSERT 之前）"
            )
