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
- SDK MCP 只暴露十个业务 Tool，participant identity 只来自 frozen `AgentContext`。
- 最终回复由 Backend 持久化、重试和恢复；progress 使用受控固定模板。
- 应用读取配置后会把父进程环境收敛到运行白名单；Claude 子进程只显式获得 DeepSeek endpoint、模型名和认证 Token。
- `.env`、数据库密码、飞书 Secret、DeepSeek Key 和 OAuth Token 不进入 Prompt、Tool schema 或 Claude stderr 日志。

## 十个业务 Tool

1. `care_get_today_context`
2. `care_record_checkin`
3. `care_get_recent_state`
4. `care_run_today_assessment`
5. `care_get_support`
6. `care_get_pressure_curve`
7. `calendar_connection_status`
8. `calendar_list_calendars`
9. `calendar_list_events`
10. `calendar_create_event`

所有参数 schema 都设置 `additionalProperties: false`，并禁止 participant、飞书
身份、Token、Secret、SQL、路径和 URL 字段。Tool 调用继续经过 `ToolRegistry` 的
校验、安全摘要和 AgentRun 审计。

## 配置

`mindflow-bot-runtime/.env` 是唯一真实配置文件。把 `.env.example` 中的新变量合并进去：

- `FEISHU_BOT_APP_ID`、`FEISHU_BOT_APP_SECRET`：Lizzy 的 WebSocket ingress、回复和 Warning sender。
- `FEISHU_CALENDAR_APP_ID`、`FEISHU_CALENDAR_APP_SECRET`：Calendar OAuth、Token 和 Calendar API provider（测试环境为“喵学姐”）。若正式 Bot App 已有 Calendar 权限，可将两项都留空，自动复用 Bot credential。
- “喵学姐”需在开放平台开通 `calendar:calendar:readonly` 和 `calendar:calendar.event:create` 用户权限并发布应用版本。新增创建权限后，已有参与者需要重新发送 `/calendar` 完成授权。
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

旧 `FEISHU_APP_ID` / `FEISHU_APP_SECRET` 仅作为已有单 App 部署的临时兼容
回退，不再是推荐配置。Calendar credential 若显式配置，ID 与 Secret 必须同时提供。

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
4. 六个 MCP Tool 真实调用、审计和身份隔离正确；
5. container recreate 后 session resume、pending reply 和 OAuth Token 均可恢复；
6. 日志不含 Secret、Token、完整 Prompt 或 MCP 身份上下文。

这些证据完成前，实验上线结论仍是 **NO-GO**。人工任务统一维护在
[`PROJECT_TASKS.md`](PROJECT_TASKS.md)。
