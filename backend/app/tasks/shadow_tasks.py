"""影子模式每日评估任务（docs/14 §六，2026-10-04）。

每日一次：按特征分桶 join 复盘结果统计（services/shadow.build_report），
结果写日报附录（notify_daily_report 调用方拉取）并落任务记录。
只统计不决策——即使某特征已达标，升格也必须走 docs/14 §八 人工流程。
"""
import logging

from app.celery_app import celery_app
from app.database import SessionLocal

logger = logging.getLogger(__name__)


def build_report_safe(db, window_days: int = 30) -> list[dict]:
    """build_report 包装：评估链路任何异常上抛由调用方处置（日报吞、任务记失败）"""
    from app.services.shadow import build_report
    return build_report(db, window_days=window_days)


def shadow_report_lines(window_days: int = 30) -> list[str]:
    """日报附录行（每特征一行摘要；无任何标记时不输出）。异常静默降级为空。"""
    try:
        db = SessionLocal()
        try:
            report = build_report_safe(db, window_days)
        finally:
            db.close()
    except Exception as e:
        logger.warning("影子评估日报行构建失败（忽略）: %s", e)
        return []
    lines: list[str] = []
    for r in report:
        if r["baseline"]["n"] == 0:
            continue  # 该特征还没有任何影子样本，日报不占版面
        base = r["baseline"]
        base_str = f"{base['win_rate'] * 100:.0f}%" if base["win_rate"] is not None else "—"
        parts = [f"基线 {base_str}（n={base['n']}）"]
        for b in r["buckets"]:
            if b["bucket"] in ("confirm", "normal") or b["n"] == 0:
                continue  # 对照桶/空桶不在日报展开，面板看全量
            wr = f"{b['win_rate'] * 100:.0f}%" if b["win_rate"] is not None else "—"
            diff = ""
            if b["win_rate"] is not None and base["win_rate"] is not None:
                d = (b["win_rate"] - base["win_rate"]) * 100
                diff = f"（{d:+.0f}pp）"
            parts.append(f"{b['bucket']} {wr} n={b['n']}{diff}")
        status = "✅ 达标" if r["sufficient"] else "观察中"
        if r["card"] == "CARD-07":
            # 再入场前置（docs/13 #12）：读数是止损后 24h 续走率，非胜率
            parts = []
            cr = base.get("cont_rate")
            parts.append(f"基线续走率 {cr * 100:.0f}%（n={base['n']}）" if cr is not None
                         else f"基线 n={base['n']}")
            for b in r["buckets"]:
                if b["n"] == 0:
                    continue
                cb = b.get("cont_rate")
                cb_s = f"{cb * 100:.0f}%" if cb is not None else "—"
                parts.append(f"{b['bucket']} {cb_s} n={b['n']}")
            lines.append(f"🔬 **{r['name']}** [{status}] " + " · ".join(parts))
            continue
        lines.append(f"🔬 **{r['name']}** [{status}] " + " · ".join(parts))
    return lines


@celery_app.task(name="app.tasks.shadow_tasks.run_shadow_evaluate_task")
def run_shadow_evaluate_task() -> dict:
    """每日影子评估：构建报告 → 记录任务日志。评估结果经 /api/shadow/report
    与次日日报附录输出；此处仅触发统计并落运行记录。"""
    from app.services.task_log import close_task, open_task

    task_id = open_task("review", "影子实验评估", trigger="scheduled")
    db = SessionLocal()
    try:
        report = build_report_safe(db)
        total_marks = sum(r["baseline"]["n"] for r in report)
        sufficient = [r["feature"] for r in report if r["sufficient"]]
        logger.info(
            "影子评估完成：标记 %d 条，达标特征 %s",
            total_marks, ",".join(sufficient) or "无",
        )
        close_task(
            task_id, "completed",
            summary=f"标记 {total_marks} 条，达标特征 {','.join(sufficient) or '无'}",
            detail={
                "total_marks": total_marks,
                "sufficient": sufficient,
                "features": {r["feature"]: r["baseline"]["n"] for r in report},
            },
        )
        return {
            "total_marks": total_marks,
            "sufficient": sufficient,
            "features": {r["feature"]: r["baseline"]["n"] for r in report},
        }
    except Exception as e:
        close_task(task_id, "failed", error=str(e))
        logger.exception("影子评估任务失败")
        raise
    finally:
        db.close()


@celery_app.task(name="app.tasks.shadow_tasks.run_reentry_backfill_task")
def run_reentry_backfill_task() -> dict:
    """每日再入场前置回填（docs/13 #12，CARD-07）：出场 48h 观察窗已过的
    止损单补打标（出场时结构 × 出场后 24/48h 续走）。打标异常全吞
    （影子铁律），任务仅记录运行结果。"""
    from app.services.shadow import tag_reentry_backfill
    from app.services.task_log import close_task, open_task

    task_id = open_task("review", "再入场前置回填", trigger="scheduled")
    try:
        tagged = tag_reentry_backfill()
        logger.info("再入场前置回填完成：%d 行", tagged)
        close_task(task_id, "completed", summary=f"回填 {tagged} 行",
                   detail={"tagged": tagged})
        return {"tagged": tagged}
    except Exception as e:
        close_task(task_id, "failed", error=str(e))
        logger.exception("再入场前置回填任务失败")
        raise
