# 桌宠 双平台协作与 Windows 适配计划

> 定位：本文档是 **双平台协作机制 + Windows 平台适配路线图**，配套 `设计思路.md`（接口单一真相源）与 `版本规划.md`（版本里程碑）。
> 状态：**规划阶段，未动工。**
> 背景：两人协作，一人 macOS、一人 Windows，**双侧都主动开发**，不存在单侧追赶。接口契约已冻结，但实现路线初版只写了 macOS。
> 协作模式：**双侧并行开发 + 互相适配对方提交的共享代码**。同一套接口契约，共享核心由双侧共同演进，平台差异集中隔离；任一侧提交的共享代码若引用了平台 API，另一侧照**平台差异登记表**做对应适配。

---

## 0. 一句话概括

macOS 侧与 Windows 侧**并行推进同一版本号**，各自负责自己平台，共享核心按分工共同编写；任一侧合入共享改动后，另一侧针对其中的平台引用点做适配。版本完成以**双平台各自 Must 全绿**为准。

---

## 1. 协作模型与分工

### 1.1 角色（双侧都是开发者）

| 侧 | 职责 |
|---|---|
| macOS 侧 | macOS 平台实现 + 共享核心分工部分（主笔）+ 对 Windows 侧共享改动的评审与适配 |
| Windows 侧 | Windows 平台实现 + 共享核心分工部分（主笔）+ 对 macOS 侧共享改动的评审与适配 |

- **共享核心按模块分工**（主笔归属可协商，按能力/兴趣分配，原则：谁主笔谁提交，另一方必须评审 + 适配）。
- 平台文件（`window/sensor/tools/hotkey/mouse_lock/permissions/打包/路径`）各自负责，接口签名对齐即可。
- **任何一方改共享文件，都要考虑对方平台的适配成本**——这是"双侧均需适配"的含义。

### 1.2 共享 vs 平台（关键原则）

- **共享核心（双侧共同演进，不 import 任何平台库）**
  `pet_state` / `behavior`(逻辑与 FSM) / `llm` / `memory` / `asset_provider` / `renderer` / `tools_schema` / `proactive` / `config` / `logging_setup` / `tray`(QSystemTrayIcon 跨平台) / **`ui`(QML 应用型界面，双平台一致)**
- **平台差异（各自维护，接口签名必须对齐）**
  透明窗口 / 传感器(Sensors 数据源) / 全局热键 / 鼠标抑制(吃鼠标) / 系统工具 tools / 权限 / 打包 / 路径

共享核心只面向 `platform.py` 暴露的 adapter 与 `Sensors` 结构编程。**谁把平台 API 漏进共享核心，谁负责登记并被打回**。

---

## 2. 代码组织

沿用现有 `tools_mac.py` 的平台后缀命名惯例，平台文件用 `_mac` / `_win`，加一个工厂选择器：

```
pet/
├── platform.py               # 新增：按 sys.platform 选择平台实现（app.py 入口只碰它）
├── window.py                 # PySide6 跨平台框架（透明置顶/穿透/工作区经 adapter）
├── sensor_mac.py sensor_win.py    # 新增拆分：Sensors 数据源（鼠标/空闲/工作区/窗口枚举）
├── tools_mac.py tools_win.py
├── hotkey_mac.py hotkey_win.py
├── mouse_lock_mac.py mouse_lock_win.py   # 吃鼠标（CGEventTap 归属此，原文档未点名文件）
├── permissions_mac.py permissions_win.py
├── ui/                   # QML 应用型界面（聊天/设置/记忆/权限），双平台共享，无平台差异
└── 其余共享文件不变（pet_state / behavior / llm / ...）
```

`window.py` 用 PySide6 为主体（跨平台可复用），平台差异点（置顶层、穿透 flag、工作区获取）经 `platform.py` 注入。若某差异导致窗口层无法共享，才拆 `window_mac.py` / `window_win.py`。

---

## 3. 平台差异登记表（活文档，适配点对照）

**这张表是双向协作机制的核心**：任一侧在共享代码里引入新平台依赖，必须在此登记一行；另一侧照表补齐实现。

