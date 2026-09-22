# MindFlow 11：Project Module Boundaries and Engineering Rules

> 状态：工程架构约束稿（经交叉审查收口）
>
> 作用：定义 MindFlow 后续重构与持续开发时的模块所有权、依赖方向、事务边界、Multi-Agent、Tool、配置版本、并发、持久任务、测试与迁移规范。
>
> 本文件约束“代码应该放在哪里、允许依赖谁、如何跨模块协作”。不重新定义 08–10 中的数据、建模、个性化、研究语义。

---

# 1. 工程形态

MindFlow 采用：

```text
Modular Monolith
```

即：

```text
单仓库
+
清晰模块边界
+
PostgreSQL
+
Bot Runtime
+
Admin
+
Research Runtime / Scripts
+
轻量后台 Worker
```

当前不建设：

```text
Microservices
Kafka-based Event Platform
Distributed Actor System
Dynamic Plugin Platform
Generic Experiment Platform
```

严格的是依赖和所有权，不是部署拓扑。

---

# 2. 主要 Ownership Zone

工程逻辑划分为：

```text
1. Domain-owned Types / Contracts
2. Knowledge
3. Modeling Boundary
4. Scientific Core
5. Policy
6. Application
7. Agent Runtime / Cognitive Adapters
8. Infrastructure
9. Research
```

外围入口：

```text
Entrypoints
Scheduler
Workers
Admin
```

这些区域不是要求分别部署，也不要求每个概念对应独立 package/service。

---

# 3. Contract Ownership

Contract 应由拥有其语义的模块持有。

例如：

```text
Evidence / ClaimAddress
→ Knowledge owner

ModelContext / ForecastResult
→ Modeling owner

PolicyInput / PolicyEvaluationResult
→ Policy owner

BotResponseUnit / ConversationTurn
→ Interaction owner

ToolSpec / ToolCallResult
→ Agent Tool owner
```

只保留极少量真正跨系统的 shared primitives：

```text
ParticipantId
ArtifactId
Timestamp / provenance primitives
```

禁止建立新的：

```text
common.py
utils.py
contracts/models.py
```

作为所有模块共享对象的垃圾桶。

---

# 4. 依赖方向

总体依赖应满足：

```text
                 Entrypoints
                     │
                     ▼
                 Application
          ┌──────────┼───────────┐
          │          │           │
          ▼          ▼           ▼
      Knowledge    Policy    Agent Ports
          │                      │
          ▼                      │
   Modeling Boundary             │
          │                      │
          ▼                      │
   Scientific Core               │
                                 │
Infrastructure ──implements──────┘
```

Research：

```text
Research
→ Knowledge
→ Modeling Boundary
→ Scientific Core
→ Pure Policy Evaluator
```

禁止：

```text
Research
→ Online Runtime Orchestration

Online Runtime
→ Research
```

---

# 5. Domain-owned Types

Domain 类型表达业务语义与不变量，不负责持久化和外部调用。

禁止依赖：

```text
PostgreSQL
SQLAlchemy
Feishu SDK
HTTP
LLM
Filesystem
Web
Admin
Research
```

禁止：

```python
class Evidence:
    def save_to_db(...):
        ...
```

ORM Row 不等于 Domain Object。

---

# 6. Knowledge Ownership

Knowledge 模块负责：

```text
Evidence semantics
Claim definitions
Correction / Supersession / Retraction
Resolver semantics
Historical as-of resolution
Current projection rules
```

Resolver 应尽量：

```text
pure
deterministic
versioned
side-effect free
```

Knowledge 不负责：

```text
Forecast
Policy
Conversation
Memory retrieval
Feishu
Database transaction ownership
```

---

# 7. ClaimDefinition

`ClaimDefinition` 仅描述：

```text
domain owner
claim type
subject type
value schema
temporal semantics
```

不承担：

```text
全局权限
全局 retention
所有 model dependency
所有 Agent exposure
Admin rendering
```

V1 使用代码级 typed definition/schema。

不建设动态 Claim Registry 数据库。

---

# 8. Scientific Core

Scientific Core 包含：

```text
Representation
Temporal Kernels
D / P / R transformation
C_D / C_U / F
A / B / S dynamics
Measurement assimilation
Forecast
Parameter estimation algorithms
```

Scientific Core 禁止依赖：

```text
DB
Repository
LLM
Feishu
HTTP
Web
Memory
Conversation
Admin
.env
datetime.now()
global runtime state
```

理想调用：

```text
Result =
f(
    ModelContext,
    ModelState,
    ModelSpec,
    ParameterSet,
    Explicit Time,
    Explicit Randomness
)
```

---

# 9. ModelSpec 与 ParameterSet

必须区分：

```text
ModelSpec
= 如何计算

ParameterSet
= 当前 participant / population 使用什么参数
```

Scientific Core：

```text
使用 ParameterSet
```

Application：

```text
决定哪个 validated ParameterSet 当前 active
```

Core 不读取：

```text
latest parameter from DB
.env parameter
```

---

# 10. Parameter Estimation 与 Promotion

Parameter Estimation Algorithm 可属于 Scientific Core。

输出：

```text
CandidateParameterSet
```

之后：

```text
Validation
↓
PromotionRule
↓
ActiveParameterSet
```

