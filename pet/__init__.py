"""桌宠核心包。"""

# 版本单一源：app.APP_VERSION 由本值派生（发版只改这里）。
# v0.16.4–0.16.6：成年转身与步态 G0–G4 合并入档 / ADULT 侧身行走 G5+G6
# （side_rig 默认启用）/ QSGTexture 释放改 deleteLater（G6 双骨骼退出段错误）。
# v0.16.7：侧身变形算法改进（链式权重、分量网格、手臂分段刚体 + 远侧手臂重绘）。
# v0.16.8：侧身步态按正常人关节曲线成形（reference_curves）+ 行走时双腿整体后移。
# v0.16.9：侧身腕关节弹簧 + 双手按侧身朝向重绘（近手背/远手心）+ 步频 1.2 Hz、远侧腿后移
# + 远侧肘/腕支点居中 + 尾巴整条重绘（尾裙间不再程序填充）+ 转回片段改用转出片段倒放。
# v0.16.10：合入 origin 的 rig 渲染循环三档自适应降频（16/33/66ms，与 side 编排合流）+
# 长运行 GC 治理（gc.freeze + 阈值放宽）——idle 稳态 CPU −19%～−35%，内存持平
# （原 origin 侧编号 v0.16.7，与本地 0.16.7 撞号，合并时顺延）。
# v0.16.11（本地 G7 线）：边缘卡住/侧身⇄正面闪 + follow 不实时修复——FSM 可达钳制
# _clamp_walk_x 与整窗在屏内的 Qt 层钳制统一（目标采样/贴边/IDLE/WALK），
# follow 目标 WALK 期间逐拍刷新 + 到达 4px/再起步 16px 滞后，app 意图按误差
# 比例给速（增益 1.2/s）+ 死区，SideLocomotion 会话 bounds 兜收步过冲。
# v0.16.11（origin ADULT 线，撞号）：修复 ADULT 转回正面后完全闭眼露出纯白眼白（眼部网格边界上界取值错误 + 左右眼闭合区不一致）。
# v0.16.12：ADULT 侧身行走期间各情绪统一用中性骨骼载体（不再退回旧两帧行走），neglected 用
# MultiEffect 去饱和保持灰暗；拖拽/离地/动作帧中断会话并恢复最新表情。
# v0.16.13：ADULT 视觉第二轮——衣身/袖口补全缺口修复、转身帧色边清理、膝部 Hermite 曲线过渡、
# 裙摆惯性限幅、尾鳍刚性约束、转身片段提速与反向归位缩短。
# v0.16.15 起 origin 线另有 FINAL F1–F7（正面蒙皮骨骼/长裙裙骨步态/Wan 3.0 转身片段等，
# 见 git log v0.16.15–v0.16.23）。合并后版本取 origin 侧。
# v0.17.0：细节打磨段启用（原约定跳过 0.17，现按用户指示回填启用）：聊天输入框
# 多行自适应（Enter 发送/Shift+Enter 换行、约 5 行封顶后内滚）、宠物右键菜单
# 置顶「聊天」直达面板、托盘 tooltip 热键提示、README 沿革表补 v0.17 行。
# v0.17.1：聊天多会话数据层——SessionStore（会话 id/标题/messages/history）
# + JSON 原子写持久化（data_dir/chat_sessions.json，重启恢复）；ChatBridge
# 多会话化（newSession/switchSession，在飞回复按发起会话落回不串 UI）；滚动
# 摘要解耦（只压缩喂 DS 的 history，UI 消息全量保留不再删行）。
# v0.17.2：顶栏会话切换器（标题 + ▾ 弹会话列表：相对时间/当前会话高亮）
# 与「＋」新建按钮；sessionList/sessionTitle/activeSid 三 Property 数据源；
# 修 load 恢复消息缺 rich 字段的 KeyError（data() 懒计算缓存）、时间戳提
# 微秒精度（同秒 touch 排序失效）。补注：0.17.1 轮漏改本行版本号（上轮
# 提交仍写 0.17.0），本轮一并修正。
# v0.17.3：会话重命名——弹层列表行 hover 出 ✎，点击行内编辑（Enter 确认/
# 失焦取消/空名拒绝保持编辑态），renameSession Slot（strip、不截断、touch
# 排序+落盘）；重命名输入框边框外包（mac 原生样式不支持 TextField 自绘
# background，告警+不生效）。
# v0.17.4：会话行右键菜单「重命名」（mac 习惯主入口，与 ✎ hover 按钮双
# 入口共存；Menu/MenuItem 用原生样式渲染）。
# v0.17.5：右键菜单加「删除会话…」（删除不可逆——居中确认弹层）；删活跃
# 会话自动切最近剩余会话、删最后一个自动新建空会话；在飞轮的发起会话被删
# 后回复丢弃不串会话；右键时行数据捕获到 Menu 属性（Popup 处理器内
# modelData 不可靠，离屏实测空 map——统一走 sessMenu.pendingSid/Title）。
# v0.17.6：每会话独立在飞（旧全局单飞——切会话后立即发消息被拒）+ 流式按
# 会话缓冲（切走隐藏/切回续显全量已流式段）；worker 信号经 partial(sid)
# 分发不依赖 sender()，pending 补账 sid 化（双在飞不串账），cancel 多
# worker 遍历收口、dying 列表化。
# 0.17.6 末：本地 G7 线与 origin/main（v0.16.12–v0.16.23，含 FINAL 线）合并入档；
# 新增三维化实验线 three_d/（wiki：方案 D01–D14 + 三份调研 + 设计 v0，render3d
# config 默认关闭不影响 2D 主线）与逆向还原工具（正/侧图板合成 + 由 2D pivot
# 推导 3D 骨架基准坐标，three_d/tools/）——three_d/ 最终随 0.17.6 段入库。
# 号段约定（用户指示）：0.18 保留给 three_d 实验线（旧注"自 0.18.0 起"继续
# 有效，未在 git 提号）；交互升级线自 0.19.0 起。
# v0.19.0：交互表现层（清单 docs/planning/交互升级-修改清单与版本规划.md）——
# 数值飘字 HUD（pet/floating.py：正增量上飘、负增量红字下沉，宠物头顶右侧、
# 点击穿透）；手动喂食咀嚼覆盖动画（复用 chew 帧源，临时态不进 FSM，
# _frame_tick 豁免 feed_chew）；气泡文案模板池（交互 × mood 分桶随机，
# config interaction.messages 平铺覆盖整池）；音效管线（pet/sound.py + sound
# 段默认关，缺 QtMultimedia/资产全程静默降级，资产 assets/sounds/ 后补）。
# v0.19.1：动词换代 + 活物感最小集——交互语义收拢 pet/interaction.py
# （InteractionOutcome 三态决策：正常/饱和拒绝/疲劳，呈现无关、3D 可沿用）；
# 右键菜单与文案池换代（喂食→喂点吃的、洗澡→梳梳毛、戳一戳→逗一逗，
# VERBS 单源；poke 增益 -8→+4，config_version v2 自动迁移旧默认）；喂食
# 饱腹 ≥reject_fullness（默认 92）拒绝进食、10 分钟内同类交互 ≥5 次增益
# 归零（interaction.reject_fullness / fatigue_times / fatigue_window_min）；
# 飘字新增 flat 中性态（拒绝/疲劳白字原地淡出）。
# v0.19.2：需求主动表达 + 状态可见化——proactive 新增数值触发源（fullness<
# 30/cleanliness<25/mood<20 求助气泡，每需求独立冷却默认 2h，quiet/DND 静默、
# 冷却只在真正发出时消耗）；托盘 tooltip 基础行+状态行合成（饱食28⚠ 心情65…
# ，与热键提示互不覆盖）+ 任何触线图标角落红点；右键菜单交互项触线加 ⚠ 后缀
# （位置固定不重排，阈值与 proactive.need_bubble 同源经 app 注入 window）。
# v0.19.3：聊天双向联动——发消息时 system prompt 追加宠物实时状态一行
# （pet_status_line，LLM 可自然说"我都饿了"，聊天↔养成从单向变双向）；
# 交互/拒绝/疲劳事件写 episodic 记忆（memory_fact + 日期戳前缀，memorize
# 同文去重=当日合并一条，重要度 0.2–0.35 靠按天衰减自然淘汰）；聊天情绪
# 判定 hungry 时宠物同步表达自己的需求（proactive.check_needs_now 绕过
# quiet 但守 DND，真饿了才开口、发了求助不叠共情气泡）。
# v0.19.4：节日与纪念日响应——mac 接入系统日历（pet/calendar_mac.py +
# adapter.get_festival_source：EventKit 读「中国节假日」订阅日历，农历节日
# 拿正确公历日期；TCC 授权一次，拒绝/未开订阅/缺绑定全程静默回落内置表，
# win 走内置表）；节日源优先→内置 MM-DD 表兜底，节日祝福附带心情 +5；
# 用户生日 config 录入（proactive.birthday "MM-DD"）当日一次专属祝福 +
# 心情 +10（生日优先于节日）。新依赖 pyobjc-framework-EventKit（requirements）。
# v0.19.5：修复节日/生日祝福——proactive 持久化 _festivaled 直存 date 致
# json.dump 抛 TypeError（只捕 OSError 异常逃出 poll），节日气泡与 0.19.4
# 心情奖励自 v0.14.47 起从未发出；festivaled/birthday_greeted 统一 ISO 串
# 落盘、读回 date（旧档兼容），生日标记落盘（重启不重发不重奖）、生日当天
# 整天不发节日、当日已发不再查日历源（mac EventKit 免每 30s 查询）。
# v0.19.6：修复 FINAL neglected 侧身行走变彩色——行走中喂食时咀嚼帧使蒙皮
# 不可见，_frame_tick 落帧路径把 feed_chew 换成循环旧 walk 帧（走完全程才停），
# 同期侧身会话接不上载体、locoNeglected 不置位；交互覆盖期间不抢播 walk 帧，
# 动作帧播放期间行走意图按 0 喂会话，帧收尾后下一拍接管载体与灰调。
# v0.19.7：neglected 动作帧灰调——咀嚼/吃鼠标/伸懒腰/打滚/摔落/眨眼帧各阶段
# 只有一套彩色版；rig 档新增场景属性 frameNeglected（与侧身行走共用
# mirrorNode 灰调层同参），frames 档 WindowBase 按同参去饱和入 pix 缓存
# （muted 入键，静态 neglected 立绘不二次压色）。
# v0.19.8：帧率分档（pet/perf.py 三档节拍表+auto 判档+过载降档，右键菜单
# 「流畅度」持久化 performance.frame_tier）+ 养成机制引擎（pet/needs.py
# 区段命名/饿脏加压心情/双高抵扣/交互落账收口，config.needs 可调）。
# v0.20.0：模型管理（pet/model_registry.py 注册表+pet/ui/model_dialog.py
# 对话框；托盘「切换模型/模型管理…」；llm.selected 记忆免启动弹选；
# 运行时切换 chat_bridge.swap_clients+proactive.set_client 热换客户端）。
# v0.21.0：mini-swe 集成（CLI 能力）——聊天侧新增 swe_task 委托工具，把需要
# 操作终端/命令行的请求交给软件工程助手（pet/swe_env.py 沙箱命令执行器 +
# pet/swe_agent.py 线性历史 agent 循环 + pet/swe_tools.py bash/submit）；
# 安全三件套（沙箱工作区 / 白名单 / 危险确认，confirm_fn fail-closed），
# config.swe 段默认关（enabled/workspace_dir/step_limit/wall_time_s/
# command_timeout_s/output_max_chars/no_network/block_patterns），步骤经
# bridge.sweStep 流式回显进聊天面板（role="swe" 等宽终端块，不进 DS 历史）。
# v0.22.0：mini-swe 上下文管理（pet/swe_context.py）——线性历史 token 治理：
# T1 观测瘦身（keep_recent 外的 tool 消息头尾摘录，确定性零成本）+ T2 轨迹
# 滚屏摘要（est token 超 budget_tokens → 最老一半可压步骤 LLM 压成单条
# [此前步骤摘要]，失败回退 T1，fail-open）；压缩只影响喂 LLM 的 messages，
# UI 步骤回显全量（摘要解耦哲学）；SweAgent(context_policy=) 注入，
# config.swe.context 段（budget_tokens 默认 24000 可按模型窗口调整）。
# v0.22.1：mini-swe 本地任务记忆（pet/swe_memory.py）——跨任务「本地上下文」：
# 任务档案 .swe/history.jsonl（上限 history_cap 条，坏行容错）+ 新任务相似
# 召回注入（复用 pet.memory._tokenize 单一实现，平滑 IDF + overlap<0.2 零相关
# 不注入 + 近期加分，top-k×recall_chars 拼前缀）+ NOTES.md 发现级记忆（system
# prompt 规则 5/6：旧记忆需验证后采信、持久发现写入 NOTES.md，任务起跑注入
# 头部）；归档存原始 instruction 防 memory 自噬膨胀；config.swe.memory 段。
__version__ = "0.27.7"
# v0.27.6：番茄自由度扩展「统计面」（wiki 设计-番茄自由度扩展技术方案 §4.5 E +
# 裁决 Q5）——纯数据面，无配置改动：`FocusSession._history` 逐日历史（跨日
# `_rollover` 归档「刚结束的那一天」，同日覆盖不追加；上限 400 天超限丢最旧；
# **只记有活动的天**）随 `focus_state.json` 落盘（schema v2 → **v3**；v1/v2 档案
# 无 `history` 仍照常加载）；`stats_range("today"/"week"/"month")` 只读聚合（今日
# 用 `_stats`、历史天用 `_history`，今日绝不算两遍；未知范围按 today）；工具
# `get_focus_stats(range)`（第 7 个番茄工具，任何相位含 IDLE 可查、零副作用、
# 无危险级）+ 托盘 tooltip 统计摘要行（`TrayManager.set_focus_stats_summary`，
# app 在 `_sync_focus_tray()` 里与相位/暂停态/模式四路正交同步）。**QML 面板
# 图表仍是占位**（§8，本版不做）。
# v0.27.3：番茄自由度扩展「模型层②（结构性）：暂停/继续」（wiki 设计-番茄自由度
# 扩展技术方案 §5.1/§5.2）——本线**最高风险版**（结构性 + `focus_state.json`
# schema v2）：
# ① 剩余量语义（pet/focus.py，§5.1）——L2 是"绝对 epoch deadline"，暂停不能靠
#    "停表"：新增 `_paused_at`（暂停时刻，None=未暂停）+ `_remaining_s`（冻结的
#    剩余秒）。`pause()` 把 `_deadline − now` 存进 `_remaining_s` 后置
#    `_deadline = None`；`resume()` 按 `now + _remaining_s` 重算绝对时刻，并
#    **回填已消耗部分** `_phase_started_at = now − (相位时长 − 剩余)`——不这样
#    resume 后 L3 实耗会丢掉"暂停前已专注的那段"（§5.2 坑 3）。新增只读属性
#    `paused`。`tick()` **最前置早退**（紧跟 `_refresh_dnd()`、在 IDLE 判断之前）：
#    暂停态睡多久都不迁移、不补算。`_focus_elapsed` 另补暂停态兜底分支（按
#    "相位时长 − 冻结剩余"作已消耗量），防别的路径在暂停态结算时丢数/多算。
# ② Q1 姿态/门禁语义——暂停期间 `active` **仍为 True**（严格 == state != IDLE，
#    L1 不被削弱；门禁久坐/吃鼠标/纠缠继续**静默**＝"我还在专注，只是暂时离开"）；
#    但 `pause()` 发 `focus_rest_off`（姿态回 idle 便于辨识）、`resume()` 在 FOCUS
#    段发 `focus_rest_on`（重新趴下）。**暂停时长不计入 `focus_sec`**。
# ③ Q2 隐式继续守卫——`complete()` 在暂停态**先** `resume()` 再 `_finish_focus()`
#    （不 resume 则 `_deadline=None` 不封顶，暂停时长会被算进专注），不额外计
#    打断/完成；`skip()`/`stop()`/自然迁移**不特判**——`_enter()` 与
#    `_enter_idle()` 各自清 `_paused_at`/`_remaining_s`，离开暂停态自动发生
#    （关键简化）。
# ④ **schema v2**（§5.2 三坑全解）——`_persist()` 写 `version: 2` + `paused` +
#    `remaining_s` + `phase_started_at`；loader 不读 version（坑 1，不能指望版本号
#    分流）→ **显式**加暂停分支，且在 `phase` 判定**之前**、**不做 deadline 三重
#    校验**（暂停态 `_deadline=None` 必过不了 → 会静默落回 IDLE 让暂停的番茄消失，
#    坑 2）。语义：**暂停跨重启仍然暂停、剩余时间不变**（app 关多久都算暂停）。
#    非暂停路径**优先**用档案里的 `phase_started_at`（存在且合理时），否则回退旧
#    反推公式——于是「**延长后重启**」也保真（v1 无该字段只能记回配置时长；此为
#    `_focus_elapsed` docstring 里挂了两版的注记，本版收口）。**v1 档案必须仍能
#    加载**：无 `paused` 字段 → 视为未暂停，走原路径。
# ⑤ 入口层（同版接线，非 UI 壳）——托盘「⏸ 暂停 / ▶️ 继续」**单项**、文案随
#    `paused` 切（`focus_availability` 的 `pause` = `active`、`pause_label`；
#    **IDLE 置灰**，符合 L1）→ `app._toggle_focus_pause()` → 按 `paused` 分发
#    `resume()`/`pause()`；可选热键 **id=5** `hotkeys.focus_pause`（**默认空串 =
#    不注册**，schema 刻意不设 minLength；`platform.start_hotkeys` 基类/mac/win
#    三处加 `pause_key`/`on_pause` 透传；app `_on_hotkey_fired` hid==5 分发）；
#    工具新增 `pause_pomodoro`/`resume_pomodoro`（IDLE/已暂停/未暂停各返回**可读
#    文案**而非静默失败）。
# ⑥ README：配置表补 `hotkeys.focus_pause`、运行期补暂停语义、记 schema v2 与
#    v1 向后兼容。本版**不做**：正计时（.4）、无休息（.5）、统计历史（.6）。
# v0.27.2：番茄自由度扩展「入口层② + 配置面」（wiki 设计-番茄自由度扩展技术方案
# §4.1 A / §4.4 D1/D3）——**启动时选时长 + 命名预设 + 配置热重载**：
# ① 托盘子菜单加「启动类」入口（pet/tray.py）——⏱ 25/45/60 分钟三档 +
#    「🕘 上次使用 N 分钟」+ 命名预设分组（`focus.presets`；**空数组时不建项、
#    不加分隔符**）+「⭐ 把 N 分钟设为默认」+「🔄 重载番茄配置」。**L1 要点**：
#    IDLE 下这些启动项**必须可用**（点击 = 用户显式 start，不是后台副作用），
#    与「跳过/延长/提前完成」（IDLE 必须置灰）是两类语义，可用性经
#    `focus_availability(state, last_work_min)` 的 `start_items` 与
#    `skip/extend/complete` 分开表达；预设分组走 `set_focus_presets` 动态重建
#    （插在「设为默认」之前），`apply_focus_menu` 对 `"presets"` 键接受**动作
#    列表**逐项施加。
# ② `FocusSession.start_preset(preset)`（pet/focus.py，D1）——**本轮覆盖**：
#    `_run` 只收录预设里显式给出的 work/short/long/cycles（缺省回落全局值，
#    schema 只 required name+work_min），`_enter_idle()` 时清除；`_work_min`/
#    `_short_min`/`_long_min`/`_cycles` 这些全局默认**一动不动**（四个有效值读口
#    `_work_minutes`/`_short_minutes`/`_long_minutes`/`_cycle_target` 统一收口）。
#    显式 `start()` 清 `_run`（= 新一轮）；`start(minutes, as_default=)` 语义零改动。
# ③ `reconfigure(cfg)` 热重载（D3）——把 `__init__` 的 `_num/_int` 解析抽成
#    `_apply_cfg(cfg)` 两处共用；只改配置派生字段，**绝不碰当前段 `_deadline`/
#    `_phase_started_at`/`_state`/`_completed_in_cycle`/`_stats`**（进行中的段不
#    被打断，新值在下一次 `_enter` 生效）。触发＝托盘「🔄 重载番茄配置」显式
#    触发（**不做**全局 config watcher）；热重载时"内存里刚记住的时长"优先于
#    文件里的 0，避免把刚记住的值冲掉。
# ④ `last_work_min`「记住上次」（§4.1）——`FocusSession.last_work_min` 只读属性
#    （0 = 无历史 → 回落当前默认，故恒 > 0）；app 在 `_sync_focus_tray()` 里
#    **检测到变化**才调 `pet.config.save_focus_work_min`（值未变零成本早返回），
#    故托盘/热键/聊天工具三入口**自动全覆盖**（工具 handler 直调 session，不经
#    app 回调，靠下一拍 tick 兜住）；检测基准在会话建立时以会话当前值初始化 →
#    IDLE 静置零写盘副作用。配置层（`focus.presets`/`focus.last_work_min` schema +
#    `save_focus_work_min` + config.example.json）同版落地。
# **本版仍无 `pause()`**（归 v0.27.3）、无正计时（.4）、无休息（.5）、无统计（.6）。
# v0.27.1：番茄自由度扩展「入口层①：运行中控制」（wiki 设计-番茄自由度扩展技术
# 方案 §4.2 B1/B2/B3 + §6）——**本版只为 v0.27.0 已存在的模型能力接线，不改模型**：
# ① 托盘子菜单「🍅 番茄钟」（pet/tray.py）——单一 toggle 项升级为四项：开始/停止
#    （文案与 toggle 语义逐字沿用）+ ⏭ 跳过当前段 + ➕ 延长 5 分钟 + ✅ 提前完成。
#    可用性走模块级纯函数 focus_availability(state)/apply_focus_menu(actions,state)
#    （只依赖 setText/setEnabled → 可脱离 QApplication 单测）；**L1：IDLE 时后三项
#    一律置灰**，✅ 仅 FOCUS 可用（休息段点了会凭空 +1 番茄），⏭/➕ 非 IDLE 可用。
#    状态提供者 set_focus_active(bool) → set_focus_phase(state)（bool 表达不了"休息
#    段禁用提前完成"），旧方法保留为兼容包装；focus.enabled=false 仍整菜单隐藏。
# ② 工具 skip_pomodoro/extend_pomodoro（pet/focus.py）——薄转发 skip()/extend()；
#    extend 的 minutes 1–180 与 start_pomodoro 同钳制口径（复用 _parse_minutes）；
#    描述如实写明 skip **不计**完成、extend **只延不缩**；IDLE 返回可读说明
#    （"当前未在番茄中"）而非静默失败；危险级与 start/stop 同级（默认 False=不打扰）。
# ③ 可选热键 id=4（默认**不注册**）——config hotkeys.focus_skip（schema **刻意
#    不设 minLength**："" 是合法默认值，且 hotkeys 段校验失败会整段回退默认，
#    拦空串会连用户自定义的 chat/spit/focus 一起丢）+ platform.start_hotkeys
#    基类/mac/win 三处加 skip_key/on_skip 透传（适配器内部改全关键字传参，防
#    透传位增多后位置错位）+ hotkey_mac/win 的 _ID_SKIP=4 注册 + app._skip_focus
#    落点 + hid==4 分发；冲突沿用既有 on_conflict 改键气泡。**id=5 留给 v0.27.3
#    的暂停，本版不占**。
# ④ 入口真实接线（非 UI 壳）：托盘三项与热键 id=4 → app._skip_focus/_extend_focus/
#    _complete_focus → FocusSession.skip()/extend(5)/complete()，每次后 _sync_focus_tray()。
# **本版不存在 pause()**；启动时长选择/预设（.2）、正计时（.4）、无休息（.5）、
# 统计（.6）一律不做。测试：tests/test_focus.py 79 → 106 全绿（本地不入库）。
# v0.27.0：番茄自由度扩展「模型层①」（wiki 设计-番茄自由度扩展技术方案 §4.1/
# §4.2/§5.2，本版只动 pet/focus.py + 文档，无入口/配置改动）：
# ① **L3 记账缺陷修复（正确性，前置）**——focus_sec 由「按配置 work_min 记账」
#    改为**按实耗**：_enter() 记 _phase_started_at，收口 elapsed =
#    min(now, _deadline) − _phase_started_at（deadline 封顶是等价性/不补算的
#    必要条件：不封顶则 +1s 拍长就记 1501、睡 2 小时记 7200，破坏 L2「过期即
#    迁移不补算」与「pomodoro 未延长时与旧口径逐值相等」两条硬要求）。重启
#    续算按 deadline − 相位时长反推起点（v1 档案无新字段，无 schema 变更；
#    "延长后重启"只能记回配置时长，待 v0.27.3 schema v2 一并携带）。
# ② start(minutes, *, as_default=False)（修 L4 / §9 Q6）——minutes **只影响这
#    一次**，不再永久改写 _work_min；仅 as_default=True 写回默认（托盘「设为
#    默认」走此路，落 config focus.last_work_min 归 v0.27.2）。保留 [1,180] 钳制
#    （>180 钳、<1/非法/NaN 当作没传）与「已运行改期重启、cycle 归零」。
# ③ extend(minutes=5)（§9 Q3）——非 IDLE 推移绝对时刻 _deadline 并立即落盘
#    （deadline 是 v1 档案字段，不落盘则重启丢延长）；延长部分靠实耗记账自动
#    计入，无需新字段；IDLE no-op（L1）。单次钳 [1,180]、非法/非正 no-op（不做缩短）。
# ④ complete()（§4.2 B3）——提前完成走 _on_focus_complete()（completed/streak/
#    实耗/mood 养成联动全走）后进休息，与 stop()（一概不计）明确对照；IDLE 与
#    休息段 no-op（休息没有「提前完成」语义，提前结束用 skip()）。
# ⑤ skip() 逻辑零改动，补 IDLE no-op 与「跳过不计 focus_sec」用例；本版**无
#    pause()**（与 v0.27.3 暂停继续同版落地，§9 Q2 的隐式 resume 守卫一并不实现）。
# 测试：tests/test_focus.py 52 → 79 全绿（本地不入库）；全量失败集合不新增
# （既有 16 条漂移 + 他线在制 1 条）。
# v0.23.7–v0.23.10：天气线增量（回填 0.23 线，接着 patch 走）。
# v0.23.7 定位+兜底核心：IP 定位（opt-in+consent，ip-api 免费档 HTTP）+ wttr.in
#   备用源兜底（Open-Meteo 偶发超时→立即重试→切 wttr，WWO 码表）+ 工具摘要 data
#   字段不透传；config 增 locate/consent/save_weather_location。
# v0.23.8 接线：proactive set_weather_fn（定位开关变更后重建 service）+ 气泡时长
#   duration_s 透传；app 定位回调 + scheduler bubble_fn 落点。
# v0.23.9 权限页：perm 定位项（隐私同意非系统特权）+ 页内确认 Dialog + 自绘按钮。
# v0.23.10 收尾：README weather 节补 locate/consent + 版本沿革。
# v0.24.6：v0.24「主动纠缠互动」线收尾——README 补「主动纠缠（可选）」节（五重门
# 判据表 / attention.* 配置表 / 托盘「安静模式」口径 / **「烦人度」坦诚说明**：
# 罐头池 ≥2/3 + LLM ≤1/3 的实情、默认每小时 2 次为「未经实机浸泡调校的从严起步
# 值」、依据标注为说明性而非已量化承诺）+ 版本沿革补 v0.24 行（v0.23 之后原本直接
# 跳到 v0.25.0）。本版**无代码改动**，只提版号 + 文档。
# 遗留（交由实机验收）：0.24.5 的托盘「安静模式」点击、静默条件逐条手测、端到端
# 纠缠触发观察；浸泡 1–2 周后按体感调 hourly_limit / backoff_s。
# v0.24.5：主动纠缠「配置与开关」——config **三处同步**（_SECTION_SCHEMAS +
# _SAFE_DEFAULTS + config.example.json；additionalProperties:False 陷阱见调查报告
# 缺口 3，behaviors 用 enum 挡下拼错的行为名——写错的行为只会静默不触发）+ 托盘
# 「安静模式」checkable QAction（整段仿 autostart/voice：blockSignals 防 setChecked
# 回环，勾选写 ``attention.pester.enabled=false``）。**两道粒度不同的闸**：
#   attention.enabled=false        = 整段陪伴系统总闸（不演化也不纠缠）
#   attention.pester.enabled=false = 安静模式（**只 gate 行为**：attention 数值与
#                                    关系记忆照常演化，故关掉再开是「接着算」）
# 引擎侧 AttentionConfig.pester_enabled 即「门 0 总闸」；set_pester_enabled 用
# dataclasses.replace 换 frozen cfg 的单字段以**保留全部运行时状态**（数值/连续
# 被无视计数/退避截止/频控窗口/上次行为），托盘勾选因此即时生效且不重置进度。
# 键位以技术方案 §6 的 pester.* 为准，同时回退兼容 0.24.0–0.24.4 的顶层写法；
# save_pester_enabled 只写这一个键（对齐 save_voice_enabled 的「只动传入键」）。
# v0.25.7：番茄线文档归位（**无代码改动**）——README「版本沿革」的 v0.25.4/.5/.6
# 三行归位到表内（0.25.4 起误追加到文件末尾「参与贡献」之后，导致表内只到
# v0.25.3），并补本行；pet/focus.py 与各接线文件与 0.25.6 逐字节一致。
# v0.24.4：主动纠缠「触发链 + 护栏接线」——pet/proactive.py 挂 poll 轮询：
# 五重门判定全在 AttentionEngine.should_pester（0.24.0），本版只负责「信号接入
# + 各归其位」：深夜静默复用 poll 既有 quiet 早退；DND/FOCUS 复用 _dnd_active/
# _focus_active（与 0.25.3 番茄门禁同一把锁，fail-closed 绝不误扰）；全屏/视频
# 复用既有双平台探测；**breakpoint 时机门控**（系统空闲 ∈ [30s,10min] 视为任务
# 边界——Adamczyk & Bailey 2004：边界是低打断成本时点；过短=仍在连续操作、
# 过长=人已离开）为 v0.24 新接入的唯一时机信号（窗口切换信号 win 侧留坑）。
# 行为①run_to_cursor 驱动 FSM 奔向光标求摸头、行为②bubble_whine 只开口，两者
# 都经 _emit_bubble 发一条撒娇气泡（宠物先开口=纠缠的存在感）。LLM 文案走
# _PesterVoiceWorker 后台线程（chat_once 读超时 120s，主线程绝不直调；实测
# PySide6 把非 QObject 普通类的绑定方法排队回主线程），隔离上下文用专用
# PESTER_SYSTEM_OVERRIDE + tools_override=None；失败/离线回落罐头且**不占**
# ≤1/3 额度（比例统计诚实）。shutdown 一并收口在飞 worker（防 QThread 被 GC
# 崩溃）。app 侧 _pester_context（idle/全屏/视频/DND）与 _pester_llm_fetch
# （独立决策客户端，不与聊天共享 _resp）。
# v0.25.6：番茄收尾——托盘 tooltip「🍅×今日完成数」（TrayManager.
# set_focus_count + 纯函数 tooltip_text；0 不显示、与 v0.19.2 状态行互不
# 覆盖，app._sync_focus_tray 每拍幂等推送）+ README 补 focus.* /
# hotkeys.focus 配置表与运行期口径（含「默认关 = 零输入抑制」安全红线）。
# v0.25.5：番茄统计与养成——app 注入 store（完成番茄 store.update(mood=
# +mood_gain) 走 observer，托盘状态行自动刷新）+ state_path=data_dir/
# focus_state.json（当日完成数/累计专注秒/打断数/连续天数，跨日归零、
# streak_days 按当日有完成维护）；重启续算按 deadline 恢复进行中节律（5min
# 宽限内且同日），恢复到 FOCUS 段时补发 focus_rest_on 陪伴姿态（全屏静默
# 守卫照旧），IDLE/休息段不补发。
# v0.25.4：番茄休息执法（可选，break_enforce 默认 false = 零输入抑制红线）
# ——app 注入 idle_fn（系统空闲 <2s 视为有键鼠活动）；休息段开始 30s 窗口内
# 活动累计每 10s 记一次 breaks_cut、累计 ≥20s 才温和气泡一次（§9 拍板），
# 不接 mouse_lock、不做任何输入抑制。同版修 FocusSession._emit：文案池元组
# 随机取一条 str（旧版整池交 bubble_fn，与 text: str 契约不符）。
# v0.26.5：占位音不再遮挡正式素材——占位音改用带标记的文件名
# ``<key>.placeholder.<ext>``（新增常量 VOICE_PLACEHOLDER_MARK），
# VoiceAssetLoader 分两轮探测：先把 5 个正式后缀全试一遍，再试占位后缀，
# 故**任何后缀的正式录音都稳赢占位音**（修正"录了 .m4a 却仍听到占位 .wav"
# 的坑，用户 2026-10-09 拍板）；ensure_placeholders 改写
# <key>.placeholder.wav，已有正式素材或已有占位的 key 一律跳过（幂等）；
# assets/voice/README.md 同步新命名、探测序与实测结论。
# v0.24.3：主动纠缠「行为② 气泡撒娇」——pet/pester_lines.py（纯共享层，零平台库）：
# 三级罐头池按纠缠阶段分档（low_attention 低陪伴度撒娇 / pat_failed 求摸头未遂 /
# ignored 被无视委屈；语气逐级**收敛**，对齐退让铁律——委屈而非指责/升级）+
# LLM 抽样 ≤1/3 **累计硬上限**（3 选 1 确定性节拍，非随机抖动，比例可日志核查）+
# 隔离调用专用 system_override（鲸鱼娘撒娇人设：禁用猫狗式行为与真整蛊）——调用方
# 以 tools_override=None 装配，本模块仅注入 llm_fn 故零依赖 + 失败/离线/坏返回一律
# 回落罐头（拔网线不崩；失败不计入 LLM 用量，比例统计诚实）+ 「N 天没理我」事实注入
# （days_since 由 RelationMemory.last_interact_at 算出——Anchor 审计：模型轨迹记忆
# 仅 44.4% 接近随机不可靠，故事实由状态层给、模型只负责措辞；无记录不编造）+
# config `attention.pester.lines` 逐类覆写。发射接线（_emit_bubble 限速管线）归 0.24.4。
# v0.25.3：番茄陪伴 + 互斥——behavior.py 增冻结模式 _FOCUS_REST
# （focus_rest_on/off 事件：停 WANDER/物理/攀爬、期间忽略 motion_mode 切换、
# 退出按存量 _motion_mode 恢复，零新立绘走 idle 静帧）+ app._LOCO_BREAK_MODES
# 收编；proactive.py 注入 focus_fn 门禁（专注运行中静默久坐提醒/吃鼠标、链式
# 唤醒顺延 10min，只挡新发起不打断已在进行的抑制；fail-open + IDLE 恒放行）；
# 0.24 纠缠经既有 app._focus_state duck-typing 自动静默。
# v0.26.4：语音来源分类修正 + 降级回复朗读——app._auto_bubble 增 keyword-only
# ``source``（默认 "proactive"：它是 pet/proactive.py 注入的 bubble_fn(text)
# 落点，即主动关怀的归属），而「用户动作触发的即时反馈」——聊天情绪
# _apply_chat_emotion、离线提示 _on_chat_offline——显式传 source="system"，
# 否则会被「主动朗读」开关默认静音；chat_bridge._on_failed 降级回复同样朗读，
# _on_offline 刻意不朗读（同事件的气泡侧已念一次，防重复）。测试补三条分类
# 不变量：默认值必须 proactive / 每个调用点必须显式声明 source / done+failed
# 挂钩而 offline 不挂钩。
# v0.25.2：番茄专注入口三件套——托盘「🍅 开始/停止番茄钟」动态项
# （TrayManager.set_focus_callback/set_focus_visible/set_focus_active，
# focus_label 纯函数）+ 全局热键 id=3（hotkey_mac/hotkey_win 的 start 增
# focus_key/on_focus，空键=入口未挂载不注册；冲突沿用 on_conflict 改键
# 气泡）+ platform 三处 start_hotkeys 透传（win RegisterHotKey 仍只在热键
# 线程消息循环内注册）；app 装配 FocusSession（now_fn/bubble_fn/fsm_event_fn/
# sound_fn/dnd_fn/fullscreen_fn/cfg 注入，每拍 _tick 调 tick）、_toggle_focus
# 收敛托盘/热键/工具三入口、ToolRegistry 注册 start_pomodoro/stop_pomodoro；
# focus.enabled=false → session=None + 托盘项隐藏 + 空热键，零接管（§1）。
# v0.25.1：番茄专注配置段与测试口径纠正——config 增 focus 段（enabled
# 默认 true=入口可见但**绝不自动运行** / work_min 25 / short_min 5 /
# long_min 15 / cycles 4 / break_enforce 默认 false=零输入抑制安全红线 /
# mood_gain 3）+ _SECTION_SCHEMAS focus 严校验（additionalProperties False，
# 非法值整段回退默认并告警）+ hotkeys.focus（mac ⌘⌥F / win Ctrl+Alt+F，
# id=3）+ config.example.json 模板同步；tests/test_focus.py 遵 .gitignore
# 「本地测试不入库」约定转本地（纠正 0.25.0 的 git add -f）；focus.py
# docstring 的「win 侧 CI 单测 + lint」表述更正为「平台库-free 代码约束 +
# 开发者本地单测」（本仓无 CI，见技术方案 §1/§6 更正）。
# v0.24.2：主动纠缠「行为① PESTER 奔向光标求摸头」——pet/behavior.py FSM 加
# _PESTER 态（两段式：直线奔向光标复用 _follow_speed/到达阈 10px/超时 5s →
# 到达转「待摸头」静姿态，窗口期 30s）+ handle_event "pester"/"pester_off" +
# 模式切换保护（pester 期间忽略 motion_mode，仿吃鼠标 v0.7.4 教训）+
# AssetProvider._FRAME_SPECS 加 "pester" 键（帧未产 → 缺帧降级静帧）+
# app 层消费（_frame_tick 两段帧：奔向走 walk/到位走静姿态帧；_tick 进出记账；
# _interact 摸头闭环 on_pester_answered → attention 回满 + 走 needs pet 通道；
# 窗口超时 mode 回落 → on_pester_ignored **只加深退避、绝不升级**）+ 建
# AttentionEngine 实例与 Stage 分档衰减回写 PetState（0.1 粒度防存盘抖动）。
# 触发链（poll 挂 should_pester 五重门）归 0.24.4；单测 tests/test_behavior_pester.py。
# v0.26.3：语音线接线（app.py/tray.py/bubble.py/ui/chat_bridge.py，config.py
# 加 save_voice_enabled）——两路来源分流：气泡 on_show 回带 source
# （proactive=自动路径 _auto_bubble / system=用户主动路径直调），聊天回复走
# chat_bridge.on_reply_done（聊天面板不走气泡，调查 F1，故必须单独挂钩）；
# VoiceService 装配（配置快照 + 平台注入位 get_asset_player/get_tts_engine）；
# 托盘「语音播报」开关（即时生效 + 写回 config，blockSignals 防回环）；
# 静态台词→罐头 key 精确映射表（动态 f-string/随机池刻意不映射，回落系统 TTS）。
# v0.26.2：语音配置段——config 增 voice 段（proactive_speak/dedup_sec/
# asset_root + tts 子段 enabled/provider/voice/rate/speak_chat/quiet_hours，
# 全部默认关，对齐 sound 先例）+ _SECTION_SCHEMAS 增 voice 校验（provider
# 枚举限死 asset/system、quiet_hours 在 voice 与 voice.tts 两处都收、
# additionalProperties 严校验）；非法值回退默认，坏配置不致启动失败；
# config.example.json 模板同步。
# v0.26.1：语音平台层与注入位——pet/voice_mac.py（MacAssetPlayer 走 AppKit
# NSSound 播放 wav/aiff/m4a/mp3/caf 系统 codec，持强引用防 GC 静音、play 前
# 先 stop 防叠音；MacTtsEngine 走系统 `say` 子进程，list 传参不开 shell、
# terminate→wait→kill 兜底、失败静默且文本不落日志）+ PlatformAdapter 四个
# 注入位（get_asset_player/get_tts_engine/get_voice_recorder/
# get_mic_permission）：基类 None/unknown 即 win 留坑，mac 侧惰性构造并按
# (voice,rate) 缓存，配置变更下次调用即重建。平台调用失败一律静默降级。
# v0.26.0：语音线共享层核心（pet/voice.py，wiki 设计-语音模块技术方案）——
# 罐头语音音包接入（VoiceAssetLoader 按序优先探测多后缀 [.wav/.aiff/.m4a/
# .mp3/.caf]，只认 mac 系统原生 codec；未命中回落动态 TTS）+ 占位音频生成
# （write_placeholder_wav/ensure_placeholders，程序生成短提示音，不含任何
# 他人录制音色，正式音包同名替换即生效）+ SpeakGate 门禁（enabled/来源开关/
# dnd/全屏/深夜/去重）+ SpeakQueue 串行队列（重入抑制、stop 清队）+
# VoiceService 门面（provider 路由 asset/system，注入对象缺失即静默降级）。
# 平台库-free（mac/win 同代码），平台能力后续经 PlatformAdapter 注入位提供；
# config.voice 段全部默认关（对齐 sound 先例）。
# v0.24.1：attention 接入 pet_state——PetState 加 attention 字段（默认 70，第四
# 独立数值）+ SCHEMA_VERSION 1→2 + migrate_v1_to_v2 旧档补默认（不丢档）+
# _NUMERIC_FIELDS 加 "attention"（apply_decay 才对其生效，调查缺口 2）+
# 托盘状态行陪伴段（低陪伴度显示「有点想你」）。
# v0.24.0：主动纠缠互动核心（pet/attention.py，wiki 设计-纠缠互动技术方案）——
# attention 第四独立数值 + 纠缠触发决策纯逻辑引擎（零平台库，mac/win 同代码）：
# Stage 分档衰减（YOUNG 4/ADULT 2.5/FINAL 1.5 每小时）+ 交互增益（摸头/逗一逗/
# 聊天）+ 五重门 should_pester（静默→breakpoint 时机→focus 防御探测→阈值→
# 退避→频控/日频）+ 被无视指数退避（300→1800→7200 封顶，退让而非升级铁律）
# + 关系记忆 RelationMemory（昵称/摸头计数/last_interact_at，显式结构化）；
# 单测 tests/test_attention.py 21 例全绿。数值本版存引擎内部，0.24.1 接
# pet_state 持久化（migrate v1→v2 + _NUMERIC_FIELDS 加 attention）。
# v0.23.0：天气感知与提醒（pet/weather.py，wiki 设计-天气感知与提醒 +
# 设计-天气模块技术方案）——Open-Meteo 免 key 数据源（非商用条款 + CC BY
# 4.0 署名，README「Powered by Open-Meteo」）；手动城市默认（city 留空=
# 功能静默，2026-10-08 拍板），geocoding 坐标随缓存落盘；daemon 线程拉新
# + 内存/磁盘双缓存（weather_cache.json 原子写）+ 离线回读 stale 标注 +
# 429/5xx 指数退避（1→5→30min，连续 5 次本日放弃）；三消费方一次落地：
# ①主动提醒挂 ProactiveScheduler（今日首次新快照评估，带伞/降温/高温/
# 寒潮阈值可配，每类每日一次落 proactive_state 去重、stale/缺数据/跨日
# 快照不触发、DND/深夜静默，不保证性措辞）；②LLM 工具 get_weather（只读
# 缓存不拉网，stale 透传让模型自行说明）；③Sensors.weather 字段位留给
# 0.24 纠缠线行为调制。本线按 0.23.0→0.23.4 分段提交（核心/config/提醒/
# 接线/提版），patch 从 0 起每次 commit +1。
