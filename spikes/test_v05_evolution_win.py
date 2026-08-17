"""v0.5 win 端 Must 实测（进化/分支/立绘/持久化/重置）—— mac v0.5.0 共享
实现的 win 适配验证。真实 PetStateStore（win 路径 %LOCALAPPDATA%）+
EmojiProvider，按 app._tick 同款驱动序列手动推进。
运行：python spikes/test_v05_evolution_win.py
"""

from __future__ import annotations

import os
import sys
import tempfile

sys.path.insert(0, ".")

from pet.asset_provider import EmojiProvider  # noqa: E402
from pet.pet_state import Branch, PetStateStore, Stage  # noqa: E402

PASS, FAIL = [], []


def check(name, cond):
    (PASS if cond else FAIL).append(name)
    print(("  ✅ " if cond else "  ❌ ") + name)


THR = {"young": 7, "adult": 21}
SCORE = {"mood_weight": 0.4, "fullness_weight": 0.4,
         "cleanliness_weight": 0.2, "healthy_threshold": 70}


def drive_evolve(store):
    """按 app 序列推进：回拨 last_update 模拟 130s 流逝（fast 5040× 下
    ≈7.6 天，真机同配置 2 分钟即幼年到期）→ apply_decay → check_evolve。"""
    store._last_update -= 130
    store.apply_decay({"mood": 2, "fullness": 3, "cleanliness": 1.5},
                      age_speed_multiplier=5040)
    return store.check_evolve(THR, SCORE)


