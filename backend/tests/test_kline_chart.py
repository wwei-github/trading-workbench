# -*- coding: utf-8 -*-
"""飞书卡片内嵌 K 线图渲染（2026-10-15，kline_chart.render_plan_chart）

纯函数、不触网：合成 K 线 → 断言 PNG 输出与降级路径。图内文字全 ASCII
（容器镜像无 CJK 字体），中文一律留在卡片 lark_md 元素里。
"""
import io
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.kline_chart import (  # noqa: E402
    _PAD_B,
    _PAD_T,
    _place_label_ys,
    render_plan_chart,
)


def _klines(n: int = 300, base: float = 100.0) -> list:
    """正弦走位 + 对称影线（涨跌两根都有，覆盖双色绘制分支）"""
    out = []
    ts0 = 1_700_000_000_000
    for i in range(n):
        c = base * (1 + 0.02 * math.sin(i / 7))
        o = c * (1 + 0.004 * math.cos(i / 5))
        out.append([ts0 + i * 3_600_000, o, max(o, c) * 1.005,
                    min(o, c) * 0.995, c, 1000.0 + i])
    return out


def _plan(**kw) -> dict:
    base = dict(symbol="KSMUSDT", direction="long", entry=100.0, stop_loss=97.0,
                take_profit_1=106.0, take_profit_2=110.0, ai_score=75)
    base.update(kw)
    return base


def test_render_png_dimensions_and_plan_lines():
    """正常渲染：PNG 魔数 + 画布尺寸 + 文件非平凡（K线/EMA/计划线都画上了）"""
    png = render_plan_chart(_klines(), **_plan())
    assert png and png[:8] == b"\x89PNG\r\n\x1a\n"
    assert len(png) > 4000

    from PIL import Image
    img = Image.open(io.BytesIO(png))
    assert img.size == (780, 470) and img.format == "PNG"


def test_render_short_and_long_direction_variants():
    """空单同样可渲染（方向色/入场标签走另一分支）；TP2 为 0/None 时跳过该线"""
    for direction in ("long", "short"):
        png = render_plan_chart(_klines(200), **_plan(direction=direction))
        assert png and png[:8] == b"\x89PNG\r\n\x1a\n"
    assert render_plan_chart(_klines(200), **_plan(take_profit_2=0)) is not None
    assert render_plan_chart(_klines(200), **_plan(take_profit_2=None)) is not None


def test_render_degrades_on_bad_input():
    """数据不足 / 空数据 / 价格无跨度 → None（调用方降级纯文字卡片）"""
    assert render_plan_chart([], **_plan()) is None
    assert render_plan_chart(_klines(10), **_plan()) is None
    # 全平价格（hi == lo，除零）→ None 而非抛错
    flat = [[i * 3_600_000, 100.0, 100.0, 100.0, 100.0, 1.0] for i in range(40)]
    assert render_plan_chart(flat, **_plan(entry=0, stop_loss=0,
                                           take_profit_1=0)) is None


def test_render_tolerates_short_ema_history():
    """K 线不足 EMA144 递推下限（155 根）时 EMA144 为 None：跳线不报错"""
    png = render_plan_chart(_klines(60), **_plan())
    assert png and png[:8] == b"\x89PNG\r\n\x1a\n"


def test_render_plan_far_from_price_range():
    """计划线远在可视区间外（如 TP2 +30%）也照样渲染（纵轴纳入计划价）"""
    png = render_plan_chart(_klines(150), **_plan(take_profit_1=140.0,
                                                  take_profit_2=170.0))
    assert png and png[:8] == b"\x89PNG\r\n\x1a\n"


def test_place_label_ys_no_overlap_and_inside_plot():
    """2026-10-15 修复：标签簇贴底时整组上抬，仍互不重叠且不出画布。

    复现原 bug 的输入（极端行情 130 根区间 200% + 紧止损：期望位置全挤在底边，
    旧实现错开后逐个钳位到同一 y → 3 对重叠）；正常分散输入行为不变。
    """
    bot = 470 - _PAD_B
    box_h = 16

    def _ok(tys):
        assert all(abs(tys[i] - tys[j]) >= box_h
                   for i in range(len(tys)) for j in range(i + 1, len(tys)))
        assert min(tys) >= _PAD_T and max(tys) + box_h <= bot

    _ok(_place_label_ys([400.6, 418.0, 430.0, 440.0]))   # 贴底簇（原 bug 场景）
    _ok(_place_label_ys([420.0, 421.0, 422.0, 423.0]))   # 四点几乎同一价
    _ok(_place_label_ys([64.9, 204.2, 343.5, 413.1]))    # 正常分散（不改行为）
    # 单标签：原样落在价格线上（不因对齐逻辑整体偏移）
    assert _place_label_ys([300.0]) == [300.0 - box_h / 2]


def test_render_extreme_range_with_tight_stop():
    """极端行情（130 根区间 200%、止损 0.4%）仍能出图（标签整组上抬不留重叠）"""
    kl = _klines(130, base=100.0)
    # 把可视区间拉到 ~200% 宽：前 100 根压到 40，后 30 根拉到 200 附近
    for i, k in enumerate(kl):
        f = 0.4 if i < 100 else 2.0
        kl[i] = [k[0], k[1] * f, k[2] * f, k[3] * f, k[4] * f, k[5]]
    png = render_plan_chart(kl, **_plan(entry=100.0, stop_loss=99.6,
                                        take_profit_1=103.0, take_profit_2=106.0))
    assert png and png[:8] == b"\x89PNG\r\n\x1a\n"
