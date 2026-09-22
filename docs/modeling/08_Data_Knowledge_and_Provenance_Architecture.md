# MindFlow 08：Data, Knowledge and Provenance Architecture
>
> 本文件定义的是 **Source → Evidence → Knowledge → Snapshot** 的语义与可追溯性边界，不要求每个概念都映射成独立表、独立 Service 或独立进程。

---

## 1. 设计目标

MindFlow 的压力模型、Agent、个性化、Forecast 与研究评估都依赖同一批用户数据，但它们不能直接共享一份“万能当前状态”。

本层要解决：

- 原始输入是什么；
- 什么信息可以升级为正式 Evidence；
- Evidence 如何形成当前可用 Knowledge；
- correction / contradiction / unknown 如何表达；
- 系统在某一时刻“合法知道什么”；
- Forecast / Policy / Replay 如何冻结可复现输入；
- LLM 能做哪些结构化抽取，不能直接写哪些状态；
- Production 与 Synthetic 如何在共享 Contract 的同时保持硬隔离。

核心原则：

```text
Evidence ≠ Knowledge
Knowledge ≠ Latent State
Raw Context ≠ Structured Evidence
```

以及：

```text
Append Evidence
→ Recompute Derived State
```

但 MindFlow **不采用完整 Event Sourcing**。

---

## 2. 总体数据流

```text
Raw Source / Tool Observation / Structured Input
                    ↓
            EvidenceCandidate
                    ↓
         Validation / Domain Binding
                    ↓
                 Evidence
                    ↓
          Domain Resolver / Rules
                    ↓
          Resolved Domain Knowledge
                    ↓
        Current Domain Projection(s)
                    ↓
      Context / Model Input Construction
                    ↓
      Forecast / Policy / Conversation
```

并行存在：

```text
Evidence Ledger
      ↓
resolve_as_of(knowledge_cutoff)
      ↓
Historical Knowledge View
      ↓
Replay / Research
```

重要的是：

> 在线当前读取与历史 as-of 重建共享同一 Evidence / Resolver 语义，但不要求共享同一种物理查询实现。

---

## 3. Evidence-first，而不是 Current-state-first

系统不能只保存：

```text
task.progress = 80%
```

而应能够回答：

```text
谁报告的？
什么时候发生？
什么时候系统知道？
这是不是后续 correction？
当前结论为什么是 80%？
```

因此采用：

```text
AppendOnlyEvidence + RecomputableDerivedState
```

其中：

### Evidence Ledger

保存历史证据与 provenance。

### Current Domain Projection

保存在线读取所需的当前 resolved state。

它是**逻辑 read model**，不要求实现为一张统一 `current_knowledge` mega table。

Event、Task、Recovery、Appraisal、Preference 等 Domain 可以维护自己的 current projection。

### Context Snapshot

关键 Forecast / Policy / Replay 冻结当时真正消费的 resolved input 与 provenance。

---

## 4. Source、Candidate、Proposal、Evidence、Knowledge

### 4.1 Source Artifact

Source 是系统实际接收到的输入或外部观测，例如：

```text
user message
calendar import
EMA response
card submit
uploaded image / document
web / URL result
structured form
```

Source 可以包含自然语言，也可以完全结构化。

Source 是“发生过的输入事实”，但其内容不自动等于可信 Domain Knowledge。

---

### 4.2 EvidenceCandidate

EvidenceCandidate 表示：

> 某个 Source 可能支持某个 Claim，但尚未经过完整 Domain validation / subject binding。

例如用户上传课程表截图：

```text
Image Understanding
→ candidate: event.start_time = 10:00
```

Candidate V1 默认是 request-level DTO，不要求持久化。

只有在以下情况才考虑持久化 Candidate：

- 跨会话确认；
- 人工 review；
- 高风险歧义；
- 需要独立审计的 extraction pipeline。

---

### 4.3 ActionProposal

Proposal 与 EvidenceCandidate 必须分离。

