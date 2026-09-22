# MindFlow 09：Personalization and Bot Context Architecture
>
> 本文件定义的是 **Personalization semantic ownership、Conversation-visible Context、Agent Memory、Tool capability、Reactive / Proactive Agent orchestration**。它不要求每一个逻辑概念都独立持久化。

---

## 1. 设计目标

MindFlow 的 Conversation Agent 需要：

- 理解当前消息；
- 使用当前 Event / Task / Appraisal 等结构化信息；
- 尊重用户明确的互动偏好；
- 在需要时主动探索用户历史；
- 保持长期对话连续性；
- 使用有限、谨慎的 longitudinal personalization；
- 使用 Web、URL、文档、图片、卡片等 Tool；
- 在 Policy 授权下执行 proactive action 或 model-driven sensing；
- 同时不把历史、推断、实验标签、模型内部状态混成“用户事实”。

核心原则：

$$
\boxed{Broad\ Read,\ Narrow\ Write}
$$

即：

```text
Agent 可以在授权范围内广泛读取、检索、推理和使用工具；
Canonical Write、Model State、Profile Promotion、Experiment Constraint
必须由 deterministic Harness / Domain path 控制。
```

---

# 2. Personalization 是语义 Ownership，不是七张表

MindFlow 至少区分：

```text
UserFacts
UserPreferences
SelfDeclaredTendencies
AppraisalBeliefs
EpisodeContext
StableProfile
ModelParameters
```

这是**语义分类与 ownership**。

并不意味着：

```text
7 tables
7 services
7 managers
```

很多内容仍然复用 08 的：

```text
Evidence
Derived Knowledge
Domain Projection
```

例如：

```text
UserPreference
SelfDeclaredTendency
AppraisalBelief
```

可以是对应 Domain 的 typed Knowledge。

真正需要独立科学状态的是：

```text
ModelParameters
```

StableProfile 则是 longitudinal derived personalization knowledge。

---

# 3. UserFacts

UserFacts 只保存：

> 与 participant 有关、且没有更明确 Domain owner 的相对稳定事实。

例如：

```text
timezone
academic stage
explicit long-term goal
school context
```

禁止复制：

```text
Course / Event
Task / Deadline
Sleep / Recovery
EMA
```

这些继续由原 Domain ownership 管理。

---

# 4. UserPreferences

UserPreferences 表示：

> 用户明确希望系统如何与自己互动。

例如：

```text
回答简洁
晚上 22:00 后不要主动提醒
偏好实际建议而非反复安慰
```

Durable Preference 的主要 authority：

```text
USER_EXPLICIT
USER_CONFIRMED
```

禁止因为：

```text
最近几次用户回复很短
```

就自动形成：

```text
prefers_short_responses = true
```

Preference 主要通过：

```text
explicit set
explicit modify
explicit revoke
```

变化。

---

# 5. SelfDeclaredTendency

这一类保存：

> 用户对自己长期模式的一般性陈述。

例如：

```text
“我一般晚上效率更高。”
“我通常考试前两天开始复习。”
“我一般午睡没什么用。”
```

其 epistemic basis：

```text
USER_SELF_DECLARED_GENERAL
```

它与 StableProfile 分离。

---

# 6. StableProfile

StableProfile 专指：

> 由多个相对独立 episode 的 longitudinal Evidence 支持形成、具有一定跨时间稳定性的个性化模式。

例如：

```text
在高数考试场景中反复出现较高内容不确定性
若干 episode 中短暂散步后恢复反馈较好
```

它不是：

```text
人格真值
心理诊断
永久标签
大型隐藏 personality dossier
```

目标仅限：

```text
减少重复询问
提升对话相关性
提供谨慎的 longitudinal prior
支持个性化研究
```

---

# 7. SelfDeclaredTendency 与 StableProfile 并存

两者允许：

```text
一致
不一致
暂时冲突
```

例如：

```text
SelfDeclared:
nap_is_not_helpful

StableProfile:
nap_recovery_pattern = positive_tendency
```

系统不强行融合为一个“真值”。

用户自我理解与历史 longitudinal pattern 都是有价值的信息。

---

# 8. AppraisalBeliefs

Appraisal 必须绑定：

```text
subject / event / task / episode
```

核心变量：

$$
C^{exec},\ I,\ C^{out},\ U^{perc},\ F^{rec}
$$

不能写成：

```text
UserProfile:
uncertainty = HIGH
importance = HIGH
```

而应是：

```text
subject_ref
appraisal_dimension
semantic_value / belief
resolution_state
epistemic_basis
valid_time
provenance
```

Appraisal 是快速变化的 episode-level knowledge，不是长期 personality。

---

# 9. EpisodeContext

EpisodeContext 表示：

> 当前对话、当前 topic、当前短时交互阶段的临时状态。

例如：

```text
今天不想聊太多
当前 topic openness 较高
当前已被问过一次 model-driven question
当前处于 task-edit workflow
```

