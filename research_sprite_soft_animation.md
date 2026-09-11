# 纸片立绘"软动效"程序化技术调研报告

> 目标：把静态 PNG 部件（绕 pivot 刚性旋转 + 固定正弦）升级为有软感、有生命力的动效，约束为程序化 / 确定性 / 零新生成像素 / 可脚本批量生成 / 不引入 Live2D 手工绑骨。技术栈 Python + PySide6 + Qt Quick(QML)，可选 ShaderEffect。
>
> 标注约定：**[可靠来源]** = 官方文档 / 原作者公式 / 教科书物理；**[经验值]** = 社区实践或本报告给出的可调起点，需按美术效果微调。

---

## 0. 一分钟结论（推荐落地路径）

1. 把每个部件的角度当作一个 **一维二阶弹簧-阻尼系统**（§1），目标角 = 动画目标角或重力静息角。这是"软感"的核心，替代现在的 `amp·sin(2πt/period)`。
2. 用 **semi-implicit Euler** 积分即可（帧率 60 下对中小刚度稳定、代码 3 行）。若要"无论 dt 多大都稳"，用 §1.5 的 **halflife 化临界阻尼闭式解**（Daniel Holden 版，可直接抄）。
3. 突发冲量 = 直接给速度加一个冲量 `v += J`（§1.4）。
4. 风/呼吸 = 在目标角上叠加 **低通后的 Perlin 噪声或非谐波多正弦**（§2）。
5. 父子链滞后 = 子部件把"父部件当前角"当目标，用自己的弹簧跟随（§3），天然产生 follow-through + 回弹，无需手工调时间轴。
6. 软弯曲二选一：**CPU 铰链链**（把长部件切成 N 段子精灵，每段按衰减权重旋转，零新像素、全后端兼容、可脚本化）优先；需要连续弯曲时再用 **ShaderEffect 顶点着色器**（§4，注意 Qt 6 的 `.qsb` 管线与 software 后端不支持 shader 的坑）。

---

## 1. 二阶弹簧-阻尼（spring-damper）

### 1.1 控制方程（单位质量、绕 pivot 的 1 自由度角度）

把角度 `x`（弧度）、角速度 `v` 当作标量，目标角 `g`、目标角速度 `q`（通常 `q=0`）：

```
a = s * (g - x) + d * (q - v)        # 加速度：弹簧项 + 阻尼项
```

- `s` = stiffness（刚度），`d` = damping（阻尼），均为"单位质量"量纲。**[可靠来源：Daniel Holden / UWA 译文]**

### 1.2 离散积分：semi-implicit Euler（务必"先速度后位置"）

```python
def spring_step(x, v, g, s, d, dt, q=0.0):
    v += dt * s * (g - x) + dt * d * (q - v)   # ① 先更新速度
    x += dt * v                                # ② 再用新速度更新位置
    return x, v
```

为什么这个顺序：显式 Euler（先位置后速度）对弹簧会**能量递增而发散**；semi-implicit 是辛积分器，能平均保能量、稳定得多。**[可靠来源：Gaffer On Games《Integration Basics》]**

Gaffer 的对照实验参数：`m=1, k=15, b=0.1`（欠阻尼振荡）在 `dt=1/100` 下 semi-implicit 稳定且接近解析解。**[可靠来源]**

### 1.3 刚度/阻尼的典型取值（可直接用）

物理量换算（Daniel Holden 给出的两个桥接公式，**[可靠来源]**）：

```
s = (2π·f)²                  # f = 希望的自振频率(Hz)
d = 4·ln2 / halflife        # halflife = 包络衰减一半的时间(秒)，ln2=0.69314718056
```

由此可得：

| 想要的感觉 | f (Hz) | s (rad²/s²) | halflife (s) | d (1/s) |
|---|---|---|---|---|
| 快速响应的头发/尾巴 | 2.0 | ≈158 | 0.10 | ≈27.7 |
| 柔软飘动的裙摆 | 1.0 | ≈39.5 | 0.20 | ≈13.9 |
| 很轻的呼吸/慢摆 | 0.5 | ≈9.9 | 0.40 | ≈6.9 |

