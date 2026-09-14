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


def build_facts(signal: dict, klines: list) -> dict:
    """技能 use_when 判定用的布尔事实（程序算，不让模型猜）

    funding_extreme 恒为 False（市场环境数据不再预取，资金费率由 Agent 调 get_funding 自查），
    依赖它的技能不会自动命中，但仍会出现在索引中由 Agent 按需加载。
    """
    facts: dict = {
        "signal_type": signal.get("signal_type") or "unknown",
        "pin_bar": False,
        "funding_extreme": False,
        "narrow_range": False,
        "role": None,
    }
    # 信号位置角色：position 即形态/突破方向派生的 support/resistance，直接作 role；
    # 旧数据的 prev_high 等历史值不命中（role=None，技能不自动触发）
    hit_kind = signal.get("position")
    if hit_kind in ("support", "resistance"):
        facts["role"] = hit_kind
    # pin_bar：已收盘最后一根 影线 > 2×实体
    if len(klines) >= 2:
        k = klines[-2]
        h, l, o, c = float(k[2]), float(k[3]), float(k[1]), float(k[4])
        body = abs(c - o)
        shadow = max(h - o, h - c) + max(o - l, c - l)  # 上下影线之和
        facts["pin_bar"] = body > 0 and shadow > 2 * body
    return facts


# P0 管线注入正文的预算：单条与总量截断（约 3k token），控制批量分析的成本与延迟
_PER_SKILL_MAX = 4000
_TOTAL_MAX = 6000


def render_matched(facts: dict) -> str:
    """渲染命中技能的正文块（无工具循环的 P0 管线直接注入系统提示词用）。

    命中即注入（Agent 管线的"按需 load_skill"在此不可用）；多条命中按
    技能名顺序拼接，超预算截断。无命中返回空串。
    """
    parts: list[str] = []
    used = 0
    for s in list_skills():
        if not _safe_eval_use_when(s["use_when"], facts):
            continue
        body = s["body"]
        if len(body) > _PER_SKILL_MAX:
            body = body[:_PER_SKILL_MAX] + "\n…（已截断）"
        if used + len(body) > _TOTAL_MAX:
            body = body[: max(0, _TOTAL_MAX - used)]
            if body:
                parts.append(f"### 技能：{s['name']}\n{body}\n…（总量截断）")
            break
        parts.append(f"### 技能：{s['name']}\n{body}")
        used += len(body)
    return "\n\n".join(parts)


def load_skill(name: str) -> str:
    """加载技能正文（带截断），供 load_skill 工具调用"""
    for s in list_skills():
        if s["name"] == name:
            body = s["body"]
            if len(body) > _BODY_MAX_CHARS:
                body = body[:_BODY_MAX_CHARS] + "\n…（已截断）"
            return body
    return f"未找到技能: {name}"
