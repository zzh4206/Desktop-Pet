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

# ---- v0.3：窗口枚举 / 全屏检测（EnumWindows + DWM，均无需特权） ----

_dwmapi = ctypes.WinDLL("dwmapi")
_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)


class _MONITORINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("rcMonitor", _RECT),
        ("rcWork", _RECT),
        ("dwFlags", wintypes.DWORD),
    ]


_WNDENUMPROC = ctypes.WINFUNCTYPE(
    wintypes.BOOL, wintypes.HWND, wintypes.LPARAM
)

_user32.EnumWindows.argtypes = [_WNDENUMPROC, wintypes.LPARAM]
_user32.IsWindowVisible.argtypes = [wintypes.HWND]
_user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(_RECT)]
_user32.GetWindowLongW.argtypes = [wintypes.HWND, wintypes.INT]
_user32.GetWindowLongW.restype = ctypes.c_long
_user32.GetForegroundWindow.restype = wintypes.HWND
_user32.MonitorFromWindow.restype = wintypes.HANDLE
_user32.MonitorFromWindow.argtypes = [wintypes.HWND, wintypes.DWORD]
_user32.GetMonitorInfoW.argtypes = [
    wintypes.HANDLE, ctypes.POINTER(_MONITORINFO)
]
_user32.WindowFromPoint.argtypes = [_POINT]
_user32.WindowFromPoint.restype = wintypes.HWND

# 子控件句柄 → 顶层窗口（WindowFromPoint 可能命中窗口内的控件）
_GA_ROOT = 2
_user32.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
_user32.GetAncestor.restype = wintypes.HWND
_kernel32.OpenProcess.restype = ctypes.c_void_p
_kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
_kernel32.QueryFullProcessImageNameW.argtypes = [
    ctypes.c_void_p, wintypes.DWORD,
    ctypes.POINTER(wintypes.DWORD), wintypes.LPWSTR,
]

_GWL_STYLE = -16
_GWL_EXSTYLE = -20
_WS_EX_TOOLWINDOW = 0x00000080
_WS_EX_NOACTIVATE = 0x08000000
_DWMWA_CLOAKED = 14
_MONITOR_DEFAULTTONEAREST = 2
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000


def _is_cloaked(hwnd) -> bool:
    """UWP 挂起/虚拟桌面窗口被 DWM 'cloaked'——枚举到但不可见，需排除。"""
    cloaked = wintypes.DWORD()
    hr = _dwmapi.DwmGetWindowAttribute(
        hwnd, _DWMWA_CLOAKED, ctypes.byref(cloaked), ctypes.sizeof(cloaked)
    )
    return bool(hr == 0 and cloaked.value)


def _hwnd_to_rect(hwnd) -> dict | None:
    """物理像素窗口框 → Qt 逻辑坐标 {x,y,width,height}（高 DPI 缩放换算）。"""
    from PySide6.QtCore import QPoint
    from PySide6.QtGui import QGuiApplication

    rc = _RECT()
    if not _user32.GetWindowRect(hwnd, ctypes.byref(rc)):
        return None
    tl = QPoint(rc.left, rc.top)
    br = QPoint(rc.right, rc.bottom)
    # 物理像素→逻辑像素：借屏幕的 devicePixelRatio 换算（Win32 坐标是物理系，
    # Qt 工作区/FSM 是逻辑系；跨屏不同缩放率时按窗口所在屏的 DPI 换算）
    for screen in QGuiApplication.screens():
        sg = screen.geometry()  # 逻辑坐标
        # 用物理区判断归属：geometry*dpr 即该屏物理区
        dpr = screen.devicePixelRatio()
        phys = (
            int(sg.x() * dpr), int(sg.y() * dpr),
            int((sg.x() + sg.width()) * dpr),
            int((sg.y() + sg.height()) * dpr),
        )
        if rc.left >= phys[0] - 8 and rc.left < phys[2] and rc.top >= phys[1] - 8 and rc.top < phys[3]:
            tl = QPoint(int(rc.left / dpr), int(rc.top / dpr))
            br = QPoint(int(rc.right / dpr), int(rc.bottom / dpr))
            break
    return {
        "x": tl.x(), "y": tl.y(),
        "width": br.x() - tl.x(), "height": br.y() - tl.y(),
    }


