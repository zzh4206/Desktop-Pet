# 桌面宠物「立绘动效引擎」行业调研报告

> 调研对象：Shimeji（しめじ）/ Shimeji-ee、Neko/Oneko、Desktop Goose、Wallpaper Engine / Live2D 动态立绘。
> 目标：为一个「paper-doll-lite」引擎（静态 PNG 拆件绕 pivot 刚性旋转 + 正弦摆动 + 呼吸 + 落地压扁）给出可落地的动效设计建议。
> 数值来源：能查到权威值的标注出处；查不到的明确标为「经验值」，不编造。

---

## 一、核心结论（TL;DR）

这些产品的共同秘密不是「动画帧多」，而是 **把少数几个低成本的程序化运动叠加在一起**：

1. **呼吸/眨眼/眼神跟随是「活」的底线**——三者都几乎不需要新美术资源，纯靠参数驱动，是性价比最高的一层。
2. **「随机 + 停顿 + 死区」比「连续运动」更自然**——真人/真动物是间歇性动作，不是一直动。Shimeji 用概率状态机，Oneko 用「够近就停下睡觉、离远了才醒」。
3. **物理感 > 帧动画**——重力掉落、拖拽、落地压扁、抛出，这些用极简代码就能带来强烈「实体感」，Shimeji 和 Desktop Goose 都靠它。
4. **朝向 + 分层频率是关键**——眼睛/身体朝向目标、头部和身体用不同周期的正弦（Live2D 呼吸默认就是多周期叠加），是「像活的」而非「像摆件」的分水岭。

---

## 二、各产品动效手法拆解

### 1. Shimeji / Shimeji-ee（极简帧动画 + 物理 + 概率状态机）

**引擎架构（来自 Shimeji-ee 源码分析）：**
- 主循环 **每 40ms tick 一次（约 25fps）**，帧率极低但「活」感很强——说明关键不在帧率。
- 动画单元 = `Pose`：一张图 + `Velocity=(dx,dy)` 每帧位移 + `Duration`（帧数）。`ImageAnchor` 是 pivot（如 `80,160` = 图片底部中心，即「脚底锚点」）。
- 动作类型（`actions.xml`）：`Stay`（站立/待机）、`Move`（走路）、`Fall`（重力下落）、`Jump`、`Dragged`（被拖拽）、`Thrown`（被抛出）、`Climb`（爬窗/爬边），以及可扩展的 `Embedded` 自定义动作（如 `ScanMove` 互相靠近、`Interact` 互动）。
- 行为状态机（`behaviors.xml`）：每个行为有 `Frequency`（权重），结束后按 `NextBehaviorList` 的概率随机选下一个。**遇到 `LostGround`（脚下没地板）自动切到 Fall**；走出屏幕外 `OOB` 则传送到屏幕顶部上方 256px 再重新落下。

**具体动效数值（源码可查）：**
| 动作 | 设计 | 数值 |
|---|---|---|
| 走路 | 4 帧循环，每帧 `Velocity="-4,0"`、`Duration="2"`（2 tick） | 每 tick 移 4px；4 帧 × 2tick = 320ms/循环；**≈100px/s**，帧时长 ≈80ms |
| 待机 | `Stay` 循环切换 1–2 张 idle 图 | 长 `Duration`（如 600 tick = 24s） |
| 下落 | 每 tick `velocityY += gravity`，加空气阻力，落地检测 | 重力常量未公开（经验值：逐帧累加即可）；地板判定用 **-80~0px 容差**扫描 |
| 拖拽 | 按住 → `Dragged`（身体跟手，腿自然下垂姿态） | 无帧动画，姿态图 1 张即可 |
| 抛出 | 松手 → `Thrown`（继承拖拽速度 + 重力抛物线） | — |
| 爬窗 | 走到窗口边缘 → `Climb` 沿边上下爬 | 贴边姿态 + 少量帧 |