```text
EvidenceCandidate = “世界可能是什么样”
ActionProposal     = “系统准备做什么”
```

例如：

```text
“把任务延期到周五”
```

可能同时形成：

```text
EvidenceCandidate:
用户认为新的 deadline 应是 Friday

ActionProposal:
将 Task Domain deadline 修改为 Friday
```

08 只定义二者语义不同。

Proposal 的确认、mutation authority 与执行生命周期由 09 / 11 定义，不在本层建设独立 Proposal 平台。

---

### 4.4 Evidence

Evidence 是：

> 已经过 Domain validation、具备明确 Claim Address、时间语义与 provenance 的正式证据。

Evidence 不是绝对真相。

它只是：

> “在某个时间，某个来源以某种方式提供了支持某 Claim 的信息。”

例如：

```text
user explicitly reports:
task_123.progress = 80%
```

是 Evidence。

但系统当前是否应该认定 progress=80%，仍由 Resolver 决定。

---

### 4.5 Derived Knowledge

Derived Knowledge 是 Resolver 根据 eligible Evidence 得出的当前或 historical conclusion。

例如：

```text
RESOLVED = 80%
UNKNOWN
CONFLICTED
```

Knowledge 本身仍不等于 latent stress state。

因此：

```text
Evidence ≠ Knowledge ≠ A/B/S
```

---

## 5. Typed Claim Architecture

Evidence 不能退化为：

```text
type + arbitrary JSONB
```

每条 Evidence 必须回答：

> 它到底在支持哪个问题？

因此使用 Typed Claim Address。

至少包括：

```text
domain
subject_type
subject_id
attribute
valid_time_scope
```

例如：

```text
event / event_123 / scheduled_state
```

与：

```text
event / event_123 / lifecycle_state
```

是不同 Claim。

所以：

```text
Calendar says SCHEDULED
User says SKIPPED
```

不一定冲突。

因为：

$$
Scheduled \neq Occurred \neq EffectiveExposure
$$

---

## 6. Claim Definition，而不是 God Registry

所有允许进入 Evidence Layer 的 Claim Type 应由对应 Domain owner 定义。

V1 推荐使用代码级 typed definitions / schemas，例如：

```text
ClaimDefinition
├── claim_type
├── domain_owner
├── value_schema
├── subject_schema
├── valid_time_semantics
├── allowed_source_classes
└── allowed_extractor_classes
```

重点是：

> 不允许 Bot / Runtime 临时创造任意心理字段或自由字符串 Claim。

但不建立一个承担所有职责的万能 `ClaimRegistry`。

以下内容由各自架构负责：

```text
Context exposure       → 09
Model impact           → ModelContext / Domain change semantics
Policy exposure        → 09 / 10
Research export        → 10
Retention / deletion   → data-governance rules
Admin visibility       → 11
```

即：

```text
Claim Definition
≠ Context Policy
≠ Privacy Policy
≠ Forecast Dependency Registry
```

工程上可以共享 metadata，但 ownership 必须分开。

---

## 7. 双时间轴：State Time 与 Knowledge Time

MindFlow 必须区分：

```text
事件什么时候发生？
```

与：

```text
系统什么时候知道？
```

至少使用以下时间语义。

### 7.1 `event_time`

事实在现实世界中的发生时间或有效时间。

例如：

```text
昨天 10:00 上课
```

### 7.2 `observed_at`

某传感器 / 平台 / 外部系统实际观察到该信息的时间。

并非所有 Evidence 都需要。

### 7.3 `reported_at`

用户或 source 对系统报告信息的时间。

### 7.4 `known_at`

这是最重要的字段之一。

定义为：

> 该 Structured Evidence 第一次合法进入下游 information set 的时间。

它不是简单等于：

```text
raw message arrival
```

例如：

```text
15:00:00 user message arrives
15:00:04 extraction + validation commits Evidence
```

可以：

```text
reported_at = 15:00:00
known_at    = 15:00:04
```

