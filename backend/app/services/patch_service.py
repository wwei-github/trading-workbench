"""规则补丁闭环服务（docs/12 §10，2026-09-18）

AI 错误归因发现某错误标签高频出现（近7天热度 ≥0.5 且样本 ≥8）→ 生成补丁草稿
→ 人工确认合入（提示词=追加 strategy_prompt 覆盖段；参数=写 system_config 列）
→ 每日评估三态（有效/无效/恶化）→ 页面一键回滚。

关键约束：
- 系统自带提示词（SYSTEM_PROMPT / AGENT_SYSTEM 代码常量）永不改动；
  提示词补丁是「覆盖规则」段（带 patch 标记注释），追加在 strategy_prompt 尾部
- 回滚统一走 DB：提示词删标记段 / 参数恢复 NULL（默认语义），即时生效
- 同标签 14 天冷却；同标签存在 draft/active 补丁时不重复生成
- 「行情异常」「无明显错误」非规则性标签，不参与补丁触发
"""
import json
import logging
import re
from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import settings
from app.models.patch import PromptPatch
from app.models.scan import AIAnalysis
from app.models.system_config import SystemConfig
from app.models.trade import TradeRecord
from app.services.ai_attribution import ERROR_TAGS
from app.services.llm_client import get_client

logger = logging.getLogger(__name__)

# 参与补丁触发的规则性标签（排除非规则性的两类）
RULE_TAGS = tuple(t for t in ERROR_TAGS if t not in ("行情异常", "无明显错误"))

# 参数补丁允许的键与说明（值均为「比例」口径：0.04=4%）
PARAM_KEYS = {
    "max_stop_pct": "止损宽度硬上限（比例，0.04=4%；NULL=不限制）",
    "liq_gate_buffer": "强平闸门缓冲（比例，0.005=0.5%；NULL=恢复默认 0.005）",
    "min_adapt_leverage": "动态杠杆下限（整数倍数，8=允许降到 8×；NULL=恢复默认 8）",
    "pullback_market_pct": "入场偏离容差（比例，0.01=1%；待回踩距离 ≤ 容差时市价入场；NULL=恢复默认 0.01）",
    "max_slippage_pct": "开仓滑点实测上限（比例，0.003=0.3%；按本单数量走订单簿计算；NULL=恢复默认 0.003）",
}

HEAT_THRESHOLD = 0.5      # 热度阈值：近7天该标签笔数/已归因笔数
MIN_SAMPLE = 8            # 最小样本（已归因笔数）
COOLDOWN_DAYS = 14        # 同标签冷却
EVAL_FIRST_DAYS = 7       # 合入后首次评估
EVAL_FINAL_DAYS = 14      # 终评（valid/invalid 定稿）

# 补丁标记注释：<!-- patch:标签:id前8位 -->（程序解析 strategy_prompt 用）
MARKER_RE = re.compile(r"<!--\s*patch:([^:>]+):([0-9a-f]{8})\s*-->")


def _get_cfg(db: Session) -> SystemConfig:
    cfg = db.get(SystemConfig, 1)
    if not cfg:
        raise RuntimeError("system_config 未初始化")
    return cfg


# ── 窗口统计（评估口径与 actual_review 一致：win=realized_pnl>0）──────────

def window_stats(db: Session, days: int = 7) -> dict:
    """近 N 天已归因交易的统计：标签热度 + 整体护栏指标 + 建议通过率。"""
    since = datetime.utcnow() - timedelta(days=days)
    trades = db.execute(
        select(TradeRecord).where(
            TradeRecord.closed_at >= since,
            TradeRecord.realized_pnl.is_not(None),
        )
    ).scalars().all()

    attributed = [t for t in trades if t.error_tags]
    tag_counts: dict[str, int] = {}
    for t in attributed:
        for tag in (t.error_tags or [])[:3]:
            if tag in ERROR_TAGS:
                tag_counts[tag] = tag_counts.get(tag, 0) + 1

    pnls = [float(t.realized_pnl or 0) for t in trades]
    wins = sum(1 for p in pnls if p > 0)

    # 建议通过率：同期 AI 分析中 suggest 占比
    total_analyses = db.execute(
        select(func.count()).select_from(AIAnalysis)
        .where(AIAnalysis.created_at >= since)
    ).scalar_one()
    suggests = db.execute(
        select(func.count()).select_from(AIAnalysis)
        .where(AIAnalysis.created_at >= since, AIAnalysis.trade_decision == "suggest")
    ).scalar_one()

    attributed_total = len(attributed)
    return {
        "window_days": days,
        "closed_trades": len(trades),
        "attributed_total": attributed_total,
        "tag_counts": tag_counts,
        "heat": {  # 相对占比：免疫开单量波动
            tag: round(c / attributed_total, 4)
            for tag, c in tag_counts.items() if attributed_total
        },
        "ev_per_trade": round(sum(pnls) / len(pnls), 2) if pnls else 0.0,
        "win_rate": round(wins / len(pnls), 4) if pnls else 0.0,
        "pass_rate": round(suggests / total_analyses, 4) if total_analyses else 0.0,
    }


