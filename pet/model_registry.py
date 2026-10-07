"""模型接入注册表 —— v0.20 模型管理（增/删/改/切换）的平台无关核心。

用户诉求：不同模型可经 URL + 自定义命名 + API Key 接入，且能改 Key、
增删模型、切换模型。落地为三块：

1. **config 落盘**（``llm.providers`` / ``llm.selected``）——raw 用户 JSON
   读-改-写 + 临时文件原子替换（对齐 app._show_chat_emotion_settings 的
   既有手法），**不整段重写**：用户 config 里其余键（behavior/proactive…）
   原样保留。
2. **条目校验**（``validate_entry``）——纯函数，管理对话框与单测共用；
   base_url 必须 http(s)://（防 schemaless 误拼），名称唯一。
3. **连通性测试**（``ping_provider``）——1 token 非流式 chat/completions，
   区分 401（Key 错）/404（URL 缺 /v1 之类）/网络失败，接入时即时反馈。

**平台库-free**：只 import json/os/re/requests，无 Qt/keyring——Key 读写经
注入（``key_reader``/``key_writer`` 闭包包 platform adapter 的 Keychain 存
取），单测不起 QApplication 也能跑（对齐 pet/llm.py 约定）。
"""

from __future__ import annotations

import json
import logging
import os
import re

import requests

log = logging.getLogger("pet")

# ping 用极小请求：max_tokens=1、双段短超时（接入测试别让用户干等）
_PING_TIMEOUT = (5, 20)
_PING_BODY = {"messages": [{"role": "user", "content": "hi"}],
              "max_tokens": 1, "stream": False}

_UNSET = object()  # save_llm_section 的"不改此项"哨兵（None=显式清除）


def validate_entry(name: str, base_url: str, model: str,
                   existing=(), self_name: str = "") -> str:
    """校验一条 provider 条目，合法返回空串，否则返回用户可读错误。

    ``existing`` 为已占用名称集合；``self_name`` 是"改名场景下自己用的旧名"
    （改名后旧名不算冲突）。
    """
    name = (name or "").strip()
    if not name:
        return "名称不能为空"
    if len(name) > 32:
        return "名称过长（最多 32 字符）"
    if "\n" in name or "\r" in name:
        return "名称不能含换行"
    others = [n for n in existing if n != self_name]
    if name in others:
        return f"名称“{name}”已存在，请换一个"
    base_url = (base_url or "").strip()
    if not re.match(r"^https?://\S+$", base_url):
        return "Base URL 需以 http:// 或 https:// 开头（如 https://api.example.com/v1）"
    if not (model or "").strip():
        return "模型 ID 不能为空（如 deepseek-chat / gpt-4o-mini）"
    return ""


def load_user_config(config_path: str) -> dict:
    """raw 用户 config（非 defaults 深合并产物）。缺失/非法返回 {}。"""
    if not os.path.exists(config_path):
        return {}
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            raw = json.load(f)
        return raw if isinstance(raw, dict) else {}
    except (OSError, ValueError) as e:
        log.warning("用户 config 读取失败（模型管理按空处理）: %s", e)
        return {}


def save_llm_section(config_path: str,
                     providers: "dict | None" = None,
                     selected=_UNSET) -> dict:
    """把 llm 段写回用户 config（原子替换），返回写入后的 llm 段。

    只动传入的键：``providers=None`` 不改，``selected`` 缺省（_UNSET）不改；
    ``selected=None``/空串显式清除。其余键原样保留。
    """
    raw = load_user_config(config_path)
    llm = raw.get("llm")
    if not isinstance(llm, dict):
        llm = {}
    if providers is not None:
        llm["providers"] = providers
    if selected is not _UNSET:
        if selected:
            llm["selected"] = selected
        else:
            llm.pop("selected", None)
    raw["llm"] = llm
    tmp = config_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(raw, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, config_path)
    return llm


def ping_provider(base_url: str, api_key: str, model: str,
                  timeout=tuple(_PING_TIMEOUT)) -> tuple[bool, str]:
    """连通性测试：1 token 非流式请求，返回 (ok, 用户可读结论)。

    只区分「端点+Key+模型可用」与常见失败形态（401 Key / 404 路径 / 超时 /
    断网），不做响应内容解析——接入验证够用且不依赖 provider 方言。
    """
    if not (api_key or "").strip():
        return False, "未设置 API Key，无法测试"
    try:
        resp = requests.post(
            f"{(base_url or '').rstrip('/')}/chat/completions",
            headers={"Authorization": f"Bearer {api_key}",
                     "Content-Type": "application/json"},
            json=dict(_PING_BODY, model=model),
            timeout=timeout,
        )
    except requests.Timeout:
        return False, "连接超时：URL 不通或服务无响应"
    except requests.ConnectionError as e:
        return False, f"连接失败：{e}"
    except Exception as e:  # requests 之外的意外（SSL 等）也不让 UI 崩
        return False, f"请求异常：{e}"
    if resp.status_code == 401:
        return False, "API Key 无效（401 未授权）"
    if resp.status_code == 404:
        return False, ("路径不存在（404）：多数 OpenAI 兼容端点 Base URL "
                       "需以 /v1 结尾，请检查")
    if resp.status_code >= 400:
        detail = ""
        try:
            detail = resp.text[:120]
        except Exception:
            pass
        return False, f"HTTP {resp.status_code}: {detail}"
    return True, "连接成功，模型可用"


def entry_label(name: str, pcfg: dict, selected: str = "",
                has_key: bool = False) -> str:
    """管理列表/托盘子菜单的展示行：名称 + 模型 + 使用中/密钥态。"""
    model = (pcfg or {}).get("model", "")
    mark = "✓ 使用中" if name == selected else ""
    key_mark = "密钥已存" if has_key else "未设密钥"
    line2 = " · ".join(x for x in (model, key_mark) if x)
    return f"{name}{('　' + mark) if mark else ''}\n{line2}"
