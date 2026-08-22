# AI 立绘交接文档（图片侧）

> 交接日期：2026-08-22
> 关联：`桌宠立绘生图指南.md`（需求源，74 张清单）/ `设计思路.md` §六（provider 三级）/ `版本规划.md` v0.10
> 本会话已完成：驱动修复 + 全新 v2 全套 30 张静态立绘生成与入库（含鲸尾灰鳍缺陷修复与全套重派生）+ SLEEPY 显示规则 + AIArtProvider 接入后验证
> **工作入口**：本文档 + `D:\Desktop-Pet\pet\asset_provider.py` + `D:\AI\ComfyUI\pet_v2_gen.py`

---

## 一、当前状态（已完成 ✅）

| 事项 | 状态 |
|---|---|
| 显卡驱动 nvlddmkm 蓝屏修复 | ✅ 592.01 → **610.88 WHQL**（WMI 版本 32.0.16.1088），升级后生图正常 |
| 旧 v1 立绘（pet_sprites_final 44 张/旧 47 组） | ⛔ **作废**（用户判定风格不一致），勿再引用 |
| v2 全新一套静态立绘 | ✅ **30 张齐**（3 阶段×2 分支×5 情绪），画风严格一致 |
| 鲸尾灰鳍缺陷（用户反馈） | ✅ 已修：主锚定合成移植大鲸尾，30 张全套重新派生入仓（v0.10.2） |
| 后处理（去背/裁剪/缩放/底对齐） | ✅ 全部完成，已入仓库 `assets/ai/` |
| 动画帧 | ⏸ 按用户决策先留空（`assets/frames/` 只有 `.gitkeep`） |
| SLEEPY 显示规则 | ✅ 已实现（代码+config+测试，见 §六） |
| NEGLECTED 不压制 | ✅ 30 张按全情绪生成齐；AIArtProvider 按 (stage,branch,mood) 全量取图未压制 |
| AIArtProvider 接入（v0.10 核心） | ✅ 已完成并提交（v0.10.0 provider/渲染/工厂 + v0.10.1 get_frames 单帧防闪烁 + v0.10.2 启动空屏修复与资产更新）；`config provider: ai` 已生效，冒烟无异常 |

---

## 二、资产清单与路径

### 成品（真源，直接给代码用）
```
D:\Desktop-Pet\assets\ai\{stage}_{branch}_{mood}.png     # 30 张
D:\Desktop-Pet\assets\frames\.gitkeep                     # 空目录占位
```
- 命名：stage ∈ young/adult/final；branch ∈ healthy/neglected；mood ∈ neutral/happy/sad/sleepy/hungry
- 尺寸按阶段：young **64×64** / adult **96×96** / final **128×128**（对齐 `asset_provider.py::_STAGE_SIZE`）
- 透明 PNG（rembg 去背 + 1px 白边腐蚀），脚底贴画布下沿、水平居中（bottom_center 锚点语义）
- final 有 1px 底边间隙（缩略不可见，可接受）
- **尚未 git 提交**（仓库此前无 assets/，`.gitignore` 不排除 assets/）

### 生成原始图（832×1216 白底 RGB，评审用）
```
D:\AI\ComfyUI\output\pet_v2\                         # v2 全部原始图 + 评审图
  masterB_555_tailled_final.png                      # ★ 主锚定图（一切图的本源）
  masterB_seed555_00001_.png                         # 前版主锚定（含灰色鳍缺陷，仅存史）
  masterC_seed271828_00001_.png                      # 尾巴移植源（大鲸尾画得最好的候选）
  {stage}_{branch}_{mood}.png / _0000X_ 编号         # 逐张原图（新轮次：neutral _00003_/其余 _00004_）
  _sheet2_{stage}.png _final_check_v3.png _tail_verify2.png _tailled_final_check2.png  # 评审图
D:\AI\ComfyUI\output\pet_sprites{,\_final}\ + pet_sprites\emotions\    # 旧 v1，作废勿用
```

### 参考
```
C:\Users\lenovo\Desktop\桌宠立绘生图指南.md   # 需求清单（总数 74 实为 73，有 off-by-one；批1≈各形态 neutral，实际按 30 静态口径执行）
```

---

## 三、生成管线（重出/补图必须用它）

