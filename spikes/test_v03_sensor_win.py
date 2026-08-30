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
    # （offscreen 平台 QScreen.name()='' 是假屏，无配对意义——跳过）
    from PySide6.QtGui import QGuiApplication
    if QGuiApplication.platformName() == "offscreen":
        check("T-g 监视器配对（offscreen 假屏跳过，真桌面跑）", True)
    else:
        mons = sw._monitor_map()
        check("T-g 监视器配对表非空且 QScreen 全配上", len(mons) >= 1
              and all(s.name() for s, _r in mons))
        prim = [m for m in mons if m[1][0] == 0 and m[1][1] == 0]
        check("T-g 主屏物理原点 (0,0)", len(prim) == 1)
        if prim:
            s0, (pl, pt, pr, pb) = prim[0]
            dpr_meas = (pr - pl) / max(1, s0.geometry().width())
            check("T-g 实测 dpr（物理/逻辑宽）与 Qt devicePixelRatio "
                  "偏差<0.05",
                  abs(dpr_meas - s0.devicePixelRatio()) < 0.05)

    print(f"\n结果：{len(PASS)} 通过 / {len(FAIL)} 失败")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
