# MindFlow 10：Runtime、Research 与 Experiment Architecture
>
> 作用：承接 `MindFlow_08_Data_Knowledge_and_Provenance_Architecture.md` 与 `MindFlow_09_Personalization_and_Bot_Context_Architecture.md`，定义 Forecast、Policy Evaluation、Action、Bot Runtime、Measurement、Research Dataset、Study Protocol、Experiment Controller，以及 Online / Research / Synthetic 的共享边界。
>
> 本文件区分“研究语义上必须区分的概念”和“工程上必须独立实现的组件”。除明确要求外，不要求每个概念都映射为独立表、独立服务或独立进程。

---

## 一、总体目标与核心链路

MindFlow 需要同时服务在线 Bot、纵向压力建模、个性化预测、主动关怀、研究评估和 Synthetic 验证。真正需要避免的是：线上模型、研究模型和 Synthetic 模型逐渐分叉，或者把“发了消息”“用户看到了”“用户回复了”“压力下降了”错误地当成同一件事。

最终顶层链路定义为：

```text
Canonical Knowledge
        ↓
ContextSnapshot
        ↓
ForecastRun
        ↓
DecisionPoint / PolicyEvaluation
        ↓
optional ExperimentAssignment
        ↓
ActionDecision
        ↓
BotResponseUnit
        ↓
Observable Action Events
        ↓
Independent Domain Outcomes
        ↓
Research Dataset Builder
```

本层必须始终保持以下区分：

```text
State ≠ Forecast
Policy Evaluation ≠ Final Action
Assignment ≠ Execution ≠ Delivery ≠ Exposure ≠ Effect
Reactive Support ≠ Proactive Treatment
Q_BS ≠ Experiment Treatment Indicator
Prediction ≠ Causal Estimation
```

---

## 二、Forecast 架构

### 1. Forecast 与 Current State 分离

`StateEstimate` 表示当前模型状态，例如：

$$
A(t),\ B(t),\ C_D(t),\ C_U(t),\ F(t),\ S(t)
$$

`ForecastRun` 表示在某一 forecast origin、某一合法 information set 下，对未来状态和观测作出的不可变预测。

```text
StateEstimate
↓
Forecast Engine
↓
ForecastRun
```

历史 Forecast 一旦产生，不因后续 correction 被覆盖。

### 2. ForecastRun 最小语义

建议至少保存：

```text
forecast_id
participant_id

forecast_origin
knowledge_cutoff
context_snapshot_id

forecast_kind

model_spec_version
representation_version
parameter_version
runtime_release_id

forecast_horizon / time_grid

latent_prediction
ema_predictive_prediction
uncertainty

future_assumption_ref / scenario_ref
random_seed if needed

created_at
```

轨迹正文可以外置保存，主记录只保留引用。

### 3. Latent Forecast 与 EMA Predictive Forecast 分离

模型中：

$$
y(t)=S(t)+\epsilon
$$

因此 latent stress prediction 与 future EMA predictive distribution 不是同一对象。必须区分：

$$
P(S_{future}\ge \theta)
$$

与：

$$
P(y_{future}\ge \theta)
$$

EMA 是 noisy measurement，不能把预测误差全部解释成 latent model error。

### 4. Forecast Kind

至少支持：

```text
NATURAL_COURSE / NO_FUTURE_MODELED_CARE
POST_ACTION_SCENARIO
```

`NATURAL_COURSE` 表示从 forecast origin 开始，不主动注入新的 modeled supportive exposure 时的未来轨迹。它是 proactive Policy 的 baseline forecast。

它不表示未来完全没有 Bot，也不禁止用户主动找 Bot，更不意味着普通 task assistance 必须消失。

`POST_ACTION_SCENARIO` 表示在明确假设某个 future action / exposure 条件下的 scenario forecast，可用于情景分析、研究模拟或未来 intervention planning，但不能替代 Natural Course baseline。

### 5. Future Assumption 必须可追溯

未来 Course、Task、Recovery 等并非完全确定。Forecast 必须保存 `future_assumption_ref` 或等价场景描述，例如：

```text
scheduled courses treated as planned
future task progress represented by scenario/distribution
future recovery represented by scenario/distribution
future proactive modeled care = none
```

这样才能解释同一 forecast origin 下不同预测结果的来源。

### 6. Forecast 与 Policy 解耦

禁止：

```text
Forecast
→ 猜未来 Policy 会 Care
→ 先把 Care 效果加入预测
→ 风险下降
→ Policy 因此不 Care
```

正确顺序：

```text
Natural Course Forecast
↓
Policy Evaluation
```

Policy 如需比较 action scenario，可额外请求 Post-action Scenario，但 baseline forecast 必须独立于自身未来 action。

### 7. Forecast superseded 不等于历史失效

新 Evidence 到来后：

```text
KnowledgeChanged
↓
old forecast becomes stale-for-current-runtime
↓
new ForecastRun
```

例如：

```text
14:00 F1
15:00 task cancelled
15:01 F2
```

当前运行使用 F2，但 F1 必须保留，因为 F1 是 14:00 时系统真实产生的 prospective prediction。

禁止删除或用 retrospective correction 覆盖历史 Forecast。

---

## 三、Decision Point、PolicyEvaluation 与 ActionDecision

### 1. 正式决策链

经过交叉审查，最终采用：

```text
Decision Point
↓
PolicyEvaluation
↓
Admissible Action Set
↓
optional ExperimentAssignment
↓
ActionDecision
↓
Final Execution Guard
↓
Execution
```

`PolicyEvaluation` 不再等同于最终 Decision。

### 2. PolicyEvaluation 的职责

PolicyEvaluation 回答：

```text
CARE 是否允许？
SENSE 是否允许？
NONE 是否允许？
是否处于 quiet hours？
是否处于 protected measurement window？
interaction burden 是否过高？
用户 preference 是否允许？
```

输出：

```text
admissible_actions
blocked_reasons
optional policy_preferred_action
```

### 3. 与 ContextSnapshot 的关系

08 已经定义 immutable ContextSnapshot，因此 10 不再建立独立 canonical `DecisionContext` 存储。

```text
PolicyEvaluation
└── context_snapshot_id → POLICY Snapshot
```

代码层可以存在 typed `DecisionContext` DTO，但不重复形成新的事实层。

### 4. V1 可合并存储

