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
import logging
import time
from ctypes import wintypes

from .behavior import Sensors

log = logging.getLogger("pet")  # 批次C/P3-12：枚举回调异常留痕用

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


class _MONITORINFOEX(ctypes.Structure):
    """批次F/M1（REVIEW-2026-08-31）：带 szDevice 的监视器信息——
    与 QScreen.name() 精确配对（混合 DPI 多屏坐标变换用）。"""
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("rcMonitor", _RECT),
        ("rcWork", _RECT),
        ("dwFlags", wintypes.DWORD),
        ("szDevice", wintypes.WCHAR * 32),
    ]


_MONITORENUMPROC = ctypes.WINFUNCTYPE(
    wintypes.BOOL, wintypes.HANDLE, wintypes.HANDLE,
    ctypes.POINTER(_RECT), wintypes.LPARAM,
)
_user32.EnumDisplayMonitors.argtypes = [
    wintypes.HANDLE, ctypes.POINTER(_RECT), _MONITORENUMPROC,
    wintypes.LPARAM,
]
_user32.EnumDisplayMonitors.restype = wintypes.BOOL


def _monitor_map(screens=None) -> list:
    """每屏 (QScreen, 物理 rcMonitor 矩形) 配对列表。

    批次F/M1（REVIEW-2026-08-31）：旧版物理原点按 ``sg.x()*dpr`` 近似，
    仅主屏/同 DPR 成立——混合缩放多屏（主 1.5× 副 1.0×）下副屏物理原点
    是主屏**物理**宽度（如 3840），近似式给出 2560，窗口逻辑坐标偏出
    数百 px。Win32 侧 EnumDisplayMonitors 的 rcMonitor 是物理像素真值，
    按 szDevice == QScreen.name() 精确配对后逐屏变换。
    配对失败/为空 → 返回 []（调用方回退旧近似，行为不劣化）。
    """
    from PySide6.QtGui import QGuiApplication

    if screens is None:
        screens = QGuiApplication.screens()
    by_name = {}
    for s in screens:
        by_name.setdefault(s.name(), s)
    out = []

    @_MONITORENUMPROC
    def _cb(hmon, _hdc, _rect, _data):
        # 批次C/P3-12（REVIEW-2026-09-05）：回调体包 try——ctypes 回调里
        # 的 Python 异常被吞且返回 0 = EnumDisplayMonitors 视为"停止枚举"，
        # 残缺配对表静默入缓存喂 FSM 几何判定
        try:
            mi = _MONITORINFOEX()
            mi.cbSize = ctypes.sizeof(_MONITORINFOEX)
            if _user32.GetMonitorInfoW(hmon, ctypes.byref(mi)):
                scr = by_name.get(mi.szDevice)
                if scr is not None:
                    out.append((scr, (mi.rcMonitor.left, mi.rcMonitor.top,
                                      mi.rcMonitor.right,
                                      mi.rcMonitor.bottom)))
        except Exception:
            log.warning("EnumDisplayMonitors 回调异常，跳过该屏", exc_info=True)
        return True

    try:
        _user32.EnumDisplayMonitors(None, None, _cb, 0)
    except Exception:
        return []
    return out


# 批次F/L17（REVIEW-2026-09-04）：监视器配对表 TTL 缓存——站窗顶时
# window_rect 每 50ms tick 调用，旧版每次都跑 QGuiApplication.screens()
# + EnumDisplayMonitors + 回调配对，20Hz 主线程 Win32 调用违背
# "绝不每帧枚举"红线精神（与 _WINDOWS_TTL_S 同拍）
_MONITOR_TTL_S = 2.0
_monitor_cache: tuple[float, list] = (0.0, [])


def _cached_monitor_map(screens=None) -> list:
    global _monitor_cache
    now = time.monotonic()
    ts, mons = _monitor_cache
    if now - ts < _MONITOR_TTL_S:
        return mons
    mons = _monitor_map(screens)
    # 批次C/P3-12（REVIEW-2026-09-05）：空结果同 TTL 缓存——旧版空结果不
    # 缓存，配对失败（降级态）下 window_rect/solid_at 每 50ms tick 全速重
    # 枚举，恰是 L17 缓存要消灭的模式
    _monitor_cache = (now, mons)
    return mons


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
    wintypes.HANDLE, ctypes.c_void_p   # _MONITORINFO/_MONITORINFOEX 通吃
]
_user32.WindowFromPoint.argtypes = [_POINT]
_user32.WindowFromPoint.restype = wintypes.HWND
_user32.IsWindow.argtypes = [wintypes.HWND]
_user32.IsWindow.restype = wintypes.BOOL
_user32.IsIconic.argtypes = [wintypes.HWND]
_user32.IsIconic.restype = wintypes.BOOL
_user32.GetWindowThreadProcessId.argtypes = [
    wintypes.HWND, ctypes.POINTER(wintypes.DWORD),
]
_user32.GetWindowThreadProcessId.restype = wintypes.DWORD

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
_kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
_kernel32.CloseHandle.restype = wintypes.BOOL

