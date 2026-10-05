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

配置内容会经过校验；不合法的配置段会回退到默认值并记录日志。`config_version` 供配置迁移使用，无需手动修改；未在下方列出的字段以 [`config.example.json`](config.example.json) 中的默认值为准。

### 外观与立绘

- `provider`：立绘来源，`emoji`（表情占位）、`ai`（AI 立绘，缺图自动回退表情）或 `commission`（约稿立绘，按与 `ai` 相同的命名约定读取 `assets/` 资产）。
- `presentation`：展示后端，`rig`（默认，v0.13 分层绑骨：交叉淡化、呼吸律动与部件弹簧；侧身行走开箱即用）、`frames`（帧动画；低配机器或在意内存占用时的回退项）或 `paperdoll`（v0.14 部件驱动步态：侧身前后腿 + 正面双腿 limb 摆动）。rig/paperdoll 需 `assets/rig/{stage}/` 资产，缺件自动回退 frames。
- `adult_locomotion`：ADULT 在 `presentation: "rig"` 下默认使用 `side_rig`（转身、侧身行走、依次收脚）；设为 `legacy` 可回退正面步态，侧身资产缺失时也会自动回退。
  侧身行走期间，各情绪暂用同一中性骨骼体态；neglected 保持灰暗配色，完整行走会话转回正面后恢复最新表情。拖拽、离地及动作帧会中断会话并恢复表情。
  当前侧身资产包含膝部曲线过渡、裙摆惯性与尾鳍刚性约束；普通转身轻微提速，反向转身使用更快的片段播放，仍先完成收脚。
- `final_locomotion`：FINAL 默认使用 `side_rig`，接入正面归位、转身片段、长裙侧身行走、停步收脚和转回正面的完整流程。设为 `legacy` 可回退，独立于 ADULT 配置；旧配置缺少此项时自动采用默认值。
  行走期间十种情绪共用中性骨骼，neglected 使用去饱和配色；会话结束或拖拽、离地、动作帧中断后恢复最新表情。阶段切换会清理旧侧身会话并装配当前阶段资产。

### 3D 呈现（`render3d`，实验线）

默认关闭（**2D 立绘呈现**）。3D 是并行开发的实验轨，开启后桌宠以 3D 模型在独立透明窗呈现，行走/拖拽/点击与昼夜光照可用。**本地测试 3D**：把 `render3d.enabled` 手动改为 `true` 即可（需先准备运行时资产，见本节末尾）；不改配置则一切保持 2D。

| 键 | 默认值 | 范围 | 说明 |
| --- | --- | --- | --- |
| `enabled` | `false` | 布尔 | 3D 呈现开关；默认 `false` = 2D。 |
| `stage` | `adult` | `young` / `adult` / `final` | 3D 资产档位（对应 `three_d_assets/models/<stage>/`）。 |
| `light_level` | `1` | 0 – 3 | 光照分级：0 无光照、1 toon 明暗（当前默认档）、2/3 预留高档。 |
| `fps_cap` | `30` | 5 – 120 | 渲染帧率上限；静止时自动停帧，不空转。 |
| `safe_rss_mb` | `500` | 100 – 5000 | 内存看门狗安全上限（增量 MB）：3D 装载后内存增量超限自动静默降级回 2D，桌宠不会因 3D 异常崩溃或消失。 |

资产缺失、初始化失败或运行超内存时**自动降级回 2D**，无需回滚配置即可继续使用。3D 运行时资产不入库，需用工具生成：`three_d/tools/make_runtime_asset.py <skinned.glb> <stage>`（蒙皮模型自动生成骨架语义映射并经 Balsam 转换；无骨架模型加 `--allow-static` 静态展示），详见 `three_d/wiki/`。

### 生长与状态

