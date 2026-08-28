# MindFlow 当前生产架构

本文只描述 `production_runtime` 当前实现。代码、迁移与自动测试是最终事实来源。

<!-- BUSINESS_TOOL_COUNT: 15 -->
<!-- MODEL_VERSION: mindflow-ctssm-runtime-v7 -->
<!-- ALEMBIC_HEAD: 0020_oauth_refresh_lease -->

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

## CTSSM 时间语义

CTSSM trajectory 的每个 point-at-t 表示 t 时刻 observation assimilation 后的
posterior/current latent state。`00:00` 是初始/后验状态，`23:55` 是最后一个轨迹点，
独立 terminal 表示 `24:00`。`delta_S`/`delta_V` 是相邻已记录点之差；累计负荷只统计 t
之前已发生的区间，所以 09:00 开始的事件在 09:00 累计量不增加、09:05 才增加。
AlertMonitor 首点 elapsed 为 0，40 分钟确认阈值最早只能在实际经过 40 分钟后触发。
Daily Review v3 的 `curve_last_point_state` 明确表示 23:55，`terminal_state` 与
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
Timeline 和运行事件查询。Calendar 视图显示 snapshot state 与最近刷新结果，便于识别
mutation pending，而不会把诊断 events 当成可用 Forecast 输入。

## PostgreSQL 与迁移

当前 Alembic 唯一 head 是 `0020_oauth_refresh_lease`。0017 增加 Warning/Daily Review
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

生产启动依赖显式 Alembic upgrade，不在应用启动时 `create_all`。可选的真实 PostgreSQL
集成测试从 0016 插入 current/degraded 数据后升级到 0017，再升级到 0020，并验证列、JSONB、
索引、FK、状态回填、旧数据保留与 reconciliation CRUD；只有配置 disposable
`MINDFLOW_TEST_POSTGRES_URL` 时才执行。

## 自动漂移保护

`tests/test_authoritative_docs.py` 校验本文的工具数、模型版本与 Alembic 唯一 head。专项测试
覆盖 Calendar fail-closed/degraded 分流、Today→Tomorrow 依赖、课程锁、Snooze、Care 偏好、
micro-break、工具 schema 和迁移；完整结果以每次当前测试运行输出为准，不在权威文档固定
长期 passed 数量。
