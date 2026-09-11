"""v0.15.1 接回：frames 后端 apply_enrichment 冒烟测试（offscreen，无网络）。

验证原有引擎（WindowBase）消费中间层 Enrichment 的加法层：
* 恒等/None = 零侵入（label 归位、阴影隐藏）；
* 非恒等 = 呼吸浮动移动 label + 地面阴影可见；
* 缺字段/异常 = 静默旁路，不阻断。
"""
from __future__ import annotations

import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PySide6.QtWidgets import QApplication  # noqa: E402

from pet.asset_provider import SpriteRef  # noqa: E402
from pet.engine_bridge import Enrichment  # noqa: E402
from pet.window import WindowBase  # noqa: E402

PASS = 0
FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    print(("  ✅ " if cond else "  ❌ ") + name
          + (f" ({detail})" if detail else ""))
    PASS += cond
    FAIL += (not cond)


def main() -> int:
    app = QApplication.instance() or QApplication(sys.argv)
    sprite = SpriteRef("🐱", 320, 480)   # emoji 路径（无文件 → 文本渲染）
    win = WindowBase(sprite)

    # E1 恒等：零侵入
    win.apply_enrichment(None)
    check("E1a None → label 原位", win._label.y() == 0)
    win.apply_enrichment(Enrichment())
    check("E1b 恒等 → label 原位 + 阴影隐藏",
          win._label.y() == 0 and win._shadow.isHidden())

    # E2 非恒等：呼吸浮动 + 阴影
    win.apply_enrichment(Enrichment(
        body_y=3.0, shadow_alpha=0.35,
        shadow_scale_x=0.45, shadow_scale_y=0.07))
    check("E2a 呼吸浮动移动 label", win._label.y() == 3)
    check("E2b 阴影显示（非隐藏）", not win._shadow.isHidden())
    check("E2c 阴影几何贴合窗口", win._shadow.width() > 0
          and win._shadow.height() > 0,
          f"w={win._shadow.width()} h={win._shadow.height()}")

    # E3 防御：缺字段对象不炸
    class _Partial:
        pass
    win.apply_enrichment(_Partial())
    check("E3 缺字段对象 → 静默旁路", True)

    print(f"\nenrichment_frames 冒烟: {PASS} 通过, {FAIL} 失败")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
