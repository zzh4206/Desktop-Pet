#!/usr/bin/env python3
"""在用户提供的 GPU 服务器上训练聊天情绪 MLP（本机不要直接运行）。

用法：python tools/train_chat_emotion.py data/chat_emotion.jsonl --out pet/models/chat_emotion_v1.npz
数据行：{"context": ["用户消息"], "label": "happy"}，标签见 pet.chat_emotion.LABELS。
需要额外安装 torch（带 CUDA 的版本由服务器环境决定），不属于桌宠运行时依赖。
"""
from __future__ import annotations

import argparse, json, math, sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from pet.chat_emotion import FEATURE_DIM, LABELS


def vectorize(texts: list[str]) -> np.ndarray:
    """与运行时相同的 2–4 gram 特征哈希；训练/推理必须保持同一实现。"""
    from hashlib import blake2b
    x = np.zeros(FEATURE_DIM, dtype=np.float32)
    for text in texts:
        text = "".join(str(text).split()).lower()
        for n in (2, 3, 4):
            for i in range(max(0, len(text) - n + 1)):
                idx = int.from_bytes(blake2b(text[i:i+n].encode(), digest_size=4).digest(), "little") % FEATURE_DIM
                x[idx] += 1
    norm = np.linalg.norm(x)
    return x / norm if norm else x


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("dataset", help="去标识 JSONL 数据集")
    ap.add_argument("--out", default="pet/models/chat_emotion_v1.npz")
    ap.add_argument("--epochs", type=int, default=40)
    ap.add_argument("--batch-size", type=int, default=64)
    ap.add_argument("--seed", type=int, default=42)
    args = ap.parse_args()
    try:
        import torch
        from torch import nn
    except ImportError as exc:
        raise SystemExit("训练需要 PyTorch；请在 GPU 服务器安装匹配 CUDA 的 torch") from exc

    rows = []
    for line in Path(args.dataset).read_text(encoding="utf-8").splitlines():
        try:
            row = json.loads(line)
            if row.get("label") in LABELS and isinstance(row.get("context"), list): rows.append(row)
        except json.JSONDecodeError: pass
    if len(rows) < 100: raise SystemExit("有效样本少于 100 条，拒绝训练")
    torch.manual_seed(args.seed); rng = np.random.default_rng(args.seed)
    rng.shuffle(rows); cut = max(1, int(len(rows) * .85)); train, valid = rows[:cut], rows[cut:]
    def pack(rows):
        return (torch.from_numpy(np.stack([vectorize(r["context"]) for r in rows])),
                torch.tensor([LABELS.index(r["label"]) for r in rows]))
    xtr, ytr = pack(train); xva, yva = pack(valid)
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    if device.type != "cuda": print("警告：未检测到 CUDA；请确认是否应在 GPU 服务器运行", file=sys.stderr)
    model = nn.Sequential(nn.Linear(FEATURE_DIM, 128), nn.ReLU(), nn.Dropout(.2),
                          nn.Linear(128, 64), nn.ReLU(), nn.Dropout(.2), nn.Linear(64, 5)).to(device)
    counts = torch.bincount(ytr, minlength=5).float()
    if (counts == 0).any():
        missing = [LABELS[i] for i, n in enumerate(counts.tolist()) if n == 0]
        raise SystemExit(f"训练集缺少类别：{', '.join(missing)}")
    weights = (counts.sum() / counts).to(device)
    opt = torch.optim.AdamW(model.parameters(), lr=1e-3); loss_fn = nn.CrossEntropyLoss(weight=weights)
    best, patience, best_state = -1., 0, None
    for epoch in range(args.epochs):
        model.train(); order = torch.randperm(len(ytr))
        for ix in order.split(args.batch_size):
            opt.zero_grad(); loss = loss_fn(model(xtr[ix].to(device)), ytr[ix].to(device)); loss.backward(); opt.step()
        model.eval()
        with torch.no_grad(): pred = model(xva.to(device)).argmax(1).cpu()
        f1 = sum((2 * ((pred == c) & (yva == c)).sum().item() /
                  max(1, (pred == c).sum().item() + (yva == c).sum().item())) for c in range(5)) / 5
        print(f"epoch={epoch + 1} val_macro_f1={f1:.4f}")
        if f1 > best: best, patience, best_state = f1, 0, {k: v.cpu().clone() for k, v in model.state_dict().items()}
        else: patience += 1
        if patience >= 6: break
    model.load_state_dict(best_state); layers = [m for m in model if isinstance(m, nn.Linear)]
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(args.out, feature_dim=FEATURE_DIM, labels=np.array(LABELS), model_version=1,
                        w1=layers[0].weight.detach().cpu().numpy().T, b1=layers[0].bias.detach().cpu().numpy(),
                        w2=layers[1].weight.detach().cpu().numpy().T, b2=layers[1].bias.detach().cpu().numpy(),
                        w3=layers[2].weight.detach().cpu().numpy().T, b3=layers[2].bias.detach().cpu().numpy())
    print(f"saved={args.out} best_macro_f1={best:.4f}")

if __name__ == "__main__": main()
