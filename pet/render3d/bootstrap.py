"""render3d 装配入口——flag 读取、初始化、降级决策（S2.9 的模块侧半边）。

对外接口（设计-子模块与接口.md §2）：
  create_renderer(cfg) -> SceneRenderer | None   # None=降级（调用方走 2D rig）
  spawn_preview(cfg)   -> Render3DWindow | None  # M1 实验轨预览窗（独立于 app 主窗）

铁律（D04/D05/D16）：
* cfg 未开 render3d.enabled → 直接 None（默认关，云端推送恒 false）。
* 任何环节异常 → None + 单行日志，**永不抛给调用方**。
* safe_rss_mb 看门狗在 window 内周期执行（D16），超限自动 hide + is_ready=False。

独立冒烟（脱离 app 验证全链路，产物=JSON 结果行 + 可选截图）::

    python -m pet.render3d.bootstrap --smoke --duration 6 --screenshot
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import time

from pet.render3d import assets

logger = logging.getLogger("pet.render3d")

DEFAULTS: dict = {
    "enabled": False,       # D05：默认关；云端推送恒 false
    "stage": "adult",
    "light_level": 1,       # D03：L1 toon 明暗首发档
    "fps_cap": 30,
    "safe_rss_mb": 500.0,   # D16 安全上限（运行时看门狗）
    "window_size_w": 240,
    "window_size_h": 420,
}


def read_section(cfg: dict | None) -> dict:
    """旧配置无 render3d 键 → 全默认（不炸）；非法类型回默认值。"""
    section = dict(DEFAULTS)
    for k, v in ((cfg or {}).get("render3d") or {}).items():
        if k in section and type(v) is type(section[k]):
            section[k] = v
    return section


def create_renderer(cfg: dict | None):
    """装配契约适配器；返回对象实现 SceneRenderer 协议（apply/is_ready）。

    依赖链（设计文档 §1）：assets 校验 → scene_host 窗 → adapter；任一环
    失败 → None（=EngineBridge/app 走 2D rig，用户无感）。**不 show()**——
    显隐由调用方（M1 预览用 spawn_preview；集成形态 S1.4 后定）。
    """
    sec = read_section(cfg)
    if not sec.get("enabled"):
        return None
    try:
        bundle = assets.load(sec["stage"], assets.default_root())
        if bundle is None:
            return None
        from pet.render3d.contract_adapter import Render3DAdapter
        from pet.render3d.scene_host import Render3DWindow

        window = Render3DWindow(bundle.qml_path, sec,
                                asset_dir=os.path.dirname(bundle.qml_path))
        return Render3DAdapter(window, level=int(sec["light_level"]),
                               sidecars=bundle.sidecars)
    except Exception:
        logger.warning("render3d 初始化失败，降级 2D", exc_info=True)
        return None


def spawn_preview(cfg: dict | None):
    """M1 实验轨：创建并 show 3D 预览窗（伴随 2D 主宠物；失败=无事发生）。"""
    renderer = create_renderer(cfg)
    if renderer is None:
        return None
    try:
        renderer._window.place_bottom_right()
        renderer._window.show()
        return renderer
    except Exception:
        logger.warning("render3d 预览窗显示失败", exc_info=True)
        return None


# --------------------------------------------------------------------------- #
# 独立冒烟入口
# --------------------------------------------------------------------------- #

def _smoke(duration: float, screenshot: bool) -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QApplication

    app = QApplication([])          # 必须先于 QQuickView（无 app 建 window=未定义行为）
    cfg = {"render3d": {"enabled": True, "stage": "adult"}}
    t0 = time.monotonic()
    renderer = spawn_preview(cfg)
    if renderer is None:
        print("SMOKE_RESULT " + json.dumps({"ok": False, "reason": "create_renderer=None"}))
        return

    from pet.scene_contract import SceneState, LightWeatherState, PoseSemantics, ExpressionState

    # 演示驱动：黄昏暖光 + 走路相位 + happy 表情
    state = SceneState(
        light=LightWeatherState(sun_azimuth_deg=250, sun_elevation_deg=18,
                                color_temp_k=3200, sun_intensity=1.0),
        pose=PoseSemantics(action_id="walk", phase=0.0, view_yaw_deg=15),
        expression=ExpressionState(emotion_label="happy"),
    )

    def tick() -> None:
        state = SceneState(
            light=LightWeatherState(sun_azimuth_deg=250, sun_elevation_deg=18,
                                    color_temp_k=3200, sun_intensity=1.0),
            pose=PoseSemantics(action_id="walk",
                               phase=(time.monotonic() * 1.2) % 1.0, view_yaw_deg=15),
            expression=ExpressionState(emotion_label="happy", blink_progress=0.0),
        )
        renderer.apply(state)

    timer = QTimer()
    timer.setInterval(33)
    timer.timeout.connect(tick)
    timer.start()
    tick()

    def shot() -> None:
        from PySide6.QtCore import QTimer
        from PySide6.QtGui import QGuiApplication
        img = QGuiApplication.primaryScreen().grabWindow(renderer._window.win_id)
        if img.isNull():               # 首帧未合成时偶发空图：延迟重试一次
            QTimer.singleShot(800, shot)
            return
        out = "/tmp/render3d_smoke_shot.png"
        img.save(out)
        print(f"SMOKE_SHOT {out} {img.width()}x{img.height()}")

    def finish() -> None:
        from pet.render3d.scene_host import _rss_mb
        print("SMOKE_RESULT " + json.dumps({
            "ok": True,
            "ready": renderer.is_ready(),
            "elapsed_s": round(time.monotonic() - t0, 1),
            "rss_mb": round(_rss_mb(), 1),
            "morph_weights": renderer.morph_weights,
            "spring_nodes": len(renderer.spring_points[0]) if renderer.spring_points else 0,
        }, ensure_ascii=False))
        app.quit()

    if screenshot:
        QTimer.singleShot(int(duration * 1000 * 0.6), shot)
    QTimer.singleShot(int(duration * 1000), finish)
    app.exec()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--smoke", action="store_true")
    ap.add_argument("--duration", type=float, default=6.0)
    ap.add_argument("--screenshot", action="store_true")
    args = ap.parse_args()
    if args.smoke:
        os.environ.setdefault("RENDER3D_ASSET_ROOT",
                              os.path.join(os.path.dirname(os.path.dirname(
                                  os.path.abspath(__file__))), "..", "three_d_assets", "models"))
        _smoke(args.duration, args.screenshot)
    else:
        ap.print_help()
