"""战法匹配服务（2026-10-02）：活动战法装载 + 提示词块渲染 + 白名单。

战法仅作借鉴参考：分析任务先把活动战法渲染成【战法库判定】块传给
ai_analyzer 注入用户消息，AI 判定命中后输出 tactic_name/tactic_reason，
risk_guard 按白名单闸门防幻觉（不在本次注入清单内的名字直接清空）。

DB 访问只在装载时短暂持有会话（LLM 长调用不持连接，与 ai_tasks 惯例一致）。
"""
import logging
from typing import Optional

from sqlalchemy import select

from app.database import SessionLocal
from app.models.tactic import Tactic

logger = logging.getLogger(__name__)


def load_active_tactics(testnet: Optional[bool]) -> list[dict]:
    """按环境装载活动战法（enabled=true 且 scope 匹配），返回轻量 dict 列表。

    testnet=None = 不区分环境，装载全部活动战法（2026-10-03 删环境切换：
    同一次 AI 分析会双网各开一单，战法注入取两网 scope 并集，输出侧由
    白名单闸门兜底）。装载失败返回空列表——调用方按无战法库降级。
    """
    db = SessionLocal()
    try:
        q = select(Tactic).where(Tactic.enabled.is_(True))
        if testnet is not None:
            scope = "testnet" if testnet else "live"
            q = q.where(Tactic.scope.in_(("both", scope)))
        rows = db.execute(q.order_by(Tactic.created_at.asc())).scalars().all()
        return [{"name": t.name, "description": t.description} for t in rows]
    except Exception as e:
        logger.warning("活动战法装载失败（本次分析不注入战法块）: %s", e)
        return []
    finally:
        db.close()


def render_tactics_block(tactics: list[dict]) -> str:
    """渲染战法库判定块（进用户消息；空列表返回空串不注入）"""
    if not tactics:
        return ""
    lines = [
        "【战法库判定】（程序维护的用户战法清单，仅作借鉴参考，不影响是否开单的结论；"
        "逐条对照当前信号，最多命中 1 个最符合者）"
    ]
    for i, t in enumerate(tactics, 1):
        lines.append(f"{i}. {t['name']}：{t['description']}")
    lines.append(
        "若当前信号符合其中某个战法，输出 tactic_name（必须逐字取自上清单，"
        "不要改写）与 tactic_reason（≤60字判定依据，引用战法条件对照结论）；"
        "均不符合或上方无战法清单时，两字段均输出空字符串。"
    )
    return "\n".join(lines) + "\n"


def allowed_names(tactics: list[dict]) -> list[str]:
    """本次注入的战法名白名单（防幻觉闸门数据源）"""
    return [t["name"] for t in tactics]
