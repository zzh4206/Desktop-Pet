# Desktop Pet

一个运行在桌面上的 AI 宠物。它会在屏幕边缘散步、跟随鼠标、随互动成长，并可通过聊天面板和系统工具陪伴你的日常。

> 项目仍在开发中，当前以源码方式运行；尚未提供稳定的安装包或发布版本。

## 功能一览

- **养成与进化**：心情、饱食度和清洁度会随时间变化；摸摸、喂食、洗澡等互动会影响状态，宠物会随年龄成长。
- **桌面行为**：透明浮窗、随机游走、拖拽/抛掷、重力、鼠标跟随和全屏场景避让。
- **AI 聊天**：可接入 DeepSeek，支持流式聊天、长期记忆和定时回访。未配置 API Key 时，桌宠本体仍可正常使用。
- **主动关怀**：早晚问候、久坐提醒、节日祝福和聊天后的回访；支持安静时段配置。
- **系统集成**：系统托盘菜单、文件拖放打开、开机自启与全局快捷键（Windows）。部分系统操作会要求二次确认。
- **休息提醒**：在满足空闲、非勿扰、非视频播放等条件时，宠物可短暂“吃鼠标”提醒休息；可随时通过“强制吐出”或快捷键解除。

## 支持的平台

| 平台 | 状态 | 说明 |
| --- | --- | --- |
| macOS | 支持 | 部分功能（如“吃鼠标”）需要在系统设置中授予辅助功能权限。 |
| Windows | 支持 | 支持全局快捷键与注册表开机自启。 |
| Linux | 暂不支持 | 当前平台适配层仅实现 macOS 与 Windows。 |

## 快速开始

### 1. 准备环境

- Python 3.10 或更高版本
- macOS 或 Windows

克隆项目并创建虚拟环境：

```bash
git clone https://github.com/zzh4206/Desktop-Pet.git
cd Desktop-Pet
python -m venv .venv
```

激活虚拟环境：

```bash
# macOS
source .venv/bin/activate

# Windows PowerShell
.venv\Scripts\Activate.ps1
```

安装依赖并启动：

```bash
pip install -r requirements.txt
python app.py
```

开发时可使用详细日志：

```bash
python app.py --verbose
```

程序限制为单实例；再次启动时会尝试唤醒已有实例并退出。

## 使用方式

| 操作 | 效果 |
| --- | --- |
| 单击宠物 | 摸摸它，提升心情。 |
| 双击宠物 / 右键“喂食” | 喂食，提升饱食度。 |
| 右键菜单 | 洗澡、戳一戳、切换跟随鼠标、打开设置或退出。 |
| 拖动宠物 | 在桌面上移动它；松开后会按桌面物理规则行动。 |
| 拖放文件或文件夹到宠物 | 使用系统默认应用打开。 |
| 系统托盘 | 打开聊天、管理记忆、重新开始、切换开机自启、强制吐出或退出。 |
| `Ctrl+Alt+P`（Windows）/ `Cmd+Option+P`（macOS） | 显示或隐藏聊天面板。 |
| `Ctrl+Alt+T`（Windows）/ `Cmd+Option+T`（macOS） | 强制解除“吃鼠标”状态。 |

## 配置

首次启动会在以下位置创建配置目录。将仓库中的 [`config.example.json`](config.example.json) 复制为 `config.json` 后，可按需修改；未填写的字段会沿用默认值。

| 平台 | 配置文件 | 数据与日志 |
| --- | --- | --- |
| macOS | `~/.config/Desktop-Pet/config.json` | `~/Library/Application Support/Desktop-Pet` 与 `~/Library/Logs/Desktop-Pet` |
| Windows | `%LOCALAPPDATA%\Desktop-Pet\config.json` | `%LOCALAPPDATA%\Desktop-Pet` |

常用配置包括：

