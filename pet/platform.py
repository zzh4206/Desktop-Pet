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

    # ---- v0.8 窗口 Space 管理（全屏时聊天面板移桌面 Space）----
    def move_window_to_all_spaces(self, widget) -> bool:
        """让窗口加入所有 Space（不跟全屏 app）——全屏时聊天面板可见。
        mac 实装 NSWindow collectionBehavior；基类/win no-op 返 False。"""
        return False

    def start_hotkeys(self, cfg: dict, on_chat, on_spit,
                      on_conflict=None) -> bool:
        """v0.11 全局热键注册。基类返 False（无实现）。"""
        return False

    def stop_hotkeys(self) -> None:
        """v0.11 注销热键。基类 no-op。"""
        pass

    def set_autostart(self, enabled: bool) -> bool:
        """v0.11 开机自启开关。基类返 False。"""
        return False

    def is_autostart_enabled(self) -> bool:
        """v0.11 读自启状态。基类返 False。"""
        return False

    def open_path(self, path: str) -> tuple:
        """v0.9 打开本地文件/文件夹（拖放用）。返回 (ok, msg)。"""
        raise NotImplementedError

    def register_own_windows(self, *widgets) -> None:
        """登记宠物自身窗口（本体/气泡）——图层探针排除自身遮挡。

        win 端实装（sensor_win.set_own_hwnds）；mac 端 no-op（v0.3.13 图层
        实装后如遇同类问题再补）。
        """
        pass

    # ---- v0.7 吃鼠标平台注入（mac 实装；win 待 WH_MOUSE_LL；基类 no-op
    # 让共享 app 接线统一，EatMouseSession 见 mouse_lock=None 即静默不抑制） ----
    def get_mouse_lock(self):
        """平台鼠标抑制对象（mac=MouseLockMac）。基类返 None（不抑制）。"""
        return None

    def start_mouse_lock(self, duration_s: float) -> bool:
        return False

    def stop_mouse_lock(self) -> None:
        pass

    def is_mouse_locked(self) -> bool:
        return False

    def is_accessibility_trusted(self) -> bool:
        return False

    def is_active_content(self, video_apps) -> bool:
        return False

    def prompt_accessibility(self) -> None:
        pass

    # ---- v0.4 DS key + 危险确认（平台密钥库/原生对话框，经此注入） ----
    def get_ds_key(self) -> str | None:
        """DS API key（v0.4 单 provider 遗留，兼容旧调用）。v0.4.15 起走
        get_llm_key("deepseek", "DEEPSEEK_API_KEY")。"""
        return self.get_llm_key("deepseek", "DEEPSEEK_API_KEY")

    def set_ds_key(self, key: str) -> None:
        """存 DS key（兼容旧调用）。v0.4.15 走 set_llm_key("deepseek", key)。"""
        self.set_llm_key("deepseek", key)

    # ---- v0.4.15 多 provider key（按 provider 名存取 Keychain） ----
    def get_llm_key(self, provider: str, env_var: str) -> str | None:
        """LLM API key：平台密钥库优先（按 provider 名），env 兜底 → None。
        基类只 env 兜底（无密钥库）；mac/win 覆盖用 keyring。"""
        return os.environ.get(env_var) or None

    def set_llm_key(self, provider: str, key: str) -> None:
        """存 provider key 到平台密钥库。基类 no-op（无密钥库）。"""
        pass

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
            self._mouse_lock = None           # v0.7 MouseLockMac 单例（惰性）

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

        # ---- v0.8 窗口 Space 管理（全屏时聊天面板移桌面 Space）----
        def move_window_to_all_spaces(self, widget) -> bool:
            """NSWindow collectionBehavior = CanJoinAllSpaces|Stationary
            ——全屏 app 独占 Space 时聊天面板仍可见（像微信）。"""
            try:
                from ctypes import c_void_p

                from AppKit import (
                    NSWindowCollectionBehaviorCanJoinAllSpaces,
                    NSWindowCollectionBehaviorStationary,
                )
                from objc import objc_object

                wid = int(widget.winId())
                view = objc_object(c_void_p=wid)
                nswin = view.window() if view is not None else None
                if nswin is None:
                    return False
                nswin.setCollectionBehavior_(
                    NSWindowCollectionBehaviorCanJoinAllSpaces
                    | NSWindowCollectionBehaviorStationary
                )
                return True
            except Exception:
                return False

        # ---- v0.7 吃鼠标平台注入（补遗#7：经 adapter 注入，共享层不直
        # import mouse_lock_mac；CGEventTap/pyobjc 全封在 mouse_lock_mac） ----
        def get_mouse_lock(self):
            """惰性建 MouseLockMac 单例（ProactiveScheduler/EatMouseSession
            经此拿平台 mouse_lock）。"""
            if self._mouse_lock is None:
                from . import mouse_lock_mac

                self._mouse_lock = mouse_lock_mac.MouseLockMac()
            return self._mouse_lock

        def start_mouse_lock(self, duration_s: float) -> bool:
            return self.get_mouse_lock().start(duration_s)

        def stop_mouse_lock(self) -> None:
            self.get_mouse_lock().force_spit()

        def is_mouse_locked(self) -> bool:
            return self.get_mouse_lock().active

        def is_accessibility_trusted(self) -> bool:
            """Accessibility 是否授权（吃鼠标 + 热键前置检测；未开只提示不抑制）。"""
            from . import mouse_lock_mac

            return mouse_lock_mac.MouseLockMac.accessibility_trusted()

        def open_path(self, path: str) -> tuple:
            """``open`` 打开文件/文件夹（Finder 关联程序）。"""
            import subprocess as _sp

            try:
                _sp.run(["open", path], check=True, capture_output=True,
                        timeout=10)
                return (True, f"已打开 {os.path.basename(path)}")
            except Exception as e:
                return (False, f"打开失败: {e}")

        def is_active_content(self, video_apps) -> bool:
            """前台视频播放器白名单命中（T8 活跃内容检测）。"""
            from . import mouse_lock_mac

            return mouse_lock_mac.MouseLockMac.is_active_content(
                tuple(video_apps) if video_apps else None
            )

        def prompt_accessibility(self) -> None:
            """深链到系统设置「隐私与安全 → 辅助功能」（未授权时引导）。"""
            from . import mouse_lock_mac

            mouse_lock_mac.open_accessibility_settings()

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
            return self.get_llm_key("deepseek", "DEEPSEEK_API_KEY")

        def set_ds_key(self, key: str) -> None:
            self.set_llm_key("deepseek", key)

        # ---- v0.4.15 多 provider key（Keychain 按 provider 名存取） ----
        def get_llm_key(self, provider: str, env_var: str) -> str | None:
            """Keychain 优先（按 provider 名）→ env 兜底 → None。"""
            try:
                import keyring

                key = keyring.get_password("Desktop-Pet", f"{provider}_api_key")
                if key:
                    return key
            except Exception as exc:
                import logging

                logging.getLogger("pet").warning(
                    "Keychain 读取 %s key 失败，回退 env: %s", provider, exc
                )
            return os.environ.get(env_var) or None

        def set_llm_key(self, provider: str, key: str) -> None:
            """存 Keychain。失败不崩（调用方 get_llm_key 取 env 兜底）。"""
            try:
                import keyring

                keyring.set_password("Desktop-Pet", f"{provider}_api_key", key)
            except Exception as exc:
                import logging

                logging.getLogger("pet").warning(
                    "Keychain 存 %s key 失败: %s", provider, exc
                )

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

        # ---- v0.7：吃鼠标（mouse_lock_win，W2 Spike 落地） ----
        def get_mouse_lock(self):
            from . import mouse_lock_win

            return mouse_lock_win.get_mouse_lock_win()

        def start_mouse_lock(self, duration_s: float) -> bool:
            return self.get_mouse_lock().start(duration_s)

        def stop_mouse_lock(self) -> None:
            self.get_mouse_lock().force_spit()

        def is_mouse_locked(self) -> bool:
            return self.get_mouse_lock().active

        def is_accessibility_trusted(self) -> bool:
            # win 无 Accessibility 概念：钩子/热键/剪贴板均无需特权 → 恒真
            return True

        def prompt_accessibility(self) -> None:
            # no-op（win 无辅助功能授权流程；UIPI 提升窗口降级已在
            # MouseLockWin.start 内处理为返回 False 走气泡路径）
            pass

        def start_hotkeys(self, cfg: dict, on_chat, on_spit,
                          on_conflict=None) -> bool:
            """v0.11：HotkeyManager 注册 Ctrl+Alt+P/T + 冲突检测。"""
            from . import hotkey_win

            self._hotkey_mgr = hotkey_win.HotkeyManager()
            hk_cfg = cfg.get("hotkeys", {})
            return self._hotkey_mgr.start(
                hk_cfg.get("chat", "ctrl+alt+p"),
                hk_cfg.get("spit", "ctrl+alt+t"),
                on_chat, on_spit, on_conflict,
            )

        def stop_hotkeys(self) -> None:
            mgr = getattr(self, "_hotkey_mgr", None)
            if mgr:
                mgr.stop()

        def set_autostart(self, enabled: bool) -> bool:
            """v0.11：注册表 HKCU Run。"""
            from . import hotkey_win

            return hotkey_win.set_autostart(enabled)

        def is_autostart_enabled(self) -> bool:
            from . import hotkey_win

            return hotkey_win.is_autostart_enabled()

        def open_path(self, path: str) -> tuple:
            """os.startfile 打开文件/文件夹/文档（关联程序）。"""
            try:
                os.startfile(path)
                return (True, f"已打开 {os.path.basename(path)}")
            except OSError as e:
                return (False, f"打开失败: {e}")

        def is_active_content(self, video_apps) -> bool:
            # 活跃内容检测：前台进程名 ∈ 视频白名单（复用 v0.3 全屏检测的
            # 进程名管道；None apps → False）
            if not video_apps:
                return False
            fs, name = sensor_win.foreground_fullscreen()
            if not name:
                return False
            return name.upper() in {
                str(a).upper() for a in video_apps
            }

        # ---- v0.4：DS key 存 Windows 凭据管理器 / 危险确认 Qt 对话框 ----
        def get_ds_key(self) -> str | None:
            return self.get_llm_key("deepseek", "DEEPSEEK_API_KEY")

        def set_ds_key(self, key: str) -> None:
            self.set_llm_key("deepseek", key)

        # ---- v0.4.15 多 provider key（凭据管理器按 provider 名存取） ----
        def get_llm_key(self, provider: str, env_var: str) -> str | None:
            """凭据管理器优先（按 provider 名）→ env 兜底 → None。"""
            try:
                import keyring

                key = keyring.get_password("Desktop-Pet", f"{provider}_api_key")
                if key:
                    return key
            except Exception as exc:
                import logging

                logging.getLogger("pet").warning(
                    "凭据管理器读取 %s key 失败，回退 env: %s", provider, exc
                )
            return os.environ.get(env_var) or None

        def set_llm_key(self, provider: str, key: str) -> None:
            """存凭据管理器。失败不崩。"""
            try:
                import keyring

                keyring.set_password("Desktop-Pet", f"{provider}_api_key", key)
            except Exception as exc:
                import logging

                logging.getLogger("pet").warning(
                    "凭据管理器存 %s key 失败: %s", provider, exc
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
