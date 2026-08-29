---
document_status: ACTIVE
project_status: NO_GO
branch: production_runtime
last_reviewed: 2026-08-29
owner: project_operator
superseded_by: null
archive_rule: 所有必须项完成并形成证据后，将 document_status 改为 COMPLETED；若被新台账替代，改为 OBSOLETE 并填写 superseded_by。
---

# MindFlow 生产 Runtime 上线任务台账

这是从“代码准备完成”到“20 人飞书实验可启动”的唯一任务台账。README 负责说明系统怎么使用；本文件只管理尚未完成的人工工作、验收证据和上线结论。

## 状态规则

| 状态 | 含义 | 使用规则 |
|---|---|---|
| `TODO` | 尚未开始 | 必须写清下一步和验收标准 |
| `DOING` | 正在处理 | 同一时间尽量只保留 1–2 项 |
| `BLOCKED` | 被外部条件阻塞 | 在备注中写明阻塞原因和解除条件 |
| `DONE` | 已验收 | 必须填写完成日期和证据路径，不能只凭口头确认 |
| `OBSOLETE` | 已过时或不再需要 | 写明原因；不要直接删除历史记录 |

维护时只需要更新任务表中的“状态、完成日期、证据/备注”。每次准备上线前，同时更新顶部的 `last_reviewed` 和 `project_status`。

## 当前结论

| 项目 | 当前值 |
|---|---|
| 代码与自动测试 | `READY`（以 `python -m pytest -q tests` 当前结果为准） |
| 业务 Tool | `15`（由文档漂移测试校验） |
| Alembic head | `0024_research_evaluation` |
| Daily Review | `IMPLEMENTED`（生产启用仍依赖 HTTPS 卡片回调） |
| Admin | `IMPLEMENTED`（生产访问仍需账号与安全入口配置） |
| 真实环境配置 | `TODO` |
| 两人真实飞书联调 | `TODO` |
| 20 人实验启动 | `NO_GO` |

## 下一步任务

按编号顺序执行。`P0` 全部完成前，不开始 20 人实验。

| ID | 优先级 | 任务 | 状态 | 验收标准 | 完成日期 | 证据/备注 |
|---|---|---|---|---|---|---|
| ENV-01 | P0 | 补齐 `mindflow-bot-runtime/.env` | TODO | 数据库、加密 Key、`CLAUDE_ANTHROPIC_BASE_URL`、主模型以及 Opus/Sonnet/Haiku/Subagent 的 DeepSeek 映射均已配置；真实值未进入 Git | — | Opus/Sonnet 使用 v4-pro，Haiku/Subagent 使用 v4-flash；不在本文件记录真实 ID 或 Secret |
| SEC-01 | P0 | 轮换曾在旧环境或聊天记录中出现过的飞书与 DeepSeek Secret | TODO | 新 Secret 生效，旧 Secret 已失效，仓库敏感信息扫描无命中 | — | 只记录轮换完成，不记录 Secret |
| DEP-01 | P0 | 首次构建并启动生产容器 | TODO | `docker compose up --build -d` 成功；`bot`、`admin`、`postgres` 健康；Alembic head `0024_research_evaluation` 与 Agent SDK import 成功 | — | 保存脱敏后的 `docker compose ps`、`alembic current` 和启动日志 |
| SDK-01 | P0 | 云端 Claude Agent SDK -> DeepSeek smoke | TODO | 容器内 `ClaudeSDKClient` 使用 DeepSeek Anthropic endpoint 返回结果；没有 Anthropic 官方模型 fallback | — | 保存模型名、状态和脱敏延迟，不保存 Prompt/Key |
| SDK-02 | P0 | 验证 session queue、`/stop` 与 progress | TODO | busy 时新消息顺序排队；`/stop` 中断当前 turn；progress 模板限频且 final 始终由 Backend 发送 | — | 使用脱敏 message/session ID |
| FS-01 | P0 | 核对飞书应用配置 | TODO | 机器人长连接已开启；订阅 `im.message.receive_v1`；日历和 `offline_access` 权限已审批 | — | 截图或审批记录路径 |
| UAT-01 | P0 | 创建 P001、P002 并导入去标识化画像 | TODO | 两名参与者存在且画像版本可查询；画像中无学号、手机号等直接身份信息 | — | 使用 `profiles/profile.example.json` |
| UAT-02 | P0 | 记录两名参与者的外部 LLM 实验授权 | TODO | P001、P002 的 `external_llm_consent` 均为 granted | — | 保存脱敏的管理命令结果 |
| UAT-03 | P0 | 两个真实飞书账号分别完成一次性绑定 | TODO | 两个账号映射到不同 participant；重复使用绑定码失败；不能交叉访问数据 | — | 记录测试账号代号和结果，不记录 open_id |
| CAL-01 | P0 | 两名参与者分别完成 `/calendar` Device Flow | TODO | 两人均显示 connected；重复日程可读；Token 不串用 | — | 保存脱敏的授权与状态证据 |
| E2E-01 | P0 | 完成双用户消息与算法端到端测试 | TODO | 两人能独立打卡、读取近期状态、运行当日评估并获得回复；预测输入/输出已入库 | — | 使用测试日期和脱敏消息 ID 作为证据 |
| REL-01 | P0 | 验证重启恢复 | TODO | 制造待处理消息或待发送回复后重启 `bot`；消息最终只产生一个业务结果且回复成功 | — | 保存重启前后状态与日志 |
| REL-02 | P0 | 验证 Claude SDK / DeepSeek 异常降级 | TODO | 超时、429 或 5xx 时返回固定安全提示，不自动重放整段 turn，不泄露内部错误 | — | 优先在隔离测试环境完成 |
| REL-03 | P0 | 验证 Claude transcript 与 session 恢复 | TODO | P001/P002 session 不同；重建 bot container 后 P001 仍 resume 原 session；`claude_state` 未丢失 | — | 保存脱敏 session 前缀和 volume 检查结果 |
| OPS-01 | P0 | 检查日志与备份策略 | TODO | 日志不含消息全文、Token、Secret；PostgreSQL 有可恢复备份并完成一次恢复演练 | — | 填写保留周期和恢复演练记录 |
| GO-01 | P0 | 两人试运行 Go/No-Go 评审 | TODO | 上述所有 P0 任务均为 `DONE`，无未处理 P0 缺陷；将顶部 `project_status` 改为 `READY_FOR_PILOT` | — | 评审人、日期和结论 |
| PILOT-01 | P1 | 分批创建其余 18 名参与者 | TODO | 每人具有唯一 participant code、画像、授权记录和一次性绑定码 | — | 不在 Git 中保存真实身份映射 |
| PILOT-02 | P1 | 20 人实验首日监控 | TODO | 首日无跨用户数据、重复处理、消息丢失或持续失败；问题已有编号与处置人 | — | 保存脱敏日报 |

