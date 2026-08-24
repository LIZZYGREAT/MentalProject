# MindFlow Care / Feedback / Admin Roadmap — Phase A Implementation Report

## 1. 结论

Phase A 已完成本地实现与全量自动回归。原有 Forecast Warning 现在会在同一事务中形成
规范化 Care Intervention，支持持久化用户控制、可信卡片回调、幂等动作、独立追加反馈和
Admin Care Timeline。

本报告只覆盖 Roadmap Phase A。`/mindflow` 主入口、Onboarding、Today Brief、产品反馈、
研究 Dashboard、导出和 Phase B–D 的其他能力不在本次范围内。

## 2. 已实现范围

### A0–A2：合同、偏好、事件与反馈

- 新增 `ParticipantCarePreference`，保存 Care/Warning/Daily Review 开关、安静时段、
  用户级每日上限、跟进开关和受审查的支持偏好。
- 系统 `max_daily_sends` 仍是硬上限；用户只能收紧，不能放宽。偏好在生成筛选、待发任务
  取消和发送 claim 三处执行，claim 事务是最终权威边界。
- 新增 `CareInterventionEvent`，一对一关联来源 Warning，并保留 Forecast、模板、原因、
  上下文、动作集合、文案、投递状态与用户动作。
- 新增 append-only `CareInterventionFeedback`。Helpful、Not Relevant、Too Early、
  Too Late 只追加反馈，不修改 Forecast、不写 Observation，也不把 Care 状态误改为 Ack。

### A3–A5：模板、策略与 Warning → Care

- 沿用并扩展确定性的 `CareContext`、`CareInterventionPolicy` 和版本化 reviewed templates。
- 参与者受控偏好拥有独立 provenance；没有受控偏好时仍兼容既有显式画像偏好，不混淆
  `profile_version` 与 `care_preference_version`。
- Warning 的创建、claim、重试、发送、不可投递、取消、过期和失败均同步更新对应 Care
  事件。Feedback 与用户动作不会反向污染模型 Forecast。
- 迁移会为现有 Warning 审计记录回填一对一 Care 事件，升级后 Admin Timeline 不会只看到
  新数据。

### A6–A8：卡片、动作与快速反馈

- 新增固定 Feishu Care Card，仅输出 allowlist 中的 Ack、Snooze 30、Mute Today、
  Helpful、Not Relevant。
- 只有已配置并启用验签 Card Callback ingress 时才发送 Care Card；否则继续发送纯文本。
  缺少有效动作的旧 Warning 也走纯文本降级。
- callback 先通过现有 Feishu 验签入口与 participant binding，再由固定后端处理，不进入 LLM。
- callback event ID 和稳定 Warning UUID 分别用于动作幂等与消息投递幂等。
- Snooze 创建新的 durable pending Warning；它可以跳过最小发送间隔，但仍受用户/系统每日
  上限、同日约束和跟进开关限制。
- Mute Today 持久化到参与者偏好，并取消该参与者当天剩余待发 Care，不影响其他参与者。

### A9：Admin Care Timeline

- 新增 participant-bound `/admin/api/participants/{participant_code}/care-timeline`。
- Admin 用户详情新增 Care Timeline 标签，展示当前偏好、来源 Forecast/Warning、模板、
  文案、上下文、投递状态、Ack/Snooze/Mute 和全部追加反馈。

### Skill 与业务工具

- Skill 增加 Care 用户控制与反馈路由规范。
- 新增 `care_update_preferences` 与 `care_respond_to_latest_intervention`。
- 生产 Tool Registry 从 13 个更新为 15 个；schema 继续封闭且不接受 participant identity。

## 3. Schema 与迁移

- Alembic head：`0016_care_intervention_feedback`
- 新表：
  - `participant_care_preferences`
  - `care_intervention_events`
  - `care_intervention_feedback`
- 迁移保持线性历史，并包含既有 Warning → Care 的 PostgreSQL 回填 SQL。
- 已通过 PostgreSQL dialect 的 Alembic offline SQL 生成检查。

## 4. 关键安全与一致性约束

- 主动发送决策来自 Backend policy/repository，不由 LLM 决定。
- participant identity 只来自绑定后的后端上下文；跨参与者 Care action 被拒绝。
- per-participant 锁作为偏好更新、Warning claim 和 Care action 的串行化根，减少并发下的
  偏好/发送竞态与反向锁顺序。
- Forecast 原始曲线和模型告警不被 Care feedback 改写；Warning/Care 只维护派生投递状态。
- Card forwarding 被禁用；任意未知 action 不会进入卡片或后端执行。
- Daily Review 同样尊重 Care 总开关、自己的功能开关、Mute Today 与安静时段。

## 5. 验证结果

- Phase A 专项测试覆盖：偏好硬上限、安静时段、来源 provenance、Warning/Care 状态镜像、
  Ack/Snooze/Mute、反馈隔离、跨用户拒绝、callback 幂等、卡片 allowlist、发送崩溃后的重启
  恢复、Daily Review 关闭后的重启持久性、Admin API/UI Timeline。
- 全量回归：`402 passed`。
- Alembic PostgreSQL offline upgrade SQL：生成成功。
- Python compileall 与 `git diff --check`：通过。
- 现有 15 条 warning 来自第三方 Starlette/Matplotlib/PyParsing deprecation，不是本次功能
  失败。

## 6. 仍需真实环境完成的验收

本地代码无法替代外部生产配置。ECS/飞书上线前仍需按 `PROJECT_TASKS.md` 完成：

- 在真实 PostgreSQL 上执行并确认 migration head；
- 配置公网 HTTPS、反向代理和 Feishu Card Callback 验签参数；
- 双用户真实绑定与卡片动作隔离测试；
- 容器重启后的 provider message UUID 去重验证；
- 日志脱敏、备份恢复与 Go/No-Go 评审。

因此本报告表示 Phase A 代码基线完成，不把尚未执行的 ECS/飞书外部验收标记为完成。
