"""飞书卡片内嵌 K 线图渲染（2026-10-15，docs/09 通知）。

正式网自动开单关闭提醒（trade_engine.notify_live_auto_open_off）在飞书卡片里
内嵌一张服务端渲染的 PNG：K 线 + EMA21/55/144 均线 + 开单计划线（入场/止损/
分批止盈），让收到提醒的人不用打开工作台就能判断该信号值不值得手动开单。

- 纯函数：`render_plan_chart(klines, ...) -> bytes | None`；数据不足 / 渲染异常
  一律返回 None，调用方降级为纯文字卡片（通知不因画图失败而丢失）
- 视觉口径与前端同源：涨跌色取 klineShared.CANDLE_COLORS 默认「红涨绿跌」方案
  （#ef5350 涨 / #26a69a 跌），EMA21/55/144 = #f0b90b / #00bcd4 / #ff9800
  （constants/indicators.MA_COLORS），计划线颜色/色块/百分比对价对齐
  ChartCore.drawAiPosition；EMA 计算复用 strategy.ema_gate.ema_series
- 图内文字只用 ASCII（SYMBOL / LONG / ENTRY / SL / TP1 / TP2 / EMA21…）：容器
  镜像无 CJK 字体，中文一律留在卡片 lark_md 元素里，图内出现中文会变豆腐块
- 依赖 Pillow 手绘（requirements 新增），不引入 matplotlib
"""
import io
import logging
import math
from datetime import datetime, timedelta, timezone
from typing import Optional

import numpy as np

from app.services.strategy.ema_gate import ema_series

logger = logging.getLogger(__name__)

# 北京时间（图内时间轴口径与前端 klineShared.fmtTime 一致）
_BJ = timezone(timedelta(hours=8))

# ── 画布与布局 ──────────────────────────────────────────────────────
_W, _H = 780, 470
_PAD_L = 10          # 左留白
_PAD_R = 84          # 右侧价格轴宽度
_PAD_T = 52          # 顶部标题 + 图例两行
_PAD_B = 28          # 底部时间轴

# ── 配色（深色渐变底，与前端图表一致）────────────────────────────────
_BG_TOP, _BG_BOT = (28, 34, 49), (18, 21, 31)     # #1c2231 → #12151f
_GRID = (29, 35, 50)                              # #1d2332
_TEXT, _MUTED = (201, 207, 221), (139, 147, 167)  # #c9cfdd / #8b93a7
_WM = (34, 41, 58)                                # 中央水印

_UP = (239, 83, 80)      # #ef5350（红涨绿跌默认方案：涨色）
_DOWN = (38, 166, 154)   # #26a69a（跌色）

# EMA 均线族：周期 + 颜色（前端 MA_COLORS 前三位）
_EMA_STYLE = ((21, (240, 185, 11)), (55, (0, 188, 212)), (144, (255, 152, 0)))

_INTERVAL_LABEL = {"1h": "1H", "4h": "4H", "1d": "1D", "15m": "15M", "5m": "5M"}


def _font(size: int):
    """默认字体（Pillow ≥10.1 的 load_default 支持 size，回退到定长位图字体）"""
    from PIL import ImageFont

    try:
        return ImageFont.load_default(size=size)
    except TypeError:  # pragma: no cover - 旧版 Pillow 兜底
        return ImageFont.load_default()


def _fmt_price(p: float) -> str:
    """价格小数位口径与前端 fmtPrice / trade_engine._fmt_price 一致"""
    if p < 1:
        return f"{p:.6f}"
    return f"{p:.4f}" if p < 100 else f"{p:.2f}"


def _pct_delta(entry: float, p: float) -> str:
    """相对入场价的百分比对价（对齐前端 pctDelta：带符号一位小数）"""
    d = (p - entry) / entry * 100
    return f"{'+' if d > 0 else ''}{d:.1f}%"


def _blend(fg: tuple, bg: tuple, alpha: float) -> tuple:
    """fg 以 alpha 叠加在 bg 上的实色（K 线图画不出真半透明，先合成为实色）"""
    return tuple(int(round(bg[i] + (fg[i] - bg[i]) * alpha)) for i in range(3))


def _place_label_ys(ys: list, *, box_h: int = 16,
                    top: int = _PAD_T, bot: int = _H - _PAD_B) -> list:
    """计划线右缘标签的纵向位置（2026-10-15 修复贴底重叠）。

    入参为按升序排好的期望标签顶边（y − box_h/2）。相邻标签至少错开 box_h，
    且整组必须落在 [top, bot − box_h] 内。**先错开、后整组平移**：原先「错开后
    逐个钳位」在标签簇贴近底边时会把错开间距压掉（极端行情 130 根区间 200% +
    紧止损时实测 3 对重叠），SL 标签被压在邻居下面读不出来——图是这张卡片唯一
    的读数来源，故改为整组上抬，平移不改变组内间距，重叠不可能再发生（≤4 个
    标签共 64px，画布高 390px，不会推到顶边之外）。
    """
    tys: list = []
    for y in ys:
        ty = y - box_h / 2
        while any(abs(ty - p) < box_h for p in tys):
            ty += 2
        tys.append(ty)
    over = max(tys) + box_h - bot
    if over > 0:  # 整组上抬消底边溢出（平移不改变组内间距，重叠不可能发生）
        tys = [p - over for p in tys]
    up = top - min(tys)
    if up > 0:  # 整组下压，保底不越顶边
        tys = [p + up for p in tys]
    return tys


