# MindFlow 12 第一阶段：Scenario Annotation 与 Representation Semantics Freeze 实施方案

> 适用分支：`refactor/stress-model-v2`
>
> 阶段定位：本阶段位于 Event V2、Appraisal、Synthetic 与新压力动力学算法实现之前。目标不是验证压力预测准确率，而是验证 Representation Layer 的变量定义是否可理解、可操作、可复现，并形成可版本化的 `Representation Semantics v1.0`。
>
> 本阶段为离线 Research 工具链，不接入生产 Bot Runtime、不修改当前 CTSSM、不依赖生产 PostgreSQL。
>
---

# 一、阶段目标

本阶段回答以下问题：

1. 不同独立标注者能否稳定识别同一 Event、Lifecycle、Exposure 与 Obligation；
2. `D_pot`、`U_context`、`D_s`、`R_pot`、`M_context` 等表示层 construct 是否定义清楚；
3. 标注者能否区分 Event Fact、Prior、Personal Appraisal Evidence；
4. `C_exec`、`I`、`C_out`、`U_perc`、`F_rec` 是否能够依据明确 evidence 进行可复现编码；
5. 标注者能否稳定区分 Recovery Opportunity、Recovery Occurrence、Recovery Fit；
6. Bot Response 是否能够稳定编码为 task assistance、supportive response、sensing、safety 等角色，并稳定标注 `V/G/Rel`；
7. 哪些变量存在系统性 double counting、future leakage 或跨层推断；
8. 哪些变量应 `KEEP / REVISE / SIMPLIFY / DROP`；
9. 哪些语义可以冻结进入 Event V2 / Appraisal / Bot Representation 的正式代码实现。

核心顺序：

```text
Representation Contract Draft
        ↓
Scenario Annotation
        ↓
Disagreement / Boundary Audit
        ↓
Revise Contract
        ↓
Re-annotation
        ↓
Representation Semantics Freeze
        ↓
Event/Appraisal/Bot Representation Code
        ↓
Synthetic
        ↓
Pilot
```

必须保持：

```text
先验证“变量能否被稳定理解”
再验证“数值映射和参数能否被数据识别”
```

---

# 二、本阶段冻结什么

第一阶段冻结：

```text
Variable Definition
Layer Ownership
Allowed Labels
UNKNOWN / N/A / NO_EVIDENCE semantics
Evidence Rules
Forbidden Inference Rules
Annotation Schema
Coding Manual
Scenario Coverage Rules
```

形成：

```text
Representation Semantics v1.0
Coding Manual v1.0
Scenario Bank v1.0
Adjudicated Reference Set v1.0
```

本阶段不冻结：

```text
ordinal → [0,1] numeric mapping
rho_app
temporal kernel parameters
latent dynamics parameters
EMA measurement parameters
Q_D / Q_P / Q_R numerical trajectories
C_D / C_U / F numerical trajectories
A / B / S
Q_BS
Forecast risk
Care effectiveness
```

因此：

```text
Semantic Freeze
≠
Numeric Parameter Freeze
```

---

# 三、本阶段不修改的现有生产代码

第一阶段不得以 Scenario Annotation 为理由重构：

```text
algorithm/dynamic_state_model.py
core_engine/*
ForecastCoordinator
BotWorker
Calendar Saga
ToolRegistry
production repositories
current CTSSM forecast path
```

Scenario Annotation 先作为独立离线 Research Package 建立。

只有 `Representation Semantics v1.0` 通过 Freeze Gate 后，下一阶段才据此实现 Event V2 / Obligation / Lifecycle / Exposure contract。

---

# 四、Scenario 的两级结构

## 1. Two-Week Participant Context Pack

每个虚拟 participant 使用连续两周上下文：

```text
Week 1
+
Week 2
```

非期末阶段默认：

```text
Week2 recurring course schedule
=
Week1 recurring course schedule
```

除非 Scenario 明确用于：

```text
调课
停课
节假日
临时课程变更
```

Course 是稳定周期骨架。

Task / research / recovery / social / sleep / conversation 是动态层。

## 2. Focal Scenario Window

从 Two-Week Pack 中抽取局部窗口，例如：

```text
周三 13:30–17:30
周五晚 + 周六上午
某一任务 deadline 前 24h
某次 Bot interaction 前后
```

Annotator 只看到当前 Annotation Module 所必需的信息。

不把完整两周全部暴露给每个标注任务。

---

# 五、现实场景生成约束

## 1. 默认课程节次

普通课程优先使用：

| 节次 | 时间 |
|---|---|
| 1 | 08:00–08:45 |
| 2 | 08:55–09:40 |
| 3 | 10:00–10:45 |
| 4 | 10:55–11:40 |
| 5 | 12:00–12:45 |
| 6 | 12:55–13:40 |
| 7 | 14:00–14:45 |
| 8 | 14:55–15:40 |
| 9 | 16:00–16:45 |
| 10 | 16:55–17:40 |
| 11 | 18:30–19:15 |
| 12 | 19:25–20:10 |
| 13 | 20:30–21:15 |
| 14 | 21:25–22:10 |

连续课程可占多个标准节次。

## 2. Course Load Coverage

Scenario corpus coverage 使用：

```text
Low course load        ~60%
Moderate course load   ~25%
Dense course edge      ~15%
```

这些比例只用于 Scenario coverage，不代表总体大学生 prevalence。

## 3. Weekend

默认：

```text
大多数 pack 周末无 recurring course
```

必须覆盖：

```text
真正休闲
Calendar empty + high task load
Calendar empty + procrastination
Research / self-study day
Sleep-recovery weekend
```

禁止：

```text
CalendarSparse
→ Recovery
```

## 4. Appraisal Evidence

大量 Scenario 必须没有完整 appraisal evidence。

禁止为了让 annotation 表“填满”而人为写全：

```text
C_exec
I
C_out
U_perc
F_rec
```