# ── 触发：热度超标 → AI 生成补丁草稿 ─────────────────────────────────────

def run_trigger(db: Session) -> dict:
    """每日触发任务：检查各规则标签热度，超标且过冷却的生成补丁草稿。

    返回 {"drafts": [...], "attributed_total": n, "skips": [...]}——skips 逐条
    记录三重门槛的判定明细（样本不足 / 各标签热度 / 已有草稿 / 冷却中），
    任务记录与手动触发响应直接展示，一眼看出没生成补丁卡在哪关。
    """
    stats = window_stats(db, days=7)
    skips: list[str] = []
    if stats["attributed_total"] < MIN_SAMPLE:
        skips.append(
            f"样本不足：近7天已归因仅 {stats['attributed_total']} 笔（门槛 {MIN_SAMPLE}），"
            f"先积累归因样本"
        )
        return {"drafts": [], "attributed_total": stats["attributed_total"], "skips": skips}

    cooldown_since = datetime.utcnow() - timedelta(days=COOLDOWN_DAYS)
    drafts: list[PromptPatch] = []
    below: list[str] = []  # 热度未达标标签（汇总一行，避免明细过长）
    for tag in RULE_TAGS:
        heat = stats["heat"].get(tag, 0.0)
        if heat < HEAT_THRESHOLD:
            if stats["tag_counts"].get(tag):
                below.append(f"{tag} {heat:.2f}")
            continue
        # 同标签存在待确认/生效中补丁 → 不重复生成
        exists = db.execute(
            select(func.count()).select_from(PromptPatch)
            .where(PromptPatch.target_tag == tag,
                   PromptPatch.status.in_(("draft", "active")))
        ).scalar_one()
        if exists:
            skips.append(f"{tag} 热度 {heat:.2f} 达标，但已有待确认/生效中补丁，跳过")
            continue
        # 冷却：同标签近期合入过（任意终态）→ 跳过
        cooled = db.execute(
            select(func.count()).select_from(PromptPatch)
            .where(PromptPatch.target_tag == tag,
                   PromptPatch.applied_at >= cooldown_since)
        ).scalar_one()
        if cooled:
            skips.append(f"{tag} 热度 {heat:.2f} 达标，但在 {COOLDOWN_DAYS} 天冷却期内，跳过")
            continue

        case_ids = [
            t.id for t in db.execute(
                select(TradeRecord).where(
                    TradeRecord.closed_at >= datetime.utcnow() - timedelta(days=7),
                    TradeRecord.realized_pnl.is_not(None),
                    TradeRecord.error_tags.isnot(None),
                ).order_by(TradeRecord.closed_at.desc()).limit(50)
            ).scalars().all()
            if tag in (t.error_tags or [])
        ][:5]
        patch = _generate_draft(db, tag, heat, stats, case_ids)
        if patch:
            db.add(patch)
            drafts.append(patch)
        else:
            skips.append(f"{tag} 热度 {heat:.2f} 达标，但 AI 生成草稿失败（见系统日志）")
    if below:
        skips.append(f"热度未达标（门槛 {HEAT_THRESHOLD}）：{'、'.join(below)}")
    if drafts:
        db.commit()
    return {"drafts": drafts, "attributed_total": stats["attributed_total"], "skips": skips}


_PATCH_GEN_SYSTEM = """你是期货交易系统的规则工程师。复盘发现某类错误高频出现，\
请生成一条「规则补丁」来堵住这个漏洞。

补丁分两类：
1. kind="prompt"：提示词覆盖规则。new_text 是一段 ≤500 字的明确规则\
（写清「什么情况下禁止/必须做什么」），将追加在系统提示词之后生效。\
old_text 从给定系统提示词中**逐字摘取**被针对的段落（≤200字，必须原文子串）。
2. kind="param"：程序参数建议。param_key 只能是：%s。\
param_value 为比例值（0.04=4%%）。

要求：规则必须可执行、可判定，禁止空话；一个标签只出一条补丁。
只输出 JSON：{"kind": "prompt|param", "param_key": "", "param_value": "", \
"old_text": "", "new_text": ""}，不要输出其他内容。
""" % "；".join(f"{k}（{v}）" for k, v in PARAM_KEYS.items())