V1 不需要单独建立 DecisionOpportunity、DecisionContext、PolicyDecision 三张表。可以合并成一条 `PolicyEvaluation / DecisionPoint` 记录：

```text
decision_point_id
participant_id

decision_type
trigger_type
trigger_ref
opportunity_rule_version

policy_context_snapshot_id

admissible_actions
blocked_reasons
policy_preferred_action

selected_action
selection_source

policy_version
experiment_assignment_id optional

evaluated_at
```

### 5. Decision Point 不是每个 5 分钟网格

模型可以使用 5 分钟 time grid，但不意味着每个网格都是 Policy Decision Point。

只有 Opportunity Generator 真正产生机会时才记录，例如：

```text
forecast enters care opportunity
scheduled experimental decision occasion
material knowledge change creates new opportunity
```

未来如果 MRT 明确规定每天固定若干 randomization occasions，再按 protocol 精确记录。

### 6. Opportunity Generation 必须 versioned

必须能够回答：

> 为什么 15:00 是 decision point，而 14:55 不是？

因此保存：

```text
trigger_type
opportunity_rule_version
```

Decision population 本身就是研究 estimand 的一部分。

### 7. ActionDecision

最终可选：

```text
NONE
DEFER
SENSE
CARE
```

并记录：

```text
selection_source =
    POLICY
    EXPERIMENT
    SAFETY_OVERRIDE
    USER_EXPLICIT
```

`DEFER` 表示未来重新评估，不是保存待发送旧消息。

### 8. NONE 也是正式结果

真正形成 Decision Point 后，CARE / NONE / DEFER / SENSE 都必须记录，否则失去 denominator。

但普通 Reactive Chat 不需要为了形式统一强行制造 `PolicyEvaluation=RESPOND`。

### 9. ActionDecision 与 Execution 分离

例如：

```text
selected_action = CARE
```

但发送前出现新约束，或者 API 失败，则：

```text
selected_action = CARE
executed_action = NONE / FAILED
nonexecution_reason = ...
```

不能改写原 ActionDecision。

### 10. Final Execution Guard

在真正执行前进行最后一次轻量检查：

```text
用户是否刚 opt-out？
是否进入 protected measurement window？
用户是否刚主动进入会话？
interaction burden 是否刚变化？
```

若不再允许执行，只阻止 Execution，不修改历史 ActionDecision 或 ExperimentAssignment。

---

## 四、BotResponseUnit 与 Observable Action Events

### 1. BotResponseUnit 是核心 Runtime 单位

`BotResponseUnit` 表示一次语义完整的 Agent 响应。

例如：

```text
text + card + image
```

即使平台层产生多个 message，也可以属于一个 `response_unit_id`。

同一次发送 retry 也仍属于同一个 ResponseUnit。

### 2. V1 不单独建立 ContentArtifact

当前无需把 `ContentArtifact` 再拆成独立持久化层。BotResponseUnit 可以保存：

```text
response_unit_id
origin
semantic_role

text/content refs
card refs
image refs

generation metadata
agent_version
prompt_version

policy_evaluation_id optional
action_decision_ref optional

session_id
thread_id

created_at
```

只有未来真实出现跨渠道复用、A/B content rendering 等需求时再拆分。

### 3. Origin

至少区分：

```text
USER_INITIATED
PROACTIVE_POLICY
POLICY_FOLLOWUP
SYSTEM_TRANSACTIONAL
```

Origin 是研究上区分 reactive support 与 proactive intervention 的关键字段。

### 4. Policy Action 与 Content Role 分开

例如 Policy Action 可以是 `SENSE`，但 Agent 生成的内容本身可能带有 supportive feature。

因此 Content Role 单独表示，例如：

```text
ORDINARY_INFORMATION
TASK_ASSISTANCE
SENSING
SUPPORTIVE_CARE
SAFETY_SUPPORT
```

不能用 Policy Action 自动推导 support exposure。

### 5. Reactive Supportive Reply

用户主动倾诉后，Bot 可能产生：

```text
origin = USER_INITIATED
semantic_role = SUPPORTIVE_CARE
```

它可以进入 Support / Exposure Research View，但它不是 proactive randomized treatment。

因此始终保持：

$$
Q_{BS} \neq Experiment\ Treatment\ Indicator
$$

### 6. ResponseUnit 与 SupportPulse

如果未来 BI1 被 promotion：

```text
1 BotResponseUnit → max 1 SupportPulse
```

不能按句子数、平台 message 数或卡片组件数计算 pulse。

用户回复后的新 BotResponseUnit 可以形成新的 pulse。

### 7. BotActionEvent

Delivery / Exposure / Engagement 语义上分离，但工程上 V1 不需要三个服务。

推荐统一记录：

```text
SEND_REQUESTED
SEND_SUCCEEDED
SEND_FAILED
DELIVERED if available
SEEN if available
CARD_CLICKED
FORM_SUBMITTED
REPLY_LINKED
```

平台没有提供的观测保持 `UNKNOWN`。

### 8. Delivery ≠ Exposure ≠ Engagement ≠ Effect

`SEND_SUCCEEDED` 只能说明 transport action 成功。

它不能证明：

```text
user saw it
user understood it
support worked
```

同样，reply / click 也不能自动等于 positive treatment effect。

### 9. 当前阶段 Exposure 先 Research-side 派生

Runtime 只保存真实可观测事件：

```text
sent_at
delivered_at if available
seen_at if available
reply_at
click events
```

当前不建设复杂在线 ExposureResolver 或 probabilistic exposure model。

Pilot / Research 阶段可通过 versioned rule 派生 exposure status；只有 BI1 真正进入 Online Modeling 后再考虑 runtime resolver。

---

## 五、SupportRepresentation 与 Bot Support Dynamics

### 1. BI1 当前仍是 Candidate

Bot Support Dynamics $Q_{BS}$ 当前不是必须进入 production core 的已确认主通道。

因此现在不实现：

```text
每个 BotResponseUnit
↓
在线 LLM Support Coder
↓
Q_BS
↓
在线模型更新
```

### 2. 当前必须保存的内容

Runtime 需要确保未来能够重建：

```text
BotResponseUnit
origin
semantic role
content
sent / delivery timestamps
engagement refs
```

### 3. SupportRepresentation 先 Offline

研究侧：

```text
BotResponseUnit
↓
AI Support Coder / Human Coder
↓
SupportRepresentation
```

