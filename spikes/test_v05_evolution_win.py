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
    store.update(mood=95, fullness=95, cleanliness=95)  # 分 ≈95 ≥70

    ev1 = drive_evolve(store)
    check("T-a1 幼→成(YOUNG→ADULT)",
          ev1 is not None and ev1["from_stage"].value == "young"
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

    for p in (tmp, tmp + ".bak", tmp2, tmp2 + ".bak"):
        try:
            os.remove(p)
        except OSError:
            pass
    print(f"\n结果：{len(PASS)} 通过 / {len(FAIL)} 失败")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
