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

    def _num(name: str, default: float) -> float:
        v = d.get(name, default)
        try:
            return float(v)
        except (TypeError, ValueError):
            log.warning("存档字段 %s 非法 %r，用默认 %s", name, v, default)
            return default

    return PetState(
        mood=_num("mood", 80.0),
        fullness=_num("fullness", 80.0),
        cleanliness=_num("cleanliness", 80.0),
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

    # ---- 冻结接口（签名不动） ----
    def get(self) -> PetState:
        return self._state

    def update(self, **deltas) -> None:
        """按增量更新（deltas 是增量，不是绝对值）。frozen → replace。"""
        if not deltas:
            return
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
                # stage / branch：直接赋值（delta 应已是 Enum）
                kwargs[key] = delta
        if kwargs:
            self._state = replace(self._state, **kwargs)
        self._last_update = time.time()
        self._notify()

    def on_change(self, cb: Callable[[PetState], None]) -> None:
        self._observers.append(cb)

    def save(self, path: str) -> None:
        """原子写：.tmp → fsync → os.replace(.tmp→.json) → copy2(.json→.bak)。"""
        dirpath = os.path.dirname(path)
        if dirpath:
            os.makedirs(dirpath, exist_ok=True)
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
            os.replace(tmp, path)
            shutil.copy2(path, path + ".bak")
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
        state = self._state
        for cb in list(self._observers):
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
        now = time.time()
        dt_hours = (now - self._last_update) / 3600.0
        if dt_hours <= 0:
            return
        deltas: dict = {}
        for key, rate in decay_per_hour.items():
            if key in _NUMERIC_FIELDS:
                try:
                    deltas[key] = -float(rate) * dt_hours
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
            # update 会把 _last_update 推到 now 并触发 observer
            self.update(**deltas)

    # ---- v0.5 进化（非冻结接口，app apply_decay 后调） ----
    _STAGE_ORDER: tuple = (Stage.YOUNG, Stage.ADULT, Stage.FINAL)

    def check_evolve(self, thresholds: dict, score_cfg: dict) -> dict | None:
        """age 到当前 stage 阈值 → 进化一阶 + 判定分支。

        ``thresholds`` 形如 ``{"young": 7, "adult": 21}``（key 取 Stage.value，
        总 age 阈值；FINAL 不在表中即不再进化）。``score_cfg`` 形如
        ``{"mood_weight":0.4,"fullness_weight":0.4,"cleanliness_weight":0.2,
        "healthy_threshold":70}``。分支取**当前养护分**（TODO 时间平均，
        fast-mode 下当前≈平均），≥阈值→HEALTHY 否则 NEGLECTED。命中则内部
        ``update(stage=, branch=)`` 触发 observer（持久化/emoji 切换）并返回
        事件 dict；未命中返回 None。一次调用最多进化一阶（防刷屏）。
        """
        state = self._state
        if state.stage == Stage.FINAL:
            return None
        try:
            thr = float(thresholds.get(state.stage.value, 0))
        except (TypeError, ValueError):
            log.warning("evolve_threshold_days.%s 非法，按 0", state.stage.value)
            thr = 0.0
        if state.age < thr:
            return None
        idx = self._STAGE_ORDER.index(state.stage)
        new_stage = self._STAGE_ORDER[idx + 1]
        score = (
            float(score_cfg.get("mood_weight", 0.4)) * state.mood
            + float(score_cfg.get("fullness_weight", 0.4)) * state.fullness
            + float(score_cfg.get("cleanliness_weight", 0.2)) * state.cleanliness
        )
        try:
            healthy = float(score_cfg.get("healthy_threshold", 70))
        except (TypeError, ValueError):
            healthy = 70.0
        new_branch = Branch.HEALTHY if score >= healthy else Branch.NEGLECTED
        # TODO(v0.5+)：分支取本阶段养护分时间平均（需累计本阶段分+计数），
        # 当前用进化那一刻的瞬时分；fast-mode 衰减慢，瞬时≈平均，Must 不卡。
        self.update(stage=new_stage, branch=new_branch)
        return {"from_stage": state.stage, "to_stage": new_stage, "branch": new_branch}

    def reset(self) -> None:
        """v0.5 重置：清内存状态回 default（YOUNG/HEALTHY/age=0/数值默认），
        重置 last_update，触发 observer。存档文件由 app 侧删除（避免单实例
        锁下 execv 自锁死，故走 in-process 复位而非重启进程）。
        """
        self._state = PetState.default()
        self._last_update = time.time()
        self._notify()

    @property
    def last_update(self) -> float:
        return self._last_update