**让它「萌」的细节：**
- **脚底锚点 + 位移驱动**：图不变，靠 `Velocity` 平移，脚始终踩在参考线上，走路不会「飘」。
- **落地压扁 / 落地顿一下**：Fall 结束后进入短促的落地姿态，制造重量感（这正是你们已有的压扁，方向对）。
- **「捣乱」动作带来个性**：坐任务栏、把浏览器窗口抓起来丢、偶尔自我复制——低成本、高辨识度。
- **边界行为**：撞墙会爬上去、掉出屏幕会从顶上掉下来，让「世界」有连续性。

### 2. Oneko / Neko（追鼠标）

**原版 X11 oneko（man page 权威值）：**
- 猫追鼠标光标，**追到后开始睡觉**；当鼠标移动速度超过阈值（`-idle`）才醒。
- `-time` 动画间隔 **默认 125000µs = 125ms/帧**；`-speed` **默认 16px/步**（≈128px/s）。
- 可 `-towindow`/`-tofocus` 改追某个窗口/焦点窗口（把「追光标」泛化为「追目标」）。

**现代 oneko.js（源码全文）：**
- `nekoSpeed = 10`（px/帧），约 10fps（>100ms 才推进一帧）→ **≈100px/s**，和 Shimeji 走路速度几乎一致。
- **死区**：`if (distance < 10 || distance < 48) → idle`。即离目标 48px 以内就停下、进入待机——「够近就不追」。
- **醒来有「alert」停顿**：从睡觉/待机恢复时，先显示 1 帧「警觉/抬头」姿态，倒计时几帧（`min(idleTime,7)`）再开追——这一顿让角色显得「注意到你了」而非机械跟随。
- **8 方向朝向**：用 `diffY/distance > 0.5` 之类的比值把方向离散成 N/NE/E/…/NW 8 个，走路帧按朝向切——身体永远朝目标。
- **随机 idle 小动作**：每约 20 秒（`idleTime>10` 且每帧 1/200 概率）随机播一个：睡觉（先 8 帧「困了」→ 2 帧循环「睡」）、挠自己、靠墙挠（靠近屏幕边缘时追加）。**没有复杂缓动，就是「匀速 + 死区 + 停顿 + 随机 idle」**。

**要点**：Oneko 不做加减速缓动，靠「死区停住 + 醒来停顿 + 朝向切换」营造自然感——这对轻量引擎是重大利好：**匀速追逐 + 朝向 + 停顿，即可达到 90% 效果**。

### 3. Desktop Goose（捣乱型桌面角色）

行为以「实体感 + 捣乱」为主，帧动画极少（SourceForge/RPS 描述）：
- 走路闲逛、**发出 HONK 叫声 + 文字气泡**（音效/文字代替复杂口型动画）。
- **抢走并拖动鼠标光标**、拖入图片/meme、留下泥脚印（跟随脚印粒子）。
- 打开并打字进记事本窗口。
- 可被鼠标拎起来拖着走。

**启示**：Desktop Goose 的「活」靠的是**主动干扰桌面 + 拖动物体的物理感 + 音效反馈**，而不是精细立绘动画。对于 AI 立绘角色，可借鉴「抢光标」「拖小窗口」「留脚印/粒子」这类轻量互动。

### 4. Wallpaper Engine / Live2D 动态立绘（静态立绘「微动」的幅度与节奏）

这是与你们「呼吸 + 摆动」最直接对齐的参照。Live2D SDK 官方文档给了**可直接抄的默认参数**：

**呼吸（CubismBreath 官方示例，权威值）：**
| 参数 | 幅度 | 周期 | 权重 |
|---|---|---|---|
| ParamAngleX（头俯仰） | 15° | 6.5345s | 0.5 |
| ParamAngleY（头偏航） | 8° | 3.5345s | 0.5 |
| ParamAngleZ（头横滚） | 10° | 5.5345s | 0.5 |
| ParamBodyAngleX（身体倾角） | 4° | 15.5345s | 0.5 |
| ParamBreath（胸腔缩放） | offset 0.5, 幅 0.5 | 3.2345s | 1.0 |

**关键设计：不同部位用不同周期（3.5s / 5.5s / 6.5s / 15.5s）叠加，避免「整身同频摆动」的机械感。** 这正是「像活物」的核心手法。

**正弦微动（HarmonicMotion 官方默认）：** `value = origin + range·sin(T·2π/Duration)`，默认 `Duration=3s`、`NormalizedOrigin=0.5`、`NormalizedRange=0.5`。