def _generate_draft(db: Session, tag: str, heat: float, stats: dict,
                    case_ids: list) -> Optional[PromptPatch]:
    """调 LLM 生成单条补丁草稿（程序校验通过才建 draft，失败返回 None）。"""
    from app.services.ai_analyzer import SYSTEM_PROMPT

    cfg = _get_cfg(db)
    active_patches = db.execute(
        select(PromptPatch).where(PromptPatch.status == "active")
    ).scalars().all()

    evidence = {
        "target_tag": tag,
        "tag_desc": ERROR_TAGS[tag],
        "heat_7d": heat,
        "attributed_total_7d": stats["attributed_total"],
        "ev_per_trade_7d": stats["ev_per_trade"],
        "win_rate_7d": stats["win_rate"],
        "case_trade_ids": case_ids,
        "current_params": {
            "max_stop_pct": (float(cfg.max_stop_pct) if cfg.max_stop_pct is not None else None),
            "liq_gate_buffer": (float(cfg.liq_gate_buffer)
                                if cfg.liq_gate_buffer is not None else None),
            "min_adapt_leverage": cfg.min_adapt_leverage,
            "pullback_market_pct": (float(cfg.pullback_market_pct)
                                    if cfg.pullback_market_pct is not None else None),
            "max_slippage_pct": (float(cfg.max_slippage_pct)
                                 if cfg.max_slippage_pct is not None else None),
        },
        "active_patches": [
            {"tag": p.target_tag, "kind": p.kind, "new_text": p.new_text[:200]}
            for p in active_patches
        ],
    }
    user_msg = (
        f"系统提示词（old_text 必须是它的原文子串）：\n{SYSTEM_PROMPT}\n\n"
        f"触发证据：\n{json.dumps(evidence, ensure_ascii=False)}"
    )
    try:
        resp = get_client().chat.completions.create(
            model=settings.AI_MODEL,
            messages=[
                {"role": "system", "content": _PATCH_GEN_SYSTEM},
                {"role": "user", "content": user_msg},
            ],
            temperature=0.2,
            max_tokens=12000,
        )
        content = (resp.choices[0].message.content or "").strip()
        if content.startswith("```"):
            lines = [l for l in content.split("\n") if not l.strip().startswith("```")]
            content = "\n".join(lines).strip()
        data = json.loads(content)
    except Exception as e:
        logger.warning("补丁草稿生成失败（%s）：%s", tag, e)
        return None

    kind = data.get("kind")
    new_text = str(data.get("new_text") or "").strip()
    if kind == "prompt":
        old_text = str(data.get("old_text") or "").strip()
        # 程序校验：old_text 逐字存在于系统提示词；new_text 非空限长
        if not old_text or old_text not in SYSTEM_PROMPT:
            logger.warning("补丁草稿校验失败（%s）：old_text 非系统提示词原文子串", tag)
            return None
        if not new_text or len(new_text) > 500:
            logger.warning("补丁草稿校验失败（%s）：new_text 空或超 500 字", tag)
            return None
        return PromptPatch(
            target_tag=tag, origin="ai", kind="prompt",
            old_text=old_text[:300], new_text=new_text,
            evidence_json={"heat_7d": heat, "attributed_total_7d": stats["attributed_total"],
                           "case_trade_ids": case_ids},
        )
    if kind == "param":
        param_key = str(data.get("param_key") or "")
        param_value = str(data.get("param_value") or "").strip()
        if param_key not in PARAM_KEYS:
            logger.warning("补丁草稿校验失败（%s）：param_key 非法 %r", tag, param_key)
            return None
        try:
            v = float(param_value)
            assert 0 < v < 1
        except (ValueError, AssertionError):
            logger.warning("补丁草稿校验失败（%s）：param_value %r 非比例值", tag, param_value)
            return None
        return PromptPatch(
            target_tag=tag, origin="ai", kind="param",
            param_key=param_key, param_value=repr(v),
            new_text=new_text[:500] or f"建议 {param_key}={v}",
            evidence_json={"heat_7d": heat, "attributed_total_7d": stats["attributed_total"],
                           "case_trade_ids": case_ids},
        )
    logger.warning("补丁草稿校验失败（%s）：kind 非法 %r", tag, kind)
    return None


