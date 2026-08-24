# MindFlow 当前生产架构

本文只描述 `production_runtime` 当前实现，不记录历史方案或未来设计。代码、迁移与
自动测试是最终事实来源。

<!-- BUSINESS_TOOL_COUNT: 13 -->
<!-- MODEL_VERSION: mindflow-ctssm-runtime-v6 -->
<!-- ALEMBIC_HEAD: 0015_daily_review_causal_source -->

## 运行边界

MindFlow 是一个面向飞书私聊的单体 Python 后端，业务数据保存在 PostgreSQL。飞书
WebSocket receiver 在独立的 `spawn` 子进程中运行，通过本地 IPC 向 Bot 进程发送普通
事件 DTO；子进程不解析参与者身份、不访问数据库，也不运行 Agent。

Bot 进程将入口事件先持久化为 `BotEvent`，再由 `BotWorker` 完成绑定、授权、安全检查、
Agent 调用、展示规划和可靠发送。事件去重、参与者隔离与最终回复恢复均以数据库状态为
准。只有私聊且已绑定、已同意外部 LLM 的参与者可以进入 Agent 路径。

## Agent 与 MCP

每位参与者拥有独立的顺序输入队列和可恢复 Claude session；全局 warm session 数量受限。
Claude Agent SDK / Claude Code Harness 使用 DeepSeek Anthropic-compatible endpoint。
Backend 禁用 Bash、文件系统、Web、Agent/Task 等内建工具，只加载 `mindflow-care` Skill
和 participant-bound SDK MCP。

当前 Registry 暴露 13 个业务 Tool：

1. `care_get_today_context`
2. `care_record_checkin`
3. `care_get_recent_state`
4. `care_run_today_assessment`
5. `care_get_support`
6. `care_get_pressure_curve`
7. `care_get_checkin_card`
8. `calendar_connection_status`
9. `calendar_list_calendars`
10. `calendar_list_events`
11. `calendar_create_event`
12. `calendar_update_event`
13. `calendar_delete_event`

所有 schema 都是封闭对象，并禁止参与者身份、飞书身份、Token、Secret、SQL、路径和任意
URL 字段。身份只来自冻结的 `AgentContext`。工具生命周期统一通过
`on_activity(AgentActivityEvent)` 发出 `tool_started`、`tool_succeeded` 或
`tool_failed`，真实事件由 MCP 执行边界产生。

## Forecast 与 CTSSM

正式模型是连续时间状态空间模型（CTSSM），`MODEL_VERSION` 为
`mindflow-ctssm-runtime-v6`。模型按参与者本地日计算 `00:00–24:00`，步长 5 分钟，
固定输出 288 个轨迹点。输入包括显式画像、学习画像、日历事件语义、近期观测和上一日
回顾收尾状态；输出包含压力/活力轨迹、置信度、告警和 provenance。

`ForecastCoordinator` 是正式评估的唯一业务入口。它同步日历、准备事件语义、生成并
持久化版本化 `ForecastSnapshot`，然后基于当前快照协调 Warning。旧 Markov、RK4、策略栈
和 SQLite 语义推理引擎不在运行路径中。

日历事件先经过确定性分类与本地课程目录检索。本地 resolver 基于生成的课程目录、受审查
的缩写别名和有界 Top-K 候选工作；显式类型、固定日常规则和精确课程匹配优先于外部语义
判断。课程相关作业、复习和考试仍是 task，只记录 related course。需要外部增强时，事件
分类、候选内课程选择和客观语义由同一次调用返回并进入同一版本化缓存；候选外课程会被
拒绝。缓存指纹包含事件、候选集、课程目录 revision、resolver、schema、prompt 和模型版本。

最终分类在生命周期和模型语义计算前完成。每个新 Forecast 的 `output_json` 都持久化
`classified_calendar_events` 以及分类、课程目录和语义版本，因此当前压力曲线、Warning、
Admin 和历史曲线复用同一份事件事实；历史展示不使用当前规则重新解释旧日历标题。

## Observation

即时 check-in 只允许通过受控卡片动作或 `care_record_checkin` 写入。仓储以参与者、来源和
幂等键区分新提交与重复提交。新提交成功后，同一参与者和本地日期的所有当前 Forecast
会立即失效，关联的待发/已认领 Warning 同一事务内取消并清空 claim；随后托管的刷新服务
按参与者/日期合并快速连续请求并异步重算。重复提交不失效、不重算。

刷新失败保持 fail-closed：旧 Forecast 不会重新变为 current，旧 Warning 不会恢复。
定时 Forecast 流程可在后续周期重新生成当前快照。

## Warning

