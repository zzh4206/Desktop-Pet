"""pet/swe_memory.py —— mini-swe 本地任务记忆（v0.22.1）。

跨任务「本地上下文」三件套的存储/召回层（纯 Python，无 Qt/无 LLM）：

- **M1 任务档案**：每次任务结束把 ``{at, instruction, exit_status,
  submission, steps}`` 追加进工作区 ``.swe/history.jsonl``（上限
  ``history_cap`` 条，超限丢最老；坏行跳过不炸）。本地明文，用户可
  随时删——对齐 chat_emotion「隐私最小化档案」姿态。
- **M2 相似度召回注入**：新任务起跑前按 instruction 相似度取 top-k
  历史记录拼进任务 prompt 前缀（``[工作区近期任务记录]``），分词与
  IDF 加权**复用 pet.memory._tokenize 单一实现**（M9 教训：双份手工
  同步 = 静默 skew），打分 = IDF 重合度 ×0.7 + 近期 ×0.3（30 天窗）。
- **M3 NOTES.md**：system prompt 鼓励 agent 把持久发现写进工作区
  ``NOTES.md``（发现级记忆，agent 自管理）；任务开始注入其头部。

压缩治理（swe_context）与记忆（本模块）互补：前者管单任务内喂 LLM
的窗口，后者管跨任务「这活以前干过没有」。注入是**建议不是事实**——
旧记录可能过时，prompt 前缀明示「可能不相关」。
"""

from __future__ import annotations

import json
import logging
import os
import time

# 复用主聊天记忆的分词（中文 2-gram + 单字兜底 + 英数词）——单一实现，
# 避免 swe 侧自抄一份后两侧演进漂移。
from .memory import _tokenize

log = logging.getLogger("pet")

MEMORY_DEFAULTS = {
    "enabled": True,
    "recall_k": 5,
    "recall_chars": 200,    # 每条历史 submission 截断
    "history_cap": 200,     # 档案上限（超限丢最老）
    "notes": True,
    "notes_max_chars": 1000,
}

NOTES_FILENAME = "NOTES.md"
_ARCHIVE_SUBDIR = ".swe"
_ARCHIVE_FILENAME = "history.jsonl"
_RECENCY_WINDOW_S = 30 * 86400.0   # 近期分 30 天线性衰减