由 Application / versioned promotion logic 管理。

Scientific Core 不自行执行：

```text
P1 → P2
```

---

# 11. Modeling Boundary

`ModelContextBuilder` 属于共享 Modeling Boundary。

流程：

```text
Resolved Knowledge
↓
ModelContextBuilder
↓
ModelContext
↓
Scientific Core
```

它被共同使用：

```text
Online Application
Research Replay
Synthetic Pipeline
```

它不属于 Online Runtime，也不属于 PostgreSQL Infrastructure。

它不计算：

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

这些只由 Scientific Core 计算。

---

# 12. StableProfile 防止 Double Counting

如果 StableProfile 已通过：

```text
StableProfile
↓
Appraisal Prior Resolution
↓
Resolved Appraisal
↓
ModelContext
```

则 Scientific Core 不再单独读取 StableProfile 作为额外 feature。

同一 personalization evidence 不能重复进入模型。

---

# 13. Pure Policy Evaluator

Policy 与 Scientific Core 平行。

输入：

```text
Forecast Contract
Policy Context
PolicySpec
```

输出：

```text
admissible_actions
preferred_action
reason_codes
```

禁止：

```text
send message
query DB
call Agent
perform randomization
```

Online 和 Research Replay 应调用同一 Policy evaluator。

---

# 14. Application Ownership

Application 是 use-case 与 orchestration owner。

负责：

```text
workflow
transaction boundary
consistency boundary
port invocation
artifact persistence orchestration
idempotency coordination
```

典型 Use Case：

```text
ReceiveIncomingMessage
ApplyEvidence
GenerateForecast
AssimilateMeasurement
EvaluateDecisionOpportunity
ExecuteActionDecision
RequestMeasurement
HandleDomainCommand
```

优先使用明确 Use Case，而不是建立大量：

```text
XxxManager
GlobalService
```

---

# 15. Command 与 Query

语义上区分：

```text
Query
= 读取

Command
= 可能修改 canonical / operational state
```

例如：

```text
GetForecastSummary
SearchMemory

CompleteTask
SetPreference
ChangeCalendarEvent
```

仅做语义区分。

不建设 CQRS 基础设施、双数据库或消息总线。

---

# 16. Port 定义原则

Port 只用于真实边界：

## Persistence Boundary

```text
EvidenceStore
KnowledgeReader
ForecastStore
ConversationStore
StudyStore
```

## External Capability Boundary

```text
ConversationAgentPort
EvidenceExtractorPort
MessageDeliveryPort
WebSearchPort
DocumentReaderPort
VisionPort
MemoryStore
```

纯函数不需要全部抽象成 Port。

Port 由“需要该能力的一侧”定义。

Infrastructure 负责实现。

---

# 17. Infrastructure Ownership

Infrastructure 负责现实世界 Adapter：

```text
PostgreSQL
SQLAlchemy
Feishu
LLM API
Web / URL
Document
Vision
Filesystem
Scheduler
Clock
Secrets
```

Infrastructure：

```text
implements Port
```

但不拥有业务规则。

禁止：

```text
Application
→ PostgresConcreteRepository

Scientific Core
→ SQLAlchemy
```

---

# 18. ORM 与 Domain 分离

Persistence schema 不等于业务对象。

原则：

```text
Persistence Row
↔ Mapper
↔ Domain / DTO
```

ORM 对象不得一路传入：

```text
Scientific Core
Agent
Research analysis
```

Read-heavy Admin / Research 场景可以直接生成 Read DTO，不要求教条式 Domain round-trip。

---

# 19. Multi-Agent Runtime

MindFlow 按 Multi-Agent-capable Runtime 设计。

至少允许独立逻辑角色：

```text
Conversation Agent
Evidence Extractor
Memory Materializer
Research Support Coder
Document Analysis Agent
```

是否成为独立 Agent Role，应满足至少部分：

```text
独立 Context boundary
独立 objective
独立 Tool permission
独立 output contract
独立 safety / research boundary
```

不按 Domain 数量机械拆 Agent。

---

# 20. LLM-backed Adapter 不等于 Agent

固定：

```text
prompt
schema
single-call transformation
```

且没有复杂 reasoning/tool loop 的能力，可以只是：

```text
LLM-backed Adapter
```

不需要全部命名为 Agent。

---

# 21. Multi-Agent 协调方式

默认：

```text
Application / Harness
├── Conversation Agent
├── Evidence Extractor
├── Memory Agent
└── Specialized Agent
```

禁止自由 Agent Mesh：

```text
Agent A → B → C → A
```

需要 delegation 时，通过：

```text
Typed Capability / Tool Handoff
```

而不是任意 Agent-to-Agent hidden messages。

---

# 22. Delegation 不继承权限

Agent 可以委托能力，但不能自动委托：

```text
Tool permissions
Memory access
Participant-wide context
Mutation authority
Study hidden data
```

原则：

```text
Delegation Capability
≠
Delegation Authority
```

Specialized Agent 只接收完成任务所需的最小 context。

---

# 23. 禁止 Shared Mutable Agent Scratchpad

多个 Agent 不得共同读写隐藏可变：

```text
global scratchpad
shared dict
implicit agent memory
```

跨 Agent 信息通过：

```text
typed handoff
artifact ref
tool result
explicit context
```

传递。