它的时间尺度通常是：

```text
minutes / hours / current episode
```

不能因为：

```text
“今天别问太多。”
```

就升级成永久 Preference。

EpisodeContext 是 runtime projection，不要求单独建设 canonical profile store。

---

# 10. StableProfile 必须带 Scope

任何 longitudinal pattern 都必须说明适用范围。

例如：

```text
profile_key = appraisal.uncertainty
scope = 高等数学考试
```

不能因为两个高数考试 episode 就升级为：

```text
所有考试都高不确定
```

更不能：

```text
这个人天生容易不确定
```

原则：

$$
Generalization\ Scope \le Evidence\ Coverage
$$

---

# 11. StableProfile 的支持单位是 Independent Episode

同一 episode 里的多次观察不等于多个独立支持。

例如：

```text
同一次考试
上午回答一次
下午被问一次
```

不能算两个 independent supports。

ProfileUpdater 应关注：

```text
independent_episode_count
temporal spread
cross-context coverage
contradictions
recency
elicitation provenance
```

而不是简单 Evidence row count。

---

# 12. StableProfile 当前阶段只冻结 Contract，不急于自动 Promotion

在没有 Calibration / Pilot 数据以前，不应拍脑袋固定：

```text
>= 3 episodes → SUPPORTED
```

或：

```text
confidence = 0.837
```

V1 可以使用：

```text
INSUFFICIENT
TENTATIVE
SUPPORTED
```

但具体 promotion threshold 需要：

```text
Scenario
Synthetic
Pilot
```

再冻结。

因此当前实现重点是：

```text
可追踪 support / contradiction / scope / provenance
有限 feature registry
保守 exposure
```

而不是马上建设复杂自动 Profile Learning 平台。

---

# 13. StableProfile 最小可追踪信息

概念上至少应能回答：

```text
profile key
scope
value
status
basis / freshness
independent episodes
support summary
contradiction summary
first / last supported time
evidence manifest
updater version
```

不要求这些全部成为数据库列。

---

# 14. StableProfile Exposure Gate

“Profile 已存在”不等于“可以直接给 Agent”。

Conversation exposure 至少考虑：

```text
support sufficient?
conflicted?
stale?
relevant to current topic?
allowed for conversation?
```

V1 对 inferred StableProfile 应保守。

TENTATIVE profile 可以作为：

```text
retrieval hint
```

帮助 Agent 找具体 episode，而不是直接作为长期事实表述。

---

# 15. 当前 Explicit User Statement 优先于历史推断

Conversation 行为中：

```text
Current explicit instruction / correction
>
Historical inferred personalization
```

例如：

```text
Stored preference: concise
Current user: “这次详细讲。”
```

本轮必须详细。

这不代表删除历史 Evidence，而是 current-turn behavior precedence。

---

# 16. 不存在统一 Personalization TTL

不同信息拥有不同 temporal semantics。

### Preference

直到修改 / revoke 前可以持续有效。

### UserFact

使用 validity interval。

### EpisodeContext

短期自动失效。

### StableProfile

可能因长期缺乏新支持而 STALE。

### ModelParameters

遵循 estimator / posterior / promotion semantics。

禁止一个：

```text
PERSONALIZATION_TTL
```

统一处理所有内容。

---

# 17. StableProfile 与 ModelParameters 平行派生

正确关系：

```text
             Evidence History
             /              \
            ↓                ↓
   ProfileUpdater      ParameterEstimator
            │                │
            ↓                ↓
    StableProfile      ModelParameters
```

Core v1 禁止：

```text
StableProfile → ParameterEstimator
ModelParameters → ProfileUpdater
```

防止 circular reinforcement 与 identifiability 污染。

---

# 18. StableProfile 作为 Appraisal Prior 时禁止 Double Counting

如果 StableProfile 被用于：

```text
StableProfile
↓
Appraisal prior
↓
Resolved Appraisal
↓
ModelContext
```

那么 Modeling Core 不再额外读取同一个 StableProfile feature。

原则：

> 同一 personalization evidence 只能通过明确的一条科学路径进入模型一次。

Core 最好只看到 resolved appraisal belief，不知道其 prior 是否来自 StableProfile。

---

# 19. ModelParameters 写权限

ModelParameters 只能通过：

```text
Estimation
↓
Validation
↓
Promotion
```

成为 active state。

Agent / ProfileUpdater / LLM 不能直接：

```text
beta_D += ...
g_A = ...
```

---

# 20. Context Access Architecture：避免 God ContextProvider

原版 `get_context(purpose)` 是有用的逻辑抽象，但不应实现成一个拥有所有数据和所有 consumer 规则的 God Service。

最终采用：

```text
Shared Context Access / Permission Semantics
            │
   ┌────────┼─────────┐
   ▼        ▼         ▼
Extraction  Policy    Conversation
Context     Context   Context
Builder     Builder   Builder
```

MODEL 路径单独使用 10 的：