_GWL_STYLE = -16
_GWL_EXSTYLE = -20
_WS_CAPTION = 0x00C00000
_WS_THICKFRAME = 0x00040000
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


def _hwnd_to_rect(hwnd, screens=None, mons=None) -> dict | None:
    """物理像素窗口框 → Qt 逻辑坐标 {x,y,width,height}（高 DPI 缩放换算）。

    批次E/M6（REVIEW-2026-08-28）：screens 列表由调用方传入（枚举 30 窗
    不再每窗调一次 QGuiApplication.screens()）。
    批次F/M1（REVIEW-2026-08-31）：mons（_monitor_map 配对表）传入时用
    Win32 物理 rcMonitor 真值逐屏精确变换——旧版物理原点按 sg.x()*dpr
    近似，混合缩放多屏（主 1.5× 副 1.0×）下副屏偏移数百 px；dpr 取
    物理宽/逻辑宽实测比值（自洽于 Win32 物理坐标系，不受 Qt 取整影响）。
    """
    from PySide6.QtCore import QPoint
    from PySide6.QtGui import QGuiApplication

    rc = _RECT()
    if not _user32.GetWindowRect(hwnd, ctypes.byref(rc)):
        return None
    if screens is None:
        screens = QGuiApplication.screens()
    if mons is None:
        mons = _cached_monitor_map(screens)   # L17：TTL 缓存（站窗顶 20Hz 路径）
    tl = QPoint(rc.left, rc.top)
    br = QPoint(rc.right, rc.bottom)
    # 物理像素→逻辑像素（Win32 坐标是物理系，Qt 工作区/FSM 是逻辑系）；
    # 按窗口中心点归属监视器（跨屏窗取中心所在屏）
    cx, cy = (rc.left + rc.right) // 2, (rc.top + rc.bottom) // 2
    for screen, (pl, pt, pr, pb) in mons:
        if pl - 8 <= cx < pr + 8 and pt - 8 <= cy < pb + 8:
            sg = screen.geometry()  # 逻辑坐标
            dpr = (pr - pl) / max(1, sg.width())
            tl = QPoint(int(sg.x() + (rc.left - pl) / dpr),
                        int(sg.y() + (rc.top - pt) / dpr))
            br = QPoint(int(sg.x() + (rc.right - pl) / dpr),
                        int(sg.y() + (rc.bottom - pt) / dpr))
            return {
                "x": tl.x(), "y": tl.y(),
                "width": br.x() - tl.x(), "height": br.y() - tl.y(),
            }
    # 回退：旧近似（配对失败时行为不劣化；主屏/同 DPR 场景与原式等价）
    for screen in screens:
        sg = screen.geometry()
        dpr = screen.devicePixelRatio()
        px0, py0 = int(sg.x() * dpr), int(sg.y() * dpr)
        if (rc.left >= px0 - 8 and rc.left < px0 + sg.width() * dpr
                and rc.top >= py0 - 8 and rc.top < py0 + sg.height() * dpr):
            tl = QPoint(int(sg.x() + (rc.left - px0) / dpr),
                        int(sg.y() + (rc.top - py0) / dpr))
            br = QPoint(int(sg.x() + (rc.right - px0) / dpr),
                        int(sg.y() + (rc.bottom - py0) / dpr))
            return {
                "x": tl.x(), "y": tl.y(),
                "width": br.x() - tl.x(), "height": br.y() - tl.y(),
            }
    # 批次F/L17（REVIEW-2026-09-04）：两级兜底都未命中（窗口完全离屏）——
    # 旧版把物理坐标当逻辑坐标返回，混合 DPI 下差 1.5× 污染 FSM 几何判定；
    # 返回 None（调用方 visible_windows/window_rect 均已按 None 跳过）
    return None


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
    if not refresh and now - _windows_cache_at < _WINDOWS_TTL_S:
        return _windows_cache

    found: list[dict] = []
    # 批次E/M6：screens 单次取用（旧版每窗在 _hwnd_to_rect 里各调一次
    # QGuiApplication.screens() 并遍历全部屏——30 窗 × N 屏的 Python 循环
    # 每 2s 在主线程脉冲执行）
    from PySide6.QtGui import QGuiApplication
    screens = QGuiApplication.screens()
    # 批次F/M1：监视器物理矩形配对表同样单次取用（混合 DPI 精确变换）
    mons = _monitor_map(screens)

    @_WNDENUMPROC
    def _on_hwnd(hwnd, _lparam):
        # 批次C/P3-12（REVIEW-2026-09-05）：回调体包 try——ctypes 回调里
        # 的 Python 异常被吞且返回 0 = EnumWindows 视为"停止枚举"，残缺窗
        # 表静默缓存 2s（Qt 收尾期窗口对象回收等窄路径可触发）
        try:
            if not _user32.IsWindowVisible(hwnd):
                return True
            ex_style = _user32.GetWindowLongW(hwnd, _GWL_EXSTYLE)
            if ex_style & (_WS_EX_TOOLWINDOW | _WS_EX_NOACTIVATE):
                return True
            if _is_cloaked(hwnd):
                return True
            rect = _hwnd_to_rect(hwnd, screens, mons)
            if rect and rect["width"] > 40 and rect["height"] > 40:
                rect["hwnd"] = int(hwnd)
                found.append(rect)
        except Exception:
            log.warning("EnumWindows 回调异常，跳过该窗", exc_info=True)
        return True

    _user32.EnumWindows(_on_hwnd, 0)
    _windows_cache = found
    _windows_cache_at = now
    return found


