# MindFlow Bot Interaction 与 Proactive Policy 建模

> 本文整理当前已经确认的 Bot Interaction 与 Proactive Policy 设计。本文只覆盖 User-Initiated / Bot-Initiated 交互、主动触发策略、Session/Thread/Response Unit、Support Representation、Exposure、Support Dynamics 以及未来 Care Trial 的边界。本文不重复 Event D/P/R 主链、Latent Stress Dynamics 和 Measurement Model。

---

## 一、模块定位

Bot Interaction 不等价于普通生活事件，也不直接并入 Event D/P/R 主链。

顶层结构：

```text
Conversation / Proactive Policy
            ↓
   User-Initiated / Bot-Initiated
            ↓
      Bot Response Unit
            ↓
   Support Representation
            ↓
      Exposure / Seen
            ↓
          Q_BS(t)
            ↓
      Acute Stress State A(t)
```

必须保持：

$$
\boxed{
Origin
\neq
Opportunity
\neq
Availability
\neq
Action
\neq
Content
\neq
Exposure
\neq
Effect
}
$$

其中：

- `Origin`：交互由谁发起；
- `Opportunity`：是否存在 care / information opportunity；
- `Availability`：是否适合主动联系；
- `Action`：系统实际采取 None / Defer / Sense / Care；
- `Content`：机器人回复内容；
- `Exposure`：用户是否真正看到；
- `Effect`：真实进入 Stress Dynamics 的支持效应。

---

## 二、Interaction Origin

定义：

$$
\boxed{
Origin_j\in\{USER,\ BOT\}
}
$$

### 1. User-Initiated

用户主动发起：

$$
\boxed{
UserInitiated
\Rightarrow
ImmediateResponse
}
$$

不经过 proactive availability gate。

即使当前日程显示：

- 上课；
- 自习；
- 会议；
- 固定任务；

只要用户主动发起对话，机器人正常响应。

必须保持：

$$
\boxed{
UserInitiatedChat
\neq
StressSignal
}
$$

以及：

$$
\boxed{
ConversationalStressExpression
\neq
PrimaryEMA
}
$$

用户主动交互主要提供：

- Conversation Evidence；
- Behavioral Evidence；
- Context Update；
- Support Opportunity。

### 2. Bot-Initiated

机器人主动交互必须经过 Proactive Policy：

```text
Risk / Information Need
        ↓
Opportunity
        ↓
Hard Admissibility
        ↓
Receptivity
        ↓
Refractory / Measurement Protection
        ↓
Action
```

因此：

$$
\boxed{
BotInitiated
\Rightarrow
PolicyGated
}
$$

---

## 三、Proactive Opportunity

机器人主动行为至少区分两类机会。

### 1. Care Opportunity

定义：

$$
\boxed{
O_i^{care}(t)
}
$$

表示当前是否存在值得提供 explicit care 的压力风险或支持机会。

Care trigger 优先基于：

$$
\boxed{
\hat S_{t+h}^{no-care}
}
$$

即 no-future-care forecast。

这样避免：

```text
预测高压力
→ 预期未来会 Care
→ 预测因 Care 被降低
→ 又不再触发 Care
```

形成 policy-prediction self-cancellation。

### 2. Information Opportunity

定义：

$$
\boxed{
O_i^{info}(t)
}
$$

表示当前是否存在高价值 context 缺口，例如：

- 高影响事件 lifecycle unknown；
- task progress 长时间 stale；
- sleep missing；
- recurring-event appraisal 缺失；
- context uncertainty 过高。

必须保持：

$$
\boxed{
O^{info}\neq O^{care}
}
$$

缺少信息不等于需要关怀。

---

## 四、Hard Proactive Admissibility

定义：

$$
\boxed{
H_i(t)\in\{0,1\}
}
$$

回答：

> 根据当前已经明确知道的信息，此时是否允许系统主动发起交互？

以下情况通常令：

