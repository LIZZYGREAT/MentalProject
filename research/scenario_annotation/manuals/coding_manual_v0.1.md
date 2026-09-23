# MindFlow Scenario Annotation Coding Manual v0.1

状态：Calibration draft  
适用 schema：1.0  
适用轮次：CALIBRATION

## 1. 使用边界

本手册只编码在 `known_at_cutoff` 前可见的表示层事实与证据。它不编码压力真值、EMA、
`A/B/S`、`Q_D/Q_P/Q_R/Q_BS`、预测风险、支持效果，也不把事件事实直接变成心理状态。

证据强度与标注者信心必须分别填写。`UNKNOWN` 表示 construct 适用但证据不足；`N/A`
表示 construct 对当前 target 不适用；`AMBIGUOUS` 表示可见证据冲突或支持多个合理解释。
Module B 使用 `NO_EVIDENCE` 表示没有 participant-specific appraisal evidence。

Unknown reason 只能取：`NOT_MENTIONED`、`INSUFFICIENT_DETAIL`、
`CONFLICTING_EVIDENCE`、`TEMPORAL_SCOPE_UNCLEAR`、`TARGET_UNCLEAR`、
`SOURCE_UNRELIABLE`、`OTHER`。Annotator confidence 只能取 `LOW/MEDIUM/HIGH`。

## 2. Scenario QA（每个 Module 均先完成）

- `scenario_valid`: `YES/NO/UNCERTAIN`。事实是否足以构成可标注场景。
- `scenario_plausibility`: `HIGH/MEDIUM/LOW`。场景在大学生生活中是否现实。
- `contradiction_present`: `YES/NO`。证据是否真正冲突；冲突不自动使场景无效。

发现 `known_at > known_at_cutoff`、hidden metadata、无法解释的时间重叠或事实自相矛盾时，
不得自行修正输入，应报告 QA 问题。

## 3. Module A — Event / Lifecycle / Exposure / Obligation / Recovery

Module A 的时间事实字段为 `SCHEDULED_START`、`SCHEDULED_END`、`ACTUAL_START`、`ACTUAL_END` 与 `DEADLINE`。

### A1. EVENT_FAMILY

- **Unit of Annotation:** 一个 `target_event_ref`。
- **Definition:** 事件在第一版表示层中的顶层事实类别。
- **Question to Annotator:** 当前可见事实最直接支持哪一类事件？
- **Allowed Labels:** `COURSE/TASK/STRUCTURED_EVENT/RECOVERY_ACTIVITY/SLEEP/NAP/CONSEQUENCE_EVENT/OTHER/UNKNOWN`。
- **Use When:** 事件本体和主要功能可由日程、任务记录或明确文本识别。
- **Do NOT Infer From:** 压力程度、participant 喜好、未来结果或 generator 意图。
- **UNKNOWN Rule:** 连事件本体都不能可靠判定；填写原因。
- **N/A Rule:** 不使用；有 target 时 family 总是适用。
- **Positive Anchor:** “课表中的高数课” → `COURSE`。
- **Counterexample:** “今晚有安排”但无更多信息 → `UNKNOWN`，不是 `OTHER`。
- **Minimal-Pair Example:** “学院讲座”与“必须提交的课程报告”只改变事件功能，分别为 `STRUCTURED_EVENT` 与 `TASK`。

### A2. EVENT_SUBTYPE

- **Unit of Annotation:** 一个已识别 family 的事件。
- **Definition:** family 内受控 subtype；未知但明确存在的 subtype 使用 `OTHER:<free-text>`。
- **Question to Annotator:** 当前事件在 family 内是什么具体类型？
- **Allowed Labels:** Course: `lecture/lab/seminar/course_presentation`；Task: `assignment/exam_preparation/report/paper/research_task/project/administrative`；Structured: `meeting/presentation/interview/competition/appointment`；Recovery: `exercise/leisure/walk/entertainment/social_recovery/meal_break/short_rest`；或 `OTHER:<text>/UNKNOWN`。
- **Use When:** subtype 有明确描述。
- **Do NOT Infer From:** 课程名称、时长或主观难度所暗示但未陈述的活动形式。
- **UNKNOWN Rule:** family 已知但 subtype 证据不足。
- **N/A Rule:** `SLEEP/NAP/CONSEQUENCE_EVENT` 无需细分时使用 `N/A`。
- **Positive Anchor:** “实验课” → `lab`。
- **Counterexample:** “计算机课”不自动是 `lab`。
- **Minimal-Pair Example:** 同一课程明确写“讲授”或“实验操作”时分别为 `lecture/lab`。

