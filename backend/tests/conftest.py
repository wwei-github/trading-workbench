# -*- coding: utf-8 -*-
"""全局夹具：单测封闭性基线（不触网）。

市场环境注入（docs/24 A4①）在 _build_messages 内部拉取资金费率与 BTC K 线，
若不拦截会让既有用例隐式触网（结果随外网波动）。默认对全部用例关闭
MARKET_CONTEXT_ENABLED；市场环境相关用例自行 monkeypatch 显式开启并 mock 数据源。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest  # noqa: E402

from app.config import settings  # noqa: E402


@pytest.fixture(autouse=True)
def _no_market_context(monkeypatch):
    """默认关闭市场环境注入：单测不触网；相关用例自行显式开启"""
    monkeypatch.setattr(settings, "MARKET_CONTEXT_ENABLED", False)


@pytest.fixture(autouse=True)
def _no_exchange_info(monkeypatch):
    """默认不触网：exchangeInfo 派生集合（listed_symbols / onboard_dates）返回空。

    两者在实现里都是「空即放行」（拉取失败不阻断扫描/开单），空集合等价于关闭
    这两道过滤，单测保持封闭性。2026-10-27 起 AI 分发前新增「不可交易合约」过滤
    （docs/16 §5.2 P0-2），若不拦截会让既有用例隐式触网 binance exchangeInfo。
    需要真实上架集合的用例自行 monkeypatch 覆盖（见 test_ai_dispatch_filters.py）。
    """
    from app.services import binance_trader as _bt

    monkeypatch.setattr(_bt.BinanceTrader, "listed_symbols", lambda self: set())
    monkeypatch.setattr(_bt.BinanceTrader, "onboard_dates", lambda self: {})


@pytest.fixture(autouse=True)
def _no_ai_dispatch(monkeypatch):
    """默认关闭 AI 诊断派发：本地 .env 若配置 AI_API_KEY，refresh_mark_pnl 会真实
    调用 run_position_advice.delay → Celery 连 Redis（Docker 主机名在本机可能被
    代理解析成不可达地址），用例表现为卡死数分钟。需要派发的用例自行显式开启
    并桩掉 .delay。"""
    monkeypatch.setattr(settings, "AI_API_KEY", "")
    # 运行时凭证覆盖一并复位（2026-10-26 Key 上页面）：上一用例 configure_from
    # 写入页面 Key 后，本用例 current_api_key() 会读残留覆盖、绕过上面的 env 关闭
    from app.services import llm_client

    monkeypatch.setattr(llm_client, "_runtime", {})
    monkeypatch.setattr(llm_client, "_client_key", ())
