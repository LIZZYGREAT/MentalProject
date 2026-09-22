# MindFlow 时间核、Channel Aggregation 与 Slow Context States 建模

> 本文定义 Effective Demand / Recovery 与 mechanism-specific Pressure appraisal 如何映射到连续时间输入 $Q_D,Q_P,Q_R$，以及 Slow Context States $C_D,C_U,F$ 如何构造并进入后续 Background Dynamics。本文默认事件层和 Personal Appraisal 层已经完成，不重复其定义。

---

## 一、模块职责

上一层已经提供每个 participant-event pair 的：

$$
D_{i,e}^{eff},
\quad
p_{ddl,i,e}^{app}(t),
\quad
p_{unc,i,e}^{app}(t),
\quad
p_{soc,i,e}^{src},
\quad
I_{i,e},
\quad
R_{i,e}^{eff}
$$

本模块负责：

1. 给 Demand / Recovery 与每一条 Pressure mechanism 加上各自的时间作用结构；
2. 将多个同时或相邻事件聚合为 bounded channel input；
3. 在每个时刻 $t$ 完成 event 内 Pressure 聚合，并在该时刻统一应用一次 Personal Importance；
4. 从历史 Demand、未解决义务和睡眠恢复构造 slow context states。

完整链路：

```text
D_eff / p_ddl_app(t) / p_unc_app(t) / p_soc_src / R_eff
        ↓
Mechanism-Specific Temporalization
        ↓
Per-Event Temporal Contribution
        ↓
Within-Event Pressure Noisy-OR
        ↓
Personal Importance × 1
        ↓
Across-Event Channel Aggregation
        ↓
Q_D(t), Q_P(t), Q_R(t)
        ↓
┌───────────────┬───────────────┐
│               │               │
C_D(t)        C_U(t)           F(t)
│               │               │
└───────────────┴───────────────┘
        ↓
Background Dynamics
```

---

## 二、变量总表

| 变量 | 名称 | 含义 |
|---|---|---|
| $K_e^D(t)$ | Demand Kernel | event $e$ 的 Demand 时间作用函数 |
| $K_e^R(t)$ | Recovery Kernel | event $e$ 的 Recovery 时间作用函数 |
| $K_e^{soc}(t)$ | Social Temporal Activation | social-evaluative mechanism 的固定时间激活函数 |
| $q_{D,e}(t)$ | Event Demand Contribution | 单个 event 在时刻 $t$ 对 Demand channel 的贡献 |
| $q_{R,e}(t)$ | Event Recovery Contribution | 单个 event 在时刻 $t$ 对 Recovery channel 的贡献 |
| $\tilde p_{ddl,i,e}(t)$ | Temporalized Deadline Pressure | deadline mechanism 在时刻 $t$ 的机制贡献 |
| $\tilde p_{unc,i,e}(t)$ | Temporalized Uncertainty Pressure | uncertainty mechanism 在时刻 $t$ 的机制贡献 |
| $\tilde p_{soc,i,e}(t)$ | Temporalized Social Pressure | social-evaluative mechanism 在时刻 $t$ 的机制贡献 |
| $P_{i,e}^{struct}(t)$ | Event Pressure Structure | event 内在时刻 $t$ 经 noisy-OR 聚合的 Pressure |
| $P_{i,e}^{eff}(t)$ | Event Effective Pressure | 在时刻 $t$ 应用一次 Personal Importance 后的 Pressure |
| $Q_D(t)$ | Aggregated Demand | 多事件聚合后的 Demand input |
| $Q_P(t)$ | Aggregated Pressure | 多事件聚合后的 Pressure input |
| $Q_R(t)$ | Aggregated Recovery | 多事件聚合后的 Recovery input |
| $C_D(t)$ | Recent Demand Accumulation | 最近实际 Demand 的慢记忆 |
| $C_U(t)$ | Unresolved Obligation Burden | 当前尚未解决的 obligation backlog |
| $F(t)$ | Sleep-Dominant Recovery Debt | 以睡眠为核心的恢复债务 |