### A3. EVENT_FACTS

- **Unit of Annotation:** 单个事实字段：`SCHEDULED_START/END`、`ACTUAL_START/END`、`DEADLINE`、`PROGRESS`、`ESTIMATED_TOTAL_EFFORT`、`REMAINING_EFFORT`、`PARENT_RELATION`、`CANCELLATION`。
- **Definition:** 来源中直接给出的事实，不做 ordinal 心理推断。
- **Question to Annotator:** 在 cutoff 前是否有可引用的明确事实值？
- **Allowed Labels:** 原始 timestamp/number/ref；或 `UNKNOWN/N/A`。
- **Use When:** 日历、任务记录或明确陈述提供事实。
- **Do NOT Infer From:** 默认课时、典型工作量、deadline proximity、情绪或之后发生的事件。
- **UNKNOWN Rule:** 字段适用但未提供或精度不足。
- **N/A Rule:** 字段对该事件不适用，如无 deadline 的散步。
- **Positive Anchor:** “作业截止 9 月 9 日 20:00”按原时间记录。
- **Counterexample:** “今晚要交”在日期上下文不足时不得自行补日期。
- **Minimal-Pair Example:** `actual 10:00–10:25` 与“上了约一半”分别记录 interval 与 fraction evidence，不互相转换。

### A4. LIFECYCLE

- **Unit of Annotation:** 一个事件 episode。
- **Definition:** 事件到 cutoff 时真实发生/推进到的状态。
- **Question to Annotator:** 可见事实直接支持哪个 lifecycle state？
- **Allowed Labels:** Course `SCHEDULED/ATTENDED/PARTIAL/SKIPPED/CANCELLED/UNKNOWN`；Task `PLANNED/OPEN/IN_PROGRESS/BLOCKED/COMPLETED/CANCELLED/OVERDUE/SUPERSEDED/UNKNOWN`；Recovery/Sleep/Nap `PLANNED/OCCURRED/PARTIAL/SKIPPED/CANCELLED/UNKNOWN`。
- **Use When:** 有明确 attendance、progress、completion、cancellation 或 occurrence 证据。
- **Do NOT Infer From:** scheduled 即 attended、计划即发生、行为意图即 completion、future correction。
- **UNKNOWN Rule:** 可选状态间无法可靠区分。
- **N/A Rule:** 不使用；event target 均有 lifecycle。
- **Positive Anchor:** “今天高数我明确没去” → Course `SKIPPED`。
- **Counterexample:** 课表存在但没有 attendance evidence → `SCHEDULED`，不是 `ATTENDED`。
- **Minimal-Pair Example:** 同一 scheduled course，一个有签到记录，一个没有 realized evidence。

### A5. OBLIGATION_EXISTS

- **Unit of Annotation:** 一个 event/task 与其未来行动承诺。
- **Definition:** 同时存在 Commitment、Future Action、Unresolved 才是 active obligation。
- **Question to Annotator:** cutoff 时是否存在明确、未解决的未来行动承诺？
- **Allowed Labels:** `YES/NO/UNKNOWN`。
- **Use When:** 有提交、补做、准备、赴约等明确承诺。
- **Do NOT Infer From:** missed event、焦虑、重要性、deadline 本身或一般建议。
- **UNKNOWN Rule:** 未来行动或 commitment 不明确。
- **N/A Rule:** 不使用；问题对事件可判断。
- **Positive Anchor:** “我周五前必须补交实验报告” → `YES`。
- **Counterexample:** “今天课没去”而无补做承诺 → `NO`。
- **Minimal-Pair Example:** missed course 后分别有/无“今晚补录像”的明确计划。

### A6. OBLIGATION_STATUS

- **Unit of Annotation:** 一个 obligation。
- **Definition:** obligation 到 cutoff 的执行状态。
- **Question to Annotator:** 已确认的 obligation 当前处于哪个状态？
- **Allowed Labels:** `OPEN/IN_PROGRESS/BLOCKED/COMPLETED/CANCELLED/SUPERSEDED/UNKNOWN/N/A`。
- **Use When:** `OBLIGATION_EXISTS=YES` 或证据支持可能 obligation。
- **Do NOT Infer From:** 时间流逝、低进度或情绪；deadline 过期且仍继续才不是自动 cancelled。
- **UNKNOWN Rule:** obligation 存在但状态不清楚。
- **N/A Rule:** `OBLIGATION_EXISTS=NO`。
- **Positive Anchor:** “已开始写，完成一半” → `IN_PROGRESS`。
- **Counterexample:** “卡住了”但仍可自行继续且无依赖证据，不自动 `BLOCKED`。
- **Minimal-Pair Example:** 同一未完成任务，一个等待导师数据，一个只是尚未开始。

