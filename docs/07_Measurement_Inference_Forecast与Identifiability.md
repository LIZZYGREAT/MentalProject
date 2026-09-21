# MindFlow Measurement、Inference、Forecast 与 Identifiability 审计

> 本文定义 MindFlow 压力模型的观测层、在线状态估计、参数估计、未来预测、不确定性传播、风险输出与 Identifiability / Synthetic Validation 规范。本文默认前六个模块已经定义事件层、Appraisal、时间核、Slow Context、Bot Interaction 与 Latent Dynamics，不重复其内部机制。

---

## 一、模块职责

完整链路：

```text
Latent States / Context States / Model Parameters
                    ↓
            Measurement Model
                    ↓
             State Assimilation
                    ↓
          Parameter Estimation
                    ↓
         Scenario-Based Forecast
                    ↓
      Predictive Distribution / Risk
                    ↓
     Identifiability / Validation
```

本模块负责回答：

1. EMA 实际测量什么；
2. 当前 latent state 如何被在线更新；
3. 个体参数和 population 参数如何估计；
4. future context 不确定时如何预测；
5. 如何输出预测分布而不是单点曲线；
6. 如何验证状态、参数和机制是否真正可识别；
7. 哪些模型可以晋级 Formal Study。

---

## 二、Primary Measurement

Primary EMA 测量用户在回答时刻的整体瞬时主观压力。

定义：

$$
\boxed{
y_{i,k}
=
S_i(t_{i,k})
+
\epsilon_{i,k}
}
$$

其中：

- $i$：participant；
- $k$：该 participant 的第 $k$ 次 EMA；
- $t_{i,k}$：实际回答时刻；
- $y_{i,k}$：0–10 absolute stress EMA；
- $S_i(t)=A_i(t)+B_i(t)$：latent total perceived stress；
- $\epsilon_{i,k}$：measurement error。

观测噪声：

$$
\boxed{
\epsilon_{i,k}
\sim
\mathcal N(0,R_{EMA})
}
$$

其中：

$$
\boxed{
R_{EMA}=\sigma_{EMA}^2
}
$$

表示 Primary EMA observation noise variance。统一使用 $R_{EMA}$，避免与 Recovery 的 $R$、Proactive Receptivity 的 $Recp^{pro}$ 以及 Bot Relevance 的 $Rel_r$ 混淆。

v1 使用 Gaussian observation approximation，不同时使用普通 Gaussian Kalman update 和另外一套 exact clipped likelihood。

---

## 三、Measurement Time

Measurement time 使用：

$$
\boxed{
answered\_at
}
$$

而不是：

- notification sent time；
- scheduled EMA time；
- notification opened time。

原因是 EMA 回答对应用户真正做出主观判断的时刻。

因此：

$$
t_{i,k}=answered\_at
$$

---

## 四、EMA 的观测边界

Primary EMA 直接测量：

$$
\boxed{
S=A+B
}
$$

不直接观测：

$$
A
$$

$$
B
$$

$$
C_D
$$

$$
C_U
$$

$$
F
$$

因此：

$$
\boxed{
EMAResidual
\rightarrow
Posterior(A,B)
}
$$

而不能：

$$
EMAResidual
\rightarrow
C_D/C_U/F
$$

$C_D/C_U/F$ 必须继续由其真实 context evidence 更新。

---

## 五、Conversational Expression 不是 Primary Measurement

必须保持：

$$
\boxed{
ConversationalStressExpression
\neq
PrimaryEMA
}
$$

例如用户说：

> “我现在压力好大。”

可以用于：

- context interpretation；
- safety handling；
- appraisal evidence；
- support routing；
- exploratory process analysis。

但不直接作为一条：

$$
y_{i,k}
$$

写入 Primary Measurement Model。

否则会将自然语言表达和正式 EMA 混成不同 reliability 的 observation。

---

## 六、Relative EMA 的定位

Relative EMA 可作为辅助测量，例如：