class TaskArchive:
    """工作区任务档案（jsonl）：append 留痕 + top-k 相似召回。"""

    def __init__(self, workspace: str, history_cap: int = 200) -> None:
        self.workspace = os.path.normpath(workspace or ".")
        self.path = os.path.join(self.workspace, _ARCHIVE_SUBDIR,
                                 _ARCHIVE_FILENAME)
        self.history_cap = max(1, int(history_cap))

    # ---- 存储（M1） ----

    def _load(self) -> list[dict]:
        records: list[dict] = []
        try:
            with open(self.path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        continue  # 坏行跳过（半写/手改），不炸召回
                    if isinstance(rec, dict) and rec.get("instruction"):
                        records.append(rec)
        except OSError:
            return []
        return records

    def append(self, instruction: str, exit_status: str, submission: str,
               steps: int = 0, at: float | None = None) -> dict:
        """任务结束留痕（原子整写：读-拼-裁-替换，档案量小无性能顾虑）。"""
        rec = {"at": float(at if at is not None else time.time()),
               "instruction": str(instruction or ""),
               "exit_status": str(exit_status or ""),
               "submission": str(submission or ""),
               "steps": int(steps or 0)}
        records = self._load() + [rec]
        if len(records) > self.history_cap:
            records = records[-self.history_cap:]
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            for r in records:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
        os.replace(tmp, self.path)
        log.info("[swe] 任务留痕: %s（%s，档案 %d 条）",
                 rec["instruction"][:24], rec["exit_status"], len(records))
        return rec

    def all(self) -> list[dict]:
        return self._load()

    # ---- 召回（M2） ----

    def recall(self, query: str, k: int = 5,
               now: float | None = None) -> list[dict]:
        """按 instruction 相似度取 top-k（IDF 重合 ×0.7 + 近期 ×0.3）。

        打分模式对齐 pet.memory.MemoryStore.recall（IDF 加权稀有词、
        高频词降权）；无重合词条的记录不进候选——零相关不如不注入。
        """
        q_tokens = _tokenize(query or "")
        records = self._load()
        if not q_tokens or not records:
            return []
        now = float(now if now is not None else time.time())

        docs, df = [], {}
        for r in records:
            toks = _tokenize(r.get("instruction", "") + " "
                             + r.get("submission", ""))
            docs.append(toks)
            for t in set(toks):
                df[t] = df.get(t, 0) + 1
        n_docs = max(1, len(docs))

        def _idf(t: str) -> float:
            # 平滑 IDF（BM25 风格恒正）：token 见于全部档案时朴素
            # log(N/df)=0 会把「两档案共有的全命中查询」打成零分。
            import math
            return math.log(1.0 + n_docs / (df.get(t, 0) + 0.5))

        q_set = set(q_tokens)
        scored = []
        for r, toks in zip(records, docs):
            common = q_set & set(toks)
            if not common:
                continue
            overlap = (sum(_idf(t) for t in common)
                       / max(0.001, sum(_idf(t) for t in q_set)))
            # 门槛：单字兜底 token（「个」「的」一类高频字）撞上一个词
            # 属噪声命中——IDF 归一重合度 <0.2 视为零相关不注入（零相关
            # 的旧记录注入只会误导，不如不注入）。
            if overlap < 0.2:
                continue
            recency = max(0.0, 1.0 - (now - float(r.get("at", 0)))
                          / _RECENCY_WINDOW_S)
            scored.append((overlap * 0.7 + recency * 0.3, r))
        scored.sort(key=lambda x: -x[0])
        return [r for _s, r in scored[:max(1, int(k))]]


# ---- 注入拼装（M2/M3 的呈现层） ----

def build_memory_prefix(records: list[dict], recall_chars: int = 200) -> str:
    """top-k 历史记录 → 任务 prompt 前缀（空列表 → 空串不注入）。"""
    if not records:
        return ""
    lines = ["[工作区近期任务记录]（本地记忆，供参考，可能过时或不相关）"]
    for i, r in enumerate(records, 1):
        instruction = str(r.get("instruction") or "")[:60]
        submission = str(r.get("submission") or "")
        if len(submission) > recall_chars:
            submission = submission[:recall_chars] + "…"
        status = r.get("exit_status") or "?"
        lines.append(f"{i}. [{status}] {instruction} → {submission}")
    return "\n".join(lines)


def read_notes_head(workspace: str, max_chars: int = 1000) -> str:
    """工作区 NOTES.md 头部（缺失/为空 → 空串；M3 发现级记忆注入）。"""
    path = os.path.join(os.path.normpath(workspace or "."), NOTES_FILENAME)
    try:
        with open(path, encoding="utf-8") as f:
            text = f.read(max_chars + 1).strip()
    except OSError:
        return ""
    if len(text) > max_chars:
        text = text[:max_chars] + "…"
    return text


def build_notes_prefix(workspace: str, max_chars: int = 1000) -> str:
    head = read_notes_head(workspace, max_chars)
    if not head:
        return ""
    return f"[工作区笔记 {NOTES_FILENAME} 头部]（agent 此前留下的持久发现）\n{head}"


def compose_task(instruction: str, workspace: str,
                 cfg: dict | None = None) -> str:
    """装配喂给 SweAgent.run 的任务文本：instruction + 记忆前缀。

    app 侧唯一入口（M2 召回 + M3 NOTES 注入都在这里收口）；归档仍存
    原始 instruction（前缀不回写档案，防记忆自噬膨胀）。
    """
    cfg = cfg or {}
    text = (instruction or "").strip()
    if not cfg.get("enabled", MEMORY_DEFAULTS["enabled"]):
        return text
    parts = []
    try:
        archive = TaskArchive(workspace,
                              cfg.get("history_cap",
                                      MEMORY_DEFAULTS["history_cap"]))
        records = archive.recall(
            instruction, k=int(cfg.get("recall_k",
                                       MEMORY_DEFAULTS["recall_k"])))
        parts.append(build_memory_prefix(
            records, int(cfg.get("recall_chars",
                                 MEMORY_DEFAULTS["recall_chars"]))))
    except Exception:
        log.warning("swe 任务记忆召回失败（跳过注入）", exc_info=True)
    if cfg.get("notes", MEMORY_DEFAULTS["notes"]):
        try:
            parts.append(build_notes_prefix(workspace, cfg.get(
                "notes_max_chars", MEMORY_DEFAULTS["notes_max_chars"])))
        except Exception:
            log.warning("swe NOTES.md 读取失败（跳过注入）", exc_info=True)
    tail = "\n\n".join(p for p in parts if p)
    if not tail:
        return text
    return text + "\n\n" + tail
