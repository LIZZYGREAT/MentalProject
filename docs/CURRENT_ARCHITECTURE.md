# MindFlow 当前生产架构

本文只描述 `production_runtime` 当前实现。代码、迁移与自动测试是最终事实来源。

<!-- BUSINESS_TOOL_COUNT: 15 -->
<!-- MODEL_VERSION: mindflow-ctssm-runtime-v7 -->
<!-- ALEMBIC_HEAD: 0030_model_promotion_decisions -->

## 运行边界

MindFlow 是面向飞书私聊的 Python 后端，业务数据保存在 PostgreSQL。飞书 receiver 在独立
进程接收事件，Bot 进程负责持久化、参与者绑定、授权、安全检查、Agent 调用、业务工具和
可靠回复。参与者身份只来自后端绑定上下文，不从模型参数接收。

## Agent 与业务工具

每位参与者使用独立顺序队列与可恢复 session。Backend 只向 Agent 暴露 15 个封闭 schema
的 participant-bound 工具，覆盖 Care、check-in、Forecast、压力曲线和 Calendar。工具
Registry 禁止身份、Token、Secret、SQL、路径与任意 URL 字段。

`calendar_update_event` 的 `start_time` 与 `end_time` 是互相依赖字段：两者同时提供或同时
省略。底层 handler 也执行相同校验，避免 schema 与实现合同漂移。

## Forecast、Calendar 与 freshness

`ForecastCoordinator` 是正式评估入口。它准备显式/学习画像、Calendar、Observation、
事件语义和前一日 terminal state，调用 CTSSM，保存版本化 `ForecastSnapshot`，并在同一
权威输入边界同步 Warning。当前评估来源是有效的 `ForecastSnapshot`，历史
`prediction_runs` 表不表示 current assessment。

`CalendarSnapshot.snapshot_state` 有三种状态：

- `current`：provider 成功返回，可作为正式 Forecast 输入；
- `provider_degraded`：普通读取暂时失败，但保留此前稳定快照，可按降级规则使用；
- `mutation_refresh_pending`：远端写入已成功、read-back 尚未成功，旧 Calendar 已不可信。

Calendar mutation 会立即使对应日期的旧 Forecast 失效并取消未发送 Warning，然后把快照
标记为 `mutation_refresh_pending`。该状态保留旧 events 仅供诊断；Forecast 禁止消费。
read-back 超时会抛出明确异常，不保存新 Forecast，也不会恢复旧 Warning。后续刷新成功后
状态回到 `current`。每次 provider read 都携带读前 snapshot id、revision 和 state；成功与
失败写回均在 `Participant -> CalendarSnapshot` 锁顺序下执行 CAS。mutation 前启动的旧请求
不能覆盖新的 pending marker，也不能把旧 Calendar 重新发布为 current/degraded。
多日事件按 `[start, end)` 覆盖全部本地日期；recurrence mutation 只检查数据库中已经存在
有效 Forecast 的日期，并按系统允许生成的 DAILY/WEEKLY/MONTHLY/YEARLY 子集匹配旧规则与
新规则的并集，不会预展开无限 recurrence。外部 Calendar 中超出该 reviewed subset 的规则
不会被静默误解释，而会保守失效 DTSTART 之后已持久化的有效 Forecast。Calendar 事件持续
时间严格限制为 `0 < duration <= 31 days`。带 `COUNT` 的规则通过目标日期的有界 occurrence
序号判断，不生成目标之后的日期；recurrence-only PATCH 即使 provider 只返回部分字段，也会
按“用户显式值 → provider 非空值 → mutation 前事件”合成完整逻辑事件后计算影响范围。

Calendar mutation 在用户请求返回前用单一事务批量 fail-close 全部直接受影响的
Forecast/Warning；重算由托管
队列在后台执行，按参与者保持日期顺序、跨参与者限制并发并支持去重与 shutdown，避免周期
事件影响日期数量线性增加用户请求时长。瞬时批量失效或 Today→Tomorrow dependency 失效
失败会在同一托管队列中有限重试，成功后才开始对应 Forecast 重算。服务重启后仍为 invalid
的日期由后续查询惰性恢复。

Today Forecast terminal 是 Tomorrow initial state 的依赖。直接修改 Today Calendar 时，
Today 使用 Calendar mutation invalidation；Tomorrow 只做 Forecast dependency dirty，不修改
Tomorrow CalendarSnapshot、revision 或 events。统一的 dependency refresh service 只承认
Yesterday→Today 和 Today→Tomorrow 两条边。Observation、semantic enrichment 与 Calendar
输入一旦变化，会在 source 重算前立即 fail-close dependent；source 成功后才刷新 dependent，
失败则保持 invalid。Daily Review retrospective ready 后也立即失效并托管刷新 eligible
dependent；历史 rebuild 不传播，也不会递归到 Day+2。