---

## 三、时间核的一般形式

对任意 event $e$，有效输入只有在相应时间结构内才作用。

Demand 与 Recovery 使用各自独立的 kernel：

$$
\boxed{
q_{D,e}(t)
=
D_{i,e}^{eff}K_e^D(t)
}
$$

$$
\boxed{
q_{R,e}(t)
=
R_{i,e}^{eff}K_e^R(t)
}
$$

其中：

$$
0\le K_e^D,K_e^R\le1
$$

Pressure 不使用统一的 $K_e^P(t)$，而是按 mechanism 分别 temporalize，见本文第五节。

Effective magnitude 与 temporal shape 分离：

$$
\boxed{
Magnitude
\neq
Duration
}
$$

这也是事件层要求 $D^{pot}$ 和 $R^{pot}$ 主要表示 intensity 而非总时长的原因。

---

## 四、Demand Kernel

v1 Demand 主要采用 active-only kernel。

对实际 execution interval：

$$
[t_e^{start},t_e^{end}]
$$

定义最小形式：

$$
\boxed{
K_e^D(t)
=
\mathbf 1
\left(
t_e^{start}
\le t
\le
t_e^{end}
\right)
}
$$

因此：

- event 尚未开始：Demand contribution 为 0；
- event 实际执行时：Demand active；
- event 结束后：不保留独立 Demand post-tail。

这里使用的是 actual execution interval（`actual_start` / `actual_end`）。按第 2 份的 `SingleExposureEncodingRule`：若已知实际只参与一部分，则 duration 已经由该 actual interval 表达，$Z_e^{exec}$ 在该区间内取 1，不再同时取 fractional value。

Event duration 通过 kernel 的 active interval 自然进入总 exposure。

v1 不再把 duration 大幅重复编码进 $D_e^{pot}$。

---

## 五、Pressure 的 Mechanism-Specific Temporalization 与 Event 内聚合

Pressure 不使用统一 kernel。删除：

$$
q_{P,e}(t)
=
P_{i,e}^{eff}K_e^P(t)
$$

以及把全部 Pressure mechanism 统一写成：

$$
K_e^P
=
K_e^{P,pre}+K_e^{P,active}
$$

的表述。原因是三条 mechanism 的时间结构不同，统一 kernel 会压平其时间语义，并可能重复编码 deadline proximity。

v1 仍然不设置任何自由的 post-pressure decay：

$$
\boxed{
Pressure\ post\text{-}tail=0
}
$$

原因是 post-event pressure decay 容易与 Acute State 的：

$$
\kappa_{\downarrow}
$$

竞争解释“事件结束后压力为什么没有立即下降”。

### 1. Deadline

Deadline source 本身已经随时间变化：

$$
U_{ddl,e}(t)
$$

因此：

$$
\boxed{
\tilde p_{ddl,i,e}(t)
=
p_{ddl,i,e}^{app}(t)
}
$$

即：

$$
\boxed{
No\ extra\ deadline\ proximity\ kernel
}
$$

不再额外乘一个随 deadline 临近增强的 anticipation kernel，否则会与 $U_{ddl}(t)$ 中已经包含的 remaining effort 与 available capacity 重复编码。其 active window 仍由事件层的 mechanism gate 控制：

$$
Z_e^{ddl}(t)
$$

### 2. Structural Uncertainty

其时间存在主要由事件层：

$$
Z_e^{unc}(t)
$$

与：

$$
U_{context,e}(t)
$$

共同控制，因此：

$$
\boxed{
\tilde p_{unc,i,e}(t)
=
p_{unc,i,e}^{app}(t)
}
$$

如果未来确实需要额外的 temporal shape，只允许使用预先冻结的 representation rule，不新增可学习的 urgency parameter。

### 3. Social Evaluation

social-evaluative structure 可以拥有独立、固定的 temporal activation：

$$
\boxed{
K_e^{soc}(t)
}
$$

例如允许：

