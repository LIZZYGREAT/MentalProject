# MindFlow 生产飞书 Runtime

这是面向约 20 名实验参与者的独立生产后端。生产 Agent 路径固定为：

```text
FeishuChannel（独立 receiver process）
  -> durable BotEvent
  -> BotWorker（binding / consent / calendar / reliable delivery）
  -> ParticipantSessionManager（queue / interrupt / bounded warm pool）
  -> ClaudeSDKClient / Claude Code Harness
  -> DeepSeek Anthropic-compatible API
  -> participant-bound MindFlow SDK MCP
  -> CareTools / AssessmentModel / Calendar / PostgreSQL
  -> Backend final delivery
```

飞书 WebSocket 只在由 `spawn` 创建的 receiver 子进程中导入和运行。子进程拥有
`lark-channel-sdk` 的模块级 event loop，并通过本地 IPC 发送普通事件 DTO；身份解析、
数据库持久化与去重、业务队列、Claude 和飞书 HTTP 回发仍留在 Backend 进程。

Claude Code 是 Agent Harness，DeepSeek 是模型提供方，MCP 是业务能力边界，
PostgreSQL 保存 identity、状态、审计和 Claude session metadata。生产代码中没有
Direct `DeepSeekClient.chat()`，Agent SDK 失败时也不会绕过 Claude Code。

## 安全边界

- 未绑定用户只能执行 `/bind`；仅允许私聊。
- `external_llm_consent_at` 为空时不会创建 Claude SDK client。
- `/calendar` 由 Backend Device Flow 处理。
- `/stop` 只中断当前 participant 的 active turn；普通新消息默认排队。
- Claude built-in tools 只保留指定 Skill；Bash、Read、Write、Edit、Web、Agent 等明确禁止。
- 生产 Skill 通过唯一的本地 `mindflow-care` 插件显式加载；`setting_sources=[]`，不读取用户或项目的隐式 Claude 配置。
- SDK MCP 只暴露十五个业务 Tool，participant identity 只来自 frozen `AgentContext`。
- 飞书 `card.action.trigger` 通过独立的已验签 HTTPS 回调入口进入固定后端处理器；卡片回调不经过对话模型。
- 最终回复由 Backend 持久化、重试和恢复；progress 使用受控固定模板。
- 应用读取配置后会把父进程环境收敛到运行白名单；Claude 子进程只显式获得 DeepSeek endpoint、模型名和认证 Token。
- `.env`、数据库密码、飞书 Secret、DeepSeek Key 和 OAuth Token 不进入 Prompt、Tool schema 或 Claude stderr 日志。

<!-- BUSINESS_TOOL_COUNT: 15 -->

## 十五个业务 Tool

1. `care_get_today_context`
2. `care_record_checkin`
3. `care_get_recent_state`
4. `care_run_today_assessment`
5. `care_get_support`
6. `care_update_preferences`
7. `care_respond_to_latest_intervention`
8. `care_get_pressure_curve`
9. `care_get_checkin_card`
10. `calendar_connection_status`
11. `calendar_list_calendars`
12. `calendar_list_events`
13. `calendar_create_event`
14. `calendar_update_event`
15. `calendar_delete_event`

所有参数 schema 都设置 `additionalProperties: false`，并禁止 participant、飞书
身份、Token、Secret、SQL、路径和 URL 字段。Tool 调用继续经过 `ToolRegistry` 的
校验、安全摘要和 AgentRun 审计。

## 对话与意图路由

当前不增加第二个“意图识别 Agent”。主 Agent 默认进行自然日常对话，只在请求依赖
个人记录、模型结果、卡片或日历时调用受限 Skill/Tool。读取操作按需执行；创建、更新
需要用户直接提出；删除必须先锁定唯一日程并明确确认。卡片按钮和问卷提交不进入
LLM，而由固定后端 action allowlist 处理。

只有当业务域和 Tool 数量继续显著扩大，并且线上审计数据证明单 Agent 路由出现稳定、
可复现的误调用时，才考虑增加轻量意图分类层。该分类层只能提供路由建议，不能获得
日历写权限，也不能替代各写 Tool 自身的确认、校验与幂等边界。

## 配置

`mindflow-bot-runtime/.env` 是唯一真实配置文件。把 `.env.example` 中的新变量合并进去：

