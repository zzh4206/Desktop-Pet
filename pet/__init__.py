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
__version__ = "0.20.0"