- pre-event anticipation；
- active-event exposure；
- v1 不设置自由 post-tail。

具体 shape 在 Scenario Annotation / Synthetic / Pilot 后冻结。定义：

$$
\boxed{
\tilde p_{soc,i,e}(t)
=
Z_e^{soc}(t)
D_{s,e}
K_e^{soc}(t)
}
$$

### 4. Event 内 Pressure Noisy-OR

在每个时刻 $t$ 聚合 event 内的三条 Pressure mechanism：

$$
\boxed{
P_{i,e}^{struct}(t)
=
1-
\left[
1-\tilde p_{ddl,i,e}(t)
\right]
\left[
1-\tilde p_{unc,i,e}(t)
\right]
\left[
1-\tilde p_{soc,i,e}(t)
\right]
}
$$

因此：

$$
0\le P_{i,e}^{struct}(t)\le1
$$

三条 mechanism 可以同时贡献，但不会线性无限叠加。

### 5. Personal Importance 在时刻 $t$ 只作用一次

$$
\boxed{
P_{i,e}^{eff}(t)
=
\mathcal T_\epsilon
\left(
P_{i,e}^{struct}(t),
\rho_{app}^{\ell(I_{i,e})}
\right)
}
$$

其中 $\mathcal T_\epsilon$ 与 $I_{i,e}$ 由第 3 份定义。必须保持：

$$
\boxed{
Importance\ applied\ once
}
$$

即 Personal Importance 不在 deadline、uncertainty、social-evaluation 三个子通道中分别作用，也不在多个时刻被重复应用。

---

## 六、Recovery Kernel

Recovery 不在活动发生前生效：

$$
\boxed{
K_e^{R,pre}=0
}
$$

活动实际发生时：

$$
K_e^{R,active}(t)>0
$$

$K_e^{R,active}$ 同样作用在 actual recovery interval 上。按 `SingleExposureEncodingRule`：若已知实际发生时长，则 duration 由该 interval 表达，$Z_e^{occ}$ 只表示是否发生，不再同时取 fractional value。

v1 的 post-recovery tail 采用保守策略：

$$
\boxed{
K_e^{R,post}=0
}
$$

或仅允许非常短、固定且 population-level 的 residual tail。

不估计自由：

$$
\tau_R
$$

以避免与：

$$
\kappa_{\downarrow}
$$

重复解释压力下降速度。

---

## 七、多个事件的 Channel Aggregation

同一时刻可以同时存在多个 Demand / Pressure / Recovery contribution。

不使用简单线性求和：

$$
\sum_e q_e(t)
$$

因为容易超过合理范围。

采用 bounded noisy-OR saturation：

$$
\boxed{
Q_D(t)
=
1-
\prod_e
[
1-q_{D,e}(t)
]
}
$$

$$
\boxed{
Q_P(t)
=
1-
\prod_e
\left[
1-P_{i,e}^{eff}(t)
\right]
}
$$

$$
\boxed{
Q_R(t)
=
1-
\prod_e
[
1-q_{R,e}(t)
]
}
$$

因此：

$$
0\le Q_D,Q_P,Q_R\le1
$$

多个事件可以共同提高同一 channel，但边际贡献递减。

Pressure 的完整顺序为：

```text
Pressure Sources
      ↓
Mechanism-Specific Appraisal
      ↓
Mechanism-Specific Temporal Activation
      ↓
Within-Event Noisy-OR
      ↓
Personal Importance × 1
      ↓
Across-Event Noisy-OR
      ↓
Q_P(t)
```

这替换了此前 `Static P_eff → Generic K_P → Q_P` 的接口。

---

## 八、同一事件的多通道作用

同一个 event 可以同时进入多个 channel。

例如：

```text
重要 presentation
```

可能产生：

- execution Demand；
- social-evaluative Pressure。

因此：

$$
q_{D,e}(t)>0
$$

与：

$$
\tilde p_{soc,i,e}(t)>0
$$

可以同时成立。

这不是 double counting，因为两者代表不同机制。