### A7. PARENT_RELATION

- **Unit of Annotation:** 一个 obligation node。
- **Definition:** 当前 node 与显式父 obligation 的结构关系。
- **Question to Annotator:** 是否有明确父任务引用？
- **Allowed Labels:** `NONE/PARENT_REF/UNKNOWN`（`PARENT_REF` 时 label 或 notes 中给出 ref）。
- **Use When:** 场景明确给出 project → subtask 关系。
- **Do NOT Infer From:** 标题相似、同一课程或时间邻近。
- **UNKNOWN Rule:** 可能有层级但无法确定。
- **N/A Rule:** 非 obligation event 可记 `N/A`。
- **Positive Anchor:** “毕业设计/完成文献综述”且有 parent_ref → `PARENT_REF`。
- **Counterexample:** 同一门课的两份作业不自动为父子。
- **Minimal-Pair Example:** 相同子任务文本，一版明确属于项目，一版为独立任务。

### A8. ACTIVE_LEAF

- **Unit of Annotation:** obligation tree 中一个 node。
- **Definition:** 当前 unresolved 且无其他 active child 代替其计数的叶节点。
- **Question to Annotator:** 该 node 是否应作为唯一 active leaf 计入？
- **Allowed Labels:** `YES/NO/UNKNOWN/N/A`。
- **Use When:** obligation 及 parent-child 结构可见。
- **Do NOT Infer From:** parent 和 child 均 unresolved 就同时标 YES。
- **UNKNOWN Rule:** 层级或 child 状态不清。
- **N/A Rule:** 非 obligation。
- **Positive Anchor:** 活跃子任务标 `YES`，其概括性 parent 标 `NO`。
- **Counterexample:** project 与其唯一进行中子任务不可默认都 `YES`。
- **Minimal-Pair Example:** parent 无 child 与 parent 有 active child。

### A9. D_POT

- **Unit of Annotation:** 单位暴露期间的 event potential。
- **Definition:** 不考虑个体能力、在意程度、焦虑、duration 或 deadline proximity 的认知/执行/体力 demand intensity。
- **Question to Annotator:** 该活动每单位暴露本身要求多高？
- **Allowed Labels:** `LOW/MEDIUM/HIGH/UNKNOWN/N/A`。
- **Use When:** activity requirements、objective complexity、cognitive/execution/physical demand 可见。
- **Do NOT Infer From:** participant 擅长与否、情绪、importance、时长、deadline。
- **UNKNOWN Rule:** activity 内容不足以判 band。
- **N/A Rule:** 无 demand 机制的 target。
- **Positive Anchor:** 高强度实验操作可为 `HIGH`。
- **Counterexample:** 两小时轻松电影不因 duration 长变 `HIGH`。
- **Minimal-Pair Example:** participant competence 改变但 activity requirements 相同，`D_POT` 不变。

### A10. U_CONTEXT

- **Unit of Annotation:** event/task 的结构信息状态。
- **Definition:** requirements、dependency、rule、outcome mechanism 的客观结构性不确定性。
- **Question to Annotator:** 对任何处于该 context 的人，结构上有多少尚未确定？
- **Allowed Labels:** `LOW/MEDIUM/HIGH/UNKNOWN/N/A`。
- **Use When:** 要求是否清楚、依赖是否解决、评分/规则是否已知。
- **Do NOT Infer From:** “我很慌”、能力感或 participant 主观拿不准。
- **UNKNOWN Rule:** 没有足够结构信息。
- **N/A Rule:** 不存在 uncertainty mechanism 的 target。
- **Positive Anchor:** 评分规则、题目范围和依赖均未公布 → `HIGH`。
- **Counterexample:** 规则完全明确但 participant 说“我还是没底” → `LOW`；后者属于 `U_PERC`。
- **Minimal-Pair Example:** 保持 participant 文本相同，只改变是否公布评分规则。

### A11. D_S

- **Unit of Annotation:** event 的 social-evaluative structure。
- **Definition:** 是否存在公开展示或他人评价结构，以及它是否为核心机制。
- **Question to Annotator:** 当前活动是否被他人直接评价/公开展示？
- **Allowed Labels:** `ABSENT/PRESENT/STRONG/UNKNOWN/N/A`。
- **Use When:** presentation、defense、interview、oral exam 或明确评价。
- **Do NOT Infer From:** participant importance、社交偏好、紧张程度。
- **UNKNOWN Rule:** 是否有评价结构不清楚。
- **N/A Rule:** target 不构成可评价活动。
- **Positive Anchor:** 公开答辩 → `STRONG`。
- **Counterexample:** 独自写高权重论文不因 stakes 高变 `STRONG`。
- **Minimal-Pair Example:** 同一报告只提交文档 vs 公开口头展示。