$$
H_i(t)=0
$$

例如：

- exam；
- 已知高投入课程；
- 明确 focus / self-study session；
- meeting；
- sleep；
- protected measurement window；
- 其他明确 no-contact 状态。

若：

$$
H_i(t)=0
$$

则：

$$
\boxed{
Send=0
}
$$

系统选择：

```text
None
或
Defer
```

而不是先发送，再通过压力动力学解释打扰。

---

## 五、Proactive Receptivity

即使：

$$
H_i(t)=1
$$

也不能推出用户一定有空。

必须保持：

$$
\boxed{
CalendarFree
\neq
ActuallyFree
}
$$

v1 使用：

$$
\boxed{
Recp_i^{pro}(t)
\in
\{
Low,\ Unknown,\ Likely
\}
}
$$

正文中统一写作 `ProactiveReceptivity`，避免与 Recovery 的 $R^{eff}$、Bot Relevance 的 $Rel_r$ 以及 EMA observation noise 的 $R_{EMA}$ 混淆。

例如：

```text
正在明确课程 / 考试
→ Hard unavailable

刚下课且有明确空档
→ Likely

纯 calendar 空白
→ Unknown

临近下一项高压力事件
→ Low
```

必须保持：

$$
\boxed{
NoKnownConflict
\neq
ConfirmedAvailability
}
$$

---

## 六、Refractory 与 Measurement Protection

定义：

$$
Z_i^{ref}(t)
=
\mathbf1(
\text{refractory passed}
)
$$

表示 Bot-Initiated proactive interaction 是否通过 refractory 约束。Refractory eligibility 使用 $Z_i^{ref}(t)$；Guidance 继续使用 $G_r$，两者不再共用符号 $G$。

定义：

$$
M_i(t)
=
\mathbf1(
\text{not in protected measurement window}
)
$$

表示当前是否不处于 protected EMA measurement window。

Refractory 只限制：

$$
\boxed{
BOT\text{-}Initiated
}
$$

不限制 User-Initiated conversation。

---

## 七、Proactive Eligibility

定义：

$$
\boxed{
E_i^{pro}(t)
=
H_i(t)
Z_i^{ref}(t)
M_i(t)
}
$$

若：

$$
E_i^{pro}(t)=0
$$

则：

$$
Action(t)\in\{None,Defer\}
$$

若：

$$
E_i^{pro}(t)=1
$$

再结合：

$$
O_i^{care}(t),
\quad
O_i^{info}(t),
\quad
Recp_i^{pro}(t)
$$

选择：

$$
\boxed{
Action(t)
\in
\{
None,\ Defer,\ Sense,\ Care
\}
}
$$

v1 不使用自由线性总分：

$$
w_1Risk+w_2Availability+w_3Information+\cdots
$$

因为上述变量语义不同，不应被强行压成一个任意评分。

---

## 八、Defer 的定义

必须保持：

$$
\boxed{
Defer
=
ReevaluateLater
}
$$

而不是：

$$
\boxed{
Defer
=
QueuedMessage
}
$$

下一 decision point 需要重新计算：

$$
O^{care},
\quad
O^{info},
\quad
H,
\quad
Recp^{pro}
$$

而不是机械发送过时消息。

---

## 九、Session 与 Semantic Thread

### 1. Exposure Session

定义：

$$
\boxed{
Session
=
TemporalInteractionEpisode
}
$$

Session 主要依据 temporal continuity 切分，不依据 topic continuity。

若：

$$
\Delta t>G_{session}
$$

则形成新 Session。

Session 主要用于：

- grouping；
- audit；
- engagement statistics；
- care-trial attribution；
- behavioral evidence；
- interaction role composition。

Session 不直接定义 Support Kernel。

### 2. Semantic Thread

定义：

$$
\boxed{
Thread
=
SemanticContinuity
}
$$

同一 topic 可以跨多个 Session。