`NO_EVIDENCE / UNKNOWN` 是正常结果。

---

# 六、Scenario Bank 类型

所有场景至少属于以下设计类型之一：

## 1. Anchor

答案边界明确，用于 Calibration。

例如：

```text
“今天高数我明确没去。”
→ Course lifecycle 的 SKIPPED anchor
```

## 2. Minimal Pair

两个 Scenario 只改变一个 target factor。

用于检查变量正交性。

## 3. Boundary / Unknown

故意提供不足信息，检查 Annotator 是否使用 Unknown / No Evidence。

## 4. Revision / Timing

信息在不同 `known_at` 到达，检查 future leakage 与 correction semantics。

## 5. Cross-Layer Trap

故意放入容易诱导跨层推断的信息。

例如：

```text
Objective difficulty high
X→ automatically C_exec low
```

---

# 七、Scenario Bank 重点边界

第一版必须覆盖：

```text
Scheduled vs Realized
Duration vs Fractional Exposure
Event vs Obligation
Parent Obligation vs Active Leaves
Free Time vs Recovery
Recovery Opportunity vs Recovery Occurrence
Recovery Opportunity vs Recovery Fit
Difficulty vs C_exec
Objective Stakes vs Personal Importance
C_exec vs C_out
U_context vs U_perc
Preference vs F_rec
Task Help vs Bot Support
Personalization vs Support Quality
Known Now vs Known Later
Support Potential vs Support Effect
```

---

# 八、Scenario Corpus 第一版规模

保留旧版 120 个 Focal Scenario 的总体规模：

```text
20 Two-Week Participant Packs
×
6 Focal Windows
=
120 Scenarios
```

但开发分阶段执行。

## Milestone A：Calibration

先实现：

```text
24 Calibration Scenarios
```

完成：

```text
Schema
Manual v0.1
Annotation Pipeline
Agreement Pipeline
Disagreement Pipeline
```

并完成一次完整 Calibration。

未通过 Calibration 不生成剩余 Main/Edge Set。

## Milestone B：正式 Corpus

Calibration 后生成：

```text
72 Main Representative
24 Edge / Orthogonal
```

最终：

```text
24 Calibration
72 Main
24 Edge
=
120
```

Calibration Set 不作为最终 blind reliability claim 的主体。

---

# 九、Natural 与 Structured 双版本

约 15–20 个最关键边界场景建立双版本：

```text
Natural-language Scenario
Structured Scenario
```

优先覆盖：

```text
Course PARTIAL
Deadline revision
Obligation
U_context vs U_perc
C_exec
Recovery occurrence
Bot support classification
```

用途：

```text
Structured disagreement
→ Representation Contract 问题

Natural-only disagreement
→ Extraction / language interpretation 问题
```

同一 Annotator 在同一轮不得看到同一 Scenario 的两个版本。

通过 assignment script 做 counterbalance。

---

# 十、Scenario Hidden Design Metadata

每个 minimal pair / orthogonal scenario 在 Annotator 不可见区域保存：

```yaml
pair_id:
variant:

manipulated_factor:

expected_sensitive_constructs:
  - ...

expected_invariant_constructs:
  - ...

coverage_tags:
  - ...

design_notes:
```

用于自动检查：

```text
Orthogonality Violation
```

例如：

```text
manipulated_factor = U_context

expected_sensitive = [U_context]
expected_invariant = [U_perc_without_new_personal_evidence, C_exec, I]
```

---

# 十一、三套 Annotation Module

禁止建立“一张表一次标完所有变量”。

共用同一 Scenario Bank，但拆为三个 Annotation Module。

```text
Module A
Event / Lifecycle / Exposure / Obligation / Recovery Representation

Module B
Personal Appraisal Evidence

Module C
Bot Interaction / Support Representation
```

各 Module 使用独立 view 与 JSON Schema。

同一 Scenario 可以进入多个 Module，但 Annotator 看到的字段和 context 必须 purpose-scoped。

---

# 十二、统一 Annotation Envelope

所有 Annotation Record 共用：

```text
annotation_id
scenario_id
scenario_version

annotation_module
target_ref
variable

label

evidence_ref / evidence_span
evidence_strength

unknown_reason
ambiguity_flag
annotator_confidence

manual_version
annotator_id
annotation_round

created_at
```

其中必须区分：

```text
Evidence Strength
= 场景证据本身有多直接

Annotator Confidence
= 标注者对自己的判断有多确定

Model Confidence
= 后续自动系统的 belief/confidence
```

第一阶段只处理前两者。

---

# 十三、统一特殊语义

## UNKNOWN

```text
Construct 适用
但 evidence 不足
```

## N/A

```text
Construct 对当前 target 本身不适用
```

## AMBIGUOUS

```text
当前可见 evidence 存在真正冲突
或支持多个合理解释
```

不能把三者混为一类。

Module B 的 Appraisal Evidence 使用更精确的：

```text
NO_EVIDENCE
```

表示当前 Scenario 没有可编码的 participant-specific appraisal evidence。

---

# 十四、Unknown Reason

第一版 controlled vocabulary：

```text
NOT_MENTIONED
INSUFFICIENT_DETAIL
CONFLICTING_EVIDENCE
TEMPORAL_SCOPE_UNCLEAR
TARGET_UNCLEAR
SOURCE_UNRELIABLE
OTHER
```

用于 disagreement audit。

---

# 十五、Annotator Confidence

第一版仅三档：

```text
LOW
MEDIUM
HIGH
```

只用于 annotation process diagnosis。

不得进入模型 evidence weighting。

---

# 十六、Module A：Event Family

第一版：

```text
COURSE
TASK
STRUCTURED_EVENT
RECOVERY_ACTIVITY
SLEEP
NAP
CONSEQUENCE_EVENT
OTHER
UNKNOWN
```

`OTHER`：

```text
事件已知，但当前 ontology 不进一步分类
```