需要避免的是：同一个事实在同一机制内被重复编码。

---

## 九、Recent Demand Accumulation $C_D$

定义：

$$
\boxed{
C_{D,i}(t)
=
\text{Recent Demand Accumulation}
}
$$

它回答：

> 最近已经实际承担了多少 Demand？

采用一阶低通：

$$
\boxed{
\frac{dC_{D,i}(t)}{dt}
=
\frac{
Q_D(t)-C_{D,i}(t)
}{
\tau_D
}
}
$$

其中：

$$
\tau_D>0
$$

为 recent-demand memory time constant。

当：

$$
Q_D>C_D
$$

时：

$$
C_D\uparrow
$$

当：

$$
Q_D<C_D
$$

时：

$$
C_D\downarrow
$$

$C_D$ 只来自真实 Demand history，不来自 future obligation，也不来自 deadline proximity。

---

## 十、$\tau_D$ 的识别约束

$C_D$ 之后还要通过 Background Dynamics 进入 $B$，因此：

```text
Q_D
 ↓
τ_D low-pass
 ↓
C_D
 ↓
κ_B low-pass
 ↓
B
```

存在双低通风险。

为避免 practical non-identifiability，要求：

$$
\boxed{
\tau_D
\ll
\tau_B
=
\frac{1}{\kappa_B}
}
$$

v1 推荐：

$$
\boxed{
\tau_D
=
Fixed\ or\ StronglyInformed
}
$$

而不是同时让：

$$
\tau_D
$$

和：

$$
\kappa_B
$$

自由寻找相近时间尺度。

---

## 十一、Unresolved Obligation Burden $C_U$

定义：

$$
\boxed{
C_{U,i}(t)
=
\text{Unresolved Obligation Burden}
}
$$

表示当前仍有多少已经承诺、未来仍需投入才能解决的工作悬而未决。

它是 algebraic context state：

$$
\boxed{
C_U
=
AlgebraicContextState
}
$$

不使用独立 ODE，也不引入：

$$
\tau_U
$$

---

## 十二、Obligation Inventory

当前 active obligations 记为：

$$
\mathcal O_i(t)
$$

每个 obligation $j$ 的核心量：

$$
W_{i,j}^{rem}(t)
$$

表示 remaining effective effort。

定义 open gate：

$$
Z_{i,j}^{open}(t)\in[0,1]
$$

对：

```text
OPEN
IN_PROGRESS
BLOCKED
```

通常：

$$
Z^{open}=1
$$

对：

```text
COMPLETED
CANCELLED
SUPERSEDED
```

则：

$$
Z^{open}=0
$$

总 unresolved effective effort：

$$
\boxed{
W_{U,i}(t)
=
\sum_{j\in\mathcal O_i(t)}
Z_{i,j}^{open}(t)
W_{i,j}^{rem}(t)
}
$$

---

## 十三、Typical Effective Capacity $C_i^{ref}$

定义：

$$
\boxed{
C_i^{ref}
=
\text{Typical Weekly Effective Self-Directed Work Capacity}
}
$$

它表示该 participant 在典型周结构下，通常可以用于：

- assignment；
- research；
- review；
- project；

等 self-directed obligations 的有效工作容量。

它不是：

$$
\text{Weekly Free Time}
$$

不是：

$$
\text{Current Deadline-Specific Capacity}
$$

也不是心理抗压能力。

它只是：

$$
\boxed{
ExposureNormalizationBaseline
}
$$

$C_i^{ref}$ 的机制结构已经确定（structure-frozen）：它只表示 typical capacity，不使用当前短期 availability。但 category-to-capacity mapping 与 $\eta_{cap}$ 等具体 representation 数值仍属 Representation-Pending-Freeze，需经 Scenario Annotation / Synthetic / Pilot 后确定。

---

## 十四、$C_i^{ref}$ 的来源

大学生课程高度周期化，因此可以利用 recurring schedule 构造典型周。

先估：

$$
T_i^{disc}
=
\text{Typical Weekly Discretionary Time}
$$

