"""语音共享编排层（v0.26.0 语音线）—— wiki 设计-语音模块技术方案.md §1/§2。

**平台库-free 承诺**：本模块只 import 标准库（os/wave/math/struct/…），
绝不 import 任何 GUI 框架、Cocoa 桥或 Windows 原生 API。平台差异全部由
**注入对象**（duck typing）承担，本项目其它模块不得在此处反向依赖。
承诺的直接收益：win 端机器与 CI 无需任何平台依赖即可跑全量单测，对齐
pet/weather.py 的平台-free 先例。

注入协议（项目经理在 pet/platform.py 提供实装，本模块只按 duck typing 调用）：

* asset player（罐头音包播放器）：``play(path) -> bool`` / ``stop() -> None``
  / ``is_playing() -> bool``
* tts engine（动态合成）：``speak(text) -> bool`` / ``stop() -> None``
  / ``is_playing() -> bool``

分层铁律（对齐 0.23 天气线「薄共享编排层 + 厚平台注入」）：

* **罐头语音优先**：``voice.tts.provider == "asset"`` 且显式 ``key`` 在
  ``assets/voice/`` 命中文件 → 直放预录音；否则回落动态 TTS。
* **缺件静默降级**：注入位为 None（win 留坑）、资产缺后缀、播放器返 False、
  注入对象抛异常——一律记日志返 False，永不外抛、永不阻断气泡。
* **占位音频绝不含他人音色**：占位期用 ``ensure_placeholders`` 程序生成短
  提示音（正弦 + 淡入淡出），用户后续自行录制同名文件即生效，代码零改动。
* **门禁先于播报**：启用开关 → 来源开关 → DND → 全屏 → 深夜静默 → 去重，
  全部在 ``SpeakGate`` 内一次性判定，便于单测锁定顺序。
"""

from __future__ import annotations

import datetime
import logging
import math
import os
import struct
import tempfile
import time
import wave
from dataclasses import dataclass

log = logging.getLogger("pet")

# 探测优先级即顺序：全部为 mac 系统原生 codec（NSSound 路径）。
# 用户拍板（2026-10-08）：不引入 QtMultimedia 后端/转码依赖，故 ogg/flac
# 等非系统 codec 格式不入白名单（如需再议为增量）。
VOICE_ASSET_SUFFIXES: tuple[str, ...] = (
    ".wav", ".aiff", ".m4a", ".mp3", ".caf",
)

# 占位音文件名标记（v0.26.5）：程序生成的占位音统一叫
# ``<key>.placeholder.<ext>``，正式录音仍是 ``<key>.<ext>``。
# 为什么加标记：占位音原先与正式素材同名（都是 ``<key>.wav``），而 ``.wav``
# 又是探测优先级最高的后缀——用户录了 ``.m4a`` 正式音包后仍会听到占位提示音
# （用户 2026-10-09 拍板修正）。加标记后**正式素材永远优先**，且占位音在
# Finder 里一眼可辨、可随时删，不必再"记得先删占位文件"。
VOICE_PLACEHOLDER_MARK = ".placeholder"

# 罐头语音（固定台词）规范 key —— 逐条对应代码里真实存在的固定气泡/提醒点，
# 不臆造。元组可加项（新增台词只需追加 key + 补 README 表 + 放同名文件）。
VOICE_KEYS: tuple[str, ...] = (
    # pet/proactive.py:829 早安窗口（08–11）每天一次
    "greet_morning",
    # pet/proactive.py:835 晚安窗口（21–23）每天一次
    "greet_night",
    # pet/proactive.py:846 生日当天一次
    "greet_birthday",
    # pet/proactive.py:863 节日当天一次（日历源回落内置表）
    "greet_festival",
    # pet/proactive.py:47-48 _SEDENTARY_TOPICS，:803 发射（久坐轮换话题）
    "reminder_sit",
    # pet/proactive.py:542 吃鼠标（强制休息）生效提示
    "eat_mouse",
    # app.py:1140 切换当前模型成功
    "switch_model",
    # app.py:1974 进化跨阶段（含离线补阶）
    "evolve",
    # app.py:2000 重置年龄/重新出生
    "reset_age",
    # app.py:472 启动致意「我醒啦～」
    "pet_wake",
    # app.py:851 / app.py:1785 未配置 API key（聊天不可用）
    "llm_no_key",
)