正式 prospective information set：

$$
\mathcal I(t_0)=\{e: known\_at(e)\le t_0\}
$$

### 7.5 `recorded_at`

数据库持久化时间。

它主要服务工程审计，不替代 `known_at`。

---

## 8. `known_at` 不限制 Agent 理解当前 Raw Message

`known_at` 约束的是：

```text
Structured Knowledge
Modeling
Policy
Research Replay
```

不是：

> “Evidence 还没 commit，所以 Conversation Agent 不能看用户刚说的话。”

Reactive Conversation 可以：

```text
Current Raw Source
↓
Conversation Agent
```

并行：

```text
Current Raw Source
↓
Evidence Pipeline
↓
known_at
```

只有当当前操作依赖最新 structured state，例如：

```text
“考试取消了，重新算一下压力。”
```

才需要等待 relevant Evidence / Resolver 完成。

该 Runtime 机制在 09 / 10 中定义为：

```text
Relevant Canonical State Barrier
```

---

## 9. Prospective 与 Retrospective 查询

### Prospective Known State

回答：

> 在历史时间 $t_0$，系统当时合法知道什么？

使用：

```text
known_at <= t0
```

主要服务：

```text
prospective forecast evaluation
policy replay
experiment audit
```

### Retrospective Best Estimate

回答：

> 使用后来出现的 correction / clarification 后，对历史事实当前最合理的重建是什么？

两者必须分开：

```text
ProspectiveKnownState
≠
RetrospectiveBestEstimate
```

不能用今天知道的信息证明过去预测“本来应该知道”。

---

## 10. Correction、Retraction、Contradiction

历史 Evidence 正常情况下不覆盖。

使用关系：

```text
SUPERSEDES
RETRACTS
CONTRADICTS
```

### SUPERSEDES

新 Evidence 替代旧陈述的当前有效性。

### RETRACTS

来源明确撤回原陈述。

### CONTRADICTS

两条 Evidence 同时存在但互相冲突。

Resolver 决定当前 Knowledge 是否：

```text
RESOLVED
UNKNOWN
CONFLICTED
```

而不是数据库 `UPDATE old_evidence`。

---

## 11. 不建立全局 Evidence Priority

不同 Domain 的 evidence authority 不同。

例如：

```text
Task completion
```

可能以用户显式确认优先。

而：

```text
scheduled class time
```

可能以正式 timetable 更权威。

因此：

> Evidence priority / conflict semantics 必须由 Domain Resolver 定义。

不建立：

```text
USER > CALENDAR > LLM > RULE
```

这种全局排序。

---

## 12. Knowledge State 使用正交维度

Knowledge 不使用一个模糊的 `confidence` 代替所有语义。

至少拆成：

### Resolution State

```text
RESOLVED
UNKNOWN
CONFLICTED
```

### Epistemic Basis

```text
OBSERVED
INFERRED
PRIOR
MIXED
```

### Freshness

```text
CURRENT
STALE
```

这些维度互相独立。

例如：

```text
RESOLVED + INFERRED + STALE
```

是合法组合。

---

## 13. UNKNOWN 是正式状态

禁止：

```text
None → MEDIUM
```

例如不知道 `U_perc` 时：

```text
UNKNOWN
```

而不是：

```text
MEDIUM
```

Unknown 应由模型通过：

```text
prior
belief distribution
uncertainty propagation
```

处理，而不是人为制造中间值。

---

## 14. Freshness 由 Domain 定义

不存在全局：

```text
EVIDENCE_TTL = 7 days
```

例如：

```text
explicit response preference
```

可能长期有效，直到用户修改。

而：

```text
current availability
```

可能几分钟后就 stale。

所以 freshness semantics 属于 Domain Definition / Resolver。

---

## 15. Resolver Contract

Resolver 应尽可能保持 pure / deterministic / versioned。

概念接口：