```text
AsOf / Current Knowledge Read
↓
ModelContextBuilder
↓
Pure Scientific Core
```

Research 不通过 Online ContextProvider 做全部历史重放，而使用：

```text
AsOfKnowledgeReader
+
Research Runtime
```

因此：

```text
ContextProvider
```

在本文件中应理解为**一组 purpose-scoped context access contract**，而不是必须存在一个万能类。

---

# 21. Context Base Envelope

公共字段只保留真正通用内容：

```text
context_id
purpose
participant_id
reference_time
knowledge_cutoff
schema_version
generated_at
```

其他：

```text
profile version
parameter version
forecast version
policy version
```

应由 purpose-specific DTO 或消费 Artifact 保存。

避免所有 Context 都携带一堆无关版本。

---

# 22. ExtractionContext

Evidence Extractor 默认只看到：

```text
current source
small necessary nearby conversation
entity candidates / aliases
current time / timezone
allowed claim definitions
necessary temporal context
```

默认禁止：

```text
Forecast conclusion
A / B / S
Q values
PolicyEvaluation / ActionDecision
experiment assignment
future outcome
stable psychological inference irrelevant to grounding
```

原则：

$$
\boxed{
Extractor\ must\ not\ see\ the\ model\ conclusion\ it\ may\ later\ influence
}
$$

---

# 23. MODEL 路径

09 不再让 ContextProvider 直接“拥有 ModelContext”。

正式路径：

```text
Current / As-of Resolved Knowledge
↓
ModelContextBuilder
↓
ModelContext
↓
Pure Scientific Core
```

ModelContextBuilder 负责 typed scientific input construction。

它不能计算：

```text
Q_D / Q_P / Q_R
C_D / C_U / F
A / B / S
```

这些属于 Core。

---

# 24. PolicyContext

PolicyContext 服务：

```text
是否允许行动？
是否存在 care / sensing opportunity？
当前 interaction burden 如何？
用户偏好与 protected window 如何？
```

可能包含：

```text
ForecastSummary
current availability / receptivity
care preferences
quiet hours
recent action history
recent question burden
EpisodeContext
relevant topic state
```

Policy 不读取：

```text
raw model parameters
raw posterior internals
完整 internal state implementation
```

而消费稳定 Forecast Contract。

---

# 25. ConversationContext

ConversationContext 不是 UserProfile dump。

它只提供当前对话实际相关的：

```text
current topic
relevant Event / Task
relevant Appraisal
explicit preferences
selected supported personalization
QuestionIntent / ActionIntent
conversation-safe forecast semantics
```

Agent 默认不看到：

```text
raw model parameters
research gold labels
experiment hidden assignment
admin-only audit
```

---

# 26. Conversation-safe Forecast

Conversation Agent 可以获得：

```text
trend
peak window
support-relevant context
qualitative uncertainty
```

不能把 latent state 精确值作为“用户真实心理事实”。

如果用户明确请求压力曲线：

```text
Agent
↓
Forecast Tool / UI
↓
正式 Forecast Artifact
```

而不是 Agent 自己重算模型。

---

# 27. Research Access Classification

可以继续保留：

```text
RESEARCH
```

作为数据 access purpose / classification。

但不建立一个 Online `ResearchContextProvider` 来承担：

```text
Historical Replay
Dataset Construction
Evaluation
```

这些职责属于 10 的 Research Runtime。

---

# 28. Experiment Hidden Information

以下信息默认不暴露给 Conversation Agent：

```text
experiment assignment
randomization probability
annotation gold
synthetic hidden truth
future outcome
causal-analysis labels
```

未来 Experiment Controller 只把最终允许的：

```text
ActionIntent / Constraint
```

交给 Agent。

Agent 不需要知道：

```text
“这是 control participant”
```

---

# 29. Context Exposure Rule

每类信息的 Context exposure 需要同时满足：

```text
Purpose Permission
+
Epistemic / Quality Gate
+
Relevance
```

Context builder 不能因为字段存在就全部暴露。

但这些规则不全部塞进 08 的 ClaimDefinition。

09 可以维护 purpose-specific allowlist / policy configuration。

---

# 30. Baseline Context 与 MemoryRetriever 分离

这是 09 的核心设计之一。

```text
Resolved Knowledge
      │
      ├────────→ Baseline Context Builder
      │                   │
      │                   ▼
      │             Conversation Agent
      │                   ▲
      ▼                   │
Memory Materializer → MemoryRetriever
```

Baseline Context：

```text
small
structured
current
purpose-scoped
```

MemoryRetriever：

```text
Agent-initiated
iterative
historical
semantic-intent-driven
```

---

# 31. Broad Read, Narrow Write

Conversation Agent 在自己的 conversation-visible universe 中可以主动：

```text
搜索 Memory
打开历史 episode
读取 source window
查看 Event / Task / Preference
查询 Forecast Summary
搜索 Web
读取 URL
读取文档
理解图片
使用 presentation tool
```

但不能直接：