从总时间中排除：

- sleep；
- recurring classes；
- fixed meetings；
- commute；
- meals/basic routines；
- 其他长期固定不可支配时段。

再使用 population-level：

$$
\eta_{cap}\in(0,1)
$$

得到：

$$
\boxed{
C_i^{sched}
=
\eta_{cap}T_i^{disc}
}
$$

同时可结合：

- cold-start self-report；
- historical realized work capacity；
- population prior。

推荐来源优先级：

$$
\boxed{
HistoricalRealizedCapacity
>
ColdStartSelfReport
>
RecurringScheduleEstimate
>
PopulationPrior
}
$$

必须保持：

$$
\boxed{
EMA
\nrightarrow
C_i^{ref}
}
$$

---

## 十五、$C_i^{ref}$ 不使用当前短期 availability

不能令：

$$
C_i^{ref}
=
\text{Next 7 Days Available Capacity}
$$

否则当前繁忙周会同时：

$$
Q_D\uparrow
$$

与：

$$
C_U\uparrow
$$

造成重复编码。

因此：

$$
\boxed{
C_i^{ref}
=
TypicalCapacity
}
$$

具体 deadline 前是否来得及，由事件层：

$$
T_{deadline}^{capacity}
$$

进入：

$$
U_{ddl}
$$

处理。

---

## 十六、$C_U$ 的最终公式

首先：

$$
\boxed{
L_{U,i}(t)
=
\frac{
W_{U,i}(t)
}{
C_i^{ref}
}
}
$$

最终采用 saturation：

$$
\boxed{
C_{U,i}(t)
=
1-
e^{-L_{U,i}(t)}
}
$$

即：

$$
\boxed{
C_{U,i}(t)
=
1-
\exp
\left[
-
\frac{
\sum_j
Z_{i,j}^{open}(t)
W_{i,j}^{rem}(t)
}{
C_i^{ref}
}
\right]
}
$$

因此：

$$
0\le C_U<1
$$

不再增加额外 saturation parameter。

---

## 十七、$C_U$ 的职责边界

以下变量不进入 $C_U$：

| 变量 | 去向 |
|---|---|
| deadline proximity | $P_{ddl}$ |
| specific deadline 前可用容量 | $P_{ddl}$ |
| personal importance | Pressure appraisal |
| perceived uncertainty | Pressure appraisal |
| social evaluation | Pressure |
| recent realized workload | $C_D$ |
| progress fraction | 只用于更新 $W^{rem}$ |
| planned future work | future scenario |
| overdue age | 通过新 consequence / pressure 表达 |
| EMA residual | 不更新 $C_U$ |

因此：

$$
\boxed{
C_U
\neq
DeadlineUrgency
}
$$

---

## 十八、Progress 与 $C_U$

Progress 不作为独立输入。

保持：

$$
\boxed{
Progress
\rightarrow
W^{rem}
\rightarrow
C_U
}
$$

而不是：

$$
C_U=f(Progress,W^{rem})
$$

计划未来工作不能提前降低：

$$
W^{rem}
$$

因此：

$$
\boxed{
PlannedWork
\neq
CompletedWork
}
$$

---

## 十九、父子任务去重

任务拆分不能改变 unresolved workload。

例如：

```text
论文修改：6h
├── 实验：3h
├── 图：1h
└── 正文：2h
```

不能累计为 12h。

v1 采用：

$$
\boxed{
CountActiveLeavesOnly
}
$$

只累计 active leaf obligations。

---

## 二十、$C_D$ 与 $C_U$ 可以反向变化

假设论文剩余：

$$
W^{rem}=10h
$$

今天实际工作 3h。

工作期间：

$$
Q_D\uparrow
$$

因此：

$$
C_D\uparrow
$$

同时：

$$
W^{rem}:10\rightarrow7
$$

因此：

$$
C_U\downarrow
$$

所以：

$$
\boxed{
DoingWork:
C_D\uparrow
\quad
while
C_U\downarrow
}
$$