AI / Human 应输出同一 versioned representation contract。

### 4. Coding 必须 Blind to Future Outcome

Support coder 不允许看到：

```text
future EMA
future task completion
future helpfulness feedback
future stress trajectory
```

建议通过专门 `Blind Coding View` 执行，而不是依赖人工自觉。

### 5. SENSE 也可能产生 Interaction Exposure

主动询问压力或 appraisal 可能改变用户自我关注、EMA 回答和 interaction burden。

因此研究数据应能标记：

```text
active_sensing_in_window = true
```

但不因此把所有普通聊天都纳入 causal treatment model。

### 6. Task Assistance、Support、Recovery 分机制处理

可能存在：

```text
Task Assistance
→ Task Progress
→ C_U
```

以及：

```text
Supportive Interaction
→ Q_BS
→ A
```

以及：

```text
Actual Recovery
→ Q_R
→ A
```

Bot 建议用户休息不等于 Actual Recovery。只有真正发生的 Recovery Evidence 才能进入 $Q_R$。

### 7. Runtime 不保存 Effect 标签

禁止：

```text
intervention.effect = POSITIVE
```

Runtime 可以保存 user feedback、EMA、Task、Recovery 等独立 observation，但 effect estimate 属于 Research Analysis。

---

## 六、Outcome Linking 与 Research Dataset

### 1. Outcome 保持在原 Domain

Care 后发生的：

```text
EMA
Task Progress
Recovery
Conversation Feedback
```

继续分别属于 Measurement / Task / Recovery / Conversation Domain。

Research 层通过 participant、time 和 artifact refs 建立关联。

禁止在 Intervention 记录中复制：

```text
outcome_ema
outcome_task
outcome_recovery
```

### 2. Research Dataset 是 Derived View

```text
Forecast
PolicyEvaluation
ActionDecision
BotResponseUnit
BotActionEvent
Measurement
Task
Recovery
Conversation
Study artifacts
        ↓
DatasetBuilder
```

产生：

```text
Prediction Evaluation View
Intervention Evaluation View
Process / Mechanism View
Blind Coding View
```

Research Dataset 不是新的 source of truth。

### 3. DatasetSpec / AnalysisSpec

DatasetSpec 属于 Research-side versioned config，例如：

```text
research/specs/
    prediction_eval_v1.yaml
    care_observational_v1.yaml
```

至少定义：

```text
population rule
decision-point rule
baseline window
treatment definition
exposure definition
outcome definition
outcome window
missingness rule
co-intervention rule
knowledge-time rule
exclusion rule
model / experiment compatibility
```

### 4. Prediction 与 Intervention Dataset 分离

Prediction Evaluation 回答：

> 在 $t_0$ 当时合法信息下，对未来压力预测得准不准？

核心：

```text
ForecastRun + Future Measurement
```

Intervention Evaluation 回答：

> 某 Decision Point 下的 assignment / action / exposure 与未来 outcome 有何关系？

核心：

```text
Decision Point + Assignment/Action/Exposure + Future Outcomes
```

二者 row unit 不同。

---

## 七、Prediction Evaluation

### 1. 基本单位

推荐：

```text
Forecast × Future Measurement
```

例如：

```text
Forecast F100 @ 14:00
Measurement M201 @ 16:00
forecast_horizon = 2h
```

### 2. 必须使用 Pre-assimilation Prediction

不能：

```text
16:00 EMA 已进入模型
↓
更新 S
↓
拿更新后的 S 与同一 EMA 比
```

这不是 prospective prediction evaluation。

### 3. Forecast Horizon 是正式维度

1h、4h、8h 预测难度不同。Dataset 必须保存 horizon，避免混成一个总体 MAE。

### 4. Latent 与 Observed Risk 分开

可以评估：

```text
predicted EMA vs observed EMA
```

但 latent threshold 没有直接 Ground Truth，不能与 observed threshold 混为一谈。

### 5. Future Intervention / Sensing 必须可识别

例如：

```text
14:00 Natural Course Forecast
15:00 Care
16:00 EMA
```

此时 observed outcome 已处于 post-intervention world。

Dataset 必须能标记：

```text
intervention_between_origin_and_target
active_sensing_between_origin_and_target
```

如何 exclude / stratify / flag 由 DatasetSpec 决定。

### 6. Future New Information 不自动使 Forecast 无效

如果 forecast 后发生考试取消、任务变化等新信息，预测误差可以反映真实 future uncertainty。

因此应：

```text
flag / characterize
```

而不是自动删除。

只有数据损坏或 protocol-defined invalidity 才直接排除。

---

## 八、Care / Intervention Evaluation

### 1. 基本单位优先采用 Decision Point

不能只收集 CARE rows，否则没有合理 denominator。

至少需要知道：

```text
什么时候存在 opportunity
为什么形成 opportunity
CARE / NONE / SENSE / DEFER
```

### 2. Assignment、Execution、Delivery、Exposure 分开

未来 randomization 后，至少能生成：

```text
assigned action
executed action
delivery status
derived exposure status
```

不能统一成单一 `treatment=1`。

### 3. No-Care Forecast 的研究定位

Natural Course Forecast 是：

```text
model-based reference trajectory
```

它可用于 Policy、descriptive comparison 和 exploratory intervention analysis。

但：

```text
Observed - NoCareForecast
```

不能自动称为 causal treatment effect。

### 4. Outcome / Mediator / Covariate 由 AnalysisSpec 决定

同一个 Task Progress、Recovery 或 Reply 在不同研究问题中可能是：

```text
Outcome
Mediator
Process Measure
Covariate
```

角色不能写死在 canonical data。

### 5. Pre-treatment Boundary

对于 Decision Time $t_0$，baseline covariate 必须来自：

```text
known_at <= t0
```

且语义上属于 pre-treatment 信息。

Treatment 后发生的 Recovery / Task Completion / Reply 不能自动进入 baseline adjustment。

### 6. Co-intervention

Outcome window 内可能出现：

```text
second care
reactive supportive reply
active sensing
task assistance
recovery
```

必须能够识别。

如何处理由 AnalysisSpec 决定：

```text
exclude
censor
flag
model jointly
```

### 7. Reactive Support 是 Background Co-intervention

如果未来研究 proactive CARE vs no proactive CARE，control participant 仍可能主动找 Bot 并得到正常支持。

应记录：