Calendar 写操作使用 durable pre-intent saga：远端 create/update/delete 前先保存
`prepared` 意图与预期影响；远端成功后进入 `remote_committed`，失败则进入已终结的
`remote_failed`。token 与 primary-calendar lookup 属于明确的 preflight 边界；该阶段的
timeout、transport error 或 5xx 表示 mutation 未发送，终结为 `remote_failed` 且禁止 replay。
只有 event mutation 请求已经发出后的 timeout、transport error 或 5xx 才进入可恢复的
`remote_outcome_unknown`。本地 Forecast/Warning 隔离成功后进入 `fenced`，刷新完成后才
`resolved`；`fencing_failed` 按有界退避恢复。进程在远端结果落库前崩溃而留下
`prepared` 时，恢复器不假设远端失败，而是记录不确定结果并保守隔离相关本地预测。新进程
启动会忽略旧进程 intent 的 live-request grace，在 Forecast/Warning scheduler 启动前完成
fail-closed fencing；本进程刚创建且仍在 provider in-flight 的 `prepared` 请求保留 grace。
`remote_committed` 一经落库便立即 due，请求路径与恢复路径通过数据库短租约 claim 竞争
fencing 所有权，因此 commit 后协程取消不会产生五分钟 stale Forecast/Warning 窗口，也不会双重刷新。

Tomorrow 解析 Today terminal 时使用 `refresh_calendar=False`；Today 无快照时仍会按需读取，
但不会因为刷新 Tomorrow 而无条件重复调用 Today Calendar provider。

## Course classification

事件分类先执行确定性规则与有界课程目录检索，再在已授权时使用同一次 semantic API 完成
语义增强。`event_type_locked` 与 `course_identity_locked` 是独立事实：例如“高数”可以锁定
为 course，但 canonical identity 仍由 API 从 Top-K 候选中选择；“线代”精确解析为“线性
代数”时才锁定 identity。API 返回的课程必须属于候选集合。

`高数A/高数B` 会分别把 semantic candidate set 限制为 A/B-compatible 课程，API 不能反转
用户明确写出的类别。`高数I/II/1/2/3` 仅作为模糊检索提示，不会用
`candidates[0]` 锁成错误课程。课程相关作业、复习和考试保持 task；rules-only 只有在去除
任务意图词后仍能 exact resolve 时才写 canonical related course。模糊结果只保留 Top-K
catalog context，等 semantic API 返回候选内的 validated match 后再写 canonical identity。
普通负例不会被目录模糊命中提升为 course。

Semantic provider 的传输/限流/不可解析错误与内容拒绝分离。objective、classification、
course match 分别验证；可接受 objective 不会因另两个可选 component 低置信而丢失。
course match 只有在候选集合内且 confidence 至少为 0.55 才写 canonical identity。缓存状态
为 `complete`、`partial` 或 `rejected`；同一 fingerprint 的 rejected 结果不会重复请求。
SQLite/PostgreSQL 原生 upsert 同时消除首次 insert race，并在数据库层原子执行
`complete > partial > rejected` 的质量优先级；后到的低质量结果不能降级已有缓存，status 与
payload 始终来自同一次获胜写入。

Semantic Schema v4 正式包含 `physical_demand`，并为每个事件保存 NASA-TLX 风格的
`workload_feature_vector` 与规则初始化 `workload_prior`。Workload v1 是独立可解释派生量：
事件预期、进行中与事后使用指数核，重叠事件按 `1-product(1-W)` 饱和合并；连续工作时长按
三小时饱和并施加有界增量。Current M0 保持阶段 3 行为作为稳定对照；Workload-aware M0 与
M1/M2/M3 候选把 W(t)、anticipation、aftermath、continuous load 和 recovery resource 接入
CTSSM。首版 recovery resource 只承认可审计的 calendar gap、protected break、sleep window 与
用户报告。候选在 rolling-origin 比较与 promotion gate 通过前不会替换生产 Current M0。

## CTSSM 时间语义