```text
resolve(
    claim_address,
    eligible_evidence,
    knowledge_cutoff,
    resolver_version
)
→ DerivedKnowledge
```

Resolver 不应该：

```text
调用 LLM
读取 Forecast
读取 latent stress
发送 Bot message
改 ModelParameters
```

同一输入 + 同一 Resolver version 应产生同一结果。

---

## 16. LLM 必须在 Resolver 之前

自然语言处理路径：

```text
Raw Source
↓
Restricted Extraction
↓
EvidenceCandidate
↓
Validation
↓
Evidence
↓
Resolver
```

禁止：

```text
Resolver
↓
LLM 猜一个当前真值
```

这样 historical replay 才可能稳定。

---

## 17. Confidence 语义分离

### Extraction Confidence

表示：

> Extractor 对“文本是否支持这个 Claim”的把握。

### Belief Distribution

表示：

> 模型 / Resolver 对 unknown psychological quantity 的 belief。

### Resolver Confidence

若未来确有需要，可表达 resolver-level uncertainty。

三者不能用一个 `confidence` 字段混合。

V1 对语言 Evidence 可以进一步简化为：

```text
EXPLICIT
STRONG_INFERENCE
NO_EVIDENCE
```

避免伪精度。

---

## 18. Evidence 幂等性

同一 source fragment 经 retry / worker redelivery 不能生成重复 Evidence。

幂等 identity 至少应能关联：

```text
source
extractor / parser version
claim address
source fragment
```

实现细节在 11 中确定。

---

## 19. Current Projection 与 Historical Resolution

在线读取：

```text
New Evidence
↓
Incremental Resolution
↓
Update relevant Domain Projection
```

历史读取：

```text
Evidence Ledger
↓
resolve_as_of(...)
↓
Historical Knowledge
```

重要修正：

> `CurrentKnowledgeProjection` 是逻辑概念，不要求一个全局 mega table / mega service。

各 Domain 可以拥有独立 projection，但都必须遵循同一 Evidence / Resolver 语义。

未来 golden test 应验证：

```text
Current Projection
≈
Full recomputation at current knowledge cutoff
```

---

## 20. Domain Change Notification

Evidence Layer 不直接调用：

```text
Forecast
Bot
Policy
Admin
Research
```

Knowledge resolution 后可产生轻量变化通知：

```text
KnowledgeChanged(
    participant_id,
    claim_family,
    affected_time_range,
    knowledge_version,
    reason
)
```

其职责只是告诉 Application：

> 哪一类 canonical knowledge 发生了变化，影响哪个 state-time 范围。

Application 再决定：

```text
重算 current derived state
生成新 Forecast
刷新相关 context
标记旧 current forecast 为 stale-for-runtime
```

关键修正：

> 变化通知不能删除或修改历史 Forecast Artifact。

应该说：

```text
supersede for current operational use
```

而不是：

```text
invalidate historical artifact
```

---

## 21. Context Snapshot

Forecast / Policy / Replay 的关键决定不能依赖未来 live tables 去“猜当时用了什么”。

因此使用 immutable purpose-scoped Snapshot。

概念字段：

```text
snapshot_id
participant_id
purpose
reference_time
knowledge_cutoff
resolved inputs actually consumed
provenance / evidence manifest
resolver versions
snapshot_schema_version
snapshot_hash
```

`purpose` 至少支持：

```text
FORECAST
POLICY
REPLAY
SYNTHETIC
```

Snapshot 只冻结**该 consumer 当时实际消费的 resolved context**。

不做 DB dump。

不默认包含：

```text
unrelated memory
admin-only fields
research gold labels
hidden experiment assignment
raw model internals irrelevant to the decision
```

---

## 22. Snapshot 与消费 Artifact 的版本责任

原版将：

```text
representation version
parameter version
policy version
```

全部塞入 Snapshot，容易把 Snapshot 变成万能 provenance container。

修订后：

```text
Snapshot
→ 冻结 Context / Knowledge provenance
```

而：