```text
写 Evidence
改 StableProfile
改 ModelParameters
直接 SQL UPDATE Domain State
改 experiment assignment
```

Canonical write 必须走受控路径。

---

# 32. Memory 的定位

Memory 是：

> Agent-facing historical retrieval artifact / index。

不是：

```text
Evidence source of truth
Model source of truth
StableProfile source of truth
```

Memory 必须尽可能可追溯到：

```text
source refs
evidence / knowledge refs
conversation refs
```

---

# 33. Memory 的两种来源

### Knowledge-backed Memory

来自：

```text
Event / Task / Recovery
Preference
StableProfile
resolved Knowledge
```

### Conversation-continuity Memory

来自：

```text
Conversation Source / Interaction History
↓
Episode Summary
```

用于：

```text
上次聊到哪里
已经解释过什么
双方约定继续什么
长期项目上下文
```

Conversation-continuity Memory 不要求硬造心理 Evidence。

---

# 34. Memory Summary 是 Versioned Derived Artifact

需要修正原先“Memory 都能简单重建”的说法。

Knowledge-backed memory 可以由 canonical state 重建。

但 Conversation-continuity summary 如果经过 LLM materialization，本身包含 versioned summarization decision。

因此 Memory Item 至少需要：

```text
memory_id
revision / hash
materialized_at
materializer / prompt version
source refs
```

这样未来才能知道 Agent 当时看的是哪一版摘要。

---

# 35. Memory 的主要粒度

优先使用：

```text
episode / topic / durable item
```

而不是每条 message 一个文件。

例如：

```text
一次考试准备
一个持续科研项目
一次 recovery episode
一个明显的 support interaction
```

Memory Item 可以在 active period 内产生新 revision。

---

# 36. V1 Memory 物理存储：Markdown 是策略，不是架构真理

当前服务器和研究规模下，不需要本地 embedding model / vector DB。

V1 推荐：

```text
memory/<participant>/
├── MEMORY.md
└── entries/
    ├── mem_xxx.md
    ├── mem_yyy.md
    └── ...
```

分类主要放在 YAML metadata：

```text
memory_type
domains
subjects
tags
event_time
status
basis
source_refs
```

这样避免：

```text
一个 item 同时属于 recovery + exam + conversation
```

时复制到多个目录。

少量稳定专题文件可以保留，但目录结构不是上层 Contract。

未来即使切换为 DB-backed index，上层 Agent 仍只依赖：

```text
memory_search
open_memory
follow_source
```

---

# 37. `MEMORY.md`

`MEMORY.md` 是导航页，不是完整 archive。

可以包含：

```text
Active / Recent Topics
Stable Explicit Preferences
Long-running Projects
Recent Significant Episodes
Index / links
```

长期历史由 search 获取。

---

# 38. Memory Retrieval V1

推荐：

```text
Index
+
Metadata
+
Lexical Search
+
Agent Query Expansion
+
Optional LLM Reranking
```

Claude Code 风格：

```text
查看索引
↓
形成检索假设
↓
rg / lexical search
↓
打开少量相关 item
↓
继续迭代
↓
必要时 follow raw source
```

不要求 local embedding inference。

---

# 39. Metadata 是辅助，不是检索牢笼

Agent 可以：

```text
按 subject / time / type 过滤
```

也可以：

```text
不知道分类时做 cross-category lexical search
```

禁止 hard-coded：

```text
if “考试” → 只能查 exam/ 目录
```

程序控制 access boundary，Agent 控制 retrieval intent。

---

# 40. Agent Query Expansion 与 Iterative Retrieval

用户：

```text
“明天考试又有点没底。”
```

Agent 可以扩展：

```text
考试
没底
不确定
没把握
范围
题型
准备不足
```

并多轮：

```text
search
→ inspect
→ search again
→ open memory
→ follow source
```

Memory 是 Agent historical exploration，不是一次性 top-k RAG。

---

# 41. Raw Conversation History

Agent 可以按需读取历史原话，但不默认加载全部聊天。

推荐：

```text
Memory Search
↓
Memory Item
↓
source_ref
↓
small related source window
```

这样同时保留性能和真实历史探索能力。

---

# 42. Memory Summary 与 StableProfile 的边界

Memory Materializer 可以写：

```text
episode factual summary
```

但不能自行跨 episode 生成：

```text
“用户通常考试前焦虑”
```

后者属于 StableProfile longitudinal inference。

正式禁止：

```text
Memory Summary
→ StableProfile canonical input
```

ProfileUpdater 应使用 canonical Evidence / Knowledge，而不是另一个 LLM summary。

---

# 43. Historical Memory 不等于 Current State

三个月前：

```text
“考试前不想被打扰”
```

不能自动成为：

```text
current_receptivity = LOW
```

Memory result 应尽量带：

```text
event_time
basis
currentness / status
```

历史可以作为：

```text
context
retrieval cue
conversation reference
```

而不是 current-state overwrite。

---

