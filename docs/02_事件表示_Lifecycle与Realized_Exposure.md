# MindFlow 事件表示、Lifecycle 与 Realized Exposure 建模

> 本文定义从原始日程、任务和行为信息到 Structured Event、Event Potential、Lifecycle 与 Realized Exposure 的完整事件层。本文不展开 Personal Appraisal、时间核、Channel Aggregation 和压力状态方程。

---

## 一、事件层的职责

事件层解决四个问题：

1. 现实中发生了什么类型的事情；
2. 该事件本身具有怎样的 Demand / Pressure / Recovery affordance；
3. 事件后来是否真实发生、发生到什么程度；
4. 哪些行为和对话信息可以更新事件状态。

完整链路：

```text
Raw Context
    ↓
Structured Event
    ↓
Event Potential
    ↓
Lifecycle State
    ↓
Mechanism-Specific Exposure
    ↓
Personal Appraisal
```

本文覆盖到 Mechanism-Specific Exposure 为止。

核心边界：

$$
\boxed{
ScheduledEvent
\neq
RealizedEvent
}
$$

$$
\boxed{
EventPotential
\neq
EffectiveInput
}
$$

---

## 二、Structured Event 的统一定义

每个事件记为：

$$
\boxed{
e
}
$$

推荐至少包含以下基础字段：

```text
event_id
participant_id
event_type
event_subtype
title
start_time
end_time
created_at
observed_at
known_at
source_type
source_id
confidence
lifecycle_state
```

其中：

- `event_type`：事件大类；
- `event_subtype`：更具体的业务语义；
- `start_time/end_time`：现实时间；
- `known_at`：系统何时合法知道该事件；
- `source_type`：信息来源；
- `lifecycle_state`：事件当前生命周期状态。

---

## 三、事件类型

v1 至少区分以下事件族。

### 1. Course Event

包括：

- lecture；
- lab；
- seminar；
- class presentation；
- exam-oriented class session。

Course Event 主要提供：

- Demand potential；
- 部分 Pressure structure；
- attendance / engagement exposure。

### 2. Task Event

包括：

- assignment；
- report；
- paper；
- research task；
- project deliverable；
- application；
- administrative task；
- exam preparation。

Task Event 同时可能产生：

- current execution Demand；
- deadline Pressure；
- unresolved obligation state。

### 3. Other Structured Event

例如：

- meeting；
- interview；
- presentation；
- competition；
- appointment；
- social commitment。

根据具体语义可产生 Demand、Pressure 或 Recovery opportunity。

### 4. Recovery Activity

例如：

- leisure；
- exercise；
- meal break；
- walk；
- entertainment；
- social recovery activity；
- short rest。

其主要输出为：

$$
R_e^{pot}
$$

但实际是否形成 Recovery 取决于 occurrence、context 和后续 Personal Recovery Fit。

### 5. Sleep / Nap

主夜睡眠和 nap 单独保留类型。

原因是：

- 主夜睡眠主要更新 $F$；
- nap 可以同时产生短时 $Q_R$ 与小幅 $F$ 更新；
- 不应把 sleep 简单当作普通 leisure event。

### 6. Consequence / Obligation-Generating Event

例如：

- missed course 后需要 catch-up；
- task scope 增加；
- missed deadline 后新增 resubmission；
- penalty handling；
- teacher communication。

这些事件本身可以生成新的 obligation，但只有真实 future action 存在时才建立。

---

## 四、Event Facts、Prior 与 Appraisal 的边界

Event Representation 只描述：

$$
\boxed{
EventIntrinsicFacts
}
$$

以及由这些事实形成的 population/event-class prior。

不直接存储 Personal Appraisal。

必须区分：

### Event Fact

例如：

- 课程时长；
- 学分；
- course category；
- deadline；
- estimated task size；
- presentation；
- physical activity；
- recovery activity type。

### Event/Class Prior

例如：

- 专业必修课通常具有较高 importance prior；
- presentation 通常具有 social-evaluative structure；
- exercise 通常具有一定 recovery affordance。

### Personal Appraisal

例如：

- 我是否擅长；
- 我是否在意；
- 我是否觉得结果可控；
- 我是否觉得不确定；
- 这个活动是否适合我恢复。

这些由后续 Appraisal 模块定义。

因此：

