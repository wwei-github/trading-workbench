"""统一决策版本（审计 7.1，第二阶段任务五，2026-10-21）

补丁合入、系统配置变更、提示词变更统一递增 system_config.decision_version，
与变更本身在同一事务内提交。AI 决策快照、交易快照与缓存指纹记录该版本，
复盘按版本分组——避免「配置摘要相同、规则来源不同」的样本混算。

版本只增不减（回滚同样递增），用于标记决策规则的变化点，不代表规则内容
的哈希；规则内容仍由缓存指纹中的配置摘要与代码侧 RULE_VERSION 共同表达。
"""
from datetime import datetime

from app.models.system_config import SystemConfig


def bump_decision_version(cfg: SystemConfig) -> int:
    """在当前事务内递增版本并刷新时刻（调用方负责 commit）。"""
    cfg.decision_version = int(cfg.decision_version or 0) + 1
    cfg.decision_version_at = datetime.utcnow()
    return cfg.decision_version


def current_decision_version(db) -> int:
    """读取当前生效版本；缺配置行按 0（本列上线前的原始版本）处理。"""
    cfg = db.get(SystemConfig, 1)
    return int(cfg.decision_version or 0) if cfg else 0