| 键 | 默认值 | 范围 | 说明 |
| --- | --- | --- | --- |
| `age_speed_multiplier` | `1` | 0 – 1000000 | 年龄流逝倍速，调大可快速观察进化。 |
| `evolve_threshold_days.young` / `.adult` | `7` / `21` | 0 – 3650 | 幼年 → 成年、成年 → FINAL 的进化天数阈值。 |
| `decay_per_hour.mood` / `.fullness` / `.cleanliness` | `2` / `3` / `1.5` | 0 – 100 | 心情、饱食度、清洁度的每小时自然衰减。 |
| `interaction_gain.pet` / `.feed` / `.clean` / `.poke` | `5` / `20` / `15` / `-8` | −100 – 100 | 摸摸、喂食、洗澡、戳一戳的单次数值影响。 |
| `score.mood_weight` / `.fullness_weight` / `.cleanliness_weight` | `0.4` / `0.4` / `0.2` | 0 – 1 | 养护分的加权系数。 |
| `score.healthy_threshold` | `70` | 0 – 100 | 养护分低于该阈值走 neglected（疏于照料）分支。 |
| `sleepy_idle_minutes` | `10` | 0 – 1440 | 空闲超过该分钟数显示睡姿；`0` 表示禁用。 |
| `user_name` | `主人` | 1 – 32 字符 | 宠物与 AI 工具执行时对你的称呼。 |

### 桌面行为（`behavior`）

| 键 | 默认值 | 范围 | 说明 |
| --- | --- | --- | --- |
| `walk_speed` | `80` | 0 – 2000 | 行走速度（px/s）。FINAL 的长裙步态基础周期为 1.6 Hz，速度提高时自适应增加步频。 |
| `follow_speed` | `600` | 0 – 5000 | 跟随鼠标模式下的移动速度（px/s）。 |
| `wander_idle_min_s` / `wander_idle_max_s` | `5` / `15` | 0 – 600 / 0 – 3600 | 随机游走之间停歇时长的随机区间（秒）。 |
| `first_idle_s` | `3` | 0 – 600 | 启动后首次游走前的等待时间（秒）。 |
| `edge_margin_px` | `40` | 0 – 500 | 距屏幕边缘保持的最小距离（px）。 |
| `climb_min_depth_px` | `30` | 0 – 200 | 判定为可攀爬平台所需的最小深度（px）。 |
| `pet_height_px` | 无（示例已移除） | 1 – 500 | 已弃用：仅在启动瞬间、真实显示尺寸送达前作为初始高度兜底，随后即被实际立绘尺寸覆盖，配置它不会改变运行值。为兼容含此键的旧配置仍被校验接受，新配置无需设置。 |

### 主动关怀与吃鼠标（`proactive`）

| 键 | 默认值 | 范围 | 说明 |
| --- | --- | --- | --- |
| `quiet_hours` | `[23, 8]` | 两个 0–23 的整点 | 安静时段（起止小时，可跨午夜），期间不主动打扰。 |
| `sedentary_min` | `45` | 0.01 – 480 | 久坐提醒阈值（分钟）。 |
| `sedentary_cooldown_min` | `30` | 0.01 – 480 | 两次久坐提醒之间的最小间隔（分钟）。 |
| `idle_threshold_min` | `5` | 0.01 – 480 | 吃鼠标的空闲门槛；空闲不足时只发气泡不吃。 |
| `eat_mouse_duration_s` | `10` | 0.3 – 15 | 单次吃鼠标时长（秒，硬上限 15）。 |
| `eat_mouse_gain.fullness` / `.mood` | `5` / `3` | −100 – 100 | 吃鼠标结束后的养成数值回补。 |
| `eat_mouse_hotkey_label` | 无 | 字符串 | 自定义气泡中“强制吐出”热键的显示文案。 |
| `dnd` | `false` | 布尔 | 手动勿扰开关；会话中途开启会立即吐出。 |
| `video_apps` | 常见播放器与浏览器 | 字符串数组 | 检测到这些应用活跃播放时不吃鼠标。 |
| `festivals` | 元旦等 6 个内置节日 | `{"MM-DD": "名称"}` | 在内置节日之外追加自定义节日祝福。 |

### 聊天情绪（`chat_emotion`）

