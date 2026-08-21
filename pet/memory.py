"""长期记忆（差异化③伴生）—— 接口冻结于 设计思路.md §2.2。

v0.9 实现（win 主笔）：
- **存储**：``memory.json``（同 pet_state.json 持久化模式：原子写+.bak+
  version migrate）。重置宠物**不**清记忆（"宠物长大了还记得你"）。
- **recall 打分**：本地 TF-IDF（中文 2-gram，无分词库）× 0.5 +
  importance × 0.3 + 近期召回加成 × 0.2——零向量库零重计算（红线）。
- **遗忘**：importance 按天衰减（×0.995^天），被 recall 命中回血 +0.05
  并刷新 last_recalled；衰减后 <0.1 物理删除（forget_expired）。
- **滚屏摘要**：``summarize_session`` 由 app 侧在超阈值时调（DS 压缩，
  本模块只存摘要行——摘要本身也是一条低权记忆？不：摘要由 chat 侧
  管理，本方法留接口占位记录会话要点）。

平台库-free；DS 工具（memory_save/memory_search）见 memory_tools.py。
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
import uuid

_log = logging.getLogger("pet")

_VERSION = 1
_DECAY_PER_DAY = 0.995      # 每天衰减系数
_FORGET_BELOW = 0.1         # 衰减至此物理删除
_RECALL_BOOST = 0.05        # 被召回回血
_MAX_MEMORIES = 500         # 上限（防无限膨胀）

_WORD = re.compile(r"[\w]+")


def _tokenize(text: str) -> list:
    """中文 2-gram + 英数词（无分词库依赖）。"""
    out = []
    for chunk in _WORD.findall(text or ""):
        if len(chunk) <= 2:
            out.append(chunk.lower())
            continue
        out.extend(chunk[i:i + 2].lower() for i in range(len(chunk) - 1))
        if len(chunk) > 2 and chunk.isascii():
            out.append(chunk.lower())
    return out


def _clamp_imp(v) -> float:
    try:
        return min(1.0, max(0.05, float(v)))
    except (TypeError, ValueError):
        return 0.5


class MemoryStore:
    """长期记忆（§2.2 冻结四方法 + load/save 持久化）。"""

    def __init__(self) -> None:
        self._mem: list[dict] = []   # [{id,fact,importance,created,
        #                            #   last_recalled,recall_count}]

    # ---- 持久化（同 pet_state 模式） ----

    @classmethod
    def load(cls, path: str) -> "MemoryStore":
        store = cls()
        for p in (path, path + ".bak"):
            try:
                with open(p, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data.get("memories"), list):
                    store._mem = [
                        m for m in data["memories"]
                        if isinstance(m, dict) and m.get("fact")
                    ]
                    _log.info("记忆载入 %d 条（%s）", len(store._mem), p)
                    return store
            except (OSError, json.JSONDecodeError):
                continue
        _log.info("无记忆档（%s），从空开始", path)
        return store

    def save(self, path: str) -> None:
        """原子写：.tmp → copy2 旧档→.bak → replace（pet_state 同序）。"""
        data = {"version": _VERSION, "memories": self._mem}
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
            f.flush()
            os.fsync(f.fileno())
        if os.path.exists(path):
            import shutil

            shutil.copy2(path, path + ".bak")
        os.replace(tmp, path)

    # ---- §2.2 冻结接口 ----

    def memorize(self, fact: str, importance: float) -> str:
        """存一条记忆（importance 钳 [0.05,1]）。返 id。同文去重。"""
        fact = (fact or "").strip()
        if not fact:
            return ""
        for m in self._mem:
            if m["fact"] == fact:
                m["importance"] = max(m["importance"],
                                      _clamp_imp(importance))
                return m["id"]
        if len(self._mem) >= _MAX_MEMORIES:
            self.forget_expired()          # 先清一轮
            if len(self._mem) >= _MAX_MEMORIES:
                self._mem.sort(key=lambda m: m["importance"])
                self._mem = self._mem[1:]  # 仍满：挤掉最不重要
        mid = "m_" + uuid.uuid4().hex[:10]
        self._mem.append({
            "id": mid, "fact": fact,
            "importance": _clamp_imp(importance),
            "created": time.time(),
            "last_recalled": time.time(),
            "recall_count": 0,
        })
        _log.info("[记忆] 存: %s(imp=%.2f)", fact[:40], importance)
        return mid

    def recall(self, query: str, k: int = 5) -> list:
        """按查询取 top-k 记忆（TF-IDF×0.5 + importance×0.3 + 近期×0.2）。

        命中项回血 +0.05 / recall_count+1 / 刷新 last_recalled。
        返回 [{id, fact, importance}]（按分降序）。
        """
        q_tokens = _tokenize(query)
        if not q_tokens or not self._mem:
            return []
        now = time.time()
        # 文档频率（IDF 分母）
        df = {}
        docs = []
        for m in self._mem:
            toks = _tokenize(m["fact"])
            docs.append(toks)
            for t in set(toks):
                df[t] = df.get(t, 0) + 1
        n_docs = max(1, len(docs))

        scored = []
        for m, toks in zip(self._mem, docs):
            overlap = sum(
                1 for t in set(q_tokens) if t in set(toks)
            ) / max(1, len(set(q_tokens)))
            recency = max(0.0, 1.0 - (now - m["last_recalled"]) / 86400.0)
            score = (overlap * 0.5 + m["importance"] * 0.3
                     + recency * 0.2)
            if overlap > 0:  # 只回词面相关的
                scored.append((score, m))
        scored.sort(key=lambda x: -x[0])
        hits = []
        for score, m in scored[:k]:
            m["recall_count"] += 1
            m["last_recalled"] = now
            m["importance"] = min(1.0, m["importance"] + _RECALL_BOOST)
            hits.append({"id": m["id"], "fact": m["fact"],
                         "importance": round(m["importance"], 3)})
        return hits

    def forget(self, mem_id: str) -> None:
        """按 id 删除（UI 用）。删后不再注入（recall 源即本列表）。"""
        before = len(self._mem)
        self._mem = [m for m in self._mem if m["id"] != mem_id]
        if len(self._mem) != before:
            _log.info("[记忆] 删: %s", mem_id)

    def clear(self) -> None:
        self._mem.clear()
        _log.info("[记忆] 清空")

    def forget_expired(self) -> int:
        """衰减+清理：importance 按天衰减，<0.1 物理删除。返删除数。"""
        now = time.time()
        kept, dropped = [], 0
        for m in self._mem:
            days = max(0.0, (now - m["last_recalled"]) / 86400.0)
            m["importance"] *= _DECAY_PER_DAY ** days
            if m["importance"] < _FORGET_BELOW:
                dropped += 1
            else:
                kept.append(m)
        self._mem = kept
        if dropped:
            _log.info("[记忆] 遗忘清理 %d 条", dropped)
        return dropped

    def summarize_session(self) -> None:
        """会话收尾钩子（app 在 shutdown/轮次切换调；v0.9 记会话要点
        的入口留给 chat 侧拼接后走 memorize——本方法保留冻结签名）。"""
        pass

    # ---- 非冻结辅助（UI/injection 用） ----

    def all(self) -> list:
        """全量（UI 列表用；按 importance 降序）。"""
        return sorted(self._mem, key=lambda m: -m["importance"])

    def __len__(self) -> int:
        return len(self._mem)
