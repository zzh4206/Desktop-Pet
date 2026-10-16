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
- **天气感知**：配置城市后可获取实时天气（Open-Meteo，免 key），今日首次刷新按阈值提醒带伞/降温/高温/寒潮（每类每天一次）；聊天中可问天气。

## 支持的平台

| 平台 | 状态 | 说明 |
| --- | --- | --- |
| macOS | 支持 | 部分功能（如“吃鼠标”）需要在系统设置中授予辅助功能权限。 |
| Windows | 支持 | 支持全局快捷键与注册表开机自启。 |
| Linux | 暂不支持 | 当前平台适配层仅实现 macOS 与 Windows。 |

## 快速开始

### 1. 环境要求

- Python 3.10 – 3.12（3.13 / 3.14 暂不支持：依赖 PySide6 的版本上限尚未放开，在这两个版本上 `pip install` 会直接失败）
- macOS 或 Windows

### 2. 获取项目

```bash
git clone https://github.com/zzh4206/Desktop-Pet.git
cd Desktop-Pet
```

没有安装 git 的话，也可以在 GitHub 仓库页面选择 Code → Download ZIP 下载后解压（解压出的目录名为 `Desktop-Pet-main`，下文命令里的 `cd Desktop-Pet` 相应换成 `cd Desktop-Pet-main`）。

### 3. 安装 Python（仅 macOS）

较新版本的 macOS 默认没有 `python` 命令，只有 `python3`；而系统自带的 `python3` 是 3.9，python.org 下载页首推的又是最新版（3.13+），都不在支持范围内。先在终端运行 `python3 --version` 检查：显示 3.10 / 3.11 / 3.12 即可跳过本节；否则按下面任一路线安装 3.12。

**路线一：python.org 安装包（推荐）**