| 模块 | macOS 实现 | 用途 | Windows 实现 |
|---|---|---|---|
| 透明置顶浮窗 | NSWindow + PySide6 | 常驻浮窗 | PySide6 `WA_TranslucentBackground` + `WindowStaysOnTopHint` + `WS_EX_TRANSPARENT`/`WS_EX_LAYERED` 点击穿透 |
| 工作区(多屏) | `NSScreen.visibleFrame` | 可用区 | `QScreen.availableGeometry()` / `SystemParametersInfo(SPI_GETWORKAREA)` |
| 鼠标/空闲传感器 | NSEvent / CGEvent | Sensors | `GetCursorPos` / `GetLastInputInfo`（均无需特权） |
| 窗口枚举(后置) | `AXUIElement`(Accessibility) | 攀爬窗口 | `EnumWindows`(pywin32) |
| 全局热键 | pyobjc 全局热键 | 唤聊天/强制吐出 | `RegisterHotKey`(ctypes) |
| 吃鼠标抑制 | `CGEventTap` | 休息提醒 | `WH_MOUSE_LL` 钩子拦截移动 + 钉住光标（看门狗/超时逻辑共用） |
| 系统工具层 | `open -a`/`osascript`/`mdfind`/`pbcopy`/`pkill` | LLM 工具 | `os.startfile`/PowerShell 音量/Windows 搜索或 Everything/`clip`/`taskkill` |
| 危险操作确认 | `NSAlert` | 拦截确认 | PySide6 模态对话框（复用确认与黑名单逻辑） |
| 权限体系 | Accessibility / Automation | 权限页 | 基本无需特权（钩子/热键/剪贴板无需 admin）；权限页改为运行时自检 |
| 开机自启 | LaunchAgents plist | 常驻 | 注册表 `HKCU\Software\Microsoft\Windows\CurrentVersion\Run` 或任务计划 |
| 打包分发 | py2app `.app` | 安装 | PyInstaller `.exe` |
| 存储/日志路径 | `~/Library/Application Support/桌宠`、`~/Library/Logs/桌宠` | 数据 | `%LOCALAPPDATA%\桌宠\`（logs 同目录下 `logs\`） |
| API key | macOS Keychain (keyring) | 凭据 | Windows 凭据管理器 (keyring 后端自动切换，仍不放明文) |

---

## 4. 版本节奏（双侧并行）

```
       v0.1         v0.2         v0.3
macOS:  ──────────→ ──────────→ ──────────→
Windows: ──────────→ ──────────→ ──────────→
       （每版 = 双平台一起合入）