> “相比你平时，此刻压力是更低、差不多还是更高？”

候选尺度：

$$
-3,\ldots,+3
$$

但 v1 不直接假设：

$$
\boxed{
y^{rel}=A
}
$$

Relative EMA 主要用于：

- construct validation；
- sensitivity analysis；
- exploratory state-separation evidence。

不能把它未经验证地作为 Acute State 的直接 Ground Truth。

---

## 七、Repeated EMA Probe

为估计 observation noise，可对少量 EMA 使用近时间重复测量。

目标：

$$
\boxed{
Estimate\ / Inform\ R_{EMA}
}
$$

例如约少量 Primary EMA 后，在短时间内重复同一或等价测量。

其主要作用不是增加很多训练数据，而是帮助区分：

$$
\boxed{
MeasurementNoise
}
$$

与：

$$
\boxed{
ProcessVariation
}
$$

Formal Study 前应使用 Pilot repeated-item data 为：

$$
R_{EMA}
$$

建立 informative prior 或固定候选范围。

---

## 八、Sparse Multi-Item Calibration

可在较低频率下使用多题压力量表或简短校准测量。

它不替代高频 Primary EMA。

主要用于：

- construct validity；
- absolute scale calibration；
- participant-specific response-style diagnostics；
- sensitivity analysis。

必须保持：

$$
\boxed{
SparseCalibration
\neq
PrimaryMomentaryOutcome
}
$$

---

## 九、Missing EMA

Missing EMA 不进行时间插值后当作真实 observation。

因此禁止：

$$
\boxed{
MissingEMA
\rightarrow
InterpolatedPseudoLabel
}
$$

State Model 可以自然执行 prediction-only step：

$$
p(X_t\mid\mathcal I_{t^-})
$$

直到下一次真实 observation 到来。

Missingness 机制本身可以单独记录和分析，但 v1 不因为“没回答 EMA”直接修改 latent stress mean。

---

## 十、Pre-Assimilation 与 Post-Assimilation

对 EMA 时刻：

$$
t_k
$$

区分：

### 1. Prior / Pre-Assimilation State

$$
\boxed{
X_k^{-}
}
$$

表示在尚未读取当前 EMA 前，模型根据此前全部合法信息得到的状态分布。

对应：

$$
S_k^{-}
=
A_k^{-}+B_k^{-}
$$

### 2. Posterior / Post-Assimilation State

吸收：

$$
y_k
$$

后得到：

$$
\boxed{
X_k^{+}
}
$$

对应：

$$
S_k^{+}
=
A_k^{+}+B_k^{+}
$$

Formal prediction accuracy 必须使用：

$$
\boxed{
S_k^{-}
}
$$

而不是：

$$
S_k^{+}
$$

否则模型已经看过当前 Ground Truth。

---

## 十一、Prediction 与 Assimilation 的标准循环

在线循环：

```text
Previous Posterior
        ↓
Propagate Dynamics
        ↓
Incorporate Known Context
        ↓
Prior State X-
        ↓
Generate Pre-Assimilation Prediction
        ↓
EMA arrives
        ↓
Measurement Update
        ↓
Posterior State X+
        ↓
Next interval
```

每次正式 evaluation 都保存：

```text
forecast_origin
predicted_at
target_time
pre_assimilation_mean
pre_assimilation_variance
observed_ema
```

---

## 十二、Online State Vector

Online state estimation 的心理 latent vector 保持：

$$
\boxed{
x_i(t)
=
\begin{bmatrix}
A_i(t)\\
B_i(t)
\end{bmatrix}
}
$$

$C_D,C_U,F$ 不放入同一个 EMA-driven stochastic filter 作为可被 observation residual 任意修正的 latent components。

它们作为外生/context-derived state 输入：

$$
u_i(t)
$$

进入 dynamics。

这样保持：

$$
\boxed{
PsychologicalLatentState
\neq
ContextDerivedState
}
$$

---

## 十三、Online State Inference