```text
proactive assignment = NONE
reactive support exposure = YES
```

这不自动等于 protocol error。

### 8. Care Evaluation 分层

建议：

```text
E0 Process / Feasibility
E1 Observational Association
E2 Randomized Causal Evaluation
```

E0 研究 send success、engagement、burden、annoyance 等。

E1 研究 care exposure 与未来 stress / behavior 的 association，不声称 causal proof。

E2 只有在完整 randomized design 下再讨论 causal estimand。

---

## 九、Measurement 与 Missingness

### 1. Missing Outcome 保持 Missing

没有 EMA：

```text
outcome_status = MISSING
```

禁止填 0，也不能推断 Care 成功或失败。

### 2. MeasurementRequest

V1 强烈建议保存：

```text
measurement_request_id
participant_id
scheduled_at
requested_at
send_status
delivered_at if available
responded_at
measurement_id optional
protocol_ref
```

这样才能区分：

```text
根本没有安排 measurement
安排了但没发
发了但没答
技术失败
protocol suppressed
```

不需要建设复杂 MeasurementOpportunity Engine。

### 3. Missingness 可能 Informative

压力高时用户可能更不愿填写 EMA，因此研究不能只看到“存在的 EMA rows”。MeasurementRequest 是 missingness audit 的基础。

### 4. Protected Natural Measurement 与 Post-intervention Outcome Measurement

Protected Natural Measurement 用于：

```text
model calibration
natural-state observation
```

其前面可以限制 proactive intervention。

Post-intervention Outcome Measurement 则明确用于观察 treatment 后状态，本来就应发生在 intervention 之后。

二者不能混为“干预后都不能测 EMA”。

---

## 十、Research Export 与 Dataset Reproducibility

### 1. Research Export Boundary

正式 Dataset 默认仅输出最小必要字段：

```text
study_subject_key
structured variables
relative/study timestamps
model outputs
decision/exposure variables
measurement values
```

不默认导出：

```text
full raw conversation
Memory Markdown
raw documents
runtime identity
```

Raw Text 只在 Support Coding、Annotation、Qualitative Analysis 等明确任务中单独导出。

### 2. Pseudonymous Study Identity

Research Dataset 推荐使用：

```text
study_subject_key
```

而不是 Bot Runtime participant identity。映射单独受控。

### 3. Blind Coding View

用于 Support Coding 的 Export 只提供：

```text
BotResponseUnit content
necessary preceding context
allowed metadata
```

排除：

```text
future EMA
future helpfulness feedback
future stress trajectory
experiment assignment if unnecessary
```

### 4. DatasetManifest

物化 CSV / Parquet / DB table 没有问题，但必须有：

```text
dataset_id
dataset_spec_version
builder_version
runtime/study release
source manifest/hash
created_at
code_revision
```

Frozen Dataset 不因后续 correction 自动变化。如需更新，生成新 dataset version。

### 5. Exclusion Rule

DatasetSpec 必须明确 exclusion rule，例如：

```text
technical corruption
invalid participant
measurement corruption
protocol-defined invalidity
synthetic contamination
```

禁止因为：

```text
预测误差太大
```

而删除样本。

---

## 十一、Study Runtime

### 1. Participant 与 StudyEnrollment 分离

```text
Participant
├── Product Runtime State
└── StudyEnrollment
```

退出 Study 不等于删除用户或禁用普通 Bot。

### 2. StudyEnrollment

正式 Pilot / Study 时建议：

```text
enrollment_id
participant_id
study_id
protocol_version/hash
status
current_phase
enrolled_at
actual_start_at
consent/authorization_ref
consent_version
consented_at
created_at
```

Consent / authorization 的具体要求由实际研究审批决定，架构只保留挂点。

### 3. Enrollment Status 与 Study Phase 分离

Status：

```text
ACTIVE
PAUSED
COMPLETED
WITHDRAWN
TERMINATED
```

Phase：

```text
BASELINE
INTERVENTION
FOLLOW_UP
...
```

二者是不同维度。

### 4. StudyProtocol

当前规模不建设通用实验平台。

建议使用：

```text
study_protocol/formal_v1.yaml
```

或 versioned Python/config schema，包含：

```text
protocol_id
protocol_version
phase config
measurement config
decision opportunity config
model spec ref
representation version
policy version
experiment config optional
effective_at
hash
```

已经用于正式 participant 的 protocol 不原地修改。

### 5. Freeze Rules, Not Personalized State

Formal Study 应冻结：

```text
ModelSpec
Representation Rule
Parameter Estimation Algorithm
Promotion Rule
Policy Rule
Measurement Rule
Opportunity Rule
```

但 participant 仍可：

```text
P0 → P1 → P2
parameter posterior update
```

正式原则：

$$
Freeze\ Algorithm \neq Freeze\ Personalized\ State
$$

### 6. StudyEvent

V1 不建立独立 PhaseTransition、Withdrawal、Override、ProtocolDeviation 子系统。

统一使用 append-only `StudyEvent`：

```text
ENROLLED
PHASE_CHANGED
PAUSED
RESUMED
WITHDRAWN
OVERRIDE_APPLIED
PROTOCOL_DEVIATION
PROTOCOL_AMENDMENT_APPLIED
```

`StudyEnrollment.status/current_phase` 是 current projection。

### 7. Withdrawal Scope

至少区分：

```text
PROACTIVE_INTERVENTION
MEASUREMENT
STUDY_PARTICIPATION
DATA_USE
```

例如“以后别主动给我发关怀”应立即阻止 proactive care，但不自动关闭普通 Bot 或删除全部历史数据。

### 8. User Constraint 高于 Experiment

原则：

```text
Safety / User Explicit Constraint
>
Experiment
```

实验不能突破用户明确 opt-out、quiet hours 等正常系统约束。

### 9. Phase 控制 Capability

不要业务代码到处：

```python
if phase == "baseline":
```

Study Protocol 应解析为：

```text
measurement_enabled
proactive_policy_enabled
randomization_enabled
followup_measurement_enabled
```

### 10. Reactive Bot 不应被 Control Condition 粗暴关闭

实验主要约束 proactive intervention / experiment-targeted sensing / measurement。

普通：

```text
conversation
task assistance
web search
document reading
reactive support
```

默认仍保持正常，除非它本身就是 experimental factor。

### 11. ProtocolDeviation

作为：