```text
ForecastRun
→ 保存 ModelSpec / Representation / Parameter / Runtime release identity

PolicyEvaluation
→ 保存 Policy version / Forecast ref / Policy snapshot ref
```

这样责任更清楚。

复现条件应表达为：

```text
same snapshot
+ same scientific spec
+ same parameter/prior state
+ same explicit randomness if used
→ same pure computation result
```

---

## 23. Snapshot 不可变

如果：

```text
Forecast F1 → Snapshot C1
```

后续用户 correction：

```text
C1 不修改
```

产生：

```text
new Knowledge
new Snapshot C2
new Forecast F2
```

F1 / C1 保留，用于 prospective audit。

---

# 24. 自然语言抽取总原则

自然语言只是一个 observation channel。

它不是：

```text
模型自由输入口
latent-state write API
万能长期记忆写入口
```

MindFlow 使用 Hybrid Extraction。

---

## 25. Hybrid Extraction Strategy

### L0：Structured Source

例如：

```text
EMA form
calendar
confirmed card
structured task edit
```

可以直接形成 validated Evidence，不需要 LLM 重解释。

### L1：Deterministic Parsing

用于：

```text
time
date
percentage
duration
number
fixed enum
```

### L2：Restricted LLM Structured Extraction

用于：

```text
semantic classification
entity grounding
appraisal evidence
recovery semantics
```

必须输出严格 schema。

### L3：Clarification

当：

```text
subject ambiguous
high-impact persistent change ambiguous
mutation target ambiguous
```

需要用户澄清。

### L4：Longitudinal Inference

例如：

```text
StableProfile
ModelParameters
slow personalization patterns
```

由独立纵向机制产生，不由单条消息直接形成。

---

## 26. Structured Source 优先

结构化来源已有明确语义时，不再调用 LLM“解释一次”。

例如：

```text
EMA slider = 8
```

不需要 LLM 判断：

```text
“用户可能压力很高。”
```

应直接形成 Measurement Evidence。

---

## 27. Regex / Parser 的边界

Regex / Parser 适合：

```text
日期
时间
数字
duration
percentage
固定格式 identifier
```

不适合：

```text
importance
controllability
uncertainty
recovery effectiveness
psychological appraisal
```

禁止：

```text
keyword → psychology
```

例如：

```text
“考试” → HIGH uncertainty
```

是错误规则。

---

## 28. LLM 的核心任务

LLM Extraction 主要承担：

```text
Entity Grounding
Semantic Classification
Structured Evidence Extraction
```

而不是：

```text
直接决定 latent state
直接决定 stable profile
直接决定 active model parameters
```

---

## 29. `NO_EVIDENCE`

Extraction schema 必须允许：

```text
NO_EVIDENCE
```

未提及的字段保持 missing。

禁止为了“schema 完整”补：

```text
MEDIUM
0.5
normal
```

---

## 30. Semantic Evidence 与 Numerical Representation 分离

例如语言：

```text
“这个考试对我挺重要的。”
```

可以形成：

```text
importance = HIGH
```

但不直接形成：

```text
importance = 0.73
```

Semantic Evidence → Numerical Representation 的映射属于科学模型层，而不是 Extraction。

---

# 31. Claim Extraction Authority

## 31.1 Event

可以抽取：

```text
identity
start / end
scheduled state
lifecycle evidence
semantic class
```

实际执行程度需要谨慎，不能从“有安排”直接推出 occurred。

---

## 31.2 Task / Obligation

可以抽取：

```text
deadline
estimated duration
remaining duration
progress
completion state
remaining work
```

“任务很难”不能自动成为 objective task property。

主观 difficulty / controllability 等应进入 Appraisal 语义。

---

## 31.3 Recovery / Sleep

必须区分：

```text
planned recovery
actual recovery
recovery characteristics
recovery evaluation
```

例如：

```text
“今晚想早点睡”
```

不是 actual sleep Evidence。