本地聊天情绪开关、每日兜底时段（默认 `22:00`）和短时表情时长（默认 5 分钟）。每次启动以 `neutral` 开始；每条用户消息仅在检测到高置信、非中性情绪时立即换表情，最多保留 5 分钟后恢复 `neutral`；22:00 没有明确情绪时显示困倦。可从托盘“聊天情绪设置”修改时段。

| 键 | 默认值 | 范围 | 说明 |
| --- | --- | --- | --- |
| `enabled` | `true` | 布尔 | 是否启用本地情绪分析。 |
| `schedule` | `["22:00"]` | 1–8 个 `HH:MM` | 每日兜底结算时段。 |
| `retention_hours` | `48` | 1 – 168 | 用户消息在本机保留的时长（小时）。 |
| `expression_minutes` | `5` | 1 – 60 | 情绪表情的最长持续时间（分钟）。 |
| `confidence_threshold` | `0.55` | 0 – 1 | 消息级情绪触发置信度。 |
| `event_confidence_threshold` | `0.5` | 0 – 1 | 事件级情绪触发置信度。 |
| `mood_delta.*` | happy `4` / neutral `0` / sad `-3` / sleepy `-1` / hungry `-2` | −20 – 20 | 检出对应情绪时对养成心情的修正。 |

**本地数据说明**：AI 长期记忆与聊天情绪上下文都只存本机（明文 JSON）——`memory.json` 存模型归纳的事实，`chat_emotion.json` 存最近 48 小时的用户消息文本（随开关即时生效，档案可随时删除）。

### 环境通道（`wind` / `sun`）

| 键 | 默认值 | 范围 | 说明 |
| --- | --- | --- | --- |
| `wind.enabled` | `false` | 布尔 | 实时风力驱动立绘静止摆幅（Open-Meteo，免 key；失败时用兜底增益）。 |
| `wind.latitude` / `wind.longitude` | `0.0` | ±90 / ±180 | 定位坐标。 |
| `wind.poll_minutes` | `20` | 5 – 1440 | 风力数据轮询间隔（分钟）。 |
| `wind.fallback_gain` | `1.0` | 0 – 4 | 取不到风数据时的兜底摆幅增益。 |
| `sun.enabled` | `false` | 布尔 | 按实时太阳位置计算地面阴影（纯本地计算，无网络请求）。 |
| `sun.latitude` / `sun.longitude` | `0.0` | ±90 / ±180 | 定位坐标。 |
| `sun.timezone_offset` | `null` | −14 – 14 或 `null` | 时区偏移；`null` 自动取系统本地时区。 |
| `sun.shadow_alpha` | `0.4` | 0 – 1 | 阴影不透明度。 |

### 热键与日志

| 键 | 默认值 | 范围 | 说明 |
| --- | --- | --- | --- |
| `hotkeys.chat` | `cmd+option+p` | 热键串 | 显示/隐藏聊天面板；Windows 上 `cmd`/`option` 自动解释为 `Ctrl`/`Alt`。 |
| `hotkeys.spit` | `cmd+option+t` | 热键串 | 强制解除“吃鼠标”状态。 |
| `log_level` | `INFO` | `DEBUG` / `INFO` / `WARNING` / `ERROR` | 日志级别；命令行 `--verbose` 等效于 `DEBUG`。 |

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

`llm` 段除 `providers` 外还有两个全局参数：`max_tokens`（默认 `4096`，范围 256–128000，单次请求的生成上限）和 `stream_total_s`（默认 `180`，范围 10–3600，单次流式回复的总时长上限，超限自动中断以防“慢滴流”）；在单个 provider 内配置 `max_tokens` 可覆盖全局值。`providers.<名称>` 内配置 `model`、`base_url` 与 `api_key_env`，任意 OpenAI 兼容端点均可接入；非 `deepseek` 的 provider 必须显式给出 `base_url`，否则启动时报错（不会把 key 误发到其他端点）。

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

### 目录结构