例如同一“论文实验焦虑”可以隔几小时甚至隔天继续，但仍属于同一 Thread。

必须保持：

$$
\boxed{
Session
\neq
Thread
}
$$

---

## 十、Session Gap

定义：

$$
\boxed{
G_{session}
}
$$

表示将连续 message stream 切分为 temporal interaction episodes 的 inactivity gap。

Pilot 中优先根据 inter-message gap：

$$
\Delta_j=t_j-t_{j-1}
$$

的经验分布确定。

可对：

$$
\log\Delta
$$

拟合 two-component mixture：

$$
p(\log\Delta)
=
\pi
N(\mu_{within},\sigma_{within}^2)
+
(1-\pi)
N(\mu_{between},\sigma_{between}^2)
$$

候选边界：

$$
P(within\mid\Delta^*)
=
P(between\mid\Delta^*)
$$

并令：

$$
\boxed{
G_{session}=\Delta^*
}
$$

$G_{session}$ 的机制结构已经确定（structure-frozen）：它只由 temporal continuity 决定，属于 representation layer。其具体数值属于 Representation-Pending-Freeze，需由 Pilot inter-message gap 经验分布确定，并在 Formal Study 前冻结。

不得通过 stress forecast MAE 调整 $G_{session}$。

---

## 十一、Bot Response Unit

真正驱动 Support Dynamics 的最小单位是：

$$
\boxed{
BotResponseUnit
}
$$

表示机器人一次完整的、面向用户的回复行为。

一个 Bot Response Unit 可以包含多个 Semantic Acts，例如：

```text
Bot Response Unit
├── Validation
├── Guidance
└── Care
```

但：

$$
\boxed{
1\ BotResponseUnit
\Rightarrow
at\ most\ 1\ SupportPulse
}
$$

如果一次完整模型回复因技术原因被拆成多个消息 bubble，应使用统一：

```text
response_action_id
```

合并为一个 Bot Response Unit。

这样避免 UI 分片或 semantic-act 数量改变 Stress Dynamics。

---

## 十二、Interaction Role

Bot Response Unit 可以包含或对应：

```text
ordinary_information
task_execution_support
context_clarification
relational_sensing
emotional_validation
coping_guidance
explicit_care
recovery_suggestion
safety_support
```

v1 不为不同 role 设置不同 $\beta$ 或不同 kernel。

采用：

$$
\boxed{
Role
\rightarrow
SupportRepresentation
\rightarrow
SupportPotential
}
$$

---

## 十三、Support-Domain Gate

定义：

$$
\boxed{
J_r^{sup}\in\{0,1\}
}
$$

表示 Bot Response Unit $r$ 是否包含直接面向：

- stress；
- coping；
- emotional support；

的 supportive content。

典型路由：

| Response 类型 | $J^{sup}$ | 主要路径 |
|---|---:|---|
| 普通知识问答 | 0 | 无直接 Stress Input |
| 普通技术 / 作业帮助 | 0 | Task / Ordinary Response |
| 真正推动任务完成 | 0 | TaskProgress $\rightarrow C_U$ |
| Context Clarification | 0 | Structured Evidence |
| Relational Sensing | 0 | Context Acquisition |
| Emotional Validation | 1 | $Q_{BS}$ |
| Coping Guidance | 1 | $Q_{BS}$ |
| Explicit Care | 1 | $Q_{BS}$ |
| User-Requested Emotional Support | 1 | $Q_{BS}$ |
| Recovery Suggestion | 条件性 | 若属于 coping support 则进入 $Q_{BS}$ |
| Safety Support | 单独标记 | 不进入普通 Care effect evaluation |

必须避免：

$$
\boxed{
AnyHelpfulBotResponse
\Rightarrow
SupportInput
}
$$

---

## 十四、普通任务帮助与 Bot Support 的边界

若机器人真实帮助任务推进，例如：

> “帮我把今晚报告提纲整理出来。”

并且确实造成：