---

# 24. Agent 输出不是 Canonical Truth

不同 Agent 的输出：

```text
Conversation Agent
→ Response / Proposal / Command Request

Evidence Extractor
→ EvidenceCandidate

Memory Agent
→ Memory Artifact

Support Coder
→ Research Coding Artifact

Document Agent
→ ToolObservation
```

均不能绕过 Application / Validation / Evidence 边界直接成为 canonical state。

---

# 25. Agent Execution Provenance

Agent execution 至少记录：

```text
agent_role
agent_version
prompt_version
provider/model
purpose

input refs
context revision refs
tool calls

trace_id
parent_call_id

timing
outcome
```

不默认保存完整：

```text
system prompt
Memory dump
Web page
Context dump
```

---

# 26. ConversationTurn Assembly

平台消息与对话 Turn 分离：

```text
SourceArtifact
≠
ConversationTurn
```

连续快速消息可以组成同一 Turn。

例如：

```text
S1: “我明天下午有考试”
S2: “但是好像改到周五了”

ConversationTurn T1
source_refs = [S1, S2]
```

不创建假的“合并 Source”。

---

# 27. TurnAssembler

Conversation Agent 消费：

```text
ConversationTurn
```

而不是直接对每个 Source 回复。

TurnAssembler 使用：

```text
same participant
same conversation
short quiet window
max coalescing window
```

进行 soft coalescing。

具体时间属于 BehaviorSpec，由 Pilot 调整。

---

# 28. Turn Revision

新 Source 到达可以增加：

```text
turn.revision
```

Agent generation 必须绑定：

```text
generation_revision
```

在 BotResponseUnit commit 前做 CAS：

```text
if generation_revision != latest_turn_revision:
    reject stale generation
```

允许偶尔浪费一次 LLM generation，但禁止发送 stale response。

---

# 29. Turn Commit Boundary

在：

```text
BotResponseUnit
+
Outbox
```

原子提交前，Turn 可以被新 Source 更新。

一旦：

```text
response_committed_at != null
```

后续 Source 必须创建新 Turn。

不尝试撤销已进入 Outbox 的历史响应。

---

# 30. Turn 的最小字段

V1 只需要：

```text
turn_id
participant_id
conversation_id

source_refs
revision

opened_at
last_source_at
response_committed_at

optional status
```

不建设复杂 Conversation Turn State Machine。

---

# 31. Turn 与其他交互单位

保持：

```text
Source
≠
Turn
≠
Session
≠
Thread
```

其中：

```text
Source = 平台事实
Turn = 一次用户交互输入聚合
Session = 时间上的交互窗口
Thread = 语义主题连续性
```

---

# 32. Evidence Extraction 与 Turn Assembly 解耦

Evidence path 仍以 Source 为基本 provenance unit。

Extractor 可以获得：

```text
current source
+
有限 local preceding-source context
```

以理解：

```text
“不对，是周五”
```

但 Evidence 必须保持真实 source provenance。

Turn coalescing 不替代 Evidence correction semantics。

---

# 33. Multimodal Turn

连续：

```text
Image
File
Text
```

可以进入同一个 ConversationTurn。

例如：

```text
课表图片
+
“帮我看看周三有什么课”
```

应触发一次 Conversation Agent response。

卡片 confirm / form submit 等明确 UI action 可以：

```text
immediate seal
```

---

# 34. Incoming Source Transaction

外部消息到达后先执行短事务：

```text
BEGIN

check inbound idempotency

persist SourceArtifact
persist processing identity

COMMIT
```

之后才调用：

```text
Agent
Extractor
Web
```

重复平台消息不得生成新的逻辑 Source/Turn/Action。

---

# 35. 外部调用不得进入 DB Transaction

禁止：

```text
BEGIN
call LLM
call Feishu
call Web
wait/retry
COMMIT
```

LLM / Agent / Web / Feishu 调用均在 DB transaction 外执行。

---

# 36. Evidence Commit Transaction

Accepted Evidence 与对应 Current Projection 应尽量在同一短事务完成：

```text
BEGIN

append Evidence

call pure Resolver

persist Current Projection
record knowledge revision/change

COMMIT
```

Resolver 自身不拥有 transaction。

---

# 37. Domain Change

`DomainChangeEvent` 是逻辑 change marker。

V1 可通过：

```text
knowledge_revision
affected_domains
affected_from
```

表达。

不建设 Kafka/Event Bus。

Application 根据 change 判断：

```text
current forecast stale?
model checkpoint recompute?
other derived state refresh?
```

---

# 38. Forecast Transaction Boundary

推荐：

```text
Tx1:
read canonical state
freeze FORECAST ContextSnapshot
record revisions
COMMIT

↓

ModelContextBuilder
↓
Scientific Core

↓

Tx2:
persist ForecastRun
COMMIT
```

Forecast 计算期间不锁 participant。

若 Knowledge 在计算期间变化，旧 Forecast 仍合法，只是：

```text
stale-for-current-runtime
```

---

# 39. Relevant Canonical State Barrier

需要 fresh canonical state 的操作，例如：

```text
fresh forecast
proactive care
external mutation
research snapshot
```

必须等待：

```text
与当前 operation 相关的 Source
→ Evidence
→ Domain Resolution
```

完成。

不等待：