`UNKNOWN`：

```text
连事件本体都无法可靠判定
```

---

# 十七、Event Subtype

使用：

```text
controlled-known value
+
OTHER:<free-text>
```

第一版建议：

```text
COURSE
- lecture
- lab
- seminar
- course_presentation

TASK
- assignment
- exam_preparation
- report
- paper
- research_task
- project
- administrative

STRUCTURED_EVENT
- meeting
- presentation
- interview
- competition
- appointment

RECOVERY_ACTIVITY
- exercise
- leisure
- walk
- entertainment
- social_recovery
- meal_break
- short_rest
```

不追求完整大学生活 ontology。

---

# 十八、Event Facts

事实类字段尽量记录事实本身，不离散化。

包括：

```text
scheduled_start
scheduled_end

actual_start
actual_end

deadline

progress

estimated_total_effort
remaining_effort

parent_relation

cancellation
```

如果 Scenario 没提供：

```text
UNKNOWN
```

Annotator 不得自行填 default。

---

# 十九、Lifecycle Label

## Course

```text
SCHEDULED
ATTENDED
PARTIAL
SKIPPED
CANCELLED
UNKNOWN
```

## Task

```text
PLANNED
OPEN
IN_PROGRESS
BLOCKED
COMPLETED
CANCELLED
OVERDUE
SUPERSEDED
UNKNOWN
```

## Recovery / Sleep / Nap

第一版 occurrence lifecycle：

```text
PLANNED
OCCURRED
PARTIAL
SKIPPED
CANCELLED
UNKNOWN
```

暂不建设复杂 Sleep State ontology。

---

# 二十、Mechanism Exposure

第一版不标 `0.37 / 0.64`。

统一：

```text
INACTIVE
ACTIVE
PARTIAL
UNKNOWN
N/A
```

字段：

```text
execution_exposure
deadline_exposure
uncertainty_exposure
social_exposure
recovery_occurrence
```

`PARTIAL` 必须同时记录：

```text
partial_encoding_basis
```

允许：

```text
ACTUAL_INTERVAL
FRACTION_ONLY
OTHER_EXPLICIT
```

---

# 二十一、SingleExposureEncodingRule

如果已知实际区间：

```text
scheduled = 10:00–11:40
actual = 10:00–10:25
```

则 partial 主要通过：

```text
ACTUAL_INTERVAL
```

表达。

不得同时再把同一事实编码为 fractional gate。

如果只知道：

```text
“大概上了一半”
```

而没有实际 interval：

```text
FRACTION_ONLY
```

可以使用。

需要专门统计：

```text
ExposureDoubleEncodingViolation
```

---

# 二十二、Obligation

第一版不计算 `C_U`。

只标：

```text
obligation_exists:
  YES
  NO
  UNKNOWN

obligation_status:
  OPEN
  IN_PROGRESS
  BLOCKED
  COMPLETED
  CANCELLED
  SUPERSEDED
  UNKNOWN

parent_relation:
  NONE
  PARENT_REF
  UNKNOWN

active_leaf:
  YES
  NO
  UNKNOWN

deadline:
  explicit timestamp / UNKNOWN

remaining_effort:
  explicit value
  SMALL
  MEDIUM
  LARGE
  UNKNOWN
```

只有同时具有：

```text
Commitment
Future Action
Unresolved
```

才构成 active obligation。

必须保持：

```text
Missed Event
≠
Automatic Obligation
```

---

# 二十三、Demand Potential

第一版：

```text
LOW
MEDIUM
HIGH
UNKNOWN
N/A
```

定义：

> 在不考虑当前 participant 是否擅长、是否在意、是否焦虑的情况下，该事件单位暴露期间本身具有多高的认知 / 执行 / 体力 Demand intensity。

允许依据：

```text
activity requirements
objective task complexity
cognitive demand
execution demand
physical demand
```

禁止依据：

```text
用户是否擅长
用户是否紧张
用户是否在意
event duration
deadline proximity
```

必须保持：

```text
D_pot
≠
Intensity × Duration
```

---

# 二十四、Deadline Scarcity

第一版不把 `U_ddl` 作为所有场景的必填人工评分。

原因：

```text
U_ddl(t)
```

原则上应由：

```text
remaining effort
deadline
effective available capacity
```

通过 versioned representation mapping 计算。

Scenario Annotation 主要验证：

```text
deadline facts
remaining work facts
deadline mechanism active/inactive
```

只在专门的 deadline mapping validation scenario 中允许额外填写：

```text
deadline_scarcity_band:
  LOW
  MEDIUM
  HIGH
  UNKNOWN
```

用于检查 qualitative ordering。

不得把该人工 band 作为后续模型 Ground Truth。

---

# 二十五、Structural Uncertainty

`U_context`：

```text
LOW
MEDIUM
HIGH
UNKNOWN
N/A
```

定义：

> Event / Task 本身的信息结构、要求、dependency、结果机制存在多少结构性不确定性。

允许：

```text
requirement unclear
dependency unresolved
grading mechanism unknown
objective rule uncertainty
```

禁止：

```text
“我很慌”
“我觉得自己不行”
```

后者属于 Appraisal Evidence。

---

# 二十六、Social-Evaluative Structure

`D_s`：

```text
ABSENT
PRESENT
STRONG
UNKNOWN
N/A
```

定义：

```text
ABSENT
= 无明显公开展示 / 他人评价结构

PRESENT
= 存在明确评价结构

STRONG
= presentation / defense / interview / oral exam 等核心评价结构
```

如果 `PRESENT / STRONG` 长期无法可靠区分，第一轮 adjudication 后允许简化为：

```text
ABSENT / PRESENT
```

---

# 二十七、Recovery Opportunity

`R_pot`：

```text
LOW
MEDIUM
HIGH
UNKNOWN
N/A
```

标注 activity affordance：