LLM 负责识别语义，确定性时间计算交给 deterministic code。

---

## 31.4 Appraisal

当前核心 Appraisal 变量：

$$
C^{exec},\ I,\ C^{out},\ U^{perc},\ F^{rec}
$$

其定义必须与模型文档保持一致。

- $C^{exec}$：对执行过程的可控性感受；
- $I$：个人重要性；
- $C^{out}$：对结果的可控性感受；
- $U^{perc}$：主观不确定性；
- $F^{rec}$：已经实际发生 Recovery 后的主观恢复有效性。

禁止：

```text
difficulty = importance
difficulty = uncertainty
```

也禁止在 recovery 尚未发生时生成 $F^{rec}$。

---

## 31.5 Preference

用户对交互方式的明确要求可以形成 Preference Evidence。

至少区分：

```text
EPISODIC
DURABLE
```

例如：

```text
“今天少问点问题”
```

更像 EpisodeContext。

而：

```text
“以后晚上十点后别主动提醒我”
```

可能是 Durable Preference。

高影响 persistent preference 可以进入 confirmation / mutation path。

---

## 32. StableProfile 不能由单条对话直接更新

单条 Source 最多产生：

```text
Evidence
SelfDeclaredTendency
Episode-level Appraisal
Preference
```

不能直接写：

```text
StableProfile
```

StableProfile 的纵向聚合规则由 09 定义。

---

## 33. 禁止自然语言直接写入的变量

Conversation / LLM 不得直接写：

```text
A
B
S
Q_D
Q_P
Q_R
Q_BS
C_D
C_U
F
ModelParameters
```

这些必须来自：

```text
Representation
Dynamics
Estimation
Promotion
```

而不是：“用户说压力很大，所以 S=85”。

如果用户给出自评压力：

```text
“我现在大概 8/10”
```

应形成 Measurement / self-report Evidence，而不是 latent state overwrite。

---

## 34. Active Model Parameters 的边界

参数只能通过：

```text
Estimation
↓
Validation
↓
Promotion
```

成为 active parameters。

禁止 Conversation Agent、Extractor、StableProfile 直接修改数学参数。

---

# 35. Evidence Extractor 与 Conversation Agent 分离

即使底层使用同一个 LLM API，也必须逻辑分离。

## Conversation Agent

目标：

```text
理解用户
搜索历史
使用工具
自然回复
```

## Evidence Extractor

目标：

```text
minimum necessary context
strict schema
low creativity
precision-oriented extraction
```

Extractor 默认不能看到：

```text
latent forecast
care eligibility
experiment hidden labels
model conclusions about the same psychological variable
```

防止 self-confirming loop。

---

## 36. Minimum Necessary Extraction Context

Extractor 可以看到：

```text
current source
small necessary nearby message window
entity candidates / aliases
claim definitions
necessary temporal context
```

不默认看到：

```text
full user memory
full stable profile
historical latent trajectory
policy decision
research labels
```

Conversation Agent 的历史读取自由度可以明显高于 Extractor。

---

# 37. Active Information Acquisition 在 08 中的边界

Active Information Acquisition 的“是否问、问什么、interaction burden、QuestionIntent、Agent wording”属于 09。

08 只定义两件事：

### 37.1 Bot-elicited 回答仍然是合法 Evidence Source

但需要保留 elicitation provenance，例如：

```text
SPONTANEOUS
OPEN_QUESTION
GUIDED_QUESTION
DIRECT_CONFIRMATION
FORM_RESPONSE
```

必要时保存：

```text
question_intent_id
```

### 37.2 Elicited Evidence 不因为是 Bot 问出来的就自动失效

它可以进入 Evidence / StableProfile longitudinal input，但后续聚合必须知道：

> 这是用户主动说的，还是被引导后回答的。

08 不再定义：

```text
one-follow-up invariant
question burden budget
QuestionOpportunity algorithm
```

这些由 09 的 PolicySpec / Agent Runtime 管理。

