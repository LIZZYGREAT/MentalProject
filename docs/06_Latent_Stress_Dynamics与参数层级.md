# MindFlow Latent Stress Dynamics 与参数层级建模

> 本文定义 Acute / Background Latent Stress Dynamics、状态方程、参数含义、person-level parameter hierarchy、scale identification、process noise 与主要 identifiability 约束。本文默认上一层已经提供 $Q_D,Q_P,Q_R,Q_{BS},C_D,C_U,F$，不重复其构造过程。

---

## 一、核心状态

总压力定义为：

$$
\boxed{
S_i(t)
=
A_i(t)+B_i(t)
}
$$

其中：

- $A_i(t)$：Acute Stress State；
- $B_i(t)$：Background Stress State；
- $S_i(t)$：总的 Latent Momentary Perceived Stress State。

$A/B$ 是模型定义的 latent decomposition，而不是两个可以被独立直接观测的心理量。

---

## 二、输入变量

本模块使用上一层提供的：

### Acute Inputs

$$
\boxed{
Q_D(t)
}
$$

Aggregated Effective Demand。

$$
\boxed{
Q_P(t)
}
$$

Aggregated Effective Pressure。

$$
\boxed{
Q_R(t)
}
$$

Aggregated Effective Recovery。

$$
\boxed{
Q_{BS}(t)
}
$$

Aggregated Bot Support，只有在 Bot Dynamics candidate model 中启用。

### Background Inputs

$$
\boxed{
C_D(t)
}
$$

Recent Demand Accumulation。

$$
\boxed{
C_U(t)
}
$$

Unresolved Obligation Burden。

$$
\boxed{
F(t)
}
$$

Sleep-Dominant Recovery Debt。

---

## 三、Acute Equilibrium

定义：

$$
\boxed{
A_i^{eq}(t)
=
g_i^A
\left[
\beta_D^AQ_D(t)
+
\beta_P^AQ_P(t)
-
\beta_R^AQ_R(t)
\right]
-
\beta_{BS}Q_{BS}(t)
}
$$

其中 Bot Support 项只在启用 BI1/BI2 candidate model 时存在。

参数含义：

| 参数 | 名称 | 含义 |
|---|---|---|
| $g_i^A$ | Acute Susceptibility | participant 相对于 cohort 平均水平的 acute responsiveness |
| $\beta_D^A$ | Acute Demand Effect | Demand 对 Acute Stress 的 population-level effect |
| $\beta_P^A$ | Acute Pressure Effect | Pressure 对 Acute Stress 的 population-level effect |
| $\beta_R^A$ | Acute Recovery Effect | Recovery 对 Acute Stress 的 population-level relief effect |
| $\beta_{BS}$ | Bot Support Effect | Bot Support 对 Acute Stress 的 population-level relief effect |

要求：

$$
\boxed{
\beta_D^A\ge0
}
$$

$$
\boxed{
\beta_P^A\ge0
}
$$

$$
\boxed{
\beta_R^A\ge0
}
$$

$$
\boxed{
\beta_{BS}\ge0
}
$$

---

## 四、Acute State Dynamics

Acute State 按一阶回归到当前 equilibrium：

$$
\boxed{
\frac{dA_i(t)}{dt}
=
\kappa_i(t)
\left[
A_i^{eq}(t)-A_i(t)
\right]
+
\epsilon_{A,i}(t)
}
$$

其中：

$$
\kappa_i(t)>0
$$

控制 Acute Stress 对 equilibrium 的响应速度。

---

## 五、上升与恢复速度

当前候选结构区分：

$$
\boxed{
\kappa_{\uparrow}
}
$$

与：

$$
\boxed{
\kappa_{\downarrow,i}
}
$$

当：

$$
A_i^{eq}(t)>A_i(t)
$$

时，使用：

$$
\kappa_i(t)=\kappa_{\uparrow}
$$

当：

$$
A_i^{eq}(t)\le A_i(t)
$$

时，使用：

$$
\kappa_i(t)=\kappa_{\downarrow,i}
$$

即：

$$
\boxed{
\kappa_i(t)
=
\begin{cases}
\kappa_{\uparrow},
&
A_i^{eq}>A_i\\[1mm]
\kappa_{\downarrow,i},
&
A_i^{eq}\le A_i
\end{cases}
}
$$

其中：

- $\kappa_{\uparrow}$：v1 优先 population-level；
- $\kappa_{\downarrow,i}$：person-level candidate，不默认对所有 participant 个体化。