# provider 合法值；后续扩 cloud/edge 时在此加项即被 from_config 接受
_VOICE_PROVIDERS: tuple[str, ...] = ("asset", "system")

# 去重表上限：长跑进程不能因文本无限增长（超限时按窗口修剪一次）
_DEDUP_TABLE_MAX = 256


# ---- 纯函数：深夜静默判定 ----


def _parse_quiet(raw) -> tuple[int, int] | None:
    """校验 quiet_hours 形态 ``[23, 8]``；非法/缺项 → None（不猜）。"""
    try:
        if not isinstance(raw, (list, tuple)) or len(raw) != 2:
            return None
        vals = tuple(int(h) for h in raw)
    except (TypeError, ValueError):
        return None
    # 语义与 pet/proactive.py 的构造校验一致：小时闭区间 0..23；
    # start == end 视为"从不静默"（与 _quiet_now 的 start <= h < end 同口径）
    if not all(0 <= h <= 23 for h in vals):
        return None
    return vals  # type: ignore[return-value]


def _pick_quiet_hours(cfg: dict) -> tuple[int, int] | None:
    """从 config 取 quiet_hours，容忍整段 config / voice 段 / tts 段三种传入。

    为什么要容忍：config 冻结布局把静默时段放在 ``voice.tts.quiet_hours``，
    但 ``VoiceSettings`` 把它拍平成一个字段；本项目多处（含未来 proactive
    迁移）会拿不同深度的 dict 调本函数，按优先级取**第一个存在**的候选，
    存在但非法即判"未配置"（不像"缺项"那样继续往下找），避免非法值被
    更深层的候选意外救活。
    """
    if not isinstance(cfg, dict):
        return None
    candidates = []
    voice_sec = cfg.get("voice")
    if isinstance(voice_sec, dict):
        tts_sec = voice_sec.get("tts")
        if isinstance(tts_sec, dict):
            candidates.append(tts_sec.get("quiet_hours"))
        candidates.append(voice_sec.get("quiet_hours"))
    tts_flat = cfg.get("tts")
    if isinstance(tts_flat, dict):
        candidates.append(tts_flat.get("quiet_hours"))
    candidates.append(cfg.get("quiet_hours"))
    for raw in candidates:
        if raw is None:
            continue
        return _parse_quiet(raw)
    return None


def quiet_hours_active(cfg: dict, now: float) -> bool:
    """深夜静默判定纯函数（v0.26.0）——未配置/非法 → False。

    本地时间小时落在静默窗口内即返 True；``start > end`` 视为**跨午夜**
    （如 23→08：``h >= 23 or h < 8``），``start <= end`` 为同日窗口
    （``start <= h < end``，右开）。语义对齐 pet/proactive.py 的
    ``_quiet_now``——**复制逻辑而非 import**，避免共享层反向依赖主动模块。

    入参 ``now`` 由调用方注入（便于单测锁定边界时刻，不在内部取时）。
    """
    hours = _pick_quiet_hours(cfg)
    if hours is None:
        return False
    try:
        h = datetime.datetime.fromtimestamp(now).hour
    except (OverflowError, OSError, ValueError):
        # 极端时间戳（越界/NaN 派生）不当作深夜，避免误静默
        return False
    start, end = hours
    if start > end:
        return h >= start or h < end
    return start <= h < end


# ---- 罐头音包目录探测 ----


def _asset_candidates(key: str) -> list[str]:
    """候选文件名列表：**正式素材全部先于占位音**（v0.26.5）。

    先按 ``VOICE_ASSET_SUFFIXES`` 顺序试一遍 ``<key>.<ext>``（正式录音），
    再试 ``<key>.placeholder.<ext>``（程序占位）——故任何后缀的正式录音都
    赢过占位音，"录了 .m4a 却听到占位提示音"的问题由此消除。
    """
    return ([f"{key}{s}" for s in VOICE_ASSET_SUFFIXES]
            + [f"{key}{VOICE_PLACEHOLDER_MARK}{s}"
               for s in VOICE_ASSET_SUFFIXES])


