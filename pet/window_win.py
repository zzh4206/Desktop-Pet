"""win 透明置顶浮窗 —— 平台适配与分工.md §六 / W1 Spike 结论。

W1 结论（spikes/w1_transparent_window.py）：v0.1 窗口即宠物尺寸（共享
``WindowBase`` resize 到 SpriteRef），不存在"背景穿透"问题——整窗就是交互区，
无需 ``WS_EX_TRANSPARENT``/``setMask``。全屏穿透方案（动态 mask 随宠物移动）
留待 v0.3 攀爬/全屏检测时按需启用。

本文件是 ``_win`` shim：平台库只进本文件 + 注入点 ``platform.py``。
v0.1 无额外 polish，直接继承 ``WindowBase``（Qt flags 双平台已够用）。
"""

from __future__ import annotations

from .asset_provider import SpriteRef
from .window import WindowBase


class PetWindow(WindowBase):
    def __init__(self, sprite: SpriteRef, parent=None):
        super().__init__(sprite, parent)