$$
\boxed{
Fact
\neq
Prior
\neq
Appraisal
}
$$

---

## 五、Event Potential 的总体结构

Event Potential 表示：

> 在不考虑当前 participant-specific appraisal 的情况下，这个事件本身具有多少 Demand、Pressure structure 或 Recovery affordance。

定义三个主要表示：

$$
\boxed{
D_e^{pot}\in[0,1]
}
$$

$$
\boxed{
P_e^{struct}
}
$$

$$
\boxed{
R_e^{pot}\in[0,1]
}
$$

Pressure 不直接压成单一标量，而保留 mechanism-specific structure：

$$
\boxed{
P_e^{struct}
=
[
U_{ddl,e},
U_{context,e},
D_{s,e}
]
}
$$

其中：

| 变量 | 名称 | 含义 |
|---|---|---|
| $U_{ddl,e}$ | Deadline / Time-Scarcity Structure | 任务在 deadline 约束下的时间稀缺程度 |
| $U_{context,e}$ | Structural Uncertainty | 事件本身客观存在的不确定结构 |
| $D_{s,e}$ | Social-Evaluative Structure | 被评价、展示、面试、答辩等社会评价结构 |

Personal Importance 不属于 $P^{struct}$。

---

## 六、Demand Potential $D_e^{pot}$

$D_e^{pot}$ 表示：

> 事件在单位暴露期间本身具有多高的 Demand intensity。

它可以基于事件事实组合：

- cognitive demand；
- execution demand；
- physical demand；
- task complexity；
- interruption requirement。

推荐将内部语义维度保持为固定 representation，而不是通过 EMA 再学习。

重要边界：

$$
\boxed{
D^{pot}
=
Intensity
}
$$

而不是：

$$
\boxed{
D^{pot}
=
Intensity\times Duration
}
$$

duration 主要通过事件 active interval 和后续 temporal kernel 表达。

这样避免事件持续时间被：

1. Potential；
2. Kernel exposure duration；

重复计算。

若未来有证据显示超长 session 会提高单位时间 Demand，再单独增加经过验证的 duration-shape modifier。

---

## 七、Deadline Structure $U_{ddl}$

Deadline Pressure 的事实基础来自：

$$
W_e^{rem}
$$

和：

$$
T_{e}^{capacity}
$$

其中：

- $W_e^{rem}$：当前任务 remaining effective effort；
- $T_e^{capacity}$：deadline 前真实可用于该任务的 effective available work capacity。

定义概念形式：

$$
\boxed{
U_{ddl,e}
=
f\left(
\frac{W_e^{rem}}
{T_e^{capacity}}
\right)
}
$$

其中 $f(\cdot)$ 为 monotonic bounded mapping。

必须保持：

$$
\boxed{
T_e^{capacity}
\neq
RawCalendarFreeTime
}
$$

它需要考虑：

- 已有课程；
- 睡眠；
- 固定活动；
- 其他任务竞争；
- 合理的有效工作上限。

$U_{ddl}$ 只描述 time scarcity，不表达 Personal Importance。

---

## 八、Structural Uncertainty $U_{context}$

定义：

$$
\boxed{
U_{context,e}\in[0,1]
}
$$

表示事件本身存在的客观或结构性不确定性，例如：

- requirement 尚未明确；
- 是否抽查未知；
- 结果机制不透明；
- meeting outcome 未知；
- task dependency 未解决。

必须保持：

$$
\boxed{
U_{context}
\neq
U^{perc}
}
$$

$U_{context}$ 属于 Event Fact / Structure。

$U^{perc}$ 属于 Personal Appraisal。

不能因为：

$$
U_{context}\uparrow
$$

就 deterministic 地生成：

$$
U^{perc}\uparrow
$$

否则会 double count。

---

## 九、Social-Evaluative Structure $D_s$

定义：

$$
\boxed{
D_{s,e}\in[0,1]
}
$$

表示事件是否包含显著的社会评价、公开展示或他人判断结构。

常见来源：

- presentation；
- defense；
- oral exam；
- interview；
- public competition；
- graded demonstration。

它描述的是：

> 事件是否存在 evaluative structure。

不描述：

> 用户是否在意。

后者属于：

$$
I_{i,e}
$$

由 Appraisal 模块处理。

---

