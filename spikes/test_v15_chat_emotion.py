"""聊天情绪纯逻辑回归（不需 Qt、网络或 GPU）。"""
from __future__ import annotations

import os, sys, tempfile, time

import numpy as np

sys.path.insert(0, ".")

from pet.chat_emotion import (ChatEmotionEngine, ConversationEmotionStore,
                              EmotionResult, FEATURE_DIM, LABELS, is_significant,
                              obvious_emotion)


def check(name, ok):
    print(("PASS" if ok else "FAIL"), name)
    if not ok: raise AssertionError(name)


with tempfile.TemporaryDirectory() as d:
    path = os.path.join(d, "emotion.json")
    store = ConversationEmotionStore(path, retention_hours=48)
    now = time.time()
    store.add_user_message("今天真的很开心", now - 60)
    store.add_user_message("太久以前的内容", now - 49 * 3600)
    check("仅保留 48 小时内用户消息", len(store.recent_messages(now)) == 1)
    # 22:00 未加载模型时安全降级为 sleepy。
    missing = ChatEmotionEngine(os.path.join(d, "missing.npz"))
    check("22 点缺模型回退 sleepy", missing.evaluate([], "22:00", now).label == "sleepy")
    check("非夜间缺模型回退 neutral", missing.evaluate([], "09:00", now).label == "neutral")
    # 制造与导出契约一致的极小测试模型：bias 明确偏向 happy。
    model = os.path.join(d, "model.npz")
    np.savez(model, feature_dim=FEATURE_DIM, labels=np.array(LABELS), model_version=1,
             w1=np.zeros((FEATURE_DIM, 128), np.float32), b1=np.ones(128, np.float32),
             w2=np.zeros((128, 64), np.float32), b2=np.ones(64, np.float32),
             w3=np.zeros((64, 5), np.float32), b3=np.array([5, 0, 0, 0, 0], np.float32))
    engine = ChatEmotionEngine(model, threshold=.55)
    result = engine.evaluate(store.recent_messages(now), "22:00", now)
    check("高置信模型结果覆盖夜间默认", result.label == "happy" and not result.used_fallback)
    check("超过 0.5 的非中性消息立即触发", is_significant(result, .5))
    check("中性消息不立即触发", not is_significant(EmotionResult("neutral", .99), .5))
    check("明确难过词即时兜底", obvious_emotion("我好难过") == EmotionResult("sad", 1.0, False, 1))
    check("英文标签可作为测试输入", obvious_emotion("sad") == EmotionResult("sad", 1.0, False, 1))
    store.mark_slot("22:00", now)
    check("每日时段只运行一次", store.due_slots(["09:00", "22:00"], now) == ["09:00"])
    store.set_current(result, now + 10)
    check("短时表情可读取", store.active_label(now) == "happy")
    check("短时表情过期清除", store.active_label(now + 11) is None)
    store.set_current(result, None)
    check("即时状态保持至下一条消息", store.active_label(now + 365 * 86400) == "happy")
    store.clear_current()
    check("可清除短时状态", store.active_label(now) is None)

print("聊天情绪检查完成")