**阻尼比** `ζ = d / (2√s)`：`ζ < 1` 欠阻尼（会过冲回弹，软感来源）、`ζ = 1` 临界（最快无振荡）、`ζ > 1` 过阻尼（慢无振荡）。**软但不乱抖**推荐 `ζ ≈ 0.5 ~ 0.8`。**[经验值]**

> 说明：f 与 halflife 是"用户友好参数"，s、d 是底层参数。上面的数值由权威换算公式推导；`ζ 0.5~0.8` 这个区间是经验建议。

### 1.4 滞后 + 回弹 + 突发冲量

- **滞后/回弹**：弹簧本身就有滞后（目标动了，当前值慢慢追）与回弹（欠阻尼时过冲再回来）。想更"跟手"就调小 halflife / 调大 f。
- **突发冲量**：外部事件（点击、甩头、落地）不要改目标角，直接给角速度加冲量：

```python
v += J          # J = 角速度冲量(rad/s)，例如 +2.0 表示快速往正方向甩一下
```

之后弹簧自动振荡并衰减，天然产生"甩动-回弹-静止"。**[经验值]**（冲量大小 J 一般 0.5~3 rad/s 量级）

### 1.5 帧率无关、绝对稳定的闭式版本（推荐生产用）

semi-implicit 在"刚度大 / dt 大"时仍可能不稳。Daniel Holden 的 **critical spring damper（halflife 参数化）** 是闭式解，任何 dt 都稳，且只用 1 个直观参数：

```python
LN2 = 0.69314718056

def halflife_to_damping(halflife, eps=1e-5):
    return (4.0 * LN2) / (halflife + eps)

def simple_spring_damper_implicit(x, v, x_goal, halflife, dt):
    y = halflife_to_damping(halflife) / 2.0
    j0 = x - x_goal
    j1 = v + j0 * y
    eydt = 1.0 / (1.0 + y*dt + 0.48*(y*dt)**2 + 0.235*(y*dt)**3)  # fast_negexp
    x = eydt * (j0 + j1 * dt) + x_goal
    v = eydt * (v - j1 * y * dt)
    return x, v
```

这是临界阻尼（无振荡）。若要"过冲回弹"的欠阻尼，用同一文章里的 `spring_damper_implicit(x, v, g, q, frequency, halflife, dt)` 完整版（欠阻尼/临界/过阻尼三分支）。**[可靠来源：Daniel Holden，源码见 Spring-It-On 仓库]**

---

## 2. 摆锤 / 风吹（sway）

### 2.1 重力摆（阻尼摆）

真实摆：`θ'' = -(g/L)·sinθ`，小角度近似 `θ'' = -(g/L)·θ`，角频率 `ω = √(g/L)`，周期 `T = 2π√(L/g)`。加阻尼与外力：

```
θ'' = -(g/L)·sinθ - c·θ' + F(t)/m
```

**对 2D 立绘来说，"重力摆"通常退化为"绕 pivot 的阻尼角弹簧 + 一个偏置的静息角"**——即 §1 的方程，把目标角设成重力静息角 `g = θ_rest`，`s = g/L`（等效）。这样不必显式积 sinθ，代码统一走弹簧。**[可靠来源：教科书物理 + 经验简化]**

### 2.2 风噪声（Perlin / 多正弦叠加）

**Perlin（推荐用 Improved Noise，可直接抄实现）**：`noise(x,y,z)` 输出约 [-1,1] 的平滑伪随机。把时间映射进噪声域，叠加 2~3 个八度：

```
wind(t) = A * (  noise(t*f1, s1, 0)
               + 0.5*noise(t*f2, s2, 0)
               + 0.25*noise(t*f3, s3, 0) )
```