## 十、Recovery Potential $R_e^{pot}$

Recovery Potential 表示：

> 该活动本身提供多少恢复机会。

可由以下 affordance 构成：

$$
r_{det}
$$

Psychological Detachment：是否有助于从任务/学业中脱离。

$$
r_{relax}
$$

Relaxation：是否提供低激活放松。

$$
r_{ctrl}
$$

Control / Autonomy：用户是否具有活动自主性。

$$
r_{related}
$$

Relatedness：是否提供积极社会联结。

$$
r_{mast}
$$

Mastery：是否提供非工作领域的积极掌控与成长体验。

这些 affordance 属于活动 representation。

最终：

$$
R_e^{pot}
$$

应通过固定、可审计的 mapping 形成。

必须保持：

$$
\boxed{
RecoveryOpportunity
\neq
ActualRecovery
}
$$

一个 calendar 空档本身不自动产生：

$$
R_e^{pot}>0
$$

只有有具体 recovery activity 或明确恢复性环境时才成立。

---

## 十一、课程类事件的表示

Course Event 至少建议保存：

```text
course_id
course_name
course_category
credit
scheduled_start
scheduled_end
delivery_mode
assessment_related
social_evaluative_component
```

其中：

`course_category` 可包含：

```text
major_required
major_elective
general_required
general_elective
foundation_required
```

这些字段用于：

- Event Potential；
- event-class prior；
- course-specific stable profile key。

但不能直接：

$$
course\_category
\Rightarrow
I_{i,e}=High
$$

课程类别最多形成 Personal Importance 的 prior。

---

## 十二、Task 类事件的表示

Task Event 至少保存：

```text
task_id
task_type
title
created_at
deadline
estimated_total_effort
remaining_effort
progress
status
parent_task_id
source
known_at
```

核心量：

$$
\boxed{
W_e^{rem}
}
$$

表示 current remaining effective effort。

Progress 只用于更新 $W^{rem}$：

$$
\boxed{
Progress
\rightarrow
W^{rem}
}
$$

不能同时把：

- progress；
- remaining effort；

作为两个独立 workload predictor。

任务若存在层级拆分，v1 采用：

$$
\boxed{
CountActiveLeavesOnly
}
$$

避免父任务与子任务重复累计。

---

## 十三、Lifecycle 的总体原则

Event Potential 只回答：

> 如果这个事件真实发生，它本身具有怎样的机制潜力？

Lifecycle 回答：

> 它实际上发生了吗？发生了多少？现在处于什么阶段？

因此：

$$
\boxed{
Potential
\neq
Lifecycle
}
$$

而 Lifecycle 也不使用一个统一 multiplier：

$$
Z_{lifecycle}
$$

去同时缩放 Demand、Pressure、Recovery。

正式原则：

$$
\boxed{
Lifecycle
\rightarrow
MechanismSpecificRouting
}
$$

---

## 十四、Mechanism-Specific Exposure Gate

定义：

$$
\boxed{
Z_{e}^{exec}\in[0,1]
}
$$

表示 Demand execution exposure。

$$
\boxed{
Z_{e}^{ddl}\in[0,1]
}
$$

表示 deadline mechanism 是否仍然 active。

$$
\boxed{
Z_{e}^{unc}\in[0,1]
}
$$

表示 uncertainty mechanism 是否仍然 active。

$$
\boxed{
Z_{e}^{soc}\in[0,1]
}
$$

表示 social-evaluative mechanism 是否真实暴露。

$$
\boxed{
Z_{e}^{occ}\in[0,1]
}
$$

表示 recovery activity 实际 occurrence 程度。

必须避免：

$$
\boxed{
Z^{exec}
=
Z^{ddl}
=
Z^{unc}
=
Z^{soc}
=
Z^{occ}
}
$$

因为不同机制可能在同一 lifecycle state 下表现不同。

---

## 十五、Course Lifecycle

Course Event 推荐状态：

```text
SCHEDULED
ATTENDED
PARTIAL
SKIPPED
CANCELLED
```

### 1. SCHEDULED

当前只是 future context。

不能自动令：

$$
Z^{exec}=1
$$

### 2. ATTENDED

通常：

$$
Z^{exec}\approx1
$$

若课程存在 presentation / evaluation，可同时：

$$
Z^{soc}>0
$$