---

## 六、为什么不设置自由 Post-Event Pressure Decay

事件层与时间核层已经采用：

$$
K_P^{post}=0
$$

或不设置独立自由 post-pressure decay。

原因是如果同时存在：

- Pressure post-tail；
- $\kappa_{\downarrow}$；

两者都会解释：

> 事件结束后为什么压力仍未下降。

因此 v1 将 post-event recovery speed 主要交给：

$$
\kappa_{\downarrow}
$$

而不再增加独立自由：

$$
\tau_P^{post}
$$

---

## 七、Background Equilibrium

定义：

$$
\boxed{
B_i^{eq}(t)
=
S_i^0
+
g_i^B
\left[
\beta_D^BC_{D,i}(t)
+
\beta_U^BC_{U,i}(t)
+
\beta_F^BF_i(t)
\right]
}
$$

参数含义：

| 参数 | 名称 | 含义 |
|---|---|---|
| $S_i^0$ | Background Stress Baseline | participant-specific background stress baseline |
| $g_i^B$ | Background Susceptibility | participant 相对于 cohort 的 background responsiveness |
| $\beta_D^B$ | Recent Demand Effect | $C_D$ 对 Background Stress 的 population-level effect |
| $\beta_U^B$ | Unresolved Obligation Effect | $C_U$ 对 Background Stress 的 population-level effect |
| $\beta_F^B$ | Recovery Debt Effect | $F$ 对 Background Stress 的 population-level effect |

要求：

$$
\boxed{
\beta_D^B\ge0
}
$$

$$
\boxed{
\beta_U^B\ge0
}
$$

$$
\boxed{
\beta_F^B\ge0
}
$$

---

## 八、Background State Dynamics

Background State 同样采用一阶回归：

$$
\boxed{
\frac{dB_i(t)}{dt}
=
\kappa_B
\left[
B_i^{eq}(t)-B_i(t)
\right]
+
\epsilon_{B,i}(t)
}
$$

其中：

$$
\kappa_B>0
$$

为 population-level Background adaptation rate。

定义：

$$
\boxed{
\tau_B
=
\frac{1}{\kappa_B}
}
$$

表示 Background 的典型响应时间尺度。

---

## 九、$C_D$ 与 $B$ 的时间尺度约束

由于：

$$
C_D
$$

本身已经是对：

$$
Q_D
$$

的一阶低通：

$$
\frac{dC_D}{dt}
=
\frac{Q_D-C_D}{\tau_D}
$$

而 $B$ 又再次低通：

```text
Q_D
 ↓
τ_D
 ↓
C_D
 ↓
κ_B
 ↓
B
```

因此必须避免：

$$
\tau_D
\approx
\tau_B
$$

导致 practical non-identifiability。

正式要求：

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

而：

$$
\kappa_B
$$

保持 population-level candidate parameter。

---

## 十、Acute / Background 的时间尺度分离

概念上：

$$
\boxed{
A:
Minutes\text{-}Hours
}
$$

$$
\boxed{
B:
Days\text{-}Weeks
}
$$

模型必须通过：

- input source；
- transition rate；
- repeated observations；
- hierarchical constraints；

保持两者区分。

由于 Measurement 只直接观测：

$$
S=A+B
$$

因此：

$$
A/B
$$

分解属于弱可观测 latent decomposition。

正式验证不能只检查：

$$
\hat S
$$

还必须在 Synthetic 中检查：

$$
RMSE_A
$$

与：

$$
RMSE_B
$$

---

## 十一、Acute Susceptibility 的尺度识别

当前：

$$
A_i^{eq}
=
g_i^A
[
\beta_D^AQ_D+
\beta_P^AQ_P-
\beta_R^AQ_R
]
+\cdots
$$

若不约束，则对任意：

$$
c>0
$$

令：

$$
g_i^{A*}=cg_i^A
$$

同时：

$$
\beta_D^{A*}
=
\frac{\beta_D^A}{c}
$$

$$
\beta_P^{A*}
=
\frac{\beta_P^A}{c}
$$

$$
\beta_R^{A*}
=
\frac{\beta_R^A}{c}
$$

会产生相同 equilibrium。

因此必须设置 cohort-level scale anchor。

推荐：

$$
\boxed{
\log g_i^A
\sim
N(0,\sigma_{g_A}^2)
}
$$

等价于：

$$
\boxed{
GeometricMean(g_A)=1
}
$$