CTSSM trajectory 的每个 point-at-t 表示 t 时刻 observation assimilation 后的
posterior/current latent state。`00:00` 是初始/后验状态，`23:55` 是最后一个轨迹点，
独立 terminal 表示 `24:00`。`delta_S`/`delta_V` 是相邻已记录点之差；累计负荷只统计 t
之前已发生的区间，所以 09:00 开始的事件在 09:00 累计量不增加、09:05 才增加。
AlertMonitor 首点 elapsed 为 0，40 分钟确认阈值最早只能在实际经过 40 分钟后触发。
Daily Review v4 的 `curve_last_point_state` 明确表示 23:55，`terminal_state` 与
`forward_terminal_state` 从 Forecast `output` 的 24:00 terminal 计算。当前 v7 Forecast 缺少
显式 output terminal 时不会把 `curve[-1]` 当作午夜；只有旧 loop 版本保留 legacy fallback。

## Observation 与 Daily Review

新 Observation commit 会先在数据库内使同日 Forecast/Warning fail-closed，再由托管刷新服务
合并重算请求。普通重复提交不触发失效。Care 使用的近期 Observation 窗口为 6 小时。
因果 `as_of` 查询同时要求 `observed_at <= cutoff` 与 `created_at <= cutoff`，之后补录的历史
Observation 不会进入过去的 Daily Review。

Daily Review 是独立、追加写的回顾链。实际提交时间经过最终授权校验，回顾曲线保留因果
Forecast，且不改写原 Forecast。新的 retrospective terminal 会立即 fail-close eligible
下一日 Forecast，并进入托管后台重算；旧历史日期的 Admin rebuild 不触发无意义传播。
Bot 与独立 Admin 进程都管理 dependency refresh 生命周期；Admin reconstruction 在线程中
执行，不阻塞 HTTP event loop。

Daily Review 卡片的所有问题显式绑定回顾 `local_date`，次日补填仍要求回答前一日状态。
`peak_stress` 小于起始或收尾压力时保留原始回答并标记 `peak_consistency=false`，但不把该冲突值
作为峰值锚点；Admin 会显示冲突提示。`energy_consumption` 是可选研究诊断，不进入曲线锚点。

Forecast currentness 的 activate/invalidate/reactivate 在同事务写入 append-only history；
Daily Review 与 calibration 通过 `current_at(submitted_at)` 选择当时真正 current 的 Forecast，
不再用当前 `valid` 状态猜历史。Daily Review 只使用 `submitted_at` 之前已生效的 currentness
事件；没有历史可见 Forecast 时因果
source 明确为 NULL，不用之后生成的版本回填。Admin“重建（因果）”严格复用响应中持久化
的 causal forecast 和提交前 Observation；“重新分析（使用最新事实）”标记
`analysis_kind=reanalysis`，只返回临时分析，不替换 causal retrospective，也不成为下一日
terminal override。

## Warning、Care 与 Snooze

Warning 的每日上限、最小间隔、提前量、宽限、重试与 claim lease 来自单一配置。发送前
执行数据库 final authorization，事务提交释放 Participant/Warning/Care 锁后才调用飞书
网络接口。

所有 Warning/Care 状态同步路径使用 `Participant -> Warning -> CareIntervention` 锁顺序。
Snooze 只有在 durable child Warning 创建成功后，才把 intervention 写为 `snoozed`。关闭
`allow_follow_up` 会取消尚未最终授权的 user-requested pending、claimed 或
delivery-unavailable follow-up；claim 与 final validation 都重新检查当前偏好。
`allow_schedule_suggestions=false` 同样是发送 Hard Control：未最终授权的旧
`schedule_adjustment` 会立即取消，claim/final validation 也会重新检查；已 final-authorized
且 lease 有效的 in-flight send 仍遵守现有 commit-point 语义。

`micro_break` 是独立 intervention type，模板为 `micro-break-v1`，建议 2–5 分钟短暂补水、
活动或看远处。`protected_break` 保持 10–15 分钟真正脱离任务。micro-break 偏好只提升
`micro_break`，不会提升 `protected_break`。

未成功发送且因过期、投递失败或 `minimum_interval` 抑制错过的主动 Warning，可在同一天
重新构建当前 Calendar、近期 Observation、current Forecast 与偏好上下文。只有当前仍有
care relevance 才创建新行，`delivery_kind=same_day_late_care`，并使用“刚才”语义重新渲染；
Tier 3 或原 `pause_and_seek_support` 在当前 relevance 仍成立时保持同等级支持，不降级成
`brief_check_in`；
已发送、静音、停用或达到每日上限的机会不会补发。Repository 自身要求 final
authorization、有效 claim lease、current Forecast 且 `sent_at >= authorized_at`，否则拒绝
`sent` transition。

