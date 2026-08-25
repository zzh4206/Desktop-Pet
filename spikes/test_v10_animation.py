"""v0.10.18 动画层自动化验证（REVIEW-2026-08-25 H1/H3 回归锁）——

恢复 78a01c9 revert 时丢失的 AIArtProvider 覆盖并新增（v0.10.15-17 只做
"offscreen 手工目检"，H1 小动作被停三个版本无人发现正是缺自动化所致）：
  T1  frames_for 返回显示档尺寸 SpriteRef（旧版 width=1/height=1 靠调用方改）
  T2  H1：IDLE 下 ANIMATE 小动作不被 _frame_tick 同 tick 停掉
  T3  H1：blink 循环到期自停（回到静帧、key 清空）
  T4  walk/fall/eat_mouse/land 状态分支切换 + 非小动作兜底停
  T5  H3：pix 缓存条目为显示档缩放图（非 1024×1536 全分辨率）
  T6  H3：缓存命中重排（真 LRU，非 FIFO）
运行：python spikes/test_v10_animation.py（offscreen，无窗口、无网络）
"""

from __future__ import annotations

import os
import sys
import types

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")  # 须先于 PySide6 导入

sys.path.insert(0, ".")

from PySide6.QtWidgets import QApplication  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402

from pet.asset_provider import AIArtProvider, SpriteRef  # noqa: E402
from pet.pet_state import Branch, Stage  # noqa: E402
from pet.window import WindowBase  # noqa: E402
from app import PetApp  # noqa: E402

PASS, FAIL = [], []


def check(name, cond):
    (PASS if cond else FAIL).append(name)
    print(("  ✅ " if cond else "  ❌ ") + name)


class _AppStub:
    """只挂 _play_* / _frame_tick 依赖的属性——不解耦 PetApp 全量构造。"""

    def __init__(self, provider, window, stage: Stage):
        self.provider = provider
        self.window = window
        self.store = types.SimpleNamespace(
            get=lambda: types.SimpleNamespace(stage=stage))
        self._anim_key = None


def main() -> int:
    app = QApplication.instance() or QApplication(sys.argv)
    provider = AIArtProvider()
    state = types.SimpleNamespace(
        stage=Stage.YOUNG, branch=Branch.HEALTHY,
        mood=80, fullness=80)
    window = WindowBase(provider.get_static(state))
    stub = _AppStub(provider, window, Stage.YOUNG)
    stub._SMALL_ANIM_KEYS = PetApp._SMALL_ANIM_KEYS
    for name in ("_play_animate", "_play_key", "_stop_anim", "_frame_tick"):
        setattr(stub, name, types.MethodType(getattr(PetApp, name), stub))

    # ---- T1 frames_for 显示档尺寸 ----
    walk = provider.frames_for("young", "walk")
    check("T1a frames_for(walk) 两帧", len(walk) == 2)
    check("T1b 帧尺寸=显示档 192×192（旧版 1×1）",
          all(f.width == 192 and f.height == 192 for f in walk))

    # ---- T2 H1：小动作不被同 tick 兜底停 ----
    stub._play_animate("blink")
    check("T2a blink 启动（_frames 非空）",
          stub._anim_key == "blink" and len(window._frames) == 2)
    stub._frame_tick(None, "idle", "idle")
    check("T2b H1 回归：IDLE 下 _frame_tick 不停小动作",
          stub._anim_key == "blink" and len(window._frames) == 2)

    # ---- T4 状态分支切换（各模式覆盖小动作；非小动作兜底停） ----
    stub._frame_tick(None, "walk", "idle")
    check("T4a walk 分支", stub._anim_key == "walk")
    stub._frame_tick(None, "fall", "walk")
    check("T4b fall 分支（fall_air 循环）", stub._anim_key == "fall_air")
    # prev=fall 会先走落地分支（fall→其他模式=land 瞬帧，真实行为）；
    # 直测 chew 用非 fall 前置
    stub._frame_tick(None, "eat_mouse", "idle")
    check("T4c eat_mouse 分支（chew 循环）", stub._anim_key == "eat_mouse_chew")
    stub._frame_tick(None, "idle", "eat_mouse")
    check("T4d 离开吃鼠标 → 兜底停", stub._anim_key is None
          and window._frames == [])
    stub._frame_tick(None, "fall", "idle")
    stub._frame_tick(None, "idle", "fall")
    check("T4e 落地分支（land 瞬帧）", stub._anim_key == "land")
    stub._anim_key = "walk"   # 模拟遗留非小动作 key
    stub._frame_tick(None, "idle", "idle")
    check("T4f 非小动作/非 land 的遗留 key 兜底停", stub._anim_key is None)

    # ---- T3 blink 到期自停（2 轮 × 2 帧 × 300ms + 120ms 余量） ----
    stub._play_animate("blink")
    QTest.qWait(300)   # 播放中：key 仍在
    check("T3a blink 播放中", stub._anim_key == "blink")
    QTest.qWait(1400)  # 越过到期点（事件循环处理 singleShot）
    check("T3b blink 到期自停（key 清空回静帧）",
          stub._anim_key is None and window._frames == [])
    stub._play_animate("roll")
    QTest.qWait(1000)  # roll 定格 260+400+120ms 后应停
    check("T3c roll 定格到期回静帧", stub._anim_key is None)

    # ---- T5 缓存条目=显示档缩放图 ----
    window._pix_cache.clear()
    window.set_sprite(provider.get_static(state))   # young 192 档静帧
    pms = [pm for pm in window._pix_cache.values() if not pm.isNull()]
    check("T5a 缓存命中（有条目）", len(pms) == 1)
    check("T5b 缓存为显示档图（≤192，非 1024 全分辨率）",
          pms and pms[0].width() <= 192 and pms[0].height() <= 192)

    # ---- T6 真 LRU：命中重排（FIFO 不会） ----
    frames = provider.frames_for("young", "stretch")
    check("T6 前置：stretch 帧存在", len(frames) == 3)
    if len(frames) >= 2:
        sa = SpriteRef(path=frames[0].path, width=64, height=64)
        sb = SpriteRef(path=frames[1].path, width=64, height=64)
        window._pix_cache.clear()
        window.set_sprite(sa)
        window.set_sprite(sb)
        window.set_sprite(sa)   # 命中 → 应移到尾部（最新）
        keys = list(window._pix_cache)
        check("T6 LRU 命中重排（最热在尾）",
              keys and keys[-1] == (sa.path, 1, 64, 64))

    print(f"\n动画层: {len(PASS)} 通过, {len(FAIL)} 失败")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