### A12. R_POT

- **Unit of Annotation:** recovery activity 的 affordance。
- **Definition:** 活动提供 detachment、relaxation、autonomy、positive connection 或 workload 外 mastery 的潜力。
- **Question to Annotator:** 不看之后是否好转，这类活动本身提供多少 recovery opportunity？
- **Allowed Labels:** `LOW/MEDIUM/HIGH/UNKNOWN/N/A`。
- **Use When:** 明确活动内容可判断 recovery affordance。
- **Do NOT Infer From:** future helpfulness、mood improvement、participant 喜欢或有空。
- **UNKNOWN Rule:** 活动内容不足。
- **N/A Rule:** 非 recovery-relevant target。
- **Positive Anchor:** 自主、无干扰的轻松散步可为 `HIGH`。
- **Counterexample:** 日历空白不是 recovery activity。
- **Minimal-Pair Example:** 同一空闲窗口，一版明确散步，一版只写“没安排”。

### A13. M_CONTEXT

- **Unit of Annotation:** recovery activity episode 的环境兼容性。
- **Definition:** 当前环境是否允许 activity 充分实现其 recovery opportunity。
- **Question to Annotator:** 即使活动发生，当前 context 会不会明显打断或挤压恢复？
- **Allowed Labels:** `POOR/PARTIAL/GOOD/UNKNOWN/N/A`。
- **Use When:** 可见 uninterrupted time、相邻课程、噪声、控制权等 context。
- **Do NOT Infer From:** participant 事后感觉有用与否（`F_REC`）。
- **UNKNOWN Rule:** 环境条件不足。
- **N/A Rule:** 非 recovery activity。
- **Positive Anchor:** 无打扰、时间充足的小睡 → `GOOD`。
- **Counterexample:** 夹在两节课之间 10 分钟的小睡不因发生就为 `GOOD`。
- **Minimal-Pair Example:** 同一 nap，一版 90 分钟无打扰，一版 15 分钟且需赶课。

### A14. MECHANISM EXPOSURE

- **Unit of Annotation:** `EXECUTION_EXPOSURE/DEADLINE_EXPOSURE/UNCERTAINTY_EXPOSURE/SOCIAL_EXPOSURE/RECOVERY_OCCURRENCE` 中一个机制 gate。
- **Definition:** lifecycle 对具体 mechanism 的 realized activation，不是统一 lifecycle multiplier。
- **Question to Annotator:** 截止当前，该机制真实 active 到什么程度？
- **Allowed Labels:** `INACTIVE/ACTIVE/PARTIAL/UNKNOWN/N/A`。
- **Use When:** realized participation、deadline obligation、uncertainty exposure、social evaluation 或 recovery occurrence 有证据。
- **Do NOT Infer From:** scheduled、event potential、appraisal、压力或 future outcome。
- **UNKNOWN Rule:** mechanism 适用但 realized evidence 不足。
- **N/A Rule:** mechanism 对事件不适用。
- **Positive Anchor:** 实际参加整节课 → execution `ACTIVE`。
- **Counterexample:** scheduled course 无 attendance evidence → execution `UNKNOWN`，不是 `ACTIVE`。
- **Minimal-Pair Example:** 同一 course，实际区间 25 分钟 vs 只说“约一半”。

### A15. PARTIAL_ENCODING_BASIS

- **Unit of Annotation:** 任一 `PARTIAL` exposure。
- **Definition:** partial fact 的唯一主要编码路径。
- **Question to Annotator:** partial 是由实际区间、仅比例还是其他明确证据表达？
- **Allowed Labels:** `ACTUAL_INTERVAL/FRACTION_ONLY/OTHER_EXPLICIT`。
- **Use When:** exposure label 为 `PARTIAL`。
- **Do NOT Infer From:** 同时使用 actual interval 与 fraction 重复表达同一事实。
- **UNKNOWN Rule:** 不允许；无法判断 basis 时 exposure 应为 `UNKNOWN`。
- **N/A Rule:** exposure 非 `PARTIAL` 时字段省略。
- **Positive Anchor:** `actual 10:00–10:25` → `ACTUAL_INTERVAL`。
- **Counterexample:** 已有实际区间仍额外填 0.25 fraction。
- **Minimal-Pair Example:** 精确离场时间 vs “大概上了一半”。

