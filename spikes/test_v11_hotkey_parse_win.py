"""v0.10.18 H2 回归锁：win 热键键位解析（cmd 别名）——

config.example.json 的 hotkeys 是跨平台 mac 键位 "cmd+option+p/t"
（load_config 以 example 为基底深合并，win 用户不写该段就恒读到）。
旧版 parse_hotkey 的 _MOD_MAP 无 "cmd" → (0,0) → 两热键均判无效 →
HotkeyManager.start 返 False，v0.11 Must（唤聊天/吐出）win 开箱不可用。
本文件锁 cmd/command→ctrl、option→alt 别名与非法串行为。
运行：python spikes/test_v11_hotkey_parse_win.py（纯函数，无副作用）
"""

from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, ".")

from pet.hotkey_win import (  # noqa: E402
    MOD_ALT, MOD_CONTROL, MOD_SHIFT, MOD_WIN, parse_hotkey,
)

PASS, FAIL = [], []


def check(name, cond):
    (PASS if cond else FAIL).append(name)
    print(("  ✅ " if cond else "  ❌ ") + name)


def main() -> int:
    vk_p, vk_t, vk_a, vk_b, vk_1 = 0x50, 0x54, 0x41, 0x42, 0x31

    # ---- H2 核心：example 的 mac 键位在 win 解析为 Ctrl+Alt+P/T ----
    check("T1 cmd+option+p → ctrl+alt+p（H2）",
          parse_hotkey("cmd+option+p") == (MOD_CONTROL | MOD_ALT, vk_p))
    check("T2 cmd+option+t → ctrl+alt+t（H2）",
          parse_hotkey("cmd+option+t") == (MOD_CONTROL | MOD_ALT, vk_t))

    # ---- 常规格式 ----
    check("T3 ctrl+alt+t 原生格式",
          parse_hotkey("ctrl+alt+t") == (MOD_CONTROL | MOD_ALT, vk_t))
    check("T4 大小写/空白容忍",
          parse_hotkey("  Ctrl + ALT + T ") == (MOD_CONTROL | MOD_ALT, vk_t))
    check("T5 command 别名 = ctrl",
          parse_hotkey("command+a") == (MOD_CONTROL, vk_a))
    check("T6 option 别名 = alt",
          parse_hotkey("option+a") == (MOD_ALT, vk_a))
    check("T7 shift+数字键",
          parse_hotkey("ctrl+shift+1") == (MOD_CONTROL | MOD_SHIFT, vk_1))
    check("T8 win 修饰键",
          parse_hotkey("win+b") == (MOD_WIN, vk_b))

    # ---- 非法输入 ----
    check("T9 空串 → (0,0)", parse_hotkey("") == (0, 0))
    check("T10 只有修饰无主键 → (0,0)", parse_hotkey("ctrl+alt") == (0, 0))
    check("T11 未知 token → (0,0)", parse_hotkey("fn+a") == (0, 0))
    # 批次J/L6（REVIEW-2026-08-31）：裸字母（无修饰键）拒收——注册全局
    # 单键热键会挡住正常打字（win/mac 双端同守卫）
    check("T11b 裸字母（无修饰键）→ (0,0)", parse_hotkey("p") == (0, 0))
    from pet.hotkey_mac import parse_hotkey as mac_parse
    check("T11c mac 端裸字母同样拒收", mac_parse("p") == (0, 0))

    # ---- 批次J/L14（F21/F23）：config schema 扩面 + safe defaults 终检 ----
    import copy

    from pet.config import _SAFE_DEFAULTS, _SECTION_SCHEMAS, load_config

    # 新收段：非法值回退默认
    import tempfile
    bad_cfg = os.path.join(tempfile.gettempdir(), "dp_test_v11_cfg.json")
    with open(bad_cfg, "w", encoding="utf-8") as f:
        json.dump({"presentation": "paperdolll",   # 拼错
                   "log_level": "CHATTY",            # 非法枚举
                   "hotkeys": {"chat": 42}}, f)      # 类型错
    got = load_config(bad_cfg)
    check("T13a 非法 presentation 回退默认 frames",
          got["presentation"] == "frames")
    check("T13b 非法 log_level 回退默认 INFO", got["log_level"] == "INFO")
    check("T13c 非法 hotkeys 段回退默认（example 值）",
          isinstance(got["hotkeys"], dict)
          and isinstance(got["hotkeys"].get("chat"), str))
    # 合法值原样通过
    with open(bad_cfg, "w", encoding="utf-8") as f:
        json.dump({"presentation": "paperdoll", "log_level": "DEBUG"}, f)
    got2 = load_config(bad_cfg)
    check("T13d 合法新段原样通过",
          got2["presentation"] == "paperdoll"
          and got2["log_level"] == "DEBUG")
    os.remove(bad_cfg)
    # safe defaults 过同名 schema 终检（F21）
    import jsonschema as _js
    uncheckable = []
    for key, safe in _SAFE_DEFAULTS.items():
        schema = _SECTION_SCHEMAS.get(key)
        if schema is None:
            uncheckable.append(key)
            continue
        try:
            _js.Draft7Validator(schema).validate(copy.deepcopy(safe))
        except _js.ValidationError:
            uncheckable.append(key + "(校验失败)")
    check("T14 _SAFE_DEFAULTS 全部过同名 schema 终检", not uncheckable)

    # ---- 入库 example 实际值端到端（读文件，不跑线程） ----
    ex_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "..", "config.example.json")
    with open(ex_path, encoding="utf-8") as f:
        example = json.load(f)
    for k in ("chat", "spit"):
        hk = example.get("hotkeys", {}).get(k, "")
        check(f"T12 example hotkeys.{k}('{hk}') win 端可解析",
              parse_hotkey(hk) != (0, 0))

    print(f"\n热键解析: {len(PASS)} 通过, {len(FAIL)} 失败")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
