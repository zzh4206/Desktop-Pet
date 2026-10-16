"""mac 平台语音实现（v0.26.1）——共享核心 ``voice.py`` 的平台能力注入点。

本文件**仅由 ``platform.py`` 的 mac 分支 import**（平台库隔离点）：
AppKit/NSSound 与 ``say`` 子进程只出现在这里（+ platform.py 注入点），共享
层 ``voice.py`` 负责门禁/队列/路由，不 import AppKit/subprocess。

v0.26 分线：「公用核心 + mac 实装 + win 留坑」。

- **罐头音包**（asset player）：只吃 mac 系统原生支持的格式
  （``.wav/.aiff/.m4a/.mp3/.caf``），走 ``NSSound`` 原生播放——按用户拍板
  **不引入 QtMultimedia、不做转码**。NSSound 是**异步**播放（靠 run loop
  驱动，Qt 的 mac 事件循环即主 CFRunLoop），故必须**持强引用**，否则对象
  被 GC 回收即静音。
- **TTS**（tts engine）：``say`` 子进程异步朗读，``-v`` 音色 / ``-r`` 语速
  按需拼接，文本一律走 argv 列表（不拼 shell 字符串，避免注入/引号问题）。
- **注入协议**（duck typing，项目经理冻结，不定义抽象基类、不 import
  voice.py）：player ``play/stop/is_playing``；tts ``speak/stop/is_playing``；
  两者另有 ``available`` 属性供共享层门禁探测。

失败哲学与 ``sound.py`` / ``render3d`` 一致：任何平台调用失败一律**静默降级
+ warning 日志**，永不外抛——平台不可用时各方法安全返 False/None。
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess

log = logging.getLogger("pet")


def _load_nssound():
    """惰性取 ``AppKit.NSSound`` 类；不可用返 None（不抛）。

    导入推迟到调用期（非 import 期）：mac 之外/未装 PyObjC 时本模块仍可
    import，且测试可经 ``sys.modules`` 注入 fake AppKit。
    """
    try:
        from AppKit import NSSound

        return NSSound
    except Exception:
        log.warning("[语音] AppKit.NSSound 不可用，罐头音包播放禁用",
                    exc_info=True)
        return None


class MacAssetPlayer:
    """mac 罐头音包播放器（NSSound）—— voice.py asset player 注入实现。

    - ``play(path) -> bool``：装载并**异步**播放，返 True=已启动；
    - ``stop() -> None``：停播并释放引用（幂等）；
    - ``is_playing() -> bool``：当前是否有声音在播；
    - ``available``：AppKit/NSSound 是否可用（False 时各方法安全降级）。

    强引用：``play`` 成功后把 NSSound 对象存 ``self._sound`` 持住——pyobjc
    对象被 GC 回收会立刻静音（异步播放期间必须保活）；再次 ``play`` 前先
    ``stop`` 上一段（不叠音）。
    """

    def __init__(self) -> None:
        self._nssound_cls = _load_nssound()
        self._ok = self._nssound_cls is not None
        # 强引用位：防 NSSound 被 GC 回收（异步播放期间丢了引用即静音）
        self._sound = None

    @property
    def available(self) -> bool:
        """AppKit/NSSound 是否可用。"""
        return self._ok

    def play(self, path: str) -> bool:
        """播放 ``path``（``.wav/.aiff/.m4a/.mp3/.caf`` 等 mac 原生格式）。

        返 True=已启动异步播放；文件不存在/平台不可用/异常 → 返 False
        （静默降级 + warning，永不外抛）。再次 play 前先停上一段。
        """
        if not self._ok:
            log.warning("[语音] NSSound 不可用，跳过罐头音包播放: %s", path)
            return False
        try:
            if not path or not os.path.isfile(path):
                log.warning("[语音] 罐头音频文件不存在，跳过播放: %s", path)
                return False
            self.stop()   # 先停上一段（不叠音）
            snd = self._nssound_cls.alloc(
            ).initWithContentsOfFile_byReference_(path, True)
            if snd is None:
                log.warning("[语音] NSSound 装载失败: %s", path)
                return False
            self._sound = snd   # 持强引用（异步播放期间必须保活）
            if not snd.play():
                log.warning("[语音] NSSound play() 返回 False: %s", path)
                self._sound = None   # 启动失败即释放引用
                return False
            return True
        except Exception:
            log.warning("[语音] NSSound 播放异常: %s", path, exc_info=True)
            self.stop()   # 异常路径也清干净（stop 自身不抛）
            return False

    def stop(self) -> None:
        """停止当前播放并释放强引用（幂等；异常吞掉仅记 warning）。"""
        snd = self._sound
        self._sound = None
        if snd is None:
            return
        try:
            snd.stop()
        except Exception:
            log.warning("[语音] NSSound stop 异常", exc_info=True)

    def is_playing(self) -> bool:
        """当前是否有声音在播；无对象 → False。"""
        snd = self._sound
        if snd is None:
            return False
        try:
            return bool(snd.isPlaying())
        except Exception:
            log.warning("[语音] NSSound isPlaying 异常", exc_info=True)
            return False


class MacTtsEngine:
    """mac TTS 引擎（``say`` 子进程异步朗读）—— voice.py tts 注入实现。

    - ``speak(text) -> bool``：异步朗读，返 True=已启动 say 进程；
    - ``stop() -> None``：终止当前 say 进程（``terminate``，必要时 ``kill``
      兜底）并清引用（幂等）；
    - ``is_playing() -> bool``：``Popen.poll() is None`` 即在播；
    - ``available``：``say`` 是否可用（``shutil.which`` 惰性判定，**不在
      import 期执行**；测试可 mock）。

    参数拼接（一律 list 传参，**不拼 shell 字符串**）::

        ["say"] (+ ["-v", voice] 仅当 voice 非空)
                (+ ["-r", str(rate)] 仅当 rate > 0) + [text]

    说话前先 ``stop`` 上一句（避免叠音）。
    """

    def __init__(self, voice: str = "", rate: int = 0) -> None:
        self._voice = str(voice or "").strip()   # 空 voice = 系统默认
        try:
            rate = int(rate)
        except (TypeError, ValueError):
            rate = 0
        self._rate = rate if rate > 0 else 0     # rate<=0 = 系统默认
        self._proc = None                        # subprocess.Popen | None

    @property
    def available(self) -> bool:
        """``say`` 是否可用（``shutil.which`` 惰性判定，可 mock）。"""
        try:
            return shutil.which("say") is not None
        except Exception:
            log.warning("[语音] 检测 say 失败", exc_info=True)
            return False

    def _build_cmd(self, text: str) -> list:
        """拼 ``say`` argv（list 形式；文本作最后一个 argv，不拼 shell）。"""
        cmd = ["say"]
        if self._voice:
            cmd += ["-v", self._voice]
        if self._rate > 0:
            cmd += ["-r", str(self._rate)]
        cmd.append(text)
        return cmd

    def speak(self, text: str) -> bool:
        """异步朗读 ``text``（先停上一句，防叠音）。

        返 True=已启动 say 进程；空文本（含全空白）/``say`` 缺失/启动异常 →
        返 False（静默降级 + warning，永不外抛）。文本不落日志（隐私）。
        """
        if text is None or not str(text).strip():
            log.warning("[语音] 空文本，跳过朗读")
            return False
        text = str(text)
        if not self.available:
            log.warning("[语音] say 不可用，跳过朗读")
            return False
        self.stop()   # 先停上一句（不叠音）
        try:
            self._proc = subprocess.Popen(
                self._build_cmd(text),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except Exception:
            log.warning("[语音] say 启动失败", exc_info=True)
            self._proc = None
            return False
        return True

    def stop(self) -> None:
        """终止当前 say 进程并清引用（幂等；terminate，必要时 kill 兜底）。"""
        proc = self._proc
        self._proc = None
        if proc is None:
            return
        try:
            proc.terminate()
        except Exception:
            log.warning("[语音] say terminate 异常，改 kill 兜底",
                        exc_info=True)
            self._kill(proc)
            return
        try:
            # say 收 SIGTERM 即退；短暂 wait 兼回收子进程（防僵尸）。
            proc.wait(timeout=0.5)
        except Exception:
            self._kill(proc)   # 未被终止掉 → kill 兜底

    @staticmethod
    def _kill(proc) -> None:
        """kill 兜底 + 回收（失败仅记 warning，永不外抛）。"""
        try:
            proc.kill()
        except Exception:
            log.warning("[语音] say kill 异常", exc_info=True)
            return
        try:
            proc.wait(timeout=0.5)
        except Exception:
            pass

    def is_playing(self) -> bool:
        """``Popen.poll() is None`` 即在播；无进程 → False。"""
        proc = self._proc
        if proc is None:
            return False
        try:
            return proc.poll() is None
        except Exception:
            log.warning("[语音] say poll 异常", exc_info=True)
            return False


def make_asset_player() -> MacAssetPlayer | None:
    """工厂：建 :class:`MacAssetPlayer`；任何异常返 None（platform.py 留坑）。

    注：``MacAssetPlayer.__init__`` 自身已不抛（不可用只标 ``available``
    False）；本工厂再加一层守卫，保证 platform.py 的 mac 分支接线不会因
    语音初始化把 app 拖崩。
    """
    try:
        return MacAssetPlayer()
    except Exception:
        log.warning("[语音] 建 MacAssetPlayer 失败，语音降级禁用", exc_info=True)
        return None


def make_tts_engine(voice: str = "", rate: int = 0) -> MacTtsEngine | None:
    """工厂：建 :class:`MacTtsEngine`；任何异常返 None（platform.py 留坑）。"""
    try:
        return MacTtsEngine(voice, rate)
    except Exception:
        log.warning("[语音] 建 MacTtsEngine 失败，语音降级禁用", exc_info=True)
        return None