def render_plan_chart(
    klines: list,
    *,
    symbol: str,
    direction: str,
    entry: float,
    stop_loss: float,
    take_profit_1: float,
    take_profit_2: Optional[float] = None,
    interval: str = "1h",
    bars: int = 130,
    ai_score: Optional[float] = None,
) -> Optional[bytes]:
    """渲染「K 线 + EMA 均线 + 开单计划」PNG（飞书卡片 img 元素用）。

    klines 为币安原格式 [[open_ms, o, h, l, c, v, …], …]（尾根未收盘，画出来
    的最后一根即当前进行中那根）；EMA 在全序列上计算后按可视窗口切片，尾根
    均线值仍是有效现值。返回 PNG 字节，任何异常/数据不足返回 None。
    """
    try:
        from PIL import Image, ImageDraw

        n_all = len(klines)
        if n_all < 30:
            return None
        closes = np.array([float(k[4]) for k in klines], dtype=float)
        emas = {period: ema_series(closes, period) for period, _ in _EMA_STYLE}

        view = klines[-min(bars, n_all):]
        off = n_all - len(view)
        n = len(view)

        # 纵轴范围：可视 K 线高低点 ∪ 计划线价格，再留 6% 呼吸空间
        # （计划线必须落在画布内，否则标签会被裁掉）
        hi = max(float(k[2]) for k in view)
        lo = min(float(k[3]) for k in view)
        plans = [float(p) for p in (entry, stop_loss, take_profit_1, take_profit_2)
                 if p and float(p) > 0]
        if plans:
            hi, lo = max(hi, max(plans)), min(lo, min(plans))
        if not hi > lo:
            return None
        pad = (hi - lo) * 0.06
        hi, lo = hi + pad, lo - pad

        plot_l, plot_r = _PAD_L, _W - _PAD_R
        plot_t, plot_b = _PAD_T, _H - _PAD_B
        plot_w, plot_h = plot_r - plot_l, plot_b - plot_t

        def y_of(p: float) -> float:
            return plot_t + (hi - p) / (hi - lo) * plot_h

        step = plot_w / n
        bw = max(1.0, step * 0.62)
        f15, f12, f11 = _font(15), _font(12), _font(11)

        # 纵向渐变底（逐行画，470 行开销可忽略）
        img = Image.new("RGB", (_W, _H), _BG_TOP)
        draw = ImageDraw.Draw(img)
        for y in range(_H):
            t = y / (_H - 1)
            draw.line([(0, y), (_W, y)],
                      fill=tuple(int(_BG_TOP[i] + (_BG_BOT[i] - _BG_TOP[i]) * t)
                                 for i in range(3)))
        mid_bg = _blend(_BG_TOP, _BG_BOT, 0.5)

        # 网格 + 右侧价格轴标签
        for k in range(6):
            y = y_of(hi - (hi - lo) * k / 5)
            draw.line([(plot_l, y), (plot_r, y)], fill=_GRID)
            draw.text((plot_r + 6, y - 7), _fmt_price(hi - (hi - lo) * k / 5),
                      font=f11, fill=_MUTED)
        for k in range(1, 6):
            x = plot_l + plot_w * k / 6
            draw.line([(x, plot_t), (x, plot_b)], fill=_GRID)

        # 中央水印（币种名，压在最底层，被 K 线覆盖）
        wm = _font(44)
        draw.text(((plot_l + plot_r - draw.textlength(symbol, font=wm)) / 2,
                   (plot_t + plot_b) / 2 - 26), symbol, font=wm, fill=_WM)

        # 计划区域色块（对齐 ChartCore.drawAiPosition：止损区红 / 止盈区绿）
        def zone(p1, p2, color, alpha):
            if not (p1 and p2 and float(p1) > 0 and float(p2) > 0):
                return
            y1, y2 = y_of(float(p1)), y_of(float(p2))
            draw.rectangle([plot_l, min(y1, y2), plot_r, max(y1, y2)],
                           fill=_blend(color, mid_bg, alpha))

        zone(entry, stop_loss, _UP, 0.16)
        zone(entry, take_profit_1, _DOWN, 0.16)
        zone(take_profit_1, take_profit_2, _DOWN, 0.08)

        # K 线（影线 + 实体，涨跌双色，实体不足 1px 时撑到 1px 保证可见）
        for i, k in enumerate(view):
            o, h, l, c = float(k[1]), float(k[2]), float(k[3]), float(k[4])
            col = _UP if c >= o else _DOWN
            x = plot_l + (i + 0.5) * step
            draw.line([(x, y_of(h)), (x, y_of(l))], fill=col, width=1)
            top, bot = y_of(max(o, c)), y_of(min(o, c))
            draw.rectangle([x - bw / 2, top, x + bw / 2, max(bot, top + 1)], fill=col)

        # EMA 均线（前 period-1 根为 NaN，跳过；越界值裁到画布内）
        for period, col in _EMA_STYLE:
            s = emas.get(period)
            if s is None:
                continue
            pts = []
            for i in range(n):
                v = float(s[off + i])
                if math.isfinite(v):
                    pts.append((plot_l + (i + 0.5) * step, y_of(min(max(v, lo), hi))))
            if len(pts) >= 2:
                draw.line(pts, fill=col, width=2)

        # 开单计划线（虚线 + 右缘色底白字标签；入场=方向色 / SL=跌色 / TP=涨色）
        is_long = direction != "short"
        entry_col = _UP if is_long else _DOWN
        lines: list = []
        if entry and float(entry) > 0:
            lines.append((y_of(float(entry)),
                          f"{'LONG' if is_long else 'SHORT'} {_fmt_price(float(entry))}",
                          entry_col))
        if stop_loss and float(stop_loss) > 0:
            lines.append((y_of(float(stop_loss)),
                          f"SL {_fmt_price(float(stop_loss))} {_pct_delta(float(entry), float(stop_loss))}",
                          _DOWN))
        if take_profit_1 and float(take_profit_1) > 0:
            lines.append((y_of(float(take_profit_1)),
                          f"TP1 {_fmt_price(float(take_profit_1))} {_pct_delta(float(entry), float(take_profit_1))}",
                          _UP))
        if take_profit_2 and float(take_profit_2) > 0:
            lines.append((y_of(float(take_profit_2)),
                          f"TP2 {_fmt_price(float(take_profit_2))} {_pct_delta(float(entry), float(take_profit_2))}",
                          _UP))
        for y, _, col in lines:
            x = plot_l
            while x < plot_r:  # 虚线：6 实 4 虚
                draw.line([(x, y), (min(x + 6, plot_r), y)], fill=col, width=2)
                x += 10

        box_h = 16
        ordered = sorted(lines, key=lambda t: t[0])
        for (y, text, col), ty in zip(ordered, _place_label_ys([t[0] for t in ordered])):
            tw = draw.textlength(text, font=f12)
            draw.rectangle([plot_r - tw - 10, ty, plot_r, ty + box_h], fill=col)
            draw.text((plot_r - tw - 5, ty + 3), text, font=f12, fill=(255, 255, 255))

        # 标题行：币种 + 方向（左）/ 周期 + 根数（右）
        draw.text((_PAD_L, 10), f"{symbol}  {'LONG' if is_long else 'SHORT'}",
                  font=f15, fill=_TEXT)
        right1 = f"{_INTERVAL_LABEL.get(interval, interval.upper())} · {n}BAR"
        draw.text((_W - 10 - draw.textlength(right1, font=f12), 12),
                  right1, font=f12, fill=_MUTED)

        # 图例行：EMA 色块 + 周期（左）/ AI 评分 + 盈亏比（右）
        lx = _PAD_L
        for period, col in _EMA_STYLE:
            draw.rectangle([lx, 35, lx + 8, 42], fill=col)
            lab = f"EMA{period}"
            draw.text((lx + 12, 31), lab, font=f11, fill=_MUTED)
            lx += 12 + draw.textlength(lab, font=f11) + 14
        risk = abs(float(entry) - float(stop_loss)) if (entry and stop_loss) else 0
        rr = (abs(float(take_profit_1) - float(entry)) / risk) if (risk and take_profit_1) else None
        parts = [f"AI {float(ai_score):.0f}" if ai_score is not None else "",
                 f"RR 1:{rr:.1f}" if rr else ""]
        right2 = " · ".join(p for p in parts if p)
        if right2:
            draw.text((_W - 10 - draw.textlength(right2, font=f11), 31),
                      right2, font=f11, fill=_MUTED)

        # 底部时间轴（北京时间，与前端 fmtTime 同口径）
        for k in range(5):
            i = int((n - 1) * k / 4)
            lab = datetime.fromtimestamp(float(view[i][0]) / 1000, _BJ).strftime("%m-%d %H:%M")
            tw = draw.textlength(lab, font=f11)
            x = plot_l + (i + 0.5) * step
            draw.text((min(max(x - tw / 2, plot_l), plot_r - tw), plot_b + 8),
                      lab, font=f11, fill=_MUTED)

        buf = io.BytesIO()
        img.save(buf, format="PNG", optimize=True)
        return buf.getvalue()
    except Exception:
        logger.exception("K 线卡片图渲染失败 symbol=%s（降级纯文字卡片）", symbol)
        return None
