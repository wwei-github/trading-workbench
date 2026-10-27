# -*- coding: utf-8 -*-
"""批量 AI 分析前的两道过滤单测（docs/16 §5.2 P0-2 / §5.3 P0-3）。

`_filter_ai_targets`：
1) 不可交易合约——两网 exchangeInfo 都不含该币才剔除；单环境失败不影响另一环境；
   两网都拉取失败（空集合）时放行不过滤。
2) 单币冷却——当日 skip 次数达上限且推荐分均值低于阈值才剔除；阈值 0 关闭；
   查询异常时放行。
不连库：用假 Session 返回聚合行；交易所用替身替换 BinanceTrader。
"""
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import settings  # noqa: E402
from app.tasks.ai_tasks import _filter_ai_targets  # noqa: E402
from app.services import binance_trader as bt_mod  # noqa: E402


def _rows(*symbols):
    return [SimpleNamespace(id=f"id-{s}", symbol=s) for s in symbols]


class _Result:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows


class _FakeDB:
    """只实现 execute(...).all()：返回给定的聚合行 [(symbol, skip_n, avg_rec)]"""

    def __init__(self, agg_rows=None, boom=False):
        self.agg_rows = agg_rows or []
        self.boom = boom
        self.calls = 0

    def execute(self, _stmt):
        self.calls += 1
        if self.boom:
            raise RuntimeError("db down")
        return _Result(self.agg_rows)


class _FakeTrader:
    live: set = set()
    test: set = set()
    boom: set = set()

    def __init__(self, testnet=False):
        self.testnet = testnet

    def listed_symbols(self):
        if self.testnet in self.boom:
            raise RuntimeError("exchangeInfo down")
        return self.test if self.testnet else self.live


def _patch_trader(monkeypatch, live, test, boom=()):
    _FakeTrader.live, _FakeTrader.test, _FakeTrader.boom = live, test, set(boom)
    monkeypatch.setattr(bt_mod, "BinanceTrader", _FakeTrader)


# ── P0-2 不可交易合约 ───────────────────────────────────────────────────────


def test_unlisted_dropped_when_absent_from_both_envs(monkeypatch):
    _patch_trader(monkeypatch, {"AUSDT"}, {"BUSDT"})
    kept, unlisted, cooled = _filter_ai_targets(
        _FakeDB(), _rows("AUSDT", "BUSDT", "GHOSTUSDT"))
    assert [r.symbol for r in kept] == ["AUSDT", "BUSDT"]
    assert unlisted == 1 and cooled == 0


def test_listed_on_testnet_only_is_kept(monkeypatch):
    """测试网有上架就保留（用户可能手动在测试网开）"""
    _patch_trader(monkeypatch, {"AUSDT"}, {"BUSDT"})
    kept, unlisted, _ = _filter_ai_targets(_FakeDB(), _rows("BUSDT"))
    assert [r.symbol for r in kept] == ["BUSDT"] and unlisted == 0


def test_both_envs_fail_open(monkeypatch):
    """两网 exchangeInfo 都失败（空集合）→ 不过滤，不阻断扫描链"""
    _patch_trader(monkeypatch, set(), set(), boom=(False, True))
    kept, unlisted, _ = _filter_ai_targets(_FakeDB(), _rows("AUSDT", "BUSDT"))
    assert len(kept) == 2 and unlisted == 0


def test_one_env_down_still_filters_with_other(monkeypatch):
    """单环境失败时用另一环境判定（不因一个环境抖动漏掉全量过滤）"""
    _patch_trader(monkeypatch, {"AUSDT"}, set(), boom=(True,))
    kept, unlisted, _ = _filter_ai_targets(_FakeDB(), _rows("AUSDT", "GHOSTUSDT"))
    assert [r.symbol for r in kept] == ["AUSDT"] and unlisted == 1


# ── P0-3 单币冷却 ───────────────────────────────────────────────────────────