这是两个机制设计的预期行为，不是矛盾。

---

## 二十一、Sleep-Dominant Recovery Debt $F$

定义：

$$
\boxed{
F_i(t)
=
\text{Sleep-Dominant Recovery Debt}
}
$$

它表示跨日累积的、以主夜睡眠为核心的恢复债务。

$F$ 与 $Q_R$ 职责不同：

- $Q_R$：当前恢复活动的 acute relief；
- $F$：睡眠主导的背景恢复债务。

因此：

$$
\boxed{
Q_R
\neq
F
}
$$

---

## 二十二、主夜睡眠对 $F$ 的更新

主夜睡眠是 $F$ 的主要更新来源。

可抽象为：

$$
\boxed{
F_{d+1}
=
\Psi_F
(
F_d,
SleepDuration_d,
SleepQuality_d,
SleepTiming_d
)
}
$$

$$
\boxed{
F\ mechanism\ structure\ closed
}
$$

但：

$$
\boxed{
\Psi_F\ numeric/representation\ mapping
\ pending\ freeze
}
$$

具体 transition mapping 属于 representation layer，需经 Scenario Annotation / Synthetic / Pilot 后确定具体数值；不应把本模块理解为 $F$ 的完整数学 transition 已经确定。

候选 representation 参数可以包括：

- sleep duration adequacy；
- sleep quality weighting；
- debt accumulation coefficient；
- overnight recovery coefficient。

但这些参数不通过 EMA 自由拟合。

---

## 二十三、Nap 的双路径

Nap 可以同时产生：

### 1. Acute Recovery

$$
Nap
\rightarrow
Q_R
\rightarrow
A
$$

### 2. Small Sleep-Debt Update

$$
Nap
\rightarrow
F
\rightarrow
B
$$

但第二条路径必须弱于主夜睡眠：

$$
\boxed{
\gamma_N
\ll
\gamma_S
}
$$

其中：

- $\gamma_N$：nap 对 $F$ 的 recovery strength；
- $\gamma_S$：main sleep 对 $F$ 的 recovery strength。

$\gamma_N,\gamma_S$ 属于 representation parameters，不通过 EMA 自由估计。

---

## 二十四、普通 Recovery Activity 不直接更新 $F$

以下活动主要进入：

$$
Q_R
$$

而不直接修改 $F$：

- leisure；
- exercise；
- walk；
- entertainment；
- social recovery；
- meal break；
- short non-sleep rest。

因此：

$$
\boxed{
GeneralRecoveryActivity
\rightarrow
Q_R
}
$$

而不是：

$$
GeneralRecoveryActivity
\rightarrow
F
$$

---

## 二十五、Sleep Representation Parameter 的识别约束

因为 $F$ 本身不可直接观测，如果同时从 EMA 学习：

- sleep transition parameters；
- $\beta_F^B$；

容易出现尺度和作用强度混淆。

因此正式要求：

$$
\boxed{
SleepTransitionParameters
=
FixedRepresentationParameters
}
$$

真正允许 Stress Model 学习的是：

$$
\boxed{
\beta_F^B
}
$$

即：

> 给定已定义好的 sleep-debt representation，$F$ 与 background stress 的关联强度是多少？

---

## 二十六、Slow Context States 的最终职责

### $C_D$

$$
\boxed{
\text{最近已经实际承担了多少 Demand}
}
$$

### $C_U$

$$
\boxed{
\text{当前还有多少 obligation 尚未解决}
}
$$

### $F$

$$
\boxed{
\text{当前睡眠型恢复债务有多高}
}
$$

三个状态必须保持不同来源：

```text
Q_D history
    ↓
C_D

Obligation Inventory
    ↓
C_U

Sleep / Nap History
    ↓
F
```

不能使用同一 EMA residual 直接修正三者。

---

## 二十七、接入 Background Dynamics 的接口

本模块最终向 Background Dynamics 提供：

$$
\boxed{
C_{D,i}(t),
\quad
C_{U,i}(t),
\quad
F_i(t)
}
$$

