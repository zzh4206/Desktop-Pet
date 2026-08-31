"""v0.14 paper-doll 部件驱动动作自动化验证（offscreen，无窗口交互、无网络）。

覆盖：
  T1  spec：kind 字段三态——limb 解析 / 缺省 sway（旧 manifest 兼容）/ 非法值拒绝
  T2  真实资产：final manifest 腿件 limb 反相成对；装配后 part_walk_active
      随 activeFigure 切换（neutral True / sad False）；基类恒 False
  T3  limb 驱动：walking+walkHz 写入；gaitK 包络渐起/渐收；walkHz=0 走缺省
  T4  app 路由（stub self 直调 _frame_tick）：paperdoll 有 limb 不播帧；
      无 limb / _part_walk=False 回退帧路径
运行：
  python -X utf8 spikes/test_v14_paperdoll.py
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

from PySide6.QtWidgets import QApplication   # noqa: E402

from pet.asset_provider import AIArtProvider, SpriteRef   # noqa: E402
from pet.behavior import ActionType   # noqa: E402
from pet.pet_state import Stage   # noqa: E402
from pet.rig.presenter import RigWindow, build_rig_window   # noqa: E402
from pet.rig.spec import RigPart, load_rig_spec   # noqa: E402
from pet.window import WindowBase   # noqa: E402

PASS, FAIL = [], []


def check(name, cond):
    (PASS if cond else FAIL).append(name)
    print(("  ✅ " if cond else "  ❌ ") + name)


REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _write_manifest(d: str, figures: dict, parts: list) -> str:
    m = {"spec": 1, "figures": figures, "parts": parts}
    p = os.path.join(d, "manifest.json")
    with open(p, "w", encoding="utf-8") as f:
        json.dump(m, f)
    return p


def _qtest():
    return getattr(__import__("PySide6.QtTest", fromlist=["QTest"]), "QTest")


def _usProp(win, name: str) -> str:
    """root 属性(url 型)→ 纯字符串（QUrl 必须走 toString()）。"""
    v = win._root.property(name)
    return v.toString() if hasattr(v, "toString") else str(v)


def main() -> int:
    app = QApplication.instance() or QApplication(sys.argv)
    QTest = _qtest()

    fig_png = os.path.join(REPO, "assets", "ai",
                           "final_neglected_neutral.png")

    # ---- T1 spec kind 三态 ----
    tmp = tempfile.mkdtemp()
    _write_manifest(tmp, {"f": fig_png}, [{
        "id": "leg_l", "file": fig_png.replace("../", ""),
        "source_figure": "f", "px_rect": [1, 2, 3, 4], "pivot": [1, 1],
        "z": "under_core", "kind": "limb",
        "sway": {"amp_deg": 7, "period_ms": 700, "phase_ms": 0},
    }])
    spec = load_rig_spec(tmp, "final")
    check("T1a kind=limb 解析", spec is not None and len(spec.parts) == 1
          and spec.parts[0].kind == "limb")

    tmp2 = tempfile.mkdtemp()
    _write_manifest(tmp2, {"f": fig_png}, [{
        "id": "tail", "file": fig_png.replace("../", ""),
        "source_figure": "f", "px_rect": [1, 2, 3, 4], "pivot": [1, 1],
        "z": "under_core",
        "sway": {"amp_deg": 4, "period_ms": 2600},
    }])
    spec2 = load_rig_spec(tmp2, "final")
    check("T1b 无 kind 缺省 sway（旧 manifest 兼容）",
          spec2 is not None and spec2.parts[0].kind == "sway"
          and isinstance(spec2.parts[0], RigPart))

    tmp3 = tempfile.mkdtemp()
    _write_manifest(tmp3, {"f": fig_png}, [{
        "id": "x", "file": fig_png.replace("../", ""),
        "source_figure": "f", "px_rect": [1, 2, 3, 4], "pivot": [1, 1],
        "z": "under_core", "kind": "bogus",
    }])
    check("T1c 非法 kind 拒绝（整体回退）",
          load_rig_spec(tmp3, "final") is None)

    # ---- T2 真实资产：part_walk_active 随 figure ----
    with open(os.path.join(REPO, "assets", "rig", "final", "manifest.json"),
              encoding="utf-8") as f:
        rm = json.load(f)
    legs = [p for p in rm["parts"] if p.get("kind") == "limb"]
    legs_h = [p for p in legs if p["source_figure"] == "healthy_neutral"]
    legs_side = [p for p in legs if p["source_figure"] == "healthy_side"]
    check("T2a real manifest 腿件 limb 成对（正面对×2 + 侧身对）",
          len(legs) == 6 and len(legs_h) == 2 and len(legs_side) == 2
          and sorted(p["sway"]["phase_ms"] for p in legs_h) == [0.0, 1300.0]
          and all(abs(p["sway"]["amp_deg"] - 7.0) < 1e-6 for p in legs_h))

    base = SpriteRef(path=os.path.join(REPO, "assets", "ai",
                                       "final_healthy_neutral.png"),
                     width=320, height=320)
    win = build_rig_window(WindowBase, base, "final",
                           rig_root=os.path.join(REPO, "assets", "rig"))
    check("T2b final 装配 RigWindow", isinstance(win, RigWindow)
          and win.rig_active)
    if not (isinstance(win, RigWindow) and win.rig_active):
        print(f"\npaperdoll 层: {len(PASS)} 通过, {len(FAIL)} 失败")
        return 1
    check("T2c 基类 part_walk_active 恒 False",
          WindowBase(base).part_walk_active() is False)
    win.set_sprite(base)     # healthy_neutral：有腿件
    check("T2d neutral figure → part_walk_active True",
          win.part_walk_active() is True)
    win.set_sprite(SpriteRef(path=os.path.join(
        REPO, "assets", "ai", "final_healthy_sad.png"), width=320, height=320))
    check("T2e sad figure（无腿件）→ False", win.part_walk_active() is False)
    win.set_sprite(base)

    # ---- T3 limb 驱动包络 ----
    win.set_motion_params(tilt_deg=0, walking=True, walk_hz=1.3)
    check("T3a walkHz 写入", abs(float(win._root.property("walkHz")) - 1.3)
          < 1e-6)
    QTest.qWait(450)         # gaitK 时间常数 ~150ms → 3τ 后 >0.9
    g_up = float(win._root.property("gaitK"))
    check("T3b 起步包络渐起（gaitK>0.8）", g_up > 0.8)
    win.set_motion_params(tilt_deg=0, walking=False, walk_hz=1.3)
    QTest.qWait(500)
    g_down = float(win._root.property("gaitK"))
    check("T3c 停步包络渐收（gaitK<0.15）", g_down < 0.15)
    win.set_motion_params(tilt_deg=0, walking=True, walk_hz=0.0)
    check("T3d walkHz=0 缺省写入（场景按部件周期回退）",
          abs(float(win._root.property("walkHz"))) < 1e-6)
    QTest.qWait(200)
    win.set_motion_params(tilt_deg=0, walking=False, walk_hz=0.0)

    # ---- T3e 淡化策略（v0.14.3）：大姿态动作硬切、blink 保留淡化 ----
    def _seq(name, n):
        return [SpriteRef(os.path.join(
            REPO, "assets", "frames", f"final_{name}_{i}.png"),
            320, 320) for i in range(n)]

    win.play_frames(_seq("stretch", 3), loop=False, interval_ms=260)
    check("T3e-1 stretch 硬切（防双臂残影）", win._fade_ms == 0)
    win.stop_frames()
    land = [SpriteRef(os.path.join(REPO, "assets", "frames",
                                   "final_fall_land.png"), 320, 320)]
    win.play_frames(land, loop=False, interval_ms=200)
    check("T3e-2 land 瞬帧硬切", win._fade_ms == 0)
    win.stop_frames()
    win.play_frames(_seq("idle_blink", 2), loop=True, interval_ms=300)
    check("T3e-3 blink 保留交叉淡化（>0）", win._fade_ms > 0)
    win.stop_frames()

    # ---- T5 行走覆盖（v0.14.4）：mood 图行走改显 neutral 核心 ----
    from pet.pet_state import Branch   # noqa: E402
    from pet.asset_provider import AIArtProvider   # noqa: E402

    happy = SpriteRef(os.path.join(REPO, "assets", "ai",
                                   "final_healthy_happy.png"),
                      width=320, height=320)
    neutral = SpriteRef(os.path.join(REPO, "assets", "ai",
                                     "final_healthy_neutral.png"),
                        width=320, height=320)
    win.set_sprite(happy)
    check("T5a happy 图无腿部件（回退前提成立）",
          win.part_walk_active() is False)
    win.set_sprite_provider(AIArtProvider())
    win.set_walk_figure(neutral)
    win.set_motion_params(tilt_deg=0, walking=True, walk_hz=1.3)
    check("T5b walking 沿改显 neutral 派生核心",
          _usProp(win, "figASrc").endswith("figs/healthy_neutral.png")
          and win._root.property("activeFigure") == "healthy_neutral"
          and win.part_walk_active() is True)
    win.on_state_change(types.SimpleNamespace(
        stage=Stage.FINAL, branch=Branch.HEALTHY, mood=80, fullness=80))
    check("T5c 覆盖期间衰减 tick 不翻回 mood 图",
          _usProp(win, "figASrc").endswith("figs/healthy_neutral.png"))
    win.set_motion_params(tilt_deg=0, walking=False, walk_hz=0.0)
    check("T5d 停步还原 mood 立绘",
          _usProp(win, "figASrc").endswith("final_healthy_happy.png")
          and win.part_walk_active() is False)

    # ---- T5e 侧身行走载体（v0.14.6）：walk_0 拷贝 + 前后腿拆件 ----
    prov = win._provider
    side = prov.side_walk_static(types.SimpleNamespace(
        stage=Stage.FINAL, branch=Branch.HEALTHY, mood=80, fullness=80))
    check("T5e-1 side_walk_static 指向侧身拷贝",
          side is not None and side.path.endswith("final_healthy_side.png"))
    win.set_walk_figure(side)
    win.set_motion_params(tilt_deg=0, walking=True, walk_hz=1.3)
    check("T5e-2 walking 改显侧身核心且腿件就绪",
          _usProp(win, "figASrc").endswith("figs/healthy_side.png")
          and win._root.property("activeFigure") == "healthy_side"
          and win.part_walk_active() is True)
    n_side_limbs = sum(1 for p in win._spec.parts
                       if p.kind == "limb" and p.source_figure == "healthy_side")
    check("T5e-3 侧身前后腿 limb 成对", n_side_limbs == 2)
    win.set_motion_params(tilt_deg=0, walking=False, walk_hz=0.0)

    # ---- T6 批次F/H4（REVIEW-2026-08-28）：行走中 set_facing 不翻回 mood ----
    win.set_sprite(happy)
    win.set_walk_figure(neutral)
    win.set_motion_params(tilt_deg=0, walking=True, walk_hz=1.3)
    win.set_facing(-1)
    check("T6a 行走中翻转朝向不切 figure（守卫生效，第三类回退封堵）",
          _usProp(win, "figASrc").endswith("figs/healthy_neutral.png")
          and win._root.property("activeFigure") == "healthy_neutral"
          and int(win._root.property("facing")) == -1
          and win.part_walk_active() is True)
    win.set_facing(1)
    win.set_motion_params(tilt_deg=0, walking=False, walk_hz=0.0)
    check("T6b 停步后恢复正常换图（happy 还原）",
          _usProp(win, "figASrc").endswith("final_healthy_happy.png"))

    # ---- T7 批次F/C1：部件两两 α 交叠门禁（静止合成对此失明）----
    from importlib.util import module_from_spec, spec_from_file_location
    _qa_spec = spec_from_file_location(
        "qa_rig_gate", os.path.join(REPO, "tools", "qa_rig_composite.py"))
    qa_gate = module_from_spec(_qa_spec)
    _qa_spec.loader.exec_module(qa_gate)
    worst = 0
    worst_pair = ""
    for st in ("young", "adult", "final"):
        rd = os.path.join(REPO, "assets", "rig", st)
        with open(os.path.join(rd, "manifest.json"), encoding="utf-8") as f:
            mf = json.load(f)
        for fig in mf["figures"]:
            for (_a, _b, n) in qa_gate.part_overlap_px(rd, mf, fig):
                if n > worst:
                    worst, worst_pair = n, f"{st}/{fig}:{_a}×{_b}"
    check(f"T7 全资产部件两两 α 交叠 ≤256px（峰値 {worst}px {worst_pair}）",
          worst <= 256)

    # ---- T8 批次F/rM2：manifest 路径已归一为正斜杠 ----
    _paths_ok = True
    for st in ("young", "adult", "final"):
        with open(os.path.join(REPO, "assets", "rig", st, "manifest.json"),
                  encoding="utf-8") as f:
            mf = json.load(f)
        _paths_ok &= all("\\" not in v for v in mf["figures"].values())
        _paths_ok &= all("\\" not in p["file"] for p in mf["parts"])
    check("T8 manifest 路径全部正斜杠（跨平台可解析）", _paths_ok)

    # ---- T9 批次F/rM1：相位累加器——hz 变化只改斜率不瞬移 ----
    win.set_sprite(base)
    win.set_walk_figure(neutral)
    win.set_motion_params(tilt_deg=0, walking=True, walk_hz=1.3)
    QTest.qWait(400)
    ph0 = float(win._root.property("gaitPhase"))
    win.set_motion_params(tilt_deg=0, walking=True, walk_hz=2.0)
    QTest.qWait(150)
    ph1 = float(win._root.property("gaitPhase"))
    adv = (ph1 - ph0) % 1.0
    check(f"T9 相位累加器：hz 1.3→2.0 相位连续推进（150ms 实推 {adv:.2f} 周期）",
          0.18 < adv < 0.45)
    win.set_motion_params(tilt_deg=0, walking=False, walk_hz=0.0)

    win.set_walk_figure(None)
    win.set_sprite(base)

    # ---- T4 app._frame_tick 路由（stub self，不起完整 PetApp）----
    import app as app_mod

    class StubWin:
        def __init__(self):
            self.played = []
            self.stopped = False

        def part_walk_active(self):
            return self._pwa

        def is_playing(self):
            return False

        def width(self):
            return 320

        def height(self):
            return 320

        def play_frames(self, frames, loop=False, interval_ms=150):
            self.played.append(frames)

        def stop_frames(self):
            self.stopped = True

    def _stub(_pwa, _pw):
        w = StubWin()
        w._pwa = _pwa
        ns = types.SimpleNamespace(
            provider=AIArtProvider(),
            store=types.SimpleNamespace(get=lambda: types.SimpleNamespace(
                stage=Stage.FINAL)),
            fsm=types.SimpleNamespace(motion_mode="free"),
            window=w, _anim_key=None, _part_walk=_pw,
            _SMALL_ANIM_KEYS=app_mod.PetApp._SMALL_ANIM_KEYS)
        # SimpleNamespace 属性不自动绑定——显式绑 method 才能带 self 调用
        for name in ("_play_key", "_stop_anim", "_frame_tick"):
            setattr(ns, name, types.MethodType(
                getattr(app_mod.PetApp, name), ns))
        return ns

    action = types.SimpleNamespace(type=ActionType.MOVE_TO, params={})

    s = _stub(_pwa=True, _pw=True)
    s._frame_tick(action, "walk", "idle")
    check("T4a paperdoll+limb：不播 walk 帧",
          s.window.played == [] and s._anim_key is None)

    # N3（实机审查 2026-08-31）：无 _anim_key 属性时 _frame_tick 不抛——
    # 真机 PetApp 旧版首赋值在 _play_key，行走先于首个小动作即每拍
    # AttributeError（stub 复刻该形态：不预置 _anim_key）
    s = _stub(_pwa=True, _pw=True)
    del s._anim_key
    raised = False
    try:
        s._frame_tick(action, "walk", "idle")
    except AttributeError:
        raised = True
    check("T4a-2 无 _anim_key 属性不抛（行走先于小动作）",
          not raised and s.window.played == [])

    s = _stub(_pwa=False, _pw=True)
    s._frame_tick(action, "walk", "idle")
    check("T4b 无 limb figure：回退帧路径",
          len(s.window.played) == 1 and s._anim_key == "walk")

    s = _stub(_pwa=True, _pw=False)
    s._frame_tick(action, "walk", "idle")
    check("T4c _part_walk=False（rig 档）：帧路径原样",
          len(s.window.played) == 1 and s._anim_key == "walk")

    # ---- T10 批次G/rL6（REVIEW-2026-08-31 N1）：静止合成门禁常驻 ----
    # 全 figure（3 阶段 × 2 分支 neutral + final 侧身）跑内部失配 ≤0.5%
    # + 部件交叠 ≤256px——旧版门禁只靠手动跑工具，final/healthy_neutral
    # 尾件基线 FAIL 长期无报警。exclude 基线读 manifest qa.exclude 段
    gate_rows = []
    for st in ("young", "adult", "final"):
        for br in ("healthy", "neglected"):
            gate_rows.append(qa_gate.evaluate(st, br, "neutral",
                                              write_qa=False))
    gate_rows.append(qa_gate.evaluate("final", "healthy", "side",
                                      write_qa=False))
    bad = [r["figure"] for r in gate_rows if not r["ok"]]
    worst = max(gate_rows, key=lambda r: r["inter_pct"])
    check(f"T10 静止合成门禁 7 figure 全过（最差 {worst['figure']} "
          f"{worst['inter_pct']:.3f}%）" + (f"，FAIL={bad}" if bad else ""),
          not bad)

    # 收尾清理定时器，防 Qt teardown 抖（对齐 v04/v13 教训）
    win.stop_frames()
    win._frame_timer.stop()

    print(f"\npaperdoll 层: {len(PASS)} 通过, {len(FAIL)} 失败")
    return 1 if FAIL else 0


if __name__ == "__main__":
    rc = main()
    print(f"[exit {rc}]", flush=True)
    os._exit(rc)