```text
Memory materialization
Profile background update
Research indexing
Admin cache
```

它是 Use Case 语义，不建设通用 Barrier Service。

---

# 40. Reactive Chat

普通聊天可以：

```text
Persist Source
↓
Conversation Agent
↓
BotResponseUnit
```

并行/随后：

```text
Evidence Extraction
↓
Knowledge Update
```

只有真正调用：

```text
fresh forecast
canonical mutation
policy-sensitive action
```

时才进入 Relevant Canonical State Barrier。

---

# 41. Agent Tool Gateway

Tool Gateway 负责：

```text
resolve ToolSpec
filter visibility
build InvocationContext
evaluate permission
dispatch handler
record audit
```

不负责：

```text
Forecast mathematics
Evidence resolution
Policy logic
Memory materialization
```

---

# 42. Tool Registry

V1 使用：

```text
code-level typed registry
```

不建设：

```text
database-driven plugin system
runtime-editable tool DSL
admin-created tools
```

---

# 43. ToolSpec

最小包含：

```text
tool_name
input_schema
output_schema

effect_kind
capability_scope
allowed_purposes

audit semantics
idempotency semantics
```

`effect_kind` 可采用少量分类：

```text
READ_LOCAL
READ_HISTORY
READ_EXTERNAL

DOMAIN_COMMAND
EXTERNAL_MUTATION

PRESENTATION
DELIVERY

RESEARCH_ONLY
```

它仅是 coarse classification，不是权限规则引擎。

---

# 44. InvocationAuthority

Mutation/High-impact Tool 不使用静态：

```text
requires_confirmation = true/false
```

而根据：

```text
USER_EXPLICIT
USER_CONFIRMED
AGENT_INFERRED
POLICY_AUTHORIZED
SYSTEM_SCHEDULED
```

结合：

```text
ambiguity
risk
reversibility
subject resolution
current state
```

输出：

```text
ALLOW
REQUIRE_CONFIRMATION
DENY
```

Permission evaluation 属于 deterministic Application rule。

---

# 45. Tool Visibility 与 Execution Permission

Tool 对 Agent 可见：

```text
≠
```

当前 invocation 合法。

两层分别控制：

```text
Visibility Gate
Execution Gate
```

不同 AgentRole / Purpose 拥有不同可见 Tool Set。

---

# 46. Broad Read, Narrow Write

Agent 可以自主：

```text
search memory
search web
read document
refine query
```

但 access universe 固定：

```text
current participant
authorized content
research-hidden data excluded
```

Agent mutation 必须进入：

```text
Tool
↓
Application Command
↓
Domain validation
↓
Persistence / External Adapter
```

Agent 不能直接调用 Repository。

---

# 47. Tool-returned Content 无 Instruction Authority

以下内容均只是 data：

```text
Web
URL
Document
Image
Memory
Specialized Agent Result
```

它们不能：

```text
grant permission
override policy
expand tool access
become system instruction
```

Web/URL/Document 是 external untrusted data。

Memory 是 internal derived data，但同样无 instruction authority。

---

# 48. ToolObservation → Evidence

Web、Document、Vision、Specialized Agent 默认只产生：

```text
ToolObservation
```

需要进入 canonical state 时：

```text
ToolObservation
↓
EvidenceCandidate
↓
Validation
↓
Evidence
```

禁止：

```text
Tool
→ direct canonical write
```

除非该 Tool 本身是经过 Application Command 定义的显式 Domain Mutation。

---

# 49. Tool Output

不同 Tool 使用 typed output：

```text
MemorySearchResult
ForecastSummary
TaskView
DocumentResult
CommandOutcome
```

不设计万能：

```text
dict[str, Any]
```

Mutation Tool 返回业务 Outcome，不暴露 DB row/SQL semantics。

---

# 50. Presentation 与 Delivery

区分：

```text
render_card
= PRESENTATION

send_card
= DELIVERY
```

Delivery 必须走：

```text
BotResponseUnit
+
Outbox
```

Agent 不直接调用 Feishu SDK。

Card click / form submit 是新的 inbound SourceArtifact。

---

# 51. Outbound Response Transaction

Agent生成完成后：

```text
BEGIN

verify turn revision if reactive

persist BotResponseUnit
persist Outbox Job

mark ConversationTurn response committed if applicable

COMMIT
```

Outbox worker 再执行实际 delivery。

---

# 52. Outbox 语义

Outbox 提供：

```text
durable execution
retry
logical idempotency
crash recovery
```

不承诺：

```text
exactly-once external delivery
```

若平台支持 client idempotency/deduplication，应使用。

若平台无法确认实际结果，允许：

```text
SEND_UNKNOWN
```

而不是强行记为失败。

---

# 53. BotActionEvent

Delivery outcome 应作为正式可观测事件：

```text
SEND_REQUESTED
SEND_SUCCEEDED
SEND_FAILED
SEND_UNKNOWN
DELIVERED if available
SEEN if available
CARD_CLICKED
REPLY_LINKED
```

Job success 不等于 delivery success。

Research 不从 job table 推断 exposure。

---

# 54. Persistent Job

统一轻量 durable job 机制可用于：

```text
SEND_BOT_RESPONSE
SEND_MEASUREMENT
EXTERNAL_MUTATION
MATERIALIZE_MEMORY
RECOMPUTE_DERIVED_STATE
UPDATE_PROFILE
```