### 3. PARTIAL

$$
0<Z^{exec}<1
$$

具体比例可由：

- attendance duration；
- explicit user report；
- high-confidence behavioral evidence；

确定。

### 4. SKIPPED

原 Course active Demand：

$$
Z^{exec}=0
$$

但 skipped course 不自动增加 $C_U$。

只有产生真实 future action，例如：

- catch-up；
-补录像；
-补实验；
-补材料；

才建立新的 obligation。

### 5. CANCELLED

一旦 cancellation 在时间 $t_c$ 被系统知道：

- future execution exposure 归零；
- future event-specific pressure mechanism 根据事实终止或更新。

不能使用未来 cancellation information 回写 $t<t_c$ 的预测。

---

## 十六、Task Lifecycle

Task Event 推荐状态：

```text
PLANNED
OPEN
IN_PROGRESS
BLOCKED
COMPLETED
CANCELLED
OVERDUE
SUPERSEDED
```

### 1. PLANNED / OPEN

任务尚未执行：

$$
Z^{exec}=0
$$

但 deadline mechanism 可以：

$$
Z^{ddl}=1
$$

所以可能出现：

$$
D^{eff}=0
$$

但：

$$
P^{eff}>0
$$

### 2. IN_PROGRESS

执行期间：

$$
Z^{exec}>0
$$

并根据 progress 更新：

$$
W^{rem}\downarrow
$$

### 3. BLOCKED

当前无法继续工作：

$$
Z^{exec}=0
$$

但如果 obligation 仍需未来完成，则：

$$
W^{rem}>0
$$

并继续保留 unresolved obligation。

### 4. COMPLETED

$$
Z^{exec}\rightarrow0
$$

$$
Z^{ddl}\rightarrow0
$$

$$
W^{rem}\rightarrow0
$$

对应 obligation 从 unresolved inventory 中关闭。

### 5. CANCELLED

任务不再需要完成：

$$
W^{rem}\rightarrow0
$$

若 cancellation 没有新 consequence，则 obligation 关闭。

### 6. OVERDUE

如果 deadline 已错过，但任务仍需继续完成：

$$
\boxed{
SameObligationContinues
}
$$

保留：

$$
W^{rem}
$$

只更新：

- deadline state；
- overdue flag；
- pressure/consequence。

不复制一个新的相同 obligation。

### 7. SUPERSEDED

原任务被新任务完全替代时，旧任务关闭，新任务单独建立。

---

## 十七、Recovery Activity Lifecycle

Recovery Activity 至少区分：

```text
PLANNED
OCCURRED
PARTIAL
SKIPPED
CANCELLED
```

### PLANNED

不能直接产生 Recovery：

$$
Z^{occ}=0
$$

### OCCURRED

$$
Z^{occ}=1
$$

### PARTIAL

$$
0<Z^{occ}<1
$$

### SKIPPED / CANCELLED

$$
Z^{occ}=0
$$

Actual Recovery 仍需后续结合：

- $R^{pot}$；
- context compatibility；
- personal recovery fit。

---

## 十八、Context Compatibility

Recovery Activity 还需要一个事件上下文兼容量：

$$
\boxed{
M_e^{context}\in[0,1]
}
$$

表示：

> 即使 recovery activity 发生，其当前上下文是否允许其充分实现恢复作用。

例如：

- 午休夹在连续高密度课程之间；
- break 时间过短；
- leisure 被频繁 interruption；
- nominal rest 实际伴随任务处理。

$M^{context}$ 属于 exposure/context layer，不属于 Personal Recovery Fit。

因此：

$$
\boxed{
M^{context}
\neq
F^{rec}
}
$$

---

## 十九、Behavioral Evidence 的定位

现实行为可以用于更新 Lifecycle，但不能直接改写 stress。

定义候选：

$$
\boxed{
E_{j,e}^{beh}
\in
\{
Congruent,\ Ambiguous,\ Incongruent
\}
}
$$

其中：

- `Congruent`：行为与 scheduled event 相符；
- `Ambiguous`：证据不足；
- `Incongruent`：行为明显与 scheduled event 冲突。

例如课程时间内：

| 观察 | Behavioral Evidence |
|---|---|
| 询问当前课程内容 | Congruent |
| 偶尔发一条无关消息 | Ambiguous |
| 长时间高密度无关对话 | Incongruent |
| 明确说“今天没去” | Explicit lifecycle evidence |