_own_hwnds: set = set()


def set_own_hwnds(hwnds) -> None:
    """登记宠物自身窗口（本体/气泡）hwnd——图层探针被自身遮挡时放行。

    宠物站窗顶时，探针点（窗顶下 5px）落在宠物身体覆盖范围内，
    WindowFromPoint 会命中宠物自己 → 身份比对失败 → 支撑被误否决
    （表现为：登顶即掉/站不稳/支撑窗记录丢失后悬空）。"""
    global _own_hwnds
    _own_hwnds = {int(h) for h in hwnds if h}


_user32.GetWindow.argtypes = [wintypes.HWND, wintypes.UINT]
_user32.GetWindow.restype = wintypes.HWND

_GW_HWNDNEXT = 2


def _window_below_point(start_hwnd, pt):
    """从 start_hwnd 起沿 Z 序向下，找第一个矩形覆盖物理点 pt 的可见窗。

    宠物窗口挡住探针时用：跳过自身/其他 own 窗与不可见窗，物理坐标直测。"""
    h = start_hwnd
    for _ in range(64):
        h = _user32.GetWindow(h, _GW_HWNDNEXT)
        if not h:
            return None
        if int(h) in _own_hwnds:
            continue
        if not _user32.IsWindowVisible(h):
            continue
        rc = _RECT()
        if _user32.GetWindowRect(h, ctypes.byref(rc)):
            if rc.left <= pt.x < rc.right and rc.top <= pt.y < rc.bottom:
                return h
    return None