Warning 的发送上限、最小间隔、提前量、迟到宽限、重试和 claim lease 都从 `Settings`
创建的一份 `WarningDeliveryPolicyConfig` 读取。Policy、Repository 与 Scheduler 共享该对象；
仓储仍负责跨进程 claim、当前 Forecast 校验、每日发送上限、最小间隔和幂等约束，避免只靠
进程内判断。

模型告警通过显式、有界的 DTO 保留 `current_events` 与 `dominant_stressors`，但模型侧文案
只作为降级 fallback。正常主动关怀由 Backend 根据风险时间查找前一项、进行中和下一项
日程，只抽取近期 check-in 与明确的关怀偏好，再经确定性 `CareInterventionPolicy` 和版本化
reviewed template 生成。非降级消息必须同时具有风险时间与日程/近期状态中的至少一项事实；
上下文不足时标记 `context_quality=degraded` 并使用通用降级模板。

`care_get_support` 与主动 Warning 复用同一 CareContext、Policy 和 Template 服务，区别仅在
来源是用户主动请求。Warning payload 持久化 message、plan、context 和 provenance；其中
包括 Warning/Forecast 标识、版本、模板、干预类型、日程 ID、Observation/Profile 版本。
Scheduler 只发送已持久化文案，不让 LLM 决定是否推送，也不改变原有 durable delivery 状态机。

## Daily Review

Daily Review 是独立的回顾反馈链。Scheduler 为 active 且已绑定的参与者创建每日任务，并
使用数据库租约、稳定消息 UUID、有效期和重试策略投递固定飞书卡片。验签回调由后端校验并
追加保存 revision，不进入 LLM。

回顾曲线由 Fixed Lag Smoother 与 Anchor/Smooth Residual Kernel 生成，保留因果来源
Forecast。它不会改写正式 Forecast 或 Warning 审计；收尾状态仅以受控增益进入下一日
Forecast provenance。该功能已经实现，但生产启用依赖可达的 HTTPS 卡片回调配置。

## Admin

Admin 是独立进程和独立 HTTP 服务。它提供角色化登录、参与者/Forecast/Warning/运行事件/
Daily Review 查询，以及明确授权的 Forecast 刷新和回顾重建。环境根账号同步为 active
superadmin；其他账号使用 `viewer`、`admin`、`superadmin` 权限边界。生产部署只应通过
loopback、SSH tunnel 或受保护的 HTTPS 入口访问。

## Response delivery

Safety 后的权威回答先经本地 sanitizer 和确定性分段，再按
`PRESENTATION_AGENT_MODE=off|adaptive|always` 决定是否调用展示模型。生产默认
`adaptive`；本地结果已满足 1–3 段投递容量时跳过第二模型。`mode` 是运行时唯一内部开关，
旧 `PRESENTATION_AGENT_ENABLED` 只在未设置 mode 时做启动期映射并输出一次弃用告警。

最终回复以 segment plan、下一段索引、provider message ID 和稳定 message UUID 持久化，
进程重启可从未发送段继续。运行时只写当前 plan 格式；仓储仍能读取历史单段
`reply_text`，恢复时记录 `legacy_reply_plan_recovered`。progress 只使用受控后端模板。

## PostgreSQL 与迁移

PostgreSQL 保存参与者、绑定与授权、OAuth Token、对话、Agent run/tool call、Claude
session、BotEvent、观测、预测输入/结果、Forecast、Warning、Daily Review、回顾曲线和
运维审计。OAuth Token 使用应用层加密，Secret 不进入 Prompt 或日志。

当前 Alembic 单一 head 是 `0015_daily_review_causal_source`。Bot 与 Admin 启动依赖
`migrate` 服务成功，不在应用启动时隐式创建或升级 schema。

## Docker 服务

`compose.yaml` 的运行单元为：

- `migrate`：执行 Alembic upgrade；
- `claude-state-init`：准备 Claude state volume 权限；
- `bot`：入口、Agent、Forecast、Warning、Daily Review 与卡片回调；
- `admin`：独立管理服务；
- `postgres`：唯一业务数据库；
- `postgres_data` 与 `claude_state`：持久卷。

Bot credential 必须显式配置为 `FEISHU_BOT_APP_ID` / `FEISHU_BOT_APP_SECRET`。
Calendar ID/Secret 两项都留空时复用 Bot credential；显式提供时必须成对配置。

## 自动漂移保护

`tests/test_authoritative_docs.py` 会把 README 和本文声明的 Tool 数与当前
`ToolRegistry` 比较，把本文的 `MODEL_VERSION` 与正式 `AssessmentModel` 比较，并验证
本文声明的 Alembic head 是迁移图的唯一 head。修改工具、模型或迁移时必须同步更新本文。