后续 Background equilibrium 使用：

$$
B_i^{eq}(t)
=
S_i^0
+
g_i^B
[
\beta_D^BC_{D,i}(t)
+
\beta_U^BC_{U,i}(t)
+
\beta_F^BF_i(t)
]
$$

本模块不再定义 $B$ 的 ODE 和参数层级。

---

## 二十八、Forecast 中的 Slow States

### 1. $C_D$

Future scenario 根据未来：

$$
Q_D^{(m)}(t)
$$

沿相同低通方程传播。

### 2. $C_U$

Scenario $m$ 中：

$$
W_j^{rem,(m)}(t)
$$

根据：

- planned work；
- occurrence probability；
- progress uncertainty；
- new obligations；
- cancellation；
- scope change；

更新。

因此：

$$
\boxed{
C_{U,i}^{(m)}(t)
=
1-
\exp
\left[
-
\frac{
\sum_j
Z_{i,j}^{open,(m)}(t)
W_{i,j}^{rem,(m)}(t)
}{
C_i^{ref,(m)}
}
\right]
}
$$

### 3. $F$

Future sleep scenario 根据：

- planned sleep window；
- sleep occurrence；
- sleep quality uncertainty；
- nap uncertainty；

传播。

---

## 二十九、当前模块的 Identifiability 边界

需要重点检查：

### 1. $\tau_D$ 与 $\kappa_B$

要求：

$$
\tau_D\ll1/\kappa_B
$$

### 2. $W^{rem}$ 同时进入 $C_U$ 与 $U_{ddl}(t)$

这是共享事实，不自动构成 double counting。

必须通过 Synthetic 构造：

```text
same W_remaining, different deadline
different W_remaining, similar urgency
```

检查：

$$
\beta_U^B
$$

与：

$$
\beta_P^A
$$

是否可分。

### 3. Nap

需要检查：

$$
\beta_R^A
$$

与：

$$
\beta_F^B
$$

在真实 exposure pattern 下是否可分。

若不可分，优先固定 nap-to-$F$ representation，而不增加新自由参数。

---

## 三十、当前冻结边界

1. Effective magnitude 与 temporal shape 分离。
2. Demand v1 使用 active-only kernel。
3. Pressure 不使用统一 kernel，按 mechanism 分别 temporalize，且不设置自由 post-tail。
4. Recovery 不设自由 long post-tail。
5. 多事件通过 bounded noisy-OR 聚合。
6. $C_D$ 是 recent Demand 的一阶低通状态。
7. $\tau_D$ v1 固定或强先验，并与 $1/\kappa_B$ 保持明显时间尺度分离。
8. $C_U$ 是 algebraic context state，不使用独立 ODE。
9. $C_U$ 只编码 unresolved committed work，不编码 urgency、importance 或 uncertainty。
10. Progress 只通过 $W^{rem}$ 影响 $C_U$。
11. $C_i^{ref}$ 是 typical capacity，不是 current availability。
12. $C_i^{ref}$ 不由 EMA 拟合。
13. $F$ 是 sleep-dominant recovery debt。
14. 普通 recovery activity 主要进入 $Q_R$，不直接进入 $F$。
15. Nap 可以同时影响 $Q_R$ 与 $F$，但 nap-to-$F$ effect 固定且较弱。
16. Sleep transition parameters 属于 representation layer，不通过 EMA 自由拟合。
17. $C_D/C_U/F$ 不接受 EMA residual 直接更新。
18. Pressure 的 within-event noisy-OR 与 Personal Importance 在每个时刻 $t$ 求值，Importance 每个时刻只作用一次。
19. $U_{ddl}(t)$ 已经包含 deadline proximity，不再叠加额外 anticipation kernel。
20. social-evaluative mechanism 使用独立固定 activation $K_e^{soc}(t)$，其 shape 属于 Representation-Pending-Freeze。
21. $C^{ref}$ 与 $F$ 的机制结构已冻结，但其 representation mapping 数值仍待冻结。