```text
detachment
relaxation
autonomy/control
positive social connection
mastery outside workload
```

禁止用 future helpfulness / mood improvement 反推 `R_pot`。

---

# 二十八、Recovery Context Compatibility

`M_context`：

```text
POOR
PARTIAL
GOOD
UNKNOWN
N/A
```

描述：

> 活动即使发生，当前环境是否允许其较充分实现 recovery opportunity。

例如：

```text
uninterrupted nap
vs
nap squeezed between dense classes
```

必须保持：

```text
M_context
≠
F_rec
```

---

# 二十九、Module B：Appraisal Evidence

Module B 不要求 Annotator 猜 participant 的“心理真值”。

Annotator 标：

```text
Text / Explicit Report
↓
StructuredAppraisalEvidence
```

五个 dimension：

```text
C_EXEC
IMPORTANCE
C_OUT
U_PERC
F_REC
```

---

# 三十、Appraisal Evidence Value

统一：

```text
LOW
MEDIUM
HIGH
NO_EVIDENCE
AMBIGUOUS
```

其中：

```text
NO_EVIDENCE
```

表示：

```text
当前 Scenario 没有可支持该 appraisal 方向的 participant-specific evidence
```

它不是：

```text
MEDIUM
```

也不表示：

```text
participant 的真实状态已知为“未知值”
```

后续 Belief Resolver 再把证据转换为 appraisal belief。

---

# 三十一、Appraisal Evidence Strength

沿用模型文档：

```text
STRONG
MODERATE
WEAK
N/A
```

规则：

```text
直接回答 appraisal probe
→ STRONG

自发、明确、episode-specific 陈述
→ STRONG

明确但间接语言
→ MODERATE

模糊暗示但仍有明确方向
→ WEAK

仅 Event metadata
→ N/A，不构成 appraisal evidence
```

---

# 三十二、Appraisal Scope

第一版同时标：

```text
EPISODE
EVENT_CLASS
STABLE_GENERAL
UNKNOWN_SCOPE
```

例如：

```text
“今天这节高数我完全跟不上”
→ EPISODE

“高数我一直都挺擅长”
→ EVENT_CLASS

“我一般处理考试都比较有把握”
→ STABLE_GENERAL
```

为后续 StableProfile 与 Episode Appraisal 分离提供依据。

---

# 三十三、Appraisal Evidence Span

所有非 `NO_EVIDENCE` Appraisal annotation 必须提供：

```text
evidence_span
```

如果无法指出具体 evidence：

```text
优先 NO_EVIDENCE / AMBIGUOUS
```

而不是凭直觉填心理状态。

---

# 三十四、Appraisal 五维判定问题

## C_EXEC

```text
用户有没有表达：
“我能不能处理/执行这件事？”
```

## IMPORTANCE

```text
用户有没有表达：
“这件事或结果对我有多重要？”
```

## C_OUT

```text
用户有没有表达：
“我还能多大程度影响结果？”
```

## U_PERC

```text
用户有没有表达：
“我主观上有多拿不准/不确定？”
```

## F_REC

```text
用户有没有表达：
“这种活动对我通常/这次是否有恢复作用？”
```

---

# 三十五、Forbidden Inference Rules

Coding Manual 必须显式列出：

```text
Objective difficulty high
X→ C_exec low

专业必修 / 学分高
X→ Importance high

Deadline near
X→ U_perc high

U_context high
X→ U_perc high

Progress low
X→ C_exec low

Presentation
X→ participant importance high

Free time
X→ Recovery occurred

Recovery activity occurred
X→ F_rec high

Missed course
X→ catch-up obligation exists

EMA high
X→ any appraisal label
```

这些规则同时用于自动 `CriticalViolation` audit。

---

# 三十六、Module C：Bot Interaction

Module C 以：

```text
BotResponseUnit
```

为最小标注单位。

不以单个句子/semantic act 独立制造多个 support pulse。

---

# 三十七、Interaction Origin

第一版：

```text
USER_INITIATED
BOT_INITIATED
SYSTEM_TRANSACTIONAL
UNKNOWN
```

---

# 三十八、Interaction Role

第一版：

```text
ORDINARY_INFORMATION
TASK_EXECUTION_SUPPORT
EMOTIONAL_SUPPORT
COPING_SUPPORT
SENSING
RECOVERY_SUGGESTION
SAFETY_SUPPORT
MIXED
OTHER
UNKNOWN
```

若 reliability 显示某些 Role 无法稳定区分，允许在 Freeze 前合并。

---

# 三十九、Support Gate

`J_sup` 第一版：

```text
SUPPORTIVE
NON_SUPPORTIVE
SAFETY_ONLY
AMBIGUOUS
```

普通：

```text
task execution help
technical advice
ordinary information
```

不自动进入 supportive dynamics。

---

# 四十、Validation / Guidance / Relevance

沿用：

```text
0
0.5
1
```

但 Coding Manual 必须使用语义 anchor。

## Validation

```text
0
= 无 supportive acknowledgment

0.5
= 有承接，但较表面或有限

1
= 明确、恰当、针对当前体验的 relational validation
```

## Guidance

```text
0
= 无 coping-oriented guidance

0.5
= 有方向，但较泛化或有限

1
= 清晰、低负担、直接针对 coping / stress management
```

普通任务技巧不计入 `G`。

## Relevance

```text
0
= 与当前已知 context 基本不匹配

0.5
= 部分相关但较泛化，或遗漏关键 context

1
= 与当前 request/context 明确匹配
```

只能使用：

```text
known_at <= send_at
```

的信息判断。

---

# 四十一、Personalization Metadata

只作为 metadata：

```text
NONE
CURRENT_CONTEXT
HISTORICAL_PREFERENCE
STABLE_PROFILE
MIXED
UNKNOWN
```

必须保持：

```text
Personalized
≠
Supportive
≠
Effective
```

不直接进入 `V/G/Rel` 或 Support Potential。

