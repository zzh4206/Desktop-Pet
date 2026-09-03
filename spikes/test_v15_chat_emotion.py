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
    # 时段判定固定在当日 23:00——旧版直接用当前墙钟，23:00 前运行时
    # "09:00" <= current 恒假必挂（审查清偿执行中在午夜实跑暴露）
    _day = time.strftime("%Y-%m-%d", time.localtime())
    now_23 = time.mktime(time.strptime(_day + " 23:00", "%Y-%m-%d %H:%M"))
    store.mark_slot("22:00", now_23)
    check("每日时段只运行一次", store.due_slots(["09:00", "22:00"], now_23) == ["09:00"])
    store.set_current(result, now + 10)
    check("短时表情可读取", store.active_label(now) == "happy")
    check("短时表情过期清除", store.active_label(now + 11) is None)
    store.set_current(result, None)
    check("即时状态保持至下一条消息", store.active_label(now + 365 * 86400) == "happy")
    store.clear_current()
    check("可清除短时状态", store.active_label(now) is None)

    # ---- M3（REVIEW-2026-09-04）阈值覆写：0.528 置信介于 0.55 截断与 0.5 放行之间 ----
    msgs = store.recent_messages(now)
    mid = os.path.join(d, "mid.npz")
    np.savez(mid, feature_dim=FEATURE_DIM, labels=np.array(LABELS), model_version=1,
             w1=np.zeros((FEATURE_DIM, 128), np.float32), b1=np.ones(128, np.float32),
             w2=np.zeros((128, 64), np.float32), b2=np.ones(64, np.float32),
             w3=np.zeros((64, 5), np.float32), b3=np.array([1.5, 0, 0, 0, 0], np.float32))
    eng_mid = ChatEmotionEngine(mid, threshold=.55)
    r_cut = eng_mid.evaluate(msgs, "22:00", now)
    check("M3a 默认 0.55 截断为回退", r_cut.used_fallback and r_cut.label == "sleepy")
    r_evt = eng_mid.evaluate(msgs, "22:00", now, threshold=.5)
    check("M3b event 阈值 0.5 放行 0.528 置信",
          not r_evt.used_fallback and r_evt.label == "happy" and is_significant(r_evt, .5))

    # ---- L1（REVIEW-2026-09-04）v2 目录残缺 → 回退旁边完好的 v1 ----
    v1_path = os.path.join(d, "chat_emotion_v1.npz")
    np.savez(v1_path, feature_dim=FEATURE_DIM, labels=np.array(LABELS), model_version=1,
             w1=np.zeros((FEATURE_DIM, 128), np.float32), b1=np.ones(128, np.float32),
             w2=np.zeros((128, 64), np.float32), b2=np.ones(64, np.float32),
             w3=np.zeros((64, 5), np.float32), b3=np.array([5, 0, 0, 0, 0], np.float32))
    v2_bad = os.path.join(d, "chat_emotion_v2")
    os.makedirs(v2_bad, exist_ok=True)
    with open(os.path.join(v2_bad, "classifier.npz"), "w", encoding="utf-8") as f:
        f.write("not-an-npz")
    eng_fb = ChatEmotionEngine(v2_bad, threshold=.55)
    check("L1a v2 残缺回退 v1", eng_fb.version == 1 and eng_fb.weights is not None)
    lonely = os.path.join(d, "other", "lonely")
    os.makedirs(lonely, exist_ok=True)  # 父目录 other/ 无 v1，才构成"无可回退"场景
    with open(os.path.join(lonely, "classifier.npz"), "w", encoding="utf-8") as f:
        f.write("not-an-npz")
    eng_none = ChatEmotionEngine(lonely, threshold=.55)
    check("L1b 无 v1 可回退则降级时段兜底",
          eng_none.version is None and eng_none.evaluate(msgs, "22:00", now).used_fallback)

    # ---- M2（REVIEW-2026-09-04）禁用即时生效：enabled=False 后不采集/推理 ----
    import logging
    import types

    import app as app_mod

    calls: list = []
    fake_store = types.SimpleNamespace(
        add_user_message=lambda t: calls.append(("add", t)))
    fake = types.SimpleNamespace(
        memory=None,
        _chat_client=types.SimpleNamespace(set_memory_context=lambda seg: None),
        logger=logging.getLogger("t15"),
        _maybe_followup=lambda t: None,
        _chat_emotion_store=fake_store,
        _chat_emotion_engine=None,
        _chat_emotion_cfg={"enabled": False},
        _evaluate_message_emotion=lambda: calls.append(("eval",)),
    )
    app_mod.PetApp._on_user_message(fake, "记录我")
    check("M2a 禁用后不采集用户消息", ("add", "记录我") not in calls)
    fake._chat_emotion_cfg = {"enabled": True}
    app_mod.PetApp._on_user_message(fake, "记录我")
    check("M2b 启用时照常采集", ("add", "记录我") in calls)

    poll_store = types.SimpleNamespace(
        active_label=lambda now=None: None,
        recent_messages=lambda: msgs,
        due_slots=lambda sched, now=None: ["22:00"] if sched else [],
        set_current=lambda r, e: calls.append(("set_current", r.label)),
        mark_slot=lambda s, now=None: None,
    )
    eval_calls: list = []

    def _fake_eval(*a, **k):
        eval_calls.append(a)
        return EmotionResult("happy", .9, False, 1)

    fake_poll = types.SimpleNamespace(
        _chat_emotion_store=poll_store,
        _chat_emotion_engine=types.SimpleNamespace(evaluate=_fake_eval),
        _chat_emotion_cfg={"enabled": False, "schedule": ["22:00"]},
        _chat_emotion_active=None,
        _chat_emotion_duration_seconds=lambda: 300.0,
        _chat_emotion_expiry_timer=types.SimpleNamespace(start=lambda ms: None),
        _apply_chat_emotion=lambda label, conf=0.: calls.append(("apply", label)),
        window=types.SimpleNamespace(set_conversation_mood=lambda m: calls.append(("mood", m))),
        bubble=types.SimpleNamespace(show=lambda *a, **k: None),
    )
    app_mod.PetApp._poll_chat_emotion(fake_poll)
    check("M2c 禁用后轮询不推理", not eval_calls)
    fake_poll._chat_emotion_cfg = {"enabled": True, "schedule": ["22:00"]}
    app_mod.PetApp._poll_chat_emotion(fake_poll)
    check("M2d 启用后轮询照常推理", len(eval_calls) == 1 and ("apply", "happy") in calls)

    # ---- M9（REVIEW-2026-09-04）共享特征实现 + 训练窗口对齐 ----
    from pet.chat_emotion import ngram_vector

    v_a = ngram_vector(["abcabc"])
    v_b = ngram_vector(["abcabc", "abcabc"])
    check("M9a ngram_vector 归一化线性不变", np.allclose(v_a, v_b))
    check("M9b 权重参数等价叠加",
          np.allclose(ngram_vector(["abc"], [2.0]), ngram_vector(["abc", "abc"])))
    six = [{"text": f"消息内容编号{i}号"} for i in range(6)]
    check("M9c _features 截最近 5 条对齐训练窗口",
          np.allclose(eng_mid._features(six),
                      ngram_vector([m["text"] for m in six[1:]])))

print("聊天情绪检查完成")
