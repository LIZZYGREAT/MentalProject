# MindFlow Research Data Contract

版本：v1（对应 Profile Schema v2）  
状态：Stage 1 Gate 1  
适用范围：后续建模、机器学习、研究分析与 Admin；本合同不修改 CTSSM 主方程。

## 1. 数据类别与时间规则

| 类别 | 含义 | 权威存储 | 状态时间 | 知识/入库时间 |
|---|---|---|---|---|
| Momentary State | 用户在某一时刻的即时状态 | `state_observations` | `observed_at` | `created_at` |
| Retrospective State | 对既往一天的回顾与曲线重建锚点 | `daily_review_responses` | `local_date` 与题目定义的日内时段 | `submitted_at`，另保留 `created_at` |
| Explicit Stable Profile | 用户明确提供、相对稳定的信息 | `participant_profiles`（Profile Schema v2） | 每字段 `updated_at` | 画像版本 `created_at` |
| Psychometric Profile | 标准化心理量表的追加式历史 | `psychometric_assessments` | `administered_at` 与 `reference_period` | `created_at` |
| Slow-varying State | 日/周频更新的近期聚合状态 | `participant_slow_states` | `effective_at` | `created_at` |
| Learned Model Parameters | 纵向数据学习出的可审计参数 | `learned_model_profiles` | `window_start`–`window_end` | `created_at` |
| Event Appraisal | 用户对真实日程事件的事后评价 | `event_appraisal_feedback` | 由 `event_id` 指向事件；提交时刻为 `submitted_at` | `created_at` |

实时模型只能按状态时间读取。任何 point-in-time、因果或回顾分析必须同时满足“状态时间不晚于 cutoff”与“知识时间不晚于 cutoff”，禁止让之后补录的数据泄漏到过去。

## 2. 变量合同

表中“LLM”指是否可在已取得外部 LLM 授权、完成去标识化且业务确有必要时提供。`No` 表示任何情况下都不直接提供。

### 2.1 Momentary State（Instant Check-in / EMA）

| 名称 | 概念定义 | 量纲 | 数据来源 | 时间语义 | 允许缺失 | 实时模型 | Retrospective | Calibration | LLM |
|---|---|---|---|---|---|---|---|---|---|
| `stress_0_10` | 瞬时主观压力 | 0–10 分 | Instant Check-in | `observed_at` | No | Yes | Yes | Yes | Yes |
| `energy_0_10` | 瞬时主观精力/活力 | 0–10 分 | Instant Check-in | `observed_at` | No | Yes | Yes | Yes | Yes |
| `activity` | 填写时正在进行的活动 | 文本，1–120 字符 | Instant Check-in | `observed_at` | No | Context only | Yes | Qualitative | Yes |
| `stress_event_since_last` | 自上次记录后是否发生压力事件 | Boolean | Instant Check-in | 截至 `observed_at` 的区间事实 | No | Yes | Yes | Yes | Yes |
| `event_ongoing` | 所述压力事件在 `observed_at` 是否仍持续 | Boolean | Instant Check-in | `observed_at` | No | Yes | Yes | Yes | Yes |
| `current_workload_0_10` | 当前主观工作/学习负荷 | 0–10 分 | 抽样 Instant Check-in | `observed_at` | Yes | Candidate only | Yes | Yes | Yes |
| `perceived_control_0_10` | 当前对任务/情境的主观控制感 | 0–10 分 | 抽样 Instant Check-in | `observed_at` | Yes | Candidate only | Yes | Yes | Yes |

`created_at` 是系统知道该记录的时刻；实时读取以 `observed_at` 为准，因果查询同时受 `created_at` 限制。

### 2.2 Retrospective State（Daily Review）

