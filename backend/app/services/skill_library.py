"""技能库（docs/04 §6 Skills 机制）：可插拔领域能力包的索引/加载/命中标注

- backend/skills/<name>/SKILL.md，frontmatter 手写解析（name/description/use_when/version）
- 索引段随 Agent 系统提示词渲染，逐条标注布尔事实命中状态
- load_skill 工具按需加载正文（超长截断），渐进式披露控制 token
"""
import logging
import re
from pathlib import Path

logger = logging.getLogger(__name__)

SKILLS_DIR = Path(__file__).resolve().parent.parent.parent / "skills"
_BODY_MAX_CHARS = 8000  # 正文截断上限（约 4k token）

# use_when 表达式允许引用的事实字段（替换后 eval 空 builtins，无法逃逸）
_FACT_KEYS = ("signal_type", "pin_bar", "funding_extreme", "narrow_range", "role")

_FM_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n?", re.S)


def _parse_frontmatter(text: str) -> tuple[dict, str]:
    m = _FM_RE.match(text)
    if not m:
        return {}, text
    meta = {}
    for line in m.group(1).splitlines():
        if ":" in line:
            k, _, v = line.partition(":")
            meta[k.strip()] = v.strip()
    return meta, text[m.end():]


def _safe_eval_use_when(expr: str, facts: dict) -> bool:
    """use_when 表达式判定：先替换事实字段，字符白名单过滤后 eval（空 builtins）"""
    if not expr or not expr.strip() or expr.strip() == "*":
        return False
    py = expr
    for k in _FACT_KEYS:
        py = re.sub(rf"\b{k}\b", repr(facts.get(k)), py)
    # YAML 惯例布尔字面量转 Python
    py = re.sub(r"\btrue\b", "True", py)
    py = re.sub(r"\bfalse\b", "False", py)
    if not re.fullmatch(r"[a-zA-Z_0-9\s=<>().,'\"\[\]]+", py):
        return False
    try:
        return bool(eval(py, {"__builtins__": {}}, {}))  # noqa: S307 白名单+空builtins
    except Exception:
        return False


def list_skills() -> list[dict]:
    """扫描技能目录，返回 [{name, description, use_when, version, body}]（按 name 排序）"""
    out = []
    if not SKILLS_DIR.exists():
        return out
    for d in sorted(SKILLS_DIR.iterdir()):
        f = d / "SKILL.md"
        if not f.is_file():
            continue
        try:
            meta, body = _parse_frontmatter(f.read_text(encoding="utf-8"))
            if meta.get("name"):
                out.append({
                    "name": meta["name"],
                    "description": meta.get("description", ""),
                    "use_when": meta.get("use_when", ""),
                    "version": meta.get("version", "1"),
                    "body": body.strip(),
                })
        except Exception as e:
            logger.warning("技能 %s 解析失败: %s", d.name, e)
    return out


def render_index(facts: dict) -> str:
    """渲染索引段：每条技能标注当前命中状态（Agent 看标注决定是否 load_skill）"""
    lines = []
    for s in list_skills():
        hit = _safe_eval_use_when(s["use_when"], facts)
        mark = "【当前命中】" if hit else ""
        lines.append(
            f"- {s['name']}: {s['description']} {mark}"
        )
    return "\n".join(lines) if lines else "（无可用技能）"


def load_skill(name: str) -> str:
    """加载技能正文（带截断），供 load_skill 工具调用"""
    for s in list_skills():
        if s["name"] == name:
            body = s["body"]
            if len(body) > _BODY_MAX_CHARS:
                body = body[:_BODY_MAX_CHARS] + "\n…（已截断）"
            return body
    return f"未找到技能: {name}"
