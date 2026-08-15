"""mac 透明置顶浮窗 —— 平台适配与分工.md §五。

``PetWindow`` 继承 ``WindowBase``，做 NSWindow floating-level polish（pyobjc
懒加载 + 守卫，无 pyobjc 时退化）。**平台库 import 只进本 ``_mac`` 文件**
（+ 注入点 ``platform.py``）；共享 ``window.py`` 不碰平台库。
"""

from __future__ import annotations

from .asset_provider import SpriteRef
from .window import WindowBase

try:
    from AppKit import (
        NSWindowCollectionBehaviorCanJoinAllSpaces,
        NSWindowCollectionBehaviorStationary,
    )
    from objc import objc_object

    _HAS_PYOBJC = True
except Exception:
    _HAS_PYOBJC = False


class PetWindow(WindowBase):
    def __init__(self, sprite: SpriteRef, parent=None):
        super().__init__(sprite, parent)
        self._polished = False

    def showEvent(self, event):
        super().showEvent(event)
        if not self._polished:
            self._polished = True
            self._polish_mac_window()

    def _polish_mac_window(self) -> None:
        """置顶到所有空间、不随 app 失活隐藏。无 pyobjc 则退化。"""
        if not _HAS_PYOBJC:
            return
        try:
            from ctypes import c_void_p

            wid = int(self.winId())
            view = objc_object(c_void_p=wid)
            nswin = view
            try:
                maybe = view.window()
                if maybe is not None:
                    nswin = maybe
            except Exception:
                pass

            # NSFloatingWindowLevel = 3
            nswin.setLevel_(3)
            nswin.setCollectionBehavior_(
                NSWindowCollectionBehaviorCanJoinAllSpaces
                | NSWindowCollectionBehaviorStationary
            )
            nswin.setHidesOnDeactivate_(False)
            nswin.setMovableByWindowBackground_(False)
        except Exception:
            # 无 pyobjc / 句柄异常时退化为纯 Qt 行为，仍可上屏
            pass