```text
StudyEvent(type=PROTOCOL_DEVIATION)
```

记录 reason、related refs、occurred_at、severity/relevance。

是否 exclude / flag / retain 由 DatasetSpec 决定。

### 12. Pilot 与 Formal Study 分离

Pilot 与 Formal Study 推荐使用不同 study_id / protocol instance。

Pilot 可允许 representation、policy 和 workflow tuning；Formal Study 需要更多版本冻结。

---

## 十二、Experiment Controller

### 1. 条件模块，不是所有请求必经层

```text
PolicyEvaluation
↓
is_randomizable?
├── NO → ActionDecision(selection_source=POLICY)
└── YES
     ↓
   ExperimentController
     ↓
   ActionDecision(selection_source=EXPERIMENT)
```

Observational phase、Pilot 和普通 Bot usage 可以完全不进入 Experiment Controller。

### 2. ExperimentSpec

只有真正随机化后再实现：

```text
experiment_id
experiment_version
randomization_unit
eligible_actions
assignment_scheme
probability_rule
phase constraints
refractory constraints
```

架构可支持：

```text
FIXED_ASSIGNMENT
PARTICIPANT_RANDOMIZATION
DECISION_POINT_RANDOMIZATION
```

但当前不强制采用 MRT。

### 3. Randomization 在 Hard Eligibility 后

```text
Decision Point
↓
PolicyEvaluation
↓
Admissible Actions
↓
ExperimentController
```

如果 CARE 被用户 opt-out、measurement protection、quiet hours 等禁止，Experiment 不能随机出 CARE。

### 4. ExperimentAssignment

真正进入 randomization 后才需要：

```text
assignment_id
enrollment_id
decision_point_id
experiment_version
eligible_actions
assigned_action
assignment_probability
randomization_unit
randomization_metadata
assigned_at
```

Assignment 一旦产生不可修改。

### 5. Randomization 幂等

同一个 `decision_point_id` 必须最多只有一个正式 assignment。

Runtime retry 必须返回同一结果，不能第一次 CARE、第二次 NONE。

### 6. Assignment Probability 留存

若使用非 50/50 或 dynamic probability，需要保存该 decision point 当时实际使用的 probability。

当前 20–50+ participant 规模下优先 simple fixed / low-dimensional randomization，避免过度分层。

### 7. Experiment 不泄露给 Agent

Conversation Agent 不读取：

```text
control group
assignment probability
experiment arm
randomization metadata
```

Agent只获得最终 Action Intent 及表达约束。

### 8. Protocol Amendment

Formal Study 中发现严重 correctness bug 时：

```text
new model/policy release
+
protocol amendment event
+
effective_at
```

历史 Forecast / Decision / Assignment 不覆盖。

---

## 十三、Runtime 幂等、重试与恢复

### 1. 核心原则

```text
same logical event
→ same logical identity
```

process crash、scheduler retry、network timeout、duplicate callback 都不能制造重复 treatment、assignment 或 measurement request。

### 2. 重要对象需要稳定 identity / idempotency key

至少包括：

```text
Decision Point
ActionDecision
ExperimentAssignment
BotResponseUnit
MeasurementRequest
```

### 3. Delivery Retry

```text
第一次 send timeout
第二次 send success
```

应该是：

```text
1 BotResponseUnit
multiple BotActionEvent / attempts
```

而不是新的 treatment、SupportPulse 或 ExperimentAssignment。

### 4. 当前规模的工程策略

预计 20–50+ participant，优先：

```text
PostgreSQL transactions
unique constraints
persistent jobs / outbox where needed
```

目标是 correctness、auditability 和 reproducibility，不是高并发分布式平台。

---

## 十四、Scientific Core 共享边界

### 1. 共享的不是整个 Runtime

Production、Research、Synthetic 真正必须共享的是：

```text
Domain semantics
Knowledge resolution semantics
ModelContextBuilder
Representation
Dynamics
Assimilation
Forecast
Parameter estimation algorithm
Policy evaluation rules
```

而不是 Feishu、Agent、Web、Dataset、Synthetic Generator 等整套 Runtime。

### 2. ModelContextBuilder

这是最关键的共享边界：

```text
Resolved Knowledge
↓
ModelContextBuilder
↓
ModelContext
↓
Pure Modeling Core
```

Online、Historical Replay、Pipeline-level Synthetic 都必须走同一 ModelContextBuilder。

否则即使最后都调用同一个 ODE，输入预处理仍可能已经分叉。

### 3. ContextProvider(MODEL) 与 ModelContextBuilder 分工

ContextProvider / AsOfKnowledgeReader 回答：

> 截止某个 knowledge cutoff，系统合法知道哪些 model-relevant facts？

ModelContextBuilder 回答：

> 如何把这些 resolved facts 组织成 Modeling Contract？

Modeling Core 回答：

> 如何根据 ModelContext 计算 Representation、State、Assimilation 与 Forecast？

工程上可以是少量函数，不要求三个独立服务。

### 4. ModelContextBuilder 不计算数学状态

它可以输出：

```text
events
obligations
recovery episodes
appraisal beliefs
measurements
prior state
parameter ref
```

不能提前计算：

```text
Q_D
Q_P
Q_R
C_D
C_U
F
A
B
S
```

这些必须只有 Modeling Core 一份实现。

---

## 十五、Pure Modeling Core

### 1. 硬边界

Core 禁止依赖：

```text
PostgreSQL
SQLAlchemy
Feishu
LLM
Web
Memory
HTTP
.env
global cache
wall clock
```

Core 只接受：

```text
ModelContext
ModelState
ModelSpec
ParameterSet
Measurement
ForecastSpec
ExplicitRandomness
```

返回：

```text
StateEstimate
AssimilationResult
ForecastResult
CandidateParameterSet
```

### 2. 时间显式传入

Core 禁止使用 `datetime.now()` 决定模型行为。

所有 reference_time、forecast_origin、measurement_time、event_time 都显式输入。时区归一化在进入 Core 前完成。

### 3. 随机性显式

如果使用 Monte Carlo、future scenario sampling 或 uncertainty propagation，需要显式 random seed / RNG state。

目标：

```text
same input
+ same spec
+ same seed
= same result
```

### 4. ModelSpec 与 ParameterSet 分离

`ModelSpec` 表示：

```text
representation rules
kernel definitions
state equations
fixed hyperparameters
measurement model configuration
enabled mechanisms
```

