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
        self.fsm = types.SimpleNamespace(motion_mode="free")  # L1 后真实读取
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

    # ---- L3（REVIEW-2026-09-04）：land 单帧 520ms 到期自停 ----
    stub._frame_tick(None, "fall", "idle")
    stub._frame_tick(None, "idle", "fall")
    check("L3a 落地分支 land", stub._anim_key == "land")
    QTest.qWait(700)          # 越过 520ms 到期点
    check("L3b land 到期自停（旧版定格 5-35s）", stub._anim_key is None)

    # ---- L21（REVIEW-2026-09-04）：paperdoll 档跳过帧版 blink ----
    class _FakePaperWin:
        def __init__(self):
            self.played = []

        def width(self):
            return 192

        def height(self):
            return 192

        def part_walk_active(self):
            return True

        def is_playing(self):
            return False

        def play_frames(self, frames, loop=False, interval_ms=150):
            self.played.append((list(frames), loop))

        def stop_frames(self):
            pass

    stub2 = _AppStub(provider, window, Stage.YOUNG)
    stub2._SMALL_ANIM_KEYS = PetApp._SMALL_ANIM_KEYS
    for name in ("_play_animate", "_play_key", "_stop_anim", "_frame_tick"):
        setattr(stub2, name, types.MethodType(getattr(PetApp, name), stub2))
    stub2._part_walk = True
    stub2.window = _FakePaperWin()
    stub2._play_animate("blink")
    check("L21 paperdoll 档跳过帧版 blink（引擎贴片已在场景内）",
          stub2._anim_key is None and not stub2.window.played)
    stub2._play_animate("stretch")
    check("L21a stretch 不受影响照常播帧",
          stub2._anim_key == "stretch" and len(stub2.window.played) == 1)

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
        # v0.10.18b 缓存键加 mtime 成 5 元组 (path, facing, w, h, mtime)
        last_key = keys[-1] if keys else None
        check("T6 LRU 命中重排（最热在尾，5 元组键）",
              last_key is not None and len(last_key) == 5
              and last_key[:4] == (sa.path, 1, 64, 64))

    # ---- T7 M1/L4 修（REVIEW-2026-08-27）：动画中 on_change 不闪静帧 ----
    window.set_sprite_provider(provider)
    static2 = provider.get_static(state)
    window._pix_cache.clear()
    window.stop_frames()                 # 清场：回到静帧
    window.set_sprite(static2)
    window.play_frames(provider.frames_for("young", "walk"), loop=True)
    check("T7a 前置：动画播放中", bool(window._frames))
    window.on_state_change(state)        # 衰减每 1s 触发同款调用
    check("T7b M1：动画中 on_change 不动当前画面（_sprite 仍动画帧）",
          window._sprite.path.endswith("walk_0.png"))
    check("T7c M1：动画中 on_change 更新恢复目标静帧",
          window._static_sprite.path == static2.path)
    window.play_frames(provider.frames_for("young", "chew"), loop=True)
    check("T7d L4：walk→chew 切换恢复目标仍是静帧（旧版变 walk 帧）",
          window._static_sprite.path == static2.path)
    window.stop_frames()
    check("T7e 停止后恢复静帧", window._sprite.path == static2.path)

    # ---- T8 M9 修（REVIEW-2026-08-27）：frames_for 结果缓存 + 防御拷贝 ----
    import pet.asset_provider as ap

    provider._frames_cache.clear()
    stat_calls = []

    def _spy_isfile(p, *a, **k):
        stat_calls.append(str(p))
        return _orig_isfile(p, *a, **k)

    # 批次H/T4（REVIEW-2026-08-31）：mock.patch.object 上下文管理器——
    # 旧版手工赋值/恢复 ap.os.path.isfile（ap.os 即全局 os 模块），
    # 恢复遗漏即全进程污染
    from unittest import mock
    _orig_isfile = os.path.isfile
    with mock.patch.object(os.path, "isfile", _spy_isfile):
        provider.frames_for("young", "walk")
        n_first = len(stat_calls)
        provider.frames_for("young", "walk")
        n_second = len(stat_calls) - n_first
    check("T8a 首次 stat 建缓存", n_first >= 1)
    check("T8b 二次调用零 stat（M9 缓存命中）", n_second == 0)
    w1 = provider.frames_for("young", "walk")
    w1[0].width = 99                     # 模拟调用方改尺寸（app._play_key）
    w2 = provider.frames_for("young", "walk")
    check("T8c 返回防御拷贝（改尺寸不污染缓存）", w2[0].width == 192)

    # L8（REVIEW-2026-09-04）：sleepy 门限接线 + None=禁用——旧版
    # _mood_from_state 恒用 600s 模块常量，config 的 sleepy_idle_minutes
    # 存进 provider 后从未被读取
    from pet.asset_provider import _mood_from_state
    from pet.pet_state import Mood

    st_l8 = types.SimpleNamespace(fullness=80, mood=80)
    check("L8a 配置门限生效(700<800 不困)",
          _mood_from_state(st_l8, 700.0, 800.0) == Mood.HAPPY)
    check("L8b 越配置门限即困(700>=650)",
          _mood_from_state(st_l8, 700.0, 650.0) == Mood.SLEEPY)
    check("L8c 门限 None=禁用(99999 不困)",
          _mood_from_state(st_l8, 99999.0, None) == Mood.HAPPY)
    p_l8 = AIArtProvider(idle_fn=lambda: 700.0, sleepy_idle_s=800.0)
    check("L8d provider 透传门限", p_l8._fallback._sleepy_idle_s == 800.0)

    print(f"\n动画层: {len(PASS)} 通过, {len(FAIL)} 失败")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