def solid_at(x: float, y: float, ref: dict | None = None) -> bool:
    """图层双检查（FSM 攀爬/落点二次确认）：

    - ref=None：逻辑坐标 (x,y) 处是否有枚举到的实体窗口（任意命中即可）；
    - ref=候选窗 dict：该点**最顶层**实体窗是否就是 ref（比对 hwnd）——
      候选窗被别的窗盖住（如全屏窗在前）→ 返回 False，否决攀爬/落顶。
    - 命中宠物/气泡自身（已登记）→ 按几何候选覆盖判断放行（自身不算遮挡）。
    逻辑→物理坐标按所在屏 DPR 换算（多屏不同缩放逐屏判断）。
    """
    from PySide6.QtGui import QGuiApplication

    screens = QGuiApplication.screens()
    pt = None
    # 批次F/M1：逻辑→物理同样按监视器配对表精确变换——旧版 x*dpr 默认
    # 原点 (0,0)，副屏（逻辑原点非零）探针点系统性错位
    for screen, (pl, pt_, pr, pb) in _cached_monitor_map(screens):
        sg = screen.geometry()
        if (sg.x() <= x < sg.x() + sg.width()
                and sg.y() <= y < sg.y() + sg.height()):
            dpr = (pr - pl) / max(1, sg.width())
            pt = _POINT(int(pl + (x - sg.x()) * dpr),
                        int(pt_ + (y - sg.y()) * dpr))
            break
    if pt is None:
        # 回退：旧近似（配对失败/点在屏外）
        dpr = 1.0
        for screen in screens:
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
    root_i = int(root)
    if root_i in _own_hwnds:
        # 探针被宠物自身遮挡：沿 Z 序向下找宠物底下真正承接该点的窗口，
        # 再做身份比对——站窗顶时底下=支撑窗(通过)；攀爬被全屏盖住的窗时
        # 底下=全屏窗≠候选(否决)。不能用"几何候选覆盖即放行"的捷径（会
        # 重新放行被盖住的窗）。
        below = _window_below_point(root, pt)
        if below is None:
            return False  # 宠物底下无实体窗（悬空/纯桌面）
        if ref is not None:
            return int(below) == ref.get("hwnd")
        return int(below) in {w.get("hwnd") for w in visible_windows()}
    if ref is not None:
        return root_i == ref.get("hwnd")
    # P3-23（REVIEW-2026-09-05）：ref=None 分支用 2s 枚举缓存——新开 <2s
    # 的窗会误否决攀爬；mac 端同分支对任意实体窗返 True。方向保守（宁可
    # 拒绝攀爬），平台差异固化于此注释，FSM 测试假件按 mac 语义建模。
    valid = {w.get("hwnd") for w in visible_windows()}
    return root_i in valid


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


def foreground_process_name() -> str:
    """前台进程名（不要求全屏）——M8 修：活跃内容检测用。

    foreground_fullscreen 只在全屏时返回进程名（窗口化播放视频漏检），
    此函数无条件返回（GetForegroundWindow→pid→QueryFullProcessImageName）。
    """
    hwnd = _user32.GetForegroundWindow()
    if not hwnd:
        return ""
    return _process_name(_window_pid(hwnd))


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
    # 批次B/P2-3（REVIEW-2026-09-05）：最大化窗口误判全屏——自动隐藏任务
    # 栏下工作区=整屏，任意最大化前台窗口的矩形（Win32 最大化带 ±8px 隐形
    # 边框缓冲）即覆盖整屏 → 宠物被无限期隐藏。带标题栏/厚边框样式的一律
    # 不是无边框全屏（游戏/放映/F11 全屏无这两个样式，照常判定）。
    # cloaked 前台窗（UWP 挂起/虚拟桌面壳）同样排除，与枚举口径一致。
    style = _user32.GetWindowLongW(hwnd, _GWL_STYLE)
    if style & (_WS_CAPTION | _WS_THICKFRAME):
        return (False, "")
    if _is_cloaked(hwnd):
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


def get_idle_seconds() -> float:
    """系统空闲秒数（GetLastInputInfo，无需特权）。供吃鼠标 idle gate / 主动关怀。"""
    info = _LASTINPUTINFO(cbSize=ctypes.sizeof(_LASTINPUTINFO))
    if not _user32.GetLastInputInfo(ctypes.byref(info)):
        # 查询失败视为"活动中"，宁可不吃鼠标（安全铁律保守侧）
        return 0.0
    # M1 修：统一 32 位域防 uptime>49.7 天回绕（64bit GetTickCount64 减
    # 32bit dwTime 差值恒偏大）——两侧 &0xFFFFFFFF 后减法回绕天然正确
    now32 = _tick_count_ms() & 0xFFFFFFFF
    return ((now32 - info.dwTime) & 0xFFFFFFFF) / 1000.0


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


def screen_rects() -> list[dict]:
    """逐屏 availableGeometry（Qt top-left 原点，已排除任务栏）。

    批次B/P2-7（REVIEW-2026-09-05）：bounding-box 工作区在不等高/纵向错位
    多屏下含无屏死区——FSM 游走目标/坠落屏底按屏取几何（behavior 端消费），
    双端同实现保证 Sensors.screen_rects 同格式。
    """
    from PySide6.QtGui import QGuiApplication

    out = []
    for s in QGuiApplication.screens():
        g = s.availableGeometry()
        out.append({
            "x": int(g.x()),
            "y": int(g.y()),
            "width": int(g.width()),
            "height": int(g.height()),
        })
    return out


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
        screen_rects=screen_rects(),
    )