### 脚本
```
D:\AI\ComfyUI\pet_v2_gen.py          # 生成：candidates / bases / emotions 三阶段
D:\AI\ComfyUI\pet_v2_postprocess.py  # 后处理：rembg→裁切→缩放→底对齐→assets/ai
```
ComfyUI 服务：**当前正在运行**（127.0.0.1:8188，D:\AI\ComfyUI，venv python 启动）。启动命令：
```bash
cd /d/AI/ComfyUI && nohup ./venv/Scripts/python.exe main.py --port 8188 > /d/AI/comfyui_server.log 2>&1 &
```

### 核心方法：主锚定层级派生（画风一致的根）
1. **txt2img 主锚定**：`adult_healthy_neutral`，多候选目检挑最佳 → 定稿 masterB_seed555_00001_.png（深蓝→亮青渐变发、鲸鱼鳍耳、正面站姿、白底）
2. **灰鳍缺陷修复（2026-08-22 用户反馈）**：555 稿裙摆右下灰色尖鳍是全图派生源缺陷 → 与 271828 候选（大鲸尾画得最好）做 **PIL 合成移植**：仅擦除右侧灰色刃 (492,848,682,985)，粘贴 271828 大鲸尾裁剪 (545,800)-(800,1085)（带其裙边衔接+13px 羽化），近白清理去阴影晕；**左侧蓝白鳍片为正常鲸尾鳍，勿擦**（曾误擦成浮空残片）。合成版 = `masterB_555_tailled_final.png` 定稿为最终主锚定
3. **形态底图**（5 张）：主锚定 img2img 派生，denoise 0.44（healthy）/ 0.58（neglected，v3 强词），全程 **DERIVE_SEED=777**
4. **情绪**（24 张）：各形态底图 img2img 派生，**denoise 0.44**（0.34 版表情太淡已弃），seed = 777+i，只动脸部词
5. 所有图与主锚定派生距离 ≤2 步 → 发色/线稿/上色习惯强统一

### 关键参数（与旧管线不同处）
| 项 | 值 |
|---|---|
| 模型/采样 | animagine-xl-3.1 / euler_ancestral / steps 30 / cfg 6.0 / clip skip 2 |
| 尺寸 | 832×1216 |
| 情绪词风格 | **直白 danbooru 标签**（smile, open mouth, blush, tears, half closed eyes, drool, star eyes）优于修饰性英文 |
| Base 正向 | `masterpiece, best quality, chibi, 1girl, solo, deep blue hair, ... whale fin shaped hair ... white frilled maid headdress ... whale embroidery on apron, whale tail ... white background, full body, standing, front view`（完整串在 pet_v2_gen.py::BASE_POSITIVE） |
| 负向 | pet_v2_gen.py::BASE_NEGATIVE（bad anatomy/hands、realistic、watermark、multiple girls…） |

---

## 四、后处理管线
```
pet_v2_postprocess.py：remove(session=u2net) → MinFilter(3) alpha 腐蚀(1px 防白边)
→ alpha bbox 裁切 → 长边 LANCZOS 缩到 64/96/128 → 方形画布底对齐粘贴 → assets/ai/
```
- venv **已装** rembg 2.0.81 + onnxruntime（本版无 isnet_anime，用 u2net 足够）

---

## 五、已定设计决策（用户拍板 2026-08-22）
1. **风格统一优先**：重出全新一套（v1 作废），色调可小幅变化但差异不能大
2. **动画帧后置**：先静态可用，`assets/frames/` 空目录占位（指南批三的 32 帧暂不生成）
3. **SLEEPY 加规则**：系统空闲 ≥ `sleepy_idle_minutes`（默认 10，config）→ 立绘休眠表情；优先级 饿 > 困 > 心情
4. **NEGLECTED 不压制情绪**：AI 立绘按全 mood 出图（neglected-happy = 勉强微笑，指南 2.11-2.15 语义）

---

## 六、代码现状（本会话已改，勿重复做）

| 文件 | 改动 |
|---|---|
| `pet/asset_provider.py` | `_mood_from_state(state, idle_s=None)` 加 SLEEPY 分支；`EmojiProvider(idle_fn=None, sleepy_idle_s=600)` 构造参数；emoji 表补 5 个 SLEEPY 条目；`get_frames` MOVE_TO 用 `.get() or base` 防 SLEEPY KeyError |
| `app.py` | `self.provider = EmojiProvider(idle_fn=lambda: self.sensors.idle_time, sleepy_idle_s=self.cfg.get("sleepy_idle_minutes", 10)*60)`（provider 构造移到 sensors 之后） |
| `config.example.json` | 加顶层键 `"sleepy_idle_minutes": 10` |