最小字段：

```text
job_id
job_kind
subject_ref
payload_ref
status

attempt_count
next_attempt_at

idempotency_key

lease / locked_until
worker_id

created_at
updated_at
```

Worker 崩溃后 expired lease 的 job 可以被重新领取。

---

# 55. 不把所有后台工作 Job 化

只有：

```text
进程崩溃后不能静默丢失
```

的 workflow 必须 durable。

可重建 cache / 非关键后台计算可以丢失后重算。

Persistent Job 是运行机制，不是 Domain Truth。

---

# 56. Scheduler Ownership

Scheduler 只负责：

```text
when to trigger
```

然后调用：

```text
Application Command / Persistent Job
```

禁止 Scheduler 直接：

```text
算压力
决定 CARE
发飞书
修改 canonical state
```

Scheduler trigger 必须幂等。

保存：

```text
scheduled_for
triggered_at
```

研究相关时间不可只保留 created_at。

---

# 57. 时间语义

Scientific Core 所有时间显式输入。

Scheduler / Runtime 必须明确：

```text
study timezone
runtime timezone
```

禁止隐式依赖 server local timezone。

最终 artifact 应使用 timezone-aware 时间或统一 UTC + explicit timezone semantics。

---

# 58. Concurrency

并发控制原则：

```text
Different participants:
    parallel

Same participant:
    LLM / read / external computation
    can parallel when safe

Same participant canonical writes:
    short DB transaction
    optimistic revision / targeted lock

Long computation:
    immutable/revisioned snapshot
    no long DB lock
```

不使用全局 Bot lock。

不建设 Actor System / vector clock。

---

# 59. Knowledge Revision

V1 可以使用：

```text
participant_knowledge_revision
```

或等价简单 revision。

写入 canonical state 时：

```text
expected revision
→ write
→ revision + 1
```

冲突则：

```text
reload
re-resolve
retry
```

具体锁粒度在 12 根据现有 schema 决定。

---

# 60. Memory Concurrency

V1 Markdown Memory 假设：

```text
per participant single logical writer
```

Conversation Agent：

```text
read-only
```

MemoryMaterializer：

```text
writer
```

写入采用：

```text
temp file
↓
atomic replace
```

必要时 per-participant lock。

Memory backend 必须位于 persistent volume。

未来如果多 Bot replica，可替换 MemoryStore backend。

---

# 61. Memory 是 Derived State

Memory：

```text
eventual
rebuildable where source-backed
```

不阻塞普通 conversation。

Knowledge-backed Memory 可以重建。

Conversation-continuity summary 是 versioned derived artifact，应记录：

```text
source refs
materializer/prompt version
revision/hash
```

---

# 62. Multi-Agent 与 Worker 分离

Agent负责：

```text
semantic reasoning
unstructured interpretation
language
```

Worker负责：

```text
durable background execution
retry
scheduling
```

例如：

```text
MemoryMaterializationJob
↓
Worker
↓
MemoryMaterializer Agent
↓
MemoryStore
```

两者职责不同。

---

# 63. Agent Delegation Budget

Agent delegation 默认有界。

Runtime 应支持：

```text
max delegation depth
max agent calls
max tool calls
timeout/cancellation
```

具体数值由 Pilot 后确定。

Specialized Agent 不允许默认无限递归 delegation。

---

# 64. Lifecycle Class

所有数据对象应归入以下之一：

## Canonical Persistent Record

例如：

```text
SourceArtifact
Evidence
Domain state/history
Measurement
StudyEnrollment
StudyEvent
```

## Immutable Historical Artifact

例如：

```text
ContextSnapshot
ForecastRun
PolicyEvaluation
ActionDecision
BotResponseUnit
ExperimentAssignment
```

## Recomputable Derived State

例如：

```text
Current Knowledge Projection
ModelStateCheckpoint
StableProfile
Memory Index
Current Forecast pointer
StudyState projection
```

## Ephemeral Runtime DTO

例如：

```text
EvidenceCandidate
ConversationTurnContext
InvocationContext
ModelContext
PolicyInput
ToolResult
```

## Research Materialized Artifact

例如：

```text
Dataset
Coding View
Evaluation Output
Statistical Result
```

存在一个 class 不意味着必须建表。

---

# 65. ContextSnapshot 使用范围

持久化 Immutable ContextSnapshot 主要用于：

```text
FORECAST
POLICY
REPLAY
```

等需要严格可复现的场景。

普通 reactive chat 不必每轮建立完整 ContextSnapshot。

只需记录：

```text
conversation context revision refs
memory refs/revisions
agent/prompt version
tool trace
```

---

# 66. Error Semantics

Application 对外输出稳定错误语义：

## Domain / Validation

```text
AMBIGUOUS_SUBJECT
INVALID_TRANSITION
PERMISSION_DENIED
```

## External Dependency

```text
LLM_TIMEOUT
FEISHU_RATE_LIMITED
WEB_UNAVAILABLE
```

## Infrastructure

```text
DB_UNAVAILABLE
CONCURRENCY_CONFLICT
```

禁止 Agent/UI 直接处理：

```text
psycopg exception
SDK exception
```

---

# 67. Retry Ownership