def main() -> int:
    tmp = os.path.join(tempfile.gettempdir(), "dp_test_v05.json")
    for suffix in ("", ".bak"):
        try:
            os.remove(tmp + suffix)
        except OSError:
            pass

    provider = EmojiProvider()
    emojis = []  # on_change 观察 emoji 切换

    # ---- 高养护 → HEALTHY 链 ----
    store = PetStateStore.load(tmp)
    store.on_change(lambda s: emojis.append(provider.get_static(s).path))
    store.update(mood=95, fullness=95, cleanliness=95)  # 增量语义 80+95→clamp 100,分≈100≥70

    ev1 = drive_evolve(store)
    check("T-a1 幼→成(YOUNG→ADULT)",
          ev1 is not None and ev1["from_stage"] == "young"
          and store.get().stage == Stage.ADULT)
    check("T-a1 高分判 HEALTHY", store.get().branch == Branch.HEALTHY)
    store._last_update -= 400  # 再模拟 ~23 天过成年阈值 21
    ev2 = drive_evolve(store)
    check("T-a2 成→终(ADULT→FINAL)",
          ev2 is not None and store.get().stage == Stage.FINAL)
    check("T-a3 FINAL 不再进化", drive_evolve(store) is None)

    # ---- 持久化：进化/分支重启不丢 ----
    store.save(tmp)
    store2 = PetStateStore.load(tmp)
    check("T-b 重启保阶段+分支",
          store2.get().stage == Stage.FINAL
          and store2.get().branch == Branch.HEALTHY)

    # ---- 低养护 → NEGLECTED ----
    tmp2 = tmp + "2"
    store3 = PetStateStore.load(tmp2)
    store3.update(mood=-75, fullness=-75, cleanliness=-75)  # 增量语义→≈5 分<70
    ev = drive_evolve(store3)
    check("T-c 低分判 NEGLECTED",
          ev is not None and store3.get().branch == Branch.NEGLECTED)

    # ---- (stage, branch) 出不同 emoji ----
    e_h_final = provider.get_static(store2.get()).path
    e_n_adult = provider.get_static(store3.get()).path
    from pet.pet_state import PetState
    e_h_young = provider.get_static(PetState()).path
    check("T-d 不同(stage,branch)emoji不同",
          len({e_h_young, e_h_final, e_n_adult}) == 3)

    # ---- 重置：清档 → 新宠物 ----
    for p in (tmp, tmp + ".bak", tmp2, tmp2 + ".bak"):
        try:
            os.remove(p)
        except OSError:
            pass
    store4 = PetStateStore.load(tmp)  # 无档(含.bak) → default
    check("T-e 清档后回 YOUNG/HEALTHY/age0",
          store4.get().stage == Stage.YOUNG
          and store4.get().age == 0.0)

    # ---- on_change 驱动过 emoji 切换（app 订阅链路在 win 可用） ----
    check("T-f on_change emoji 随进化切换(≥2种)",
          len(set(emojis)) >= 2)

    # ---- M2 离线多阶进化用时间平均分判分支（非瞬时衰减到底全 NEGLECTED）----
    # 模拟：高养护 pet，离线 30 天（fast 5040×）→ apply_decay 推过 young+adult
    # 两阈值，check_evolve 应连进两阶且分支用离线前高养护分判 HEALTHY（非衰减后
    # 底值判 NEGLECTED）。app._apply_decay 循环 check_evolve 补齐多阶。
    tmp3 = tmp + "3"
    for suffix in ("", ".bak"):
        try:
            os.remove(tmp3 + suffix)
        except OSError:
            pass
    store_m = PetStateStore.load(tmp3)
    store_m.update(mood=95, fullness=95, cleanliness=95)  # 高养护
    pre = store_m.get()
    pre_score = 0.4 * pre.mood + 0.4 * pre.fullness + 0.2 * pre.cleanliness
    # 离线 30 天（fast 5040×：30天*5040=151200 宠物天 >> 21 天 adult 阈值）
    store_m._last_update -= 30 * 86400
    store_m.apply_decay({"mood": 2, "fullness": 3, "cleanliness": 1.5},
                        age_speed_multiplier=5040)
    # 循环 check_evolve 补齐多阶（模拟 app._apply_decay）
    events = []
    while True:
        ev = store_m.check_evolve(THR, SCORE, avg_score=pre_score)
        if ev is None:
            break
        events.append(ev)
    check("M2 离线多阶进化连进两阶(YOUNG→ADULT→FINAL)",
          len(events) == 2 and events[0]["to_stage"] == "adult"
          and events[1]["to_stage"] == "final")
    check("M2 离线高养护用平均分判 HEALTHY(非衰减底值判NEGLECTED)",
          store_m.get().branch == Branch.HEALTHY)

    # ---- 健壮性：clamp / off_change / None 防 / 阈值缺省 inf ----
    # _state_from_dict clamp mood=150 → 100
    import json as _json
    bad = os.path.join(tempfile.gettempdir(), "dp_test_v05_bad.json")
    with open(bad, "w") as f:
        _json.dump({"version": 1, "state": {
            "mood": 150, "fullness": 80, "cleanliness": 80, "age": 0,
            "stage": "young", "branch": "healthy"}}, f)
    s_bad = PetStateStore.load(bad)
    check("健壮 _state_from_dict clamp mood=150→100",
          s_bad.get().mood == 100.0)
    os.remove(bad)

    # off_change：取消订阅后不再触发
    s_off = PetStateStore(PetState.default())
    hits = [0]
    def cb(s):
        hits[0] += 1
    s_off.on_change(cb)
    s_off.update(mood=10)
    s_off.off_change(cb)
    s_off.update(mood=10)
    check("健壮 off_change 取消后不再触发", hits[0] == 1)

    # check_evolve(None, {}) 不崩 + 阈值缺省 inf 不立即进化
    s_none = PetStateStore(PetState.default())
    s_none._last_update -= 100
    s_none.apply_decay({"mood": 2}, age_speed_multiplier=1)
    check("健壮 check_evolve(None,{})不崩",
          s_none.check_evolve(None, None) is None)
    # age 小但阈值缺省 inf → 不进化
    s_thr = PetStateStore(PetState.default())
    check("健壮 阈值缺省inf不立即进化(age=0)",
          s_thr.check_evolve({}, {}) is None)

    # check_evolve 返回值可 json.dumps（.value 字符串，非 Enum）
    import json as _json2
    s_json = PetStateStore(PetState.default())
    s_json._last_update -= 200  # 200s * 5040/86400 ≈ 11.7 天 > young 阈值 7
    s_json.apply_decay({"mood": 2}, age_speed_multiplier=5040)
    ev_json = s_json.check_evolve(THR, SCORE)
    check("健壮 check_evolve返回值可json.dumps",
          ev_json is not None and _json2.dumps(ev_json) is not None)

    for p in (tmp, tmp + ".bak", tmp2, tmp2 + ".bak",
              tmp3, tmp3 + ".bak"):
        try:
            os.remove(p)
        except OSError:
            pass
    print(f"\n结果：{len(PASS)} 通过 / {len(FAIL)} 失败")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