测试（CPython 3.12 绝对路径 `C:\Users\lenovo\AppData\Local\Programs\Python\Python312\python.exe -u`）：
- `spikes/test_v02_interaction.py` 13/13、`test_v03_fsm.py` 62/62、`test_v05_evolution_win.py` 16/16 + SLEEPY 专项断言全绿

**未改**：window 渲染（`set_sprite` 仍 `QLabel.setText(emoji)`）、AIArtProvider 不存在、`provider: ai` 未接线（app.py 硬编码 EmojiProvider）。

---

## 七、已知限制与坑

1. ~~neglected 表情不够丧~~（已按灰调+脏污区分，可接受；要更强需独立丧脸锚定或 ControlNet）
2. **情绪可读性**：0.44 版本特写可读（腮红/泪/张嘴/垂眼），64px 幼年形态会更模糊——如用户反馈辨不清，单情绪升 denoise 至 0.5 局部重出
3. **文件名踩坑**：SaveImage 自动加 `_00001_`/`_00002_` 序号；glob `*_happy_*_00002_` 会失配（happy 与序号共用一个下划线），用 `*_0000X_` 且取最新编号即可；确认新轮次编号：neutral _00003_、其余情绪 _00004_
4. **rembg 白边**：白裙边缘偶有 1px 残留，MinFilter 已腐蚀；若发现彩边改 u2netp 或加大腐蚀
5. **本版 ComfyUI 的 inpaint 不可用**：`VAEEncodeForInpaint`/`InpaintModelConditioning` 在本机 ComfyUI 0.33.0 都会整图重生成（noise_mask 语义问题），**改图勿用 inpaint**，用 PIL 合成移植（见 §三 方法②）或整图 txt2img 重出
6. **驱动环境**（换会话可能失效）：nvlddmkm 蓝屏旧因、610.88 已修；bash 里 `Start-Process -Verb RunAs` 必须 `-Wait` 保活否则 UAC 被父 shell 退出取消；NVIDIA 下载防盗链 curl 要带 UA+Referer（详见记忆 `nv-driver-bsod-61088`）
7. **测试环境**：win 端依赖只装在 CPython 3.12 绝对路径；跑测试加 `-u`（UTF-8 输出）
8. **755 号左翼注意**：主锚定左下蓝白鳍片是角色正常鲸尾鳍（非缺陷），任何合成/擦除勿动它

---

## 八、已完成与后续路线（v0.10 已交付）

**已完成并提交**：AIArtProvider 读取 assets/ai 静态图（缺文件降级 emoji 不崩）、window QPixmap 渲染分支（file→位图/emoji→文本）、app `_make_provider` 工厂（config provider=emoji/ai/commission）、get_frames 返回 AI 静帧单帧（防 emoji 闪烁）→ v0.10.0/v0.10.1/v0.10.2；`spikes/test_v10_ai_provider.py` 11/11 + 回归 v02/v03/v05 绿。

**剩余项**：
1. **动画帧（v0.10 后）**：`assets/frames/` 空目录待生成（指南批三 32 帧为素材源，但按用户决策"动画帧先留空"）；接帧播放需 window/app 侧接线（MOVE_TO 交替/FALL/CLIMB/EAT_MOUSE 咀嚼循环等），当前 get_frames 只回单帧
2. **约稿 provider**：config `provider: commission` 暂走 AIArtProvider 同路径（读 assets/ 同命名约定，§六第 3 级留接口）

**补图工具**（若用户要求）：改 `pet_v2_gen.py` 的 EMOTIONS/BASES 后 `bases`/`emotions` 命令重跑，再跑 `pet_v2_postprocess.py` 入仓；注意新轮次文件编号递增（取最新 `_0000X_`）。

---

*配套记忆（跨会话持久）：`ai-art-pipeline`（v2 管线+锚定）、`nv-driver-bsod-61088`（驱动/下载/UAC 坑）、`win-dev-environment-python`（测试环境）*
