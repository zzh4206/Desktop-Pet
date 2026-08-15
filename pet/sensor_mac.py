"""mac 平台传感器 —— 设计思路.md §2.2 Sensors 的数据源。

平台 API 隔离：平台调用只进 ``_mac`` 文件。``mouse_pos`` 用 ``QCursor.pos``
（全局 Qt 坐标，与 work_area 同系，免转换）；``work_area`` 用
``NSScreen.visibleFrame``（已排除 Dock + 菜单栏，多屏取合集），NS 坐标
(bottom-left origin) 翻转成 Qt (top-left origin)。无 pyobjc 时用
``QScreen.availableGeometry`` 兜底。

``idle_time`` / ``windows`` v0.1 不需要（v0.6/v0.3），占位。
"""

from __future__ import annotations

from .behavior import Sensors

try:
    from AppKit import NSScreen  # noqa: F401  仅探测可用性

    _HAS_PYOBJC = True
except Exception:
    _HAS_PYOBJC = False


def mouse_pos() -> tuple:
    from PySide6.QtGui import QCursor

    p = QCursor.pos()
    return (p.x(), p.y())


def work_area() -> dict:
    if _HAS_PYOBJC:
        try:
            return _work_area_ns()
        except Exception:
            pass
    return _work_area_qt()


def _work_area_ns() -> dict:
    screens = list(NSScreen.screens())
    if not screens:
        return _work_area_qt()
    # 主屏全屏高度（含菜单栏），用于 NS→Qt 翻转
    primary_h = float(screens[0].frame().size.height)

    min_x = min_y = float("inf")
    max_x = max_y = float("-inf")
    for s in screens:
        f = s.visibleFrame()
        ox, oy = float(f.origin.x), float(f.origin.y)
        ow, oh = float(f.size.width), float(f.size.height)
        min_x = min(min_x, ox)
        min_y = min(min_y, oy)
        max_x = max(max_x, ox + ow)
        max_y = max(max_y, oy + oh)

    qt_x = int(round(min_x))
    # NS union 的顶部(max_y) 翻转为 Qt 顶部；高 = max_y - min_y
    qt_y = int(round(primary_h - max_y))
    qt_w = int(round(max_x - min_x))
    qt_h = int(round(max_y - min_y))
    return {"x": qt_x, "y": qt_y, "width": qt_w, "height": qt_h}


def _work_area_qt() -> dict:
    from PySide6.QtGui import QGuiApplication

    screens = QGuiApplication.screens()
    if not screens:
        return {"x": 0, "y": 0, "width": 1440, "height": 900}
    min_x = min_y = float("inf")
    max_x = max_y = float("-inf")
    for s in screens:
        g = s.availableGeometry()
        min_x = min(min_x, g.x())
        min_y = min(min_y, g.y())
        max_x = max(max_x, g.x() + g.width())
        max_y = max(max_y, g.y() + g.height())
    return {
        "x": int(min_x),
        "y": int(min_y),
        "width": int(max_x - min_x),
        "height": int(max_y - min_y),
    }


def build_sensors() -> Sensors:
    return Sensors(
        mouse_pos=mouse_pos(),
        work_area=work_area(),
        windows=[],
        idle_time=0.0,
    )