**自动眨眼（官方）：**
- 眨眼间隔：`setBlinkingInterval(6.0)`（C++）或 `8.0`（Web/Java），实际间隔在 **0 ~ 2×该值之间随机**（即 0–12s / 0–16s 随机）。
- 眨眼三段：`setBlinkingSettings(0.8, 0.2, 0.8)` = 闭眼/闭住/睁眼时长（SDK 示例值；**注意这偏慢，真实自然眨眼约 0.1–0.4s，经验值——建议下调到 0.1s 级**）。
- **参数化眨眼**：只需一个「眼皮开合」参数（`ParamEyeLOpen/R`），由 SDK 自动驱动，无需逐帧美术。

**头发/衣摆飘动：** Live2D 用**参数化摆锤物理（pendulum physics）**驱动，幅度与周期随运动状态变化（文档在 Physics 章节）。静态壁纸常见做法：**周期 1–3s 的小幅正弦（经验值）**，且与身体摆动错开相位。

**粒子 / 光影：** Wallpaper Engine 常见加成，但无权威数值；经验值——低频少量粒子 + 轻微光影呼吸即可，优先级最低。

---

## 三、手法清单与数值速查（可直接落地）

| 手法 | 实现成本 | 幅度 / 频率（参考值） | 来源 |
|---|---|---|---|
| 呼吸（身体/胸腔） | 低（已有） | 周期 3–6s；身体 4°、胸腔缩放 ±0.5 | Live2D 官方 |
| 呼吸分层（头 vs 身体） | 低 | 头 8–15°@3.5–6.5s，身体 4°@15.5s，**错开周期** | Live2D 官方 |
| 眨眼 | 低 | 随机间隔 0–12s；开合 ≈0.1–0.4s（经验值） | Live2D 官方 + 经验 |
| 眼神/身体朝向鼠标 | 低 | 死区 48px；8 方向离散；跟随前 0.3–0.7s「警觉」停顿 | oneko.js 源码 |
| 追逐/走路 | 低 | 匀速 ≈100px/s；4 帧循环，帧 ≈80ms | Shimeji + oneko.js |
| 走路颠簸/倾斜 | 低 | 前进方向前倾 + 上下正弦颠簸 + 轻微横滚（经验值 2–4°、2–3Hz） | 经验值（综合） |
| 随机 idle 小动作 | 中 | 每 15–30s 随机 1 次；睡觉/挠/抖/打哈欠 | oneko.js 源码 |
| 重力掉落 | 低 | 每 tick `vy += g`；落地检测容差 -80~0px | Shimeji 源码 |
| 拖拽 + 抛出 | 低 | 拖拽继承手速，抛出带初速抛物线 | Shimeji 源码 |
| 落地压扁 | 低（已有） | squash 短暂 + 回弹 | Shimeji/通用 |
| 表情切换 | 中 | 事件驱动（情绪/交互） | 各产品共性 |
| 抓边/爬窗 | 高 | 贴窗口边缘爬升姿态 | Shimeji |
| 粒子 / 光影 | 高 | 低频少量 | Wallpaper Engine（经验值） |

---

## 四、低成本高收益排序（建议优先级）

> 面向「paper-doll-lite」：假设已有呼吸、正弦摆动、落地压扁。按「资源少但观感提升大」排序。