$$
TaskProgress\uparrow
$$

$$
W^{rem}\downarrow
$$

则其主要路径为：

$$
\boxed{
TaskProgress
\rightarrow
C_U
\rightarrow
B
}
$$

普通：

```text
task_execution_support
ordinary_information
```

不自动进入：

$$
Q_{BS}
$$

只有真正 stress/coping/emotional-support-oriented response 才进入 Bot Support Dynamics。

---

## 十五、Support Representation

Support Potential 保留三个低维字段：

$$
\boxed{
V_r,\quad G_r,\quad Rel_r
}
$$

其中：

### Validation

$$
V_r
=
\text{Relational Validation / Supportive Acknowledgment}
$$

表示机器人是否承接用户体验、认可当前困难并提供恰当 relational presence。

### Guidance

$$
G_r
=
\text{Coping-Oriented Action Guidance}
$$

只表示：

- coping；
- stress management；
- 当前低负担支持策略。

不包括普通任务或技术建议。

### Relevance

$$
Rel_r
=
\text{Contextual Relevance}
$$

表示支持内容与发送时已知的：

- current context；
- user request；
- structured evidence；

是否匹配。

三个字段采用：

$$
\boxed{
V_r,G_r,Rel_r\in\{0,0.5,1\}
}
$$

避免生成假精确心理评分。

---

## 十六、Support Potential

定义：

$$
\boxed{
S_r^{pot}
=
J_r^{sup}
Rel_r
\left[
1-(1-V_r)(1-G_r)
\right]
}
$$

其中：

- Validation 与 Guidance 是两种互补 supportive affordance；
- Relevance 作为 appropriateness gate；
- v1 不学习额外 $w_V,w_G,w_R$。

因此：

$$
0\le S_r^{pot}\le1
$$

---

## 十七、Personalization 的定位

记录：

$$
\boxed{
P_r^{pers}
}
$$

表示 response 是否使用：

- participant context；
- stable personal profile；
- historical preference。

但：

$$
\boxed{
P_r^{pers}\notin S_r^{pot}
}
$$

必须保持：

$$
\boxed{
Personalized
\neq
Effective
}
$$

v1 中 Personalization 仅作为：

- metadata；
- strategy descriptor；
- candidate moderator；
- future ablation variable。

---

## 十八、Support Exposure

Support Potential 不等于真实 exposure。

定义：

$$
\boxed{
Z_r^{seen}\in\{0,1\}
}
$$

表示用户是否实际看到 Bot Response Unit。

若有可靠：

```text
opened_at
```

则：

$$
Z_r^{seen}=1
$$

且：

$$
t_r^{seen}=opened\_at
$$

若没有 read receipt，但用户之后发生 reply，则可以确认：

$$
Z_r^{seen}=1
$$

但保守使用：

$$
t_r^{seen}=reply\_at
$$

不能利用未来 reply 提前回填过去时点的 exposure。

若 exposure 不确定，则保持：

$$
Z_r^{seen}=Unknown
$$

Future scenario 中可使用：

$$
Z_r^{seen,(m)}
\sim
Bernoulli(p_r^{seen})
$$

且在单条 scenario 内保持一致。

---

## 十九、Effective Support

定义：

$$
\boxed{
S_r^{eff}
=
Z_r^{seen}
S_r^{pot}
}
$$

因此：

$$
Z_r^{seen}=0
\Rightarrow
S_r^{eff}=0
$$

真正进入 Support Dynamics 的是：

$$
S_r^{eff}
$$

而不是已发送消息数量。

---

## 二十、Support Kernel

v1 所有 Support Role 与 Interaction Origin 共用统一 kernel：

$$
\boxed{
K_{BS}(\Delta t)
=
e^{-\Delta t/\tau_{BS}}
\mathbf1(\Delta t\ge0)
}
$$

其中：

$$
\tau_{BS}>0
$$

为 Bot Support effect decay time constant。