### A16. DEADLINE_SCARCITY_BAND

- **Unit of Annotation:** 专门的 deadline mapping validation target。
- **Definition:** remaining effort、deadline、available capacity 的定性 ordering 检查，不是模型 Ground Truth。
- **Question to Annotator:** 仅在场景明确要求 mapping validation 时，资源相对截止期是否稀缺？
- **Allowed Labels:** `LOW/MEDIUM/HIGH/UNKNOWN/N/A`。
- **Use When:** scenario 明确标示此字段，且三类事实可见。
- **Do NOT Infer From:** 焦虑、importance 或单独的 deadline proximity。
- **UNKNOWN Rule:** capacity/effort/deadline 任一关键事实不足。
- **N/A Rule:** 普通场景默认 `N/A`。
- **Positive Anchor:** 还需 8 小时、2 小时后截止、仅有 1 小时可用 → `HIGH`。
- **Counterexample:** “明天截止”但 remaining work 未知不能自动 `HIGH`。
- **Minimal-Pair Example:** deadline 不变，只改变 remaining effort。

## 4. Module B — Personal Appraisal Evidence

Module B 每个 dimension 单独一条 record。Directional label 必须引用逐字 `evidence_span`。
仅 Event metadata 时，label 必须为 `NO_EVIDENCE` 且 evidence strength 为 `N/A`。

### B1. C_EXEC

- **Unit of Annotation:** participant 对一个 episode/event class/general situation 的执行能力陈述。
- **Definition:** “我能不能处理/执行这件事”的 participant-specific evidence。
- **Question to Annotator:** 说话者是否表达自己完成或应对该行动的把握？
- **Allowed Labels:** `LOW/MEDIUM/HIGH/NO_EVIDENCE/AMBIGUOUS`。
- **Use When:** 直接回答 probe 或明确 episode-specific 能力陈述。
- **Do NOT Infer From:** objective difficulty、progress、deadline、专业背景、EMA。
- **NO_EVIDENCE Rule:** 没有可引用的 participant-specific 能力证据。
- **N/A Rule:** 不使用；用 `NO_EVIDENCE`。
- **Positive Anchor:** “按这个步骤我肯定能在今晚做完” → `HIGH`。
- **Counterexample:** “这题公认很难” → `NO_EVIDENCE`。
- **Minimal-Pair Example:** task facts 相同，只改变“我有把握/我完全不会”。

### B2. IMPORTANCE

- **Unit of Annotation:** participant 对事件或结果重要性的陈述。
- **Definition:** personal importance，不是 objective stakes。
- **Question to Annotator:** participant 是否说这件事/结果对自己有多重要？
- **Allowed Labels:** `LOW/MEDIUM/HIGH/NO_EVIDENCE/AMBIGUOUS`。
- **Use When:** 明确在意、不在意、优先级或个人后果陈述。
- **Do NOT Infer From:** 必修、学分、deadline、presentation 或他人评价。
- **NO_EVIDENCE Rule:** 只有客观 stakes 而无 personal evidence。
- **N/A Rule:** 不使用。
- **Positive Anchor:** “这次奖学金评定对我非常重要” → `HIGH`。
- **Counterexample:** “这是 4 学分必修课” → `NO_EVIDENCE`。
- **Minimal-Pair Example:** objective stakes 相同，只改变“我很在意/对我无所谓”。

### B3. C_OUT

- **Unit of Annotation:** participant 对结果可控性的陈述。
- **Definition:** “我还能多大程度影响结果”，不同于能否执行动作。
- **Question to Annotator:** participant 是否表达自己的行动能否改变最终结果？
- **Allowed Labels:** `LOW/MEDIUM/HIGH/NO_EVIDENCE/AMBIGUOUS`。
- **Use When:** 结果已定、仍可挽回、取决于自己或外部随机性的明确文本。
- **Do NOT Infer From:** `C_EXEC`、进度、objective uncertainty 或 deadline。
- **NO_EVIDENCE Rule:** 只有执行把握，没有 outcome controllability evidence。
- **N/A Rule:** 不使用。
- **Positive Anchor:** “内容我能写，但老师已经定分，怎么改也没用” → `LOW`。
- **Counterexample:** “我知道怎么写”只支持 `C_EXEC`。
- **Minimal-Pair Example:** 执行能力相同，一版结果可由修改影响，一版结果已经锁定。

### B4. U_PERC

