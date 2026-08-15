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


def _mac_paths() -> dict:
    home = os.path.expanduser("~")
    data_dir = os.path.join(home, "Library", "Application Support", "Desktop-Pet")
    log_dir = os.path.join(home, "Library", "Logs", "Desktop-Pet")
    config_dir = os.path.join(home, ".config", "Desktop-Pet")
    config_path = os.path.join(config_dir, "config.json")
    lock_path = os.path.join(data_dir, "Desktop-Pet.lock")

    for d in (data_dir, log_dir, config_dir):
        os.makedirs(d, exist_ok=True)
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
            """派发 osascript 后台前置已有实例，不阻塞——第二实例立即退出，
            不残留为第二个桌宠进程。唤醒是 best-effort。"""
            if not pid:
                return
            try:
                subprocess.Popen(
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
                )
            except OSError:
                pass

        def hide_dock_icon(self) -> None:
            try:
                from AppKit import (
                    NSApp,
                    NSApplicationActivationPolicyAccessory,
                )

                NSApp.setActivationPolicy_(NSApplicationActivationPolicyAccessory)
            except Exception:
                pass

        def get_sensors(self):
            return sensor_mac.build_sensors()

        def create_pet_window(self, sprite):
            return window_mac.PetWindow(sprite)

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
            self._lock_fd = None

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
            SetForegroundWindow（注意其参数是 HWND，非进程句柄）。"""
            if not pid:
                return
            try:
                import ctypes
                from ctypes import wintypes

                _u = ctypes.WinDLL("user32")

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