1. **【P0】眨眼 + 随机间隔**——零额外美术，参数化眼皮即可；「会眨眼」是「活」的第一信号。Live2D 默认 0–12s 随机。
2. **【P0】眼神/身体朝向鼠标（LookAt + 朝向）+ 死区 + 醒来停顿**——直接复用 oneko.js 思路：瞳孔/眼珠向鼠标偏移，够近就停，离远先「警觉」一帧再动。这是「交互感」和「有意识」的来源。
3. **【P1】呼吸分层**——把现有单一正弦改成头部/身体多周期叠加（抄 Live2D 那组数值），成本极低、收益立竿见影，去掉「摆件同频」感。
4. **【P1】随机 idle 小动作**——每 15–30s 随机播一个（睡觉、挠、抖、打哈欠），用 1–3 张变体姿态或正弦变体实现。oneko.js 的核心魅力点。
5. **【P1】走路颠簸/倾斜 + 脚底锚点**——走路时沿前进方向前倾 + 上下正弦颠簸，锚点固定在脚底，避免「飘移」。几乎只用现有 pivot。
6. **【P2】重力掉落 + 拖拽物理 + 抛出**——补「被拎起时身体拉长/腿自然下垂」「抛出带初速」「落地 dust/顿一下」。物理感极强，代码量小。
7. **【P2】表情切换**——事件驱动（被摸、被放下、长时间待机等），几张表情图即可。
8. **【P3】抓边/爬窗**——观感高但需要窗口几何信息 + 抓边姿态，实现成本最高，放最后。
9. **【P3】粒子 / 光影**——氛围加分，收益比最低，作为长期打磨项。

---

## 五、给我们引擎的落地参数建议（可直接做初始默认值）

- **呼吸**：身体 `4° @ 15.5s`；头 `10° @ 5.5s`（横滚）+ `8° @ 3.5s`（偏航）叠加；胸腔缩放 `±0.5 @ 3.2s`。（Live2D 官方默认，可整体按角色风格缩放幅度）
- **眨眼**：间隔随机 `0–8s`（均值约 4s），开合总时长约 `0.15s`（闭 0.05 / 闭住 0.03 / 睁 0.07，经验值）。
- **走路**：速度 `100px/s`；4 帧循环（帧 `80ms`）；身体前倾 2–4°、颠簸 2–3Hz（经验值）。
- **追逐**：匀速 `100px/s`；死区 `48px`；醒来「警觉」停顿 `0.3–0.7s`；8 方向朝向离散。
- **随机 idle**：间隔 `15–30s` 随机，每次 1 个短动作（约 1–3s）。
- **下落/落地**：`vy += g`（g 经验值，逐帧累加即可），落地检测容差 `-80~0px`，落地压扁 `squash 0.85` + 回弹。

---

## 六、引用来源

- [Shimeji-ee Affordances Tutorial（Kilkakon，含走路帧/速度/Pose 结构示例）](https://kilkakon.com/shimeji/affordances.php)
- [Shimeji-ee 源码架构分析（CODE_ARCHITECTURE.md，zhizhiji-shimeji）](https://github.com/YuZhu412/zhizhiji-shimeji/blob/main/CODE_ARCHITECTURE.md)
- [Shimeji-ee for Windows（SourceForge，行为清单：坐任务栏/爬窗/走路/捣乱）](https://sourceforge.net/app/shimeji-ee/)
- [oneko(6) man page（Debian：125ms/帧、16px/步、idle 阈值、towindow/tofocus）](https://manpages.debian.org/bookworm/oneko/oneko.6.en.html)
- [oneko.js 源码（adryd325：死区 48px、nekoSpeed=10、alert 停顿、8 方向、随机 idle）](https://github.com/adryd325/oneko.js)
- [oneko.js 原始脚本 oneko.js（逐行逻辑）](https://raw.githubusercontent.com/adryd325/oneko.js/main/oneko.js)
- [Live2D SDK — Breath（官方呼吸默认参数：角度/周期/权重）](https://docs.live2d.com/en/cubism-sdk-manual/breath/)
- [Live2D SDK — HarmonicMotion（正弦微动公式与默认值）](https://docs.live2d.com/en/cubism-sdk-manual/harmonicmotion/)
- [Live2D SDK — Automatic eye-blinking（眨眼间隔与三段时长）](https://docs.live2d.com/en/cubism-sdk-manual/autoeyeblink/)
- [Desktop Goose（SourceForge，Windows/Mac 行为描述）](https://sourceforge.net/app/desktop-goose/)
- [Rock Paper Shotgun — Desktop Goose 介绍（捣乱行为）](https://www.rockpapershotgun.com/that-dang-goose-can-now-be-let-loose-on-your-desktop)

> 标注说明：带具体数值且未标「经验值」的，均直接引自上述官方文档/源码；「经验值」为综合行业常识给出的起始建议值，供调试起步，非权威测量。