def test_cooldown_drops_repeat_skipped_symbol(monkeypatch):
    _patch_trader(monkeypatch, {"MAGMAUSDT", "NEWUSDT"}, {"MAGMAUSDT", "NEWUSDT"})
    monkeypatch.setattr(settings, "AI_REANALYZE_SKIP_LIMIT", 2)
    monkeypatch.setattr(settings, "AI_REANALYZE_MIN_REC", 50.0)
    db = _FakeDB(agg_rows=[("MAGMAUSDT", 5, 34.0), ("NEWUSDT", 0, None)])
    kept, unlisted, cooled = _filter_ai_targets(db, _rows("MAGMAUSDT", "NEWUSDT"))
    assert [r.symbol for r in kept] == ["NEWUSDT"]
    assert cooled == 1 and unlisted == 0


def test_cooldown_keeps_high_average_rec(monkeypatch):
    """skip 次数够但均分高（说明信号本身不差）→ 不冷却"""
    _patch_trader(monkeypatch, {"XUSDT"}, {"XUSDT"})
    monkeypatch.setattr(settings, "AI_REANALYZE_SKIP_LIMIT", 2)
    monkeypatch.setattr(settings, "AI_REANALYZE_MIN_REC", 50.0)
    db = _FakeDB(agg_rows=[("XUSDT", 3, 62.0)])
    kept, _, cooled = _filter_ai_targets(db, _rows("XUSDT"))
    assert len(kept) == 1 and cooled == 0


def test_cooldown_keeps_below_skip_limit(monkeypatch):
    """skip 次数未达上限 → 不冷却（阈值是"当日已反复被拒"）"""
    _patch_trader(monkeypatch, {"XUSDT"}, {"XUSDT"})
    monkeypatch.setattr(settings, "AI_REANALYZE_SKIP_LIMIT", 3)
    monkeypatch.setattr(settings, "AI_REANALYZE_MIN_REC", 50.0)
    db = _FakeDB(agg_rows=[("XUSDT", 2, 10.0)])
    kept, _, cooled = _filter_ai_targets(db, _rows("XUSDT"))
    assert len(kept) == 1 and cooled == 0


def test_cooldown_disabled_by_zero_limit(monkeypatch):
    """阈值 0 = 关闭：不查库、不过滤"""
    _patch_trader(monkeypatch, {"XUSDT"}, {"XUSDT"})
    monkeypatch.setattr(settings, "AI_REANALYZE_SKIP_LIMIT", 0)
    db = _FakeDB(boom=True)  # 若仍查库会抛错
    kept, _, cooled = _filter_ai_targets(db, _rows("XUSDT"))
    assert len(kept) == 1 and cooled == 0 and db.calls == 0


def test_cooldown_query_failure_fails_open(monkeypatch):
    """冷却查询异常 → 放行（不吞分析）"""
    _patch_trader(monkeypatch, {"XUSDT"}, {"XUSDT"})
    monkeypatch.setattr(settings, "AI_REANALYZE_SKIP_LIMIT", 2)
    monkeypatch.setattr(settings, "AI_REANALYZE_MIN_REC", 50.0)
    kept, _, cooled = _filter_ai_targets(_FakeDB(boom=True), _rows("XUSDT"))
    assert len(kept) == 1 and cooled == 0


def test_both_filters_combined(monkeypatch):
    """两道过滤同时生效：未上架优先剔除，剩余再按冷却剔"""
    _patch_trader(monkeypatch, {"GOODUSDT", "MAGMAUSDT"}, {"GOODUSDT", "MAGMAUSDT"})
    monkeypatch.setattr(settings, "AI_REANALYZE_SKIP_LIMIT", 2)
    monkeypatch.setattr(settings, "AI_REANALYZE_MIN_REC", 50.0)
    db = _FakeDB(agg_rows=[("MAGMAUSDT", 9, 30.0)])
    kept, unlisted, cooled = _filter_ai_targets(
        db, _rows("GOODUSDT", "MAGMAUSDT", "GHOSTUSDT"))
    assert [r.symbol for r in kept] == ["GOODUSDT"]
    assert unlisted == 1 and cooled == 1
