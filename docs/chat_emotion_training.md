# 聊天情绪模型训练

运行时不训练，也不会上传用户聊天。模型训练只在你准备好的 GPU 服务器上进行。

数据为 UTF-8 JSONL，每行格式：

```json
{"context": ["最近的一句或多句用户发言"], "label": "sad", "source": "public_or_synthetic"}
```

标签只允许 `happy`、`neutral`、`sad`、`sleepy`、`hungry`。先完成去标识、人工抽检、训练/验证拆分后，再运行：

```bash
python tools/train_chat_emotion.py data/chat_emotion.jsonl --out pet/models/chat_emotion_v1.npz
```

服务器须预装与 CUDA 匹配的 PyTorch。训练结束后复核 macro-F1、每类召回率和混淆矩阵，再将导出的模型带回应用；训练脚本不会自行下载数据或启动训练。
