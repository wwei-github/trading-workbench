"""交易账号注册表（docs/17 P0，2026-09-23）

多账号配置全走 .env（纯配置驱动，无新表），命名统一为 BINANCE_LIVE_{N}_*：
- 测试网单账号：BINANCE_TESTNET_API_KEY/SECRET_KEY → 账号 key `testnet-1`
- 正式网账号：BINANCE_LIVE_{N}_API_KEY/SECRET_KEY（+可选 _NAME）→ `live-N`，N≥1 不限数量
- 向后兼容：live-1 未配置 BINANCE_LIVE_1_* 时回落旧变量 BINANCE_API_KEY/SECRET_KEY
  （存量部署零改动；两处都配时 LIVE_1 优先）

账号 key 是稳定标识（trade_records.account_key 记录归属）；NAME 仅展示用。
启动时（uvicorn lifespan 与 celery worker 各自进程内）构建注册表并校验，
配了 _API_KEY 缺 _SECRET_KEY 直接抛错，防止半配置账号上线。
"""
import logging
import os
import re
from dataclasses import dataclass
from typing import Optional

from app.config import settings

logger = logging.getLogger(__name__)

_TESTNET_KEY = "testnet-1"
_LIVE_N_RE = re.compile(r"^BINANCE_LIVE_(\d+)_API_KEY$")


@dataclass(frozen=True)
class Account:
    """一个交易账号（一组币安 key 的逻辑封装）。testnet 是账号属性而非环境开关。"""

    key: str          # 稳定标识：testnet-1 / live-1 / live-2…
    name: str         # 显示名（.env NAME 或默认「正式网-N」/「测试网」）
    testnet: bool     # 账号属性：下单 base 与记录归属
    api_key: str
    api_secret: str

    @property
    def base_url(self) -> str:
        from app.services.binance_trader import _PROD_BASE, _TEST_BASE
        return _TEST_BASE if self.testnet else _PROD_BASE


def _validate(key: str, api_key: str, secret: str) -> None:
    if api_key and not secret:
        raise RuntimeError(
            f"账号 {key} 配置不完整：检测到 {api_key} 对应的 API_KEY 但缺少 SECRET_KEY，"
            f"请在 .env 补全后重启"
        )


def build_registry() -> list[Account]:
    """扫环境变量构建账号清单（每次调用重新扫描，进程内启动时调一次）。"""
    accounts: list[Account] = []

    tk, ts = settings.BINANCE_TESTNET_API_KEY, settings.BINANCE_TESTNET_SECRET_KEY
    if tk or ts:
        _validate(_TESTNET_KEY, tk, ts)
        if tk and ts:
            accounts.append(Account(_TESTNET_KEY, "测试网", True, tk, ts))

    # 正式网账号：BINANCE_LIVE_{N}_*（N≥1），按编号排序保证注册表顺序稳定。
    # live-1 未配置扩展写法时回落旧变量 BINANCE_API_KEY/SECRET_KEY（向后兼容）
    found: list[tuple[int, str, str, Optional[str]]] = []
    for env_name, value in os.environ.items():
        m = _LIVE_N_RE.match(env_name)
        if m and value.strip():
            n = int(m.group(1))
            found.append((n, value.strip(), os.getenv(f"BINANCE_LIVE_{n}_SECRET_KEY", ""),
                          os.getenv(f"BINANCE_LIVE_{n}_NAME")))
    if not any(n == 1 for n, _, _, _ in found):
        lk, ls = settings.BINANCE_API_KEY, settings.BINANCE_SECRET_KEY
        if lk or ls:
            found.append((1, lk, ls, None))
    for n, ak, sec, name in sorted(found):
        key = f"live-{n}"
        _validate(key, ak, sec)
        if sec:
            accounts.append(Account(key, name or f"正式网-{n}", False, ak, sec))

    return accounts


# ── 进程内注册表（惰性构建；uvicorn / celery worker 各自进程一份）──────────
_registry: Optional[list[Account]] = None


def registry() -> list[Account]:
    global _registry
    if _registry is None:
        _registry = build_registry()
        names = ", ".join(f"{a.key}({a.name})" for a in _registry) or "无"
        logger.info("账号注册表构建完成：%s", names)
    return _registry


def get_account(account_key: str) -> Optional[Account]:
    return next((a for a in registry() if a.key == account_key), None)


def live_accounts() -> list[Account]:
    return [a for a in registry() if not a.testnet]


def testnet_accounts() -> list[Account]:
    return [a for a in registry() if a.testnet]


def parse_live_keys(raw: Optional[str]) -> list[str]:
    """逗号分隔的启用账号配置串 → 去重后的 key 列表（保持顺序）"""
    if not raw:
        return []
    out: list[str] = []
    for k in raw.split(","):
        k = k.strip()
        if k and k not in out:
            out.append(k)
    return out