_windows_cache: list[dict] = []
_windows_cache_at: float = 0.0
_WINDOWS_TTL_S = 2.0  # ≤2s 刷新（性能红线：绝不每帧枚举）


def visible_windows(refresh: bool = False) -> list[dict]:
    """其他可见顶层窗口框（逻辑坐标），供 v0.3 WANDER 窗口顶面走/攀爬留后。

    缓存 TTL 2s；app.py 的 2s 传感器 timer 恰好命中缓存节奏，FSM 快 tick
    不触发 Win32 枚举。排除：不可见/cloaked/工具窗/最小化/自身零面积。
    每项含 hwnd（图层双检查用；mac 端无此键，FSM 不依赖）。
    """
    global _windows_cache, _windows_cache_at
    now = _tick_count_ms() / 1000.0
    if not refresh and _windows_cache and now - _windows_cache_at < _WINDOWS_TTL_S:
        return _windows_cache

    found: list[dict] = []

    @_WNDENUMPROC
    def _on_hwnd(hwnd, _lparam):
        if not _user32.IsWindowVisible(hwnd):
            return True
        ex_style = _user32.GetWindowLongW(hwnd, _GWL_EXSTYLE)
        if ex_style & (_WS_EX_TOOLWINDOW | _WS_EX_NOACTIVATE):
            return True
        if _is_cloaked(hwnd):
            return True
        rect = _hwnd_to_rect(hwnd)
        if rect and rect["width"] > 40 and rect["height"] > 40:
            rect["hwnd"] = int(hwnd) if hasattr(hwnd, "value") else int(hwnd)
            found.append(rect)
        return True

    _user32.EnumWindows(_on_hwnd, 0)
    _windows_cache = found
    _windows_cache_at = now
    return found


def solid_at(x: float, y: float, ref: dict | None = None) -> bool:
    """图层双检查（FSM 攀爬/落点二次确认）：

    - ref=None：逻辑坐标 (x,y) 处是否有枚举到的实体窗口（任意命中即可）；
    - ref=候选窗 dict：该点**最顶层**实体窗是否就是 ref（比对 hwnd）——
      候选窗被别的窗盖住（如全屏窗在前）→ 返回 False，否决攀爬/落顶。
    宠物自身/气泡是 NOACTIVATE/TOOL 窗不在有效集，不会误报。
    逻辑→物理坐标按所在屏 DPR 换算（多屏不同缩放逐屏判断）。
    """
    from PySide6.QtGui import QGuiApplication

    dpr = 1.0
    for screen in QGuiApplication.screens():
        sg = screen.geometry()
        if (sg.x() <= x < sg.x() + sg.width()
                and sg.y() <= y < sg.y() + sg.height()):
            dpr = screen.devicePixelRatio()
            break
    pt = _POINT(int(x * dpr), int(y * dpr))
    hwnd = _user32.WindowFromPoint(pt)
    if not hwnd:
        return False
    root = _user32.GetAncestor(hwnd, _GA_ROOT) or hwnd  # 子控件 → 顶层
    if ref is not None:
        return int(root) == ref.get("hwnd")
    valid = {w.get("hwnd") for w in visible_windows()}
    return int(root) in valid


def _process_name(pid: int) -> str:
    """进程名（如 POWERPNT.EXE），供演示模式白名单判断。best-effort。"""
    h = _kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(512)
        size = wintypes.DWORD(512)
        if _kernel32.QueryFullProcessImageNameW(h, 0, ctypes.byref(size), buf):
            return buf.value.rsplit("\\", 1)[-1].upper()
        return ""
    finally:
        _kernel32.CloseHandle(h)


def _window_pid(hwnd) -> int:
    pid = wintypes.DWORD()
    _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return pid.value