Adapter：

```text
single-call technical retry
```

Application / Job：

```text
workflow retry
```

Domain / Scientific Core：

```text
no retry logic
```

禁止 Core 中：

```text
sleep
network retry
```

---

# 68. Failure Semantics

必须 fail closed：

```text
proactive Policy unavailable
Experiment assignment failure
permission evaluation failure
required fresh canonical/model state unavailable
```

可 graceful degradation：

```text
Memory unavailable
Profile background refresh failure
Web unavailable
nonessential personalization unavailable
```

禁止 Agent 越权替代其他角色。

例如：

```text
Extractor failed
X→ Conversation Agent writes Evidence

Policy failed
X→ Agent decides proactive CARE
```

---

# 69. Fresh Forecast Failure

如果明确请求 fresh forecast：

```text
relevant canonical state unavailable
or
Scientific Core failed
```

则返回：

```text
fresh forecast unavailable
```

不能 silent fallback 为旧 Forecast。

只有明确允许 stale display 时才可返回，并必须标记 stale。

---

# 70. Measurement Failure

MeasurementRequest 必须保留：

```text
scheduled
requested
delivery
response
```

相关状态。

EMA 缺失不能自动解释成：

```text
user noncompliance
```

需要区分系统未发送、发送失败、已发送未响应等情况。

---

# 71. Performance Path

分三类：

## Interactive Critical Path

```text
normal conversation
simple read tool
```

不等待无关后台更新。

## Relevant-State Path

```text
fresh forecast
proactive care
external mutation
```

优先正确性。

## Background Path

```text
Memory materialization
ProfileUpdater
Research export
Dataset build
Synthetic analysis
```

不得占据用户 critical path。

---

# 72. Performance Budget

当前不在架构中拍死：

```text
2s
500ms
```

等硬数字。

先记录：

```text
p50
p95
p99

Agent latency
Extractor latency
Forecast latency
DB transaction latency
Memory search latency
Tool latency
Outbox delay
```

Pilot 后再制定 SLO。

---

# 73. Backpressure

当前不建设复杂 backpressure system。

V1 使用：

```text
ConversationTurn coalescing
bounded worker concurrency
bounded Agent/tool delegation
persistent job deduplication
```

控制 burst。

禁止无限：

```text
spawn Agent task
spawn Tool task
```

---

# 74. 配置分类

正式分五类：

## Secrets

```text
API keys
passwords
Feishu secret
```

## Deployment Config

```text
DB host
service port
worker count
log level
timeout
```

## Scientific Spec

```text
ModelSpec
RepresentationSpec
MeasurementModelSpec
EstimationSpec
PromotionRuleSpec
```

## Behavior / Application Spec

```text
PolicySpec
OpportunityRuleSpec
ToolPermissionSpec
PromptSpec
TurnAssemblySpec
MemoryMaterializerSpec
ProfileUpdaterSpec
```

## Study / Research Spec

```text
StudyProtocol
MeasurementProtocol
ExperimentSpec
DatasetSpec
CodingSpec
```

---

# 75. `.env` 边界

`.env` 只承载：

```text
Secrets
Deployment Config
```

不再承载正式：

```text
scientific model parameters
kernel definitions
research threshold
experiment probability
promotion rule
```

当前已有相关字段在 12 中迁移。

---

# 76. Scientific Default

任何会改变研究结果的 default 必须进入 versioned spec。

禁止隐藏：

```python
def forecast(gamma=0.04, threshold=85):
```

但普通工程默认，例如：

```text
HTTP timeout
```

可以留在 deployment config。

---

# 77. RuntimeRelease

`RuntimeRelease` 聚合：

```text
release_id
git revision

major scientific spec refs
major behavior spec refs

container/image identity if applicable
```

不建设巨型 version manifest。

关键 Artifact 继续保存直接影响自身的版本，例如：

```text
ForecastRun
→ ModelSpec / ParameterSet

PolicyEvaluation
→ PolicySpec
```

---

# 78. Prompt Versioning

下列 Prompt 属于 Behavior/Research Spec：

```text
Conversation Agent Prompt
Evidence Extractor Prompt
Memory Materializer Prompt
Support Coder Prompt
```

至少应有：

```text
prompt_id
prompt_version
content/hash
expected output schema
```

放仓库管理即可。

不建设独立 Prompt Management Platform。

---

# 79. StudyProtocol 与其他 Spec

StudyProtocol 应：

```text
reference exact versions
```

例如：

```text
ModelSpec v3
PolicySpec v2
MeasurementSpec v4
ConversationPrompt v7
```

而不是复制它们的内容。

只有 study-relevant behavior surface 的变化才需要研究层版本/amendment。

普通无研究影响修改不自动构成 protocol amendment。

---

# 80. Boundary Validation

所有 trust boundary 输入进入内部逻辑前必须：

```text
validate
normalize
type
```

包括：

```text
LLM structured output
Tool input/output
Feishu payload
Web/document parser output
ModelSpec
StudySpec
Research config
```

原则：

```text
Validate at trust boundary
Trust typed internal contracts thereafter
```

内部纯函数之间不重复做无意义 parse/validation。

---

# 81. Architecture Enforcement

11 定义语义规则。

12 根据最终 package structure 实现具体 architecture tests。

必须至少约束：