必须保持：

$$
\boxed{
\tau_B
=
\frac{1}{\kappa_B}
}
$$

只表示 Background stress time constant（见第 6 份）。Bot Support 的时间常数统一记为 $\tau_{BS}$，不得再写作 $\tau_B^{Bot}$。

也可使用 half-life：

$$
\boxed{
h_{BS}=\tau_{BS}\ln2
}
$$

表示。

v1 保持：

$$
\boxed{
h_{BS}^{USER}
=
h_{BS}^{BOT}
=
h_{BS}
}
$$

以及：

$$
\boxed{
h_{BS}^{validation}
=
h_{BS}^{guidance}
=
h_{BS}^{care}
=
h_{BS}
}
$$

kernel 的机制结构已经确定（structure-frozen）；$K_{BS}$ 的具体 shape 与 $\tau_{BS}$ 的固定值或先验范围属于 Representation-Pending-Freeze。

原因是控制参数预算与 identifiability，而不是假设真实世界完全相同。

---

## 二十一、Support Pulse

单个 Bot Response Unit 的动态贡献：

$$
\boxed{
u_r^{BS}(t)
=
S_r^{eff}
e^{-(t-t_r^{seen})/\tau_{BS}}
\mathbf1(t\ge t_r^{seen})
}
$$

---

## 二十二、Bot Support Aggregation

多个 supportive response 使用 saturation aggregation：

$$
\boxed{
Q_{BS}(t)
=
1-
\prod_r
[
1-u_r^{BS}(t)
]
}
$$

因此：

$$
0\le Q_{BS}(t)\le1
$$

多个 supportive response 可以叠加，但不会出现：

$$
10\ messages
=
10\times support
$$

Session duration、turn count、message density 不再额外乘入 $Q_{BS}$。

---

## 二十三、User-Origin 与 Bot-Origin 的 Dynamics

两类 interaction 保存不同：

```text
origin = USER
origin = BOT
```

但 v1 共用：

$$
\boxed{
\beta_{BS}^{USER}
=
\beta_{BS}^{BOT}
=
\beta_{BS}
}
$$

以及：

$$
\boxed{
\tau_{BS}^{USER}
=
\tau_{BS}^{BOT}
=
\tau_{BS}
}
$$

Interaction Origin 第一版主要影响：

$$
\boxed{
Exposure/SelectionProcess
}
$$

而不是：

$$
\boxed{
StressCoefficient
}
$$

只有在 Synthetic recoverability 和 held-out evidence 支持时，才考虑 origin-specific extension。

---

## 二十四、User-Initiated Behavioral Evidence

用户主动聊天本身可以提供当前真实行为证据，但不能直接改变 Stress。

候选：

$$
\boxed{
E_{j,e}^{beh}
\in
\{
Congruent,\ Ambiguous,\ Incongruent
\}
}
$$

例如课程时间内：

| 行为 | Evidence |
|---|---|
| 询问当前课程内容 | Congruent |
| 偶尔一条无关消息 | Ambiguous |
| 长时间高密度无关对话 | Incongruent |
| 明确说“今天没去” | Explicit lifecycle evidence |

路径必须是：

$$
\boxed{
ConversationBehavior
\rightarrow
Lifecycle/EngagementEvidence
\rightarrow
StructuredEventState
}
$$

而不是：

$$
ConversationBehavior
\rightarrow
Stress
$$

---

## 二十五、User Interaction Intent

可记录：

```text
ordinary_query
task_support
context_update
casual_conversation
disclosure
emotional_support_seeking
problem_solving
safety_related
```

但：

$$
\boxed{
EmotionalSupportSeeking
\neq
HighStress
}
$$

Intent 主要用于：

- response routing；
- context interpretation；
- process analysis；
- stratified evaluation。

不直接作为 Stress Input。

---

## 二十六、Recovery Suggestion 与 Actual Recovery

Bot 可能建议：