| 名称 | 概念定义 | 量纲 | 数据来源 | 时间语义 | 允许缺失 | 实时模型 | Retrospective | Calibration | LLM |
|---|---|---|---|---|---|---|---|---|---|
| `start_stress` | 当日开始阶段回忆压力 | 0–10 分 | Daily Review | `local_date` 的开始时段 | No | No | Anchor | Yes | Yes |
| `start_energy` | 当日开始阶段回忆精力 | 0–10 分 | Daily Review | `local_date` 的开始时段 | No | No | Anchor | Yes | Yes |
| `peak_stress` | 当日回忆压力峰值 | 0–10 分 | Daily Review | `peak_period` | No | No | Conditional anchor | Yes | Yes |
| `peak_period` | 峰值所在日内时段 | 枚举/时间段 | Daily Review | `local_date` 内 | No | No | Anchor time | Yes | Yes |
| `end_stress` | 当日收尾阶段回忆压力 | 0–10 分 | Daily Review | `local_date` 的收尾时段 | No | No | Anchor | Yes | Yes |
| `end_energy` | 当日收尾阶段回忆精力 | 0–10 分 | Daily Review | `local_date` 的收尾时段 | No | No | Anchor | Yes | Yes |
| `energy_consumption` | 全天主观精力消耗诊断 | 0–10 分 | Daily Review | 整个 `local_date` | Yes | No | Diagnostic only | Diagnostic only | Yes |
| `main_stressor` | 当日主要压力来源 | 文本 | Daily Review | 整个 `local_date` | Yes | No | Qualitative | No numeric input | Yes |
| `recovery_note` | 当日恢复行为/体验 | 文本 | Daily Review | 整个 `local_date` | Yes | No | Qualitative | No numeric input | Yes |
| `free_text` | 其他回顾上下文 | 文本 | Daily Review | 整个 `local_date` | Yes | No | Qualitative | No numeric input | Yes |

文本只用于研究解释、Care 个体化和定性分析，不直接作为 CTSSM 数值输入。提交知识时间为 `submitted_at`。

### 2.3 Explicit Stable Profile（Profile Schema v2）

每个字段必须保存 `{value, source, updated_at}`。顶层固定为 `schema_version: "2.0"` 与 `explicit`；兼容性 `model_params` 可继续存在，但不属于用户心理特征。

| 名称 | 概念定义 | 量纲 | 数据来源 | 时间语义 | 允许缺失 | 实时模型 | Retrospective | Calibration | LLM |
|---|---|---|---|---|---|---|---|---|---|
| `preferred_name` | 用户希望被称呼的名字（禁止直接身份标识） | 文本 | 用户明确填写 | 字段 `updated_at` | Yes | No | No | No | Yes |
| `typical_sleep_window` | 通常睡眠起止窗口 | 本地时间窗口 | 用户明确填写 | 字段 `updated_at` | Yes | Context only | Yes | Candidate | Yes |
| `chronotype` | 用户自述昼夜偏好 | 分类 | 用户明确填写 | 字段 `updated_at` | Yes | Context only | Yes | Candidate | Yes |
| `typical_study_load` | 通常学习负荷 | 结构化值/0–10 | 用户明确填写 | 字段 `updated_at` | Yes | Candidate only | Yes | Candidate | Yes |
| `exercise_frequency` | 通常运动频率 | 次/周或分类 | 用户明确填写 | 字段 `updated_at` | Yes | No | Yes | Candidate | Yes |
| `preferred_recovery_methods` | 用户偏好的非临床恢复方式 | 字符串列表 | 用户明确填写 | 字段 `updated_at` | Yes | Care only | Yes | No | Yes |

`source` 允许如 `participant`、`researcher_verified`；不得把模型推断写入 Explicit 层。

### 2.4 Psychometric Profile

支持首批量表 `PSS`、`BRS`。每次施测追加一行，不覆盖历史版本。

| 名称 | 概念定义 | 量纲 | 数据来源 | 时间语义 | 允许缺失 | 实时模型 | Retrospective | Calibration | LLM |
|---|---|---|---|---|---|---|---|---|---|
| `instrument_name` | 量表标识 | `PSS` / `BRS` | 量表施测 | `administered_at` | No | No | Yes | Yes | Yes |
| `instrument_version` | 题目/计分版本 | 版本字符串 | 量表定义 | `administered_at` | No | No | Yes | Yes | Yes |
| `language` | 施测语言版本 | BCP-47/短标签 | 量表定义 | `administered_at` | No | No | Yes | Yes | Yes |
| `raw_items_json` | 原始题项答案 | JSON | 用户量表回答 | `administered_at` | No | No | Yes | Re-score only | No |
| `scores_json` | 总分及分量表结果 | JSON 数值 | 版本化计分器 | `administered_at` | No | No | Yes | Yes | Yes |
| `reference_period` | 题目要求回顾的时间范围 | 文本/枚举 | 量表定义 | 相对 `administered_at` | Yes | No | Yes | Yes | Yes |