- `f1≈0.1~0.3 Hz`（慢主风向漂移），`f2、f3` 递增（细节抖动）；`A` = 风引起的目标角偏置幅度，如 `2°~8°`。**[公式可靠：Ken Perlin Improved Noise；系数 A/f 为经验值]**
- 用不同 `s1,s2,s3`（种子/相位）给不同部件去相关，避免所有部件同步摆动。**[经验值]**

**免噪声库的确定性替代：非谐波多正弦**（完全确定、可脚本、跨平台零依赖）：

```
wind(t) = Σ_i A_i · sin(2π·f_i·t + φ_i)
```

关键：`f_i` 取**非谐波比**（如 0.13、0.31、0.47 Hz），避免周期锁死出现"机械感"。**[经验值]**

把 `wind(t)` 作为目标角偏置加入：`g = θ_rest + wind(t)`，或作为外力 `v += wind_gain * wind(t) * dt`。前者是"风吹向哪就倾向哪"，后者是"阵风加速"。**[经验值]**

---

## 3. follow-through / 父子链滞后

### 3.1 原理与典型量

迪士尼原则 follow-through/overlapping action：主质量先停，次级元素（头发/衣服/尾巴）继续动、再回落。程序化实现就是"子弹簧追父目标"。**[可靠来源：101_follow_through 技术文档]**

实测/社区典型量（**[经验值，来源为社区文档]**）：

- 滞后时间：次级元素落后主运动 **1~5 帧**（60fps 下约 16~83ms）。
- 过冲：**10~30%**（超过目标再回落）。
- 位置公式：`x = base + A·e^(-ζω t)·sin(ω_d t)`（欠阻尼振荡包络）。
- 刚度/阻尼社区取值：stiffness `10~25`（飘、慢）~ `25~50`（跟手）；damping `0.3~0.6`（弹）~ `0.6~0.9`（稳）。

> 注意：这些"社区值"用的是简化量纲（damping 直接乘速度），与本报告 §1 的 s/d 定义不完全同标。落地建议统一用 §1 的 halflife/frequency 参数化，数值更可控。

### 3.2 父子链实现（每帧自顶向下）

```python
def update_chain(parts, dt):
    # parts[0] 是根（由动画驱动），其余是子级链
    for p in parts[1:]:
        p.target = p.parent.angle          # 子级目标是父级"本帧已更新后"的角度
        p.angle, p.vel = spring_step(p.angle, p.vel, p.target, p.s, p.d, dt)
```

- 单链叠加：世界旋转 = 父世界旋转 × 子局部旋转（对 2D 角度即相加）。**[可靠来源：标准骨骼/父子变换]**
- 想让链末端更软：越靠末端 `halflife` 越大 / `f` 越小（每段递增），或全部同参（自然叠加滞后）。**[经验值]**

---

## 4. 软弯曲（soft bend）

目标：把"整块绕 pivot 刚性旋转"升级为"**沿部件长轴衰减的旋转**"——根部几乎不动、越靠尖端转越多。

### 4.1 加权旋转衰减（weighted rotation falloff）

对部件内归一化坐标 `u ∈ [0,1]`（`u=0` 在 pivot，`u=1` 在尖端）：

```
θ(u) = θ_max · w(u)
w(u) = u            # 线性（最朴素）
w(u) = u²           # 根部更硬、尖端更弯（更像毛发/草）
w(u) = smoothstep(0,1,u)   # 两端平缓、中段集中
```

**[公式：本报告综合，属通用图形技术/经验值]**

### 4.2 方案 A：CPU 铰链链（零新像素、全后端安全，推荐）

把长部件切成 `N` 段子精灵，每段绕其自身 pivot 旋转 `θ_max · w(i/N)`（每段内部仍是刚性）。这就是"离散化的 weighted falloff"，与 Live2D 的旋转 deformer / ArtMesh 分段思路一致。优点：确定性、可脚本批量切分、不依赖 shader、软件渲染后端也 OK。缺点：段数少时接缝可见（段间 pivot 要正好贴合）。**[可靠来源：Live2D 旋转 deformer 分段做法 + 经验]**