---

# 四十二、Support Exposure

若 Scenario 提供可靠 exposure evidence，可额外标：

```text
seen_status:
  SEEN
  NOT_SEEN
  UNKNOWN
  N/A
```

它与 `J_sup/V/G/Rel` 分开。

未来 reply 不能提前回写：

```text
seen_at < reply_at
```

不存在 read receipt 时，不得凭空制造 precise seen time。

---

# 四十三、Bot Annotation 不标 Effect

禁止 Annotator 输出：

```text
reduced_stress
helped_user
effective_support
Q_BS
care_effect
```

Human/AI Coding 只回答：

```text
这条 Bot Response 提供了什么 support affordance？
```

而不是：

```text
用户最终是否变好？
```

---

# 四十四、Coding Manual 单变量模板

所有 construct 使用同一格式：

```text
Variable:
Unit of Annotation:

Definition:

Question to Annotator:

Allowed Labels:

Use When:

Do NOT Infer From:

UNKNOWN / NO_EVIDENCE Rule:

N/A Rule:

Common Confusion With:

Positive Anchor:

Counterexample:

Minimal-Pair Example:
```

理论推导保留在 02/03/05 模型文档。

Coding Manual 只负责：

```text
怎么判
```

---

# 四十五、Scenario Validity

所有 Module 共用一个独立 Scenario QA 层：

```text
scenario_valid:
  YES
  NO
  UNCERTAIN

scenario_plausibility:
  HIGH
  MEDIUM
  LOW

contradiction_present:
  YES
  NO
```

无效 Scenario 不进入正式 representation reliability。

`contradiction_present=YES` 不自动等于无效；某些 Scenario 故意用于测试 conflicting evidence。

---

# 四十六、Generator 与 Annotator 严格分离

Scenario Generator 只生成：

```text
Realistic Scenario Facts
```

禁止输出：

```text
correct annotation
gold label
model pressure
EMA
latent state
```

Generator hidden metadata 与 Annotator input 必须物理分离。

---

# 四十七、Scenario 自动校验

交给 Annotator 前至少检查：

```text
Week1 / Week2 recurring course consistency
course time in allowed timetable unless edge case
no unexplained time overlap
completed task has no positive remaining effort
known_at does not expose future information
weekend structure follows design constraints
partial exposure not double encoded
overdue continuing task keeps same obligation
skipped course creates obligation only with actual future action
hidden metadata not present in annotator view
```

校验失败的 Scenario 不进入 annotation round。

---

# 四十八、Primary Annotator 配置

第一版内部工程验证保留：

```text
AI-A
AI-B
AI-C
Human
```

三个 AI 尽量来自不同 provider/model family。

要求：

```text
same Coding Manual
independent context
do not see other labels
do not see hidden metadata
do not see Gold
scenario order randomized
stable/low-randomness settings
schema-constrained output
```

Human 必须先独立完成，再查看 AI 结果。

说明：

> 该组合可用于项目内部 Representation engineering validation；若论文后续要声称“human inter-rater reliability”，应增加至少两名真正独立、未参与变量设计的人类标注者，不能把 3 AI + 项目作者等价为 human IRR。

---

# 四十九、Jev 定位

旧版提出 Jev atomic judge。

当前第一阶段处理为：

```text
OPTIONAL / DEFERRED SHADOW CODER
```

它不参与：

```text
Manual Ready Gate
Semantic Reliability Gate
Gold Adjudication
Representation Freeze
```

只有 Gold Set 形成后，才允许评估：

```text
Jev vs Gold
```

并按字段决定是否未来用于辅助自动编码。

第一阶段实现不得把 Jev 作为 blocking dependency。

---

# 五十、Calibration Round

先使用：

```text
24 scenarios
```

流程：

```text
Coding Manual v0.1
        ↓
24 Calibration Scenarios
        ↓
AI-A / AI-B / AI-C / Human independent annotation
        ↓
Agreement + Critical Violation Audit
        ↓
Disagreement Review
        ↓
Manual / Schema revision
        ↓
Coding Manual v1.0 candidate
```

Calibration 阶段允许讨论和修改。

Calibration 数据不作为最终 blind reliability 主结果。

---

# 五十一、Blind Validation Round

Manual candidate 暂时冻结。

然后：

```text
72 Main
+
24 Edge/Orthogonal
```

独立标注。

Main Validation 过程中不得边看结果边修改规则。

必须先完成整轮 annotation，再统一分析。

---

# 五十二、Reliability 指标

## Nominal Fields

例如：

```text
event_family
lifecycle
obligation_exists
support_gate
```

报告：

```text
Krippendorff alpha nominal
raw exact agreement
confusion matrix
```

## Ordinal Fields

例如：

```text
D_pot band
U_context
R_pot
M_context
V/G/Rel
Appraisal Evidence Value
Evidence Strength
```

报告：

```text
Krippendorff alpha ordinal
raw agreement
weighted confusion/disagreement
```

## Facts

例如：

```text
timestamps
remaining effort
actual interval
```

报告：

```text
exact agreement when applicable
absolute deviation
range mismatch
```

---

# 五十三、内部 Reliability Target

可继续使用旧版内部目标：

```text
alpha >= 0.80
→ strong candidate

0.67 <= alpha < 0.80
→ review required

alpha < 0.67
→ definition/manual/schema must be reconsidered
```

该阈值只作为项目内部质量目标。

不得把单一 alpha 阈值当作唯一 Freeze Gate。

同时必须看：

```text
raw agreement
UNKNOWN / NO_EVIDENCE rate
confusion pattern
critical invariant violations
adjudication result
```

---

# 五十四、项目专属 Critical Violation Metrics

必须单独统计：