### 2.5 Slow-varying State

| 名称 | 概念定义 | 量纲 | 数据来源 | 时间语义 | 允许缺失 | 实时模型 | Retrospective | Calibration | LLM |
|---|---|---|---|---|---|---|---|---|---|
| `rolling_7d_stress` | 过去 7 天压力聚合 | 0–10 分 | EMA/回顾派生 | `effective_at` 前 7 天 | Yes | Candidate only | Yes | Yes | Yes |
| `rolling_7d_workload` | 过去 7 天负荷聚合 | 0–10 分 | Calendar/EMA 派生 | `effective_at` 前 7 天 | Yes | Candidate only | Yes | Yes | Yes |
| `rolling_7d_energy` | 过去 7 天精力聚合 | 0–10 分 | EMA/回顾派生 | `effective_at` 前 7 天 | Yes | Candidate only | Yes | Yes | Yes |
| `recent_recovery_quality` | 近期主观恢复质量 | 0–10 分 | Review/EMA 派生 | 截至 `effective_at` | Yes | Candidate only | Yes | Yes | Yes |
| `recent_sleep_debt` | 近期累计睡眠债 | 小时，0–24 | 睡眠窗口/自报派生 | 截至 `effective_at` | Yes | Candidate only | Yes | Yes | Yes |
| `exam_period_flag` | 是否处于考试阶段 | Boolean | 研究配置/日历派生 | `effective_at` | Yes | Candidate only | Yes | Yes | Yes |

`cadence` 只能是 `daily` 或 `weekly`，`source` 必须记录生成算法或研究流程。

### 2.6 Learned Model Parameters

每个版本是追加式快照。参数名来自 `parameters_json`；相应不确定性来自 `uncertainty_json`。所有参数共享该版本的证据窗口、样本量、模型版本与验证状态。

| 名称 | 概念定义 | 量纲 | 数据来源 | 时间语义 | 允许缺失 | 实时模型 | Retrospective | Calibration | LLM |
|---|---|---|---|---|---|---|---|---|---|
| `parameter_name` | 参数规范名称 | 字符串/JSON path | 纵向校准 | `window_start`–`window_end` | No | If validated | Yes | Output | No |
| `estimate` | 参数点估计 | 参数自身量纲 | 纵向校准 | 证据窗口 | No | If validated | Yes | Output | No |
| `uncertainty` | 标准误/区间等不确定性 | 与估计对应 | 纵向校准 | 证据窗口 | Yes for legacy; No for new validated | No | Yes | Output | No |
| `sample_count` | 有效观测样本数 | count | 校准数据集 | 证据窗口 | No | No | Yes | Audit | No |
| `window_start` / `window_end` | 训练证据窗口 | 日期 | 校准数据集 | 闭区间 | No | No | Yes | Audit | No |
| `model_version` | 产生参数的模型版本 | 版本字符串 | 校准服务 | `created_at` | No | Gate | Yes | Audit | No |
| `validation_status` | 参数是否可进入正式模型 | `candidate` / `validated` / `rejected` | OOT 评估 | `created_at` | No | Only `validated` | Yes | Audit | No |

Stage 1 明确区分 latest 与 runtime-active：`validated` 可以进入生产；迁移前已经生效且
`model_version=legacy` 的行保持兼容，但不被错误标记为 validated；新的 `candidate` 与所有
`rejected` 行只保留作研究历史，不进入 Forecast。

### 2.7 Event Appraisal Feedback

下列八个评分统一为 0–10，全部必填；用于 Calendar Semantic 和后续 Workload 校准，不直接改变当前 CTSSM 状态。

