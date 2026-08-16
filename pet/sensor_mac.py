"""mac 平台传感器 —— 设计思路.md §2.2 Sensors 的数据源。

平台 API 隔离：平台调用只进 ``_mac`` 文件。``mouse_pos`` 用 ``QCursor.pos``
（全局 Qt 坐标，与 work_area 同系，免转换）；``work_area`` 用
``NSScreen.visibleFrame``（已排除 Dock + 菜单栏，多屏取合集），NS 坐标
(bottom-left origin) 翻转成 Qt (top-left origin)。无 pyobjc 时用
``QScreen.availableGeometry`` 兜底。

v0.3：``windows``（窗口框枚举）+ 全屏检测。用 ``Quartz.CGWindowListCopyWindowInfo``
（不需 Accessibility 权限，直接给窗口 bounds；与 AXUIElement 比，v0.3 不攀爬、
只需框 + 全屏判定，CGWindowList 更合适且免权限——AXUIElement 留将来攀爬逐 app
窗口树）。**缓存 ≤2s**（``build_sensors`` 由 app 2s timer 调，绝不每帧），
``idle_time`` v0.6。
"""

from __future__ import annotations

import time

from .behavior import Sensors

try:
    from AppKit import NSScreen  # noqa: F401  仅探测可用性

    _HAS_PYOBJC = True
except Exception:
    _HAS_PYOBJC = False

try:
    from Quartz import (
        CGWindowListCopyWindowInfo,
        kCGNullWindowID,
        kCGWindowListOptionOnScreenOnly,
    )

    _HAS_QUARTZ = True
except Exception:
    _HAS_QUARTZ = False


# 窗口枚举缓存（≤2s；app 的 _sensor_timer 2s 调 build_sensors，命中缓存即返回）
_WINDOWS_CACHE: list[dict] = []
_WINDOWS_CACHE_TS: float = 0.0
_WINDOWS_TTL = 2.0

# solid_at/alive_at 专用短缓存（200ms）：比 enumerate_windows 2s 实时——
# 幽灵窗（已关闭/移动）200ms 内移除，防"识别不存在的边框爬上去又掉"；
# 不每次 CopyWindowInfo（_surface_y 每 cand 调 solid_at，走缓存查）
_SOLID_CACHE: list[dict] = []
_SOLID_CACHE_TS: float = 0.0
_SOLID_TTL = 0.2

# 全屏判定时忽略的 owner（桌面/菜单栏/Dock/Window Server）
_DESKTOP_OWNERS = {"Window Server", "Dock", "程序坞", "Finder", "ControlCenter"}


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


def _screen_full_frames() -> list[tuple[int, int, int, int]]:
    """各屏**全 frame**（含菜单栏区域，Y=0；用于全屏判定——全屏窗覆盖全 frame
    而非 visibleFrame）。NS→Qt 翻转。无 pyobjc 时用 QScreen.geometry 兜底。"""
    frames = []
    if _HAS_PYOBJC:
        try:
            screens = list(NSScreen.screens())
            primary_h = (
                float(screens[0].frame().size.height) if screens else 0.0
            )
            for s in screens:
                f = s.frame()  # 全屏 frame（含菜单栏）
                w, h = float(f.size.width), float(f.size.height)
                x, oy = float(f.origin.x), float(f.origin.y)
                frames.append((int(round(x)), int(round(primary_h - oy - h)),
                               int(round(w)), int(round(h))))
            return frames
        except Exception:
            pass
    from PySide6.QtGui import QGuiApplication

    for s in QGuiApplication.screens():
        g = s.geometry()
        frames.append((g.x(), g.y(), g.width(), g.height()))
    return frames


def _is_fullscreen_bounds(b, screen_frames) -> bool:
    """窗口 bounds 是否覆盖某屏全 frame（全屏判定，非 visibleFrame）。"""
    x, y = int(b["X"]), int(b["Y"])
    w, h = int(b["Width"]), int(b["Height"])
    for (sx, sy, sw, sh) in screen_frames:
        if x <= sx + 2 and y <= sy + 2 and w >= sw - 2 and h >= sh - 2:
            return True
    return False


def _enumerate_windows_uncached() -> list[dict]:
    """CGWindowList 枚举可见窗口框（Qt 坐标），过滤桌面/Dock/菜单栏。
    返回 list of {x,y,width,height,owner,wid}。无 Quartz → []。
    wid = kCGWindowNumber（窗口唯一 ID，作 hwnd 等价供 solid_at/alive_at 比对）。"""
    if not _HAS_QUARTZ:
        return []
    out: list[dict] = []
    try:
        wins = CGWindowListCopyWindowInfo(
            kCGWindowListOptionOnScreenOnly, kCGNullWindowID
        )
    except Exception:
        return []
    for w in wins:
        try:
            layer = int(w.get("kCGWindowLayer", 0))
        except (TypeError, ValueError):
            layer = 0
        if layer != 0:
            # 菜单栏/Dock/桌面层（正层）与负层都不算普通窗口
            continue
        owner = w.get("kCGWindowOwnerName", "")
        if owner in _DESKTOP_OWNERS:
            continue
        b = w.get("kCGWindowBounds")
        if not b:
            continue
        try:
            x, y = int(b["X"]), int(b["Y"])
            ww, hh = int(b["Width"]), int(b["Height"])
            wid = int(w.get("kCGWindowNumber", 0))
        except (KeyError, TypeError, ValueError):
            continue
        if ww < 40 or hh < 40:
            continue
        out.append({"x": x, "y": y, "width": ww, "height": hh,
                    "owner": str(owner), "wid": wid})
    return out


