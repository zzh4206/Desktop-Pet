"""win 平台传感器（v0.1：鼠标/工作区；v0.3 补 EnumWindows 缓存；v0.6 补空闲）。

填充共享 BehaviorFSM 消费的 Sensors 数据源（设计思路 §2.2）：
- mouse_pos  —— GetCursorPos（ctypes，无需 pywin32/特权）
- work_area  —— SPI_GETWORKAREA（QScreen.availableGeometry 为 Qt 侧备选；
                多屏由 platform.py 对各屏取合集，此处先给主屏）
- idle_time  —— GetLastInputInfo（无需特权）
- windows    —— v0.3 起 EnumWindows + 事件/低频缓存，绝不每帧枚举（性能红线）

仅使用 ctypes，避免共享核心直接 import win32*（协作规则：平台依赖只在 shim 内）。
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes

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
    """主屏可用工作区（已排除任务栏），Sensors.work_area 数据源。

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