v1 使用低维 Gaussian / EKF-like state inference。

概念上：

$$
x_k^{-}
=
f(
x_{k-1}^{+},
u_{k-1:k},
\theta
)
$$

预测 covariance：

$$
P_k^{-}
$$

Measurement：

$$
y_k
=
H x_k
+
\epsilon_k
$$

其中：

$$
\boxed{
H=
\begin{bmatrix}
1&1
\end{bmatrix}
}
$$

因为：

$$
y=A+B+\epsilon
$$

Measurement Update 后得到：

$$
x_k^{+},
\quad
P_k^{+}
$$

具体数值积分与 covariance propagation 由实现层定义，但必须保留完整 uncertainty。

---

## 十四、Input Uncertainty

未来和当前输入都可能不确定，例如：

- lifecycle unknown；
- appraisal unknown；
- $W^{rem}$ unknown；
- recovery occurrence unknown；
- Bot response seen unknown；
- future task progress unknown；
- sleep quality unknown。

因此 State Prediction 不能只使用一个硬填充后的 deterministic input。

v1 支持：

$$
\boxed{
InputUncertainty
}
$$

通过：

- exact discrete marginalization；
- scenario sampling；
- moment approximation；

进入预测。

---

## 十五、Appraisal Uncertainty 的在线处理

Appraisal 使用：

$$
Low,\ Medium,\ High
$$

categorical belief。

由于状态空间很小，online update 中优先使用 exact discrete marginalization。

例如：

$$
E[D^{eff}]
=
\sum_c
P(C^{exec}=c)
D^{eff}(c)
$$

Pressure 中最多枚举：

$$
3^3=27
$$

个：

$$
(I,C^{out},U^{perc})
$$

组合。

这样避免仅因为 appraisal uncertainty 引入额外 Monte Carlo noise。

---

## 十六、Lifecycle 与 Obligation Uncertainty

Lifecycle unknown 时，可以维护：

$$
P(
Lifecycle
)
$$

例如：

$$
P(
ATTENDED,
PARTIAL,
SKIPPED
)
$$

Remaining effort unknown 时维护：

$$
P(
W^{rem}
\mid
TaskClass,History,Evidence
)
$$

Online state update 应根据当前 belief 边缘化，不将 Unknown 强制映射为单一状态。

---

## 十七、Knowledge-Time Correctness

任意 forecast origin：

$$
t_0
$$

只允许使用：

$$
\boxed{
\mathcal I_{t_0}
}
$$

即：

> 在 $t_0$ 时系统真实已经知道的信息集合。

必须保存：

```text
event_time
created_at
reported_at
observed_at
known_at
updated_at
```

例如 18:00 用户说：

> “今天 10:00 的课我没去。”

在 retrospective reconstruction 中可以修正 10:00 事件。

但 12:00 的 prospective forecast 不得使用这条 18:00 才获得的信息。

---

## 十八、Retrospective Best Estimate 与 Prospective Known State

必须区分：

$$
\boxed{
RetrospectiveBestEstimate
}
$$

与：

$$
\boxed{
ProspectiveKnownState(t)
}
$$

前者用于：

- event audit；
- retrospective descriptive analysis；
- improving semantic/lifecycle labels。

后者用于：

- forecast evaluation；
- deployment simulation；
- Formal Study predictive claims。

正式预测指标只基于 Prospective Known State。

---

## 十九、Parameter Estimation 的层级

Parameter estimation 分成：

### 1. Representation Parameters

例如：

- semantic mapping；
- Appraisal coding；
- $\rho_{app}$；
- sleep transition parameters；
- $\tau_D$ 若冻结；
- capacity mapping；
- fixed temporal kernel shape。

这些不通过 EMA 与 stress dynamic parameters 一起自由拟合。

### 2. Dynamic Parameters

例如：

$$
\beta_D^A,
\beta_P^A,
\beta_R^A,
\beta_D^B,
\beta_U^B,
\beta_F^B,
\kappa_{\uparrow},
\kappa_B
$$

