#!/usr/bin/env python3
"""构建 v2 情绪训练集和不可混训的人工验收集。

输出行：{"text": "...", "label": "sad", "source": "curated_template"}。
本工具不读用户聊天、不联网；CPED 只可作为额外公开语料，由人工抽检后加入。
"""
from __future__ import annotations

import argparse
import json
import random
from pathlib import Path

LABELS = ("happy", "neutral", "sad", "sleepy", "hungry")

# 每组含正例、近义表达和与其他类别最易混淆的否定/转折表达。
SEEDS = {
    "happy": ["我很开心", "我好高兴", "今天心情特别好", "这件事让我很快乐", "我终于做到了", "刚刚收到好消息", "太棒了", "我被夸奖了"],
    "neutral": ["我在整理东西", "今天天气怎么样", "我刚吃完饭", "等会儿有个会议", "我在看资料", "知道啦", "先这样吧", "我去忙一会儿"],
    "sad": ["我很难过", "我好伤心", "我有点失落", "心里空落落的", "这件事让我很挫败", "我想哭", "今天过得不太好", "我有些委屈"],
    "sleepy": ["我好困", "眼睛睁不开了", "我想睡觉", "今天太累了", "我要去休息", "困得不行", "我想打个盹", "我得早点睡"],
    "hungry": ["我好饿", "肚子在叫", "我想吃东西", "还没吃饭", "到饭点了", "我想吃点热乎的", "饿得不行", "我得去吃饭"],
}

# 这些样例固定只写入 acceptance，绝不出现在训练集。每条应人工审核。
ACCEPTANCE = [
    ("我真的特别开心，今天的努力有结果了", "happy"), ("我不开心，但我会慢慢处理", "sad"),
    ("我很难过", "sad"), ("我不难过，刚才只是开玩笑", "neutral"),
    ("有点难过，不过现在好多了", "sad"), ("我好饿，想马上吃饭", "hungry"),
    ("我不饿了，刚吃完", "neutral"), ("我困得睁不开眼", "sleepy"),
    ("我不困，今晚还能再看一会儿", "neutral"), ("今天没什么特别的", "neutral"),
    ("考试通过了，我太高兴了", "happy"), ("事情搞砸了，我很失落", "sad"),
    ("开心是开心，不过我现在要睡了", "sleepy"), ("我不是饿，是有点馋", "neutral"),
    ("终于下班了，虽然累但很开心", "happy"),
]

PREFIX = ("", "我觉得", "说实话，", "刚刚", "今天", "现在", "唉，", "真的")
SUFFIX = ("", "。", "！", "，想和你说说", "，怎么办呀", "，不过没关系")


def variants(label: str) -> list[str]:
    out = set()
    for seed in SEEDS[label]:
        for prefix in PREFIX:
            for suffix in SUFFIX:
                out.add(f"{prefix}{seed}{suffix}")
    return sorted(out)


def write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--train-out", type=Path, required=True)
    ap.add_argument("--acceptance-out", type=Path, required=True)
    ap.add_argument("--per-label", type=int, default=300,
                    help="模板预训练样本数；正式训练须与人工审核公开语料合并至每类至少数千条")
    ap.add_argument("--seed", type=int, default=42)
    args = ap.parse_args(); rng = random.Random(args.seed)
    train = []
    for label in LABELS:
        pool = variants(label)
        if args.per_label > len(pool):
            raise SystemExit(f"{label} 仅有 {len(pool)} 个去重模板；请先补充人工审核语料，不要复制凑数")
        chosen = rng.sample(pool, args.per_label)
        train.extend({"text": text, "label": label, "source": "curated_template"} for text in chosen)
    rng.shuffle(train)
    acceptance = [{"text": text, "label": label, "source": "manual_acceptance"}
                  for text, label in ACCEPTANCE]
    write_jsonl(args.train_out, train); write_jsonl(args.acceptance_out, acceptance)
    print(f"train={len(train)} acceptance={len(acceptance)}")


if __name__ == "__main__":
    main()