Behavioral Evidence 的作用是：

$$
\boxed{
Behavior
\rightarrow
LifecycleBelief
}
$$

不是：

$$
Behavior
\rightarrow
Stress
$$

---

## 二十、Conversation Evidence 对 Event State 的作用

Conversation 可以提取：

- explicit attendance；
- explicit skip；
- current task progress；
- new deadline；
- task cancellation；
- remaining effort；
- catch-up obligation；
- recovery occurrence；
- appraisal evidence。

但必须走：

$$
\boxed{
Conversation
\rightarrow
StructuredEvidence
\rightarrow
Event/LifecycleState
}
$$

LLM 不能直接根据一句模糊文本把：

$$
Lifecycle=SKIPPED
$$

写成确定事实。

证据不足时应保留：

$$
\boxed{
LifecycleBelief
}
$$

或触发 clarification。

---

## 二十一、Lifecycle Uncertainty

Lifecycle 不确定时，不强行选择唯一状态。

例如：

$$
P(
Lifecycle
\in
\{ATTENDED,PARTIAL,SKIPPED\}
)
$$

可以由：

- calendar prior；
- behavioral evidence；
- explicit report；

共同更新。

Online inference 可以使用：

- discrete marginalization；
- expected gate；
- scenario sampling。

Future scenario 中，单条 scenario 内应保持同一个 lifecycle realization 一致。

---

## 二十二、Obligation 的建立规则

只有同时满足：

$$
Commitment
$$

$$
FutureAction
$$

$$
Unresolved
$$

$$
W^{rem}>0
$$

才建立正式 obligation。

因此：

$$
\boxed{
FutureEvent
\neq
Obligation
}
$$

普通未来课程不属于 obligation。

普通考试事件也不自动属于 obligation。

只有具体：

```text
Exam Preparation
```

或：

```text
Catch-up Work
```

等 future action 才建立。

---

## 二十三、LLM Inferred Candidate 的限制

LLM 可以提出：

```text
Possible Obligation
Possible Lifecycle Change
Possible Recovery Activity
```

但若来源只是语义推断，应标记：

```text
INFERRED_CANDIDATE
```

不能直接进入正式：

- $C_U$；
- realized exposure；
- completed progress；
- recovered state。

需要：

- explicit evidence；
- clarification；
- high-confidence deterministic rule；

之一完成确认。

---

## 二十四、Knowledge-Time 与 Event Revision

所有 lifecycle/state revision 都必须保存：

```text
event_time
reported_at
observed_at
known_at
updated_at
```

若用户在 18:00 才说：

> “今天 10:00 那节课我没去。”

则从 18:00 开始，系统可以修正历史 event state 用于当前 inference 和 retrospective analysis。

但在评估 12:00 时真实可用的信息时，仍不能假装 12:00 已经知道这一事实。

必须区分：

$$
\boxed{
RetrospectiveBestEstimate
}
$$

与：

$$
\boxed{
ProspectiveKnownState
}
$$

---

## 二十五、Course 与 Task 的关联

Course Event 与 Task/Obligation 不应混为一个对象。

例如：

```text
Course: Machine Learning Lecture
Task: Assignment 3
```

两者可以关联：

```text
course_id
```

但分别拥有：

- Lifecycle；
- Demand；
- Pressure；
- obligation state。

这样避免课程本身持续被错误当作 unfinished task。

---

## 二十六、Event Potential 与 Duration 的最终边界

Duration 主要通过：

```text
start_time
end_time
actual_start
actual_end
```

进入后续 temporal exposure。

因此 $D^{pot}$ 和 $R^{pot}$ 表示：

$$
\boxed{
Per-Exposure / Event Intensity
}
$$

而不是将总 duration 再整体乘入 potential。

例如三小时课程相对于一小时课程，首先通过更长 active interval 提供更多 Demand exposure，而不是简单：

$$
D^{pot}_{3h}=3D^{pot}_{1h}
$$

Recovery 也遵循相同原则。

---

## 二十七、事件层必须保存的核心变量

### 通用