# 44. Replay 不直接使用今天的 Live Memory

这是与 10 对齐的重要修订。

Live Markdown Memory 可能已经吸收后来发生的信息或新版 summary。

因此 historical prospective replay 不能：

```text
直接打开今天的 MEMORY.md
```

而应：

```text
AsOf canonical Source / Knowledge
↓
Replay-specific historical view
```

或使用明确版本化、满足 cutoff 的 Memory revision。

研究 Replay 的 source of truth 仍是 canonical artifact store，不是 live Agent memory。

---

# 45. Memory Retrieval Provenance

Bot Turn 不能只记录：

```text
memory_id
```

因为同一个 memory item 后面可能被更新。

至少记录：

```text
memory_id
revision / hash
```

必要时再记录：

```text
search query
opened refs
retrieval result digest
```

不用复制整棵 Memory tree。

---

# 46. Tool Architecture

MindFlow Agent 需要支持：

```text
Memory Search
Web Search
URL Read
Document Read
Image Understanding
Forecast
Question Opportunity
Card Render / Send
Calendar / Task Commands
Preference Commands
...
```

统一使用轻量 Tool Capability / Effect Contract。

稳定字段优先是：

```text
tool identity
capability domain
effect kind
permission scope
audit policy
```

可以有：

```text
reads participant data?
reads external data?
external side effect?
mutation capability?
```

但不把所有 runtime 决策都硬编码成 Tool metadata Boolean。

---

# 47. Confirmation 与 Consistency 不是简单 Tool 常量

原版：

```text
requires_confirmation = bool
requires_strong_consistency = bool
```

过于僵硬。

例如同一个 `update_task`：

用户明确说：

```text
“把任务 X 标成完成。”
```

subject 唯一时可能无需额外确认。

而 Agent 推断：

```text
“看起来你已经做完了。”
```

只能 Proposal / confirmation。

所以 confirmation 由：

```text
command origin
risk
ambiguity
mutation scope
```

共同决定。

同理，一致性依赖：

```text
当前操作读取哪些 canonical domains
```

而不是简单 Tool 级全局 true/false。

---

# 48. Tool Effect 语义

可以保留以下分类：

```text
Local Context Read
Historical Retrieval
External Observation
Model / Decision Read
Presentation / Delivery
Domain / External Mutation
Research-only
```

它们是权限和审计语义，不要求每类都变成独立 Tool subsystem。

---

# 49. Tool Result 默认不是 Evidence

Tool 最多产生：

```text
Source Artifact
Tool Observation
EvidenceCandidate
```

不能直接：

```text
Tool → Evidence
```

绕过 Domain validation。

例如：

```text
Web Search
→ 某考试日期
```

默认只是 external observation。

只有经过：

```text
source authority validation
subject binding
Domain validation
```

才可能形成 Evidence。

---

# 50. Document / Image Tool

普通论文文档：

```text
read_document
→ temporary conversational context
```

不自动写长期 Memory。

课程表图片：

```text
image understanding
→ Event Candidate
→ validation
→ Evidence
```

运动截图：

```text
image understanding
→ Recovery Candidate
```

Vision / Document Tool 永远不直接改 canonical state。

---

# 51. Card Tool

必须区分：

```text
Render Card
Send Card
Care / Supportive Card
Card Interaction
```

Card Interaction：

```text
click / choice / submit
```

是新的 structured Source。

具体 Runtime persistence 在 10 中统一为：

```text
BotResponseUnit
BotActionEvent
```

09 不另建 Card-specific intervention ledger。

---

# 52. Tool-returned Content 没有 Instruction Authority

需要修正“所有 Memory 都是 external untrusted content”的表述。

更准确：

### External Untrusted Data

```text
Web
URL
external document
```

### Internal Derived Data

```text
Memory Summary
Knowledge View
```

二者 trust provenance 不同。

但共同原则是：

> 任何 Tool 返回正文都不能因为文本内容而提升权限、修改 system policy 或获得新的 write authority。

即：

```text
Content can inform reasoning
Content cannot grant authority
```

---

# 53. Agent Runtime 总体职责

Agent 可以：

```text
Read
Search
Reason
Respond
Propose
Use allowed tools
```

Harness 控制：

```text
participant isolation
context exposure
canonical writes
mutation validation
relevant consistency barriers
policy enforcement
experiment constraints
tool permission
audit provenance
```

Agent 控制：

```text
retrieval intent
tool choice within permission
reasoning
language
interaction strategy
```

---

# 54. Reactive Conversation 与 Knowledge Update 解耦

普通用户消息：

```text
User Message
↓
Persist Source
├───────────────┐
▼               ▼
Interactive     Evidence / Knowledge Update
Path            Path
```

Interactive Path 不需要等待完整 Evidence Pipeline。

Agent 可以直接理解当前 raw message。

Structured Evidence 则在 validation 后通过 `known_at` 进入模型 / Policy information set。

---

# 55. Current-Turn Overlay

