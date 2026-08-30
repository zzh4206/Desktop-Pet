#!/usr/bin/env python3
"""GPU 训练小型中文句向量编码器上的线性情绪分类头，并导出 CPU 资产。

运行前由用户在服务器准备 transformers、torch、onnx、onnxruntime；本脚本不下载
数据。默认编码器为 BAAI/bge-small-zh-v1.5，运行时部署量化 ONNX + tokenizer + npz 头。
"""
from __future__ import annotations

import argparse, json, sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from pet.chat_emotion import LABELS


def rows(path: Path) -> list[dict]:
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            row = json.loads(line)
            if row.get("label") in LABELS and isinstance(row.get("text"), str): out.append(row)
        except json.JSONDecodeError: continue
    return out


def metrics(y: np.ndarray, p: np.ndarray) -> dict:
    cm = np.zeros((len(LABELS), len(LABELS)), dtype=int)
    for truth, pred in zip(y, p): cm[truth, pred] += 1
    recalls = np.divide(np.diag(cm), np.maximum(1, cm.sum(1)))
    f1 = []
    for i in range(len(LABELS)):
        precision = cm[i, i] / max(1, cm[:, i].sum()); recall = recalls[i]
        f1.append(2 * precision * recall / max(1e-9, precision + recall))
    return {"macro_f1": float(np.mean(f1)), "recall": dict(zip(LABELS, recalls.tolist())),
            "confusion_matrix": cm.tolist()}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("train", type=Path); ap.add_argument("acceptance", type=Path)
    ap.add_argument("--out-dir", type=Path, default=Path("pet/models/chat_emotion_v2"))
    ap.add_argument("--encoder", default="BAAI/bge-small-zh-v1.5")
    ap.add_argument("--max-length", type=int, default=128); ap.add_argument("--epochs", type=int, default=80)
    ap.add_argument("--batch-size", type=int, default=64); ap.add_argument("--seed", type=int, default=42)
    args = ap.parse_args()
    try:
        import torch
        from torch import nn
        from transformers import AutoModel, AutoTokenizer
    except ImportError as exc: raise SystemExit("训练需要 torch 与 transformers") from exc
    train, acceptance = rows(args.train), rows(args.acceptance)
    if len(train) < 500 or len(acceptance) < 10: raise SystemExit("训练或独立验收集太小")
    if {r['label'] for r in train} != set(LABELS): raise SystemExit("训练集缺少类别")
    if {r['label'] for r in acceptance} != set(LABELS): raise SystemExit("验收集缺少类别")
    torch.manual_seed(args.seed); device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    if device.type != "cuda": raise SystemExit("拒绝在非 GPU 环境训练；请在用户指定服务器运行")
    tokenizer = AutoTokenizer.from_pretrained(args.encoder); encoder = AutoModel.from_pretrained(args.encoder).to(device).eval()
    def embed(items: list[dict]) -> np.ndarray:
        result = []
        with torch.inference_mode():
            for start in range(0, len(items), args.batch_size):
                batch = tokenizer([x['text'] for x in items[start:start + args.batch_size]], padding=True,
                                  truncation=True, max_length=args.max_length, return_tensors='pt').to(device)
                hidden = encoder(**batch).last_hidden_state; mask = batch['attention_mask'].unsqueeze(-1)
                pooled = (hidden * mask).sum(1) / mask.sum(1).clamp(min=1)
                result.append(torch.nn.functional.normalize(pooled, p=2, dim=1).cpu().numpy())
        return np.vstack(result).astype(np.float32)
    x = embed(train); xa = embed(acceptance)
    y = np.array([LABELS.index(r['label']) for r in train]); ya = np.array([LABELS.index(r['label']) for r in acceptance])
    head = nn.Linear(x.shape[1], len(LABELS)).to(device); opt = torch.optim.AdamW(head.parameters(), lr=2e-3, weight_decay=.01)
    loss_fn = nn.CrossEntropyLoss(); xt, yt = torch.from_numpy(x).to(device), torch.from_numpy(y).to(device)
    best = None; best_f1 = -1.
    for epoch in range(args.epochs):
        head.train(); order = torch.randperm(len(y), device=device)
        for ix in order.split(args.batch_size):
            opt.zero_grad(); loss_fn(head(xt[ix]), yt[ix]).backward(); opt.step()
        head.eval(); pred = head(torch.from_numpy(xa).to(device)).argmax(1).cpu().numpy(); report = metrics(ya, pred)
        print(f"epoch={epoch + 1} acceptance_macro_f1={report['macro_f1']:.4f}")
        if report['macro_f1'] > best_f1: best_f1, best = report['macro_f1'], {k: v.detach().cpu().clone() for k, v in head.state_dict().items()}
    head.load_state_dict(best); args.out_dir.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(args.out_dir / "classifier.npz", labels=np.array(LABELS), weight=head.weight.detach().cpu().numpy().T,
                        bias=head.bias.detach().cpu().numpy(), max_length=args.max_length, version=2)
    tokenizer.save_pretrained(args.out_dir / "tokenizer")
    # 导出编码器为跨平台 ONNX；量化失败不影响训练结果，但不允许把未量化资产当发布物。
    class EncoderForOnnx(nn.Module):
        def __init__(self, inner): super().__init__(); self.inner = inner
        def forward(self, input_ids, attention_mask, token_type_ids):
            hidden = self.inner(input_ids=input_ids, attention_mask=attention_mask,
                                token_type_ids=token_type_ids).last_hidden_state
            mask = attention_mask.unsqueeze(-1)
            pooled = (hidden * mask).sum(1) / mask.sum(1).clamp(min=1)
            return torch.nn.functional.normalize(pooled, p=2, dim=1)
    sample = tokenizer("情绪模型导出", return_tensors="pt")
    input_ids = sample["input_ids"].to(device); attention = sample["attention_mask"].to(device)
    type_ids = sample.get("token_type_ids", torch.zeros_like(input_ids)).to(device)
    onnx_path = args.out_dir / "encoder.onnx"
    torch.onnx.export(EncoderForOnnx(encoder).to(device).eval(), (input_ids, attention, type_ids), onnx_path,
                      input_names=["input_ids", "attention_mask", "token_type_ids"], output_names=["embedding"],
                      dynamic_axes={"input_ids": {0: "batch", 1: "sequence"}, "attention_mask": {0: "batch", 1: "sequence"},
                                    "token_type_ids": {0: "batch", 1: "sequence"}, "embedding": {0: "batch"}}, opset_version=17)
    try:
        from onnxruntime.quantization import QuantType, quantize_dynamic
        quantize_dynamic(str(onnx_path), str(args.out_dir / "encoder.int8.onnx"), weight_type=QuantType.QInt8)
        onnx_path.unlink()
    except Exception as exc:
        raise SystemExit(f"ONNX 量化失败，拒绝导出未量化部署模型：{exc}") from exc
    report = metrics(ya, head(torch.from_numpy(xa).to(device)).argmax(1).cpu().numpy())
    (args.out_dir / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False)); print(f"saved={args.out_dir}")


if __name__ == '__main__': main()