- `FEISHU_BOT_APP_ID`、`FEISHU_BOT_APP_SECRET`：Lizzy 的 WebSocket ingress、回复和 Warning sender。
- `FEISHU_CALENDAR_APP_ID`、`FEISHU_CALENDAR_APP_SECRET`：Calendar OAuth、Token 和 Calendar API provider（测试环境为“喵学姐”）。若正式 Bot App 已有 Calendar 权限，可将两项都留空，自动复用 Bot credential。
- “喵学姐”需在开放平台开通 `calendar:calendar:readonly`、`calendar:calendar.event:create`、`calendar:calendar.event:update` 和 `calendar:calendar.event:delete` 用户权限并发布应用版本。新增权限后，已有参与者需要重新发送 `/calendar` 完成授权。
- Bot 应用需要配置新版卡片回传交互请求地址（`https://你的域名/feishu/card/callback`），并把同一组 Verification Token 与 Encrypt Key 写入 Runtime。每日状态问卷与主动 Care 卡片都由固定后端校验；未启用可信回调时，主动 Care 自动降级为纯文本。
- 设置 `FEISHU_CARD_CALLBACK_ENABLED=true`，并配置 `FEISHU_CARD_CALLBACK_HOST`、`FEISHU_CARD_CALLBACK_PORT`、`FEISHU_CARD_CALLBACK_PATH`、`FEISHU_CARD_VERIFICATION_TOKEN` 和 `FEISHU_CARD_ENCRYPT_KEY`。生产环境应由反向代理提供公网 HTTPS，只把回调路径转发到 Bot 容器端口。
- `DEEPSEEK_API_KEY`
- `CLAUDE_ANTHROPIC_BASE_URL=https://api.deepseek.com/anthropic`
- `CLAUDE_MODEL`：主会话模型或 Claude Code alias。
- `CLAUDE_DEFAULT_OPUS_MODEL`、`CLAUDE_DEFAULT_SONNET_MODEL`：都填写云端已验证的 DeepSeek `v4-pro` 模型 ID。
- `CLAUDE_DEFAULT_HAIKU_MODEL`、`CLAUDE_CODE_SUBAGENT_MODEL`：都填写云端已验证的 DeepSeek `v4-flash` 模型 ID；两者不一致时启动会拒绝配置。当前生产权限仍禁止 `Agent/Task`，不会因此开放子代理能力。
- `POSTGRES_PASSWORD`、`DATABASE_URL`
- `TOKEN_ENCRYPTION_KEY`
- session pool、timeout 和 progress policy 参数

不要保留旧的 `DEEPSEEK_BASE_URL`、`DEEPSEEK_MODEL`、`BOT_HISTORY_LIMIT`、
`AGENT_MAX_TOOL_STEPS` 或 Direct API retry 配置。

Bot credential 必须使用 `FEISHU_BOT_APP_ID` / `FEISHU_BOT_APP_SECRET`，缺失时
Runtime 会拒绝启动。Calendar credential 若显式配置，ID 与 Secret 必须同时提供；
两项都留空时复用 Bot credential。

测试阶段的身份关系是：Lizzy 下的 `open_id` 只用于 Bot binding，“喵学姐”只负责
Calendar OAuth。`open_id` 是 App-scoped identity，不能跨 App join；MindFlow 内部统一
身份始终是 `participant_id`。

生成 Token 加密 Key：

```powershell
cd mindflow-bot-runtime
conda activate MentalProject
python -c "from app.services.token_service import TokenEncryptionService; print(TokenEncryptionService.generate_key())"
```

## 容器启动

```powershell
cd mindflow-bot-runtime
docker compose up --build -d
docker compose ps
docker compose logs -f bot
```

生产验收期间用标准 smoke override 启动 Bot（`restart=no`、Rules-only、单 forecast
并发）：

```bash
docker compose -f compose.yaml -f compose.smoke.yaml up -d --no-deps bot
```

验收结束并正式上线后，改回只使用主 `compose.yaml`，恢复生产 restart policy。

可在 ECS 容器内单独验证 WebSocket receiver 的连接、存活和关闭（不会打印 Secret）：

```powershell
docker compose run --rm bot python -m app.smoke.feishu_gateway --seconds 30
```

Agent SDK Python 包自带固定版本的 Claude Code runtime，不依赖宿主机安装的
`claude`。Compose 把 `/home/mindflow/.claude` 挂到 `claude_state` volume，确保
container recreate 后 transcript 仍可用于 `resume=session_id`。

容器内检查：

```powershell
docker compose exec bot python -c "import claude_agent_sdk; print('sdk ok')"
docker compose exec bot python -c "from app.config import Settings; s=Settings.from_env(); print(s.claude_model, s.claude_workdir)"
```

## 创建参与者

