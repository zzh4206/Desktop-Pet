# 罐头语音音包资产（v0.26.0 语音线）

> 设计依据：`wiki/设计-语音模块技术方案.md` §2.1b；加载实现：`pet/voice.py`
> 的 `VoiceAssetLoader`。语音总开关默认**关**（config `voice.tts.enabled`），
> 与音效线（`assets/sounds/`）同哲学：缺件静默降级、随时可补、不算破坏性
> 变更。

## 目录约定与后缀探测

```
assets/voice/<key>.<ext>
```

`<key>` 取下方「key 对照表」中的语义键名。同一个 key 允许只放**一个**文件，
扩展名从白名单里任选；`VoiceAssetLoader` 对每个 key 按下列**优先级顺序**
探测，取第一个存在的文件：

| 优先级 | 后缀 | 说明 |
|---|---|---|
| 1 | `.wav` | 首选（无损、体积可控） |
| 2 | `.aiff` | mac 原生无损 |
| 3 | `.m4a` | AAC，QuickTime/语音备忘录默认导出 |
| 4 | `.mp3` | 兼容老素材 |
| 5 | `.caf` | Core Audio Format |

**只用 mac 系统原生格式**（用户拍板 2026-10-08）：不引入 QtMultimedia
后端与转码依赖，故 `.ogg` / `.flac` **不在白名单**（如需再议为增量）。
换格式时**只换文件后缀，代码零改动**。

## key 对照表

| `<key>` | 触发场景 | 代码出处 |
|---|---|---|
| `greet_morning` | 早安（08:00–11:00 窗口，每天一次） | `pet/proactive.py:829` |
| `greet_night` | 晚安（21:00–23:00 窗口，每天一次） | `pet/proactive.py:835` |
| `greet_birthday` | 生日祝福（生日当天一次） | `pet/proactive.py:846` |
| `greet_festival` | 节日祝福（当天一次；日历源回落内置表） | `pet/proactive.py:863` |
| `reminder_sit` | 久坐提醒（轮换话题：喝水休息 / 站起来活动 / 吃点东西） | `pet/proactive.py:47`、`:803` |
| `eat_mouse` | 吃鼠标（强制休息）真正生效时的提示 | `pet/proactive.py:542` |
| `switch_model` | 切换当前模型成功 | `app.py:1140` |
| `evolve` | 进化跨阶段（含离线补阶的"我长大了"） | `app.py:1974` |
| `reset_age` | 重置年龄 / 重新出生 | `app.py:2000` |
| `pet_wake` | 启动致意「我醒啦～」 | `app.py:472` |
| `llm_no_key` | 未配置 API key（聊天暂不可用） | `app.py:851`、`app.py:1785` |

key 集合是 `pet/voice.VOICE_KEYS` 元组，**可加项**：新增台词只需追加 key +
补本表一行 + 放同名文件，接线方显式传 `key=` 即生效。
（池化台词（如聊天情绪、需求求助的随机池）暂未纳入——一句台词对应多份
文案会让单份音包语义失真，待有明确录音计划再单独登记。）

## 占位音频（当前状态，重要）

**本目录只随仓库提供本 README，不含任何音频文件。** 占位期不引入任何
第三方语音，也**绝不使用任何他人录制的音色**（用户拍板 2026-10-08）：
占位音由程序现场生成——`pet/voice.write_placeholder_wav` 产出
0.35s / 660Hz 正弦短提示音（16bit 单声道 44.1kHz，带淡入淡出防爆音）。
需要占位音时执行：

```bash
cd <仓库根>
.venv/bin/python -c "from pet.voice import ensure_placeholders; print(ensure_placeholders('assets/voice'))"
```

`ensure_placeholders` 只为**缺失**的 key 生成 `<key>.wav`，已存在（任一合法
后缀）的文件绝不覆盖——所以你可以随时用自己录制的正式音包替换，反复执行
该命令也不会被占位音盖掉。

## 录制与替换

用户自行录制后，把文件命名为 `<key>.<ext>`（`<ext>` 取上表白名单任一后缀）
放进本目录即生效，**代码零改动**；不需要删除同名占位 `.wav`——探测优先级
里 `.wav` 最高，若你的正式素材是 `.m4a` 而占位 `.wav` 还在，会命中占位音，
此时删掉那份占位 `.wav` 即可。

建议录音规格（对齐 `assets/sounds/README.md` 的 44.1kHz/单声道）：

- 采样率 44.1kHz、单声道、16bit（wav 时）；导出响度按正常说话电平即可，
  整体音量后续由 config 统一控制
- 单条时长 0.3–3.0s（罐头语音是短句，不是配音）
- 录音前后留 ~50ms 静音，避免首尾被系统截断爆音
- 只用 mac 原生格式（`.wav` / `.aiff` / `.m4a` / `.mp3` / `.caf`）

## 缺失时的行为（静默降级）

某 key **全部后缀都不存在**，或对应播放器注入位未接（win 留坑）时：
回落 `voice.tts.provider` 动态合成（系统 `say` 等）朗读原文；两者都不可用
则静默跳过——只记 debug 日志，永不外抛、永不阻断气泡与主循环。
播放门禁（启用开关 / 来源开关 / DND / 全屏 / 深夜静默 / 3s 文本去重）由
`pet/voice.SpeakGate` 统一把守，对罐头音包与动态 TTS 一视同仁。