`ParameterSet` 表示 population / participant 参数值。

Participant 参数变化不等于 ModelSpec 版本变化。

### 5. Parameter Estimation Algorithm 共享

Scientific Core 可逻辑分为：

```text
Modeling
├── Representation
├── Dynamics
├── Assimilation
└── Forecast

Estimation
└── Parameter Estimation Algorithms
```

什么时候运行 estimation、用哪些数据、是否 promotion、哪个 ParameterSet 激活，属于 Application / Promotion Rule。

### 6. Parameter Promotion 不属于 Core

```text
estimate()
→ CandidateParameterSet
↓
Validation
↓
PromotionRule
↓
ActiveParameterSet
```

Core 不自行决定 P0→P1→P2。

### 7. StableProfile Anti-double-counting

如果 StableProfile 已作为 Appraisal prior：

```text
StableProfile
↓
Appraisal Resolver
↓
Resolved Appraisal
↓
ModelContext
```

则不能再把 StableProfile 本身作为另一个模型 feature。

同一 personalization evidence 只能通过合法语义路径进入一次。

### 8. Bot / Memory 到 Model 的合法路径

```text
Conversation
↓
Evidence
↓
Knowledge
↓
Event / Appraisal / Recovery / Measurement
↓
ModelContext
```

禁止：

```text
Memory Markdown
→ Modeling Core
```

---

## 十六、ModelStateCheckpoint 与 Derived State

Online 可以保存：

```text
ModelStateCheckpoint
participant_id
state_time
state_vector
model_spec_version
parameter_version
input_revision
```

它只是 derived cache，不是 canonical psychological truth。

历史 correction 影响 $t^*$ 后：

```text
find last valid checkpoint before t*
↓
recompute forward
```

不能手工修改 A/B/S。

Forecast provenance 不能只依赖可变 checkpoint。ForecastRun 仍应绑定 ContextSnapshot、ModelSpecVersion、ParameterVersion、prior-state identity/minimal state 和必要 randomness。

---

## 十七、Pure Policy Evaluator

Policy logic 也尽量 pure：

```text
evaluate_policy(
    PolicyInput,
    PolicySpec
)
→ admissible_actions
  preferred_action
  reason_codes
```

它不能：

```text
send message
query DB
call LLM
randomize experiment
```

Online 与 Research Replay 可调用同一 Policy Evaluator。

Policy 只消费稳定 Forecast Contract + Policy Context，不直接读取 Core internal A/B/C_D/C_U/F 等内部表示。

---

## 十八、Agent 与 Scientific Core 解耦

Conversation Agent 可以调用：

```text
get_forecast_summary
```

但不能直接：

```text
modeling_core.propagate()
load ParameterSet
read internal state
```

正确依赖：

```text
Agent
↓
Application Tool
↓
Forecast Artifact / Forecast Service
```

这样模型内部表示变化不会直接破坏 Agent。

---

## 十九、Online Runtime、Research Runtime 与 Synthetic

### 1. Online Runtime

负责：

```text
Receive user / scheduler / tool events
Persist Source
Resolve Knowledge
Build Context
Call Pure Core
Persist derived artifacts
Call Policy
Run Agent / Tools
Persist observable effects
```

它负责 orchestration、transactions、idempotency、consistency barrier、external systems 和 persistence，而不是重新实现数学。

### 2. Research Runtime

主要负责：

```text
Historical As-of Replay
Dataset Construction
Evaluation / Statistics
Coding / Annotation
```

Research 不应该 import 整个 Online Bot Runtime。

应：

```text
AsOfKnowledgeReader
↓
same ModelContextBuilder
↓
same Scientific Core
```

Online Runtime 也禁止 import Research helper。

### 3. Prospective Replay

Prospective Replay 使用：

```text
known_at <= replay_time
```

以及当时 committed 的 Evidence / Knowledge / ContextSnapshot。

不能用今天的 LLM 重新抽取历史文本，再声称这是当时系统知道的内容。

### 4. Retrospective Reprocessing

若研究新 Extractor / Resolver：

```text
Raw Source
↓
new extractor/resolver
```

必须进入独立 retrospective analysis namespace，不能覆盖 historical system state。

### 5. Historical Agent Replay 与 Counterfactual Simulation

历史已有 BotResponseUnit 时，Replay 使用 recorded response。

用今天的新模型重新生成回复属于：

```text
Counterfactual Agent Simulation
```

不是 historical replay。

### 6. Policy Replay

可以用 historical Policy Snapshot + Policy v1 复现 recorded evaluation，也可以用 Policy v2 做 counterfactual analysis，但都不能覆盖历史 PolicyEvaluation。

---

## 二十、Synthetic Architecture

### 1. Core-level Synthetic

直接构造：

```text
ModelContext
ParameterSet
```

用于公式、状态恢复、参数可识别性与边界测试。

### 2. Pipeline-level Synthetic

生成：

```text
synthetic Event
Task
Recovery
EMA
Appraisal Evidence
known_at delay
conflict
correction
missingness
```

再经过：

```text
Resolver
ModelContextBuilder
Core
```

测试完整科学管线。

### 3. Ground Truth Sidecar

Synthetic 可拥有：

```text
true A
true B
true S
true parameters
true lifecycle
```

但必须作为 Evaluation Sidecar，不能进入 ModelContext。

### 4. In-model 与 Perturbed Synthetic

只用同一模型生成再恢复，只能证明实现正确或理想条件下 estimator 可工作。

还应允许 Perturbed Synthetic，引入：

```text
unmodeled event
kernel misspecification
measurement missingness
delayed knowledge
lifecycle uncertainty
```

用于 robustness。

### 5. Namespace Isolation

Synthetic participant 必须显式：

```text
origin = SYNTHETIC
```

并与真实 participant 严格隔离。Admin、DatasetBuilder 和 Research Export 均需防止 synthetic contamination。

---

## 二十一、Relevant Canonical State Barrier

09 的 Strong Consistency Barrier 在此进一步收缩为：

> 只等待与当前 operation 有关的 canonical state。

Forecast 请求需要等待：

```text
current relevant Evidence
relevant Domain Resolver
required current projection
```

不等待：

```text
Memory Markdown refresh
StableProfile background refresh
Research indexing
```

保证科学一致性，但不人为拉高响应时延。