class VoiceAssetLoader:
    """``assets/voice/<key>.<ext>`` 多后缀探测（v0.26.0）。

    多后缀是用户拍板需求（2026-10-08）：换格式只换文件后缀，代码零改动。
    探测顺序（v0.26.5 起）：**全部正式后缀 → 全部占位后缀**，正式素材永不
    被占位音遮挡。
    任何异常静默（返 None/False），永不外抛——探测在气泡路径上，坏掉不能
    连带炸掉主动关怀。
    """

    def __init__(self, root: str):
        self.root = "" if root is None else root

    def find(self, key: str) -> str | None:
        """返首个命中文件的绝对路径（正式素材优先，占位音兜底）。"""
        try:
            if not key or not self.root:
                return None
            base = os.fspath(self.root)
            for name in _asset_candidates(key):
                path = os.path.join(base, name)
                if os.path.isfile(path):
                    return os.path.abspath(path)
        except Exception:
            log.debug("[语音] 资产探测异常 key=%r", key, exc_info=True)
            return None
        return None

    def has(self, key: str) -> bool:
        """任一台法后缀存在即视为命中（正式素材或占位音都算"有"）。"""
        try:
            return self.find(key) is not None
        except Exception:
            log.debug("[语音] has 异常 key=%r", key, exc_info=True)
            return False

    def missing(self, keys=None) -> list[str]:
        """收集"全部后缀都缺"的 key；keys 默认 VOICE_KEYS。"""
        out: list[str] = []
        try:
            wanted = tuple(VOICE_KEYS if keys is None else keys)
        except Exception:
            log.debug("[语音] missing keys 不可迭代", exc_info=True)
            return out
        for key in wanted:
            try:
                if not self.has(key):
                    out.append(key)
            except Exception:
                log.debug("[语音] missing 单键异常 key=%r", key, exc_info=True)
                out.append(key)
        return out


# ---- 占位音频生成 ----


