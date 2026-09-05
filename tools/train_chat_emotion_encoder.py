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
    # 批次C/P3-18（REVIEW-2026-09-05）：训练侧复验不相交——"绝不混训"的
    # 结构性保证不再只依赖构建侧 assert（误喂预修复旧档/合并档时静默恢复
    # 泄漏，acceptance 门禁被高估）
    _overlap = {r["text"] for r in train} & {r["text"] for r in acceptance}
    if _overlap:
        raise SystemExit(
            f"训练/验收集文本相交 {len(_overlap)} 条（严禁混训），"
            f"示例: {sorted(_overlap)[:3]}")
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
    # H3（REVIEW-2026-09-04）：按变体族（group=种子句）切分——同一 seed 的
    # 前后缀变体近重复，行级随机切分让变体族跨 train/valid，validation F1
    # 虚高且 best checkpoint 被泄漏指标驱动（0.992 vs acceptance 0.911 的
    # 差值即泄漏量）。旧数据行无 group 字段时按文本分组（等价单行组）。
    rng = np.random.default_rng(args.seed); train_ix, valid_ix = [], []
    for label in range(len(LABELS)):
        indices = np.flatnonzero(y == label)
        by_group: dict[str, list[int]] = {}
        for ix in indices:
            row = train[int(ix)]
            by_group.setdefault(str(row.get("group") or row["text"]), []).append(int(ix))
        keys = sorted(by_group); rng.shuffle(keys)
        cut = max(1, int(len(keys) * .85))
        for k in keys[:cut]: train_ix.extend(by_group[k])
        for k in keys[cut:]: valid_ix.extend(by_group[k])
    if not valid_ix: raise SystemExit("分组切分后验证集为空；请检查数据 group 字段")
    train_ix, valid_ix = np.asarray(train_ix), np.asarray(valid_ix)
    head = nn.Linear(x.shape[1], len(LABELS)).to(device); opt = torch.optim.AdamW(head.parameters(), lr=2e-3, weight_decay=.01)
    loss_fn = nn.CrossEntropyLoss(); xt, yt = torch.from_numpy(x[train_ix]).to(device), torch.from_numpy(y[train_ix]).to(device)
    xv, yv = torch.from_numpy(x[valid_ix]).to(device), y[valid_ix]
    best = None; best_f1 = -1.
    for epoch in range(args.epochs):
        head.train(); order = torch.randperm(len(yt), device=device)
        for ix in order.split(args.batch_size):
            opt.zero_grad(); loss_fn(head(xt[ix]), yt[ix]).backward(); opt.step()
        head.eval(); pred = head(xv).argmax(1).cpu().numpy(); report = metrics(yv, pred)
        print(f"epoch={epoch + 1} validation_macro_f1={report['macro_f1']:.4f}")
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
                                    "token_type_ids": {0: "batch", 1: "sequence"}, "embedding": {0: "batch"}},
                      opset_version=17, dynamo=False)
    try:
        from onnxruntime.quantization import QuantType, quantize_dynamic
        quantize_dynamic(str(onnx_path), str(args.out_dir / "encoder.int8.onnx"), weight_type=QuantType.QInt8)
        onnx_path.unlink()
    except Exception as exc:
        onnx_path.unlink(missing_ok=True)  # L20：不留 fp32 中间物被误当发布物打包
        raise SystemExit(f"ONNX 量化失败，拒绝导出未量化部署模型：{exc}") from exc
    # M8（REVIEW-2026-09-04）：部署物 int8 ONNX 参与评估——旧版 report 指标
    # 全部算在 fp32 embedding 上，运行时真正加载的量化模型误差无任何门禁
    import onnxruntime as _ort
    sess = _ort.InferenceSession(str(args.out_dir / "encoder.int8.onnx"),
                                 providers=["CPUExecutionProvider"])
    emb_a8: list = []
    for start in range(0, len(acceptance), args.batch_size):
        batch = tokenizer([r['text'] for r in acceptance[start:start + args.batch_size]],
                          padding=True, truncation=True, max_length=args.max_length,
                          return_tensors="np")
        ids = np.asarray(batch["input_ids"], dtype=np.int64)
        mask = np.asarray(batch["attention_mask"], dtype=np.int64)
        types = (np.asarray(batch["token_type_ids"], dtype=np.int64)
                 if "token_type_ids" in batch else np.zeros_like(ids))
        emb_a8.append(sess.run(["embedding"], {"input_ids": ids, "attention_mask": mask,
                                               "token_type_ids": types})[0])
    logits8 = (np.vstack(emb_a8).astype(np.float32) @ head.weight.detach().cpu().numpy().T
               + head.bias.detach().cpu().numpy())
    report = {"validation": metrics(yv, head(xv).argmax(1).cpu().numpy()),
              "acceptance": metrics(ya, head(torch.from_numpy(xa).to(device)).argmax(1).cpu().numpy()),
              "acceptance_int8": metrics(ya, logits8.argmax(1))}
    drop = report["acceptance"]["macro_f1"] - report["acceptance_int8"]["macro_f1"]
    if drop > 0.05:
        raise SystemExit(f"int8 量化劣化超阈值：acceptance macro_f1 下降 {drop:.4f} > 0.05")
    (args.out_dir / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False)); print(f"saved={args.out_dir} int8_drop={drop:.4f}")


if __name__ == '__main__': main()