```
├── app.py            # 入口：装配配置、托盘与平台适配层
├── pet/              # 运行时包：行为、骨骼、聊天、平台适配
├── assets/           # 立绘与骨骼资产（frames / rig_* 各代 / ai / reference）
├── tools/            # 产线脚本：图层拆分、生成、训练、修复与渲染
├── spikes/           # 各阶段验证脚本（spikes/_qa/ 为本地证据产物，不入库）
├── docs/             # 工作文档
│   ├── reviews/      # 阶段评审 REVIEW-*、浸泡测试与内存对比报告
│   ├── research/     # 动效/LoRA 产线等调研资料
│   └── planning/     # 设计思路、版本规划、工作表与平台分工
├── wiki/             # 项目 wiki：概念、设计、实验与资料索引（见 wiki/index.md）
└── output/           # 运行/复查产出的带日期 GIF 与帧（仅本地，不入库）
```

更多设计、版本规划和平台适配说明请参阅：[设计思路.md](docs/planning/设计思路.md)、[版本规划.md](docs/planning/版本规划.md) 与 [平台适配与分工.md](docs/planning/平台适配与分工.md)。

## 版本沿革

各次要版本（0.x）的主题与主要改动速览；逐版任务台账与留痕教训见 [wiki/资料-版本实现台账.md](wiki/资料-版本实现台账.md)，各版验收标准见 [版本规划.md](docs/planning/版本规划.md)。

