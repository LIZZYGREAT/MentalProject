# MindFlow 生产飞书 Runtime

这是面向约 20 名实验参与者的独立生产后端。生产消息链路不依赖旧 MCP、Claude Session、`feishu_im_watch`、SQLite 或 `tokens.json`。

## 架构

```text
一个飞书 App / 一条 WebSocket
  -> PostgreSQL 事件幂等与可恢复载荷
  -> participant 级顺序执行
  -> DeepSeek 有界 Tool Calling
  -> 六个 participant-bound 业务工具
  -> Backend 统一发送回复
```

PostgreSQL 保存 participant、一次性绑定、画像、打卡、完整预测输入/输出、有限对话历史、事件恢复状态、Agent 审计以及 AES-256-GCM 加密的 participant OAuth Token。

## 代码边界

- `app/`：飞书 Gateway、身份、Agent、Tool、OAuth、数据库和 Worker。
- `mindflow_core/`：生产 Tool 到现有压力/活力模型的结构化适配。
- 根目录的 `algorithm/`、`core_engine/`、`entity/`、`event/` 等：经测试保留的模型依赖闭包。
- `skills/mental-health-care/SKILL.md`：只作为模型指令读取，不执行其中代码。
- `migrations/`：唯一生产 Schema 来源；生产拒绝 SQLite。

## 人工联调前配置

项目保留根目录 `.env` 作为唯一真实配置文件。把
`mindflow-bot-runtime/.env.example` 中缺少的变量合并到根目录 `.env`，然后执行：

```powershell
cd mindflow-bot-runtime
python -c "from app.services.token_service import TokenEncryptionService; print(TokenEncryptionService.generate_key())"
```

将生成值和下列真实配置写入 `.env`：

- `FEISHU_APP_ID`、`FEISHU_APP_SECRET`
- `DEEPSEEK_API_KEY`
- `POSTGRES_PASSWORD`、`DATABASE_URL`
- `TOKEN_ENCRYPTION_KEY`

飞书应用需要开启机器人长连接并订阅 `im.message.receive_v1`；日历权限至少包含 `offline_access calendar:calendar:readonly`。已在旧环境中出现过的 Secret 必须先旋转。

## 启动

```powershell
docker compose up --build -d
docker compose logs -f bot
```

Compose 仅包含 `bot` 和 `postgres`。PostgreSQL 不映射宿主机端口；bot 启动前自动执行 Alembic migration。

## 创建参与者

先把 `profiles/profile.example.json` 复制为每名参与者各自的画像文件，只填写研究所需的去标识化字段；`model_params` 留空时使用当前已审查的默认模型参数。

```powershell
Copy-Item .\profiles\profile.example.json .\profiles\P001.json
docker compose exec bot python -m app.admin create-participant P001
docker compose cp .\profiles\P001.json bot:/tmp/P001.json
docker compose exec bot python -m app.admin set-profile P001 /tmp/P001.json
docker compose exec bot python -m app.admin set-llm-consent P001
```

第一条命令只显示一次 `/bind <code>`。数据库只保存绑定码 Hash。只有研究者明确记录外部 LLM 实验授权后，参与者消息才会发送给 DeepSeek；撤回命令为：

```powershell
docker compose exec bot python -m app.admin set-llm-consent P001 --revoke
```

绑定后，参与者可发送 `/calendar` 启动自己的飞书 Device Flow。Pending Device Flow 和待发送回复均可在进程重启后恢复。

## Agent 工具

1. `care_get_today_context`
2. `care_record_checkin`
3. `care_get_recent_state`
4. `care_run_today_assessment`
5. `care_get_support`
6. `calendar_connection_status`

所有 Tool Schema 均禁止额外字段以及 participant、飞书身份、Token、Secret、SQL、路径或任意 URL。participant 只来自 Backend 创建的 frozen `AgentContext`。

## 自动验证

在项目根目录执行：

```powershell
D:\Miniconda\envs\MentalProject\python.exe -m pytest -q mindflow-bot-runtime\tests
```

测试使用 SQLite 内存后端和 Fake 外部服务，只验证代码语义。进入实验前仍必须人工完成：

1. 真实 PostgreSQL upgrade、重启恢复和跨连接 Token 刷新竞争；
2. 两个真实飞书账号同时绑定、打卡、评估和查询历史；
3. 真实 DeepSeek Tool Calling、超时、429/5xx 和安全 fallback；
4. 两名参与者的真实 Device Flow、重复日程读取和 Token 隔离；
5. 容器重启、待发送回复恢复、日志脱敏和原文保留策略确认。

以上五项没有形成证据前，结论仍是 **NO-GO**。