- `provider`：立绘来源，`emoji`（表情占位）或 `ai`（AI 立绘，缺图自动回退表情）。
- `presentation`：展示后端，`frames`（默认帧动画）、`rig`（v0.13 分层绑骨：交叉淡化、呼吸律动与部件弹簧）或 `paperdoll`（v0.14 部件驱动步态：侧身前后腿 + 正面双腿 limb 摆动）；需 `assets/rig/{stage}/` 资产，缺件自动回退 frames。
- `interaction_gain`：摸摸、喂食、洗澡和戳一戳的数值影响。
- `behavior`：行走和跟随速度、游走间隔、边缘距离等桌面行为参数。
- `proactive`：安静时段、久坐阈值、视频应用白名单及“吃鼠标”持续时间。
- `chat_emotion`：本地聊天情绪开关、每日兜底时段（默认 `22:00`）和短时表情时长（默认 5 分钟）。每次启动以 `neutral` 开始；每条用户消息仅在检测到高置信、非中性情绪时立即换表情，最多保留 5 分钟后恢复 `neutral`；22:00 没有明确情绪时显示困倦。仅在本机保留最近 48 小时的用户消息，可从托盘“聊天情绪设置”修改时段。
- **本地数据说明**：AI 长期记忆与聊天情绪上下文都只存本机（明文 JSON）——`memory.json` 存模型归纳的事实，`chat_emotion.json` 存最近 48 小时的用户消息文本（随开关即时生效，档案可随时删除）。
- `decay_per_hour`：心情、饱食度和清洁度的每小时衰减速度。

配置内容会经过校验；不合法的配置段会回退到默认值并记录日志。

## 启用 AI 聊天（可选）

默认配置提供 DeepSeek：

```json
{
  "llm": {
    "providers": {
      "deepseek": {
        "model": "deepseek-chat",
        "base_url": "https://api.deepseek.com",
        "api_key_env": "DEEPSEEK_API_KEY"
      }
    }
  }
}
```

可在启动前设置环境变量：

```bash
export DEEPSEEK_API_KEY="your-api-key"
python app.py
```

也可以在应用首次提示时输入 Key；程序会尝试保存到系统凭据库，而不是写入配置文件。请勿将 API Key 提交到仓库。

## 权限与安全

- macOS 上与鼠标控制相关的功能需要“辅助功能”权限；未授权时会降级而不会锁定鼠标。
- “吃鼠标”仅在用户空闲、非勿扰（勿扰为配置手动开关；会话中途开启会立即吐出）、非活跃视频内容、非全屏演示（v0.14.11 起含到达点复查）等条件满足时触发，单次时长受限，并提供快捷键、托盘菜单和自动超时释放作为退出路径。
- AI 工具调用分级：结束进程、系统睡眠、读写剪贴板、打开网址会先弹出确认框（拒绝或失败均默认不执行）；音量调节、文件搜索等无破坏性的操作不打扰。系统关键进程（explorer/dwm 等）硬拒绝。
- 文件搜索（file_search/mdfind）命中的文件名与路径会作为工具结果发送给所配置的 LLM 供其决策；拖放可执行/脚本文件给宠物打开时会先弹确认框。

## 开发与验证

项目将核心逻辑与平台实现分离：共享代码位于 `pet/`，平台差异集中在 `*_mac.py`、`*_win.py` 和 `platform.py`。`spikes/` 下保留了各阶段的验证脚本。

运行基础语法检查：

```bash
python -m compileall app.py pet
```

更多设计、版本规划和平台适配说明请参阅：[设计思路.md](设计思路.md)、[版本规划.md](版本规划.md) 与 [平台适配与分工.md](平台适配与分工.md)。

## 参与贡献

欢迎通过 Issue 或 Pull Request 参与改进。提交前请尽量保持 macOS 与 Windows 的适配层一致，并运行与改动模块相关的 `spikes/` 验证脚本。

## 许可证

本仓库当前未声明许可证。使用、复制或分发前，请先与仓库维护者确认授权范围。
