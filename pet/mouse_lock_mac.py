"""mac 鼠标抑制（吃鼠标平台层）—— 设计思路.md §七 / 平台适配与分工 §五。

平台 API 全部封装于此（CGEventTap / AXIsProcessTrusted / 前台 app 查询 /
强制吐出热键）；共享 ``EatMouseSession`` / ``ProactiveScheduler`` 经
``platform.MacPlatformAdapter`` 注入本模块，**不直 import**（平台隔离，
设计思路.md §2.1 注入点例外）。

安全设计（铁律不可妥协，设计思路.md §七）：

1. **只锁鼠标不碰键盘**——鼠标 tap 用 ``kCGEventTapOptionDefault`` 回调
   ``return None`` 抑制；键盘 tap 用 ``kCGEventTapOptionListenOnly``
   **物理上无法抑制**（listen-only 回调返回值被系统忽略），只用来监听
   强制吐出热键 ``Cmd+Option+T``。两 tap 分离 = 即便回调有 bug 也不会
   吞键盘，defense-in-depth。

2. **单次锁定 ≤15s**——``start`` 时 duration 钳制 ``[0.3, 15.0]``。

3. **看门狗先于 tap 启动**——``start()`` 先起 daemon 看门狗线程并记
   deadline，**再**创建/启用 tap。看门狗独立 daemon 线程，主逻辑崩溃 /
   异常也按 deadline 强制 ``_release()``。看门狗 = 自动超时释放(铁律2)
   与崩溃兜底(铁律6)的同一机制：deadline 到 → ``_release``。

4. **``_release()`` 幂等**——看门狗 / ``force_spit`` / 热键 / shutdown
   全走 ``_release``，多次调用安全（``_active`` 标志 + 锁串行化）。

5. **Accessibility 未开 → 不抑制**——``start`` 先 ``AXIsProcessTrusted``
   检测，未授权返 ``False``（上层只气泡提示 + 深链，不锁死用户）。

6. **tap 创建失败（权限被拒 / 系统异常）→ 返 ``False``，不锁死**。

CGEventTap 回调只在 run loop 服务其 source 时触发。本模块把两个 source
加到 ``CFRunLoopGetMain()``（``kCFRunLoopCommonModes``）——Qt 的 mac 事件
循环即主 CFRunLoop，common-modes source 在默认/模态运行期都会被服务。
``release`` 关键动作是 ``CGEventTapEnable(tap, False)``（mach-port 级标志，
跨线程安全，立即放行事件）；source 移除做 hygiene。

run loop source 经 ``CFMachPortCreateRunLoopSource(None, tap, None)`` 包
（本 pyobjc-framework-Quartz 构建未导出 ``CGEventTapGetRunLoopSource``
符号——ctypes 在 ApplicationServices/CoreGraphics/HIServices 均查无）。
此包法是 pynput 等成熟库对 CGEventTap 的既证写法：tap 端口收到事件时
经此 source 在 run loop 上派发到 CGEventTap 回调（非裸 CFMachPort 回调）。
"""

from __future__ import annotations

import ctypes
import logging
import threading
import time

log = logging.getLogger("pet")

# ---- 铁律常量 ----
_MAX_DURATION = 15.0      # 铁律2：单次锁定 ≤15s
_MIN_DURATION = 0.3       # 下限：≥ 看门狗一轮轮询(0.25s)，防极短 duration
                          # 在 tap 创建前看门狗先释放的竞态
_WATCHDOG_POLL = 0.25     # 看门狗轮询间隔（每 0.25s 检查 deadline）

# ---- 平台符号探测（pyobjc-framework-Cocoa 子集未含 HIServices/
# ApplicationServices 的 AXIsProcessTrusted，经 ctypes 从框架直取；Quartz 供
# CGEventTap；CoreFoundation 供 run loop source）----
_HAS_QUARTZ = False
try:
    from Quartz import (
        CGEventTapCreate,
        CGEventTapEnable,
        CGEventTapIsEnabled,
        CGEventMaskBit,
        CGEventGetFlags,
        CGEventGetIntegerValueField,
        kCGSessionEventTap,
        kCGHeadInsertEventTap,
        kCGEventTapOptionDefault,
        kCGEventTapOptionListenOnly,
        kCGEventMouseMoved,
        kCGEventLeftMouseDown,
        kCGEventLeftMouseUp,
        kCGEventRightMouseDown,
        kCGEventRightMouseUp,
        kCGEventLeftMouseDragged,
        kCGEventRightMouseDragged,
        kCGEventKeyDown,
        kCGKeyboardEventKeycode,
        kCGEventFlagMaskCommand,
        kCGEventFlagMaskAlternate,
    )

    _HAS_QUARTZ = True