## 已完成的代码基线

这些项目已经由当前分支实现；除非回归失败，不要重新列为待办。

| ID | 状态 | 已实现内容 | 验证方式 |
|---|---|---|---|
| BASE-01 | DONE | PostgreSQL 事件幂等、完整载荷保存和重启恢复 | 自动测试 |
| BASE-02 | DONE | participant Session Manager 顺序 queue、跨用户隔离和显式 interrupt | 自动测试 |
| BASE-03 | DONE | ClaudeSDKClient 配置、session metadata 持久化、timeout 回收与固定降级 | 自动测试 |
| BASE-04 | DONE | 十五个 participant-bound SDK MCP Tool、封闭 schema 与身份隔离 | 自动测试（历史完成基线：2026-08-25） |
| BASE-05 | DONE | OAuth Token AES-256-GCM 加密与参与者隔离 | 自动测试 |
| BASE-06 | DONE | 完整预测输入快照、轨迹和告警留存 | 自动测试 |
| BASE-07 | DONE | Agent SDK Docker、Claude state volume、Alembic `0023` head 和生产配置模板 | 自动迁移、静态验证与可选真实 PostgreSQL 0016→0023 集成测试 |
| BASE-08 | DOING | FeishuChannel 独立 receiver process、稳定 IPC DTO、字段映射、dedupe 与 queue-full 恢复 | 本地自动测试通过；待 ECS WebSocket smoke、restart_count=0 与旧 event-loop warning 消失后验收 |
| BASE-09 | DONE | Backend progress policy、可靠 final delivery 与 `/stop` 控制路径 | 自动测试 |
| BASE-10 | DONE | 独立 Admin 服务、角色权限、Forecast/回顾查询与显式运维操作 | 自动测试（历史完成基线：2026-08-25） |
| BASE-11 | DONE | Daily Review 调度、卡片回调、revision 留存、回顾曲线与因果来源 | 自动测试（历史完成基线：2026-08-25） |
| BASE-12 | DONE | Phase A Care 偏好、规范化干预/反馈、可恢复卡片动作与 Admin Care Timeline | 自动测试（2026-08-25） |

## 文档生命周期

- `ACTIVE`：仍有需要执行或复核的任务，是当前唯一有效台账。
- `COMPLETED`：所有任务已完成；保留作为实验上线记录，不再新增任务。
- `OBSOLETE`：已被其他文件替代。必须在 `superseded_by` 填写新文件路径，并在文件顶部明显保留该状态。
- 不要为每日进度复制本文件。新发现的问题直接增加新 ID，并在证据中链接日志、截图或工单位置。
