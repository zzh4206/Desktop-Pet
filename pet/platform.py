"""按 sys.platform 选平台 shim 工厂 —— 设计思路.md §2.1 / 补遗#3。

``platform.py`` 是平台库隔离的**例外**（注入点）：允许 import ``fcntl``/
``AppKit`` 及 ``_mac`` shim（``sensor_mac``/``window_mac``）——所有平台特定
代码集中于此，让 ``app.py`` / ``window.py`` 等共享文件平台库-free。

v0.1 把单实例锁（fcntl 文件锁 + osascript 唤醒）/ dock 隐藏（NSApp policy）/
传感器选取（sensor_mac）/ 浮窗创建（window_mac）都收进 ``MacPlatformAdapter``。
``PlatformAdapter`` 接口仍在本文件定义，**不进 设计思路.md §2.2**——等 v0.3
用稳了再走留痕流程正式冻结。

win 分支 ``WinPlatformAdapter``（v0.1.3 填齐：msvcrt 单实例锁 / sensor_win /
window_win / %LOCALAPPDATA% 路径）。app 数据目录名是 ASCII
``Desktop-Pet``（非中文，防路径编码问题）。
"""

from __future__ import annotations

import os
import subprocess
import sys


class PlatformAdapter:
    """平台注入点接口。v0.1 在 platform.py 定义，不进 设计思路.md §2.2。"""

    def __init__(self, data_dir, log_dir, config_path, lock_path):
        self.data_dir = data_dir
        self.log_dir = log_dir
        self.config_path = config_path
        self.lock_path = lock_path

    def get_paths(self) -> dict:
        return {
            "data_dir": self.data_dir,
            "log_dir": self.log_dir,
            "config_path": self.config_path,
            "lock_path": self.lock_path,
        }

    # 平台行为接口（mac 实现见 MacPlatformAdapter；win 端以后填）
    def acquire_single_instance_lock(self) -> bool:
        """获取单实例锁。True=本进程是主实例，继续；False=已有实例，
        本进程应退出（已派发唤醒已有实例）。"""
        raise NotImplementedError

    def hide_dock_icon(self) -> None:
        """让 app 不入 Dock（mac）/ no-op（win，无此概念）。"""
        pass

    def get_sensors(self):
        raise NotImplementedError

    def create_pet_window(self, sprite):
        raise NotImplementedError

    def is_fullscreen_active(self) -> bool:
        """前台是否全屏窗口（v0.3 全屏/演示检测）。

        基类返回 False（不抑制）；mac/win 各自覆盖。
        v0.12.1：删冗余 is_fullscreen 中间层（仅 mac 自家调用），
        mac 直接在 is_fullscreen_active 调 sensor_mac.fullscreen_status。
        """
        return False

    def register_own_windows(self, *widgets) -> None:
        """登记宠物自身窗口（本体/气泡）——图层探针排除自身遮挡。

        win 端实装（sensor_win.set_own_hwnds）；mac 端 no-op（v0.3.13 图层
        实装后如遇同类问题再补）。
        """
        pass

    # ---- v0.4 DS key + 危险确认（平台密钥库/原生对话框，经此注入） ----
    def get_ds_key(self) -> str | None:
        """DS API key：平台密钥库优先，env ``DEEPSEEK_API_KEY`` 兜底。

        都无→返 None（app 触发首次引导）。**不入 config.json 明文**。
        基类返回 env 兜底（无密钥库概念），mac/win 覆盖。
        """
        return os.environ.get("DEEPSEEK_API_KEY") or None

    def set_ds_key(self, key: str) -> None:
        """存入平台密钥库。基类 no-op（无密钥库概念时调用方应自行降级）。"""
        pass

    def confirm_dangerous(
        self, title: str, command: str, risk: str
    ) -> bool:
        """危险操作二次确认（平台原生模态对话框）。

        v0.8.1：基类默认 False（fail-closed 拒绝危险操作，旧版 True 放行与
        "危险操作必须二次确认" Must 相悖）。mac/win 子类覆盖用原生模态对话框；
        失败兜底也返 False（拒绝）。
        """
        return False