except Exception:
    log.warning("Quartz 不可用，鼠标抑制禁用", exc_info=True)

_HAS_CF = False
try:
    from CoreFoundation import (
        CFRunLoopGetMain,
        CFRunLoopAddSource,
        CFRunLoopRemoveSource,
        CFMachPortCreateRunLoopSource,
        kCFRunLoopCommonModes,
    )

    _HAS_CF = True
except Exception:
    log.warning("CoreFoundation run loop 符号不可用", exc_info=True)


# AXIsProcessTrusted 经 ctypes（无 pyobjc HIServices 包）。
# 框架路径固定，符号 ABI 稳定；返回 c_bool。
_AX_LIB: ctypes.CDLL | None = None


def _ax_trusted() -> bool:
    """Accessibility 是否授权（``AXIsProcessTrusted``）。查询失败返 False
    （fail-closed：未确认授权就不抑制）。"""
    global _AX_LIB
    try:
        if _AX_LIB is None:
            _AX_LIB = ctypes.CDLL(
                "/System/Library/Frameworks/ApplicationServices.framework/"
                "ApplicationServices"
            )
            _AX_LIB.AXIsProcessTrusted.restype = ctypes.c_bool
            _AX_LIB.AXIsProcessTrusted.argtypes = []
        return bool(_AX_LIB.AXIsProcessTrusted())
    except Exception:
        log.warning("AXIsProcessTrusted 查询失败", exc_info=True)
        return False


# 强制吐出热键 Cmd+Option+T（T=吐；决策点 v0.0.12：原 Cmd+Option+Shift+P
# 4 键难按 → 3 键；与 win Ctrl+Alt+T 对应；v0.11 统一热键管理时整合）
_SPIT_KEYCODE = 17        # kVK_ANSI_T
_SPIT_FLAGS = (          # 必须同时按下的修饰键
    kCGEventFlagMaskCommand | kCGEventFlagMaskAlternate
) if _HAS_QUARTZ else 0

# 鼠标 mask（**不含任何键盘 mask**——铁律1：键盘不进鼠标抑制回调）
_MOUSE_MASK = (
    CGEventMaskBit(kCGEventMouseMoved)
    | CGEventMaskBit(kCGEventLeftMouseDown)
    | CGEventMaskBit(kCGEventLeftMouseUp)
    | CGEventMaskBit(kCGEventRightMouseDown)
    | CGEventMaskBit(kCGEventRightMouseUp)
    | CGEventMaskBit(kCGEventLeftMouseDragged)
    | CGEventMaskBit(kCGEventRightMouseDragged)
) if _HAS_QUARTZ else 0

# 键盘 mask（仅 KeyDown；listen-only tap，永不抑制）
_KEY_MASK = CGEventMaskBit(kCGEventKeyDown) if _HAS_QUARTZ else 0


def open_accessibility_settings() -> None:
    """深链到系统设置「隐私与安全 → 辅助功能」（Accessibility 未开时引导）。

    命令行 deep-link 稳定可跨版本；打开后用户手动 toggle 本进程开关。
    """
    import subprocess

    try:
        subprocess.Popen(
            ["open", "x-apple.systempreferences:com.apple.preference."
             "security?Privacy_Accessibility"]
        )
    except Exception:
        log.warning("打开辅助功能设置失败", exc_info=True)


def frontmost_app_name() -> str:
    """前台 app 名（活跃内容检测用，T8）。CGWindowList 取最顶层实体窗 owner
    ——免 Accessibility（CGWindowList 不需权限），比 AppleScript 'frontmost'
    少一道 Automation 权限。无 Quartz / 无窗口 → ``""``。"""
    if not _HAS_QUARTZ:
        return ""
    try:
        from Quartz import (
            CGWindowListCopyWindowInfo,
            kCGNullWindowID,
            kCGWindowListOptionOnScreenOnly,
        )

        wins = CGWindowListCopyWindowInfo(
            kCGWindowListOptionOnScreenOnly, kCGNullWindowID
        )
    except Exception:
        log.warning("frontmost 查询失败", exc_info=True)
        return ""
    for w in wins or []:
        try:
            if int(w.get("kCGWindowLayer", 0)) != 0:
                continue
        except (TypeError, ValueError):
            continue
        owner = w.get("kCGWindowOwnerName", "")
        if owner:
            return str(owner)
    return ""


