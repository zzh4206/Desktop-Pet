# AI 立绘交接文档（图片侧）

> 交接日期：2026-08-22
> 关联：`桌宠立绘生图指南.md`（需求源，74 张清单）/ `设计思路.md` §六（provider 三级）/ `版本规划.md` v0.10
> 本会话已完成：驱动修复 + 全新 v2 全套 30 张静态立绘生成与入库 + SLEEPY 显示规则代码
> **下一步工作对话的入口**：本文档 + `D:\Desktop-Pet\pet\asset_provider.py` + `D:\AI\ComfyUI\pet_v2_gen.py`

---

## 一、当前状态（已完成 ✅）

| 事项 | 状态 |
|---|---|
| 显卡驱动 nvlddmkm 蓝屏修复 | ✅ 592.01 → **610.88 WHQL**（WMI 版本 32.0.16.1088），升级后生图正常 |
| 旧 v1 立绘（pet_sprites_final 44 张/旧 47 组） | ⛔ **作废**（用户判定风格不一致），勿再引用 |
| v2 全新一套静态立绘 | ✅ **30 张齐**（3 阶段×2 分支×5 情绪），画风严格一致 |
| 后处理（去背/裁剪/缩放/底对齐） | ✅ 全部完成，已入仓库 `assets/ai/` |
| 动画帧 | ⏸ 按用户决策先留空（`assets/frames/` 只有 `.gitkeep`） |
| SLEEPY 显示规则 | ✅ 已实现（代码+config+测试，见 §六） |
| NEGLECTED 不压制 | ✅ 30 张按全情绪生成齐；AIArtProvider 接入时按 (stage,branch,mood) 全量取图即可 |
| AIArtProvider 接入（v0.10 核心） | ⬜ 未做，见 §八 |

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
  masterB_seed555_00001_.png                         # ★ 主锚定图（一切图的本源）
  {stage}_{branch}_{mood}.png / _00001_/00002_ 编号  # 逐张原图（_00002_ 为情绪 0.44 版）
  _sheet_master.png _sheet_bases_v3.png _sheet_{stage}.png _final_check.png  # 评审图
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
1. **txt2img 主锚定**：`adult_healthy_neutral`，4 候选目检挑最佳 → 定稿 `masterB_seed555_00001_.png`（深蓝→亮青渐变发、鲸鱼鳍耳、腿后鲸尾可见、正面站姿、白底）
2. **形态底图**（5 张）：主锚定 img2img 派生，denoise 0.44（healthy）/ 0.58 方向·实选 v3（neglected，灰调+脏污），全程 **DERIVE_SEED=777**
3. **情绪**（24 张）：各形态底图 img2img 派生，**denoise 0.44**（0.34 版表情太淡已弃），seed = 777+i，只动脸部词
4. 所有图与主锚定派生距离 ≤2 步 → 发色/线稿/上色习惯强统一

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

1. **neglected 表情不够丧**：img2img 从主锚定笑脸派生，denoise ≤0.58 脸不易变丧；3 轮调参后接受"灰调+脏污围裙"区分（v3 选稿）。若用户仍不满意：可做独立"丧脸"主锚定或上 ControlNet
2. **情绪可读性**：0.44 版本特写可读（腮红/泪/张嘴/垂眼），64px 幼年形态会更模糊——如用户反馈辨不清，单情绪升 denoise 至 0.5 局部重出
3. **文件名踩坑**：SaveImage 自动加 `_00001_`/`_00002_` 序号；glob `*_happy_*_00002_` 会失配（happy 与序号共用一个下划线），用 `*_00002_.png` 即可
4. **rembg 白边**：白裙边缘偶有 1px 残留，MinFilter 已腐蚀；若发现彩边改 u2netp 或加大腐蚀
5. **驱动环境**（换会话可能失效）：nvlddmkm 蓝屏旧因、610.88 已修；bash 里 `Start-Process -Verb RunAs` 必须 `-Wait` 保活否则 UAC 被父 shell 退出取消；NVIDIA 下载防盗链 curl 要带 UA+Referer（详见记忆 `nv-driver-bsod-61088`）
6. **测试环境**：win 端依赖只装在 CPython 3.12 绝对路径；跑测试加 `-u`（UTF-8 输出）

---

## 八、下一步（主要工作对话从这开始）

**v0.10 接入 AIArtProvider**（按 `设计思路.md` §六 与 版本规划 v0.10 Must）：
1. `asset_provider.py` 新增 `AIArtProvider`：`get_static` 读 `assets/ai/{stage}_{branch}_{mood}.png`（skin 非 default 加 `_{skin}` 后缀再找），缺文件/IO 异常 → 降级 EmojiProvider
2. `window.py::set_sprite` 加图片渲染分支：`SpriteRef.path` 为文件路径（os.path.exists）→ QPixmap 加载+按 label 尺寸缩放显示；emoji 文本分支保留（降级用）
3. `app.py`：`self.provider = _make_provider(self.cfg.get("provider", "emoji"), ...)`（emoji/ai/commission）——注意 EmojiProvider 的 idle_fn 注入方式要保留
4. `config.example.json` 的 `"provider": "emoji"` 切 `"ai"` 实测：不同 (stage,branch,mood) 出不同图；删图模拟降级不崩；切回 emoji 养成/物理零改动（git diff 验证）
5. 动画帧（后续）：`get_frames` 目前约定降级 emoji；接帧播放需 window/app 侧接线（MOVE_TO 交替/FALL/CLIMB/EAT_MOUSE 咀嚼循环等，指南批三为素材源）

**补图工具**（若用户要求）：改 `pet_v2_gen.py` 的 EMOTIONS/BASES 后 `bases`/`emotions` 命令重跑，再跑 `pet_v2_postprocess.py` 入仓。

---

*配套记忆（跨会话持久）：`ai-art-pipeline`（v2 管线+锚定）、`nv-driver-bsod-61088`（驱动/下载/UAC 坑）、`win-dev-environment-python`（测试环境）*
