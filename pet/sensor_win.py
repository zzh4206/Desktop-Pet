"""win 平台传感器 —— 设计思路.md §2.2 Sensors 的数据源（平台适配与分工.md §六）。

填充共享 BehaviorFSM 消费的 Sensors：
- mouse_pos  —— QCursor.pos（全局 Qt 坐标，与 work_area 同系，免转换）
- work_area  —— QScreen.availableGeometry 多屏取合集（Qt top-left 原点，
                已排除任务栏；与 sensor_mac 的 Qt 兜底同实现，保证同系坐标）
- idle_time  —— GetLastInputInfo（ctypes，无需特权；供 v0.6+ 吃鼠标 idle gate）
- windows    —— v0.3 起 EnumWindows + 事件/低频缓存，绝不每帧枚举（性能红线）

平台 API 隔离：ctypes 只进本 ``_win`` 文件，不泄漏到共享层（协作规则）。
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes

from .behavior import Sensors

# ---- Windows API 绑定（仅本文件可见，不泄漏到共享层） ----

_user32 = ctypes.WinDLL("user32", use_last_error=True)


class _POINT(ctypes.Structure):
    _fields_ = [("x", wintypes.LONG), ("y", wintypes.LONG)]


class _LASTINPUTINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.UINT),
        ("dwTime", wintypes.DWORD),  # 上次输入的 tick（ms，系统启动起算）
    ]


class _RECT(ctypes.Structure):
    _fields_ = [
        ("left", wintypes.LONG),
        ("top", wintypes.LONG),
        ("right", wintypes.LONG),
        ("bottom", wintypes.LONG),
    ]


_user32.GetCursorPos.argtypes = [ctypes.POINTER(_POINT)]
_user32.GetCursorPos.restype = wintypes.BOOL

_user32.GetLastInputInfo.argtypes = [ctypes.POINTER(_LASTINPUTINFO)]
_user32.GetLastInputInfo.restype = wintypes.BOOL

_SPI_GETWORKAREA = 0x0030
_user32.SystemParametersInfoW.argtypes = [
    wintypes.UINT, wintypes.UINT, ctypes.c_void_p, wintypes.UINT,
]
_user32.SystemParametersInfoW.restype = wintypes.BOOL

_tick_count_ms = ctypes.windll.kernel32.GetTickCount64
_tick_count_ms.restype = ctypes.c_ulonglong


def get_mouse_pos() -> tuple[int, int]:
    """物理像素屏幕坐标（v0.3 高 DPI 适配时再统一换算 Qt 逻辑坐标）。"""
    pt = _POINT()
    if not _user32.GetCursorPos(ctypes.byref(pt)):
        raise ctypes.WinError(ctypes.get_last_error())
    return (pt.x, pt.y)


def get_idle_seconds() -> float:
    """系统空闲秒数（GetLastInputInfo，无需特权）。供吃鼠标 idle gate / 主动关怀。"""
    info = _LASTINPUTINFO(cbSize=ctypes.sizeof(_LASTINPUTINFO))
    if not _user32.GetLastInputInfo(ctypes.byref(info)):
        # 查询失败视为"活动中"，宁可不吃鼠标（安全铁律保守侧）
        return 0.0
    now = _tick_count_ms()
    return max(0.0, (now - info.dwTime) / 1000.0)


def get_primary_work_area() -> dict:
    """主屏可用工作区（已排除任务栏），SPI_GETWORKAREA（Qt 备选见 work_area）。

    多屏：v0.3 配合高 DPI Spike 扩展为 EnumDisplayMonitors 取合集。
    """
    rc = _RECT()
    ok = _user32.SystemParametersInfoW(
        _SPI_GETWORKAREA, 0, ctypes.byref(rc), 0
    )
    if not ok:
        # 兜底：整主屏（少见失败；Qt 侧 QScreen.availableGeometry 为备选实现）
        rc = _RECT(left=0, top=0, right=1920, bottom=1080)
    return {"left": rc.left, "top": rc.top,
            "right": rc.right, "bottom": rc.bottom}


def mouse_pos() -> tuple:
    """全局 Qt 坐标鼠标位置（与 sensor_mac 同实现，同系免转换）。"""
    from PySide6.QtGui import QCursor

    p = QCursor.pos()
    return (p.x(), p.y())


def work_area() -> dict:
    """多屏可用工作区合集（Qt top-left 原点，已排除任务栏）。

    与 sensor_mac._work_area_qt 同实现，保证双端 Sensors.work_area 同格式。
    """
    from PySide6.QtGui import QGuiApplication

    screens = QGuiApplication.screens()
    if not screens:
        return {"x": 0, "y": 0, "width": 1920, "height": 1080}
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
    """与 sensor_mac.build_sensors 对齐（app.py 经 platform.py 注入调用）。"""
    return Sensors(
        mouse_pos=mouse_pos(),
        work_area=work_area(),
        windows=[],
        idle_time=get_idle_seconds(),
    )