这样：

- $\beta_D^A,\beta_P^A,\beta_R^A$ 定义 population effect scale；
- $g_i^A$ 只表示 participant 相对于 cohort 的 acute susceptibility。

---

## 十二、Background Susceptibility 的尺度识别

同理：

$$
B_i^{eq}
=
S_i^0+
g_i^B[
\beta_D^BC_D+
\beta_U^BC_U+
\beta_F^BF
]
$$

也存在：

$$
g_B
\leftrightarrow
\beta^B
$$

尺度不唯一。

推荐：

$$
\boxed{
\log g_i^B
\sim
N(0,\sigma_{g_B}^2)
}
$$

等价于：

$$
\boxed{
GeometricMean(g_B)=1
}
$$

从而：

- $\beta_D^B,\beta_U^B,\beta_F^B$ 定义 population effect scale；
- $g_i^B$ 表示 participant 相对于 cohort 的 background susceptibility。

---

## 十三、Person-Level Parameter 最大候选集

当前最大候选：

$$
\boxed{
\theta_i^{person}
=
[
S_i^0,
g_i^A,
g_i^B,
\kappa_{\downarrow,i}
]
}
$$

但：

$$
\boxed{
CandidateSet
\neq
DefaultPersonalization
}
$$

Formal v1 不默认每个 participant 都拥有四个可自由估计的 person-specific dynamic parameters。

---

## 十四、Parameter Observability Gate

每个 person-level parameter 只有在对应数据条件充分时才允许晋级。

### 1. $S_i^0$

需要：

- 足够数量 EMA；
- 跨多个日期；
- 不同 event context 下仍有 baseline information。

### 2. $g_i^A$

需要：

- 多次 event-near EMA；
- $Q_D/Q_P/Q_R$ 有足够 variation；
- 至少有多个 acute episodes。

### 3. $g_i^B$

需要：

- 跨日观测；
- $C_D/C_U/F$ 有足够 slow variation；
- 足够长的 longitudinal exposure。

### 4. $\kappa_{\downarrow,i}$

需要：

- 多次 post-event observation；
- 能观察从高 acute state 向低 equilibrium 恢复的轨迹；
- 恢复窗口不被新事件大量打断。

若数据不足，对应 parameter 保持 population-level / strongly pooled。

---

## 十五、Hierarchical Parameterization

推荐对 person-level candidate 使用 partial pooling。

概念上：

$$
\boxed{
\theta_i
\sim
PopulationDistribution
}
$$

例如：

$$
S_i^0
\sim
N(\mu_{S_0},\sigma_{S_0}^2)
$$

$$
\log g_i^A
\sim
N(0,\sigma_{g_A}^2)
$$

$$
\log g_i^B
\sim
N(0,\sigma_{g_B}^2)
$$

$$
\log\kappa_{\downarrow,i}
\sim
N(\mu_{\kappa_\downarrow},\sigma_{\kappa_\downarrow}^2)
$$

具体 prior 数值不在本模块中拍定，由 Synthetic / Pilot 进一步校准。

---

## 十六、Global Dynamic Parameters

v1 主要 global / population-level dynamic parameters 包括：

$$
\boxed{
\beta_D^A,
\beta_P^A,
\beta_R^A
}
$$

$$
\boxed{
\beta_D^B,
\beta_U^B,
\beta_F^B
}
$$

$$
\boxed{
\kappa_{\uparrow},
\kappa_B
}
$$

以及 candidate：

$$
\boxed{
\beta_{BS}
}
$$

Process noise 相关：

$$
q_A,
\quad
q_B
$$

Measurement noise：

$$
R
$$

其中 $R,q_A,q_B$ 的最终推断策略由 Measurement / Inference 模块进一步定义。

---

## 十七、Process Noise

Acute / Background Dynamics 中：

$$
\epsilon_A(t)
$$

和：

$$
\epsilon_B(t)
$$

表示模型未解释的随机状态变化。

离散化后可分别对应：

$$
q_A
$$

和：

$$
q_B
$$

作为 process noise scale。

v1 不增加 person-specific：

$$
q_{A,i},
\quad
q_{B,i}
$$

避免在数据稀疏条件下进一步扩大不可识别空间。

---

## 十八、Measurement Noise 与 Process Noise 的区分

Measurement Model 还包含：

$$
R
$$

表示 EMA observation noise variance。

必须区分：

$$
\boxed{
R
=
ObservationNoise
}
$$