Conversation Agent 每轮接收：

```text
current raw message
+
baseline conversation context
+
current-turn explicit overlay
```

当前用户明确指令优先于历史 Preference / Profile。

Current-Turn Overlay：

```text
只影响当前 turn
```

不建设新的数据库层。

Evidence Pipeline 再决定它属于：

```text
EpisodeContext
Durable Preference
Domain correction
```

中的哪一种。

---

# 56. Relevant Canonical State Barrier

原来的 `STRONG Consistency Barrier` 修订为更精确的：

```text
Relevant Canonical State Barrier
```

当某操作依赖当前 turn 最新 structured state 时，只等待：

```text
相关 Source extraction
相关 Evidence commit
相关 Domain resolution
该 operation 必需 projection
```

不等待：

```text
Memory Markdown refresh
StableProfile background update
Research indexing
unrelated Domain updates
```

例如：

```text
“考试取消了，重新算明天压力。”
```

必须等 Event correction 进入 canonical knowledge 后再 Forecast。

---

# 57. Eventual Path

普通：

```text
conversation
memory recall
general support
non-state-sensitive reads
```

可以使用 eventual knowledge update。

但如果正式需要：

```text
fresh Forecast
material PolicyEvaluation
state-dependent mutation
Research Snapshot
```

则使用 Relevant Canonical State Barrier。

---

# 58. Normal Follow-up 与 Model-driven Information Acquisition

普通自然追问：

```text
用户：“明天有考试。”
Agent：“准备得怎么样了？”
```

可以只是正常 Conversation。

如果 Agent 明确因为模型缺失：

```text
U_perc UNKNOWN
```

而希望收集该变量，则进入：

```text
QuestionOpportunity / InformationAcquisitionPolicy
↓
QuestionIntent
↓
Conversation Agent wording
```

---

# 59. QuestionIntent

QuestionIntent 控制：

```text
问什么
```

Agent 控制：

```text
怎么问
```

如果：

```text
target = U_perc
```

Agent不能顺手把主要目标扩展成：

```text
importance + sleep + C_out
```

除非用户主动继续展开正常对话。

---

# 60. 一层追问是 V1 Policy 默认，不是系统不变量

原版：

```text
一次主动分享
→ 最多一个 model-driven 主动追问
```

保留为：

> 当前 V1 的默认 `PolicySpec`，用于控制 interaction burden。

不把它写成架构 invariant。

Pilot 后可以根据：

```text
burden
compliance
conversation quality
information value
```

调整。

---

# 61. SENSE 也是可观测 Interaction Exposure

Model-driven sensing 不只是“采数据”。

QuestionIntent 最终生成 BotResponseUnit 时，应能够记录：

```text
semantic_role = SENSING
question_intent_ref
elicitation provenance
```

这样 10 的 Research Dataset 可以识别：

```text
active_sensing_in_window
```

用于研究 measurement reactivity / co-intervention。

---

# 62. Agent 可以主动申请 Question Opportunity

Agent 可以发现：

```text
这里有一个高价值 unresolved claim
```

然后调用受控 decision tool 请求：

```text
是否值得问？
```

Policy 不能盲信 Agent 的任意心理变量名称。

它必须把 request 映射到 registered claim / subject，并检查：

```text
relevance
burden
receptivity
permission
```

---

# 63. Domain / External Mutation

Agent 不拥有直接 DB mutation 权限。

### 用户显式命令

```text
“把任务 X 标记完成。”
```

subject 清晰、风险低时，可以：

```text
Explicit Command
↓
Domain Command
↓
Execute
↓
Source / Evidence
```

### Agent 推断

```text
“看起来你应该做完了。”
```

只能：

```text
ActionProposal / Clarification
```

不能直接改 Task。

---

# 64. Bot Turn Provenance

普通 Bot Response 不需要保存完整 Prompt dump。

至少能追踪：

```text
bot_response_unit_id / message refs
conversation context version/ref
QuestionIntent if any
ActionIntent if any
memory refs + revision/hash
tool calls / result refs
agent version
prompt version
created_at
```

具体 ResponseUnit / ActionEvent persistence 在 10 定义。

---

# 65. Memory / Profile 更新不阻塞普通对话

更新速度分层：

### Fast

```text
Current Domain Projection
EpisodeContext
```

### Memory

根据 episode / meaningful interaction materialize。

### StableProfile

远慢于 Evidence。

只在：

```text
episode close
sufficient independent evidence
periodic / explicit profile update trigger
```

时更新。

原则：

$$
UpdateRate_{Profile} < UpdateRate_{Evidence}
$$

但不要求为了实现它建设异步队列平台。

“慢”表示不在每条 message 上重算。

---

# 66. StableProfile 防抖

禁止：

```text
Episode 1 positive → Profile positive
Episode 2 negative → Profile negative
Episode 3 positive → Profile positive
```

证据不足时保持：

```text
INSUFFICIENT / TENTATIVE
```