## Response、Admin 与历史兼容

最终回复先经安全检查、清理与确定性分段，再按展示模式决定是否调用展示模型。回复计划、
稳定消息 UUID 与发送进度均持久化，可在重启后恢复。`reply_text` 保存当前回复计划的
authoritative full text，`reply_segments_json` 保存当前分段；只有历史行缺少 segments 时，
`reply_text` 才额外承担 legacy single-segment recovery。
展示模型只能选择 SemanticSegmenter 批准的边界，权威 slice 不执行 `strip`，各段拼接后与
清理后的权威正文逐字一致，且不会切断 URL、链接或未闭合结构。

Admin 是独立 HTTP 服务，提供参与者、Forecast、Warning、Calendar、Daily Review、Care
Timeline 和运行事件查询。研究评估页按日期窗口展示 cohort 数据完整性、Forecast 误差、
Prediction Interval 校准与 Warning/Care 指标；参与者研究诊断展示 7/14 日趋势、参数历史、
分层残差及因果匹配明细。数据质量页可按参与者和日期过滤缺失、迟到、回填、降级、语义状态、
重复、合成与时间异常。Calendar 视图显示 snapshot state 与最近刷新结果，便于识别
mutation pending，而不会把诊断 events 当成可用 Forecast 输入。

## PostgreSQL 与迁移

当前 Alembic 唯一 head 是 `0030_model_promotion_decisions`。0017 增加 Warning/Daily Review
实际授权时间、Snooze provenance FK/唯一约束及 Calendar snapshot state。升级会将 0016
Warning JSON 中能与真实 CareIntervention UUID 匹配的 snooze provenance 安全回填；缺失或
无效值保留 NULL，不会因 UUID cast 失败阻断迁移。已有 `degraded=true` CalendarSnapshot
会回填为 `provider_degraded`，其余旧快照为 `current`。

0018 新增 `calendar_mutation_reconciliations`，使用 `work_json`（PostgreSQL JSONB）、
`last_error_class`、attempt/next-attempt 时间及 created/updated/resolved 审计字段，并为 due scan
与 participant history 建索引及 participant 外键。

0019 新增 append-only `forecast_currentness_events`。迁移只 seed 上线时已知的 current state，
不会伪造更早 activation chronology；严格 point-in-time history 从 0019 上线后保证。0020 为
OAuth token 增加 expiring refresh lease。claim 与 finalize 都是短事务，Feishu HTTP 在两者之间
且数据库事务已关闭；竞争进程等待 lease，过期 lease 可在崩溃后恢复。Device Flow 的同步
SQLAlchemy 读写全部在线程执行，不阻塞 asyncio loop。

生产启动依赖显式 Alembic upgrade，不在应用启动时 `create_all`。0021 将 Daily Review 中
仅作研究诊断的全天精力消耗改为可空字段。0022 增加追加式量表、事件评价与慢状态表，
并为学习参数补充不确定性、模型版本和验证状态。0023 为学习参数与 Slow State 增加数据库
CHECK；Forecast 只读取 validated 参数，或保留迁移前已生效且 `model_version=legacy` 的兼容行。
更新的 candidate 与 rejected 参数不会进入正式 Forecast。四层画像在 Admin 中分别显示 Explicit、
Psychometrics、Slow State 与 Learned Parameters，Schema/兼容字段与 Explicit 分开；Stage 1 不修改
CTSSM 主方程。0024 新增 `forecast_observation_matches`、`dataset_snapshots` 与
`model_evaluation_runs`；匹配记录使用观测时点当时生效的 Forecast，在最近五分钟 grid point
的 ±2.5 分钟内落库，并保留预测区间和分层诊断上下文。模型训练和比较通过 observation/calendar
cutoff、schema version 与 manifest 固定数据边界。0025 增加不可变 `dataset_snapshot_items`，
逐条冻结 Observation、causal Forecast、Currentness event、Calendar 表示及 Match Source，
manifest hash 由规范化合同与条目共同生成；Evaluation Run 只读取冻结条目，不重新选择 live 数据。
0025 创建的 legacy Dataset Schema v2 冻结 Observation、Forecast、Currentness、Calendar 与
Match Source，不伪造历史 membership；当前 evaluator 仍按原 v2 manifest 合同读取这些快照，
participant-specific 查询仅在不可变条目能证明参与者出现时允许。0026 后新快照使用 Dataset
Schema v3 并冻结 participant membership；零观测参与者仍保留在 cohort 中，participant_count
由 membership items 计算并纳入 manifest hash，participant-specific 零样本评估合法完成。
Stage 2 新评估运行记录 `stage2-evaluation.v3`，已有运行保持原 provenance 不变；Stage 4
新增运行记录 `stage4-evaluation.v6`。评估模式明确区分 `historical_online` 与执行候选模型族
Rolling-Origin 比较的 `offline_replay`。Instant Check-in
只接受 research contract 定义的正式 `checkin` 类型；其他 StateObservation 即使包含压力字段也
不会进入匹配、快照或 EMA 指标。Check-in 是用户主动观测，因此只报告 observed-day rate，
不推断 `missing_ema`；participant-day 分母从
参与者创建日开始计算。Peak 指标明确标记为至少两个匹配样本形成的 observed peak proxy，
并按 participant/date/forecast version 隔离。Stage 2 同样不修改 CTSSM 主方程。
可选的真实 PostgreSQL 集成测试从 0016 插入 current/degraded 数据后升级到 0017，
先升级到 0025 创建真实 v2 快照，再升级到 0026，验证 v2 快照原样保留且仍可评估，并验证
新 v3 快照的 participant membership、JSONB、
索引、FK、状态回填、旧数据保留与 reconciliation CRUD；只有配置 disposable
`MINDFLOW_TEST_POSTGRES_URL` 时才执行。