到 [python.org 下载页](https://www.python.org/downloads/) 的版本列表里下载最新的 3.12.x（macOS 64-bit universal2 installer），双击安装，装完重新打开终端。

**路线二：Homebrew**

> 下面的命令请**逐条**复制到终端执行，不要整块粘贴：安装脚本会交互式询问开机密码，多行一起粘会被当作密码或按键吃掉。输入密码时屏幕不显示任何字符是正常的。

```bash
# 安装 Homebrew（会先自动安装 Xcode 命令行工具，需要输入密码，耐心等待）
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"

# 装完按屏幕上 "Next steps" 的提示执行两行命令把 brew 加入 PATH，例如（Apple Silicon）：
echo 'eval "$(/opt/homebrew/bin/brew shellenv)"' >> ~/.zprofile
eval "$(/opt/homebrew/bin/brew shellenv)"
# Intel 机器的路径是 /usr/local/bin/brew，以屏幕提示为准

# 安装 Python 3.12
brew install python@3.12
```

> Homebrew 的安装脚本需要访问 GitHub，网络不通时请改走路线一。

两条路线装好后都有 `python3.12` 命令。装了多个 Python 时，`python3.12` 一定指向 3.12，`python3` 未必——后续步骤请按实际情况选择。

### 4. 首次安装

```bash
# macOS
rm -rf .venv                   # 之前用过其他 Python 版本建过 .venv 的话先删掉重建；没有可跳过
python3 -m venv .venv          # python3 --version 不在 3.10–3.12 时，改用 python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python app.py
```

```powershell
# Windows PowerShell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python app.py
```

激活成功后命令行前会出现 `(.venv)` 前缀；虚拟环境内自带 `python` 和 `pip` 命令，依赖只装在虚拟环境里，不污染系统 Python。

### 5. 之后每次启动

```bash
# macOS
cd Desktop-Pet                 # 换成你的项目目录
source .venv/bin/activate
python app.py
```

```powershell
# Windows PowerShell
cd Desktop-Pet                 # 换成你的项目目录
.venv\Scripts\Activate.ps1
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

**本地数据说明**：AI 长期记忆与聊天情绪上下文都只存本机（明文 JSON）——`memory.json` 存模型归纳的事实，`chat_emotion.json` 存最近 48 小时的用户消息文本（随开关即时生效，档案可随时删除）。天气缓存 `weather_cache.json`（最近快照 + 城市坐标，明文可删；删除后下次刷新自动重建）。天气数据由 [Open-Meteo](https://open-meteo.com/) 提供（CC BY 4.0，非商用免费）。

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

### 天气感知与提醒（`weather`）

| 键 | 默认值 | 范围 | 说明 |
| --- | --- | --- | --- |
| `weather.enabled` | `false` | 布尔 | 天气感知总开关（关闭时功能静默，不发任何请求）。 |
| `weather.city` | `""` | 城市名 | 手动填写（如 `"Shanghai"` / `"上海"`），经 geocoding 转坐标并缓存；隐私最优，不上传其他信息。填了则优先于 IP 定位。 |
| `weather.refresh_min` | `30` | 5 – 720 | 刷新间隔（分钟）。断网时用最后一次快照并标注「数据可能过期」，不触发提醒。 |
| `weather.locate` | `"none"` | `"none"` / `"ip"` | 定位方式：`"ip"` 用 IP 定位（仅 `city` 为空且 `locate_consent=true` 时生效）。 |
| `weather.locate_consent` | `false` | 布尔 | IP 定位「确认开启」门禁。建议在「设置 → 桌宠权限自检」页开启（会弹确认框），也可手动填 `true`。 |
| `weather.alerts.precip_prob` | `50` | 0 – 100 | 今日最大降水概率超过该值 → 提醒带伞（措辞不保证性：「看起来要下雨」）。 |
| `weather.alerts.drop_c` | `5` | 0 – 40 | 较昨日最高温降温超过该值（℃）→ 提醒添衣。 |
| `weather.alerts.hot_c` | `35` | 0 – 60 | 今日最高温达到该值（℃）→ 高温提醒。 |
| `weather.alerts.cold_c` | `0` | −60 – 20 | 今日最低温达到该值（℃）→ 寒潮提醒。 |

每类提醒每天最多一次（重启不重复发）；安静时段与勿扰模式静默。聊天中宠物可回答「今天穿什么/要下雨吗」（`get_weather` 工具，读本地缓存）。天气数据 **Powered by [Open-Meteo](https://open-meteo.com/)**（CC BY 4.0，非商用免费；准确性不做保证）。

**国内可达性兜底**：Open-Meteo（境外源）在国内偶发超时——抓取失败会**立即重试一次**，仍失败自动切换备用源 [wttr.in](https://wttr.in/)（免 key；实测直连可达）。备用源字段降级：无「昨日温差」数据，降温提醒自动跳过（缺数据不猜）；其余提醒（带伞/高温/寒潮）正常。单次请求超时 6 秒，连续失败按 1→5→30 分钟退避、连续 5 次当日放弃。IP 定位由 [ip-api.com](http://ip-api.com/) 提供（免费档仅 HTTP、城市级精度；开启后仅单次发送公网 IP，坐标随缓存落盘）。

### 热键与日志

| 键 | 默认值 | 范围 | 说明 |
| --- | --- | --- | --- |
| `hotkeys.chat` | `cmd+option+p` | 热键串 | 显示/隐藏聊天面板；Windows 上 `cmd`/`option` 自动解释为 `Ctrl`/`Alt`。 |
| `hotkeys.spit` | `cmd+option+t` | 热键串 | 强制解除“吃鼠标”状态。 |
| `hotkeys.focus` | `cmd+option+f` | 热键串 | 开始/停止番茄钟（v0.25.2，mac `Cmd+Option+F` / Windows `Ctrl+Alt+F`）；被占用时气泡提示改键，功能不降级（托盘/聊天指令入口不受影响）。 |
| `log_level` | `INFO` | `DEBUG` / `INFO` / `WARNING` / `ERROR` | 日志级别；命令行 `--verbose` 等效于 `DEBUG`。 |

### 番茄专注（可选）

默认**入口可见但绝不自动运行**（不点不 start 即 IDLE，对吃鼠标/久坐/纠缠/主动关怀
零影响）。开始方式三选一：托盘「🍅 开始番茄钟」、热键 `hotkeys.focus`、聊天说
「开始番茄钟」。

| 键 | 默认值 | 范围 | 说明 |
| --- | --- | --- | --- |
| `focus.enabled` | `true` | 布尔 | 总开关。`false` = 托盘入口与热键全部隐藏、不加载节律器（彻底不用）。 |
| `focus.work_min` | `25` | 1 – 180 | 专注段时长（分钟）。 |
| `focus.short_min` | `5` | 1 – 60 | 短休息时长（分钟）。 |
| `focus.long_min` | `15` | 1 – 120 | 长休息时长（分钟，每 `cycles` 个专注段后）。 |
| `focus.cycles` | `4` | 1 – 12 | 几轮专注后进入长休息。 |
| `focus.break_enforce` | `false` | 布尔 | 休息执法（**默认关 = 零输入抑制**）：休息开始 30s 内键鼠活动累计每 10s 记一次 `breaks_cut`，累计 ≥20s 才温和气泡一次；不接管鼠标、不做任何输入限制。 |
| `focus.mood_gain` | `3` | 0 – 20 | 每完成一个专注段的心情增益（走养成状态机，托盘状态行自动刷新）。 |

运行期行为：专注段宠物趴下陪伴（停止游走，拖拽仍可挪动）；运行中静默久坐提醒与
吃鼠标、链式唤醒顺延 10min（只挡新发起，不打断已在进行的抑制）；DND 只静默提醒、
节律照走。当日完成数/累计时长/打断数/连续天数落 `focus_state.json`（数据目录，
可删），重启在 5 分钟宽限内恢复进行中的节律；托盘悬停可见「🍅×今日完成数」。

### 主动纠缠（可选）

宠物有自己的「陪伴值」（`attention`）——与心情/饱食/清洁度并列的**第四个独立数值**，
托盘状态行显示为「陪伴」。摸头/逗一逗/聊天会涨，随时间按成长阶段衰减（幼年掉得最快，
成年次之）。陪伴值低到阈值以下时，它会**主动凑过来**：跑到光标边求摸头，或冒个气泡
撒娇（鲸鱼娘式的傲娇抱怨，比如「才、才不是想你了」）。

默认**不打扰正在干活的你**——下列五重门全部通过才可能触发一次：

| 门 | 判据 |
| --- | --- |
| 静默 | 全屏、系统勿扰、番茄专注中、正在看视频 → 一律不吵；深夜时段（`proactive.quiet_hours`，默认 23–8 点）不动 |
| 时机 | **只在任务边界放行**——键鼠空闲 30 秒到 10 分钟之间（正打字 / 刚离开都不算；这是本版唯一接入的新时机信号） |
| 阈值 | 陪伴值低于 `attention.pester_threshold`（默认 35） |
| 退让 | **被无视就退让，绝不升级**：连续被无视后按 5 分钟 → 30 分钟 → 2 小时阶梯静默，静默期内完全不打扰。这是铁律，**不存在「越不理越闹」** |
| 频控 | 默认每小时最多 2 次、每 24 小时最多 3 次（**从严**起步） |

此外托盘「**安静模式**」是更外层的总闸，勾上即恒不触发。它只挡**行为**——陪伴值照常
演化，所以取消勾选是「接着算」，不是回出厂值。

| 键 | 默认值 | 范围 | 说明 |
| --- | --- | --- | --- |
| `attention.enabled` | `true` | 布尔 | 陪伴系统总开关。`false` = 陪伴值不演化、也不主动纠缠。 |
| `attention.pester.enabled` | `true` | 布尔 | **纠缠行为总闸**（托盘「安静模式」就是它）。`false` = 不主动纠缠，但陪伴值照常演化。 |
| `attention.pester.hourly_limit` | `2` | 0 – 20 | 每小时最多主动纠缠几次。**觉得烦就调小或设 0**。 |
| `attention.pester.behaviors` | `["run_to_cursor","bubble_whine"]` | 行为名列表 | 纠缠行为池：`run_to_cursor` 奔光标求摸头（走 FSM 两段态）、`bubble_whine` 气泡撒娇。写错的名字会被配置校验挡下（整段回退默认 + 日志告警）。 |
| `attention.pester.lines` | 内置池 | 按类覆写 | 撒娇文案罐头池覆写，三类：`low_attention` / `pat_failed` / `ignored`。 |
| `attention.pester_threshold` | `35` | 0 – 100 | 陪伴值低于此线才可能纠缠（调低 = 更少打扰）。 |
| `attention.daily_limit` | `3` | 0 – 50 | 每 24 小时最多几次。 |
| `attention.backoff_s` | `[300, 1800, 7200]` | 秒列表 | 退让阶梯：连续被无视第 1 / 2 / 3+ 次后的静默时长。 |
| `attention.decay_per_hour` | `{"YOUNG":4.0,"ADULT":2.5,"FINAL":1.5}` | 每小时点数 | 陪伴值衰减速率（按成长阶段）。 |
| `attention.gain` | `{"pet":40.0,"poke":25.0,"chat":30.0}` | 每次点数 | 摸头 / 逗一逗 / 聊天的陪伴值增益。 |

**「烦人度」坦诚说明**：撒娇文案以**本地罐头池为主**（≥2/3），少量（≤1/3，按累计调用
计）由 LLM 现编——也就是说它大多数时候说的是写死的话，**不是「每次都新想一句」**。LLM
那一路在后台线程执行，**失败即回退罐头**且不占用那 1/3 配额。默认每小时 2 次是**未经实机
浸泡调校的从严起步值**，实机体感后按需调 `hourly_limit` / `backoff_s`；完全不想要就把
托盘「安静模式」勾上。

> 说明性/机制性依据见 [wiki/设计-纠缠互动技术方案.md](wiki/设计-纠缠互动技术方案.md)
> 与 [wiki/资料-桌宠与虚拟陪伴全景调研.md](wiki/资料-桌宠与虚拟陪伴全景调研.md)（打断
> 成本、可打断性预测、任务边界门控等）；「降低打扰感」是设计目标而非已量化承诺。

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
| v0.20 | 模型管理：`pet/model_registry.py` 注册表 + `pet/ui/model_dialog.py` 对话框；托盘「切换模型 / 模型管理…」；`llm.selected` 记忆免启动弹选；运行时切换 `chat_bridge.swap_clients` + `proactive.set_client` 热换客户端。 |
| v0.21 | mini-swe 集成（桌宠具备操纵 CLI 的能力）：聊天侧新增 `swe_task` 委托工具，把「操作终端 / 命令行」的请求交给软件工程助手——`pet/swe_env.py` 沙箱命令执行器 + `pet/swe_agent.py` 线性历史 agent 循环 + `pet/swe_tools.py` bash/submit 工具（只复用 mini-swe-agent 约百行核心，不加 litellm/pydantic 依赖，走现有 `OpenAICompatibleClient`）；安全三件套（沙箱工作区 + 白名单 + 危险确认，fail-closed）；`config.swe` 段默认关；执行步骤经 `bridge.sweStep` 流式回显进聊天面板（`role="swe"` 等宽终端块，不进 DS 历史）。 |
| v0.22 | mini-swe 上下文管理（`pet/swe_context.py`）：线性历史 token 治理两层手段——T1 观测瘦身（`keep_recent` 外老步骤 tool 消息头尾摘录，确定性零成本）+ T2 轨迹滚屏摘要（估算 token 超 `budget_tokens` → 最老一半可压步骤 LLM 压成单条 `[此前步骤摘要]`，单条滚动不累积；失败/关闭回退 T1，fail-open 不杀任务）；压缩只影响喂 LLM 的 messages、UI 步骤回显全量（同 v0.17.1 摘要解耦哲学）；压缩单位为步骤对（assistant `tool_calls` 与 tool 消息按 `tool_call_id` 配对整对替换，杜绝孤儿 tool_call 400）；`SweAgent(context_policy=)` 注入，`config.swe.context` 段（`budget_tokens` 默认 24000 可按模型窗口调整，`summarize=false` 即 T1-only）。 |
| v0.22.1 | mini-swe 本地任务记忆（`pet/swe_memory.py`）：跨任务「本地上下文」三件套——M1 任务档案（任务结束把 instruction/exit_status/submission 追加进工作区 `.swe/history.jsonl`，上限可配、坏行容错、本地明文可删）+ M2 相似召回注入（新任务起跑按 instruction 相似度取 top-k 历史记录拼进任务前缀；分词复用 `pet.memory._tokenize` 单一实现，平滑 IDF 加权 + 重合度 <0.2 视为零相关不注入 + 30 天近期加分）+ M3 NOTES.md 发现级记忆（system prompt 新增规则：旧记忆需命令验证后采信、值得跨任务保留的发现写入工作区 `NOTES.md`，任务起跑注入其头部）；归档只存原始 instruction（防记忆自噬膨胀）；`config.swe.memory` 段（`recall_k=5`/`recall_chars=200`/`history_cap=200`/`notes` 开关）。 |
| v0.23 | 天气感知与提醒（`pet/weather.py`）：Open-Meteo 免 key 数据源（**Powered by Open-Meteo**，CC BY 4.0 非商用免费；调研报告核实并修正初稿「商用免费」表述）；手动城市默认 + geocoding 坐标随缓存落盘（`city` 留空=功能静默）；daemon 线程拉新 + 内存/磁盘双缓存（`weather_cache.json` 原子写、明文可删）+ 离线回读 stale 标注 + 429/5xx 指数退避（连续 5 次失败本日放弃）；主动提醒挂 ProactiveScheduler——今日首次新快照评估带伞/降温/高温/寒潮（阈值 `weather.alerts.*` 可配，每类每日一次落 proactive_state 去重、stale/缺数据/跨日快照不触发、DND/深夜静默、不保证性措辞）；LLM 工具 `get_weather`（只读缓存不拉网，stale 透传）；`Sensors.weather` 字段位留给 0.24 纠缠线行为调制；纯共享层 mac/win 同代码（系统级天气源 adapter `get_weather_source()` 坑位预留，本线不实装）。 |
| v0.24 | 主动纠缠互动（`pet/attention.py` + `pet/pester_lines.py`）：新增**第四个独立数值「陪伴值」**（`PetState.attention`，存档 schema v2 带 v1 迁移，托盘状态行显示「陪伴」、低陪伴追一句「有点想你」）；纯逻辑引擎 `AttentionEngine`（按成长阶段分档衰减 + 摸头/逗一逗/聊天增益 + 阈值触发）与多门 `should_pester`（静默 → 任务边界时机 → 专注防御探测 → 阈值 → 退让阶梯 → 频控/日频，未过门不消耗配额）；**退让铁律**——被无视只降级不升级（5min → 30min → 2h 静默），从机制上杜绝「越不理越闹」；纠缠行为池两个——`run_to_cursor` 奔光标求摸头（FSM `_PESTER` 两段态：奔向光标 → 停留待摸）与 `bubble_whine` 气泡撒娇；撒娇文案**本地罐头池 ≥2/3 + LLM 现编 ≤1/3**（按累计调用计，后台线程执行、失败回退罐头且不占配额）；托盘「安静模式」一键总闸（`attention.pester.enabled=false`，与「陪伴值照常演化」解耦）+ config `attention.*` 三处同步（说明见「主动纠缠（可选）」节）。任务边界本版只接入键鼠空闲窗口（30s–10min）一个信号；窗口切换信号按共享层约定留 win 侧坑位（诚实标注，未实装）。 |
| v0.25.0 | 番茄专注核心（`pet/focus.py`）：可选节律器 `FocusSession`（`IDLE→FOCUS→SHORT→LONG` 迁移链、`start/stop/skip/tick/on_dnd_active`、`active` 门禁、虚拟时钟 `now_fn` 注入、`focus_state.json` 原子写重水化、当日统计 + streak）；**IDLE 惰性接管不变量**——不 start 即零影响。 |
| v0.25.1 | 番茄专注配置：`config.focus` 段（`enabled` 默认 true=入口可见但绝不自动运行、`work_min`/`short_min`/`long_min`/`cycles`/`break_enforce` 默认关/`mood_gain`）+ `hotkeys.focus`（mac ⌘⌥F / win Ctrl+Alt+F，id=3）；测试口径纠正为本地不入库，`focus.py` docstring 的「CI 单测」表述更正为「平台库-free + 本地单测」（本仓无 CI）。 |
| v0.25.2 | 番茄专注入口：托盘「🍅 开始/停止番茄钟」动态项 + 全局热键 id=3（双平台 shim 注册、冲突沿用 `on_conflict` 改键气泡）+ `platform.start_hotkeys` 透传 `focus_key`/`on_focus` + app 装配 `FocusSession`（气泡/FSM 事件/DND/全屏注入 + 每拍 tick + 三入口收敛 toggle）+ ToolRegistry `start_pomodoro`/`stop_pomodoro`（无危险级）；`focus.enabled=false` 时托盘项与热键同步隐藏。 |
| v0.25.3 | 番茄陪伴 + 互斥：`behavior.py` 增冻结模式 `_FOCUS_REST`（事件 `focus_rest_on/off`——停 WANDER/物理/攀爬、期间忽略 motion_mode 切换、退出按存量恢复，零新立绘走 idle 静帧）+ `app._LOCO_BREAK_MODES` 收编；`proactive.py` 注入 `focus_fn` 门禁（专注运行中静默久坐提醒/吃鼠标、链式唤醒顺延 10min；只挡新发起不打断已在进行的抑制，fail-open 且 IDLE 恒放行）；0.24 纠缠经既有 `_focus_state` duck-typing 自动静默。 |
| v0.25.4 | 番茄休息执法（可选，`focus.break_enforce` **默认 false = 零输入抑制红线**）：app 注入 `idle_fn`（系统空闲 <2s 视为有键鼠活动），休息段开始 30s 窗口内活动累计每 10s 记一次 `breaks_cut`、累计 ≥20s 才温和气泡一次（单次误触不提醒，§9 拍板阈值）；**不接 mouse_lock、不做任何输入抑制**。同版修：`FocusSession._emit` 文案池元组 → 随机取一条 `str`（旧版把整池交给 `bubble_fn`，与 `_auto_bubble/BubbleManager.show` 的 `text: str` 契约不符）。 |
| v0.25.5 | 番茄统计与养成：app 注入 `store`（完成番茄 `store.update(mood=+mood_gain)` 走 observer，托盘状态行自动刷新）+ `state_path=data_dir/focus_state.json`（当日完成数/累计专注秒/打断数/连续天数，跨日归零、`streak_days` 按当日有完成维护）；**重启续算**——进行中节律按 deadline 恢复（5min 宽限内且同日），恢复到 FOCUS 段时补发 `focus_rest_on` 陪伴姿态（全屏静默守卫照旧），IDLE/休息段不补发。 |
| v0.25.6 | 番茄收尾：托盘 tooltip「🍅×今日完成数」（`TrayManager.set_focus_count` + 纯函数 `tooltip_text`，0 不显示、与状态行互不覆盖）；README 补 `focus.*` / `hotkeys.focus` 配置表与运行期口径（含「默认关 = 零输入抑制」红线）+ 版本沿革。 |
| v0.25.7 | 番茄线文档归位（无代码改动）：`README.md`「版本沿革」的 v0.25.4/.5/.6 三行归位到表内（0.25.4 起误追加到文件末尾「参与贡献」之后，导致表内只到 v0.25.3），并补本行。 |
| v0.27.0 | 番茄自由度扩展·模型层①（`pet/focus.py`）：**L3 记账缺陷修复**——`focus_sec` 由「按配置 `work_min` 记账」改为**按实耗**（`_enter` 记 `_phase_started_at`，收口 `min(now, _deadline) − 起点`），pomodoro 未延长时与旧口径**逐值相等**、睡眠大跳仍不补算（L2），是「延长/正计时」不失真的前置；`start(minutes, *, as_default=False)` 拆「一次性 vs 设为默认」（修 L4：旧版 `start(minutes)` 永久改写默认，Q6）；新增 `extend(minutes=5)`（只推移绝对时刻 `_deadline`，延长部分按实耗自动计入，Q3）与 `complete()`（提前完成**计入** completed/streak，与 `stop()` 不计形成明确对照）；`skip()` 逻辑零改动、只补测。入口接线/预设热更新/暂停继续/正计时/统计历史分别归 0.27.1–.7，本版 `pause()` **不存在**。 |

## 参与贡献

欢迎通过 Issue 或 Pull Request 参与改进。提交前请尽量保持 macOS 与 Windows 的适配层一致，并运行与改动模块相关的 `spikes/` 验证脚本。

## 许可证

本仓库当前未声明许可证。使用、复制或分发前，请先与仓库维护者确认授权范围。
