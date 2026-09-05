"""本地聊天情绪：跨平台消息存储、CPU MLP 推理和每日调度。

本模块故意不依赖 Qt、平台 API 或网络；app 负责把结果显示为立绘/气泡。
模型是训练期 PyTorch 导出的 NumPy 权重：4096 → 128 → 64 → 5。
"""
from __future__ import annotations

import json
import logging
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

try:
    import onnxruntime as ort
    from tokenizers import Tokenizer
except ImportError:  # v1 仍可在未安装 v2 运行时依赖的环境中安全降级
    ort = None; Tokenizer = None

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


def ngram_vector(texts: Iterable[str], weights: Iterable[float] | None = None,
                 feature_dim: int = FEATURE_DIM) -> "np.ndarray":
    """2–4 gram blake2b 特征哈希——训练（tools/train_chat_emotion）与推理
    共享的唯一实现（M9，REVIEW-2026-09-04：旧版双份手工同步，单侧改动即
    静默训练-推理 skew）。``weights`` 与 ``texts`` 等长（缺省全 1），
    返回 L2 归一化向量。
    """
    clean = ["".join(str(t).split()).lower() for t in texts]
    ws = list(weights) if weights is not None else [1.0] * len(clean)
    vec = np.zeros(feature_dim, dtype=np.float32)
    for text, w in zip(clean, ws):
        for n in (2, 3, 4):
            for i in range(max(0, len(text) - n + 1)):
                idx = int.from_bytes(
                    blake2b(text[i:i + n].encode("utf-8"), digest_size=4).digest(),
                    "little") % feature_dim
                vec[idx] += w
    norm = float(np.linalg.norm(vec))
    return vec / norm if norm else vec


# 这是模型的保守护栏，不是对话内容上传或 LLM 调用。它只处理极直白、
# 无歧义的自述，避免小样本首版模型把“我好难过”一类话错当成 neutral。
_OBVIOUS_SIGNALS = {
    "happy": ("好高兴", "很高兴", "太高兴", "好开心", "很开心", "太开心", "好快乐", "太快乐", "太棒了", "真棒", "happy"),
    "sad": ("好难过", "很难过", "太难过", "好伤心", "很伤心", "太伤心", "想哭", "崩溃了", "不开心", "sad"),
    "sleepy": ("好困", "很困", "太困", "想睡觉", "要睡觉", "困死了", "sleepy"),
    "hungry": ("好饿", "很饿", "太饿", "饿死了", "想吃东西", "想吃饭", "hungry"),
}


def obvious_emotion(text: str, model_version: int = MODEL_VERSION
                    ) -> EmotionResult | None:
    """识别最新一条消息中的明确情绪词，供即时交互作可靠兜底。

    批次C/P3-17（REVIEW-2026-09-05）：model_version 默认仍 MODEL_VERSION
    （v1 兜底语义不变，直调方/测试兼容）；app 即时路径显式传 engine.version，
    v2 引擎激活时元数据不再错标（该函数本就只在 v1 分支被调用）。
    """
    compact = "".join((text or "").split()).lower()
    if compact == "neutral":
        return EmotionResult("neutral", 1.0, False, model_version)
    for label, signals in _OBVIOUS_SIGNALS.items():
        if any(signal in compact for signal in signals):
            return EmotionResult(label, 1.0, False, model_version)
    return None


class ConversationEmotionStore:
    """只保存用户文本的滚动本地档；采用原子写和 .bak 兜底。"""
    def __init__(self, path: str, retention_hours: float = 48) -> None:
        self.path = path
        self.retention_s = max(1.0, float(retention_hours) * 3600)
        self.messages: list[dict] = []
        self.ran_slots: dict[str, list[str]] = {}
        self.current: dict | None = None
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
                self.prune(save=False)
                return
            except (OSError, ValueError, json.JSONDecodeError):
                continue

    def save(self) -> None:
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        data = {"version": 1, "messages": self.messages, "ran_slots": self.ran_slots,
                "current": self.current}
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
        # 批次B/P2-11（REVIEW-2026-09-05）：消息清空（或禁用后清残档）时
        # .bak 仍留旧文本——空档同步移除 .bak，与"隐私最小化档案"承诺一致。
        if not self.messages:
            try:
                os.remove(self.path + ".bak")
            except OSError:
                pass

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
        # 调度去重记录保留当年（跨年 1 月 1 日清零，防档案无限增长；
        # 时段每日重复，跨年重跑一次无害）——L19：旧注释"最近 14 天"与实现不符
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

    def clear_current(self) -> None:
        """清除短时聊天表情；不会影响保留的用户消息。"""
        self.current = None
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


