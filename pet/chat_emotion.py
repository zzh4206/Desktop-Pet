"""本地聊天情绪：跨平台消息存储、CPU MLP 推理和每日调度。

本模块故意不依赖 Qt、平台 API 或网络；app 负责把结果显示为立绘/气泡。
模型是训练期 PyTorch 导出的 NumPy 权重：4096 → 128 → 64 → 5。
"""
from __future__ import annotations

import json
import logging
import math
import os
import time
from dataclasses import dataclass
from datetime import datetime
from hashlib import blake2b
from typing import Iterable

try:
    import numpy as np
except ImportError:  # 让主程序在可选依赖未安装时安全降级
    np = None

log = logging.getLogger("pet")

LABELS = ("happy", "neutral", "sad", "sleepy", "hungry")
FEATURE_DIM = 4096
MODEL_VERSION = 1


@dataclass(frozen=True)
class EmotionResult:
    label: str
    confidence: float
    used_fallback: bool = False
    model_version: int | None = None


def fallback_for_slot(slot: str | None) -> str:
    return "sleepy" if slot == "22:00" else "neutral"


def is_significant(result: EmotionResult, threshold: float) -> bool:
    """消息到来时只响应明确、非中性的情绪，避免普通聊天频繁换脸。"""
    return (not result.used_fallback and result.label != "neutral"
            and result.confidence >= float(threshold))


# 这是模型的保守护栏，不是对话内容上传或 LLM 调用。它只处理极直白、
# 无歧义的自述，避免小样本首版模型把“我好难过”一类话错当成 neutral。
_OBVIOUS_SIGNALS = {
    "happy": ("好高兴", "很高兴", "太高兴", "好开心", "很开心", "太开心", "好快乐", "太快乐", "太棒了", "真棒", "happy"),
    "sad": ("好难过", "很难过", "太难过", "好伤心", "很伤心", "太伤心", "想哭", "崩溃了", "不开心", "sad"),
    "sleepy": ("好困", "很困", "太困", "想睡觉", "要睡觉", "困死了", "sleepy"),
    "hungry": ("好饿", "很饿", "太饿", "饿死了", "想吃东西", "想吃饭", "hungry"),
}


def obvious_emotion(text: str) -> EmotionResult | None:
    """识别最新一条消息中的明确情绪词，供即时交互作可靠兜底。"""
    compact = "".join((text or "").split()).lower()
    if compact == "neutral":
        return EmotionResult("neutral", 1.0, False, MODEL_VERSION)
    for label, signals in _OBVIOUS_SIGNALS.items():
        if any(signal in compact for signal in signals):
            return EmotionResult(label, 1.0, False, MODEL_VERSION)
    return None