```text
Scientific Core
X→ Infrastructure
X→ Agent
X→ Research

Domain/Knowledge
X→ Infrastructure concrete adapter

Online Runtime
X→ Research

Research
X→ Online orchestration

Agent implementation
X→ concrete DB repository
X→ Scientific internals
```

---

# 82. Test Layers

推荐：

```text
Fast Unit
↓
Invariant / Property
↓
Architecture
↓
Application Use Case
↓
Adapter Integration
↓
Idempotency / Recovery
↓
Replay / Synthetic
↓
Runtime Acceptance
```

Heavy Replay / Synthetic / ECS acceptance 不要求每个小改动全部运行。

---

# 83. Scientific Invariant Tests

至少覆盖：

```text
SingleExposureEncodingRule
UNKNOWN 不自动变 Medium
Recovery contribution sign
course contribution cap
no future known_at leakage
same input/spec/seed → same result
```

---

# 84. Projection Golden Test

构造：

```text
Evidence
Correction
Contradiction
```

验证：

```text
Incremental Current Projection
==
Full As-Of Recompute
```

同 knowledge cutoff 下应一致。

---

# 85. Forecast Replay Test

固定：

```text
ContextSnapshot
ModelSpec
ParameterSet
Prior State
Seed
```

应得到同样 Forecast。

---

# 86. Policy Replay Test

固定：

```text
Policy Context / Snapshot
PolicySpec
```

应得到同样：

```text
admissible_actions
preferred_action
reason_codes
```

---

# 87. Idempotency / Recovery Tests

至少覆盖：

```text
duplicate incoming message
duplicate scheduler trigger
LLM retry
outbox retry
worker crash
duplicate card callback
turn revision race
randomization retry
```

要求：

```text
no duplicate Evidence
no duplicate logical ResponseUnit
no duplicate MeasurementRequest
one immutable ExperimentAssignment
```

外部 delivery 无法确认时允许：

```text
SEND_UNKNOWN
```

而不是假装 exactly-once。

---

# 88. Database Migration

所有 production schema change 使用：

```text
explicit migration
```

禁止：

```text
runtime startup auto-alter
ORM auto-sync production schema
```

至少测试：

```text
Fresh DB
→ apply all migrations

Upgrade fixture
→ apply migration
→ new runtime reads correctly
```

DB schema version 与 Scientific/Behavior version 相互独立。

---

# 89. Admin Boundary

Admin 是 Application/API consumer。

Read-heavy 页面可以使用：

```text
dedicated Read DTO / Query
```

但 mutation：

```text
change participant state
force study phase
change preference
```

必须进入 Application Command。

禁止 Admin 页面直接 SQL 修改 canonical scientific state。

---

# 90. Entrypoint Boundary

Bot main、Admin API、Scheduler worker、CLI 等 Entrypoint 只负责：

```text
load config
wire dependencies
start runtime
```

不放核心业务逻辑。

---

# 91. Research Boundary

Research 可以：

```text
read canonical artifacts
use Knowledge semantics
use ModelContextBuilder
use Scientific Core
use Policy evaluator
build Dataset
run Replay
run Synthetic
```

正式分析优先：

```text
DatasetBuilder
↓
DatasetManifest
↓
Frozen Dataset
```

不让 notebook 成为隐藏业务层。

Research 不应直接依赖 Online Bot workflow。

---

# 92. Observability

重要 workflow 使用：

```text
correlation_id
trace_id

source_ref
turn_id
decision_point_id
response_unit_id
job_id
```

串联：

```text
Inbound
→ Knowledge
→ Forecast/Policy
→ Agent
→ Outbox
→ Delivery
```

日志默认记录：

```text
ID
status
timing
error code
```

不默认打印：

```text
raw conversation
document content
personal data
```

---

# 93. Metrics

Metrics 用于系统可观测性，例如：

```text
LLM latency
extractor failure rate
forecast latency
outbox retry count
worker lag
```

Metrics 不是 canonical participant state。

Research 不依赖 Metrics 系统重建实验事实。

---

# 94. 命名规范

避免模糊职责模块：

```text
Manager
Utils
Helpers
Common
Misc
GlobalService
```

如果只能用这些名字描述模块，通常表示 ownership 不清。

优先使用：

```text
forecast
appraisal_resolution
memory_search
tool_permissions
conversation_turn
```

直接表达责任。

---

# 95. 禁止 Universal Participant Dump

禁止建立：

```text
get_user_everything()
```

供 Agent/Admin/Research/Scientific 共用。

Context 必须 purpose-scoped。

防止绕过：

```text
participant isolation
knowledge cutoff
privacy
research hidden boundary
```

---

# 96. 新 Tool 的扩展原则

新增 read tool：

```text
1. typed input/output
2. ToolSpec
3. capability/effect classification
4. purpose/role visibility
5. Adapter
6. audit
7. contract test
```

不应要求修改：

```text
Scientific Core
Knowledge Resolver
Memory
Policy
```

除非真的产生新 Domain 语义。

---

# 97. 新 External Mutation Tool

流程：

```text
ToolSpec
↓
InvocationAuthority
↓
Application permission / confirmation
↓
Command / Proposal
↓
Persistent ToolEffect / existing Saga when needed
↓
External Adapter
↓
Result reconciliation
```