class ChatEmotionEngine:
    def __init__(self, model_path: str, threshold: float = .55, feature_dim: int = FEATURE_DIM) -> None:
        self.threshold = float(threshold); self.feature_dim = int(feature_dim)
        self.weights = None; self.version = None
        self.session = None; self.tokenizer = None; self.classifier = None
        if np is None:
            log.warning("未安装 numpy，聊天情绪功能降级")
            return
        try:
            if os.path.isdir(model_path):
                try:
                    self._load_v2(model_path)
                    return
                except Exception as exc:
                    # L1（REVIEW-2026-09-04）：v2 目录存在但资产残缺 → 回退
                    # 同目录旁完好的 v1.npz（旧版直接降级时段兜底，v1 白白闲置）
                    v1 = os.path.join(
                        os.path.dirname(os.path.normpath(model_path)),
                        "chat_emotion_v1.npz")
                    if not os.path.isfile(v1):
                        raise
                    log.warning("v2 模型不可用，回退 v1：%s", exc)
                    model_path = v1
            raw = np.load(model_path, allow_pickle=False)
            if int(raw["feature_dim"]) != self.feature_dim or tuple(raw["labels"].tolist()) != LABELS:
                raise ValueError("模型元数据不兼容")
            self.weights = tuple(raw[k].astype(np.float32) for k in ("w1", "b1", "w2", "b2", "w3", "b3"))
            self.version = int(raw["model_version"])
        except Exception as exc:
            log.warning("聊天情绪模型不可用，将使用时段回退：%s", exc)

    def _load_v2(self, model_dir: str) -> None:
        """加载量化 ONNX 编码器和线性分类头；不触碰任何平台 API。"""
        if ort is None or Tokenizer is None:
            raise RuntimeError("v2 需要 onnxruntime 和 tokenizers")
        raw = np.load(os.path.join(model_dir, "classifier.npz"), allow_pickle=False)
        if tuple(raw["labels"].tolist()) != LABELS or int(raw["version"]) != 2:
            raise ValueError("v2 模型元数据不兼容")
        self.classifier = (raw["weight"].astype(np.float32), raw["bias"].astype(np.float32), int(raw["max_length"]))
        self.tokenizer = Tokenizer.from_file(os.path.join(model_dir, "tokenizer", "tokenizer.json"))
        self.tokenizer.enable_truncation(max_length=self.classifier[2])
        self.tokenizer.enable_padding()
        self.session = ort.InferenceSession(os.path.join(model_dir, "encoder.int8.onnx"), providers=["CPUExecutionProvider"])
        self.version = 2

    def _features(self, messages: Iterable[dict], now: float | None = None) -> "np.ndarray":
        # M9（REVIEW-2026-09-04）：对齐训练窗口——v1 训练 context 恒 ≤5 句
        # 等权，旧版推理带 exp 衰减且上下文无上限（48h 全量）=分布漂移。
        # now 参数保留仅为调用方兼容，不再参与计算。
        return ngram_vector([m.get("text", "") for m in list(messages)[-5:]])

    def evaluate(self, messages: Iterable[dict], scheduled_time: str | None = None,
                 now: float | None = None,
                 threshold: float | None = None) -> EmotionResult:
        """推理情绪标签。

        M3（REVIEW-2026-09-04）：``threshold`` 允许调用方按路径覆写内层置信
        截断（缺省仍用构造时的 ``confidence_threshold``）——旧版内层 0.55
        截断把结果标 used_fallback 后，app 层 event_confidence_threshold
        （可配 0.5）的 is_significant 判定永远先被 `not used_fallback` 否决，
        有效触发阈值恒为 max(两者)，低配侧旋钮是死的。
        """
        fallback = fallback_for_slot(scheduled_time)
        cut = self.threshold if threshold is None else float(threshold)
        messages = list(messages)
        if self.session is not None:
            # 批次B/P2-8（REVIEW-2026-09-05）：逐句推理、概率平均——encoder
            # 训练分布是单句（build_chat_emotion_v2_data 每 row 一个 text），
            # 旧版把最近 3 句 "\n".join 成单串喂入，tokenize 后空白折叠 =
            # 模型从未见过的分布外输入，每日定时路径（48h 档取尾 3 句）恒
            # 跑偏。单句（即时路径）行为不变。
            texts = [str(x.get("text", "")).strip() for x in messages[-3:]]
            texts = [t for t in texts if t]
            if not texts:
                return EmotionResult(fallback, 0., True, self.version)
            encoded = self.tokenizer.encode_batch(texts)
            ids = np.asarray([x.ids for x in encoded], dtype=np.int64)
            mask = np.asarray([x.attention_mask for x in encoded], dtype=np.int64)
            types = np.asarray([x.type_ids for x in encoded], dtype=np.int64)
            emb = self.session.run(["embedding"], {"input_ids": ids, "attention_mask": mask,
                                                     "token_type_ids": types})[0]
            weight, bias, _ = self.classifier
            logits = emb @ weight + bias                     # (n, 5)
            logits -= logits.max(axis=1, keepdims=True)
            probs = np.exp(logits)
            probs /= probs.sum(axis=1, keepdims=True)
            probs = probs.mean(axis=0)                        # 逐句概率平均
            idx = int(np.argmax(probs)); confidence = float(probs[idx])
            if confidence < cut:
                return EmotionResult(fallback, confidence, True, self.version)
            return EmotionResult(LABELS[idx], confidence, False, self.version)
        if self.weights is None:
            return EmotionResult(fallback, 0., True, self.version)
        x = self._features(messages, float(now if now is not None else time.time()))
        if not x.any(): return EmotionResult(fallback, 0., True, self.version)
        w1, b1, w2, b2, w3, b3 = self.weights
        h1 = np.maximum(0., x @ w1 + b1); h2 = np.maximum(0., h1 @ w2 + b2)
        logits = h2 @ w3 + b3; logits -= logits.max()
        probs = np.exp(logits); probs /= probs.sum()
        idx = int(np.argmax(probs)); confidence = float(probs[idx])
        if confidence < cut:
            return EmotionResult(fallback, confidence, True, self.version)
        return EmotionResult(LABELS[idx], confidence, False, self.version)