- **Unit of Annotation:** participant 的主观不确定感陈述。
- **Definition:** “我主观上有多拿不准”，不同于结构不确定性。
- **Question to Annotator:** participant 是否表达自己对要求/结果/下一步的主观确定程度？
- **Allowed Labels:** `LOW/MEDIUM/HIGH/NO_EVIDENCE/AMBIGUOUS`。
- **Use When:** “没底/很确定/拿不准”等明确方向文本。
- **Do NOT Infer From:** `U_CONTEXT`、deadline、progress、模糊规则本身。
- **NO_EVIDENCE Rule:** 有结构不确定事实但无 participant-specific subjective report。
- **N/A Rule:** 不使用。
- **Positive Anchor:** “要求虽然没公布，但我一点不担心，大概知道怎么做” → `LOW`。
- **Counterexample:** “评分规则未公布” → `NO_EVIDENCE`。
- **Minimal-Pair Example:** 结构信息相同，只改变 participant 表达“很确定/完全没底”。

### B5. F_REC

- **Unit of Annotation:** participant 对某 recovery activity 的个人恢复适配陈述。
- **Definition:** 活动对这个人通常或这次是否具有恢复作用。
- **Question to Annotator:** participant 是否报告该活动能否让自己恢复？
- **Allowed Labels:** `LOW/MEDIUM/HIGH/NO_EVIDENCE/AMBIGUOUS`。
- **Use When:** 明确 helpfulness/fit 陈述，scope 可为 episode/class/general。
- **Do NOT Infer From:** activity 发生、喜好、`R_POT`、mood afterwards。
- **NO_EVIDENCE Rule:** 只有 occurrence 或一般 recovery affordance。
- **N/A Rule:** 不使用。
- **Positive Anchor:** “散步通常能让我真正缓过来” → `HIGH/EVENT_CLASS`。
- **Counterexample:** “我喜欢散步”不充分支持 recovery fit，优先 `NO_EVIDENCE`。
- **Minimal-Pair Example:** 同一散步 episode，一版只报告发生，一版明确报告恢复作用。

### B6. APPRAISAL EVIDENCE STRENGTH

- **Unit of Annotation:** 每条非 `NO_EVIDENCE` appraisal evidence。
- **Definition:** 场景证据本身的直接程度，不是 annotator confidence。
- **Question to Annotator:** evidence 对当前 dimension 有多直接？
- **Allowed Labels:** `STRONG/MODERATE/WEAK/N/A`。
- **Use When:** direct probe 或自发、明确、episode-specific → `STRONG`；明确但间接 → `MODERATE`；模糊但有方向 → `WEAK`。
- **Do NOT Infer From:** annotator 自己确信程度、来源长度或模型概率。
- **UNKNOWN Rule:** 不使用；无法确定方向时 label `AMBIGUOUS` 并按证据直接度填写。
- **N/A Rule:** `NO_EVIDENCE` 时为 `N/A`。
- **Positive Anchor:** 对“你有把握完成吗？”回答“完全没把握” → `STRONG`。
- **Counterexample:** task metadata 不构成 `WEAK` appraisal evidence，而是 `N/A`。
- **Minimal-Pair Example:** 直接回答 probe vs “可能有点麻烦”。

### B7. APPRAISAL SCOPE

- **Unit of Annotation:** 每条 appraisal evidence。
- **Definition:** 证据指向单次 episode、event class、stable general 或范围未知。
- **Question to Annotator:** 这句话的时间/对象范围是什么？
- **Allowed Labels:** `EPISODE/EVENT_CLASS/STABLE_GENERAL/UNKNOWN_SCOPE`。
- **Use When:** 指示词、频率词和 target 清楚。
- **Do NOT Infer From:** participant profile 或 annotator 对稳定性的猜测。
- **UNKNOWN Rule:** 用 `UNKNOWN_SCOPE`。
- **N/A Rule:** 不使用；`NO_EVIDENCE` 时仍填写 `UNKNOWN_SCOPE`。
- **Positive Anchor:** “今天这节高数” → `EPISODE`。
- **Counterexample:** “我应该可以”没有清楚 target 时不是自动 `STABLE_GENERAL`。
- **Minimal-Pair Example:** “今天”/“高数一直”/“一般考试都”。

## 5. Module C — Bot Interaction / Support Representation

Module C 的单位是完整 `BotResponseUnit`，不是把一句话拆成多个 support pulse。只评价发送时
`known_at <= sent_at` 的内容与 context；不查看 future outcome。

### C1. ORIGIN