def _solid_windows() -> list[dict]:
    """CGWindowList 实体窗短缓存（200ms；含 wid/owner/bounds/layer，过滤桌面/
    Dock/小窗），供 solid_at/alive_at 用。比 enumerate_windows 2s 实时（幽灵窗
    200ms 内移除）；比每次 CopyWindowInfo 快（_surface_y 每 cand 调 solid_at 走
    缓存查，不重复枚举）。无 Quartz → []。"""
    global _SOLID_CACHE, _SOLID_CACHE_TS
    now = time.monotonic()
    if _SOLID_CACHE and (now - _SOLID_CACHE_TS) < _SOLID_TTL:
        return _SOLID_CACHE
    out: list[dict] = []
    if _HAS_QUARTZ:
        try:
            wins = CGWindowListCopyWindowInfo(
                kCGWindowListOptionOnScreenOnly, kCGNullWindowID
            )
        except Exception:
            wins = []
        for w in wins:
            try:
                layer = int(w.get("kCGWindowLayer", 0))
            except (TypeError, ValueError):
                layer = 0
            if layer != 0:
                continue
            owner = w.get("kCGWindowOwnerName", "")
            if owner in _DESKTOP_OWNERS:
                continue
            b = w.get("kCGWindowBounds")
            if not b:
                continue
            try:
                wx, wy = int(b["X"]), int(b["Y"])
                ww, hh = int(b["Width"]), int(b["Height"])
                wid = int(w.get("kCGWindowNumber", 0))
            except (KeyError, TypeError, ValueError):
                continue
            if ww < 40 or hh < 40:
                continue
            out.append({"x": wx, "y": wy, "width": ww, "height": hh,
                        "owner": str(owner), "wid": wid})
    _SOLID_CACHE = out
    _SOLID_CACHE_TS = now
    return out


def _top_window_wid_at(x: float, y: float) -> int | None:
    """(x,y) 处最顶层实体窗 wid（_solid_windows 按 z-order front-to-back，
    第一个包含点即最顶层）。无 Quartz/无命中 → None。"""
    for w in _solid_windows():
        if w["x"] <= x < w["x"] + w["width"] and w["y"] <= y < w["y"] + w["height"]:
            return w["wid"]
    return None


def solid_at(x: float, y: float, ref: dict | None = None) -> bool:
    """图层双检查（mac，CGWindowList 200ms 短缓存实装）：

    - ref=None：(x,y) 处是否有实体窗（任意命中）；
    - ref=候选窗 dict：该点最顶层实体窗是否就是 ref（比对 wid）——
      候选窗被别的窗盖住 → 返回 False 否决攀爬/落顶。
    用 200ms 短缓存（win WindowFromPoint O(1) 实时，mac 退 200ms 延迟但比 2s
    实时、比每次 CopyWindowInfo 快；幽灵窗 200ms 内移除防鬼线）。"""
    if not _HAS_QUARTZ:
        return True
    top = _top_window_wid_at(x, y)
    if top is None:
        return False
    if ref is not None:
        return top == ref.get("wid")
    return any(w["wid"] == top for w in _solid_windows())


def alive_at(ref: dict) -> bool:
    """窗口存活可见（在 200ms 短缓存 OnScreenOnly 里）。ref 无 wid → True。
    用 200ms 短缓存（win IsWindow+IsIconic O(1) 实时，mac 退 200ms 延迟）。"""
    wid = (ref or {}).get("wid")
    if not wid:
        return True
    return any(w["wid"] == wid for w in _solid_windows())


def enumerate_windows() -> list[dict]:
    """窗口框枚举（缓存 ≤2s；app 2s 调 build_sensors，绝不每帧）。"""
    global _WINDOWS_CACHE, _WINDOWS_CACHE_TS
    now = time.monotonic()
    if _WINDOWS_CACHE and (now - _WINDOWS_CACHE_TS) < _WINDOWS_TTL:
        return _WINDOWS_CACHE
    _WINDOWS_CACHE = _enumerate_windows_uncached()
    _WINDOWS_CACHE_TS = now
    return _WINDOWS_CACHE


def fullscreen_status() -> tuple[bool, str]:
    """全屏检测：是否有普通 app 的窗口覆盖某屏全 frame。
    返回 (is_fullscreen, owner)。演示模式（Keynote/PowerPoint 全屏）同命中。
    无 Quartz → (False, "")。"""
    if not _HAS_QUARTZ:
        return (False, "")
    try:
        wins = CGWindowListCopyWindowInfo(
            kCGWindowListOptionOnScreenOnly, kCGNullWindowID
        )
    except Exception:
        return (False, "")
    screen_frames = _screen_full_frames()
    for w in wins:
        try:
            layer = int(w.get("kCGWindowLayer", 0))
        except (TypeError, ValueError):
            layer = 0
        if layer != 0:
            continue
        owner = w.get("kCGWindowOwnerName", "")
        if owner in _DESKTOP_OWNERS:
            continue
        b = w.get("kCGWindowBounds")
        if not b:
            continue
        if _is_fullscreen_bounds(b, screen_frames):
            return (True, str(owner))
    return (False, "")


def build_sensors() -> Sensors:
    return Sensors(
        mouse_pos=mouse_pos(),
        work_area=work_area(),
        windows=enumerate_windows(),
        idle_time=0.0,
        solid_at=solid_at,
        alive_at=alive_at,
    )
