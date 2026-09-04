# MindFlow Research Data Contract

文档版本：Research Data Contract v2

Profile Schema：v2

状态：CURRENT

覆盖范围：Stage 1–6 当前研究数据、因果时间边界、回顾、校准、个体化与评估；本合同不修改 CTSSM 主方程。

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

Event Appraisal 同时冻结 `event_local_date`、`event_start_at`、因果时点的
`source_forecast_id/version`、`source_semantic_revision`、`workload_feature_vector`、
`workload_prior`、Raw-TLX 风格 `observed_workload`、`workload_residual` 与 schema/estimator version；残差定义为
`observed_workload - workload_prior`，取值尺度为 0–1。Performance 仅来自事后反馈，
不进入事前 semantic workload prior。因果截止点是 event start、submitted、created 三者最早值；
其中 `created_at` 仅由 repository 的系统时钟生成，业务调用者不得提供或回填。只允许使用该时点
实际 current 的 Forecast，不得回退到后来生成的版本；workload context 缺失、不完整或非法时，
observed workload 与八项反馈照常保存，但 prior、residual 及 workload provenance 必须全部为空。

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

`event_id` 是事件关联键；`submitted_at` 是用户完成评价的时刻；`created_at` 是系统内部生成的
知识/入库时刻，不能由业务请求传入。

## 3. Workload、Resilience 与参数边界

| 概念 | 当前定义 | Stage 4 用途 |
|---|---|---|
| Workload / `W(t)` | Calendar/Event Semantic demand 与主观 workload 的待校准综合量，运行时归一化到 0–1 | Workload-aware M0 及 M1–M3 的可观测输入；结合真实 EMA Stress 估计个体 stress reactivity |
| Recovery / `R(t)` | calendar gap、protected break、sleep window、user-reported recovery 的保守聚合，运行时为 0–1 | 以 `-β_R R(t)` 进入候选模型，并用于纵向 recovery efficiency 与恢复速率估计 |
| Resilience | 面对 demand 后维持功能并恢复的能力/过程；与单次低压力不同 | BRS 只作为 `β_R` 的慢 trait prior；真实纵向恢复数据继续更新恢复参数 |
| Learned Parameters | 只能由 longitudinal data 学习，必须带证据窗口、样本量、模型版本、不确定性和验证状态 | 学习 `β_W`、`β_R` 与上升/恢复响应速率 `κ`；`candidate` 不等于生产生效，外部 LLM 不接触内部参数 |

Stage 4 离线比较固定使用 `mindflow-research-dataset-v4` Dataset Snapshot。BRS、Daily Review recovery、Slow State recovery/sleep、Forecast、Calendar、EMA 与 match source 必须一并冻结；Current M0、Workload-aware M0、M1、M2、M3 使用同一 Rolling-Origin split。M1/M2/M3 缺少 vitality、perseverative cognition 或 recovery debt 对应观测证据时可以保留研究指标，但 promotion gate 必须失败。

候选模型必须通过 `stage4-real-ctssm-replay.v6` 调用真实 CTSSM；Current M0 同样必须真实回放，冻结的 historical production prediction 只能作为独立诊断，不能作为 promotion baseline。任何独立近似公式产生的结果不得进入 promotion。Current M0 只用 `m0-simulator-fit.v2` 在真实 AssessmentModel/Simulator 上对 `S_star_init∈[0,100]` 做 training-only restricted SSE 搜索；每个搜索点必须使用冻结 Calendar、initial state、target 前 known observations 与相同 target timestamp，不能用压力均值或 full-model intercept。与参数无关的输入只预处理一次，相同 `S_star_init` objective 必须缓存，并冻结 evaluated parameter、Simulator call 与 training sample count。WM0/M1/M2/M3 使用 `workload-recovery-ridge.v2`。rolling-origin 使用 `rolling-origin-knowledge-causal.v2`，训练 EMA、BRS 与 Slow State 同时受 state-time 和 split origin knowledge-time 约束。M2 support 使用 post-event exposure/EMA、stress persistence transition、参与者数和天数；M3 support 使用 sustained-workload episode、continuous-load variation、post-load recovery transition、vitality EMA、参与者数和天数，阈值版本为 `ctssm-observable-support.v2`。这些正式 support 必须在每个 split 的 causal training `fit_samples` 上计算，按 participant/family 冻结最终最大 training-window `evaluation_observable_support_evidence`；cohort 仅在所有 participating participant 均 supported 时通过。完整 Dataset support 只能作为 `descriptive_observable_support` 且必须标记 `descriptive_only=true`，不得进入 Gate。M3 Recovery Debt 采用 `recovery-debt-dynamics.v1`：$F\in[0,1]$ 且 $dF/dt=\alpha_FW(1-F)-\lambda_FRF$，以饱和累积和非负恢复保证状态有界。

Dataset v4 的 Forecast item 必须冻结 day-boundary `initial_state`、`initial_state_revision` 以及完整生产模型身份。ForecastObservationMatch context 必须冻结相同的 model/promotion provenance。个体 production promotion 只能来自同一 participant 的 evaluation run；cohort run 只能产生 cohort research decision。promotion profile 的 uncertainty 不包含 `model_selection`，且不得用零标准误或固定 1.0 confidence 伪造统计证据。

Workload/recovery Ridge 估计使用 Gaussian prior 对应的 `ridge-posterior-covariance.v1`：$\Sigma_\beta=\hat\sigma^2A^{-1}$，其中 $A=X^TX+\lambda I$。必须分别冻结 baseline、workload 与 recovery coefficient SE，以及 ridge lambda、sample count、design condition number、identifiability status 和 boundary clipping；`ctssm-promotion-gate.v2` 中 `not_identified` 必须失败，`weak` 可通过但必须 warning，boundary clipping 在 v2 中必须 warning。Participant promotion decision 与 learned profile 必须同事务提交。

Rolling-Origin 参数是 evaluation-only，必须保存为 `evaluation_candidate_parameters`、`evaluation_candidate_uncertainty` 和 `evaluation_candidate_evidence`。正式 identifiability Gate 只允许使用每个参与者最后一个/最大 training window split 的 `evaluation_parameter_gate_evidence`；cohort 按 not_identified 优先、其次 weak 的规则聚合，禁止读取 test-day label 或任何 `deployment_*`。仅当至少一个候选 Gate PASS 后，最终生产参数才能由 `stage4-deployment-refit.v1` 生成：它只读取同一 immutable Dataset v4 中目标参与者截至 snapshot observation cutoff 的全部 eligible frozen observations/BRS/Slow State，产出独立 `deployment_*` 字段，不得查询 live evidence；所有 Gate FAIL 时这些字段必须为空。LearnedModelProfile 的 window、sample/day count 与 uncertainty 必须对应真实 deployment refit；model-selection provenance 必须包含 dataset snapshot、knowledge cutoff 与 refit version。部署重拟合不得改变 rolling-origin metrics。若 deployment refit 为 `not_identified`，晋级以 `deployment_refit_not_identifiable` fail closed，且不得反写原 evaluation Gate。

历史评估的 resolved model identity filter 及匹配到的 promotion decision/parameters hash 必须写入 evaluation provenance。`snapshot_source_set` 表示 Dataset 内 identity filtering 前全部候选 match source；`evaluation_source_set` 只表示真正进入 metrics 的 observation、forecast、match source hash、promotion decision 与 promotion parameters hash，不得混入未匹配 revision。

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