# ── 合入 / 回滚（页面操作，全部 DB 即时生效）─────────────────────────────

def apply_patch(db: Session, patch_id: str) -> PromptPatch:
    """草稿合入：提示词追加覆盖段 / 参数写列；快照合入前基线指标。"""
    patch = db.get(PromptPatch, patch_id)
    if not patch:
        raise ValueError("补丁不存在")
    if patch.status != "draft":
        raise ValueError(f"仅 draft 可合入（当前 {patch.status}）")
    cfg = _get_cfg(db)

    if patch.kind == "prompt":
        marker = f"<!-- patch:{patch.target_tag}:{patch.id[:8]} -->"
        seg = f"\n\n{marker}\n{patch.new_text}"
        cfg.strategy_prompt = (cfg.strategy_prompt or "") + seg
    else:  # param
        setattr(cfg, patch.param_key, float(patch.param_value))

    patch.status = "active"
    patch.verdict = "pending"
    patch.applied_at = datetime.utcnow()
    patch.metrics_before_json = window_stats(db, days=7)
    db.commit()
    db.refresh(patch)
    logger.info("补丁合入：%s %s（%s）", patch.target_tag, patch.kind, patch.id[:8])
    return patch


def rollback_patch(db: Session, patch_id: str) -> PromptPatch:
    """回滚：提示词删除标记段 / 参数恢复 NULL（默认语义）。即时生效。"""
    patch = db.get(PromptPatch, patch_id)
    if not patch:
        raise ValueError("补丁不存在")
    if patch.status == "draft":
        raise ValueError("草稿直接删除即可，无需回滚")
    if patch.status == "rolled_back":
        raise ValueError("该补丁已回滚")
    cfg = _get_cfg(db)

    if patch.kind == "prompt":
        marker = f"<!-- patch:{patch.target_tag}:{patch.id[:8]} -->"
        text = cfg.strategy_prompt or ""
        idx = text.find(marker)
        if idx >= 0:
            # 删除标记行与其后到下一个标记/文末的内容
            nxt = MARKER_RE.search(text, idx + len(marker))
            end = nxt.start() if nxt else len(text)
            cfg.strategy_prompt = (text[:idx] + text[end:]).strip()
    else:  # param → 恢复默认（NULL=不限制/内置默认）
        setattr(cfg, patch.param_key, None)

    patch.status = "rolled_back"
    db.commit()
    db.refresh(patch)
    logger.info("补丁回滚：%s %s（%s）", patch.target_tag, patch.kind, patch.id[:8])
    return patch


# ── 评估：三态判定（docs/12 §10.6）───────────────────────────────────────

def _judge(before: dict, after: dict, target_tag: str) -> tuple[str, list[str]]:
    """返回 (verdict, checks 说明列表)。verdict ∈ valid/invalid/worse/pending。"""
    checks: list[str] = []
    h_before = before.get("heat", {}).get(target_tag, 0.0)
    h_after = after.get("heat", {}).get(target_tag, 0.0)
    heat_ratio = (h_after / h_before) if h_before else 1.0
    checks.append(
        f"目标热度 {h_before:.2f}→{h_after:.2f}（×{heat_ratio:.2f}）"
    )

    # 护栏三：建议通过率（补丁过严 → 系统被阉割）
    pass_after = after.get("pass_rate", 0.0)
    if pass_after < 0.3:
        checks.append(f"护栏三告警：建议通过率 {pass_after:.0%} < 30%")
        return "worse", checks

    # 护栏一：整体不劣化（EV 转负 / 相对恶化超 30%（负基线同样适用） / 胜率跌 20pp）
    ev_b, ev_a = before.get("ev_per_trade", 0.0), after.get("ev_per_trade", 0.0)
    wr_b, wr_a = before.get("win_rate", 0.0), after.get("win_rate", 0.0)
    checks.append(f"EV/笔 {ev_b}→{ev_a}；胜率 {wr_b:.0%}→{wr_a:.0%}")
    ev_flip = ev_b >= 0 and ev_a < 0
    ev_drop = abs(ev_b) > 0 and (ev_b - ev_a) > 0.3 * abs(ev_b)
    if ev_flip or ev_drop:
        checks.append("护栏一告警：EV 显著恶化")
        return "worse", checks
    if wr_b and wr_a < wr_b - 0.20:
        checks.append("护栏一告警：胜率下降超 20pp")
        return "worse", checks

    # 护栏二：其他标签热度翻倍（镜像对盯防）
    for tag, h_a in after.get("heat", {}).items():
        if tag == target_tag:
            continue
        h_b = before.get("heat", {}).get(tag, 0.0)
        if h_b and h_a >= h_b * 2:
            checks.append(f"护栏二告警：{tag} 热度 {h_b:.2f}→{h_a:.2f} 翻倍")
            return "worse", checks

    # 主指标判定
    if heat_ratio <= 0.5:
        checks.append("主指标达标：热度下降 ≥50%")
        return "valid", checks
    if 0.8 <= heat_ratio <= 1.2:
        checks.append("主指标无改善（±20% 内）")
        return "invalid", checks
    return "pending", checks  # 改善但未达 50%，继续观察


