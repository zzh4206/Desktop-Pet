"""养成 State（单一真相源）—— 接口冻结于 设计思路.md §2.2。

v0.2：实装 ``PetStateStore``（get/update/on_change/save/load）。
``PetState`` 保持 ``frozen=True``，``update`` 经 ``dataclasses.replace``
生成新实例（不改成 mutable）。持久化走原子写（.tmp → os.replace →
fsync）+ ``.bak`` 备份；``load`` 读顶层 ``version`` 走 migrate 链，
``.json`` 损坏/缺失从 ``.bak`` 恢复，都坏回 ``PetState.default()``。
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import threading
import time
from dataclasses import asdict, dataclass, replace
from enum import Enum
from typing import Callable

log = logging.getLogger("pet")

# 存档 schema 版本；load 读到此值即停止 migrate。v0.2 = 1。
SCHEMA_VERSION = 1

# 0-100 数值字段（衰减/交互都作用于此，update 时 clamp）
_NUMERIC_FIELDS = ("mood", "fullness", "cleanliness")


class Stage(Enum):
    YOUNG = "young"
    ADULT = "adult"
    FINAL = "final"


class Branch(Enum):
    HEALTHY = "healthy"
    NEGLECTED = "neglected"


class Mood(Enum):
    HAPPY = "happy"
    NEUTRAL = "neutral"
    SAD = "sad"
    SLEEPY = "sleepy"
    HUNGRY = "hungry"


@dataclass(frozen=True)
class PetState:
    """冻结的养成状态（单一真相源）。frozen=True 不变。"""

    mood: float = 80.0
    fullness: float = 80.0
    cleanliness: float = 80.0
    age: float = 0.0
    stage: Stage = Stage.YOUNG
    branch: Branch = Branch.HEALTHY

    @classmethod
    def default(cls) -> "PetState":
        return cls()


def _enum_from_str(value, enum_cls, default):
    if isinstance(value, enum_cls):
        return value
    if isinstance(value, str):
        for member in enum_cls:
            if member.value == value:
                return member
    return default


def _state_to_dict(state: PetState) -> dict:
    d = asdict(state)
    d["stage"] = state.stage.value
    d["branch"] = state.branch.value
    return d


def _state_from_dict(d: dict | None) -> PetState:
    """从存档 dict 还原 PetState，缺字段/非法值填默认 + 告警。"""
    if not isinstance(d, dict):
        d = {}

    def _num(name: str, default: float, clamp_lo=None, clamp_hi=None) -> float:
        v = d.get(name, default)
        try:
            val = float(v)
        except (TypeError, ValueError):
            log.warning("存档字段 %s 非法 %r，用默认 %s", name, v, default)
            val = default
        if clamp_lo is not None and val < clamp_lo:
            val = float(clamp_lo)
        if clamp_hi is not None and val > clamp_hi:
            val = float(clamp_hi)
        return val

    return PetState(
        mood=_num("mood", 80.0, 0.0, 100.0),
        fullness=_num("fullness", 80.0, 0.0, 100.0),
        cleanliness=_num("cleanliness", 80.0, 0.0, 100.0),
        age=max(0.0, _num("age", 0.0)),
        stage=_enum_from_str(d.get("stage"), Stage, Stage.YOUNG),
        branch=_enum_from_str(d.get("branch"), Branch, Branch.HEALTHY),
    )


def migrate_v1_to_v2(raw: dict) -> dict:
    """v1 → v2 迁移占位（v0.2 字段未增，no-op）。

    v0.3+ 给 PetState 加字段时在此填缺字段默认 + 改 schema，并把
    SCHEMA_VERSION 提到 2。v0.2 先占位保证 migrate 链结构就位。
    """
    return raw


# migrate 链：version N → 调用 _MIGRATORS[N] 得到下一版 raw
_MIGRATORS: dict[int, Callable[[dict], dict]] = {
    1: migrate_v1_to_v2,
}


class PetStateStore:
    """养成状态存储 + observer。签名冻结于 设计思路.md §2.2，v0.2 实装行为。"""

    def __init__(self, state: PetState, last_update: float | None = None):
        self._state = state
        self._observers: list[Callable[[PetState], None]] = []
        self._last_update = (
            float(last_update) if last_update is not None else time.time()
        )
        self._lock = threading.Lock()  # v0.5.3：保护 update/save/_notify 线程安全
        self._dirty = False  # H1（REVIEW-2026-09-04）：有未落盘变更

    @property
    def dirty(self) -> bool:
        """update/reset 置位，save 成功清零——app 周期/防抖落盘的前置判断。"""
        return self._dirty

    # ---- 冻结接口（签名不动） ----
    def get(self) -> PetState:
        return self._state

    def update(self, **deltas) -> None:
        """按增量更新（deltas 是增量，不是绝对值）。frozen → replace。"""
        if not deltas:
            return
        with self._lock:
            kwargs: dict = {}
            for key, delta in deltas.items():
                if not hasattr(self._state, key):
                    log.warning("update 忽略未知字段 %s", key)
                    continue
                cur = getattr(self._state, key)
                if key in _NUMERIC_FIELDS:
                    kwargs[key] = max(0.0, min(100.0, float(cur) + float(delta)))
                elif key == "age":
                    kwargs[key] = max(0.0, float(cur) + float(delta))
                else:
                    # stage / branch：校验 Enum 类型（防 update(stage="adult") 使
                    # state.stage 变 str，后续 .value/_STAGE_ORDER.index 崩）
                    if key == "stage":
                        kwargs[key] = _enum_from_str(delta, Stage, self._state.stage)
                    elif key == "branch":
                        kwargs[key] = _enum_from_str(delta, Branch, self._state.branch)
                    else:
                        kwargs[key] = delta
            if not kwargs:
                return  # 无有效变更不 notify（旧版仍触发，v0.5.3）
            new_state = replace(self._state, **kwargs)
            # 只有实际变化才更新+notify（update(mood=0) 等值不触发）
            if new_state == self._state:
                self._last_update = time.time()
                return
            self._state = new_state
            self._dirty = True
            self._last_update = time.time()
            observers = list(self._observers)
        for cb in observers:
            try:
                # 批次E/L11：传本次提交的 new_state——锁外再读 self._state
                # 可能拿到比本次更新的状态，观察者视角语义混乱
                cb(new_state)
            except Exception:
                log.exception("on_change 回调异常")

    def on_change(self, cb: Callable[[PetState], None]) -> None:
        self._observers.append(cb)

    def off_change(self, cb: Callable[[PetState], None]) -> None:
        """取消订阅（v0.5.3：旧版只增不减，重复注册重复触发）。"""
        try:
            self._observers.remove(cb)
        except ValueError:
            pass

    def save(self, path: str) -> None:
        """原子写：先 copy2 旧 .json→.bak（.bak 为上一版可兜底），再
        .tmp→fsync→os.replace(.tmp→.json)→fsync 目录（POSIX）。

        v0.5.3：.bak 改为 replace 前备份旧 .json（旧版 replace 后才 copy2，
        .bak 是新 .json 副本，本次序列化错误时 .json/.bak 同时坏无法兜底）。
        """
        dirpath = os.path.dirname(path)
        if dirpath:
            os.makedirs(dirpath, exist_ok=True)
        with self._lock:
            data = {
                "version": SCHEMA_VERSION,
                "last_update": self._last_update,
                "state": _state_to_dict(self._state),
            }
        tmp = path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
                f.flush()
                os.fsync(f.fileno())
            # 先备份旧 .json → .bak（replace 前旧版还在，可兜底本次序列化错误）
            if os.path.exists(path):
                try:
                    shutil.copy2(path, path + ".bak")
                except OSError:
                    pass
            os.replace(tmp, path)
            self._dirty = False  # H1：落盘成功才清 dirty（失败保持，下轮重试）
            # POSIX 下 fsync 目录项变更持久化（Windows 无需，跳过）
            if os.name != "nt":
                try:
                    dirfd = os.open(dirpath or ".", os.O_RDONLY)
                    try:
                        os.fsync(dirfd)
                    finally:
                        os.close(dirfd)
                except OSError:
                    pass
        except OSError:
            # 残留 .tmp 无害（下次 save 覆盖）；不让写盘失败崩 app
            if os.path.exists(tmp):
                try:
                    os.remove(tmp)
                except OSError:
                    pass
            log.exception("save 失败: %s", path)

    @classmethod
    def load(cls, path: str) -> "PetStateStore":
        """读 .json（失败转 .bak）→ version migrate → 还原。都坏回 default。"""
        for p in (path, path + ".bak"):
            try:
                with open(p, "r", encoding="utf-8") as f:
                    raw = json.load(f)
            except FileNotFoundError:
                continue
            except (json.JSONDecodeError, OSError) as e:
                log.warning("存档 %s 解析失败，尝试下一份: %s", p, e)
                continue
            if not isinstance(raw, dict):
                log.warning("存档 %s 顶层非 dict，跳过", p)
                continue
            state, last_update = cls._migrate_and_build(raw)
            return cls(state, last_update)
        log.warning("无可用存档（%s 与 .bak 均缺失/损坏），回退 PetState.default()", path)
        return cls(PetState.default())

    # ---- 内部 ----
    @staticmethod
    def _migrate_and_build(raw: dict) -> tuple[PetState, float | None]:
        version = raw.get("version", 1)
        try:
            version = int(version)
        except (TypeError, ValueError):
            log.warning("存档 version 非法 %r，按 1 处理", raw.get("version"))
            version = 1
        # 走 migrate 链直到当前 SCHEMA_VERSION
        while version < SCHEMA_VERSION:
            migrator = _MIGRATORS.get(version)
            if migrator is None:
                log.warning("无 %d→%d migrator，停止迁移", version, version + 1)
                break
            raw = migrator(raw)
            version += 1
        last_update = raw.get("last_update")
        try:
            last_update = float(last_update) if last_update is not None else None
        except (TypeError, ValueError):
            log.warning("存档 last_update 非法 %r，忽略", raw.get("last_update"))
            last_update = None
        return _state_from_dict(raw.get("state")), last_update

    def _notify(self) -> None:
        with self._lock:
            state = self._state
            observers = list(self._observers)
        for cb in observers:
            try:
                cb(state)
            except Exception:
                log.exception("on_change 回调异常")

    # ---- v0.2 衰减驱动（非冻结接口，app 1s QTimer 调） ----
    def apply_decay(
        self,
        decay_per_hour: dict,
        age_speed_multiplier: float = 1.0,
    ) -> None:
        """按 wall-clock delta（last_update→now）衰减 mood/fullness/cleanliness，
        并增长 age（v0.5）。

        基于时间戳而非累加 dt：重启间也照算（load 读回 last_update 后
        第一次 apply_decay 即补上离线期间的衰减 + age 增长）。age 增长 =
        ``dt_days * age_speed_multiplier``（fast-mode 设大值秒级跳阶段）。
        """
        decay_per_hour = decay_per_hour or {}
        now = time.time()
        dt_hours = (now - self._last_update) / 3600.0
        if dt_hours <= 0:
            # 时钟回拨：仍更新 last_update，防衰减长期停滞（v0.5.3）
            self._last_update = now
            log.warning("apply_decay dt_hours<=0（时钟回拨?），重置 last_update")
            return
        deltas: dict = {}
        for key, rate in decay_per_hour.items():
            if key in _NUMERIC_FIELDS:
                try:
                    # 校验 rate 非负（负配置致数值不降反升，v0.5.3）
                    rate = max(0.0, float(rate))
                    deltas[key] = -rate * dt_hours
                except (TypeError, ValueError):
                    log.warning("decay_per_hour.%s 非法 %r，跳过", key, rate)
        # v0.5：年龄随 wall-clock 增长（age_speed_multiplier 进 config，含 fast-mode）
        dt_days = dt_hours / 24.0
        try:
            deltas["age"] = dt_days * float(age_speed_multiplier)
        except (TypeError, ValueError):
            log.warning("age_speed_multiplier 非法 %r，按 1.0", age_speed_multiplier)
            deltas["age"] = dt_days
        if deltas:
            # H1（REVIEW-2026-09-04）：亚粒度衰减不触发 update——age 每秒微增
            # 会让 on_change 每秒触发、500ms 防抖必然到期，全量落盘
            # （dump+fsync+bak+replace）每秒一次 24/7。未跨档整体跳过：
            # 不推 _last_update，wall-clock 自然累计到跨档为止，增量只延后不丢。
            if not self._decay_meaningful(deltas):
                return
            # update 会把 _last_update 推到 now 并触发 observer
            self.update(**deltas)

    _DECAY_NUM_STEP = 0.1              # 数值跨一个 0.1 档才算有意义
    _DECAY_AGE_STEP_DAYS = 1.0 / 1440.0  # age 长 1 分钟才算有意义

    def _decay_meaningful(self, deltas: dict) -> bool:
        state = self._state
        for key, delta in deltas.items():
            if key == "age":
                if abs(float(delta)) >= self._DECAY_AGE_STEP_DAYS:
                    return True
            elif key in _NUMERIC_FIELDS:
                cur = float(getattr(state, key))
                new = max(0.0, min(100.0, cur + float(delta)))
                if round(new, 1) != round(cur, 1):
                    return True
        return False

    # ---- v0.5 进化（非冻结接口，app apply_decay 后调） ----
    _STAGE_ORDER: tuple = (Stage.YOUNG, Stage.ADULT, Stage.FINAL)

    def check_evolve(
        self, thresholds: dict, score_cfg: dict, avg_score: float | None = None
    ) -> dict | None:
        """age 到当前 stage 阈值 → 进化一阶 + 判定分支。

        ``thresholds`` 形如 ``{"young": 7, "adult": 21}``（key 取 Stage.value，
        总 age 阈值；FINAL 不在表中即不再进化）。``score_cfg`` 形如
        ``{"mood_weight":0.4,"fullness_weight":0.4,"cleanliness_weight":0.2,
        "healthy_threshold":70}``。分支取养护分：``avg_score`` 优先（离线多阶
        进化时传时间平均分，避免数值衰减到底全判 NEGLECTED，v0.5.3），否则用
        当前瞬时养护分。命中则内部 ``update(stage=, branch=)`` 触发 observer
        （持久化/emoji 切换）并返回事件 dict；未命中返回 None。一次调用最多
        进化一阶（防刷屏，app 离线补衰减后循环调用补齐多阶）。
        """
        thresholds = thresholds or {}
        score_cfg = score_cfg or {}
        state = self._state
        if state.stage == Stage.FINAL:
            return None
        try:
            # 阈值缺省 inf（旧版缺省 0 致漏配立即进化，v0.5.3）
            thr = float(thresholds.get(state.stage.value, float("inf")))
        except (TypeError, ValueError):
            log.warning("evolve_threshold_days.%s 非法，按 inf", state.stage.value)
            thr = float("inf")
        if state.age < thr:
            return None
        try:
            idx = self._STAGE_ORDER.index(state.stage)
            new_stage = self._STAGE_ORDER[idx + 1]
        except (ValueError, IndexError):
            log.warning("stage %s 不在 _STAGE_ORDER 或已末阶", state.stage)
            return None
        # 养护分：avg_score 优先（离线多阶进化用时间平均，否则瞬时）
        if avg_score is None:
            score = (
                float(score_cfg.get("mood_weight", 0.4)) * state.mood
                + float(score_cfg.get("fullness_weight", 0.4)) * state.fullness
                + float(score_cfg.get("cleanliness_weight", 0.2)) * state.cleanliness
            )
        else:
            score = float(avg_score)
        try:
            healthy = float(score_cfg.get("healthy_threshold", 70))
        except (TypeError, ValueError):
            healthy = 70.0
        new_branch = Branch.HEALTHY if score >= healthy else Branch.NEGLECTED
        self.update(stage=new_stage, branch=new_branch)
        # 返回 .value 字符串（旧版返 Enum 不可 json.dumps，v0.5.3）
        return {
            "from_stage": state.stage.value,
            "to_stage": new_stage.value,
            "branch": new_branch.value,
        }

    def reset(self) -> None:
        """v0.5 重置：清内存状态回 default（YOUNG/HEALTHY/age=0/数值默认），
        重置 last_update，触发 observer。存档文件由 app 侧删除（避免单实例
        锁下 execv 自锁死，故走 in-process 复位而非重启进程）。

        批次E/L11（REVIEW-2026-08-28）：状态变更收进锁——类内明确带 _lock
        自称线程安全，旧版 reset 绕锁直写。"""
        with self._lock:
            self._state = PetState.default()
            self._last_update = time.time()
            self._dirty = True
        self._notify()

    @property
    def last_update(self) -> float:
        return self._last_update