class ConversationEmotionStore:
    """只保存用户文本的滚动本地档；采用原子写和 .bak 兜底。"""
    def __init__(self, path: str, retention_hours: float = 48) -> None:
        self.path = path
        self.retention_s = max(1.0, float(retention_hours) * 3600)
        self.messages: list[dict] = []
        self.ran_slots: dict[str, list[str]] = {}
        self.current: dict | None = None
        self.feedback: list[dict] = []
        self.load()

    def load(self) -> None:
        for candidate in (self.path, self.path + ".bak"):
            try:
                with open(candidate, encoding="utf-8") as f:
                    raw = json.load(f)
                if not isinstance(raw, dict):
                    continue
                self.messages = [m for m in raw.get("messages", [])
                                 if isinstance(m, dict) and isinstance(m.get("text"), str)
                                 and isinstance(m.get("at"), (int, float))]
                self.ran_slots = {str(k): list(v) for k, v in raw.get("ran_slots", {}).items()
                                  if isinstance(v, list)}
                self.current = raw.get("current") if isinstance(raw.get("current"), dict) else None
                self.feedback = [x for x in raw.get("feedback", []) if isinstance(x, dict)]
                self.prune(save=False)
                return
            except (OSError, ValueError, json.JSONDecodeError):
                continue

    def save(self) -> None:
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        data = {"version": 1, "messages": self.messages, "ran_slots": self.ran_slots,
                "current": self.current, "feedback": self.feedback}
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.flush(); os.fsync(f.fileno())
        if os.path.exists(self.path):
            try:
                import shutil; shutil.copy2(self.path, self.path + ".bak")
            except OSError:
                pass
        os.replace(tmp, self.path)

    def add_user_message(self, text: str, at: float | None = None) -> None:
        text = (text or "").strip()
        if not text:
            return
        self.messages.append({"at": float(at if at is not None else time.time()), "text": text})
        self.prune(save=False); self.save()

    def recent_messages(self, now: float | None = None) -> list[dict]:
        self.prune(now, save=False)
        return list(self.messages)

    def prune(self, now: float | None = None, save: bool = True) -> None:
        now = float(now if now is not None else time.time())
        cutoff = now - self.retention_s
        before = len(self.messages)
        self.messages = [m for m in self.messages if float(m["at"]) >= cutoff]
        # 仅保留最近 14 天的调度去重记录，防档案无限增长。
        today = datetime.fromtimestamp(now).date().isoformat()
        self.ran_slots = {d: s for d, s in self.ran_slots.items() if d >= today[:4] + "-01-01"}
        if save and before != len(self.messages): self.save()

    def due_slots(self, schedule: Iterable[str], now: float | None = None) -> list[str]:
        now_dt = datetime.fromtimestamp(now if now is not None else time.time())
        day = now_dt.date().isoformat(); ran = set(self.ran_slots.get(day, []))
        current = now_dt.strftime("%H:%M")
        return [slot for slot in schedule if slot <= current and slot not in ran]

    def mark_slot(self, slot: str, now: float | None = None) -> None:
        day = datetime.fromtimestamp(now if now is not None else time.time()).date().isoformat()
        self.ran_slots.setdefault(day, []).append(slot); self.save()

    def set_current(self, result: EmotionResult, expires_at: float | None) -> None:
        self.current = {"label": result.label, "confidence": result.confidence,
                        "expires_at": expires_at, "model_version": result.model_version}
        self.save()

    def active_label(self, now: float | None = None) -> str | None:
        if not self.current: return None
        expires_at = self.current.get("expires_at")
        if expires_at is not None and float(expires_at) <= float(now if now is not None else time.time()):
            self.current = None; self.save(); return None
        label = self.current.get("label")
        return label if label in LABELS else None

    def clear_messages(self) -> None:
        self.messages = []; self.current = None; self.save()

    def add_feedback(self, expected: str, predicted: str) -> None:
        if expected in LABELS:
            self.feedback.append({"at": time.time(), "expected": expected, "predicted": predicted})
            self.save()


class ChatEmotionEngine:
    def __init__(self, model_path: str, threshold: float = .55, feature_dim: int = FEATURE_DIM) -> None:
        self.threshold = float(threshold); self.feature_dim = int(feature_dim)
        self.weights = None; self.version = None
        if np is None:
            log.warning("未安装 numpy，聊天情绪功能降级")
            return
        try:
            raw = np.load(model_path, allow_pickle=False)
            if int(raw["feature_dim"]) != self.feature_dim or tuple(raw["labels"].tolist()) != LABELS:
                raise ValueError("模型元数据不兼容")
            self.weights = tuple(raw[k].astype(np.float32) for k in ("w1", "b1", "w2", "b2", "w3", "b3"))
            self.version = int(raw["model_version"])
        except Exception as exc:
            log.warning("聊天情绪模型不可用，将使用时段回退：%s", exc)

    def _features(self, messages: Iterable[dict], now: float) -> "np.ndarray":
        vec = np.zeros(self.feature_dim, dtype=np.float32)
        for message in messages:
            age_h = max(0., (now - float(message["at"])) / 3600.)
            weight = math.exp(-age_h / 18.)
            text = "".join((message.get("text") or "").split()).lower()
            for n in (2, 3, 4):
                for i in range(max(0, len(text) - n + 1)):
                    gram = text[i:i + n].encode("utf-8")
                    idx = int.from_bytes(blake2b(gram, digest_size=4).digest(), "little") % self.feature_dim
                    vec[idx] += weight
        norm = float(np.linalg.norm(vec))
        return vec / norm if norm else vec

    def evaluate(self, messages: Iterable[dict], scheduled_time: str | None = None,
                 now: float | None = None) -> EmotionResult:
        fallback = fallback_for_slot(scheduled_time)
        if self.weights is None:
            return EmotionResult(fallback, 0., True, self.version)
        x = self._features(messages, float(now if now is not None else time.time()))
        if not x.any(): return EmotionResult(fallback, 0., True, self.version)
        w1, b1, w2, b2, w3, b3 = self.weights
        h1 = np.maximum(0., x @ w1 + b1); h2 = np.maximum(0., h1 @ w2 + b2)
        logits = h2 @ w3 + b3; logits -= logits.max()
        probs = np.exp(logits); probs /= probs.sum()
        idx = int(np.argmax(probs)); confidence = float(probs[idx])
        if confidence < self.threshold:
            return EmotionResult(fallback, confidence, True, self.version)
        return EmotionResult(LABELS[idx], confidence, False, self.version)