而不是快速翻转长期 personalization。

---

# 67. Proactive Runtime 与 10 对齐

原版：

```text
PolicyDecision
→ Experiment Constraint
```

已被 10 修正。

最终链：

```text
Trigger / Opportunity
↓
Relevant Canonical State Barrier
↓
ForecastRun / ForecastSummary
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
ConversationContext / ActionIntent
↓
Conversation Agent
↓
BotResponseUnit
↓
BotActionEvent
```

其中：

```text
Policy / Experiment decides IF / WHAT ACTION
Agent decides HOW TO EXPRESS
```

Agent不能改变 ActionDecision 的 action class。

---

# 68. Reactive 与 Proactive 权限不同

### Reactive

用户主动发起。

Agent具有较高语言、检索与工具自主性。

普通响应不需要创建 proactive PolicyEvaluation。

### Proactive

系统主动发起。

必须存在：

```text
Opportunity / PolicyEvaluation / ActionDecision
```

未来如进入随机实验，再有：

```text
ExperimentAssignment
```

Agent不能独立决定：

```text
“我觉得用户压力高，所以主动发一条。”
```

---

# 69. Tool 与 Proactive Action

Agent即使在 proactive turn 中可以：

```text
搜索相关 Memory
查看允许的 Event / Task
使用 presentation tool
```

也不能通过历史检索改变：

```text
ActionDecision = CARE
```

为另一个未经 Policy 授权的 action class。

Memory 主要用于：

```text
个性化 wording
选择相关背景
避免重复
```

需要更换 action 时，应重新进入 Policy evaluation，而不是 Agent 自行 override。

---

# 70. Graceful Degradation

### MemoryRetriever Failure

普通 Conversation 继续，只降低 personalization。

### StableProfile unavailable

Conversation 继续，使用 current context / explicit preference。

### Noncritical Evidence Extraction failure

普通 Reactive Response 可以继续，并记录 knowledge update failure。

### State-sensitive operation

如果用户明确要求：

```text
“根据我刚说的最新情况重新计算。”
```

而 relevant canonical update 失败：

```text
不得 silent fallback 到 stale forecast
```

应明确失败 / 降级。

---

# 71. Agent / Harness 最终职责边界

## Harness / Application 必须控制

```text
participant isolation
purpose-scoped context access
source persistence
evidence commit
canonical write validation
Relevant Canonical State Barrier
snapshot creation
PolicyEvaluation
Experiment constraint
ActionDecision
mutation authority
tool permission
audit provenance
```

## Agent 可以自主决定

```text
如何理解自然语言
是否搜索历史
搜索什么
如何迭代搜索
打开哪些相关 memory
是否调用允许的 read / external tools
如何组织回答
如何自然表达 QuestionIntent / ActionIntent
```

---

# 72. 不继续增加智能中间层

当前不引入：

```text
Memory Planner
Semantic Router
Context Reasoner
Profile Controller
Tool Intelligence Layer
```

优先采用：

```text
clear typed contracts
+
Agent reasoning
+
small deterministic harness
```

减少重复状态与过度抽象。

---

# 73. 本文件最终架构决策