def _mac_paths() -> dict:
    home = os.path.expanduser("~")
    data_dir = os.path.join(home, "Library", "Application Support", "Desktop-Pet")
    log_dir = os.path.join(home, "Library", "Logs", "Desktop-Pet")
    config_dir = os.path.join(home, ".config", "Desktop-Pet")
    config_path = os.path.join(config_dir, "config.json")
    lock_path = os.path.join(data_dir, "Desktop-Pet.lock")

    for d in (data_dir, log_dir, config_dir):
        os.makedirs(d, exist_ok=True)
        # v0.12.1：data_dir/log_dir 收紧到 0o700（存 pet_state.json 含养成数据，
        # 旧版 0o755 其他用户可读；config_dir 同理）
        try:
            os.chmod(d, 0o700)
        except OSError:
            pass
    if os.path.exists(config_path):
        try:
            os.chmod(config_path, 0o600)
        except OSError:
            pass
    return {
        "data_dir": data_dir,
        "log_dir": log_dir,
        "config_path": config_path,
        "lock_path": lock_path,
    }


# mac 平台实现（注入点例外：允许 import 平台库 + _mac shim）。
# 放在 if sys.platform 块内，win 端 import 本模块时不触发这些 import。
if sys.platform == "darwin":
    import fcntl

    from . import sensor_mac, window_mac

    class MacPlatformAdapter(PlatformAdapter):
        def __init__(self, data_dir, log_dir, config_path, lock_path):
            super().__init__(data_dir, log_dir, config_path, lock_path)
            self._lock_fd: int | None = None  # 持有 fd 防释放锁

        def acquire_single_instance_lock(self) -> bool:
            fd = os.open(self.lock_path, os.O_CREAT | os.O_RDWR, 0o644)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                pid = self._read_pid(fd)
                os.close(fd)
                self._activate_existing(pid)
                return False
            os.ftruncate(fd, 0)
            os.write(fd, str(os.getpid()).encode())
            self._lock_fd = fd  # 进程存活期间保持锁
            return True

        @staticmethod
        def _read_pid(fd: int) -> int | None:
            try:
                os.lseek(fd, 0, os.SEEK_SET)
                data = os.read(fd, 64).strip()
                return int(data) if data else None
            except (OSError, ValueError):
                return None

        @staticmethod
        def _activate_existing(pid: int | None) -> None:
            """派发 osascript 前置已有实例。v0.12.1：改 subprocess.run timeout
            （旧版 Popen 不 wait，Popen 对象 GC 可能杀掉 osascript 致唤醒失败）。"""
            if not pid:
                return
            try:
                subprocess.run(
                    [
                        "osascript",
                        "-e",
                        (
                            "tell application \"System Events\" to set "
                            f"frontmost of (first process whose unix id is "
                            f"{pid}) to true"
                        ),
                    ],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=5,
                )
            except (OSError, subprocess.TimeoutExpired):
                pass

        def hide_dock_icon(self) -> None:
            try:
                from AppKit import (
                    NSApp,
                    NSApplicationActivationPolicyAccessory,
                )

                NSApp.setActivationPolicy_(NSApplicationActivationPolicyAccessory)
            except Exception as exc:
                import logging
                logging.getLogger("pet").warning(
                    "hide_dock_icon 失败（AppKit 未装?）: %s", exc)

        def get_sensors(self):
            return sensor_mac.build_sensors()

        def is_fullscreen_active(self) -> bool:
            # v0.12.1：删冗余 is_fullscreen 中间层，直接调 sensor_mac
            return sensor_mac.fullscreen_status()[0]

        def create_pet_window(self, sprite):
            return window_mac.PetWindow(sprite)

        # ---- v0.3.13 mac 适配：图层探针排除自身（win register_own_windows 同类）----
        def register_own_windows(self, *widgets) -> None:
            """登记宠物自身窗口（本体/气泡）——solid_at 探针被自身遮挡时放行。
            widget.winId()→NSView→NSWindow→windowNumber()（CGWindowNumber）。"""
            from ctypes import c_void_p

            from objc import objc_object

            wids = set()
            for w in widgets:
                try:
                    view = objc_object(c_void_p=int(w.winId()))
                    nswin = view.window() if view is not None else None
                    if nswin is not None:
                        wids.add(int(nswin.windowNumber()))
                except Exception as exc:
                    import logging
                    logging.getLogger("pet").warning(
                        "register_own_windows 登记 widget 失败: %s", exc)
            sensor_mac.set_own_wids(wids)

        # ---- v0.4：DS key 存 Keychain / 危险确认 NSAlert ----
        def get_ds_key(self) -> str | None:
            """Keychain（keyring）优先 → env ``DEEPSEEK_API_KEY`` 兜底 → None。

            不入 config.json 明文。keyring 失败（Keychain 损坏/无授权）时
            静默回退 env + 日志，不崩。
            """
            try:
                import keyring

                key = keyring.get_password("Desktop-Pet", "ds_api_key")
                if key:
                    return key
            except Exception as exc:  # Keychain 未解锁/后端不可用
                import logging

                logging.getLogger("pet").warning(
                    "Keychain 读取 DS key 失败，回退 env: %s", exc
                )
            return os.environ.get("DEEPSEEK_API_KEY") or None

        def set_ds_key(self, key: str) -> None:
            """存 Keychain。失败不崩（调用方 next get_ds_key 取 env 兜底）。"""
            try:
                import keyring

                keyring.set_password("Desktop-Pet", "ds_api_key", key)
            except Exception as exc:
                import logging

                logging.getLogger("pet").warning("Keychain 存 DS key 失败: %s", exc)

        def confirm_dangerous(
            self, title: str, command: str, risk: str
        ) -> bool:
            """NSAlert 模态确认（显示命令+风险）。v0.4 框架就位不触发
            （open_app 不危险）；v0.8 全工具用。v0.8.1：失败 fail-closed
            返 False（拒绝，旧版 True 放行违反安全 Must）。按钮：第一=继续/第二=取消。

            **线程安全**（v0.8.1）：NSAlert runModal 必须主线程；本方法经
            ToolRegistry.dispatch 在 ChatWorker 子线程被调时，由 dispatch 负责
            跨线程派发（BlockingQueuedConnection），此处假设已在主线程。
            """
            try:
                from AppKit import (
                    NSAlertFirstButtonReturn,
                    NSAlertStyleInformational,
                    NSAlert,
                )

                alert = NSAlert.alloc().init()
                alert.setMessageText_(title)
                alert.setInformativeText_(f"命令：{command}\n风险：{risk}")
                alert.setAlertStyle_(NSAlertStyleInformational)
                alert.addButtonWithTitle_("继续")
                alert.addButtonWithTitle_("取消")
                return alert.runModal() == NSAlertFirstButtonReturn
            except Exception as exc:
                import logging

                logging.getLogger("pet").warning(
                    "NSAlert 失败，fail-closed 拒绝: %s", exc
                )
                return False

    def get_platform_adapter() -> PlatformAdapter:
        p = _mac_paths()
        return MacPlatformAdapter(
            p["data_dir"], p["log_dir"], p["config_path"], p["lock_path"]
        )