> “如果方便，可以出去走十分钟。”

若属于 coping guidance，可产生：

$$
Q_{BS}>0
$$

如果用户随后真实散步，则：

$$
ActualWalking
\rightarrow
Q_R
$$

两者属于不同 causal stages：

$$
\boxed{
BotSuggestion
\rightarrow
SupportiveExposure
}
$$

$$
\boxed{
ActualRecoveryBehavior
\rightarrow
RecoveryInput
}
$$

进入正式模型前需要检查：

$$
Corr(Q_{BS},Q_R)
$$

若高度共线，则需要在 Synthetic / Pilot 中判断两条路径能否区分。

---

## 二十七、Burden Channel 的当前定位

当前不将：

$$
Q_{BI}
$$

作为 v1 默认机制。

可预防的 interaction burden 优先通过：

```text
PreSendQualityGate
AvailabilityGate
RefractoryRule
LowBurdenContent
```

在发送前规避。

当前原则：

$$
\boxed{
Policy\ prevents\ predictable\ burden
}
$$

$$
\boxed{
Dynamics\ models\ realized\ supportive\ exposure
}
$$

未知 busy context、主观 annoyance、不可预见 interruption 等 residual effect 第一版不建立独立 burden state。

只有当 Pilot 显示：

1. 良好 policy 后仍存在稳定 residual burden；
2. burden predictor 可观测；
3. effect 可识别；
4. 增加 burden channel 改善 prospective prediction；

才考虑：

$$
Q_{BI}
$$

作为 extension。

---

## 二十八、Pre-Send Quality Gate

可检测的 inappropriate response 应在发送前处理：

```text
Candidate Response
        ↓
Quality Gate
        ↓
PASS / REGENERATE
```

该层属于：

$$
\boxed{
AgentQualityControl
}
$$

而不是 Stress Dynamics。

已发送的质量问题记录：

```text
quality_incident
reason
detected_at
```

用于：

- audit；
- safety；
- sensitivity analysis；
- Agent optimization。

v1 不直接：

$$
QualityIncident
\rightarrow
A^{eq}
$$

---

## 二十九、Helpfulness / Annoyance 的定位

Pilot 中可以稀疏收集：

```text
helpfulness
relevance
annoyance
interruption
```

用于：

$$
\boxed{
ConstructValidation
}
$$

但这些 post-interaction outcomes 不能回写：

$$
S_r^{pot}
$$

或 treatment definition。

---

## 三十、Bot Interaction Coding

建立：

```text
Bot Interaction Coding Manual v1.0
```

主要定义：

```text
J_sup
Validation V
Guidance G
Relevance Rel
Personalization Metadata
Interaction Origin
Interaction Role
Interaction Intent
```

Human Annotation 只能看到：

- 当前 Bot Response；
- 必要的最近 conversation context；
- 发送时已经 known 的 structured context；
- interaction role。

不能看到：

- future EMA；
- future user reply；
- future helpfulness；
- future annoyance；
- future task completion。

必须：

$$
\boxed{
BlindToDownstreamOutcome
}
$$

有序特征建议使用：

$$
\boxed{
Krippendorff's\ \alpha_{ordinal}
}
$$

若长期无法达到可接受一致性，则 revise/drop 对应 feature。

---

## 三十一、LLM 在 Bot Coding 中的职责

LLM 只作为 validated automatic coder。

流程：

```text
Coding Manual
        ↓
Independent Human Annotation
        ↓
Consensus Gold Set
        ↓
Reliability Check
        ↓
Freeze Schema
        ↓
LLM Structured Coder
        ↓
Validate Against Gold Set
```

推荐输出：

```text
support_gate
validation_level
guidance_level
relevance_level
personalization_level
confidence
evidence_span
abstain
coder_version
```

不输出自由连续“心理效果真值”。

---

## 三十二、Support Dynamics 接口

Bot Support 最终向 Latent Stress Dynamics 提供：