1. Personalization 是语义 ownership，不等于一类一表、一类一 Service。
2. UserFacts 不复制 Event / Task / Recovery 等已有 Domain 事实。
3. Durable UserPreference 主要来自用户 explicit / confirmed input，不根据短期行为自动写死。
4. SelfDeclaredTendency 与 longitudinal StableProfile 分离。
5. Appraisal 必须绑定具体 subject / episode。
6. EpisodeContext 是短期 runtime projection，不升级成 durable profile。
7. StableProfile 专指 repeated-evidence longitudinal pattern，不是 personality truth。
8. StableProfile 必须带 Scope，generalization 不超过 evidence coverage。
9. Profile support 以 independent episode 为主，不按 Evidence row 数量机械累计。
10. 当前阶段优先冻结 StableProfile Contract 与 provenance，不急于在无 Pilot 依据下自动 promotion。
11. StableProfile 与 ModelParameters 平行派生，Core v1 不互相喂数据。
12. StableProfile 如用于 Appraisal prior，必须防止同一信息再次直接进入 Modeling Core。
13. ContextProvider 不实现为 God Service；使用 purpose-specific typed context builders / access contract。
14. MODEL 路径正式由 `Knowledge Read → ModelContextBuilder → Pure Core` 负责。
15. Research historical replay 不依赖 Online ContextProvider。
16. Context common envelope 只保存真正通用字段，consumer-specific version 放对应 DTO / Artifact。
17. ExtractionContext 保持 minimum necessary，并隔离可能自证的模型结论。
18. ConversationContext 不是 Profile dump，只暴露当前 relevant / allowed context。
19. Experiment hidden info 不暴露给 Agent。
20. Baseline Context 与 Agent-driven MemoryRetriever 分离。
21. Conversation Agent 使用 `Broad Read, Narrow Write`。
22. Memory 是 Agent-facing derived artifact，不是 Model / Profile / Evidence source of truth。
23. Memory 来源分 Knowledge-backed 与 Conversation-continuity 两类。
24. Conversation-continuity summary 需要 revision/hash/materializer provenance，不能只当可随意重建文本。
25. V1 Markdown + lexical retrieval 是存储策略，不是上层架构绑定。
26. V1 推荐 `MEMORY.md + entries/*.md + YAML metadata`，避免过细目录产生复制问题。
27. Memory retrieval 支持 Agent query expansion 与 iterative exploration，不依赖本地 embedding。
28. Historical Memory 不自动成为 Current State。
29. Prospective Research Replay 不能盲读今天的 live Memory。
30. Bot Turn 对 Memory 使用应记录 `memory_id + revision/hash`。
31. Tool 使用统一轻量 Capability / Effect Contract，不把所有 runtime 决策硬编码成 Tool boolean metadata。
32. Confirmation 依赖 explicit command / inferred proposal、risk、ambiguity；Consistency 依赖 relevant canonical domains。
33. Tool Result 默认只是 Source / Observation / Candidate，不能绕过 validation 直接成为 Evidence。
34. Web / URL / Document 等 external content 没有 instruction authority；内部 Memory 同样不能通过正文提升权限。
35. Reactive Conversation 与 Knowledge Update 解耦，Agent 可以直接看当前 raw message。
36. Current-Turn Overlay 只影响当前 turn，不形成新持久状态层。
37. 强一致收缩为 `Relevant Canonical State Barrier`，不等待 Memory / Profile / Research background work。
38. 普通 conversational follow-up 不强制经过 Question Policy；model-driven acquisition 必须有 QuestionIntent。
39. 一次主动分享最多一个 model-driven follow-up 是 V1 Policy 默认，而不是系统不变量。
40. SENSE 需要作为可研究的 interaction exposure 留下 role / QuestionIntent provenance。
41. Agent 可以主动申请 Question Opportunity，但 Policy 必须校验 target claim、burden 和 relevance。
42. Agent 不直接 mutation canonical DB；explicit user command 与 Agent inferred action 使用不同 authority。
43. Bot Turn 保存必要 context / memory / tool provenance，不保存巨大 prompt dump。
44. StableProfile / Memory update 不阻塞普通 conversation。
45. Proactive Runtime 使用 `PolicyEvaluation → optional ExperimentAssignment → ActionDecision → Final Guard → BotResponseUnit`。
46. Reactive ordinary answer 不为了形式统一强制造 PolicyEvaluation。
47. Proactive action class 由 Policy / Experiment 决定，Agent只负责表达与授权范围内的检索/tool use。
48. 非关键 Memory / Profile / extraction failure 应 graceful degrade；state-sensitive operation 不得 silent 使用 stale state。
49. Harness 控制权限、写入、Policy、Experiment 与一致性；Agent控制 reasoning、retrieval、tool selection 和 language。
50. 不再增加新的智能 Router / Planner 层。

---

# 74. 与其他架构文档的边界

## 08：Data / Knowledge / Provenance

负责：

```text
Source
Evidence
known_at
Correction
Resolver
Current Domain Projection
ContextSnapshot
Extraction
```

09 不重新定义 Evidence。

## 10：Runtime / Research / Experiment

负责：

```text
ForecastRun
PolicyEvaluation
ActionDecision
BotResponseUnit
BotActionEvent
MeasurementRequest
Study / Experiment
Research Dataset
ModelContextBuilder / Pure Core sharing
```

09 的 Proactive 链以 10 的术语为准。

## 11：Project Module Boundaries and Engineering Rules

负责把本文件中的逻辑边界落实为：

```text
package ownership
ports / adapters
allowed imports
Tool Registry implementation
Memory storage adapter
transactions
architecture tests
configuration ownership
```

## 12：Repository Refactor and Migration Plan

基于当前真实仓库确定：

```text
现有 Agent runtime 如何迁移
Memory Markdown 放在哪里
哪些 ToolEffect 复用
哪些旧 service 合并 / 拆分
如何 staged cutover
```

---

# 75. 本层核心架构

最终可以压缩为：

```text
Canonical Knowledge
      │
      ├────────→ Purpose-specific Baseline Context
      │                       │
      │                       ▼
      │               Conversation Agent
      │                  ▲          │
      ▼                  │          ▼
Memory Materializer → MemoryRetriever   Allowed Tools
      │                             │
      └─────────────────────────────┘
```

Agent 的写入仍然返回：

```text
Conversation / Tool Result
↓
Source / Proposal / EvidenceCandidate
↓
Controlled Canonical Path
```

而模型路径严格保持：

```text
Canonical Knowledge
↓
ModelContextBuilder
↓
Pure Scientific Core
```

最终原则：

$$
\boxed{
让 Agent 拥有足够的历史探索、工具使用和语言自主性，
但不让它成为事实源、模型状态写入者或实验控制器。
}
$$
