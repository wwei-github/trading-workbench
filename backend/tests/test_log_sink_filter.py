# -*- coding: utf-8 -*-
"""日志落库 sink 的级别口径单测（docs/16 §12 观测缺口）。

口径（`log_sink._AppLoggerFilter`）：
- 非 `app.*`（uvicorn/celery/httpx）一律不落库；
- `app.spot.*` 与 `app.services.scanner`：**INFO 即落库**
  （扫描链路诊断全是 INFO，不放行就等于「扫描过程不可见」）；
- 其余 `app.*`：仅 WARNING 及以上，防止 INFO 刷库。
"""
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.log_sink import _AppLoggerFilter  # noqa: E402


def _rec(name: str, level: int) -> logging.LogRecord:
    return logging.LogRecord(
        name=name, level=level, pathname=__file__, lineno=1,
        msg="x", args=(), exc_info=None,
    )


def test_framework_loggers_never_persisted():
    f = _AppLoggerFilter()
    for name in ("uvicorn.access", "celery.app.trace", "httpx", "sqlalchemy.engine"):
        assert f.filter(_rec(name, logging.WARNING)) is False


def test_generic_app_info_dropped():
    f = _AppLoggerFilter()
    assert f.filter(_rec("app.services.trade_engine", logging.INFO)) is False
    assert f.filter(_rec("app.api.scan", logging.INFO)) is False


def test_generic_app_warning_persisted():
    f = _AppLoggerFilter()
    assert f.filter(_rec("app.services.trade_engine", logging.WARNING)) is True
    assert f.filter(_rec("app.api.scan", logging.ERROR)) is True


def test_spot_info_persisted():
    f = _AppLoggerFilter()
    assert f.filter(_rec("app.spot.tasks", logging.INFO)) is True


def test_scanner_info_persisted():
    """扫描链路 INFO 必须落库：否则「扫描完成/抑制/质量分下限拦截」全丢。"""
    f = _AppLoggerFilter()
    assert f.filter(_rec("app.services.scanner", logging.INFO)) is True
    assert f.filter(_rec("app.services.scanner", logging.WARNING)) is True


def test_scanner_sibling_not_whitelisted():
    """白名单按前缀精确匹配，不能顺带把整个 app.services.* 都放行。"""
    f = _AppLoggerFilter()
    assert f.filter(_rec("app.services.trade_engine", logging.INFO)) is False