普通 Reactive Conversation 的 Memory/Profile background failure 不应拖死回复；但依赖最新 canonical state 的 Forecast / Policy / Research Snapshot 不得 silent fallback 到 stale state。

---

## 二十二、Versioning 与 Reproducibility

### 1. 不建设 Dynamic Model Plugin Registry

配置 / 参数变化用：

```text
ModelSpec version
PolicySpec version
DatasetSpec version
```

算法代码结构变化用：

```text
git commit
release tag
runtime_release_id
container image digest if needed
```

需要严格历史复现时 checkout 对应 release。

### 2. Runtime Release Identity

关键研究 Artifact 应能追到：

```text
runtime_release_id
```

由 release 再映射到 git revision、container digest 等。

### 3. Reproducibility Chain

```text
Study Release
+
Canonical Artifact Store
+
DatasetSpec
+
DatasetBuilder Revision
↓
DatasetManifest
↓
Frozen Dataset
↓
Analysis Code Revision
↓
Result
```

无需建设大型 experiment-tracking 平台。

---

## 二十三、必须禁止的依赖

正式禁止：

```text
research/ 复制一份 stress_model.py
synthetic/ 无意写一套漂移的 production model
online runtime → import research helper
modeling core → DB / LLM / Feishu / Web / Memory
Agent → Core internal state / ParameterSet
Memory Markdown → Modeling Core
StableProfile → ParameterEstimator directly
ModelParameters → ProfileUpdater directly
```

---

## 二十四、推荐逻辑依赖图

```text
               ┌────────────────────────────┐
               │ Domain Contracts / Types   │
               └─────────────┬──────────────┘
                             │
               ┌─────────────▼──────────────┐
               │ Knowledge Resolution        │
               └─────────────┬──────────────┘
                             │
               ┌─────────────▼──────────────┐
               │ ModelContextBuilder         │
               └─────────────┬──────────────┘
                             │
               ┌─────────────▼──────────────┐
               │ Pure Scientific Core        │
               │ Representation              │
               │ Dynamics                    │
               │ Assimilation                │
               │ Forecast                    │
               │ Estimation                  │
               └─────────────┬──────────────┘
                             │
        ┌────────────────────┼────────────────────┐
        │                    │                    │
        ▼                    ▼                    ▼
 Online Application   Research Application   Synthetic Tests
        │                    │                    │
        ▼                    ▼                    ▼
 Infrastructure       Dataset / Metrics      Synthetic Generator
 Feishu / DB / LLM    Coding / Replay        Ground Truth Sidecar
 Web / Files
```

平行共享：

```text
Pure Policy Evaluator
```

供 Online 与 Research Replay 共用。

---

## 二十五、Online / Research / Synthetic 共享边界

| 能力 | Online | Research | Synthetic | 是否同实现 |
|---|---:|---:|---:|---|
| Domain semantics | ✓ | ✓ | ✓ | 是 |
| Knowledge resolver semantics | ✓ | ✓ | ✓ | 是 |
| ModelContextBuilder | ✓ | ✓ | ✓ | 是 |
| Representation | ✓ | ✓ | ✓ | 是 |
| Dynamics | ✓ | ✓ | ✓ | 是 |
| Assimilation | ✓ | ✓ | ✓ | 是 |
| Forecast | ✓ | ✓ | ✓ | 是 |
| Parameter estimation algorithm | 可用 | ✓ | ✓ | 是 |
| Promotion rule | ✓ | 分析/模拟 | 可测 | 同规则 |
| Policy evaluator | ✓ | replay | 可测 | 是 |
| PostgreSQL / Feishu | ✓ | read adapter only | 否 | 否 |
| Conversation Agent | ✓ | recorded/counterfactual only | optional | 非 Scientific Core |
| DatasetBuilder | 否 | ✓ | ✓ | Research-side |
| Metrics / Statistics | 否 | ✓ | ✓ | Research-side |
| Synthetic Ground Truth | 否 | 否 | ✓ | Synthetic-only |

---

## 二十六、分阶段实现要求

### 1. 当前重构必须保留/建立的边界

```text
ForecastRun
PolicyEvaluation / DecisionPoint
ActionDecision
BotResponseUnit
BotActionEvent
MeasurementRequest
ContextSnapshot refs

ModelContextBuilder
Pure Modeling Core boundary
Pure Policy Evaluator boundary

ModelSpec / ParameterSet separation
runtime release/version identity
idempotency / retry semantics
Relevant Canonical State Barrier
```

并复用已有：

```text
Evidence
Knowledge
Event
Task
Recovery
EMA
```

### 2. Pilot 前实现

```text
StudyEnrollment
StudyEvent
basic StudyProtocol versioned config
Research Export Boundary
study pseudonymous identity
DatasetSpec
DatasetManifest
Prediction Evaluation DatasetBuilder
observational CARE/NONE decision logging
co-intervention flags
Blind Coding View
offline Support Coding
```

### 3. 只有决定 Randomized Study 后实现

```text
ExperimentController
ExperimentAssignment
assignment probability
randomization idempotency
Final Guard experiment integration
protocol amendment for randomization
```

### 4. 只有 BI1 Promotion 后实现

```text
Online Support Coder
Online ExposureResolver
Online Q_BS reconstruction
Bot Support state update in production model
```

---

## 二十七、当前明确不建设的内容

```text
通用临床试验平台
复杂动态 Experiment DSL
大型 Protocol 管理后台
Kafka 级事件系统
独立 Exposure 微服务
独立 Engagement 微服务
独立 ContentArtifact 服务
在线 Support Coding 服务
动态历史 Model Plugin Registry
Research 与 Production 两套 Stress Model
Synthetic 独立复制 Stress Model
```

---

## 二十八、与 08 / 09 的边界

### 08：Data / Knowledge / Provenance

负责：

```text
Source
Evidence
known_at
Resolver
Correction
ContextSnapshot
Natural Language Extraction
```

10 不重新定义 Evidence 语义。

### 09：Personalization / Bot Context

负责：

```text
ContextProvider
MemoryRetriever
StableProfile
SelfDeclaredTendency
UserPreference
Agent Tool Boundary
Reactive / Proactive Agent orchestration
```

10 不重新定义 Memory / Personalization。

### 10：Runtime / Research / Experiment

负责：

```text
Forecast Artifact
Policy Evaluation
Action Decision
Bot Response / Observable Action Events
Measurement Request
Study Runtime
Experiment Assignment
Research Dataset
Online / Research / Synthetic Scientific-Core boundary
```