| 变量 | 含义 |
|---|---|
| $e$ | event identifier |
| $t_e^{start}$ | scheduled/known event start |
| $t_e^{end}$ | scheduled/known event end |
| $D_e^{pot}$ | event-level Demand intensity potential |
| $U_{ddl,e}$ | deadline/time-scarcity structure |
| $U_{context,e}$ | structural uncertainty |
| $D_{s,e}$ | social-evaluative structure |
| $R_e^{pot}$ | recovery opportunity potential |
| $M_e^{context}$ | recovery context compatibility |

### Lifecycle / Exposure

| 变量 | 含义 |
|---|---|
| $Z_e^{exec}$ | actual execution exposure |
| $Z_e^{ddl}$ | deadline mechanism active gate |
| $Z_e^{unc}$ | uncertainty mechanism active gate |
| $Z_e^{soc}$ | social-evaluative mechanism exposure |
| $Z_e^{occ}$ | recovery activity occurrence |

### Task / Obligation

| 变量 | 含义 |
|---|---|
| $W_e^{rem}$ | remaining effective effort |
| $W_e^{total}$ | total effective effort estimate |
| $Progress_e$ | observed/reported completion fraction |
| $Deadline_e$ | current valid deadline |
| $Status_e$ | task lifecycle state |

---

## 二十八、事件层禁止的直接映射

必须禁止：

$$
\boxed{
ScheduledCourse
\Rightarrow
Attended
}
$$

$$
\boxed{
PlannedTask
\Rightarrow
DemandExposure
}
$$

$$
\boxed{
FreeTime
\Rightarrow
Recovery
}
$$

$$
\boxed{
SkippedCourse
\Rightarrow
C_U+\delta
}
$$

$$
\boxed{
HighCredit
\Rightarrow
HighPersonalImportance
}
$$

$$
\boxed{
StructuralUncertainty
\Rightarrow
HighPerceivedUncertainty
}
$$

$$
\boxed{
ConversationDuringClass
\Rightarrow
SkippedClass
}
$$

$$
\boxed{
LLMInference
\Rightarrow
ConfirmedLifecycle
}
$$

---

## 二十九、事件层的最终输出

Event Layer 最终不直接输出 Stress。

它只向后续模块提供：

$$
\boxed{
D_e^{pot}
}
$$

$$
\boxed{
[
U_{ddl,e},
U_{context,e},
D_{s,e}
]
}
$$

$$
\boxed{
R_e^{pot}
}
$$

$$
\boxed{
[
Z_e^{exec},
Z_e^{ddl},
Z_e^{unc},
Z_e^{soc},
Z_e^{occ}
]
}
$$

以及：

- lifecycle belief；
- task remaining effort；
- obligation state；
- context compatibility；
- structured evidence；
- provenance / known_at。

后续 Personal Appraisal 模块才负责将这些 event-level quantities 转换为 participant-specific：

$$
D_{i,e}^{eff},
\quad
P_{i,e}^{eff},
\quad
R_{i,e}^{eff}
$$

---

## 三十、当前事件层冻结边界

1. Event Potential 与 Realized Exposure 分离。
2. Event Fact、Event/Class Prior、Personal Appraisal 分离。
3. Demand Potential 表示 intensity，不重复编码 duration。
4. Pressure 保留 deadline、structural uncertainty、social evaluation 三类 mechanism structure。
5. Personal Importance 不进入 Event Pressure Structure。
6. Recovery Opportunity 不等于 Actual Recovery。
7. Lifecycle 采用 mechanism-specific routing，不使用一个统一 gate。
8. Scheduled Course 不自动等于 attended。
9. Planned Task 不自动产生 execution Demand。
10. Planned Recovery 不自动产生 Recovery。
11. Task progress 只用于更新 $W^{rem}$。
12. 父子任务只累计 active leaf，避免重复工作量。
13. Overdue 但仍需完成的同一任务继续保留同一 obligation。
14. Missed Course 只有形成真实 catch-up action 才建立 obligation。
15. Behavioral Evidence 只更新 Lifecycle/Exposure，不直接修改 Stress。
16. Conversation 只通过 Structured Evidence 更新 Event State。
17. LLM inferred candidate 不直接成为 confirmed event/lifecycle/obligation。
18. Lifecycle uncertainty 允许保持概率分布。
19. 所有 Event revision 必须遵守 `known_at`。
20. Course Event 与关联 Task/Obligation 保持独立对象。