以及 person-level candidates：

$$
S_i^0,
g_i^A,
g_i^B,
\kappa_{\downarrow,i}
$$

### 3. Candidate Bot Parameter

$$
\beta_{BS}
$$

只有 Bot ablation 通过后才晋级正式模型。

---

## 二十、Offline Reference Inference

完整离线 reference inference 优先采用：

$$
\boxed{
Hierarchical\ Bayesian\ Inference
}
$$

并使用：

$$
\boxed{
HMC/NUTS
}
$$

作为 reference 方法。

目标包括：

- population parameters；
- person-level posterior；
- parameter covariance；
- latent state smoothing；
- identifiability diagnostics。

Reference model 主要用于研究阶段、Synthetic 和正式离线分析，不要求实时部署使用 HMC/NUTS。

---

## 二十一、Runtime Parameter Inference

如果需要 runtime personalization，可使用：

$$
\boxed{
MAP+Laplace
}
$$

作为 candidate approximation。

但只有在：

- Synthetic parameter recovery；
- posterior approximation；
- calibration；
- held-out forecast；

与 reference inference 足够一致时才启用。

不能因为运行快就默认替代 reference posterior。

---

## 二十二、Person-Level Parameter Promotion

最大候选：

$$
[
S_i^0,
g_i^A,
g_i^B,
\kappa_{\downarrow,i}
]
$$

但根据 participant data observability 逐项晋级。

必须保持：

$$
\boxed{
NoEvidence
\Rightarrow
StrongPooling
}
$$

而不是：

$$
NoEvidence
\Rightarrow
FreePersonalParameter
$$

每个 participant 可以拥有不同的实际 personalization depth。

---

## 二十三、Parameter Freeze 与 Formal Study

Formal Study 前需要冻结：

- model structure；
- representation mapping；
- semantic coding；
- Appraisal schema；
- temporal kernel rules；
- priors；
- global hyperparameters；
- EMA protocol；
- parameter-promotion rules；
- forecasting procedure；
- primary evaluation metrics。

冻结分为两层：

- Structure-Frozen：变量语义、因果与信息路径、哪些量进入哪个通道、哪些参数允许存在、哪些重复计数路径被禁止；
- Representation-Pending-Freeze：需要经 Scenario Annotation → Synthetic → Pilot 确定的具体映射与数值，包括 $\Psi_F$ transition mapping、sleep representation 数值、nap-to-$F$ fixed strength、$C^{ref}$ category-to-capacity mapping、social-evaluation temporal activation shape、$\rho_{app}$ 数值、$\tau_{BS}$ 固定值或先验范围、$G_{session}$。

Formal freeze 指两层都已完成并锁定。

Formal Study 中允许：

- state assimilation；
- person parameter learning；

但必须严格按照预注册 / 预声明规则运行。

不能在看到 Formal Study 结果后反复修改模型结构。

---

## 二十四、Future Forecast 的定义

对 forecast origin：

$$
t_0
$$

目标是预测：

$$
\boxed{
p(
S_i(t_0+h)
\mid
\mathcal I_{t_0}
)
}
$$

而不是只输出：

$$
\hat S_i(t_0+h)
$$

一个 deterministic point estimate。

预测必须传播至少三类 uncertainty：

$$
\boxed{
StateUncertainty
}
$$

$$
\boxed{
ParameterUncertainty
}
$$

$$
\boxed{
InputUncertainty
}
$$

---

## 二十五、Scenario-Based Forecast

Future context 通常不确定，因此使用 scenario ensemble。

第 $m$ 条 scenario 包含：

$$
\omega^{(m)}
=
[
FutureLifecycle,
FutureAppraisal,
FutureProgress,
FutureRecovery,
FutureSleep,
FutureExposure,\ldots
]
$$

每条 scenario：