---

## 二十九、最终架构决策

1. Production、Research、Synthetic 共享科学语义和纯算法，不共享整个 Runtime。
2. Forecast 是不可变历史 Artifact，不等于 Current State。
3. Natural Course / NoFutureModeledCare Forecast 是 proactive Policy baseline，但不是 causal truth。
4. Latent Forecast 与 EMA Predictive Forecast 分离。
5. Forecast 必须绑定 ContextSnapshot、版本、future assumptions 和必要 randomness。
6. New Knowledge 只让旧 Forecast 对当前运行 stale，不删除历史 Forecast。
7. PolicyEvaluation 与最终 ActionDecision 分离。
8. PolicyEvaluation 输出 admissible action set；optional Experiment 只能在其中选择。
9. Decision Point 只在真实 opportunity 产生，不把所有 5 分钟网格都当 decision。
10. NONE / DEFER / SENSE / CARE 都是正式 Decision Point 结果。
11. DEFER 表示未来重新评估，不保存待发送旧消息。
12. ActionDecision、Execution、Delivery、Exposure、Engagement、Effect 必须语义分离。
13. BotResponseUnit 是 Agent 语义响应单位，不等于 platform message。
14. Delivery retry 不生成新的 treatment、ResponseUnit 或 randomization。
15. Policy Action、Content Role、Origin 分离。
16. Reactive supportive interaction 与 proactive experimental treatment 分离。
17. $Q_{BS}$ 不等于 Experiment Treatment Indicator。
18. Runtime 当前保存客观 Delivery / Engagement 事件，不提前建设复杂 Exposure Service。
19. SupportRepresentation 先 Research-side offline coding；BI1 promotion 前不强制进入 Online Core。
20. SENSE 本身可能产生 measurement reactivity，应可在研究数据中识别。
21. Task Assistance、Support Exposure、Actual Recovery 分机制编码。
22. Runtime 不保存简单 positive/negative effect 标签。
23. Outcome 保留在原 Domain；Research 通过关联构建分析视图。
24. Prediction Evaluation 与 Care Effect Evaluation 使用不同 Dataset Construction。
25. Prediction 基本单位是 Forecast × Future Measurement，并使用 pre-assimilation prediction。
26. Future intervention / sensing / new information 需要被识别，不自动把预测样本删除。
27. Care research 基本单位优先采用 Decision Point，而不是只保留 CARE rows。
28. Assignment、Execution、Delivery、Exposure 分开。
29. Outcome / Mediator / Covariate 角色由 AnalysisSpec 定义，不写死在 canonical data。
30. Missing Outcome 保持 Missing，并记录 MeasurementRequest 以支持 missingness analysis。
31. Protected Natural Measurement 与 Post-intervention Outcome Measurement 分离。
32. Co-intervention 包括 reactive support、active sensing、second care 等，应可被识别。
33. Care effectiveness 研究分 Process / Observational Association / Randomized Causal Evaluation 三层。
34. DatasetSpec / DatasetManifest 属于 Research-side，不建设在线 Dataset Service。
35. Research Export 默认数据最小化，并使用 pseudonymous study identity。
36. Human / AI Coding 使用 Blind Coding View，禁止 future outcome leakage。
37. StudyEnrollment 与 Participant 分离。
38. Enrollment Status 与 Study Phase 分离。
39. StudyProtocol 使用简单 versioned config，不建设通用实验平台。
40. 正式 Study 冻结 algorithm / rule definition，而不是冻结 participant personalized state。
41. Study phase / withdrawal / override / deviation 统一通过 StudyEvent 历史表达。
42. User opt-out / hard constraint 高于 Experiment。
43. 普通 Reactive Bot 不因 control condition 被粗暴关闭。
44. Experiment Controller 是条件模块，不是所有请求必经层。
45. Randomization 发生在 Hard Eligibility 之后。
46. ExperimentAssignment 一旦产生不可修改，并且必须幂等。
47. Agent 不决定 Randomization，也不读取隐藏 assignment / probability。
48. Formal bug fix 通过 versioned Amendment / release，不覆盖历史 Artifact。
49. Runtime 必须保证重要 action 的 idempotency，防止 retry 污染研究 exposure。
50. `ModelContextBuilder` 必须在 Online / Research / Synthetic 共用。
51. Pure Modeling Core 禁止 DB、LLM、Network、Clock、Memory 等隐藏依赖。
52. 时间和随机性显式传入。
53. ModelSpec 与 ParameterSet 分离。
54. Parameter Estimation Algorithm 可共享，Parameter Promotion 属于外部 versioned rule。
55. StableProfile 作为 Appraisal prior 时必须防止 double counting。
56. ModelStateCheckpoint 是 derived cache，不是 canonical truth。
57. Research 不 import Online Runtime；Online 不 import Research。
58. Prospective Replay 不重新运行今天的 LLM 去改写历史 Evidence。
59. Historical Agent Response 使用 recorded ResponseUnit；重新生成属于 Counterfactual Simulation。
60. Policy Evaluator 应尽量 pure，供 Online 与 Replay 共用。
61. Synthetic Ground Truth 是独立 sidecar，不进入模型输入。
62. Synthetic 同时支持 in-model correctness test 与 perturbed robustness test。
63. Synthetic namespace 与真实 participant 严格隔离。
64. Relevant Canonical State Barrier 只等待与当前 operation 有关的 canonical state。
65. 当前项目优先 PostgreSQL + transactional correctness + auditability，不建设高并发分布式实验平台。

---

## 三十、最终核心原则

本层最终压缩为：

```text
Runtime 记录真实发生了什么；
Scientific Core 负责可复现的数学与规则；
Research Layer 决定如何把这些 Artifact 解释为预测样本、
Treatment、Exposure、Mediator 与 Outcome。
```

$$
\boxed{Forecasting \neq Causal\ Estimation}
$$

$$
\boxed{Assignment \neq Execution \neq Exposure \neq Effect}
$$

$$
\boxed{Shared\ Scientific\ Core \neq Shared\ Entire\ Runtime}
$$

最终目标不是构建一个复杂实验平台，而是让 MindFlow 的在线行为、压力模型、个性化、主动关怀和论文研究之间拥有一条可追溯、可复现、不会因工程便利而混淆科学语义的完整链路。