elif sys.platform == "win32":

    import msvcrt

    from . import sensor_win, window_win

    class WinPlatformAdapter(PlatformAdapter):
        """win 注入点（v0.1）：路径 %LOCALAPPDATA% / msvcrt 文件锁 +
        SetForegroundWindow 唤醒 / sensor_win / window_win。"""

        def __init__(self, data_dir, log_dir, config_path, lock_path):
            super().__init__(data_dir, log_dir, config_path, lock_path)
            self._lock_fd: int | None = None  # v0.12.1：注解对齐 mac

        def acquire_single_instance_lock(self) -> bool:
            fd = os.open(self.lock_path, os.O_CREAT | os.O_RDWR)
            try:
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            except OSError:
                pid = self._read_pid(fd)
                os.close(fd)
                self._activate_existing(pid)
                return False
            os.ftruncate(fd, 0)
            os.write(fd, str(os.getpid()).encode())
            os.lseek(fd, 0, os.SEEK_SET)  # 解锁/再锁从文件头开始
            self._lock_fd = fd  # 进程存活期间保持锁
            return True

        @staticmethod
        def _read_pid(fd: int) -> int | None:
            try:
                os.lseek(fd, 0, os.SEEK_SET)
                data = os.read(fd, 64).strip()
                return int(data) if data else None
            except (OSError, ValueError):
                return None

        @staticmethod
        def _activate_existing(pid: int | None) -> None:
            """best-effort 前置已有实例：按 pid 枚举可见顶层窗口后
            SetForegroundWindow（注意其参数是 HWND，非进程句柄）。

            v0.12.1：加 AllowSetForegroundWindow(ASFW_ANY) 解除前台锁定
            （旧版第二实例无前台权限，SetForegroundWindow 静默失败不前置）。
            多窗口时取第一个可见顶层窗（宠物本体通常唯一，聊天窗隐藏态）。
            """
            if not pid:
                return
            try:
                import ctypes
                from ctypes import wintypes

                _u = ctypes.WinDLL("user32")
                # P3：解除前台锁定，让 SetForegroundWindow 生效
                try:
                    ASFW_ANY = -1  # DWORD(-1) = ASFW_ANY
                    _u.AllowSetForegroundWindow(ASFW_ANY)
                except Exception:
                    pass  # 旧版 Windows 可能无此 API

                @ctypes.WINFUNCTYPE(
                    wintypes.BOOL, wintypes.HWND, wintypes.LPARAM
                )
                def _on_hwnd(hwnd, _lparam):
                    owner = wintypes.DWORD()
                    _u.GetWindowThreadProcessId(
                        hwnd, ctypes.byref(owner)
                    )
                    if owner.value == pid and _u.IsWindowVisible(hwnd):
                        _u.SetForegroundWindow(hwnd)
                        return False  # 找到即停
                    return True

                _u.EnumWindows(_on_hwnd, 0)
            except Exception:
                pass  # 唤醒失败不阻塞第二实例退出

        def get_sensors(self):
            return sensor_win.build_sensors()

        def create_pet_window(self, sprite):
            return window_win.PetWindow(sprite)

        def is_fullscreen_active(self) -> bool:
            return sensor_win.foreground_fullscreen()[0]

        def register_own_windows(self, *widgets) -> None:
            ids = []
            for wgt in widgets:
                try:
                    ids.append(int(wgt.winId()))
                except Exception:
                    pass
            sensor_win.set_own_hwnds(ids)

        # ---- v0.4：DS key 存 Windows 凭据管理器 / 危险确认 Qt 对话框 ----
        def get_ds_key(self) -> str | None:
            """凭据管理器（keyring）优先 → env DEEPSEEK_API_KEY 兜底 → None。

            不入 config.json 明文。keyring 失败时静默回退 env + 日志，不崩
            （与 mac Keychain 路径行为对齐）。"""
            try:
                import keyring

                key = keyring.get_password("Desktop-Pet", "ds_api_key")
                if key:
                    return key
            except Exception as exc:
                import logging

                logging.getLogger("pet").warning(
                    "凭据管理器读取 DS key 失败，回退 env: %s", exc
                )
            return os.environ.get("DEEPSEEK_API_KEY") or None

        def set_ds_key(self, key: str) -> None:
            """存凭据管理器。失败不崩（调用方 next get_ds_key 取 env 兜底）。"""
            try:
                import keyring

                keyring.set_password("Desktop-Pet", "ds_api_key", key)
            except Exception as exc:
                import logging

                logging.getLogger("pet").warning(
                    "凭据管理器存 DS key 失败: %s", exc
                )

        def confirm_dangerous(self, title: str, command: str,
                              risk: str) -> bool:
            """Qt 模态确认（显示命令+风险），按钮文案与 mac NSAlert 对齐
            （继续/取消）。v0.4 open_app 不危险不触发；v0.8 全工具用。
            v0.8.1：失败 fail-closed 返 False（拒绝，旧版 True 放行违反安全 Must）。

            **线程安全**（v0.8.1）：QMessageBox.exec 必须主线程；本方法经
            ToolRegistry.dispatch 在 ChatWorker 子线程被调时，由 dispatch 负责
            跨线程派发（BlockingQueuedConnection），此处假设已在主线程。
            """
            try:
                from PySide6.QtWidgets import QMessageBox

                box = QMessageBox()
                box.setIcon(QMessageBox.Icon.Warning)
                box.setWindowTitle(title)
                box.setText(f"命令：{command}\n风险：{risk}")
                cont = box.addButton("继续", QMessageBox.ButtonRole.YesRole)
                box.addButton("取消", QMessageBox.ButtonRole.NoRole)
                box.exec()
                return box.clickedButton() is cont
            except Exception as exc:
                import logging

                logging.getLogger("pet").warning(
                    "Qt 确认框失败，fail-closed 拒绝: %s", exc
                )
                return False

    def _win_paths() -> dict:
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser(
            "~/AppData/Local"
        )
        data_dir = os.path.join(base, "Desktop-Pet")
        log_dir = os.path.join(data_dir, "logs")
        config_path = os.path.join(data_dir, "config.json")
        lock_path = os.path.join(data_dir, "Desktop-Pet.lock")

        for d in (data_dir, log_dir):
            os.makedirs(d, exist_ok=True)
        return {
            "data_dir": data_dir,
            "log_dir": log_dir,
            "config_path": config_path,
            "lock_path": lock_path,
        }

    def get_platform_adapter() -> PlatformAdapter:
        p = _win_paths()
        return WinPlatformAdapter(
            p["data_dir"], p["log_dir"], p["config_path"], p["lock_path"]
        )

else:

    def get_platform_adapter() -> PlatformAdapter:
        raise NotImplementedError(
            f"platform {sys.platform!r} not handled (mac/win only)"
        )