---

## 38. Extraction Calibration

LLM Extraction Specification 与 Human Coding Manual 应尽量共享变量定义。

未来 Calibration 可以比较：

```text
claim precision
claim recall
NO_EVIDENCE agreement
entity grounding accuracy
field-level agreement
false-positive rate
```

当前系统应优先控制 false positive。

因为：

```text
missing claim
→ UNKNOWN
```

通常比：

```text
fake psychological evidence
→ model contamination
```

更安全、更可解释。

---

# 39. 工程复杂度约束

本文件描述逻辑 Contract，不要求：

```text
一概念一表
一概念一 Service
一概念一 Manager
```

V1 可以只使用少量稳定接口：

```text
extract_evidence(...)
validate_evidence(...)
commit_evidence(...)
resolve_current(...)
resolve_as_of(...)
build_context_snapshot(...)
```

只有某对象具有：

```text
独立生命周期
独立权限
跨会话持久状态
独立版本
独立审计需求
```

时才考虑物理拆分。

---

# 40. Evidence、Bot Action、Operational Audit 的边界

### Domain Evidence

记录：

```text
participant / event / task / appraisal / recovery / measurement
```

相关研究与模型证据。

### Bot Runtime Action Records

具体由 10 定义为：

```text
BotResponseUnit
BotActionEvent
PolicyEvaluation
ActionDecision
```

08 不建立独立 Intervention / Exposure 子系统。

重要修正：

> EMA、Recovery、Task Progress 等 outcome 保留在各自 Domain，不复制进所谓 Intervention Ledger。

Exposure / SupportRepresentation 当前主要作为 10 的 Research-side derived semantics。

### Operational Audit

例如：

```text
HTTP retry
DB error
worker failure
runtime health
token usage
```

这些不是 Domain Evidence。

---

# 41. Synthetic Isolation

Synthetic 可以复用：

```text
Claim Contract
Evidence Contract
Resolver
Projection semantics
ModelContextBuilder
Scientific Core
```

但必须使用硬 namespace：

```text
origin = SYNTHETIC
scenario_id
dataset_id
```

并禁止：

```text
Synthetic
→ Production participant projection
```

修订后的原则不是“只能进入 Replay/Evaluation”，而是：

> Synthetic 可以完整走科学管线，但只能在 Synthetic namespace 中运行。

Synthetic Ground Truth 仍必须作为独立 sidecar，不能进入普通 ModelContext。

---

# 42. Privacy、Withdrawal 与 Append-only

Append-only 是正常业务 / 研究 provenance 规则，不凌驾于数据治理要求。

但必须区分：

```text
停止 proactive intervention
停止 measurement
退出 study
撤回 data use authorization
隐私删除
```

它们不是同一件事。

只有 withdrawal scope / data-governance rule 明确要求删除时，才执行：

```text
raw source deletion / redaction
sensitive evidence deletion
sensitive derived-state deletion
snapshot degradation
```

必要时可以标记：

```text
REPRODUCIBILITY_DEGRADED_BY_PRIVACY_DELETION
```

而不是为了 reproducibility 保留本应删除的数据。

具体 Study Withdrawal semantics 由 10 定义。

---

# 43. 本文件最终架构决策

