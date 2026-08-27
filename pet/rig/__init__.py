"""分层绑骨（paper-doll）呈现层 —— 版本规划.md §v0.13.0。

与既有帧动画（``presentation="frames"``）并存的第二套展示后端：
``pet.rig.presenter.RigWindow`` 继承 ``WindowBase`` 复用全部手势/菜单/拖放，
把画面驱动换成 Qt Quick 场景（交叉淡化 + 全局变换 + 部件弹簧），动画过程
零新生成像素 —— 同阶段画风由构造保持一致。

模块仅含平台中立代码；QML 场景在 ``rig_scene.qml``，部件资产与清单在
``assets/rig/{stage}/``（缺文件自动回退 frames 模式，见 app.py 装配处）。
"""

from .spec import RigSpec, load_rig_spec

__all__ = ["RigSpec", "load_rig_spec"]