def write_placeholder_wav(path: str, seconds: float = 0.35,
                          freq: float = 660.0,
                          sample_rate: int = 44100) -> bool:
    """生成短提示音占位 wav（v0.26.0）——正弦 + 淡入淡出，16bit 单声道。

    **绝不使用任何他人录制的音色**（用户拍板 2026-10-08）：占位期只要
    "能听见有个接入点"，正式音包由用户自行录制后同名替换。

    为什么先写临时文件再 ``os.replace``：目标目录可能已有用户录制的正式
    音包，半截写入既毁文件又难排查；``os.replace`` 同目录改名是原子的。
    失败返 False 且不留半截文件（临时件在 finally 清理）。
    """
    tmp_path: str | None = None
    try:
        if not path:
            return False
        sr = int(sample_rate)
        if sr <= 0:
            return False  # 采样率非法：早退，别让除零走成"写盘失败"的噪声日志
        n = max(1, int(float(seconds) * sr))
        # 0.28 满幅：提示音要能听见但不刺耳；淡入淡出 ~12ms 防起止爆音
        amp = 0.28 * 32767.0
        fade = max(1, min(n // 4, int(0.012 * sr) or 1))
        frames = bytearray()
        for i in range(n):
            if i < fade:
                env = i / fade
            elif i >= n - fade:
                env = (n - 1 - i) / fade
            else:
                env = 1.0
            val = int(amp * env * math.sin(2.0 * math.pi * freq * i / sr))
            frames += struct.pack("<h", max(-32768, min(32767, val)))
        target = os.fspath(path)
        directory = os.path.dirname(os.path.abspath(target))
        os.makedirs(directory, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(suffix=".tmp.wav", dir=directory)
        os.close(fd)
        with wave.open(tmp_path, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(sr)
            wf.writeframes(bytes(frames))
        os.replace(tmp_path, target)
        tmp_path = None  # 已改名，finally 不必清理
        return True
    except Exception:
        log.warning("[语音] 占位音频写入失败 path=%r", path, exc_info=True)
        return False
    finally:
        if tmp_path:
            try:
                os.remove(tmp_path)
            except OSError:
                pass


def ensure_placeholders(root: str, keys=None) -> list[str]:
    """为缺失的 key 生成 ``<root>/<key>.placeholder.wav`` 占位（v0.26.0）。

    已存在（任一合法后缀，含正式素材与旧占位）的 key 直接跳过——用户录制的
    正式音包绝不会被程序生成的提示音覆盖，也**不必删占位音**：正式素材
    ``<key>.<ext>`` 在探测中永远优先于 ``<key>.placeholder.<ext>``（v0.26.5）。
    返已创建文件的绝对路径列表。
    """
    created: list[str] = []
    try:
        if not root:
            return created
        base = os.fspath(root)
        os.makedirs(base, exist_ok=True)
        loader = VoiceAssetLoader(base)
        wanted = tuple(VOICE_KEYS if keys is None else keys)
    except Exception:
        log.warning("[语音] 占位目录准备失败 root=%r", root, exc_info=True)
        return created
    for key in wanted:
        try:
            if loader.has(key):
                continue
            target = os.path.join(
                base, f"{key}{VOICE_PLACEHOLDER_MARK}.wav")
            if os.path.isfile(target):  # has() 已覆盖，双保险防覆写
                continue
            if write_placeholder_wav(target):
                created.append(os.path.abspath(target))
        except Exception:
            log.debug("[语音] 占位生成单键异常 key=%r", key, exc_info=True)
    return created


# ---- 配置 ----


def _as_bool(value, default: bool) -> bool:
    """严格布尔：只认真正的 bool，其余（含 "false" 字符串）回落默认。"""
    return value if isinstance(value, bool) else default


def _as_str(value, default: str) -> str:
    return value if isinstance(value, str) else default


def _as_int(value, default: int) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return default


def _as_float(value, default: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    try:
        val = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return val


@dataclass
class VoiceSettings:
    """语音配置快照（v0.26.0）——总开关默认关，对齐 sound 先例调研 F10。

    ``from_config`` 容错读取 config 的 ``voice`` 段（布局见
    设计-语音模块技术方案.md §4）：``enabled/provider/voice/rate/
    speak_chat/quiet_hours`` 在 ``voice.tts`` 子段，``proactive_speak/
    asset_root/dedup_sec`` 在 ``voice`` 段。段缺失或类型错一律回落默认，
    绝不抛异常——配置写坏不能让宠物起不来。

    v0.26.6 语义修正（用户拍板「语音要和气泡结合」）：``proactive_speak``
    默认改为 **True**——主开关一开，气泡（含主动关怀）与聊天回复**一起朗读**，
    这才是用户点「语音播报」时的预期；该键降级为**细调项**（显式写 false 才
    压掉主动气泡）。``enabled`` 仍默认 False，故未开启前一切静默。

    注意（语义非对称，接线时须知）：``quiet_hours`` 字段默认 ``(23, 8)``，
    即**未配置 quiet_hours 时深夜静默仍然默认生效**；而独立的纯函数
    ``quiet_hours_active`` 对未配置返 False（"没写就是不静默"）。门面走
    前者（settings），故默认行为是"深夜不吵"。
    """

    enabled: bool = False
    provider: str = "asset"
    asset_root: str = ""
    voice: str = ""
    rate: int = 0
    speak_chat: bool = True
    proactive_speak: bool = True
    quiet_hours: tuple[int, int] = (23, 8)
    dedup_sec: float = 3.0

    @classmethod
    def from_config(cls, cfg: dict, default_root: str = "") -> VoiceSettings:
        """从 config 的 ``voice`` 段容错读取；任何异常回落默认（全关）。"""
        settings = cls(asset_root=_as_str(default_root, ""))
        try:
            if not isinstance(cfg, dict):
                return settings
            voice_sec = cfg.get("voice")
            if not isinstance(voice_sec, dict):
                return settings
            tts_sec = voice_sec.get("tts")
            tts_sec = tts_sec if isinstance(tts_sec, dict) else {}

            settings.enabled = _as_bool(tts_sec.get("enabled"), False)
            provider = tts_sec.get("provider")
            settings.provider = (provider if provider in _VOICE_PROVIDERS
                                 else "asset")
            settings.voice = _as_str(tts_sec.get("voice"), "")
            settings.rate = _as_int(tts_sec.get("rate"), 0)
            settings.speak_chat = _as_bool(tts_sec.get("speak_chat"), True)
            # proactive_speak 冻结在 voice 段；容忍误写进 tts 子段。
            # v0.26.6：默认 True（气泡与聊天一起朗读，用户拍板「语音要和
            # 气泡结合」）；显式写 false 才压掉主动气泡。
            if "proactive_speak" in voice_sec:
                settings.proactive_speak = _as_bool(
                    voice_sec.get("proactive_speak"), True)
            else:
                settings.proactive_speak = _as_bool(
                    tts_sec.get("proactive_speak"), True)
            root = voice_sec.get("asset_root")
            if isinstance(root, str) and root:
                settings.asset_root = root
            hours = _parse_quiet(tts_sec.get("quiet_hours"))
            if hours is None:
                hours = _parse_quiet(voice_sec.get("quiet_hours"))
            if hours is not None:
                settings.quiet_hours = hours
            dedup = _as_float(voice_sec.get("dedup_sec"), 3.0)
            settings.dedup_sec = dedup if dedup >= 0 else 3.0
        except Exception:
            log.warning("[语音] voice 配置解析异常，回落默认（全关）",
                        exc_info=True)
        return settings


# ---- 门禁 ----


def normalize_text(text: str) -> str:
    """去重用的文本规范化：strip + 折叠空白 + 小写（v0.26.0）。

    ``str.split()`` 无参形式同时吃全角空格/制表/换行，故折叠空白一步到位。
    """
    try:
        return " ".join(str(text).strip().lower().split())
    except Exception:
        return ""


@dataclass
class SpeakItem:
    """待播条目（v0.26.0）：``source`` 为 ``"chat"`` / ``"proactive"``。"""

    text: str
    key: str | None = None
    source: str = "chat"
    at: float = 0.0


class SpeakGate:
    """播报门禁（v0.26.0）——顺序即优先级，单测锁定。

    1. ``settings.enabled`` 假 → ``disabled``
    2. 来源开关：``proactive`` 且 ``proactive_speak`` 假 → ``proactive_off``；
       ``chat`` 且 ``speak_chat`` 假 → ``chat_off``（v0.26.6 起
       ``proactive_speak`` 默认 True，即气泡与聊天同开；显式关掉才只剩聊天）
    3. ``dnd`` → ``dnd``
    4. ``fullscreen`` → ``fullscreen``（对齐 _auto_bubble 的全屏丢弃语义）
    5. 深夜静默 → ``quiet_hours``
    6. 去重：同规范化文本在 ``dedup_sec`` 窗口内已放行过 → ``dedup``

    放行返 ``(True, "ok")`` 并记录时间戳。**时刻一律由 ``now`` 注入**，
    门禁自己不取时——便于单测直接推到边界（如 3s 窗口的整点）。
    """

    def __init__(self, settings: VoiceSettings):
        self.settings = settings
        self._recent: dict[str, float] = {}

    def allow(self, item: SpeakItem, *, now: float, dnd: bool = False,
              fullscreen: bool = False) -> tuple[bool, str]:
        """判定是否放行；返 ``(放行, 原因串)``。异常保守拒绝，不外抛。"""
        try:
            settings = self.settings
            if not settings.enabled:
                return False, "disabled"
            source = getattr(item, "source", "chat") or "chat"
            if source == "proactive" and not settings.proactive_speak:
                return False, "proactive_off"
            if source == "chat" and not settings.speak_chat:
                return False, "chat_off"
            if dnd:
                return False, "dnd"
            if fullscreen:
                return False, "fullscreen"
            if quiet_hours_active({"quiet_hours": list(settings.quiet_hours)},
                                  now):
                return False, "quiet_hours"
            norm = normalize_text(getattr(item, "text", ""))
            if norm:
                last = self._recent.get(norm)
                if last is not None and now - last < settings.dedup_sec:
                    return False, "dedup"
                if len(self._recent) >= _DEDUP_TABLE_MAX:
                    # 修剪：窗口外的旧记录不可能再命中，长跑进程不涨内存
                    window = max(settings.dedup_sec, 1.0)
                    self._recent = {k: v for k, v in self._recent.items()
                                    if now - v < window}
                self._recent[norm] = float(now)
            return True, "ok"
        except Exception:
            log.debug("[语音] 门禁判定异常，保守拒绝", exc_info=True)
            return False, "error"


# ---- 串行队列 ----


class SpeakQueue:
    """串行播报队列（v0.26.0）——不引入线程/Qt，处理在调用线程内同步完成。

    串行保证靠重入抑制而非锁：``_busy`` 只允许最外层 ``enqueue`` 消费队列，
    处理过程中（sink 内）再次入队只挂到待播列表，待当前项结束按序播完。
    故"同一时刻只应有一个 item 在处理"在单线程内是硬保证。

    ``stop()`` 清空待播并打停止标记；``stop()`` 期间在处理中的当前项，其
    sink 结果被忽略（``enqueue`` 返 False）。停止标记在下一次 ``enqueue``
    时自动解除——stop 语义是"打断当前这一串"，不是"永久关停"（永久关停
    由 ``settings.enabled`` 与托盘开关承担）。
    """

    def __init__(self, sink):
        self._sink = sink
        self._pending: list[SpeakItem] = []
        self._busy = False
        self._stopped = False

    def enqueue(self, item: SpeakItem) -> bool:
        """入队。空闲时在本线程内同步播完并返 sink 结果；处理中只挂队返 True。"""
        try:
            if item is None:
                return False
            self._stopped = False
            self._pending.append(item)
            if self._busy:
                return True  # 串行：交给最外层 drain 统一消费
            return self._drain(item)
        except Exception:
            log.warning("[语音] 入队异常，丢弃该条", exc_info=True)
            return False

    def stop(self) -> None:
        """清空待播 + 打停止标记（当前项结果将被忽略）。幂等、不外抛。"""
        try:
            self._stopped = True
            self._pending.clear()
        except Exception:
            log.debug("[语音] 停止队列异常", exc_info=True)

    def is_busy(self) -> bool:
        """是否有 item 正在处理中。"""
        return bool(self._busy)

    def pending(self) -> int:
        """待播条数（不含处理中的那一条）。"""
        return len(self._pending)

    def _drain(self, target: SpeakItem) -> bool:
        """串行消费；返 ``target`` 这一条的 sink 结果。"""
        result = True
        self._busy = True
        try:
            while self._pending and not self._stopped:
                current = self._pending.pop(0)
                ok = False
                try:
                    ok = bool(self._sink(current))
                except Exception:
                    # 单条播报失败不阻断后续（队列不能被一条坏条目卡死）
                    log.warning("[语音] 播报 sink 异常，丢弃该条", exc_info=True)
                if current is target:
                    result = ok
                if self._stopped:
                    break
        finally:
            self._busy = False
            if self._stopped:
                self._pending.clear()
                result = False  # 当前项结果被忽略
        return result


# ---- 门面 ----


class VoiceService:
    """语音门面（v0.26.0）——项目经理接线用；平台库-free，注入对象可全 None。

    路由：先过 ``SpeakGate``（拒则 debug 日志 + False）→ ``provider ==
    "asset"`` 且显式 ``key`` 命中资产 → ``asset_player.play(path)``；否则
    回落 ``tts_engine.speak(text)`` 动态合成。两者皆 None（win 留坑/未注入）
    或任一环节异常 → 静默降级返 False，永不外抛。

    设计取舍：**只认显式 ``key``**，不做"文本→key"宽松匹配——文案随时可能
    被改（含随机池与 f-string 插值），按文本猜 key 会静默放错音，宁可回落
    动态 TTS。罐头音包的接线方（气泡咽喉）显式传 key 即可。
    """

    def __init__(self, settings: VoiceSettings, *, asset_player=None,
                 tts_engine=None, asset_root: str | None = None):
        self.settings = settings
        self.asset_player = asset_player
        self.tts_engine = tts_engine
        root = settings.asset_root if asset_root is None else asset_root
        self.assets = VoiceAssetLoader(root)
        self.gate = SpeakGate(settings)
        self.queue = SpeakQueue(self._play)

    # -- 内部：单条播报（队列 sink，串行执行） --

    def _play(self, item: SpeakItem) -> bool:
        try:
            path = None
            if self.settings.provider == "asset" and item.key:
                path = self.assets.find(item.key)
            if path is not None and self.asset_player is not None:
                try:
                    if self.asset_player.play(path):
                        return True
                    log.debug("[语音] 罐头播放失败，回落动态 TTS: %s", path)
                except Exception:
                    log.warning("[语音] asset_player 异常，回落动态 TTS",
                                exc_info=True)
            elif path is not None:
                # 命中资产但没有播放器（win 留坑/注入位未接）：静默跳过直放
                log.debug("[语音] 无 asset_player，跳过罐头语音 %s", path)
            engine = self.tts_engine
            if engine is None:
                log.debug("[语音] 无 tts_engine，静默降级（不朗读）")
                return False
            try:
                return bool(engine.speak(item.text))
            except Exception:
                log.warning("[语音] tts_engine 异常，静默降级", exc_info=True)
                return False
        except Exception:
            log.warning("[语音] 播报异常，静默降级", exc_info=True)
            return False

    # -- 对外 --

    def speak(self, text: str, source: str = "chat", key: str | None = None,
              *, now: float | None = None, dnd: bool = False,
              fullscreen: bool = False, force: bool = False) -> bool:
        """过门禁后入队播报；被拒/降级返 False（永不外抛）。

        ``now`` 为 None 时取 ``time.time()``；测试与离线补播请显式注入。

        ``force=True`` **跳过全部门禁**（开关/来源/深夜/去重），直接入队。
        仅供给**界面反馈音**用（如托盘勾选「语音播报」时的"已开启"确认）——
        它必须能在深夜、在主开关刚打开的那一刻出声，否则用户又会"开了没
        反馈"。**不做内容播报**：内容播报一律走门禁，别拿它绕开静默策略。
        """
        try:
            if now is None:
                now = time.time()
            stamp = float(now)
            item = SpeakItem(text="" if text is None else str(text), key=key,
                             source=source, at=stamp)
            if force:
                log.debug("[语音] 强制播报（界面反馈）：%s", item.text[:40])
                return self.queue.enqueue(item)
            allowed, reason = self.gate.allow(item, now=stamp, dnd=dnd,
                                              fullscreen=fullscreen)
            if not allowed:
                log.debug("[语音] 拒播(%s)：%s", reason, item.text[:40])
                return False
            return self.queue.enqueue(item)
        except Exception:
            log.warning("[语音] speak 异常，静默降级", exc_info=True)
            return False

    def stop(self) -> None:
        """打断当前这串：清队 + 停注入的播放器/合成器（esc/关开关时调）。"""
        try:
            self.queue.stop()
        except Exception:
            log.debug("[语音] 队列停止异常", exc_info=True)
        for name, obj in (("asset_player", self.asset_player),
                          ("tts_engine", self.tts_engine)):
            if obj is None:
                continue
            try:
                obj.stop()
            except Exception:
                log.warning("[语音] %s.stop 异常，忽略", name, exc_info=True)

    def is_speaking(self) -> bool:
        """队列忙或有注入对象正在出声即为假。异常保守返 False。"""
        try:
            if self.queue.is_busy() or self.queue.pending():
                return True
            for obj in (self.asset_player, self.tts_engine):
                if obj is None:
                    continue
                try:
                    if obj.is_playing():
                        return True
                except Exception:
                    log.debug("[语音] is_playing 异常，忽略", exc_info=True)
            return False
        except Exception:
            log.debug("[语音] is_speaking 异常", exc_info=True)
            return False