1. 使用 `AppendOnlyEvidence + RecomputableDerivedState`，但不采用完整 Event Sourcing。
2. Source、EvidenceCandidate、ActionProposal、Evidence、DerivedKnowledge 严格分离。
3. Candidate V1 默认 request-level，不为了架构完整性强制持久化。
4. Evidence 使用 Typed Claim Address，不允许 Runtime 临时创造自由心理字段。
5. Claim Type 由 Domain owner 通过 typed ClaimDefinition 管理；不建设承担所有职责的 God Registry。
6. Claim exposure、privacy、research、model impact 等 policy 不全部塞入 ClaimDefinition。
7. `event_time` 与 `known_at` 双时间轴是 prospective correctness 的基础。
8. `known_at` 约束 Structured Knowledge / Model / Policy，不阻止 Agent 理解当前 raw message。
9. Correction / Retraction / Contradiction 追加表达，不正常覆盖旧 Evidence。
10. 不建立全局 Evidence priority；冲突语义由 Domain Resolver 定义。
11. Knowledge 使用 `resolution / epistemic_basis / freshness` 正交表达。
12. `UNKNOWN` 是一等状态，不映射成 MEDIUM。
13. Resolver 尽量 pure、deterministic、versioned，LLM 位于 Resolver 之前。
14. Current Projection 是 Domain read-model 逻辑概念，不要求全局 mega table。
15. Historical Resolution 使用 as-of knowledge cutoff 重建。
16. Knowledge change 只通知 Application 哪些 canonical input 发生变化，不直接调用 Forecast / Agent / Research。
17. Knowledge change 只能使旧 Forecast 对当前运行 stale/superseded，不能修改历史 Artifact。
18. ContextSnapshot 只冻结 consumer 实际使用的 resolved context 与 provenance，不做 DB dump。
19. Scientific / Policy / Runtime 版本主要由消费 Snapshot 的 Artifact 保存，而不是全部塞进 Snapshot。
20. Structured source 优先；Deterministic Parser 处理低歧义字段；LLM 只做 allowlisted semantic extraction。
21. Extraction schema 必须允许 `NO_EVIDENCE`。
22. Semantic Evidence 与 Numerical Representation 分离。
23. Appraisal、Preference、Recovery 等 Claim 的 extraction authority 必须与模型语义一致。
24. StableProfile 不能由单条消息直接更新。
25. A/B/S、Q、C_D/C_U/F、ModelParameters 禁止 Conversation 直接写。
26. Evidence Extractor 与 Conversation Agent 逻辑隔离；Extractor 使用 minimum necessary context。
27. Bot-elicited Evidence 可以合法进入 Evidence，但必须保存 elicitation provenance。
28. Active Question Policy / burden / wording 属于 09，不在 08 重复定义。
29. V1 优先少表、少 Service、少持久化中间态。
30. Bot action / exposure 的 runtime 事实由 10 管理，Outcome 保留在原 Domain。
31. Synthetic 可以走完整科学管线，但必须与 Production namespace 硬隔离。
32. Append-only 不凌驾于明确的数据删除要求；Study withdrawal 与 data deletion 分离。

---

# 44. 与 09 / 10 / 11 / 12 的边界

## 09：Personalization and Bot Context

负责：

```text
Personalization semantic ownership
StableProfile
SelfDeclaredTendency
Context access
Memory / MemoryRetriever
Agent Tool Boundary
QuestionIntent
Reactive / Proactive Agent orchestration
```

08 只负责这些交互产生的信息如何进入 Evidence。

## 10：Runtime, Research and Experiment

负责：

```text
ForecastRun
PolicyEvaluation
ActionDecision
BotResponseUnit
BotActionEvent
MeasurementRequest
Study / Experiment
Dataset / Replay
ModelContextBuilder / Scientific Core sharing
```

## 11：Project Module Boundaries and Engineering Rules

负责：

```text
package ownership
allowed imports
ports / adapters
transactions
idempotency implementation
configuration ownership
architecture tests
```

## 12：Repository Refactor and Migration Plan

负责基于当前真实仓库决定：

```text
保留
迁移
拆分
合并
淘汰
兼容
staged cutover
```

---

# 45. 本层核心原则

可以将 08 压缩为：

```text
Raw Source
   ↓
Conservative Structured Extraction
   ↓
Typed Evidence
   ↓
Versioned Domain Resolution
   ↓
Resolved Knowledge
   ↓
Purpose-scoped Snapshot / Context Read
```

最终目标不是“把所有信息结构化”，而是：

> **确保系统只使用它当时真正知道、能够说明来源、能够说明时间、能够说明为什么成立的信息。**
