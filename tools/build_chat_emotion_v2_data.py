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
    "happy": ["我很开心", "我好高兴", "今天心情特别好", "这件事让我很快乐", "我终于做到了", "刚刚收到好消息", "太棒了", "我被夸奖了", "虽然很累但我很开心", "我不难过，反而特别开心", "事情终于顺利解决了", "我今天过得真好"],
    "neutral": ["我在整理东西", "今天天气怎么样", "我刚吃完饭", "等会儿有个会议", "我在看资料", "知道啦", "先这样吧", "我去忙一会儿", "我不难过，刚才只是开玩笑", "我不饿了，已经吃过饭", "我不困，还想再看一会儿", "今天没什么特别的", "我不是不开心，只是很平静", "有点馋但并不饿", "我刚吃饱，现在不饿", "我只是嘴馋，不是真的饿", "我肚子一点也不饿", "我想喝水，不想吃东西", "晚饭刚吃完", "我吃得很饱"],
    "sad": ["我很难过", "我好伤心", "我有点失落", "心里空落落的", "这件事让我很挫败", "我想哭", "今天过得不太好", "我有些委屈", "我不开心", "我真的很失望", "事情搞砸了让我难受", "我难过得不想说话", "虽然努力了结果还是不好", "好一点了但我还是难过", "情绪缓和了一些但仍然失落", "虽然没那么难过了，心里还是不舒服"],
    "sleepy": ["我好困", "眼睛睁不开了", "我想睡觉", "今天太累了", "我要去休息", "困得不行", "我想打个盹", "我得早点睡", "我不是难过，只是太困了", "虽然开心但我现在想睡", "忙了一天该休息了", "我困得快睡着了"],
    "hungry": ["我好饿", "肚子在叫", "我想吃东西", "还没吃饭", "到饭点了", "我想吃点热乎的", "饿得不行", "我得去吃饭", "我不是馋，是真的饿", "我还没吃晚饭", "饿得没有力气了", "我想马上找点吃的"],
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


def variants(label: str) -> list[tuple[str, str]]:
    """(变体文本, 种子句) 列表——group=种子句供训练侧做变体族分组切分（H3）。"""
    out: dict[str, str] = {}
    for seed in SEEDS[label]:
        for prefix in PREFIX:
            for suffix in SUFFIX:
                out[f"{prefix}{seed}{suffix}"] = seed
    return sorted(out.items())


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
    # H2（REVIEW-2026-09-04）：验收文本先从采样池整体剔除——ACCEPTANCE 的
    # 部分句子逐字存在于 SEEDS，旧版无排除，"绝不混训"承诺结构性失效
    acceptance_texts = {text for text, _ in ACCEPTANCE}
    train = []
    for label in LABELS:
        pool = [(t, g) for t, g in variants(label) if t not in acceptance_texts]
        if args.per_label > len(pool):
            raise SystemExit(f"{label} 仅有 {len(pool)} 个去重模板；请先补充人工审核语料，不要复制凑数")
        chosen = rng.sample(pool, args.per_label)
        assert not ({t for t, _ in chosen} & acceptance_texts), "验收文本混入训练集"
        train.extend({"text": text, "label": label, "source": "curated_template",
                      "group": group} for text, group in chosen)
    rng.shuffle(train)
    acceptance = [{"text": text, "label": label, "source": "manual_acceptance"}
                  for text, label in ACCEPTANCE]
    write_jsonl(args.train_out, train); write_jsonl(args.acceptance_out, acceptance)
    print(f"train={len(train)} acceptance={len(acceptance)} "
          f"overlap={len({r['text'] for r in train} & acceptance_texts)}")


if __name__ == "__main__":
    main()
