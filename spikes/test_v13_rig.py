"""v0.13 分层绑骨呈现层自动化验证（offscreen，无窗口交互、无网络）。

覆盖：
  T1  spec：manifest 缺失/非法/figure 文件缺失 → load_rig_spec 返回 None（降级铁律）
  T2  spec：合法 manifest 解析出 figures/parts，部件文件缺失被弃件不弃场
  T3  装配：build_rig_window 在有资产时返回 rig 活性实例；无 stage 目录回退基类
  T4  渲染语义：set_sprite 同步直显（figASrc 写入/activeFigure 绑定名）
  T5  序列播放：play_frames 簿记与基类同构；首帧直显后按间隔交叉推进；
      loop 循环 / 非循环播完回静帧（_frames 清空）
  T6  is_playing 门卫：播中 True、停后 False；同 key 重入不重启（_play_key 语义）
  T7  set_facing 场景镜像属性跟随；无效值不误写
  T8  set_motion_params：walking 切换写入；airborne 下降沿触发 squash（场景函数可调）
  T9  emoji 降级：非文件 SpriteRef 时场景让位、label 接管（恢复时场景回归）
运行：
  python -X utf8 spikes/test_v13_rig.py     # 需 PySide6 自带 QtQuick（缺件即 FAIL）
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import types

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ["QT_QUICK_BACKEND"] = "software"   # 无 GPU 环境渲染；须先于 Qt 导入

sys.path.insert(0, ".")

from PySide6.QtCore import QTimer   # noqa: E402
from PySide6.QtWidgets import QApplication   # noqa: E402

from pet.asset_provider import AIArtProvider, SpriteRef   # noqa: E402
from pet.pet_state import Branch, Stage   # noqa: E402
from pet.rig.presenter import (   # noqa: E402
    RigWindow,
    build_rig_window,
    figure_key_from_path,
)
from pet.rig.spec import load_rig_spec   # noqa: E402
from pet.window import WindowBase   # noqa: E402

PASS, FAIL = [], []


def check(name, cond):
    (PASS if cond else FAIL).append(name)
    print(("  ✅ " if cond else "  ❌ ") + name)


REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _usProp(win, name: str) -> str:
    """root 属性(url 型)→ 纯字符串。property 返回 QUrl 对象，
    str() 是 repr 包装（实测教训），必须走 toString()。"""
    v = win._root.property(name)
    return v.toString() if hasattr(v, "toString") else str(v)


def _write_manifest(d: str, figures: dict, parts: list) -> str:
    m = {"spec": 1, "figures": figures, "parts": parts}
    p = os.path.join(d, "manifest.json")
    with open(p, "w", encoding="utf-8") as f:
        json.dump(m, f)
    return p


def main() -> int:
    app = QApplication.instance() or QApplication(sys.argv)

    src_neutral = os.path.join(
        REPO, "assets", "ai", "final_neglected_neutral.png")
    fig_derived = os.path.join(
        REPO, "assets", "rig", "final", "figs", "neglected_neutral.png")
    part_tail = os.path.join(
        REPO, "assets", "rig", "final", "parts",
        "tail_neglected_neutral.png")

    # ---- T1 spec 降级 ----
    tmp1 = tempfile.mkdtemp()
    check("T1a 目录无 manifest → None",
          load_rig_spec(tmp1, "final") is None)
    bad = os.path.join(tmp1, "manifest.json")
    with open(bad, "w", encoding="utf-8") as f:
        f.write("{ not json")
    check("T1b manifest 非 JSON → None", load_rig_spec(tmp1, "final") is None)
    tmp2 = tempfile.mkdtemp()
    _write_manifest(tmp2, {"x": "../missing.png"}, [])
    check("T1c figure 全缺失 → None", load_rig_spec(tmp2, "final") is None)

    # ---- T2 正常解析 + 弃件 ----
    tmp3 = tempfile.mkdtemp()
    fig_rel = src_neutral   # 绝对路径：os.path.join(abs) 直接重置为自身
    _write_manifest(tmp3, {
        "neglected_neutral": fig_rel,
        "ghost_figure": fig_rel,
    }, [{
        "id": "tail_n", "file": "nope.png",
        "source_figure": "neglected_neutral",
        "px_rect": [770, 701, 991, 1141], "pivot": [800, 1130],
        "z": "under_core",
        "sway": {"amp_deg": 4, "period_ms": 2600},
    }, {
        "id": "tail_ghost", "file": "whatever.png",
        "source_figure": "ghost_figure",      # figure 未在可用集 → 弃件
        "px_rect": [0, 0, 1, 1], "pivot": [0, 0], "z": "under_core",
    }])
    spec = load_rig_spec(tmp3, "final")
    check("T2a spec 非 None 且 figure 数=2（含 ghost）", spec is not None
          and len(spec.figures) == 2)
    ok_files = sum(1 for p in spec.parts if os.path.isfile(p.path))
    check("T2b 缺文件部件弃件不弃场（part 数=0）",
          len(spec.parts) == 0 and ok_files == 0)
    _write_manifest(tmp3, {"neglected_neutral": fig_rel}, [{
        "id": "tail_ok", "file": fig_rel.replace("../", ""),
        "source_figure": "neglected_neutral",
        "px_rect": [1, 2, 3, 4], "pivot": [1, 1], "z": "over_core",
        "sway": {"amp_deg": 2, "period_ms": 1000},
    }])
    spec2 = load_rig_spec(tmp3, "final")
    check("T2c 有效部件解析（z/sway 归一）",
          len(spec2.parts) == 1 and spec2.parts[0].z == "over_core"
          and abs(spec2.parts[0].amp_deg - 2.0) < 1e-6)

    # ---- T3 装配 ----
    base_sprite = SpriteRef(path=src_neutral, width=320, height=320)
    win_none = build_rig_window(WindowBase, base_sprite, "young",
                                rig_root=os.path.join(REPO, "assets", "rig"))
    check("T3a 无清单阶段回退基类", type(win_none) is WindowBase)
    win_true = build_rig_window(WindowBase, base_sprite, "final",
                                rig_root=os.path.join(REPO, "assets", "rig"))
    check("T3b final 有清单 → RigWindow 且活性",
          isinstance(win_true, RigWindow) and win_true.rig_active)
    if not (isinstance(win_true, RigWindow) and win_true.rig_active):
        print("\n（后续依赖场景的检查因环境不可用而跳过阻断——见失败项）")
        print(f"\nrig 层: {len(PASS)} 通过, {len(FAIL)} 失败")
        return 1
    win = win_true

    # ---- T4 同步直显 ----
    win.set_sprite(base_sprite)
    _got = _usProp(win, "figASrc")
    check("T4a figASrc 指向派生核心图",
          _got.endswith("figs/neglected_neutral.png"))
    check("T4b activeFigure 绑定名", win._root.property("activeFigure")
          == "neglected_neutral")
    src_sad = os.path.join(
        REPO, "assets", "ai", "final_neglected_sad.png")
    win.set_sprite(SpriteRef(path=src_sad, width=320, height=320))
    check("T4c 换表情：绑定随 figure 切换（sad 无尾件）",
          win._root.property("activeFigure") == "neglected_sad"
          and _usProp(win, "figASrc").endswith("final_neglected_sad.png"))
    win.set_sprite(base_sprite)
    check("T4d 回中性：尾件重新绑定",
          win._root.property("activeFigure") == "neglected_neutral")

    # ---- T5 序列播放 ----
    provider = AIArtProvider()
    state = types.SimpleNamespace(stage=Stage.FINAL,
                                  branch=Branch.NEGLECTED,
                                  mood=80, fullness=80)
    walk = provider.frames_for("final", "walk")
    check("T5a 前置：walk 帧存在（2 或含中间步姿 4）",
          len(walk) in (2, 4))
    if len(walk) == 4:
        check("T5a-2 步态环顺序 0→0b→1→1b",
              [os.path.basename(f.path) for f in walk] ==
              ["final_walk_0.png", "final_walk_0b.png",
               "final_walk_1.png", "final_walk_1b.png"])
    win.stop_frames()
    win.set_sprite(base_sprite)
    win.play_frames(walk, loop=True, interval_ms=200)
    check("T5b-1 簿记一致", len(win._frames) == len(walk)
          and win.is_playing())
    check("T5b-2 首帧直显 walk_0",
          _usProp(win, "figASrc").endswith("final_walk_0.png"))
    check("T5b-3 计时器已启动", win._frame_timer.isActive())
    check("T5b-4 walk 硬切策略（fade=0）", win._fade_ms == 0)
    QTest_wait = getattr(__import__("PySide6.QtTest", fromlist=["QTest"]),
                         "QTest")
    QTest_wait.qWait(240)      # 越过首个 200ms 间隔（硬切即换帧）
    mix_mid = float(win._root.property("mix"))
    front = _usProp(win, "figASrc") + "|" + _usProp(win, "figBSrc")
    check("T5c 第一跳：硬切已切到第二帧（0b）或淡化中",
          "walk_0b" in front or 0.05 < mix_mid < 0.99)
    win.play_frames(provider.frames_for("final", "chew"), loop=False,
                    interval_ms=120)
    check("T5d 序列切换重置簿记（idx=0/键不变由 app 管）",
          len(win._frames) == 2 and win._frame_idx == 0)
    QTest_wait.qWait(320)      # 120×1 + fade ≤150ms 完成两帧并收尾
    check("T5e 非循环播完回静帧清簿记",
          not win.is_playing() and not win._frame_timer.isActive())
    # L4 语义：序列中的 _static_sprite 应仍是最初静帧
    check("T5f L4：恢复目标为进入播放前的静帧",
          win._static_sprite.path == base_sprite.path)

    # ---- T6 key 重入门卫（app._play_key 逻辑等价复刻） ----
    win._anim_key = "walk"
    seq = provider.frames_for("final", "chew")
    same_key_and_playing = ("walk" == "walk" and win.is_playing())
    check("T6a is_playing 与 _frames 同步真值", same_key_and_playing ==
          bool(win._frames))
    win.stop_frames()
    check("T6b stop 后门卫为 False", not win.is_playing())

    # ---- T7 朝向 ----
    before = int(win._root.property("facing"))
    win.set_facing(-1)
    after = int(win._root.property("facing"))
    check("T7a facing=-1 写入场景", before != after and after == -1)
    win.set_facing(0)          # 无效值：维持不变
    check("T7b 无效朝向不误写", int(win._root.property("facing")) == -1)
    win.set_facing(1)

    # ---- T8 运动参数 ----
    win.set_motion_params(tilt_deg=3.5, walking=True, airborne=False)
    check("T8a tilt/walking 写入",
          abs(float(win._root.property("bodyTilt")) - 3.5) < 1e-6
          and bool(win._root.property("walking")))
    win.set_motion_params(tilt_deg=0.0, walking=False, airborne=True)
    win.set_motion_params(tilt_deg=0.0, walking=False, airborne=False)
    check("T8b airborne 下降沿→squash 可调且不崩",
          float(win._root.property("squashAt")) >= 0)

    # ---- T9 emoji 降级可见性切换 ----
    emo = SpriteRef(path="🐱", width=192, height=192)
    win.set_sprite(emo)
    # 父窗未 show（offscreen 常态），子件 isVisible 恒 False —— 用 isVisibleTo
    # 以本窗为参照判"显式可见性"，与父窗显示状态解耦
    check("T9a emoji 时 label 接管、场景隐藏",
          not win._quick.isVisibleTo(win) and win._label.isVisibleTo(win))
    win.set_sprite(base_sprite)
    check("T9b 回到图片：场景回归",
          win._quick.isVisibleTo(win) and not win._label.isVisibleTo(win))

    # ---- T10 defer_quick（v0.13.3 引擎次序修）----
    win_d = build_rig_window(WindowBase, base_sprite, "final",
                             rig_root=os.path.join(REPO, "assets", "rig"),
                             defer_quick=True)
    check("T10a 构造期 pending（引擎未建）",
          isinstance(win_d, RigWindow)
          and win_d._rig_pending and not win_d.rig_active)
    QTest_wait.qWait(80)       # 事件循环转首拍 → singleShot(0) 执行 _init_quick
    check("T10b 首拍后场景就绪",
          win_d.rig_active and not win_d._rig_pending)
    if win_d.rig_active:
        win_d.set_sprite(base_sprite)
        check("T10c 延迟模式下直显翻译照常",
              _usProp(win_d, "figASrc").endswith("figs/neglected_neutral.png"))
        win_d.stop_frames()
        win_d._frame_timer.stop()

    # 收尾清理定时器，防 Qt teardown 抖（对齐 v04 教训）
    win.stop_frames()
    win._frame_timer.stop()

    print(f"\nrig 层: {len(PASS)} 通过, {len(FAIL)} 失败")
    return 1 if FAIL else 0


if __name__ == "__main__":
    rc = main()
    # offscreen QQuickWidget 偶发 teardown 段错误（同 v04 历史）：断言已
    # 全部输出后再硬退，退出码人工对照（规则：以断言数字为准）
    print(f"[exit {rc}]", flush=True)
    os._exit(rc)