| 名称 | 概念定义 | 数据来源 | 时间语义 | 允许缺失 | 实时模型 | Retrospective | Calibration | LLM |
|---|---|---|---|---|---|---|---|---|
| `mental_demand` | 事件的主观心理需求 | Event Appraisal | `event_id` 所指事件 | No | No | Yes | Yes | Yes |
| `physical_demand` | 事件的主观身体需求 | Event Appraisal | 同上 | No | No | Yes | Yes | Yes |
| `temporal_demand` | 时间紧迫/节奏要求 | Event Appraisal | 同上 | No | No | Yes | Yes | Yes |
| `effort` | 完成事件投入的主观努力 | Event Appraisal | 同上 | No | No | Yes | Yes | Yes |
| `frustration` | 事件引发的挫败感 | Event Appraisal | 同上 | No | No | Yes | Yes | Yes |
| `perceived_control` | 对事件过程/结果的控制感 | Event Appraisal | 同上 | No | No | Yes | Yes | Yes |
| `actual_stress` | 事件实际带来的主观压力 | Event Appraisal | 同上 | No | No | Yes | Yes | Yes |
| `perceived_performance` | 用户自评表现 | Event Appraisal | 同上 | No | No | Yes | Yes | Yes |

`event_id` 是事件关联键；`submitted_at` 是用户完成评价的时刻；`created_at` 是系统入库时刻。

## 3. Workload、Resilience 与参数边界

| 概念 | 当前定义 | Stage 1 用途 |
|---|---|---|
| Workload / `W(t)` | Calendar/Event Semantic demand 与主观 workload 的待校准综合量，目标尺度 0–10 | 只做观测、诊断与候选输入；Stage 3 前不新增动态状态 |
| Resilience | 面对 demand 后维持功能并恢复的能力/过程；与单次低压力不同 | BRS 可作稳定特征证据，recovery quality 可作慢状态证据；Stage 4 前不改主方程 |
| Learned Parameters | 只能由 longitudinal data 学习，必须带证据窗口、样本量、模型版本、不确定性和验证状态 | `candidate` 不等于生产生效；外部 LLM 不接触内部参数 |

## 4. Sampling Protocol

- Instant Check-in：用户主动；`current_workload_0_10` 与 `perceived_control_0_10` 只在抽样时出现。
- Daily Review：每位参与者每天最多 1 次正式任务；修订作为追加 revision 保存。
- Event Appraisal：每天最多 1 次。优先级依次考虑新 event type、低 semantic confidence、历史 residual 大、模型 uncertainty 高、高重要度任务。
- Psychometrics：按研究方案施测；不同量表版本与施测时间不得覆盖。
- Slow State：`daily` 或 `weekly` 生成；同一参与者的历史行不得原地改写。

## 5. 数据使用硬约束

1. `observed_at` 与 `created_at` 不得互换；补录观测不得进入其创建前的因果输入。
2. Daily Review 是 retrospective evidence，不得伪装为即时 EMA。
3. `energy_consumption` 仅诊断；回顾文本不得直接作为 CTSSM 数值输入。
4. 显式画像、量表、慢状态、学习参数必须分层读取和展示，不得合并成来源不明的单一画像。
5. 量表原始题项不发给外部 LLM；内部学习参数也不发给外部 LLM。
6. Event Appraisal 的所有分值在应用层与数据库层限制为 0–10。
7. 所有研究表按 `participant_id` 隔离；Admin 输出继续执行敏感字段递归清理。

## 6. Gate 1 完成定义

- 本文覆盖 Stage 1 的 Momentary、Retrospective、Stable/Slow、Workload、Resilience 与 Learned variables。
- `psychometric_assessments`、`event_appraisal_feedback`、`participant_slow_states` 均有正式 schema 与追加式历史。
- Profile Schema v2 的显式字段带 `value/source/updated_at`。
- Admin Participant 画像页分层显示 Explicit、Psychometrics、Slow State、Learned Parameters，并展示量表与参数历史。
- 学习参数记录包含 uncertainty、model version 和 validation status；当前 CTSSM 主方程保持不变。