```

- **双侧并行推进同一版本号**，每版本内：
  - 平台文件：各侧自己写；
  - 共享核心模块：按分工主笔，主笔提交 → 另一方评审 + 适配其平台引用点 → 合入。
- **版本完成 = 双平台各自 Must 全绿**，里程碑评审两人一起（各 15 min 或合并）。
- **共享文件冲突规避**：
  1. 同一共享文件**同一时间只允许一方在改**（改前在 issue/群声明认领）；
  2. 接口冻结前提下按模块划片，双份工作互不重叠；
  3. 共享文件改动提交后，对方须在合入前过一遍接口签名。
- 若一侧进度落后，版本以**双平台达成**为完成标志，落后方补齐后一起合入，不单独推进下一版。

> ⚠️ 默认"双侧同步推进同一版本"。若共享核心某处改动频繁引起对方反复适配，可将该模块降级为"任一方独占 + 对方只适配不写"。

---

## 5. 协作契约（提交规范，双向生效）

1. **接口契约不可破坏**：`设计思路.md` §2.2 是单一真相源。任一侧改共享接口 = 同步更新设计思路 + 通知对方。
2. **平台 API 隔离**：共享核心不 import 平台库（pyobjc / pywin32 / ctypes 平台调用）。违反 = review 打回。
3. **登记制（双向）**：任一侧共享代码引入平台依赖，必须在 §3 登记表加行 + commit message 标 `[平台]`，对方照表适配。
4. **适配信号词**：diff 中出现任一即触发对方适配登记 —— `NSScreen / CGEvent / osascript / NSAlert / pyobjc / AXUIElement / LaunchAgents / ~/Library / Keychain / pbcopy / pkill`（macOS 引用点）；`win32 / ctypes / RegisterHotKey / WH_MOUSE_LL / GetLastInputInfo / LOCALAPPDATA / SetWindowLong / os.startfile`（Windows 引用点）。
5. **各自平台文件各自负责**：接口签名对齐即可，不互相 review 平台实现细节。
6. **认领制**：共享文件改动前在 issue/群声明，避免双侧同时改同一文件。

### 适配流程（任一侧，收到对方共享改动后）

1. 审阅对方 commit/PR diff，用 §5.4 信号词定位平台引用点；
2. 对照 §3 登记表：无现成实现 → 补自己平台的实现；已有 → 复核接口签名是否仍一致；
3. 跑自己平台对应里程碑的 Must 验收（`python app.py` 本机验证）；
4. 合并前与对方确认共享接口签名无漂移。

---

## 6. Windows 特有风险（前置 Spike，不占版本号）

| 风险 | 验证版 | 说明 |
|---|---|---|
| 透明点击穿透 | Spike W1（v0.1 前） | `WS_EX_TRANSPARENT`+`WS_EX_LAYERED` 组合有已知坑（穿透区 vs 绘制区），先验证再写 window |
| 吃鼠标对管理员窗口失效（UIPI） | Spike W2（v0.7 前） | `WH_MOUSE_LL` 收不到 UAC/管理员窗口的输入事件；降级策略：检测到该情况只发气泡不吃鼠标 |
| LL 鼠标钩子被杀软拦截/误报 | Spike W2 + v0.12 | 说明文档 + 白名单建议；打包签名可缓解 |
| 高 DPI 多屏坐标 | v0.3 | 不同缩放率屏幕的工作区计算（`GetDpiForMonitor` / Qt 逻辑坐标） |
| PyInstaller 报毒 | v0.12 | 签名或使用说明兜底 |
| 防锁死 | 共用看门狗 | 与 macOS 同一 watchdog 逻辑，平台只换"释放实现"（取消钩子 + 恢复光标位置） |

> macOS 的 S0/S1 Spike 照旧；Windows 侧新增 W1/W2，均为**并行 Spike，不阻塞主线**。

---

## 7. 各版本双平台交付与适配范围（简表）

| 版本 | 共享核心（分工主笔，另一侧评审+适配） | macOS 侧交付 | Windows 侧交付 | 典型双向适配点 |
|---|---|---|---|---|
| v0.1 | AssetProvider 接口+EmojiProvider；ActionType+BehaviorFSM 骨架；config/logging 地基 | 透明窗+托盘+sensor；路径平台化 | Spike W1 → 透明穿透窗+托盘+sensor；路径平台化 | BehaviorFSM 若引 `NSScreen.visibleFrame` → win 换 `SPI_GETWORKAREA` |
| v0.2 | PetStateStore+衰减+save/load；EmojiProvider 按 mood | 点击交互 | 点击交互 | 存储路径平台化（§3） |
| v0.3 | get_frames；FSM 扩展(拖拽/抛掷/跟随/WANDER)；窗口枚举缓存 | sensor(鼠标/工作区/枚举) | sensor(GetCursorPos/GetLastInputInfo/EnumWindows) | 高 DPI 工作区计算 |
| v0.4 | llm 客户端+function calling+降级；ToolRegistry/ToolContext；system prompt；**聊天面板 UI（QML 共享）** | keyring(Keychain)；tools_mac open_app | keyring(凭据管理器)；tools_win open_app | 危险操作确认 UI（NSAlert ↔ Qt 对话框，保持原生模态） |
| v0.5 | 年龄进化+分支+fast-mode | 进化可视化 | 进化可视化 | 无新平台依赖 |
| v0.6 | ProactiveScheduler+链式唤醒+提醒 | 空闲传感器 | GetLastInputInfo 空闲传感器 | idle gate 双条件对齐 |
| v0.7 | EatMouseSession+看门狗+安全铁律；FSM 加 EAT_MOUSE | Spike S1 → CGEventTap | Spike W2 → WH_MOUSE_LL+钉光标 | 释放实现平台化（看门狗逻辑共享） |
| v0.8 | tools_schema 危险拦截+黑名单；**permissions 权限页（QML 共享）** | tools_mac 补全；NSAlert | tools_win 补全；Qt 确认框 | 权限自检项不同（win 基本无需特权） |
| v0.9 | memory 记忆/遗忘/摘要；**记忆管理 UI（QML 共享）** | 拖放 | Qt 拖放 | 无新平台依赖 |
| v0.10 | AIArtProvider+S0 选型 | provider 平台无关 | provider 平台无关 | 无新平台依赖 |
| v0.11 | hotkey 统一管理；自启逻辑 | pyobjc 热键+LaunchAgents | RegisterHotKey+注册表 Run | 热键冲突处理平台化 |
| v0.12 | 打包脚本/安装卸载文档 | py2app .app | PyInstaller .exe | bundle 内 assets 路径处理差异 |

---

## 8. 目标环境与性能红线

- **macOS 侧**：沿用原规划（Mac，Python 3.10+）。
- **Windows 侧**：Windows 10/11 x64；Python 3.10+（与 macOS 对齐）。
- 共享依赖：`PySide6` / `keyring` / `psutil` / `requests`。
- Windows 额外：`pywin32`（钩子/枚举）；`ctypes` 兜底（热键/穿透）。
- 性能红线沿用 `设计思路.md` §一（双侧各自验证）：idle CPU <1%、动画 <5%、内存 <200MB、网络间隔 >5s。

---

## 9. 待确认决策点（审阅时定）

1. **版本节奏**：默认"双侧同步推进同一版本，双平台一起合入"（§4）；是否改关键里程碑对齐。
2. **共享核心分工**：按模块划片（各认领一批模块）还是按版本轮换主笔，双方协商后在 `版本规划.md` 里落实。
3. **窗口层组织**：默认 PySide6 共享 + adapter 注入；若穿透差异过大再拆双文件（§2）。**应用型界面统一走 QML（Qt Quick）**：聊天/设置/记忆/权限向导双平台共享、无平台差异；本体浮窗保持 Qt 原生渲染，不引入 Web 技术。
4. **冲突规避机制**：默认"共享文件认领制 + 合入前过签名"（§4）；是否引入分支/PR 流程由双方工具习惯定。
5. **Windows 目标**：仅 Windows 10/11；是否兼容 Win10 旧版缩放行为，留 v0.3 实测再定。

---

## 附：与既有文档的关系

- `设计思路.md` —— 接口契约、技术路线、性能红线（本计划不改其内容，仅新增登记制引用 §3）。
- `版本规划.md` —— 版本里程碑（本计划将"每版验收"扩展为"双平台各自 Must 全绿"，不改原 Must 项）。
- 本文档 —— 双平台协作机制 + Windows 适配路线图。
