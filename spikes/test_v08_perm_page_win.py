"""v0.8 权限页异步自检回归锁（M8 修，REVIEW-2026-08-27）——

旧版 PermBridge 构造/refresh 在主线程同步跑全部自检（剪贴板走
PowerShell 典型 1-3s + COM 音量 + 文件探测），app 启动路径与 QML
"重新检测"都冻结 UI 数秒。M8 后自检走 _PermCheckWorker 后台线程，
done 信号回主线程更新 items。
本文件用全 stub 检查函数验证异步管线（worker 启动/信号回送/引用回收/
二次刷新），不打真实 PowerShell/COM。
运行：python spikes/test_v08_perm_page_win.py（offscreen，无副作用）
"""

from __future__ import annotations

import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")  # 须先于 PySide6 导入
sys.path.insert(0, ".")

from PySide6.QtWidgets import QApplication  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402

from pet.ui.perm_bridge import PermBridge  # noqa: E402

PASS, FAIL = [], []


def check(name, cond):
    (PASS if cond else FAIL).append(name)
    print(("  ✅ " if cond else "  ❌ ") + name)


class _StubBridge(PermBridge):
    """六项检查全 stub（不打 PowerShell/COM）——只验异步管线。"""

    def _check_ll_hook(self):
        return (True, "")

    def _check_hotkey(self):
        return (True, "")

    def _check_clipboard(self):
        return (True, "")

    def _check_volume(self):
        return (True, "")

    def _check_paths(self):
        return (True, "")

    def _check_ds_key(self):
        return (False, "stub 未设置")


def main() -> int:
    app = QApplication.instance() or QApplication(sys.argv)
    b = _StubBridge(object())   # 检查全 stub，adapter 不被触碰
    # 批次F/L14（REVIEW-2026-09-04）：构造不再自动自检（旧版每次开机后台
    # 读一次剪贴板）——权限页打开（perm.qml onCompleted）/显式 refresh 才跑
    check("T0 构造不自检（L14 启动零剪贴板访问）", b._items == [])
    b.refresh()
    # 等 worker 完成且 done 信号经事件循环送达
    waited = 0
    while waited < 5000 and not b._items:
        QTest.qWait(50)
        waited += 50
    check("T1 refresh 后台自检（items 到位）", len(b._items) == 6)
    check("T2 stub 结果如约（5 过 1 挂）",
          sum(1 for i in b._items if i["ok"]) == 5)
    check("T3 worker 引用已回收", b._worker is None)
    b.refresh()                 # 二次刷新正常再跑一轮
    waited = 0
    while waited < 5000 and b._worker is not None:
        QTest.qWait(50)
        waited += 50
    QTest.qWait(50)             # done 信号送达
    check("T4 二次 refresh 再出结果", len(b._items) == 6
          and b._worker is None)

    # ---- 批次B/P2-2（REVIEW-2026-09-05）：权限页剪贴板自检零内容访问 ----
    import pet.ui.perm_bridge as _pbm
    # 真方法（_StubBridge 覆盖不影响类方法）：offscreen 下无内容探测应健康通过
    _real = _pbm.PermBridge._check_clipboard(None)
    check("P2-2a 剪贴板自检零内容探测（序号/开合探针）", _real[0] is True)
    # 旧实现经 ClipboardHandler 真读内容——毒化模块后调用不得受其影响
    class _Poison:
        def __getattr__(self, item):
            raise AssertionError("权限页不得 import/执行剪贴板内容读取")

    _saved = sys.modules.get("pet.tools_win")
    sys.modules["pet.tools_win"] = _Poison()
    try:
        _pbm.PermBridge._check_clipboard(None)
        _untouched = True
    except AssertionError:
        _untouched = False
    finally:
        if _saved is None:
            sys.modules.pop("pet.tools_win", None)
        else:
            sys.modules["pet.tools_win"] = _saved
    check("P2-2b 不再经 ClipboardHandler 读剪贴板内容", _untouched)

    print(f"\n权限页异步: {len(PASS)} 通过, {len(FAIL)} 失败")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