与：

$$
\boxed{
q_A,q_B
=
ProcessNoise
}
$$

如果三者同时自由且缺少重复测量，容易竞争解释同一 residual。

因此推荐：

- $R$ 由 repeated EMA item / Pilot 形成 informative prior；
- $q_A,q_B$ 保持 population-level；
- 若 $q_B$ 无法恢复，优先固定为 small process floor，而不是增加 person-specific noise。

---

## 十九、Representation Parameters 不进入 Dynamic Fit

以下内容属于 representation layer，不作为本模块自由 dynamic parameters：

- Event semantic mapping；
- $D^{pot}$ 内部 mapping；
- $R^{pot}$ affordance mapping；
- $\rho_{app}$；
- Appraisal ordinal coding；
- $\tau_D$ 若 v1 已固定；
- Sleep transition parameters；
- $C^{ref}$ representation rules；
- Bot Support feature weights（v1 不存在自由 $w_V,w_G,w_R$）。

这样保持：

$$
\boxed{
RepresentationCalibration
\neq
StressDynamicFitting
}
$$

---

## 二十、Sleep Representation 与 $\beta_F^B$

$F$ 已经由 Sleep Representation 层构造。

不能同时从 EMA 自由学习：

- sleep debt transition shape；
- nap recovery coefficient；
- overnight recovery coefficient；
- $\beta_F^B$。

因此当前要求：

$$
\boxed{
SleepTransitionParameters
=
FixedRepresentationParameters
}
$$

Stress Dynamics 只学习：

$$
\boxed{
\beta_F^B
}
$$

即：

> 给定已经定义好的 $F$ representation，它与 Background Stress 的关系强度。

---

## 二十一、$C_U$ 与 Deadline Pressure 的参数边界

Remaining effort：

$$
W^{rem}
$$

会同时参与：

- $C_U$：unresolved amount；
- $U_{ddl}$：time scarcity。

这属于共享事实，不自动构成 double counting。

必须通过 Synthetic 构造正交条件：

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

是否能够稳定区分。

---

## 二十二、Recovery Effect 与 Recovery Speed 的边界

需要区分：

$$
\beta_R^A
$$

和：

$$
\kappa_{\downarrow,i}
$$

其中：

- $\beta_R^A$：Recovery input 将 Acute equilibrium 拉低多少；
- $\kappa_{\downarrow,i}$：Acute state 向较低 equilibrium 回落多快。

因此：

$$
\boxed{
RecoveryMagnitude
\neq
RecoverySpeed
}
$$

为减少混淆：

- $K_R^{post}$ 不设置自由长尾；
- $\tau_R$ 不作为自由 dynamic parameter。

---

## 二十三、Bot Support 与 Recovery 的边界

在 BI1 中：

$$
\beta_{BS}Q_{BS}
$$

与：

$$
\beta_R^AQ_R
$$

都可能解释压力下降。

因此进入正式模型前必须检查：

$$
Corr(Q_{BS},Q_R)
$$

以及 posterior：

$$
Corr(\beta_{BS},\beta_R^A)
$$

若高度相关且 parameter recovery 不稳定，则：

$$
\beta_{BS}
$$

不能仅凭解释合理性进入正式 Dynamics。

---

## 二十四、核心模型层级

推荐将 Dynamic Model 理解为逐层 promotion。

### M0：Baseline Latent Dynamics

只保留：

- $S_i^0$；
- population-level acute/background effects；
- minimal process noise。

### M1：Person Baseline

允许：

$$
S_i^0
$$

个体化。

### M2：Acute Susceptibility

当 observability 足够时增加：

$$
g_i^A
$$

### M3：Background Susceptibility

当 slow-state variation 足够时增加：

$$
g_i^B
$$

### M4：Recovery-Speed Personalization

当 post-event recovery data 足够时增加：

$$
\kappa_{\downarrow,i}
$$

这里的 M0-M4 仅表示 parameter-promotion logic，不应与其他文档中的实验 ablation 命名混用。

---

## 二十五、Bot Dynamics 的模型地位

核心 Stress Dynamics 可以定义为不含 Bot Support 的：

$$
\boxed{
A_i^{eq}
=
g_i^A
[
\beta_D^AQ_D+
\beta_P^AQ_P-
\beta_R^AQ_R
]
}
$$

Bot candidate model 再扩展：