### 4.3 方案 B：QML ShaderEffect 顶点着色器

**关键前提**：Qt 文档明确——ShaderEffect 默认只有 **4 个顶点**；要做非线性顶点变形（如卷页），必须通过 `mesh: GridMesh { resolution: "Nx1" }` 细分网格，否则顶点弯曲无法表现。**[可靠来源：Qt ShaderEffect 文档]**

Qt 6 顶点着色器骨架（Vulkan 风格 GLSL，需经 `qsb` 离线编译成 `.qsb`；D3D11/Metal/Vulkan 由 RHI 自动翻译）：

```glsl
#version 440
layout(location = 0) in vec4 qt_Vertex;        // 局部坐标，左上(0,0)，右下(width,height)
layout(location = 1) in vec2 qt_MultiTexCoord0;
layout(location = 0) out vec2 coord;
layout(std140, binding = 0) uniform buf {
    mat4 qt_Matrix;      // 必须第 1 个，offset 0
    float qt_Opacity;    // 必须第 2 个，offset 64
    vec2  pivot;         // pivot 在 item 局部坐标(px)
    float angle;         // 尖端最大弯角(弧度)
    float falloffExp;    // 1=线性, 2=二次
};
void main() {
    vec2 p = qt_Vertex.xy - pivot;
    float u = clamp(p.y / length, 0.0, 1.0);   // 假设长轴沿 +y
    float w = pow(u, falloffExp);
    float a = angle * w;
    float c = cos(a), s = sin(a);
    vec2 rp = vec2(p.x * c - p.y * s, p.x * s + p.y * c);
    coord = qt_MultiTexCoord0;
    gl_Position = qt_Matrix * vec4(pivot + rp, qt_Vertex.z, qt_Vertex.w);
}
```

### 4.4 跨 D3D11 / 软件后端的坑（重要）

1. **software 后端不渲染 ShaderEffect**：Qt 文档明确"with the `software` backend effects will not be rendered at all"。所以 shader 弯曲必须有 **CPU 铰链链兜底**，或在运行前探测后端。**[可靠来源：Qt ShaderEffect 文档]**
2. **Qt 6 必须 `.qsb`**：不能写内联 GLSL 字符串，要离线用 `qsb` 把 Vulkan 风格 GLSL 编成 SPIR-V 再转 HLSL/MSL；CMake 可集成 `qt_add_shaders`。**[可靠来源：Qt ShaderEffect 文档]**
3. **顶点输入位置固定**：location 0 = 位置、location 1 = 纹理坐标；uniform 必须放进 `binding=0` 的 std140 块，`qt_Matrix`/`qt_Opacity` 必须在前两格；采样器 binding 从 1 起。**[可靠来源：Qt ShaderEffect 文档]**
4. **顶点输出/片元输入命名要一致**：跨 API（尤其非 core-profile GL）靠名字链接，建议同名同 location。**[可靠来源：Qt ShaderEffect 文档]**
5. **纹理原点在左上、颜色预乘**：Scene Graph 纹理左上为原点、颜色预乘 alpha，写片段着色器时注意。**[可靠来源：Qt ShaderEffect 文档]**
6. **顶点弯曲只变形不生成像素**：它是对既有 quad 的非线性顶点变换 + 采样原纹理，**不产生新像素**，符合"零新生成像素"约束；但弯曲过大会把像素拉伸糊掉（那是采样拉伸，不是新内容）。**[经验值]**

---

## 5. Live2D 物理演算（physics）模型参考

Live2D 的 physics 本质就是"**输入参数 → 摆锤 → 输出参数**"，可简化为绕 pivot 的 1 自由度旋转。**[可靠来源：Live2D 官方手册]**

### 5.1 输入/输出换算