class MouseLockMac:
    """mac 鼠标抑制 + 强制吐出热键 + 看门狗。

    生命周期：
    - ``start(duration)`` → 看门狗先起 → 创建鼠标/键盘两 tap（加主 run loop）
      → 抑制开始。返 True=已抑制。
    - ``force_spit()`` → ``_release()``（热键/托盘/shutdown 调，幂等）。
    - 看门狗 → deadline 到自动 ``_release()``（主逻辑崩溃也释放）。
    """

    # 前台视频白名单（活跃内容检测，T8）；进 config.proactive.video_apps 覆盖
    VIDEO_APPS = (
        "IINA", "QuickTime Player", "VLC", "Safari", "Google Chrome",
        "Firefox", "Microsoft Edge", "Brave Browser", "Arc",
    )

    def __init__(self) -> None:
        self._tap = None            # 鼠标 CFMachPort（default 模式，抑制）
        self._src = None            # 鼠标 run loop source
        self._ktap = None           # 键盘 CFMachPort（listen-only，热键）
        self._ksrc = None           # 键盘 run loop source
        self._active = False
        self._lock = threading.Lock()
        self._watchdog: threading.Thread | None = None
        self._deadline = 0.0
        # 保留回调引用防 GC（CGEventTap 不全权持有 Python 回调）
        self._mouse_cb = self._on_mouse
        self._key_cb = self._on_key

    # ---- 公开查询 ----

    @property
    def active(self) -> bool:
        return self._active

    @staticmethod
    def accessibility_trusted() -> bool:
        return _ax_trusted()

    @staticmethod
    def is_active_content(video_apps: tuple[str, ...] | None = None) -> bool:
        """前台是否视频播放器（T8 活跃内容检测）。白名单命中 → 只气泡不吃。"""
        name = frontmost_app_name()
        if not name:
            return False
        apps = tuple(video_apps) if video_apps else MouseLockMac.VIDEO_APPS
        return any(a.lower() == name.lower() for a in apps)

    # ---- CGEventTap 回调 ----
    # 签名 (proxy, event_type, event, refcon) -> CGEventRef|None

    def _on_mouse(self, proxy, event_type, event, refcon):
        # 鼠标 tap（default 模式）：return None 抑制事件（不传下游）。
        # 键盘事件永不进此回调（mask 只含鼠标）——铁律1。
        return None

    def _on_key(self, proxy, event_type, event, refcon):
        # 键盘 tap（listen-only）：return 值被系统忽略，事件照传下游——
        # 物理上无法吞键盘（铁律1 defense-in-depth）。只检测热键。
        try:
            if event is not None:
                flags = CGEventGetFlags(event)
                if (flags & _SPIT_FLAGS) == _SPIT_FLAGS:
                    code = CGEventGetIntegerValueField(
                        event, kCGKeyboardEventKeycode
                    )
                    if code == _SPIT_KEYCODE:
                        log.info("强制吐出热键 Cmd+Option+T 触发")
                        self.force_spit()
        except Exception:
            log.warning("热键回调异常", exc_info=True)
        return event

    # ---- 启动 / 释放 ----

    def start(self, duration_s: float) -> bool:
        """抑制鼠标，duration 钳制 [0.3, 15.0]s。返 True=已抑制；False=
        权限不足/符号缺失/tap 创建失败（上层只气泡，不锁死）。

        看门狗先于 tap 启动：先起 daemon 线程记 deadline，再创建 tap，
        任何路径下看门狗按 deadline 强制释放。
        """
        if not _HAS_QUARTZ or not _HAS_CF:
            log.warning("鼠标抑制跳过：Quartz/CoreFoundation 不可用")
            return False
        dur = max(_MIN_DURATION, min(_MAX_DURATION, float(duration_s)))

        # ① 看门狗先起：标记 active + 记 deadline + 建线程对象
        with self._lock:
            if self._active:
                # 已激活：重置 deadline，不重建 tap（T16 不叠加）
                self._deadline = time.monotonic() + dur
                log.info("鼠标抑制续期 %.1fs（不叠加）", dur)
                return True
            if not _ax_trusted():
                log.info("鼠标抑制跳过：Accessibility 未授权（只气泡提示）")
                return False
            self._active = True
            self._deadline = time.monotonic() + dur
            wd = threading.Thread(
                target=self._watchdog_loop,
                name="mouse-lock-watchdog",
                daemon=True,
            )
            self._watchdog = wd

        # ② 启动看门狗（锁外启动，避免线程持锁等 join 死锁）
        wd.start()

        # ③ 创建 tap（锁内）；force_spit 若在此前竞速释放，_active 已 False
        #   → 此处不创建，避免孤儿 tap
        ok = self._create_taps()
        if not ok:
            with self._lock:
                self._active = False
            return False
        log.info("鼠标抑制开始，%.1fs 后看门狗自动释放（吐出 ⌘⌥T）", dur)
        return True

    def _create_taps(self) -> bool:
        """创建鼠标 + 键盘两 tap 并加主 run loop。返 False=创建失败。"""
        with self._lock:
            if not self._active:
                return False  # force_spit 已抢先释放
            try:
                mtap = CGEventTapCreate(
                    kCGSessionEventTap,
                    kCGHeadInsertEventTap,
                    kCGEventTapOptionDefault,
                    _MOUSE_MASK,
                    self._mouse_cb,
                    None,
                )
            except Exception:
                log.warning("CGEventTapCreate(鼠标) 异常", exc_info=True)
                mtap = None
            if mtap is None:
                log.warning("CGEventTapCreate(鼠标) 返回 NULL（权限被拒/系统拒绝）")
                return False
            try:
                ktap = CGEventTapCreate(
                    kCGSessionEventTap,
                    kCGHeadInsertEventTap,
                    kCGEventTapOptionListenOnly,
                    _KEY_MASK,
                    self._key_cb,
                    None,
                )
            except Exception:
                log.warning("CGEventTapCreate(键盘) 异常", exc_info=True)
                ktap = None
            if ktap is None:
                # 键盘 tap 失败不致命：鼠标抑制仍生效，只是热键退化为
                # 托盘+看门狗（托盘在非锁定时可点；看门狗兜底）。记日志。
                log.warning("键盘 listen tap 创建失败，热键退托盘+看门狗")

            try:
                msrc = CFMachPortCreateRunLoopSource(None, mtap, None)
                CFRunLoopAddSource(
                    CFRunLoopGetMain(), msrc, kCFRunLoopCommonModes
                )
            except Exception:
                log.warning("鼠标 tap 接 run loop 失败", exc_info=True)
                try:
                    CGEventTapEnable(mtap, False)
                except Exception:
                    pass
                return False
            ksrc = None
            if ktap is not None:
                try:
                    ksrc = CFMachPortCreateRunLoopSource(None, ktap, None)
                    CFRunLoopAddSource(
                        CFRunLoopGetMain(), ksrc, kCFRunLoopCommonModes
                    )
                except Exception:
                    log.warning("键盘 tap 接 run loop 失败（热键退化）",
                                exc_info=True)
                    ksrc = None
                    ktap = None
            self._tap = mtap
            self._src = msrc
            self._ktap = ktap
            self._ksrc = ksrc
            return True

    def _watchdog_loop(self) -> None:
        """独立 daemon 线程：到 deadline 强制 ``_release``。

        主逻辑崩溃（Python 异常被 Qt 吞掉后 run loop 照跑，但即使主线程
        整个卡死，本线程照常轮询 deadline 并释放）。每 0.25s 查一次。
        """
        while True:
            time.sleep(_WATCHDOG_POLL)
            with self._lock:
                if not self._active:
                    return
                if time.monotonic() >= self._deadline:
                    break
        self._release(reason="watchdog/deadline")

    def force_spit(self) -> None:
        """强制吐出（热键 / 托盘 / shutdown 调）——立即释放，幂等。"""
        self._release(reason="force_spit")

    def _release(self, reason: str) -> None:
        """幂等释放：CGEventTapEnable(False) 立即放行事件（跨线程安全），
        source 移除做 hygiene。多次调用安全。"""
        with self._lock:
            if not self._active:
                return
            self._active = False
            mtap, msrc = self._tap, self._src
            ktap, ksrc = self._ktap, self._ksrc
            self._tap = self._src = self._ktap = self._ksrc = None

        # 关键释放：CGEventTapEnable(False) 是 mach-port 级使能标志，跨线程
        # 调用安全，立即让事件放行（鼠标恢复）。看门狗线程与主线程都调它。
        for tap in (mtap, ktap):
            if tap is not None:
                try:
                    CGEventTapEnable(tap, False)
                except Exception:
                    log.warning("CGEventTapEnable(False) 异常", exc_info=True)
        # source 移除（hygiene；跨线程调用，best-effort）
        rl = CFRunLoopGetMain()
        for src in (msrc, ksrc):
            if src is not None:
                try:
                    CFRunLoopRemoveSource(rl, src, kCFRunLoopCommonModes)
                except Exception:
                    pass
        log.info("鼠标释放(%s)", reason)
