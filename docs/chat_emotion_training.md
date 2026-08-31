# 聊天情绪 v2 训练与验收

v2 不再把字符 n-gram MLP 作为发布模型。它使用小型中文句向量编码器
`BAAI/bge-small-zh-v1.5` 与线性五分类头；GPU 只用于数据编码与训练，桌宠部署
量化 ONNX，在 macOS/Windows 的 CPU 上推理。

训练数据和验收数据必须是两个独立 JSONL 文件：

```json
{"text":"我好难过","label":"sad","source":"audited_public_or_manual"}
```

先用 `tools/build_chat_emotion_v2_data.py` 生成可审计的模板基线与验收集。模板基线
不能靠重复凑数：正式训练前，每类至少补足数千条去标识、人工抽检的公开或人工写作语料，
特别覆盖近义、程度、否定和转折。`spikes/fixtures/chat_emotion_acceptance.jsonl` 是独立
验收样例，严禁混入训练数据。

在用户指定的 GPU 服务器上安装匹配 CUDA 的 `torch`，并安装训练依赖：

```bash
pip install transformers onnx onnxruntime
python tools/train_chat_emotion_encoder.py data/train.jsonl \
  spikes/fixtures/chat_emotion_acceptance.jsonl \
  --out-dir pet/models/chat_emotion_v2
```

导出目录必须包含 `encoder.int8.onnx`、`tokenizer/`、`classifier.npz` 和 `report.json`。
验收门槛是：人工验收集中明显表达全部正确；再审查 macro-F1、每类召回率和混淆矩阵。
未满足门槛不得替换应用内模型。