- **输入（Input）** = "挂摆锤的线"：身体/头部参数。两类：`Position X`（横向移动）、`Angle`（Z 轴倾斜）。同类型多输入按 `Influence %` 加权，同类 Influence 总和 ≤100%。
- **输入归一化（Input normalization）**：把输入参数的最小/最大定义映射到归一化区间；Angle 默认 `-10.0 / 0.0 / +10.0`（度），Position X 同样默认 `-10/0/10`。**这是"角度限制"的来源**——超出范围的输入被压缩，间接限制摆幅。
- **输出（Output）** = 摆锤算出的摆动，写回某个参数（如头发摆角）。

### 5.2 物理模型参数（可直接映射到本报告实现）

| Live2D 参数 | 含义 | 映射到本报告 |
|---|---|---|
| **Duration** | 摆动速度：越小越快 | ≈ 弹簧 `halflife` / 频率（小 halflife=快） |
| **Ease of swinging** | 摆动量：越大同样输入摆得越大，**官方建议 0.7~0.99** | ≈ 输出增益 / `θ_max` 幅度 |
| **Reaction time** | 响应敏捷度：1 为基准，>1 更敏捷，<1 更迟钝 | ≈ 子级跟随父级的滞后系数 |
| **Speed of convergence** | 摆动收敛快慢：1 为基准，>1 更快停，<1 更慢 | ≈ 阻尼 `d`（越大收敛越快） |

**[可靠来源：Live2D 官方手册，含 "0.7~0.99" 的经验值]**

### 5.3 多级摆锤 = 父子链

Live2D 允许多段摆锤（父摆锤动 → 子摆锤再动），与 §3 的父子链滞后完全同构：子摆锤的目标是父摆锤当前角度。**[可靠来源：Live2D 官方手册]**

### 5.4 可简化移植清单（到 1 自由度 pivot）

1. 每部件 = 一个摆锤（角度弹簧），目标角 = 根角度 × 输入增益。
2. 摆幅上限 = clamp 到归一化区间（如 ±10°~±30°，按部件自定）。
3. "Ease of swinging" 0.7~0.99 作为幅度增益，直接乘。
4. "Speed of convergence" 映射到阻尼，1.0 对应你调好的中性 `ζ`。
5. 周期性呼吸/摆锤循环（Live2D 的 CubismHarmonicMotionController）就是"输出参数按正弦/周期律驱动"，等价于你现在的固定正弦，可用 §2 的噪声风替换掉以增加生命力。**[可靠来源：Live2D Harmonic Motion 教程]**

---

## 6. 引用来源

- [游戏开发中的阻尼器和阻尼弹簧（Daniel Holden《Code vs Data Driven Displacement》中译）](https://blog.uwa4d.com/archives/USparkle_Springs.html)
- [Daniel Holden 原文：Code vs Data Driven Displacement](https://theorangeduck.com/page/code-vs-data-driven-displacement)
- [Daniel Holden 源码：Spring-It-On](https://github.com/orangeduck/Spring-It-On)
- [Gaffer On Games：Integration Basics（semi-implicit Euler）](https://gafferongames.com/post/integration_basics/)
- [Live2D 官方手册：About Physics（输入/输出/归一化/物理模型参数）](https://docs.live2d.com/en/cubism-editor-manual/physics-operation/)
- [Live2D 官方手册：How to Set Up Physics（Duration / Ease of swinging / Reaction time / Speed of convergence）](https://docs.live2d.com/en/cubism-editor-manual/physical-operation-setting/)
- [Live2D SDK 教程：How to Operate Parameters Cyclically（周期摆动）](https://docs.live2d.com/en/cubism-sdk-tutorials/harmonicmotion/)
- [101. Follow-Through and Overlapping Action（社区技术文档）](https://github.com/raduacg/game-mechanics-optimizations/blob/main/101_follow_through.md)
- [Qt 6 ShaderEffect QML Type（.qsb 管线 / software 后端限制 / mesh 细分 / 顶点输入约定）](https://doc.qt.io/qt-6/qml-qtquick-shadereffect.html)
- [Ken Perlin：Improved Noise reference implementation](https://mrl.cs.nyu.edu/~perlin/noise/)