def run_evaluate(db: Session) -> list[PromptPatch]:
    """每日评估任务：active 补丁按合入天数做首评/终评与恶化告警。"""
    active = db.execute(
        select(PromptPatch).where(PromptPatch.status == "active")
    ).scalars().all()
    if not active:
        return []
    now = datetime.utcnow()
    updated: list[PromptPatch] = []
    for patch in active:
        days = (now - patch.applied_at).days if patch.applied_at else 0
        if days < EVAL_FIRST_DAYS and patch.verdict != "worse":
            continue
        after = window_stats(db, days=7)
        # 样本不足不判定（前后窗口各需 ≥8 笔已归因），顺延观察
        if after.get("attributed_total", 0) < MIN_SAMPLE and patch.verdict != "worse":
            patch.metrics_after_json = {
                "after": after, "checks": [f"评估窗口样本不足（{after.get('attributed_total', 0)} < {MIN_SAMPLE}），顺延"],
                "evaluated_days": days,
            }
            continue
        verdict, checks = _judge(patch.metrics_before_json or {}, after,
                                 patch.target_tag)
        patch.metrics_after_json = {
            "after": after, "checks": checks, "evaluated_days": days,
        }
        if days >= EVAL_FINAL_DAYS:
            # 终评定稿：恶化保持 active+告警（等人工回滚），其余定稿
            if verdict in ("valid", "invalid", "worse"):
                if verdict == "worse":
                    patch.verdict = "worse"  # 保持 active，页面置顶告警
                else:
                    patch.verdict = verdict
                    patch.status = verdict
            else:  # 14 天仍无结论 → 无效处理
                patch.verdict = "invalid"
                patch.status = "invalid"
        elif verdict == "worse":
            patch.verdict = "worse"  # 提前告警，保持 active
        else:
            patch.verdict = "pending"
        updated.append(patch)
    db.commit()
    return updated


# ── 生效提示词分层视图（页面「查看当前生效提示词」）──────────────────────

def build_prompt_view(db: Session) -> dict:
    """三层拼装：系统自带（代码常量只读）/ 用户自定义 / 补丁覆盖段。"""
    from app.services.ai_agent import render_system_prompt
    from app.services.ai_analyzer import SYSTEM_PROMPT

    cfg = _get_cfg(db)
    text = cfg.strategy_prompt or ""
    patches = db.execute(
        select(PromptPatch).where(PromptPatch.status.in_(("active", "valid")))
    ).scalars().all()
    by_marker = {f"<!-- patch:{p.target_tag}:{p.id[:8]} -->": p for p in patches}
    segments: list[dict] = []
    matches = list(MARKER_RE.finditer(text))
    custom_head = text[:matches[0].start()].strip() if matches else text.strip()
    for i, m in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        p = by_marker.get(m.group(0))
        segments.append({
            "marker": m.group(0),
            "tag": m.group(1),
            "patch_id": p.id if p else None,
            "status": p.status if p else "orphan",
            "verdict": p.verdict if p else None,
            "applied_at": p.applied_at.isoformat() if p and p.applied_at else None,
            "text": text[m.end():end].strip(),
        })
    return {
        "system_prompt": SYSTEM_PROMPT,
        "agent_system": render_system_prompt(),
        "user_custom": custom_head,
        "patch_segments": segments,
        "params": {
            "max_stop_pct": (float(cfg.max_stop_pct)
                             if cfg.max_stop_pct is not None else None),
            "liq_gate_buffer": (float(cfg.liq_gate_buffer)
                                if cfg.liq_gate_buffer is not None else None),
            "min_adapt_leverage": cfg.min_adapt_leverage,
            "pullback_market_pct": (float(cfg.pullback_market_pct)
                                    if cfg.pullback_market_pct is not None else None),
            "max_slippage_pct": (float(cfg.max_slippage_pct)
                                 if cfg.max_slippage_pct is not None else None),
        },
    }