```text
UnsupportedAppraisalInferenceRate
LayerLeakageRate
FutureKnowledgeLeakageRate
ExposureDoubleEncodingViolationRate
ParentChildObligationDoubleCountRate
FreeTimeAsRecoveryErrorRate
MissedCourseAutomaticObligationErrorRate
TaskHelpAsSupportErrorRate
PersonalizationAsEffectErrorRate
```

这些指标对 Representation Freeze 的优先级高于“总平均 agreement”。

---

# 五十五、Orthogonality Analysis

对 hidden minimal pair：

```text
Variant A
Variant B
```

根据：

```text
manipulated_factor
expected_sensitive_constructs
expected_invariant_constructs
```

自动计算：

```text
expected change?
unexpected spillover?
```

例如：

```text
U_context changed
but no new personal evidence
```

若 Annotator 同时改变：

```text
U_perc
```

记录：

```text
PotentialOrthogonalityViolation
```

它首先触发 Manual/construct review，而不是自动判 Annotator 错误。

---

# 五十六、Disagreement 分类

## Major

例如：

```text
ordinal 最大差距 >= 2 levels
Human 与三个 AI 全部方向冲突
NO_EVIDENCE vs strong directional evidence
Event family/lifecycle fundamental conflict
future leakage
double encoding
```

## Minor

例如：

```text
adjacent ordinal difference
且 evidence interpretation 基本相同
```

Major 必须进入 adjudication queue。

---

# 五十七、Adjudication

禁止：

```text
MajorityVote = Truth
```

流程：

```text
Independent Labels
        ↓
Disagreement Cluster
        ↓
Review Evidence Spans
        ↓
判断原因：

Annotator mistake?
Scenario ambiguous?
Manual ambiguous?
Construct overlap?
Label system too fine?
        ↓
KEEP / REVISE / SIMPLIFY / DROP
```

Gold 保存：

```text
gold_label
adjudication_reason
manual_version
scenario_version
adjudicated_by
```

---

# 五十八、Reference Set

分两类：

## Anchor Reference

Scenario 本身设计为明确边界，可在 calibration 中有 intended reference。

## Adjudicated Reference

正式 Validation Scenario 必须经过：

```text
independent annotation
+
adjudication
```

以后才成为：

```text
Reference / Gold Set
```

Generator proposed label 不得自动成为 Gold。

---

# 五十九、Representation Gate

按 construct 独立判断。

例如：

```text
Event Family            PASS
Lifecycle               PASS
Obligation              PASS
D_pot                    PASS
U_context                NEED REVISION
C_exec Evidence          PASS
C_out Evidence           NEED REVISION
Bot Support Gate         PASS
Relevance                PASS
```

不得用：

```text
一个 overall score
```

宣布整个 Representation Layer 全部通过。

---

# 六十、三个正式 Gate

## Gate A：Manual Ready

Calibration 后要求：

```text
核心定义无明显冲突
每个关键 construct 有正例/反例
UNKNOWN/N/A/NO_EVIDENCE 规则可执行
Schema 能稳定校验
Critical boundary 有 scenario coverage
```

通过后才能进入 Blind Validation。

## Gate B：Semantic Reliability

Blind Validation 后要求：

```text
field-level agreement acceptable
critical confusion rate low
future leakage 接近零
double encoding 接近零
cross-layer unsupported inference 接近零
```

有问题的变量进入 revision。

## Gate C：Representation Semantics Freeze

完成：

```text
adjudication
manual revision
必要字段 re-annotation
```

后生成：

```text
Representation Semantics v1.0
Coding Manual v1.0
Scenario Bank v1.0
Adjudicated Reference Set v1.0
Freeze Manifest
```

然后才进入 Event/Appraisal/Bot Representation 代码实现。

---

# 六十一、推荐目录结构

第一阶段新增独立 research package：

```text
research/
└── scenario_annotation/
    ├── README.md
    │
    ├── manuals/
    │   ├── coding_manual_v0.1.md
    │   └── coding_manual_v1.0.md
    │
    ├── schemas/
    │   ├── scenario.schema.json
    │   ├── event_annotation.schema.json
    │   ├── appraisal_annotation.schema.json
    │   └── bot_annotation.schema.json
    │
    ├── scenarios/
    │   ├── calibration.jsonl
    │   ├── main.jsonl
    │   └── edge.jsonl
    │
    ├── hidden/
    │   ├── coverage_tags.jsonl
    │   ├── pair_design.jsonl
    │   └── anchor_reference.jsonl
    │
    ├── assignments/
    │   ├── round_calibration/
    │   └── round_validation/
    │
    ├── annotations/
    │   ├── calibration/
    │   └── validation/
    │
    ├── adjudication/
    │   ├── disagreement_queue.jsonl
    │   ├── adjudication_log.jsonl
    │   └── gold.jsonl
    │
    ├── analysis/
    │   ├── agreement.py
    │   ├── confusion.py
    │   ├── critical_violations.py
    │   ├── orthogonality.py
    │   ├── disagreement.py
    │   └── report.py
    │
    ├── manifests/
    │   └── representation_semantics_v1.0.json
    │
    └── tests/
        ├── test_scenario_schema.py
        ├── test_annotation_schema.py
        ├── test_hidden_metadata.py
        ├── test_assignment_blinding.py
        ├── test_known_at.py
        ├── test_minimal_pairs.py
        └── test_metrics.py
```

若仓库后续 12 正式重排 package，可移动目录，但本阶段不要依赖 Online Runtime。

---

# 六十二、Scenario Schema

建议 `scenario.schema.json` 至少表达：

```yaml
scenario_id:
scenario_version:

pack_id:
participant_id:

presentation_mode:
  NATURAL
  STRUCTURED

annotation_time:
known_at_cutoff:

participant_context:

recurring_course_context:

recent_context:

current_tasks:

focal_window:

observed_conversation_evidence:

bot_response_units:

source_refs:
```

Annotator-visible Scenario 文件禁止包含：

```text
coverage_tags
manipulated_factor
expected labels
expected sensitive constructs
expected invariant constructs
generator rationale
```