1. 从当前 posterior state 开始；
2. 抽取或传播 parameter draw；
3. 抽取 coherent future input realization；
4. 沿 dynamics 向前传播；
5. 得到一条：

$$
S_i^{(m)}(t)
$$

最终形成：

$$
\boxed{
PredictiveDistribution
}
$$

---

## 二十六、Scenario Coherence

同一个 uncertainty variable 在单条 scenario 中必须保持内部一致。

例如：

若：

$$
Z_e^{seen,(m)}=0
$$

则该 scenario 中不能在后续时间又假装同一条消息已被看到，除非真实有新的 exposure event。

若 future task 被 scenario 判定为：

$$
COMPLETED
$$

则其：

$$
W^{rem}
$$

随后必须变为 0。

因此必须保持：

$$
\boxed{
ScenarioSampling
\neq
IndependentPointwiseNoise
}
$$

---

## 二十七、Morning / Day-Ahead 与 Online Forecast

建议至少区分：

### 1. Morning / Day-Ahead Forecast

forecast origin 在当天较早时刻。

主要使用：

- current state posterior；
- known schedule；
- known tasks；
- known sleep；
- current obligation state；
- future scenario uncertainty。

### 2. Online Forecast

当天过程中不断吸收：

- new EMA；
- task progress；
- lifecycle evidence；
- conversation evidence；
- actual recovery；
- changed schedule。

二者回答不同问题，应分别报告。

---

## 二十八、Clean Modeling 与 Deployment-Realistic Forecast

必须区分：

### Clean Modeling Evaluation

使用严格整理后的合法 context 与标准 inference pipeline，主要回答：

> 模型机制本身是否有预测价值？

### Deployment-Realistic Evaluation

允许真实系统中存在：

- missing context；
- delayed reports；
- semantic extraction error；
- lifecycle uncertainty；
- Bot exposure uncertainty。

主要回答：

> 系统实际部署时表现如何？

必须分别报告，不能混成一个指标。

---

## 二十九、Risk Probability

风险输出应基于 observed EMA 或 latent state的不同定义分别报告。

### Observed EMA Risk

$$
\boxed{
P(
y_{future}\ge\theta
)
}
$$

用于回答：

> 下一次真实 EMA 达到高压力阈值的概率。

### Latent Risk

$$
\boxed{
P(
S_{future}\ge\theta
)
}
$$

用于回答：

> latent stress 超过阈值的概率。

必须保持：

$$
\boxed{
ObservedRisk
\neq
LatentRisk
}
$$

如果正式实验的高压力 label 基于 EMA，则 primary risk evaluation 应使用：

$$
P(y\ge\theta)
$$

---

## 三十、Prediction Interval 与 Calibration

Forecast 输出至少包括：

- predictive mean / median；
- uncertainty interval；
- threshold probability。

Formal evaluation 不能只看 MAE。

还需要检查：

$$
\boxed{
Coverage
}
$$

$$
\boxed{
Calibration
}
$$

$$
\boxed{
Sharpness
}
$$

对于风险概率，使用：

- calibration plot；
- Brier score；
- calibration intercept/slope；

等候选方法。

---

## 三十一、Primary Prospective Accuracy

Primary modeling outcome 推荐继续使用 participant-balanced prospective absolute EMA MAE。

概念上：

$$
MAE_i
=
\frac{1}{n_i}
\sum_k
|
y_{i,k}-\hat y_{i,k}^{-}
|
$$

然后 participant-balanced：

$$
\boxed{
MAE_{PB}
=
\frac{1}{N}
\sum_i
MAE_i
}
$$

这样不会让 EMA 较多的 participant 完全主导整体指标。

预测必须使用 pre-assimilation output。

---

## 三十二、Prequential / Rolling-Origin Evaluation

Formal evaluation 使用：

$$
\boxed{
Prequential
}
$$

或：

$$
\boxed{
RollingOrigin
}
$$

而不是随机打乱所有 EMA 做普通 train/test split。