- **Unit of Annotation:** 一个 BotResponseUnit 所属 interaction。
- **Definition:** interaction 的发起来源。
- **Question to Annotator:** 当前交互由谁/什么机制发起？
- **Allowed Labels:** `USER_INITIATED/BOT_INITIATED/SYSTEM_TRANSACTIONAL/UNKNOWN`。
- **Use When:** preceding turn 或系统事件明确。
- **Do NOT Infer From:** response 语气、support quality 或之后是否回复。
- **UNKNOWN Rule:** 上下文不足。
- **N/A Rule:** 不使用。
- **Positive Anchor:** 用户先问“怎么安排复习？” → `USER_INITIATED`。
- **Counterexample:** 定时提醒不是 `BOT_INITIATED` care，若纯事务则 `SYSTEM_TRANSACTIONAL`。
- **Minimal-Pair Example:** 相同文本分别由用户提问或 proactive trigger 引发。

### C2. ROLE

- **Unit of Annotation:** 一个完整 BotResponseUnit。
- **Definition:** response 的主要交互功能。
- **Question to Annotator:** 这条回复主要在提供什么？
- **Allowed Labels:** `ORDINARY_INFORMATION/TASK_EXECUTION_SUPPORT/EMOTIONAL_SUPPORT/COPING_SUPPORT/SENSING/RECOVERY_SUGGESTION/SAFETY_SUPPORT/MIXED/OTHER/UNKNOWN`。
- **Use When:** 内容功能可识别；多种不可约角色并存用 `MIXED`。
- **Do NOT Infer From:** personalization、helpfulness outcome 或用户情绪改善。
- **UNKNOWN Rule:** 内容残缺或功能不可判。
- **N/A Rule:** 不使用。
- **Positive Anchor:** 把报告拆成三个操作步骤 → `TASK_EXECUTION_SUPPORT`。
- **Counterexample:** 普通技术建议不自动为 `COPING_SUPPORT`。
- **Minimal-Pair Example:** “先列提纲” vs “先做一分钟呼吸，再选最小一步”。

### C3. SUPPORT_GATE

- **Unit of Annotation:** 一个 BotResponseUnit。
- **Definition:** 是否具备进入 supportive dynamics 的内容 affordance；不代表 effect。
- **Question to Annotator:** 回复是否提供 emotional/coping/care support，而非仅任务帮助？
- **Allowed Labels:** `SUPPORTIVE/NON_SUPPORTIVE/SAFETY_ONLY/AMBIGUOUS`。
- **Use When:** 明确 validation、coping guidance、care 或安全响应。
- **Do NOT Infer From:** task completion、personalization、seen、future stress change。
- **UNKNOWN Rule:** 使用 `AMBIGUOUS` 并说明冲突，不使用 `UNKNOWN`。
- **N/A Rule:** 不使用。
- **Positive Anchor:** 承接焦虑并给低负担 coping step → `SUPPORTIVE`。
- **Counterexample:** 只给代码修复步骤 → `NON_SUPPORTIVE`。
- **Minimal-Pair Example:** 相同任务步骤，一版额外提供恰当情绪承接。

### C4. VALIDATION

- **Unit of Annotation:** supportive content 中的 relational acknowledgment。
- **Definition:** 是否承接体验、认可困难并提供恰当 relational presence。
- **Question to Annotator:** 回复对当前体验的承接有多充分？
- **Allowed Labels:** `0/0.5/1`。
- **Use When:** `0` 无承接；`0.5` 表面/有限；`1` 明确且针对当前体验。
- **Do NOT Infer From:** 礼貌用语、任务正确性、personalization 或用户感谢。
- **UNKNOWN Rule:** schema 不提供 unknown；内容不足时 scenario QA 应标 uncertain。
- **N/A Rule:** 不使用；非 supportive response 通常标 `0`。
- **Positive Anchor:** “连续两次被退回确实很挫败，你现在紧绷很合理” → `1`。
- **Counterexample:** “收到” → `0`。
- **Minimal-Pair Example:** “别担心”与针对具体体验的承接。

### C5. GUIDANCE

- **Unit of Annotation:** coping-oriented guidance content。
- **Definition:** 面向应对/压力管理的清晰、低负担行动指导。
- **Question to Annotator:** 回复提供了多少直接可执行的 coping guidance？
- **Allowed Labels:** `0/0.5/1`。
- **Use When:** `0` 无 coping；`0.5` 泛化/有限；`1` 清晰、低负担且 context-specific。
- **Do NOT Infer From:** 普通任务技巧、技术 advice 或 recovery activity 发生。
- **UNKNOWN Rule:** 不使用；按可见内容评分。
- **N/A Rule:** 不使用。
- **Positive Anchor:** “先把肩膀放松，做三轮慢呼吸，再只处理第一小步” → `1`。
- **Counterexample:** “把报告分三段写”是 task help，`GUIDANCE=0`。
- **Minimal-Pair Example:** task decomposition vs coping-first decomposition。

