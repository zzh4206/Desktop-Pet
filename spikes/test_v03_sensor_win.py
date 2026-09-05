"""v0.3 win 传感器自动验证 —— EnumWindows 缓存 / 高 DPI 坐标 / 全屏检测。

运行：python spikes/test_v03_sensor_win.py
"""

from __future__ import annotations

import sys

from PySide6.QtWidgets import QApplication

sys.path.insert(0, ".")

import pet.sensor_win as sw  # noqa: E402

PASS, FAIL = [], []


def check(name, cond):
    (PASS if cond else FAIL).append(name)
    print(("  ✅ " if cond else "  ❌ ") + name)


def main() -> int:
    app = QApplication(sys.argv)  # Qt 几何/换算需要 QGuiApplication
    _ = app
    # 批次H/M12（REVIEW-2026-08-31 F31）：offscreen 是 800x800 假屏——
    # 本套件测真实 Win32 桌面（EnumWindows/监视器配对），假屏下坐标
    # 体系不成立，整体跳过（需要真桌面会话）
    from PySide6.QtGui import QGuiApplication
    if QGuiApplication.platformName() == "offscreen":
        print("offscreen 假屏：win 传感器套件整体跳过（需真桌面会话）")
        print("\n结果：0 通过 / 0 失败（跳过）")
        return 0

    # T-a 可见窗口枚举：格式与 work_area 同系（逻辑坐标 dict）
    wins = sw.visible_windows(refresh=True)
    check("T-a 枚举到可见窗口(≥1)", len(wins) >= 1)
    check(
        "T-a 窗口框格式 {x,y,width,height[,hwnd]}(v0.3.6 图层检查附 hwnd)",
        all({"x", "y", "width", "height"} <= set(w) for w in wins)
        and all(set(w) <= {"x", "y", "width", "height", "hwnd"} for w in wins),
    )
    check(
        "T-a 窗口框尺寸合理(>40px)",
        all(w["width"] > 40 and w["height"] > 40 for w in wins),
    )

    # T-b 缓存：TTL 内二次调用不重新枚举（同一 list 对象）
    again = sw.visible_windows()
    check("T-b TTL 内命中缓存(同对象)", again is wins)

    # T-c 强制刷新拿到新枚举
    fresh = sw.visible_windows(refresh=True)
    check("T-c 强制刷新重新枚举", fresh is not wins and len(fresh) >= 1)

    # T-d 全屏检测：返回 (bool, 进程名) 二元组（当前桌面环境通常 False）
    fs, name = sw.foreground_fullscreen()
    check("T-d 全屏检测返回二元组", isinstance(fs, bool) and isinstance(name, str))

    # T-d2 窗口类名可获取（桌面排除的基础）；桌面类被正确识别
    cls = sw._window_class(sw._user32.GetForegroundWindow())
    check("T-d2 前台窗口类名可获取", isinstance(cls, str))
    check("T-d2 桌面类排除表含 Progman/WorkerW",
          sw._DESKTOP_CLASSES == {"Progman", "WorkerW"})

    # T-e build_sensors 装配 windows
    s = sw.build_sensors()
    check("T-e Sensors.windows 已装配", len(s.windows) >= 1)

    # T-f 工作区与窗口框同系（均在工作区合集附近，无荒谬坐标）
    wa = sw.work_area()
    sane = all(
        wa["x"] - 300 <= w["x"] <= wa["x"] + wa["width"] + 300 for w in wins
    )
    check("T-f 窗口框与工作区同坐标系(逻辑px)", sane)

    # T-g 批次F/M1（REVIEW-2026-08-31）：监视器配对表 + 精确 DPI 变换
    mons = sw._monitor_map()
    check("T-g 监视器配对表非空且 QScreen 全配上", len(mons) >= 1
          and all(s.name() for s, _r in mons))
    prim = [m for m in mons if m[1][0] == 0 and m[1][1] == 0]
    check("T-g 主屏物理原点 (0,0)", len(prim) == 1)
    if prim:
        s0, (pl, pt, pr, pb) = prim[0]
        dpr_meas = (pr - pl) / max(1, s0.geometry().width())
        check("T-g 实测 dpr（物理/逻辑宽）与 Qt devicePixelRatio 偏差<0.05",
              abs(dpr_meas - s0.devicePixelRatio()) < 0.05)

    # ---- 批次B/P2-3+P2-7（REVIEW-2026-09-05）真桌面冒烟 ----
    _fs_info = sw.foreground_fullscreen()
    check("P2-3 前台全屏判定（样式+cloak 过滤后）返回 (bool, str)",
          isinstance(_fs_info, tuple) and len(_fs_info) == 2
          and isinstance(_fs_info[0], bool) and isinstance(_fs_info[1], str))
    _srs = sw.screen_rects()
    check("P2-7 逐屏几何非空且含四键",
          bool(_srs) and all({"x", "y", "width", "height"} <= set(r)
                             for r in _srs))

    # ---- 批次D/E1+E5（REVIEW-2026-09-05）：真实探针层冒烟 ----
    from PySide6.QtCore import Qt as _Qt
    from PySide6.QtTest import QTest as _QTest
    from PySide6.QtWidgets import QLabel as _QLabel

    _probe = _QLabel("probe")
    _probe.setWindowFlags(_Qt.Tool | _Qt.FramelessWindowHint
                          | _Qt.WindowStaysOnTopHint)
    _probe.resize(80, 60)
    _probe.move(10, 10)
    _probe.show()
    _probe.raise_()
    _QTest.qWait(300)
    _ref = {"hwnd": int(_probe.winId()), "x": _probe.x(), "y": _probe.y(),
            "width": _probe.width(), "height": _probe.height()}
    check("E1a window_alive 实窗 True", sw.window_alive(_ref) is True)
    check("E1b window_alive 死句柄 False",
          sw.window_alive({"hwnd": 0x1DEADBEEF}) is False)
    _r = sw.window_rect(_ref)
    check("E1c window_rect 实时矩形有效",
          isinstance(_r, dict) and _r["width"] > 0 and _r["height"] > 0)
    # Z 序下探函数真实执行（命中受桌面遮挡状态影响，只验可调用不炸）
    _cx, _cy = _probe.x() + 40, _probe.y() + 30
    _hit = sw.solid_at(_cx, _cy, _ref)
    check("E1d solid_at 实窗探针可调用且返 bool", isinstance(_hit, bool))
    check("E1e solid_at 命中自身实窗（置顶探针窗）", _hit is True)
    _probe.close()
    _probe.deleteLater()

    # ---- E5：idle 秒数 32bit 回绕安全域 ----
    _idle = sw.get_idle_seconds()
    check("E5 get_idle_seconds 安全域（0 ≤ idle < 2^32/1000）",
          0.0 <= _idle < 4294967.295)

    print(f"\n结果：{len(PASS)} 通过 / {len(FAIL)} 失败")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