每一个 forecast origin 只使用当时之前合法可用的数据。

这样保持真实部署顺序。

---

## 三十三、Baselines

至少比较：

### Persistence

$$
\hat y_{t+h}=y_t
$$

### Personal Mean

$$
\hat y=\bar y_i
$$

### Time-of-Day Baseline

根据 participant / population 的 time-of-day pattern 预测。

必要时可加入简单 context baseline。

主模型只有稳定超过这些低复杂度基线，才具有实际建模价值。

---

## 三十四、Synthetic 的总体目的

Synthetic 不是为了证明模型“能拟合自己”。

主要目的：

1. 检查 structural / practical identifiability；
2. 检查 person-level parameter recoverability；
3. 检查 latent $A/B$ decomposition；
4. 评估 EMA frequency / event probe 设计；
5. 测试 missingness / corruption；
6. 估计 oracle ceiling 与 deployable ceiling；
7. 决定哪些机制应进入 Pilot / Formal Study。

---

## 三十五、True World 与 Observed World 分离

Synthetic 必须区分：

### True World

保存真实：

- latent state；
- true parameters；
- true lifecycle；
- true appraisal；
- true remaining effort；
- true sleep；
- true exposure。

### Observed World

加入现实误差：

- missing EMA；
- delayed report；
- noisy appraisal；
- lifecycle ambiguity；
- context missingness；
- wrong semantic extraction；
- imperfect read receipt；
- task effort uncertainty。

因此：

$$
\boxed{
SyntheticObservedData
\neq
PerfectOracleData
}
$$

---

## 三十六、Oracle 与 Deployable Ceiling

至少比较：

### Oracle Input Model

使用真实 Synthetic context / lifecycle / appraisal。

回答：

> 如果所有上下文都知道，动力学本身上限如何？

### Deployable Input Model

只使用模拟真实系统可获得的信息。

回答：

> partial observability 与 extraction error 会损失多少性能？

二者差距可以帮助区分：

$$
\boxed{
ModelError
}
$$

与：

$$
\boxed{
ObservationPipelineError
}
$$

---

## 三十七、Parameter Recovery 指标

对每个参数至少检查：

$$
\boxed{
Bias
}
$$

$$
\boxed{
RMSE
}
$$

$$
\boxed{
IntervalCoverage
}
$$

$$
\boxed{
PosteriorContraction
}
$$

$$
\boxed{
PosteriorCorrelation
}
$$

同时检查：

- multimodality；
- ridge；
- boundary sticking；
- prior domination。

不能只看 posterior mean 是否“看起来合理”。

---

## 三十八、Latent State Recovery

Synthetic 必须分别检查：

$$
\boxed{
RMSE_S
}
$$

$$
\boxed{
RMSE_A
}
$$

$$
\boxed{
RMSE_B
}
$$

$A$ 是 signed latent component，而非独立的 0–10 量表；$RMSE_A$ 的检查必须允许负值，也不能用 $A$ 与 EMA 的绝对尺度直接比较。

因为完全可能：

$$
\hat S\approx S
$$

但：

$$
\hat A,\hat B
$$

严重错分。

因此总压力预测准确不能替代 latent decomposition validation。

---

## 三十九、必须专项审计的参数组

### 1. Acute Scale

$$
\boxed{
(g_A,\beta_D^A,\beta_P^A,\beta_R^A)
}
$$

检查 scale anchor 是否有效。

### 2. Background Scale

$$
\boxed{
(g_B,\beta_D^B,\beta_U^B,\beta_F^B,S^0)
}
$$

### 3. Slow-Time Separation

$$
\boxed{
(\tau_D,\kappa_B)
}
$$

### 4. Recovery Magnitude / Speed

$$
\boxed{
(\beta_R^A,\kappa_{\downarrow})
}
$$

### 5. Measurement / Process Noise

$$
\boxed{
(R_{EMA},q_A,q_B)
}
$$

### 6. Pressure / Unresolved Obligation

