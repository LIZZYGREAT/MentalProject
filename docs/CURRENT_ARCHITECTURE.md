# MindFlow 当前生产架构

本文只描述 `production_runtime` 当前实现。代码、迁移与自动测试是最终事实来源。

<!-- BUSINESS_TOOL_COUNT: 15 -->
<!-- MODEL_VERSION: mindflow-ctssm-runtime-v6 -->
<!-- ALEMBIC_HEAD: 0017_care_delivery_authorization -->

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

Today Forecast terminal 是 Tomorrow initial state 的依赖。直接修改 Today Calendar 时，
Today 使用 Calendar mutation invalidation；Tomorrow 只做 Forecast dependency dirty，不修改
Tomorrow CalendarSnapshot、revision 或 events。统一的 dependency refresh service 只承认
Yesterday→Today 和 Today→Tomorrow 两条边。Observation、semantic enrichment 与 Calendar
输入一旦变化，会在 source 重算前立即 fail-close dependent；source 成功后才刷新 dependent，
失败则保持 invalid。Daily Review retrospective ready 后也立即失效并托管刷新 eligible
dependent；历史 rebuild 不传播，也不会递归到 Day+2。

Tomorrow 解析 Today terminal 时使用 `refresh_calendar=False`；Today 无快照时仍会按需读取，
但不会因为刷新 Tomorrow 而无条件重复调用 Today Calendar provider。

## Course classification

事件分类先执行确定性规则与有界课程目录检索，再在已授权时使用同一次 semantic API 完成
语义增强。`event_type_locked` 与 `course_identity_locked` 是独立事实：例如“高数”可以锁定
为 course，但 canonical identity 仍由 API 从 Top-K 候选中选择；“线代”精确解析为“线性
代数”时才锁定 identity。API 返回的课程必须属于候选集合。

`高数A/高数B` 只提高相应类别候选排序。`高数I/II/1/2/3` 仅作为模糊检索提示，不会用
`candidates[0]` 锁成错误课程。课程相关作业、复习和考试保持 task；rules-only 只有在去除
任务意图词后仍能 exact resolve 时才写 canonical related course。模糊结果只保留 Top-K
catalog context，等 semantic API 返回候选内的 validated match 后再写 canonical identity。
普通负例不会被目录模糊命中提升为 course。

## Observation 与 Daily Review

新 Observation commit 会先在数据库内使同日 Forecast/Warning fail-closed，再由托管刷新服务
合并重算请求。普通重复提交不触发失效。Care 使用的近期 Observation 窗口为 6 小时。

Daily Review 是独立、追加写的回顾链。实际提交时间经过最终授权校验，回顾曲线保留因果
Forecast，且不改写原 Forecast。新的 retrospective terminal 会立即 fail-close eligible
下一日 Forecast，并进入托管后台重算；旧历史日期的 Admin rebuild 不触发无意义传播。

## Warning、Care 与 Snooze

Warning 的每日上限、最小间隔、提前量、宽限、重试与 claim lease 来自单一配置。发送前
执行数据库 final authorization，事务提交释放 Participant/Warning/Care 锁后才调用飞书
网络接口。

所有 Warning/Care 状态同步路径使用 `Participant -> Warning -> CareIntervention` 锁顺序。
Snooze 只有在 durable child Warning 创建成功后，才把 intervention 写为 `snoozed`。关闭
`allow_follow_up` 会取消尚未最终授权的 user-requested pending、claimed 或
delivery-unavailable follow-up；claim 与 final validation 都重新检查当前偏好。

`micro_break` 是独立 intervention type，模板为 `micro-break-v1`，建议 2–5 分钟短暂补水、
活动或看远处。`protected_break` 保持 10–15 分钟真正脱离任务。micro-break 偏好只提升
`micro_break`，不会提升 `protected_break`。

## Response、Admin 与历史兼容

最终回复先经安全检查、清理与确定性分段，再按展示模式决定是否调用展示模型。回复计划、
稳定消息 UUID 与发送进度均持久化，可在重启后恢复。历史 `reply_text` 只用于读取旧记录。
展示模型只能选择 SemanticSegmenter 批准的边界，权威 slice 不执行 `strip`，各段拼接后与
清理后的权威正文逐字一致，且不会切断 URL、链接或未闭合结构。

Admin 是独立 HTTP 服务，提供参与者、Forecast、Warning、Calendar、Daily Review、Care
Timeline 和运行事件查询。Calendar 视图显示 snapshot state 与最近刷新结果，便于识别
mutation pending，而不会把诊断 events 当成可用 Forecast 输入。

## PostgreSQL 与迁移

当前 Alembic 唯一 head 是 `0017_care_delivery_authorization`。0017 增加 Warning/Daily Review
实际授权时间、Snooze provenance FK/唯一约束及 Calendar snapshot state。升级会将 0016
Warning JSON 中能与真实 CareIntervention UUID 匹配的 snooze provenance 安全回填；缺失或
无效值保留 NULL，不会因 UUID cast 失败阻断迁移。已有 `degraded=true` CalendarSnapshot
会回填为 `provider_degraded`，其余旧快照为 `current`。

生产启动依赖显式 Alembic upgrade，不在应用启动时 `create_all`。可选的真实 PostgreSQL
集成测试从 0016 插入 current/degraded 数据后升级到 0017，并验证列、FK、唯一约束、状态
回填与旧数据保留；只有配置 disposable `MINDFLOW_TEST_POSTGRES_URL` 时才执行。

## 自动漂移保护

`tests/test_authoritative_docs.py` 校验本文的工具数、模型版本与 Alembic 唯一 head。专项测试
覆盖 Calendar fail-closed/degraded 分流、Today→Tomorrow 依赖、课程锁、Snooze、Care 偏好、
micro-break、工具 schema 和迁移；完整结果以每次当前测试运行输出为准，不在权威文档固定
长期 passed 数量。
