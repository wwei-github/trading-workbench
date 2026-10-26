"""AI 分析暂停时段（2026-10-26）。

每天固定整点区间内跳过「筛选后自动 AI 分析」，降低无人在场时段的 LLM 消耗；
开关关闭（默认）时行为与历史完全一致——每轮筛选后照常分析。

- 时区固定北京时间（UTC+8，无夏令时），与 celery beat 的 Asia/Shanghai 口径一致；
  用固定 offset 而非 ZoneInfo，避免 slim 镜像缺 tzdata 时初始化失败。
- 区间语义 [start_hour, end_hour)：start < end 为同日区间，start > end 为跨零点
  区间（如 22→6）；start == end 视为空区间（不暂停），防止误配成全天停摆。
- 只拦批量/自动分析（与 ai_analysis_enabled 同语义），手动单币重分析不受限。
"""
from datetime import datetime, timedelta, timezone
from typing import Optional

BEIJING_TZ = timezone(timedelta(hours=8))


def pause_window_label(cfg) -> Optional[str]:
    """返回暂停时段文案「HH:00-HH:00」；未开启/未配置完整/空区间返回 None。"""
    if not getattr(cfg, "ai_pause_enabled", False):
        return None
    start = getattr(cfg, "ai_pause_start_hour", None)
    end = getattr(cfg, "ai_pause_end_hour", None)
    if start is None or end is None:
        return None
    start, end = int(start), int(end)
    if start == end:
        return None
    return f"{start:02d}:00-{end:02d}:00"


def is_ai_paused(cfg, now: Optional[datetime] = None) -> bool:
    """当前时刻是否落在暂停时段。

    now 为 None 时取当前北京时间；naive datetime 按 UTC 理解（与项目 utcnow 惯例一致）。
    """
    label = pause_window_label(cfg)
    if label is None:
        return False
    start = int(cfg.ai_pause_start_hour)
    end = int(cfg.ai_pause_end_hour)
    if now is None:
        hour = datetime.now(BEIJING_TZ).hour
    elif now.tzinfo is None:
        hour = now.replace(tzinfo=timezone.utc).astimezone(BEIJING_TZ).hour
    else:
        hour = now.astimezone(BEIJING_TZ).hour
    if start < end:
        return start <= hour < end
    return hour >= start or hour < end