$$
\boxed{
(\beta_P^A,\beta_U^B)
}
$$

### 7. Bot Support / Recovery

$$
\boxed{
(\beta_{BS},\beta_R^A)
}
$$

### 8. Latent Decomposition

$$
\boxed{
(A,B)
}
$$

---

## 四十、正交 Synthetic 场景

Synthetic 必须主动制造机制可区分场景，而不是仅模拟“自然相关”的输入。

例如：

### $C_U$ vs Deadline Pressure

```text
same W_remaining
different deadline
```

以及：

```text
different W_remaining
similar urgency
```

### Demand vs Pressure

```text
high execution demand
low stakes
```

与：

```text
low execution demand
high deadline/social pressure
```

### Recovery Magnitude vs Recovery Speed

```text
strong recovery input
slow kappa_down
```

与：

```text
weak recovery input
fast kappa_down
```

### Bot Support vs Actual Recovery

```text
support message only
```

与：

```text
actual recovery behavior only
```

以及两者同时发生的情况。

---

## 四十一、Missingness / Corruption Audit

Synthetic 至少测试：

- EMA missingness；
- lifecycle missingness；
- appraisal Unknown；
- remaining effort uncertainty；
- delayed report；
- sleep missingness；
- semantic extraction noise；
- Bot exposure uncertainty。

重点观察：

- point prediction degradation；
- interval coverage；
- parameter bias；
- calibration；
- failure mode。

---

## 四十二、Parameter-Specific Observability Gate

每个 person-level parameter 的晋级必须通过对应 evidence count / variation gate。

可以记录：

```text
parameter
eligible
reason
supporting_observations
effective_variation
posterior_contraction
```

如果 parameter 没有足够观测：

$$
\boxed{
StrongPooling
}
$$

而不是强行输出一个 personalized point estimate。

---

## 四十三、Model Ablation

需要分机制进行 ablation，而不是只比较一个 Full Model。

至少包括：

### Core Stress Dynamics

```text
Base
+ Acute D/P/R
+ Slow C_D
+ Slow C_U
+ Sleep F
```

### Personalization

```text
P0: Population Only
P1: + S0
P2: + g_A
P3: + g_B
P4: + kappa_down
```

### Bot

```text
BI0: No Bot Dynamics
BI1: Support Only
BI2: Extended Candidate
```

Ablation 命名在最终研究文档中必须唯一。Personalization ablation 与第 6 份的 parameter-promotion level 使用同一套 P0–P4 命名，Bot ablation 使用 BI0–BI2，不再出现 M0–M4。

---

## 四十四、模型晋级 Gate

某机制或参数只有同时满足以下条件，才允许进入下一阶段：

### Mechanism Gate

1. semantic definition 清晰；
2. 数据来源可观测；
3. parameter recovery 可接受；
4. posterior correlation 不出现严重 ridge；
5. held-out prospective prediction 有稳定增量；
6. calibration 不恶化；
7. 复杂度与样本量匹配。

### Person-Level Parameter Gate

1. participant 有对应观测；
2. posterior contraction 足够；
3. recovery bias 可接受；
4. held-out prediction 有增量。

---

## 四十五、开发阶段顺序

当前推荐研究流程：

```text
Scenario Annotation
        ↓
Synthetic
        ↓
Pilot
        ↓
Synthetic Recalibration
        ↓
Freeze
        ↓
Formal Study
```

### Scenario Annotation

用于：

- 检查语义映射；
- 设计极端/正交案例；
- 校准 representation hyperparameters。

### Synthetic

用于：

- identifiability；
- parameter recovery；
- design optimization。

### Pilot

用于：

- 真实数据可行性；
- response burden；
- missingness；
- prior / noise / representation calibration。

### Synthetic Recalibration

将 Pilot 观察到的真实 missingness 与 variation 回灌 Synthetic。

### Freeze

冻结正式研究规格。

### Formal Study

按预先声明规则执行，不再根据最终结果随意修改模型。

