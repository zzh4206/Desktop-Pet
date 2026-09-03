#!/usr/bin/env python3
"""将 CPED 与公开可审计的模板合成为五类桌宠情绪 JSONL。

不会读取用户聊天，也不会调用网络或 LLM。输出每行：
{"context": ["..."], "label": "happy", "source": "cped"|"synthetic"}。
"""
from __future__ import annotations

import argparse
import csv
import json
import random
from collections import defaultdict
from pathlib import Path

LABELS = ("happy", "neutral", "sad", "sleepy", "hungry")
POSITIVE = {"happy", "happiness", "like", "surprise"}
NEGATIVE = {"sad", "sadness", "anger", "angry", "fear", "disgust"}

TEMPLATES = {
    "happy": (["今天收到一个好消息", "我终于把事情做完了", "刚刚被夸奖了", "考试/面试结果不错"],
              ["太好了", "我好开心", "想和你分享一下", "今天心情特别好"]),
    "neutral": (["今天天气怎么样", "我在整理桌面", "刚吃完午饭", "等会儿要开个会"],
                ["嗯", "知道啦", "就这样吧", "我先忙一会儿"]),
    "sad": (["今天努力了但结果不太好", "和朋友闹别扭了", "最近压力有点大", "事情没有按计划进行"],
            ["我有点难过", "感觉很挫败", "不太想说话", "心里空落落的"]),
    "sleepy": (["今天忙了一整天", "已经晚上了", "刚写完作业", "明天还要早起"],
               ["我好困", "眼睛都睁不开了", "想睡觉了", "先休息吧"]),
    "hungry": (["今天一直在赶事情", "刚下课/下班", "到饭点了", "早上没来得及吃"],
               ["我饿了", "想吃点热乎的", "还没吃饭", "肚子在叫"]),
}


def map_label(raw: str) -> str | None:
    raw = (raw or "").strip().lower()
    if raw in POSITIVE: return "happy"
    if raw == "neutral": return "neutral"
    if raw in NEGATIVE: return "sad"
    return None


def read_cped(root: Path) -> dict[str, list[dict]]:
    by_label: dict[str, list[dict]] = defaultdict(list)
    for split in ("train", "valid", "test"):
        with (root / f"{split}_split.csv").open(encoding="utf-8-sig", newline="") as f:
            dialogs: dict[str, list[dict]] = defaultdict(list)
            for row in csv.DictReader(f): dialogs[row["Dialogue_ID"]].append(row)
        for rows in dialogs.values():
            rows.sort(key=lambda r: r["Utterance_ID"])
            for i, row in enumerate(rows):
                label = map_label(row.get("Emotion", ""))
                text = (row.get("Utterance") or "").strip()
                if label and text:
                    context = [r["Utterance"].strip() for r in rows[max(0, i - 4):i + 1]
                               if (r.get("Utterance") or "").strip()]
                    if context: by_label[label].append(
                        {"context": context, "label": label, "source": "cped",
                         # M7（REVIEW-2026-09-04）：对话 id 供训练侧分组切分
                         "group": f"{split}:{row['Dialogue_ID']}"})
    return by_label


def synthetic(label: str, n: int, rng: random.Random, start_id: int) -> list[dict]:
    setup, reaction = TEMPLATES[label]; filler = ["今天过得怎么样", "我想和你说件事", "刚刚想到这个", "你在忙吗"]
    out = []
    for i in range(n):
        context = [rng.choice(filler), rng.choice(setup), rng.choice(reaction)]
        if rng.random() < .55: context.pop(0)
        out.append({"context": context, "label": label, "source": "synthetic",
                    "group": f"synth:{label}:{start_id + i}"})
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cped", required=True, type=Path, help="CPED data/CPED 目录")
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--per-label", type=int, default=2500)
    ap.add_argument("--seed", type=int, default=42)
    args = ap.parse_args(); rng = random.Random(args.seed)
    source = read_cped(args.cped); samples = []; synth_id = 0
    for label in LABELS:
        picked = list(source.get(label, [])); rng.shuffle(picked); picked = picked[:args.per_label]
        extra = synthetic(label, args.per_label - len(picked), rng, synth_id)
        synth_id += len(extra); picked += extra
        rng.shuffle(picked); samples.extend(picked)
        print(f"{label}: {len(picked)} (cped={sum(x['source']=='cped' for x in picked)})")
    rng.shuffle(samples); args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("w", encoding="utf-8") as f:
        for row in samples: f.write(json.dumps(row, ensure_ascii=False) + "\n")
    print(f"wrote {len(samples)} rows to {args.out}")

if __name__ == "__main__": main()