0027 为 Event Appraisal 增加事件类型、课程、workload feature/prior、Raw-TLX observed workload、
residual 与 estimator version；0028 进一步冻结事件日期、开始时间及 source Forecast/semantic/schema
provenance。`created_at` 是 repository 单次生成且不接受业务调用者输入的系统知识时间；反馈只从
`min(event_start_at, submitted_at, created_at)` 时实际 current 的 Forecast 读取 prior。不存在因果
Forecast、对应事件或完整有效的 workload context 时只保存 observed workload，不保存部分 provenance。
Admin Workload 的当前曲线明确为
`latest_descriptive`；EMA 与 0/5/10/15/30/60 分钟 lag 则固定使用 observation 因果时点的同一 Forecast，
并显示 workload bin 误差和按 event type、
course、participant 分层的 appraisal residual。

0029 扩展 Dataset Snapshot item contract，冻结 BRS psychometric、Daily Review recovery 和
Slow State recovery/sleep 证据。Stage 4 offline replay 对 Current M0、Workload-aware M0、M1、
M2、M3 使用同一不可变快照与 expanding rolling-origin split，统一输出 MAE、RMSE、Median AE、
峰值幅度/时序误差、90% coverage、interval width、high-stress precision/recall 与 PR-AUC。
晋级门槛要求 MAE 相对改善至少 3%，且 coverage、peak timing、high-stress recall 均不退化；
结果保留样本量和 participant-level effect。BRS 只进入 recovery coefficient 的 slow trait prior，
纵向 EMA episode 继续估计 workload reactivity、recovery coefficient、上升/恢复响应速率 κ 与
observed recovery efficiency。Admin 提供模型族比较表及参与者当前模型、候选模型和验证结果。

Stage 4 candidate evaluation 由 `stage4-real-ctssm-replay.v5` 直接调用真实
`AssessmentModel.predict_candidate → Simulator → step_latent_state`；目标时点 EMA 及未来 EMA
不会提前 assimilate，候选区间来自 `LatentUncertainty/prediction_interval`，峰值指标使用完整 288 点
trajectory。0030 新增 `model_promotion_decisions`：非 M0 只有在 completed offline replay、有效
Dataset manifest、兼容 evaluation/gate version 且 gate 通过后，才能写入
`retained_from_empirical_evidence`。Forecast 读取不到匹配的 durable provenance 或参数 hash 时一律
fail closed 到 Current M0。

`rolling-origin-knowledge-causal.v2` 以测试日当地 00:00 作为 split origin；训练 EMA 的
`created_at`、BRS 的 `max(administered_at, created_at)` 与 Slow State 的
`max(effective_at, created_at)` 均必须早于该 origin。测试日峰值以第一条 eligible EMA 的 causal
cutoff 为统一信息集，并复用 Dataset v4 冻结的 `initial_state` / `initial_state_revision`。
候选参数不确定性由回放 estimator 的标准误按 0–10→0–100 尺度转换；尚无可靠标准误的个体响应
rate 不进入晋级参数。晋级 confidence 使用样本、天数、transition 与参数不确定性的
`stage4-calibration-confidence.v1` 保守定义，最高为 0.95。