---

# 六十三、Hidden Pair Schema

`pair_design.jsonl` 示例：

```json
{
  "pair_id": "APP_U_004",
  "scenario_ids": ["APP_U_004_A", "APP_U_004_B"],
  "manipulated_factor": "structural_uncertainty",
  "expected_sensitive_constructs": ["U_context"],
  "expected_invariant_constructs": [
    "U_perc_without_new_personal_evidence",
    "C_exec",
    "importance"
  ],
  "notes": "Tests U_context vs U_perc boundary"
}
```

---

# 六十四、Module A Annotation Schema

逻辑字段：

```text
scenario_id
target_event_ref

event_family
event_subtype

event_facts

lifecycle

obligation_exists
obligation_status
parent_relation
active_leaf
remaining_effort

D_pot_band
U_context_band
D_s_structure
R_pot_band
M_context

execution_exposure
deadline_exposure
uncertainty_exposure
social_exposure
recovery_occurrence
partial_encoding_basis

evidence_refs

annotator_confidence
notes
```

---

# 六十五、Module B Annotation Schema

每个 appraisal dimension 一条 record：

```text
scenario_id
participant_event_ref

dimension

evidence_value
evidence_strength
scope

evidence_span
evidence_ref

ambiguity_flag
unknown_reason

annotator_confidence
notes
```

不要在一个 record 中一次塞五维 posterior。

---

# 六十六、Module C Annotation Schema

逻辑字段：

```text
scenario_id
response_unit_ref

origin
role
support_gate

validation
guidance
relevance

personalization_metadata

seen_status

evidence_refs

annotator_confidence
notes
```

不包含：

```text
Q_BS
support_effect
stress_reduction
helpfulness_as_truth
```

---

# 六十七、实现脚本

至少提供：

```text
validate-scenarios
build-assignments
validate-annotations
analyze-agreement
analyze-critical-violations
analyze-orthogonality
build-disagreement-queue
build-report
freeze-representation
```

CLI 可以使用现有项目风格实现。

推荐调用形式：

```bash
python3 -m research.scenario_annotation.cli validate-scenarios
```

```bash
python3 -m research.scenario_annotation.cli build-assignments \
  --round calibration
```

```bash
python3 -m research.scenario_annotation.cli validate-annotations \
  --round calibration
```

```bash
python3 -m research.scenario_annotation.cli analyze \
  --round calibration
```

```bash
python3 -m research.scenario_annotation.cli build-report \
  --round calibration
```

Freeze 时：

```bash
python3 -m research.scenario_annotation.cli freeze-representation \
  --manual-version 1.0 \
  --scenario-version 1.0
```

具体 CLI 名可按仓库命名习惯调整，但职责必须保持分离。

---

# 六十八、Assignment Builder

Assignment Builder 负责：

```text
randomize scenario order
prevent Natural/Structured pair seen by same annotator in same round
strip hidden metadata
bind manual_version
bind scenario_version
produce annotator-specific input
```

不得：

```text
generate annotation
inject gold labels
```

---

# 六十九、Schema Validation

所有：

```text
Scenario
Annotation
Adjudication
Freeze Manifest
```

必须在进入分析前通过 schema validation。

无效 label：

```text
直接 reject
```

不能静默纠正。

例如：

```text
C_EXEC = "VERY_HIGH"
```

若 schema 不允许：

```text
validation failure
```

---

# 七十、Agreement Analysis 输出

`field_metrics.csv` 至少包含：

```text
field
module

n_valid
n_unknown
unknown_rate

raw_agreement
krippendorff_alpha
alpha_type

major_disagreement_rate
minor_disagreement_rate

human_vs_ai_a
human_vs_ai_b
human_vs_ai_c

freeze_status
```

对 facts 类型字段使用适合的 deviation metric，而不是硬套 alpha。

---

# 七十一、Critical Violation Report

`critical_violations.jsonl` 每条包含：

```text
scenario_id
annotator_id
violation_type
affected_fields
evidence
manual_version
severity
```

优先级：

```text
future leakage
double exposure encoding
unsupported appraisal inference
parent-child obligation double count
free-time-as-recovery
task-help-as-support
```

---

# 七十二、Disagreement Report

必须按字段聚类，而不是只列全部不一致案例：

```text
U_context
  common confusion:
    U_perc leakage

C_exec
  common confusion:
    objective difficulty used as appraisal

F_rec
  common confusion:
    preference used as recovery fit

Lifecycle
  common confusion:
    SCHEDULED interpreted as ATTENDED
```

该报告直接决定 Manual 修订。

---

# 七十三、Freeze Manifest

`representation_semantics_v1.0.json` 至少记录：

```text
representation_version
frozen_at

manual_version
scenario_bank_version
gold_set_version

schema_versions

included_constructs
revised_constructs
simplified_constructs
dropped_constructs

field_metrics_refs
critical_violation_report_ref

known_open_questions

git_revision
```

Freeze 后任何语义变更必须创建：

```text
Representation Semantics v1.1 / v2
```

不能静默修改 v1.0。

---

# 七十四、测试要求

## Schema Tests

验证所有 Scenario / Annotation / Gold 文件。

## Hidden Metadata Leak Test

Annotator assignment 中不得出现：

```text
coverage_tags
pair metadata
expected labels
```

## Natural/Structured Pair Test

同一 annotator / round 不得看到同一 pair 的两种表达。

## Known-at Test

Annotator-visible context 中不得出现：

```text
known_at > cutoff
```

的信息。

## Exposure Rule Test

不得同时：

```text
actual interval encodes partial duration
+
fractional exposure encodes same partial fact
```

## Obligation Test

Parent + child active 时不允许默认都计为 active leaves。

## Metrics Unit Test

用小型人工 fixture 验证 alpha/confusion/violation 计算。

---

# 七十五、第一阶段 Codex 实施顺序