def _window_class(hwnd) -> str:
    """窗口类名（排除桌面窗口用）。"""
    buf = ctypes.create_unicode_buffer(64)
    n = _user32.GetClassNameW(hwnd, buf, 64)
    return buf.value[:n] if n else ""


# 桌面相关窗口类：Progman(桌面)/WorkerW(壁纸层)。桌面框选/点空白时前台
# 是它们，虽"覆盖整屏"但不是全屏应用——不触发宠物隐藏（v0.3.11）。
_DESKTOP_CLASSES = {"Progman", "WorkerW"}

_user32.GetClassNameW.argtypes = [
    wintypes.HWND, wintypes.LPWSTR, ctypes.c_int,
]
_user32.GetClassNameW.restype = ctypes.c_int


def foreground_fullscreen() -> tuple[bool, str]:
    """前台窗口是否全屏（覆盖其所在显示器整块）。

    桌面窗口（Progman/WorkerW）不算——桌面框选/点桌面空白时前台虽是
    覆盖整屏的桌面层，但不是全屏应用（否则框选含宠物会导致宠物消失）。
    返回 (is_fullscreen, 进程名)。供 v0.3 全屏/演示检测：FSM 收到 True 时
    隐藏或移副屏 + 暂停 WANDER；演示软件（PowerPoint 等）前台全屏则完全
    隐藏 + 禁吃鼠标（白名单判断在共享层 config，本函数只给事实）。
    """
    hwnd = _user32.GetForegroundWindow()
    if not hwnd:
        return (False, "")
    if _window_class(hwnd) in _DESKTOP_CLASSES:
        return (False, "")
    rc = _RECT()
    if not _user32.GetWindowRect(hwnd, ctypes.byref(rc)):
        return (False, "")
    mon = _user32.MonitorFromWindow(hwnd, _MONITOR_DEFAULTTONEAREST)
    mi = _MONITORINFO(cbSize=ctypes.sizeof(_MONITORINFO))
    if not _user32.GetMonitorInfoW(mon, ctypes.byref(mi)):
        return (False, "")
    fs = (
        rc.left <= mi.rcMonitor.left
        and rc.top <= mi.rcMonitor.top
        and rc.right >= mi.rcMonitor.right
        and rc.bottom >= mi.rcMonitor.bottom
    )
    return (fs, _process_name(_window_pid(hwnd)) if fs else "")


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


def window_alive(ref: dict) -> bool:
    """窗口实时存活（未关闭/未最小化），O(1)。

    FSM 攀爬/站立支撑每 tick 调用——2s 枚举缓存对"最小化正在爬的窗"延迟
    太大，IsWindow+IsIconic 立即可判。ref 无 hwnd（mac/测试）→ True。"""
    hwnd = (ref or {}).get("hwnd")
    if not hwnd:
        return True
    return bool(_user32.IsWindow(hwnd)) and not bool(_user32.IsIconic(hwnd))


def window_rect(ref: dict) -> dict | None:
    """支撑窗实时矩形（逻辑坐标），O(1) GetWindowRect 新鲜读。

    FSM 站窗顶时每 tick 调用：窗口上/下移即时骑乘跟随、移走即坠落，
    消除 2s 枚举缓存不敏感。ref 无 hwnd/窗口已亡 → None（走几何兜底）。"""
    hwnd = (ref or {}).get("hwnd")
    if not hwnd or not _user32.IsWindow(hwnd):
        return None
    return _hwnd_to_rect(hwnd)


def build_sensors() -> Sensors:
    """与 sensor_mac.build_sensors 对齐（app.py 经 platform.py 注入调用）。

    solid_at：win 端图层双检查（FSM 攀爬/落点二次确认）；
    alive_at：win 端窗口实时存活检查（攀爬掉落去延迟）；
    rect_at：win 端支撑窗实时矩形（骑乘跟随移动窗口）。
    mac 端暂缺 → None（FSM 退纯几何判定）。"""
    return Sensors(
        mouse_pos=mouse_pos(),
        work_area=work_area(),
        windows=visible_windows(),
        idle_time=get_idle_seconds(),
        solid_at=solid_at,
        alive_at=window_alive,
        rect_at=window_rect,
    )