生产模型身份不再只依赖单一 algorithm label：Current M0 使用
`mindflow-ctssm-runtime-v7:m0`，已晋级 WM0/M1/M2/M3 使用 runtime v8，并在 Forecast output、
ForecastObservationMatch context 与 Dataset v4 中冻结 `model_family`、`model_variant`、
`model_spec_version`、`promotion_decision_id` 和 `promotion_parameters_hash`。Participant Admin
页面的 current model 读取 `runtime_active()`；cohort evaluation 单独标记，不作为个体验证结果。

Current M0 不再复用 Dataset 中可能已经属于 M1/M2/M3 的历史生产预测，而是与 WM0/M1/M2/M3
共同通过 `AssessmentModel → Simulator → step_latent_state` 真实回放；五个模型共享冻结 initial
state、daily causal origin、Calendar 与 known observations。原预测保留为独立
`historical_production` 指标，不参与 promotion gate。参数估计采用
`ridge-posterior-covariance.v1`：$\Sigma_\beta=\hat\sigma^2(X^TX+\lambda I)^{-1}$，分别报告三个
系数的标准误、设计条件数、可识别状态及 boundary clipping。`not_identified` 的 workload/recovery
参数不能晋级生产。

Current M0 的训练参数由 `m0-simulator-fit.v2` 在真实
`AssessmentModel → Simulator → M0` 上对 `S_star_init∈[0,100]` 执行粗粒度与 0.1 细粒度
training-only SSE 搜索；它不是压力均值，也不会增加新的 M0 参数。WM0/M1/M2/M3 使用
`workload-recovery-ridge.v2`。两者都只读取 split training evidence，`parameter_history`
按 family 冻结 fit method、training loss/window 与 parameter fit version。`ctssm-promotion-gate.v2` 将
identifiability 纳入正式 blocking check；`weak` 可通过但产生 warning，`not_identified` 必须失败，
boundary clipping 在 v2 中作为非阻断 warning 展示给 Admin。

Rolling-Origin 的最后一次/最大 training-window fit 另外冻结为
`evaluation_parameter_gate_evidence`，正式 Gate 的 identifiability 只从该证据聚合，不能读取最终
test label 或 `deployment_*`。仅当至少一个候选 Gate PASS 后，`stage4-deployment-refit.v1` 才在
不访问 live EMA/Profile 的前提下，对同一 Dataset v4 中目标参与者
截至 observation cutoff 的全部 eligible frozen evidence 重拟合，产出独立的
`deployment_parameters`、`deployment_uncertainty` 与 `deployment_evidence`。生产晋级只消费这些
deployment 字段；LearnedModelProfile 的 window、sample/day count 与 uncertainty 均来自真实 refit，
model-selection provenance 同时保存 snapshot、knowledge cutoff 和 refit version。
若所有 Gate 均失败，三个 deployment 字段保持空对象；若 evaluation Gate PASS 但 full-data refit
变为 `not_identified`，晋级执行以 `deployment_refit_not_identifiable` fail closed，原 OOS Gate 结果不变。

Participant promotion 在同一个数据库事务中写入 `ModelPromotionDecision` 与
`LearnedModelProfile`，任一写入失败会同时回滚。`historical_online` 支持完整
`model_identity_filter`，尤其可按 `promotion_decision_id` 与 `promotion_parameters_hash` 精确过滤；
evaluation config 冻结 resolved filter 及实际匹配的 decision/hash 集合。
来源审计同时保存 identity filtering 前 Dataset 全部候选来源的 `snapshot_source_set`，以及实际进入
metrics 的 `evaluation_source_set`；后者包含 observation/forecast/match hash 与精确 promotion
decision/parameters hash 集合。

M3 使用 `recovery-debt-dynamics.v1`：
$dF/dt=\alpha_FW(t)(1-F)-\lambda_FR(t)F$，并约束 $F\in[0,1]$。这一 bounded dynamics
使高负荷累积逐渐饱和、恢复只作用于已有恢复债，从而避免无界增长和负恢复债。

## 自动漂移保护

`tests/test_authoritative_docs.py` 校验本文的工具数、模型版本与 Alembic 唯一 head。专项测试
覆盖 Calendar fail-closed/degraded 分流、Today→Tomorrow 依赖、课程锁、Snooze、Care 偏好、
micro-break、工具 schema 和迁移；完整结果以每次当前测试运行输出为准，不在权威文档固定
长期 passed 数量。