## Part 1：建立 Package 与 Schema

创建：

```text
research/scenario_annotation/
```

完成：

```text
schemas
typed loader
validator
CLI skeleton
tests
```

不生成大规模 Scenario。

验收：

```text
valid fixture PASS
invalid fixture FAIL with clear reason
hidden metadata cannot enter annotator view
```

## Part 2：Coding Manual v0.1

把本文 Module A/B/C 规则整理成可操作 Manual。

要求：

```text
每个变量都有：
definition
question
labels
allowed evidence
forbidden inference
unknown rule
N/A rule
anchor
counterexample
minimal pair
```

## Part 3：24 Calibration Scenarios

优先覆盖：

```text
Scheduled vs Realized
Course Partial two encoding paths
Task Open vs In Progress
Blocked Task
Overdue continuing obligation
Skipped course with/without catch-up
Free time vs Recovery
Recovery occurrence vs F_rec
Difficulty vs C_exec
Importance prior vs evidence
C_exec vs C_out
U_context vs U_perc 2x2
Bot task-help vs support
Personalization vs support quality
known_at future leakage
```

## Part 4：Assignment Pipeline

生成四套独立 annotation input：

```text
AI-A
AI-B
AI-C
Human
```

保证 blindness 和 randomization。

## Part 5：Calibration Annotation + Analysis

完成：

```text
annotation validation
agreement
critical violations
orthogonality
disagreement report
```

只输出报告，不自动修改 Manual。

## Part 6：Manual Revision

人工根据 disagreement：

```text
KEEP
REVISE
SIMPLIFY
DROP
```

修改 Manual/Schema。

必要时重新标 Calibration 子集。

## Part 7：Manual Ready Gate

通过后生成：

```text
Coding Manual v1.0 candidate
```

## Part 8：96 Formal Scenarios

生成：

```text
72 Main
24 Edge
```

并完成自动 QA。

## Part 9：Blind Validation

四方独立完成正式 annotation。

## Part 10：Adjudication + Freeze

生成：

```text
Gold Set
Field Metrics
Representation Revision Log
Freeze Manifest
Representation Semantics v1.0
```

---

# 七十六、Calibration 24 场景最低覆盖

24 个 Calibration 不需要平均分配，而应集中攻击高风险边界。

建议最低覆盖：

```text
Course/Lifecycle/Exposure        6
Task/Obligation                  5
Recovery                         4
Appraisal                        6
Bot Support                      3
```

部分 Scenario 可覆盖多个边界。

其中必须包含 minimal pair / contrast：

```text
actual interval vs fraction-only PARTIAL
skipped course no obligation vs catch-up obligation
free time vs explicit recovery activity
recovery activity vs recovery fit evidence
difficulty same / C_exec different
U_context high/U_perc low vs U_context low/U_perc high
task assistance vs supportive coping response
known-now vs known-later cancellation
```

---

# 七十七、正式 96 场景 Coverage

Main Set 负责代表性：

```text
normal low-load weekday
course-dense day
low-course/high-task
weekend sparse calendar
research/self-study day
large workload/far deadline
small workload/near deadline
normal sleep/recovery
common Bot interactions
```

Edge Set 负责：

```text
extreme leisure
extreme load
partial attendance
conflicting evidence
late-known correction
parent-child tasks
overdue continuation
poor recovery context
rare lifecycle transitions
cross-layer traps
minimal-pair orthogonality
```

---

# 七十八、报告模板

最终 `scenario_annotation_report.md` 按 construct 输出：

```text
Construct:
Status:
  KEEP / REVISE / SIMPLIFY / DROP

Definition Version:

N:
Unknown/No-Evidence Rate:

Agreement:
  alpha:
  raw agreement:

Critical Violations:

Common Confusions:

Minimal-Pair Orthogonality:

Adjudication Summary:

Decision:

Open Questions:
```

禁止只写：

```text
“整体一致性较好”
```

---

# 七十九、与后续 Stage 的接口

本阶段通过后，下一阶段只允许使用 Freeze Manifest 中：

```text
KEEP
SIMPLIFY
```

后的 construct 建立 Event V2 / Appraisal / Bot Representation code。

流程：

```text
Representation Semantics v1.0
        ↓
Typed Event / Obligation / Lifecycle / Exposure Contracts
        ↓
Shadow Representation
        ↓
Synthetic
        ↓
Numeric Representation Freeze
        ↓
Latent Dynamics
```

任何 `REVISE / DROP` construct 不得未经处理直接进入 Scientific Core。

---

# 八十、当前明确禁止的实现

第一阶段不要：

```text
接生产 PostgreSQL
修改 CTSSM
修改 ForecastCoordinator
建立 Web Annotation Platform
建立新的 Agent Runtime
把 Jev 设成唯一 judge
让 Scenario Generator 输出 Gold
让同一个模型自己生成场景再自己生成 Gold
让 Annotator 输出 A/B/S/EMA
让 Appraisal 用 Event metadata 直接补值
让 Support Coding 查看 future outcome
用 overall accuracy 代替 field-level analysis
用 majority vote 直接形成 Gold
```

---

# 八十一、第一阶段完成定义

只有以下产物齐全，第一阶段才算闭合：

```text
1. Coding Manual v1.0
2. Annotation Schema v1.0
3. Scenario Bank v1.0
4. 24 Calibration + 96 Formal Scenarios
5. Independent Annotation Records
6. Field-Level Reliability Report
7. Critical Violation Report
8. Orthogonality Report
9. Disagreement / Adjudication Log
10. Adjudicated Reference Set v1.0
11. Representation Revision Log
12. Representation Semantics v1.0 Freeze Manifest
```

完成后才能进入：

```text
Event / Obligation / Lifecycle / Exposure V2 正式代码实现
```

以及后续：

```text
Synthetic
Numeric Mapping
Latent Dynamics
Pilot
```