```powershell
Copy-Item .\profiles\profile.example.json .\profiles\P001.json
docker compose exec bot python -m app.admin create-participant P001
docker compose cp .\profiles\P001.json bot:/tmp/P001.json
docker compose exec bot python -m app.admin set-profile P001 /tmp/P001.json
docker compose exec bot python -m app.admin set-llm-consent P001
```

第一条命令只显示一次 `/bind <code>`；数据库仅保存绑定码 Hash。撤回外部 LLM
授权：

```powershell
docker compose exec bot python -m app.admin set-llm-consent P001 --revoke
```

## Admin 账号与密码 Hash

`ADMIN_USERNAME` / `ADMIN_PASSWORD_HASH` 是环境根账号。Admin 启动时会把它
同步到 `admin_users` 表，并强制保持 `superadmin + active`；数据库里的其他账号
不能降权或停用它。生成 PBKDF2-SHA256 密码 Hash：

```powershell
conda activate MentalProject
cd mindflow-bot-runtime
python -c "from getpass import getpass; from app.admin_web.auth import hash_password; print(hash_password(getpass('Admin password: ')))"
```

把输出完整写入 `.env` 的 `ADMIN_PASSWORD_HASH`，不要把明文密码写进 `.env`。
登录 `/admin/` 后，`superadmin` 可在“管理员”页面新增 `viewer`、`admin` 或
其他 `superadmin`，也可停用非环境账号。`viewer` 只读，`admin` 可执行 Forecast
刷新和回顾重建，`superadmin` 额外管理管理员账号。系统不提供公开注册入口。

## Daily Review

每日 22:00（默认 `Asia/Shanghai`）向所有 active 且已绑定飞书会话的参与者发送
固定表单。投递具有数据库租约、指数退避和稳定消息 UUID，重启不会重复创建同日
同版本任务。回调不进入大模型：后端验证字段、追加保存 revision，并使用
`Fixed Lag Smoother + Anchor/Smooth Residual Kernel` 生成独立回顾曲线。

- `forecast_snapshots` 与原预警记录保持不变；
- 即时反馈只在自己的时间窗内参与回顾平滑；
- 早晨、峰值时段、收尾状态作为软锚点；峰值不会作为提交时刻的当前状态；
- 收尾状态以较低增益影响下一日初始状态，来源和 revision 会写入 Forecast provenance；
- Admin 的 Forecast 页可叠加“预测 / 即时反馈 / 回顾反馈 / 回顾估计”，
  Daily Review 页可查看 revision 并显式重建。

部署前执行迁移；`compose.yaml` 已提供一次性的 `migrate` 服务，Bot 和 Admin 都会
等待迁移成功。生产环境还需把飞书卡片回调配置为真实可访问的 HTTPS 地址；本地
代码和测试无法替代域名、证书、反向代理及飞书后台的外部配置。

## Response Presentation 性能策略

生产默认使用 `PRESENTATION_AGENT_MODE=adaptive`：本地 sanitizer 与确定性分段
已经可以无损生成 1–3 段时，不再串行等待第二个模型。只有本地结果超出投递容量
才尝试 PresentationAgent；超时采用硬截止，SDK 断连清理不会继续阻塞最终回复。

诊断时查看 BotEvent telemetry 的 `presentation_agent_outcome`，可区分
`skipped_adaptive`、`timeout`、`validation_reject`、`agent_error`、
`cleanup_backpressure` 和 `used`。当前运行边界、模型版本和各持久化链路统一见
[`CURRENT_ARCHITECTURE.md`](../docs/CURRENT_ARCHITECTURE.md)。

## 自动验证

```powershell
conda activate MentalProject
cd mindflow-bot-runtime
python -m pytest -q tests
```

本地测试使用 SQLite、Fake SDK client 和 Fake 外部服务，不需要安装系统级 Claude
Code，也不会真实调用 DeepSeek。正式上线前必须在云端形成以下证据：

1. `FeishuChannel` 长连接稳定，bot restart count 为 0；
2. 容器内 Claude Agent SDK 能通过 DeepSeek 返回结果；
3. 连续消息按 participant 排队，`/stop` 能中断 active turn；
4. 十五个 MCP Tool 真实调用、审计和身份隔离正确；
5. container recreate 后 session resume、pending reply 和 OAuth Token 均可恢复；
6. 日志不含 Secret、Token、完整 Prompt 或 MCP 身份上下文。

这些证据完成前，实验上线结论仍是 **NO-GO**。人工任务统一维护在
[`PROJECT_TASKS.md`](PROJECT_TASKS.md)。