def live_enabled_accounts(db) -> list["Account"]:
    """正式网启用账号：live_trading_keys ∩ 注册表（保持配置顺序），空/未配置
    回落 live-1（P0 单账号语义）。

    2026-10-03 删环境切换后语义不变：正式网自动交易开关（trading_enabled_live）
    开启时由 auto_trade_accounts 按此清单扇出；手动单接管/巡检走
    managed_accounts（含测试网）。"""
    from app.models.system_config import SystemConfig

    cfg = db.get(SystemConfig, 1)
    accs: list[Account] = []
    for k in parse_live_keys(cfg.live_trading_keys if cfg else None):
        a = get_account(k)
        if a and not a.testnet and a not in accs:
            accs.append(a)
    return accs or live_accounts()


def managed_accounts(db) -> list["Account"]:
    """双网管理清单（2026-10-03 删环境切换）：测试网账号 + 正式网启用账号，
    去重保持顺序。手动单接管/导入持仓/结算巡检等「管理行为」一律覆盖两网
    ——账号是否存在手动单由交易所持仓查询决定，管理范围不随任何开关变化
    （正式网启用账号见 live_enabled_accounts；测试网恒 testnet-1 单账号）。"""
    accs: list["Account"] = []
    for a in testnet_accounts() + live_enabled_accounts(db):
        if a not in accs:
            accs.append(a)
    return accs


def auto_trade_accounts(db) -> list["Account"]:
    """双网自动交易扇出清单（2026-10-03 总开关拆分，用户拍板：两网可同时
    在 AI 分析后自动交易）：
    - trading_enabled_testnet（默认开）→ testnet_accounts
    - trading_enabled_live（默认关，实盘安全基线）→ live_enabled_accounts
    各网独立判定、互不回落——测试网开关开但无测试网账号时直接跳过该网，
    不落回正式网账号（防开关语义交叉误开实盘）。"""
    from app.models.system_config import SystemConfig

    cfg = db.get(SystemConfig, 1)
    if cfg is None:
        return []
    accs: list[Account] = []
    if cfg.trading_enabled_testnet:
        accs.extend(a for a in testnet_accounts() if a not in accs)
    if cfg.trading_enabled_live:
        accs.extend(a for a in live_enabled_accounts(db) if a not in accs)
    return accs


# ── 现货（docs/18）：环境与合约完全独立，仅共用 .env 账号注册表 ─────────────

def spot_enabled_accounts(db) -> list["Account"]:
    """现货当前环境的账号清单：
    - 现货测试网（spot_trading_testnet=true，默认）→ testnet-1 单账号
    - 现货正式网 → spot_live_trading_keys ∩ 注册表（保持配置顺序），
      空回落 live-1；与合约的 live_trading_keys 互不影响
    """
    from app.models.system_config import SystemConfig

    cfg = db.get(SystemConfig, 1)
    if cfg is not None and cfg.spot_trading_testnet:
        accs = testnet_accounts() or live_accounts() or testnet_accounts()
        return _with_spot_testnet_keys(accs)
    accs: list[Account] = []
    for k in parse_live_keys(cfg.spot_live_trading_keys if cfg else None):
        a = get_account(k)
        if a and not a.testnet and a not in accs:
            accs.append(a)
    return accs or live_accounts() or testnet_accounts()


def _with_spot_testnet_keys(accs: list["Account"]) -> list["Account"]:
    """现货测试网账号的 key 覆盖（可选 BINANCE_SPOT_TESTNET_*）。

    现货测试网（testnet.binance.vision）与合约测试网（testnet.binancefuture.com）
    是两套独立系统，key 不互通（2026-09-24 实证：同一把 key 合约测试网 200 /
    现货测试网 401，双出口 IP 一致，排除 IP 绑定）。配置了现货专用 key 时，
    以 replace 生成 testnet 账号副本仅替换凭据（key/name/testnet 与归属口径不变，
    且不污染注册表单例）；未配置回落 BINANCE_TESTNET_* 保持旧行为。
    """
    import dataclasses

    sk = settings.BINANCE_SPOT_TESTNET_API_KEY
    ss = settings.BINANCE_SPOT_TESTNET_SECRET_KEY
    if not (sk or ss):
        return accs
    _validate("spot-testnet-1", sk, ss)
    if not (sk and ss):
        return accs
    return [dataclasses.replace(a, api_key=sk, api_secret=ss) if a.testnet else a for a in accs]


def resolve_spot_account(db) -> "Account":
    """解析现货当前账号（P0 只读查询用：取启用清单第一个）。
    失败语义与 spot_enabled_accounts 一致：任何配置下都有可用账号。"""
    accs = spot_enabled_accounts(db)
    return accs[0]