### C6. RELEVANCE

- **Unit of Annotation:** response 与发送时 context/request 的匹配。
- **Definition:** supportive 内容是否针对当时合法已知的信息。
- **Question to Annotator:** 仅看 sent_at 前信息，这条回复有多匹配？
- **Allowed Labels:** `0/0.5/1`。
- **Use When:** `0` 不匹配；`0.5` 泛化/遗漏关键 context；`1` 明确匹配。
- **Do NOT Infer From:** 发送后新信息、future reply 或最终效果。
- **UNKNOWN Rule:** 不使用；context 不足时 scenario QA 标 uncertain。
- **N/A Rule:** 不使用。
- **Positive Anchor:** 针对已知“20 分钟后答辩”给极短 coping step → `1`。
- **Counterexample:** 利用发送后才知道的取消信息判高相关属于 future leakage。
- **Minimal-Pair Example:** 相同回复的关键 context 在发送前已知 vs 发送后才出现。

### C7. PERSONALIZATION

- **Unit of Annotation:** response 使用的个体 context 类型。
- **Definition:** 仅描述 personalization 来源，不表示 supportive 或 effective。
- **Question to Annotator:** 回复是否使用当前 context、历史偏好或稳定 profile？
- **Allowed Labels:** `NONE/CURRENT_CONTEXT/HISTORICAL_PREFERENCE/STABLE_PROFILE/MIXED/UNKNOWN`。
- **Use When:** response 中可见明确个体化依据。
- **Do NOT Infer From:** 使用姓名、语气温暖或结果有效。
- **UNKNOWN Rule:** 看似个体化但来源无法判断。
- **N/A Rule:** 不使用。
- **Positive Anchor:** “你之前说散步比冥想更适合你” → `HISTORICAL_PREFERENCE`。
- **Counterexample:** “你可以休息一下” → `NONE`。
- **Minimal-Pair Example:** 泛化建议 vs 引用明确历史偏好；其他内容相同。

### C8. SEEN_STATUS

- **Unit of Annotation:** 一个已发送 BotResponseUnit。
- **Definition:** participant 是否真正暴露于该 response。
- **Question to Annotator:** cutoff 前是否有可靠 read/exposure evidence？
- **Allowed Labels:** `SEEN/NOT_SEEN/UNKNOWN/N/A`。
- **Use When:** read receipt 或其他可靠 contemporaneous exposure evidence 存在。
- **Do NOT Infer From:** future reply；没有 receipt 时不得制造 seen time。
- **UNKNOWN Rule:** 已发送但无可靠 exposure evidence。
- **N/A Rule:** response 未成功发送或纯内部候选。
- **Positive Anchor:** read receipt 10:03 → `SEEN`。
- **Counterexample:** 10:30 的回复不能用于把 10:03 预填为 seen。
- **Minimal-Pair Example:** 相同 response，一版有 cutoff 前 receipt，一版没有。

## 6. Critical forbidden-inference checklist

提交前逐项检查：

1. Objective difficulty high ≠ `C_EXEC=LOW`。
2. 必修/学分高 ≠ `IMPORTANCE=HIGH`。
3. Deadline near 或 `U_CONTEXT=HIGH` ≠ `U_PERC=HIGH`。
4. Progress low ≠ `C_EXEC=LOW`。
5. Presentation ≠ personal importance high。
6. Free time ≠ recovery occurred。
7. Recovery occurrence ≠ `F_REC=HIGH`。
8. Missed course ≠ catch-up obligation。
9. EMA high ≠ 任一 appraisal label。
10. Task help ≠ supportive content。
11. Personalization ≠ support quality/effect。
12. `known_at > cutoff/sent_at` 的事实不得用于当前标签。
13. Actual interval 与 fractional gate 不得重复编码同一 partial exposure。
14. Parent 与 active child 不得默认同时为 active leaves。

## 7. Calibration 提交流程

1. 只打开分配给自己的 assignment，不查看 hidden、其他 annotator 或 reference。
2. 完成 Scenario QA。
3. 按 Module/target/variable 逐条生成统一 envelope record。
4. 所有 directional Module B 记录提供逐字 evidence span。
5. 所有 unknown/no-evidence/ambiguous 记录填写 unknown reason。
6. 运行 `validate-annotations`；无效 label 不得静默改写。
7. 提交完整轮次后才进入 agreement/disagreement 分析。