| 版本 | 主题与主要改动 |
| --- | --- |
| v0.0 | 立项与规划：设计思路、版本规划与模块接口契约冻结；随机游走原型；双平台分工计划（UI 分层走 QML）。 |
| v0.1 | 上屏：透明置顶浮窗 + emoji 立绘 + 底边行走；`platform.py` 工厂与单实例锁。 |
| v0.2 | 状态与存档：心情/饱食/清洁度养成、原子写存档（PetStateStore）与单击/拖动手势消解。 |
| v0.3 | 桌面物理：五态行为 FSM、边缘攀爬、窗口图层探针 `solid_at`、透明穿透双层窗。 |
| v0.4 | AI 聊天：DeepSeek 流式对话 + function calling + 离线降级；密钥入系统凭据库。 |
| v0.5 | 进化：年龄驱动 幼年 → 成年 → final 三阶段，按养护分走 healthy/neglected 分支。 |
| v0.6 | 主动关怀：链式唤醒的早晚问候、久坐提醒与节日祝福。 |
| v0.7 | 吃鼠标：安全六铁律 + 看门狗超时 + 强制吐出热键。 |
| v0.8 | 系统工具与危险拦截：ToolRegistry 危险分级、执行前确认框与权限向导。 |
| v0.9 | 长期记忆：TF-IDF 检索 + 重要度/时间衰减，记忆只存本机。 |
| v0.10 | AI 立绘：AIArtProvider 接入与缓存降级；gpt-image 产线交付 三阶段 × 五情绪 × 双分支 30 张立绘（显示尺寸定档 128/160/192）。 |
| v0.11 | 全局热键与开机自启（LaunchAgents / 注册表）。 |
| v0.12 | 打包（py2app / PyInstaller），顺延至 v1.0 发布前完成。 |
| v0.13 | 分层绑骨渲染器：立绘拆件 + RigWindow（QML / QQuickWidget），七批渲染与步态修复。 |
| v0.14 | paperdoll 部件驱动步态 + 四轮 REVIEW 大批次质量修复（深至 v0.14.49，测试扩至 552 项）。 |
| v0.15 | 动效引擎：实时风/阳光环境通道 + EngineBridge（观感未达预期回退 frames）；本地聊天情绪运行时（CPED 训练分类器 + 高置信即时触发）。 |
| v0.16 | 骨骼蒙皮与成年行走主线：v0.16.0–0.16.7 幼年体 skinned_mesh 全栈（47 骨/20 层/LBS、QSG 硬件加速）+ 渲染三档降频与 GC 治理；v0.16.8–0.16.23 成年 ADULT 侧身步态与视觉多轮修复、Wan 3.0 转身片段，FINAL 线 F1–F7（正面/侧身蒙皮骨骼、长裙四片裙骨 GaitSolver 步态、运行时集成）。 |
| v0.17 | 细节打磨：集中处理体验细节问题（首轮：聊天输入框多行自适应、宠物右键/热键直达聊天入口、多会话与历史持久化）。v0.17.7 全量配置文档入 README；v0.17.8 起默认展示后端切 `rig`，侧身行走主线开箱即用（低配可显式配 `frames` 回退）。 |
| v0.18 | 三维化实验线（`three_d/`，config `render3d.enabled` 默认 false=2D）：v0.18.0–0.18.2 资产管线——立绘投影烘焙（xatlas 展开 + moderngl 单 pass GPU 投影 + 扩散填死角）、迷彩九轮排查破案（atlas 行序 V 颠倒一行修复，双约定校验 + 渲染仲裁定位法入册）、多视图投影 + 角度相互拟合（五角度视觉终审 8.5 vs 5.5）；v0.18.3–0.18.4 Magnus A100 GPU 线作业脚本强化、分发打包立项。v0.18.5–0.18.8 绑骨与主力换代——47 骨自动权重（水密母网格骨热 → DataTransfer 转贴图网格）、程序化行走端到端（视觉终审 9/10）、用户模型接棒为主力（母版入库常驻）。v0.18.9–0.18.12 运行时接入三件套（与模型解耦）——Render3DBridge 呈现选择桥（3D→2D 降级矩阵）、semantic_source 语义对接器（2D 实况 → 契约四通道）、整窗点击 + 系统级拖拽交互；README 配置章节与版本沿革补 3D 节。 |
| v0.19 | 交互升级（0.18 号段保留给三维实验线）：反馈四通道与"宠物有意见"。v0.19.0 表现层——交互数值飘字 HUD、手动喂食咀嚼动画、气泡文案模板池（心情分桶）、音效管线（默认关、资产后补）；v0.19.1 动词换代（喂食→喂点吃的、洗澡→梳梳毛、戳一戳→逗一逗，poke 增益 -8→+4 含 config v2 迁移）+ 饱和拒绝（饱腹 ≥92 不再进食）+ 互动疲劳（10 分钟内同类交互 ≥5 次增益归零）；交互语义收拢 `pet/interaction.py`（InteractionOutcome 三态决策，呈现无关）。v0.19.2 需求主动表达与状态可见化：数值触线宠物主动开口求助（proactive 数值触发源，独立冷却）、托盘 tooltip 状态行 + 触线红点、右键菜单交互项 ⚠ 标记（位置固定）。清单见 [docs/planning/交互升级-修改清单与版本规划.md](docs/planning/交互升级-修改清单与版本规划.md)。v0.19.3 聊天双向联动：system prompt 注入宠物实时状态（LLM 可自然提及"我都饿了"）、交互/拒绝/疲劳事件写入长期记忆（当日合并）、聊天判 hungry 时宠物同步表达自身需求。v0.19.4 节日响应：mac 接 EventKit 系统日历（「中国节假日」订阅日历，农历节日正确日期，拒绝授权静默回落内置表）+ 用户生日 config 录入（当日一次祝福 + 心情奖励）。v0.19.5 修复节日/生日祝福：节日气泡此前因持久化序列化异常从未发出、生日每次重启重发并重复奖励；生日当天不再补发节日。v0.19.6 修复 FINAL neglected 行走中喂食后侧身行走变回彩色（旧行走帧抢播咀嚼动画）。v0.19.7 neglected 状态下动作帧（咀嚼/吃鼠标/伸懒腰/打滚/摔落/眨眼）统一灰调，rig 与 frames 两档一致。 |

## 参与贡献

欢迎通过 Issue 或 Pull Request 参与改进。提交前请尽量保持 macOS 与 Windows 的适配层一致，并运行与改动模块相关的 `spikes/` 验证脚本。

## 许可证

本仓库当前未声明许可证。使用、复制或分发前，请先与仓库维护者确认授权范围。