本地单数据库 mutation 不强行 Saga 化。

---

# 98. 新 Scientific Mechanism

例如修改 kernel：

```text
RepresentationSpec new version
↓
Scientific Core support
↓
Synthetic / Replay validation
↓
ModelSpec release
↓
Study / Runtime release
```

不需要修改：

```text
Agent Runtime
Tool Registry
Feishu
```

---

# 99. 工程硬规则汇总

1. 采用 modular monolith，不做微服务化。
2. Contract 由语义 owner 持有，禁止新的 common dumping ground。
3. Scientific Core 零 DB/LLM/Feishu/Web/Memory/Clock 隐藏依赖。
4. Knowledge Resolver 纯化；事务由 Application 持有。
5. `ModelContextBuilder` 是共享 Modeling Boundary，不属于 Online Runtime。
6. Policy Evaluator 与 Scientific Core 平行、纯化。
7. Application 负责 Use Case、事务、一致性和 orchestration。
8. Port 只定义真实边界，不为所有函数造接口。
9. Infrastructure 实现 Port，不拥有业务规则。
10. ORM Row 不等于 Domain Object。
11. Multi-Agent 是受控角色系统，不建设自由 Agent Mesh。
12. Agent delegation 不继承 authority/context/tool 权限。
13. 禁止 shared mutable Agent scratchpad。
14. 所有 Agent output 都不是 canonical truth。
15. Conversation Agent 消费 revisioned ConversationTurn，而不是逐 Source 回复。
16. 快速连续消息通过 quiet-window + max-window coalescing。
17. stale generation 在 BotResponseUnit commit 时通过 turn revision CAS 拒绝。
18. ResponseUnit + Outbox commit 后新 Source 必须进入新 Turn。
19. Evidence path 与 ConversationTurn path 解耦。
20. 外部调用永远不处于长 DB transaction 中。
21. Accepted Evidence + Current Projection 尽量短事务原子更新。
22. Forecast 基于 immutable Snapshot，无长锁。
23. Relevant Canonical State Barrier 只等待当前 operation 所需 canonical state。
24. Agent Mutation 必须走 Application Command。
25. Tool Registry 代码级 typed，不做动态插件平台。
26. Tool Visibility 与 Execution Permission 分离。
27. Tool permission 根据 InvocationAuthority 等 deterministic rule 决定。
28. Tool-returned content 没有 instruction authority。
29. External Observation 默认先是 Observation/Candidate，不直写 Knowledge。
30. Delivery 通过 BotResponseUnit + persistent Outbox。
31. Outbox 不承诺 external exactly-once；支持 SEND_UNKNOWN。
32. Durable Job 有 idempotency + lease/crash recovery。
33. Scheduler 只产生 Application Command/Job，并记录 planned vs actual time。
34. 当前并发主要依靠 PostgreSQL short transaction/revision/targeted lock。
35. 不建设 global bot lock / Actor System / vector clock。
36. Markdown Memory V1 使用 single-writer + atomic replace + persistent volume。
37. Memory/Profile 属于 derived state，可 graceful degradation。
38. Agent 与 Worker 分离。
39. Agent delegation 有界。
40. Interactive / Relevant-State / Background 三类路径分离。
41. 先观测真实 latency，再冻结性能 SLO。
42. Critical Policy/Experiment/fresh-state failure 应 fail closed。
43. Agent不能越权替代 Extractor/Policy/Research Coder。
44. `.env` 只放 Secrets / Deployment Config。
45. Scientific/Behavior/Study Spec 均 versioned。
46. Formal Study 不允许 deployment env silent override scientific/study behavior。
47. RuntimeRelease 聚合主要 release identity，不做巨型版本注册表。
48. StudyProtocol 引用各 Spec 版本，不复制配置。
49. 所有外部/LLM/Tool 输入在 trust boundary 做 schema validation。
50. 架构边界必须由自动测试执行。
51. Projection / Forecast / Policy 均应支持 replay/golden consistency test。
52. 所有 DB schema change 使用显式 migration。
53. Admin Write 走 Application Command。
54. Entrypoint 只负责 dependency wiring/startup。
55. Research 不依赖 Online orchestration；Online 不依赖 Research。
56. 禁止 universal participant dump API。
57. Observability 记录 IDs/status/timing，默认不记录敏感全文。
58. Metrics 不是 Domain/Research truth。

---

# 100. 最终工程原则

MindFlow 后续开发遵循：

```text
Application orchestrates
Domain / Knowledge define semantics
Scientific Core computes
Policy evaluates
Agents reason
Infrastructure executes
Research analyzes
```

跨边界原则：

```text
Explicit Contract
Explicit Authority
Explicit Provenance
Explicit Version
```

状态原则：

```text
Canonical facts are persistent
Historical actions are immutable
Derived state is rebuildable
Runtime DTOs are ephemeral
Research datasets are derived
```

运行原则：

```text
Pure core
Short transactions
Asynchronous external side effects
Idempotent logical actions
Participant-local concurrency
Bounded Multi-Agent delegation
```

最终目标是：

> 在保持当前单仓库和轻量部署结构的前提下，使后续新增模型机制、Agent、Tool、研究流程和交互能力时，不再依靠跨层引用、共享隐式状态和临时补丁维持系统运行。