$$
\boxed{
Q_{BS}(t)
}
$$

在 candidate BI1 模型中：

$$
\boxed{
A_i^{eq}(t)
=
\cdots
-
\beta_{BS}Q_{BS}(t)
}
$$

其中：

$$
\beta_{BS}\ge0
$$

表示 population-level acute relief effect of Bot Support。

$\beta_{BS}$ 是否进入最终正式模型，需要通过 Bot ablation 与 identifiability 检验。

---

## 三十三、Bot Interaction Ablation

推荐：

### BI0：No Bot Dynamics

$$
\boxed{
Q_{BS}=0
}
$$

### BI1：Support-Only Bot Dynamics

使用当前：

$$
Q_{BS}
$$

### BI2：Candidate Extended Bot Dynamics

只有 evidence 支持时才增加：

- burden；
- origin-specific effect；
- role-specific effect；
- personalization moderator。

Bot Dynamics 只有在：

- parameter recovery 可接受；
- held-out prospective prediction 有增量价值；
- calibration 不恶化；

时才晋级正式模型。

---

## 三十四、未来 Care Trial 的边界

User-Initiated Conversation 永远属于：

$$
\boxed{
BaseConversationalSystem
}
$$

Future Care Trial 只针对：

$$
\boxed{
BOT\text{-}Initiated
}
$$

eligible decision points。

随机化前要求：

$$
\boxed{
Eligible
\cap
Available
}
$$

然后比较：

$$
\boxed{
BaseConversationalSystem
}
$$

与：

$$
\boxed{
BaseConversationalSystem
+
ExplicitCare
}
$$

User-Initiated Conversation 不被人为禁止。

若其发生在 care proximal outcome window，则记录：

$$
\boxed{
CoIntervention
}
$$

必须保持：

$$
\boxed{
PredictionAssociation
\neq
CausalTreatmentEffect
}
$$

Bot Support Dynamics 用于预测，不自动构成 Care causal claim。

---

## 三十五、当前冻结边界

1. User-Initiated 与 Bot-Initiated 必须分开。
2. Availability / Refractory 只限制 Bot-Initiated interaction。
3. User-Initiated interaction 正常响应，不经过 proactive gate。
4. User-Initiated interaction 本身不等于 Stress Signal。
5. Care Opportunity 与 Information Opportunity 分离。
6. Care trigger 使用 no-future-care forecast。
7. Defer 表示未来重新判断，不是 queued send。
8. Session 表示 temporal interaction episode；Thread 表示 semantic continuity。
9. Session gap 不通过 stress MAE 调整。
10. Bot Response Unit 是 Support Dynamics 最小响应单位。
11. Semantic Acts 不独立产生多个 Support Pulses。
12. 普通知识问答和 task help 不自动进入 $Q_{BS}$。
13. Support Representation 只使用 $J^{sup},V,G,Rel$。
14. Personalization 不直接提高 Support Potential。
15. Support 必须经过 Seen / Exposure gate。
16. User-Origin 与 Bot-Origin v1 共用 $\beta_{BS}$ 与 $\tau_{BS}$。
17. Bot Support 只直接作用于 Acute State。
18. Predictable burden 优先通过 policy / quality control 规避。
19. Burden Channel 只作为 candidate extension。
20. Bot coding 必须 blind to downstream outcomes。
21. Future Care Trial 只随机化 eligible Bot-Initiated decision points。
22. User-Initiated Conversation 属于 Base Conversational System。
23. Bot predictive association 不等于 causal care effect。
24. Proactive receptivity 记为 $Recp^{pro}$；refractory eligibility 记为 $Z^{ref}$。
25. $\tau_{BS}$ 只表示 Bot Support effect decay；$\tau_B=1/\kappa_B$ 只表示 Background stress time constant。
26. Session gap 与 Support kernel 的机制结构已冻结，其数值仍属 Representation-Pending-Freeze。