$$
\boxed{
A_i^{eq}
=
g_i^A
[
\beta_D^AQ_D+
\beta_P^AQ_P-
\beta_R^AQ_R
]
-
\beta_{BS}Q_{BS}
}
$$

因此：

$$
\boxed{
\beta_{BS}
=
CandidateParameter
}
$$

不是无条件进入最小正式模型。

---

## 二十六、模型状态与参数的可识别性重点

Synthetic 必须重点检查以下 parameter groups：

### Acute Scale

$$
(g_A,\beta_D^A,\beta_P^A,\beta_R^A)
$$

### Background Scale

$$
(g_B,\beta_D^B,\beta_U^B,\beta_F^B,S^0)
$$

### Slow-Time Separation

$$
(\tau_D,\kappa_B)
$$

### Recovery Magnitude / Speed

$$
(\beta_R^A,\kappa_{\downarrow})
$$

### Noise Separation

$$
(R,q_A,q_B)
$$

### Pressure / Obligation Separation

$$
(\beta_P^A,\beta_U^B)
$$

### Bot / Recovery Separation

$$
(\beta_{BS},\beta_R^A)
$$

### Latent State Decomposition

$$
(A,B)
$$

---

## 二十七、Synthetic Recovery 指标

对每个 parameter 至少检查：

- bias；
- RMSE；
- interval coverage；
- posterior contraction；
- posterior correlation；
- non-identifiable ridge / multimodality。

对 latent state 至少检查：

$$
RMSE_S
$$

$$
RMSE_A
$$

$$
RMSE_B
$$

不能只因为：

$$
\hat S\approx S
$$

就认为：

$$
A/B
$$

分解正确。

---

## 二十八、当前最小可识别 v1

在进入 Synthetic 时，建议最小 Dynamics 为：

$$
\boxed{
S_i(t)=A_i(t)+B_i(t)
}
$$

Acute：

$$
\boxed{
A_i^{eq}
=
g_i^A
[
\beta_D^AQ_D
+
\beta_P^AQ_P
-
\beta_R^AQ_R
]
}
$$

$$
\boxed{
\frac{dA_i}{dt}
=
\kappa_i(t)
[
A_i^{eq}-A_i
]
+
\epsilon_A
}
$$

Background：

$$
\boxed{
B_i^{eq}
=
S_i^0
+
g_i^B
[
\beta_D^BC_D
+
\beta_U^BC_U
+
\beta_F^BF
]
}
$$

$$
\boxed{
\frac{dB_i}{dt}
=
\kappa_B
[
B_i^{eq}-B_i
]
+
\epsilon_B
}
$$

并要求：

$$
\boxed{
GeometricMean(g_A)=1
}
$$

$$
\boxed{
GeometricMean(g_B)=1
}
$$

以及：

$$
\boxed{
\tau_D\ll1/\kappa_B
}
$$

Bot Support 通过 BI1 candidate extension 单独验证。

---

## 二十九、当前冻结边界

1. 总压力为 $S=A+B$。
2. $A$ 为分钟—小时 Acute State；$B$ 为天—周 Background State。
3. Acute equilibrium 只使用 $Q_D,Q_P,Q_R$，Bot Support 作为 candidate extension。
4. Background equilibrium 只使用 $C_D,C_U,F$。
5. $\beta_D^A,\beta_P^A,\beta_R^A,\beta_D^B,\beta_U^B,\beta_F^B$ 为 population-level effect parameters。
6. $S_i^0,g_i^A,g_i^B,\kappa_{\downarrow,i}$ 是最大 person-level candidate set，而非默认全部个体化。
7. $\kappa_{\uparrow}$ 与 $\kappa_B$ v1 优先 population-level。
8. $g_A$ 与 $g_B$ 必须分别使用 cohort-level geometric-mean scale anchor。
9. $\tau_D$ 与 $1/\kappa_B$ 必须保持明显时间尺度分离。
10. Recovery magnitude $\beta_R^A$ 与 recovery speed $\kappa_{\downarrow}$ 必须区分。
11. v1 不增加 person-specific process noise。
12. $R$ 与 $q_A/q_B$ 需要通过 repeated probe / Synthetic 约束。
13. Sleep transition parameters 不通过 EMA 自由拟合。
14. Representation layer 与 Dynamic fitting 必须解耦。
15. Bot Support 参数 $\beta_{BS}$ 只有通过 ablation、recoverability 与 held-out prediction 后才允许晋级。
16. Synthetic 必须分别检验 $S/A/B$ recovery，而不能只检验总压力。