---

## 四十六、Formal Study 中允许与不允许的学习

Formal Study 中允许：

- online state assimilation；
- 按预声明规则更新 stable profile；
- person parameter posterior learning；
- scenario forecast；
- predeclared parameter promotion。

不允许：

- 看正式结果后修改 semantic mapping；
- 修改 Appraisal schema；
- 调整 kernel 以提高 MAE；
- 重新选择 primary outcome；
- 用 future information 回填历史 forecast；
- 根据最终 EMA 调整 representation rules。

---

## 四十七、预测研究与 Care Causal Study 的边界

Prediction Model 估计：

$$
\boxed{
p(
S_{future}
\mid
CurrentInformation
)
}
$$

Future Care Trial 估计：

$$
\boxed{
CausalEffect(
Care
\rightarrow
Outcome
)
}
$$

两者不能混淆。

必须保持：

$$
\boxed{
NoCareForecast
\neq
CounterfactualOutcome
}
$$

以及：

$$
\boxed{
PredictionAssociation
\neq
TreatmentEffect
}
$$

Bot predictive dynamics 可以帮助 forecast，但不能替代 randomized causal analysis。

---

## 四十八、最终输出接口

部署侧每个 forecast 至少应能够输出：

```text
forecast_origin
target_time
predicted_mean
predicted_median
prediction_interval
P_observed_EMA_above_threshold
P_latent_S_above_threshold
state_uncertainty
input_uncertainty
parameter_uncertainty
model_version
known_at_cutoff
```

研究侧额外保存：

```text
pre_assimilation_state
post_assimilation_state
parameter_posterior
scenario_id
scenario_weight
input_provenance
prediction_error
```

---

## 四十九、当前冻结边界

1. Primary EMA 测量 $S=A+B$，不直接观测子状态。
2. Measurement time 使用 `answered_at`。
3. Conversational stress expression 不等于 Primary EMA。
4. Missing EMA 不插值成 pseudo-label。
5. Formal accuracy 使用 pre-assimilation prediction。
6. Online psychological state vector保持 $[A,B]$。
7. $C_D/C_U/F$ 不接受 EMA residual 直接修正。
8. Input uncertainty 必须显式传播。
9. Appraisal uncertainty 优先用 exact discrete marginalization。
10. Forecast 必须遵守 `known_at`。
11. Retrospective best estimate 与 prospective known state 分开。
12. Representation parameters 与 dynamic parameters 分开估计。
13. Offline reference inference 使用 hierarchical Bayesian + HMC/NUTS。
14. Runtime MAP+Laplace 只有验证通过后才允许使用。
15. Person-level parameter 按 observability gate 晋级。
16. Future forecast 输出 predictive distribution，不只输出 point curve。
17. Forecast 至少传播 state、parameter、input 三类 uncertainty。
18. Morning/day-ahead 与 online forecast 分开报告。
19. Clean modeling 与 deployment-realistic evaluation 分开。
20. Observed EMA risk 与 latent risk 分开。
21. Primary accuracy 使用 participant-balanced prospective MAE。
22. Formal evaluation 使用 prequential / rolling-origin。
23. Synthetic 必须区分 True World 与 Observed World。
24. Synthetic 必须分别报告 $S/A/B$ state recovery。
25. Parameter recovery 必须检查 bias、RMSE、coverage、contraction 与 posterior correlation。
26. Synthetic 必须主动构造正交机制场景。
27. Missingness / corruption 必须进入 Synthetic。
28. Representation、priors、EMA protocol、promotion rule 与 evaluation protocol 在 Formal Study 前冻结。
29. Prediction Model 与未来 Care causal estimator 必须分离。
30. Observation noise 统一记为 $R_{EMA}=\sigma_{EMA}^2$。
31. $A$ 是 signed acute latent component，$A/B$ 不能用 EMA 绝对尺度直接校验。
32. 机制结构冻结与 representation 数值冻结分开管理。
