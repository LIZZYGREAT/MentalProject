# MindFlow 阿里云部署与开发 Runbook

> 用途：以后重新购买 ECS、重装服务器、重新部署 MindFlow 时按此文档执行。  
> 当前目标架构：飞书自建应用 + Python Backend + Claude Code / Claude Agent SDK + DeepSeek + MindFlow Tools + PostgreSQL。  
> 日期：2026-08-11

---

# 1. 最终目标架构

```text
本地 Windows
    │
    │ SSH / Git
    ▼
阿里云 ECS
    │
    ├── Docker
    │   ├── PostgreSQL
    │   └── MindFlow Bot
    │
    ├── Claude Code（开发/人工排错）
    │
    └── MindFlow Runtime
         │
         ├── Feishu Gateway
         ├── Participant / Binding / Consent
         ├── Claude Agent SDK / Claude Code Harness
         ├── DeepSeek API
         ├── MindFlow MCP/Tools
         └── AssessmentModel / Calendar / DB
```

生产消息链：

```text
飞书用户
 -> 飞书 WebSocket
 -> Backend
 -> Claude Code Agent Harness
 -> DeepSeek
 -> MindFlow Tools
 -> 算法 / 日历 / PostgreSQL
 -> Backend
 -> 飞书回复
```

---

# 2. 用户权限约定

整个部署过程中只记一个原则。

## root 负责系统

```text
apt
/etc/*
systemctl
Docker Engine 安装
创建用户
用户组
Swap
系统网络
```

提示符通常：

```text
root@host:~#
```

## agent 负责项目

```text
Git
Claude Code
Python venv
.env
docker compose
项目代码
Bot Runtime
```

提示符通常：

```text
agent@host:~$
```

**后续如果没有特别说明，项目命令全部使用 `agent`。**

---

# 3. ECS 初始检查

SSH：

```powershell
ssh root@<ECS公网IP>
```

检查：

```bash
whoami
hostnamectl
cat /etc/os-release
uname -m
nproc
free -h
df -h /
```

本次服务器环境：

```text
Ubuntu 24.04.x LTS
x86_64
2 vCPU
约 1.6 GiB RAM
40 GB 系统盘
```

安全组测试阶段只需要：

```text
TCP 22
```

飞书使用 WebSocket 长连接，不需要因为 Bot 随意开放：

```text
5432
8000
8080
3000
```

PostgreSQL 不应映射公网端口。

---

# 4. 配置 Swap

小内存 ECS 建议至少配置 2 GiB Swap：

```bash
fallocate -l 2G /swapfile
chmod 600 /swapfile
mkswap /swapfile
swapon /swapfile
free -h
```

持久化：

```bash
echo '/swapfile none swap sw 0 0' >> /etc/fstab
```

---

# 5. 安装基础系统依赖

root：

```bash
apt update

apt install -y \
  curl \
  wget \
  git \
  gnupg \
  ca-certificates \
  build-essential \
  unzip \
  zip \
  jq \
  vim \
  tmux \
  htop \
  python3 \
  python3-pip \
  python3-venv \
  xz-utils
```

检查：

```bash
git --version
python3 --version
curl --version
```

---

# 6. 创建项目用户

root：

```bash
adduser --disabled-password --gecos "" agent
mkdir -p /opt/feishu-agent
chown -R agent:agent /opt/feishu-agent
```

项目不要长期以 root 运行。

---

# 7. 安装 Node.js 22 和宿主机 Claude Code

> 这一部分主要用于服务器上的开发和人工排错。  
> 后续生产 Bot 建议使用 Claude Agent SDK，不依赖宿主机 Claude CLI。

本次环境使用：

```text
Node.js 22.22.3
Claude Code 2.1.227
```

因为 ECS 对：

```text
claude.ai/install.sh
downloads.claude.ai
```

存在地区/网络访问问题，所以没有使用 native installer。

从 nodejs.org 获取 Node 22 二进制：

```bash
cd /tmp

curl -fLO https://nodejs.org/dist/v22.22.3/node-v22.22.3-linux-x64.tar.xz
curl -fLO https://nodejs.org/dist/v22.22.3/SHASUMS256.txt

grep 'node-v22.22.3-linux-x64.tar.xz' SHASUMS256.txt | sha256sum -c -
```

必须看到：

```text
OK
```

安装：

```bash
mkdir -p /usr/local/lib/nodejs

tar -xJf node-v22.22.3-linux-x64.tar.xz \
  -C /usr/local/lib/nodejs

ln -sfn /usr/local/lib/nodejs/node-v22.22.3-linux-x64/bin/node /usr/local/bin/node
ln -sfn /usr/local/lib/nodejs/node-v22.22.3-linux-x64/bin/npm /usr/local/bin/npm
ln -sfn /usr/local/lib/nodejs/node-v22.22.3-linux-x64/bin/npx /usr/local/bin/npx
```

检查：

```bash
node --version
npm --version
```

切换 agent：

```bash
su - agent
```

配置用户 npm prefix：

```bash
mkdir -p ~/.local
npm config set prefix "$HOME/.local"
echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.bashrc
export PATH="$HOME/.local/bin:$PATH"
```

安装：

```bash
npm config set registry https://registry.npmjs.org/
npm install -g @anthropic-ai/claude-code
```

检查：

```bash
claude --version
which claude
claude doctor
```

---

# 8. Claude Code 接入 DeepSeek

不要把 API Key 发到聊天、Git、日志。

临时测试：

```bash
read -rsp "DeepSeek API Key: " ANTHROPIC_AUTH_TOKEN
echo
export ANTHROPIC_AUTH_TOKEN

export ANTHROPIC_BASE_URL="https://api.deepseek.com/anthropic"
```

模型变量使用**部署当天 DeepSeek 官方 Claude Code 文档给出的当前模型名**。

不要在未来重新部署时直接照抄旧模型名；模型名可能变化。

检查：

```bash
echo "$ANTHROPIC_BASE_URL"

if [ -n "$ANTHROPIC_AUTH_TOKEN" ]; then
  echo "DeepSeek API Key loaded"
fi
```

测试：

```bash
mkdir -p ~/claude-test
cd ~/claude-test

claude -p "只回复一句：Claude Code 已成功通过 DeepSeek API 工作。"
```

成功后说明：

```text
Claude Code
 -> Anthropic-compatible protocol
 -> DeepSeek
```

已打通。

---

# 9. 项目目录

当前项目按 monorepo 保持完整：

```text
/opt/feishu-agent/mindflow/
├── .agents/
├── algorithm/
├── calibration/
├── core_engine/
├── entity/
├── entry/
├── event/
├── services/
├── settings/
├── strategy/
├── utils/
│
└── mindflow-bot-runtime/
    ├── app/
    ├── migrations/
    ├── mindflow_core/
    ├── profiles/
    ├── skills/
    ├── tests/
    ├── Dockerfile
    ├── compose.yaml
    ├── requirements.txt
    └── README.md
```

不要重新拆成多个孤立目录。

---

# 10. 上传项目

推荐 Git。

```bash
cd /opt/feishu-agent
git clone <repo> mindflow
cd mindflow
```

私有 GitHub 推荐配置 SSH key：

```bash
ssh-keygen -t ed25519 -C "aliyun-mindflow"
cat ~/.ssh/id_ed25519.pub
```

把公钥添加到 GitHub 后：

```bash
ssh -T git@github.com
git clone git@github.com:<owner>/<repo>.git mindflow
```

---

# 11. `.env` 安全

检查：

```bash
cd /opt/feishu-agent/mindflow
git check-ignore .env
```

必须输出：

```text
.env
```

权限：

```bash
chmod 600 .env
```

**禁止：**

```text
git add .env
把 .env 发给 AI
cat .env 后复制到聊天
```

---

# 12. Python 开发环境

agent：

```bash
cd /opt/feishu-agent/mindflow

python3 -m venv .venv
source .venv/bin/activate

python -m pip install --upgrade pip setuptools wheel
pip install -r mindflow-bot-runtime/requirements.txt
```

注意：

生产依赖和开发测试依赖分开：

```text
requirements-dev.txt
```

```text
-r requirements.txt
pytest==8.4.1
pytest-asyncio==1.1.0
```

---

# 13. 生产 `mindflow-bot-runtime/.env` 核心变量

当前 Claude Agent SDK Runtime 使用：

```dotenv
APP_ENV=production
LOG_LEVEL=INFO
APP_TIMEZONE=Asia/Shanghai

# Lizzy: WebSocket ingress, replies and warnings
FEISHU_BOT_APP_ID=...
FEISHU_BOT_APP_SECRET=...

# 喵学姐: Calendar OAuth, token and Calendar API
# 正式 Bot App 已有 Calendar 权限时，两项都留空以回退到 Bot credential。
FEISHU_CALENDAR_APP_ID=...
FEISHU_CALENDAR_APP_SECRET=...

DEEPSEEK_API_KEY=...
CLAUDE_ANTHROPIC_BASE_URL=https://api.deepseek.com/anthropic
CLAUDE_MODEL=<云端已经验证的主模型或 alias>
CLAUDE_DEFAULT_OPUS_MODEL=<云端已经验证的 v4-pro 模型 ID>
CLAUDE_DEFAULT_SONNET_MODEL=<云端已经验证的 v4-pro 模型 ID>
CLAUDE_DEFAULT_HAIKU_MODEL=<云端已经验证的 v4-flash 模型 ID>
CLAUDE_CODE_SUBAGENT_MODEL=<与上一行相同的 v4-flash 模型 ID>

POSTGRES_PASSWORD=...
DATABASE_URL=postgresql+psycopg://mindflow:<同一个密码>@postgres:5432/mindflow

TOKEN_ENCRYPTION_KEY=...

CLAUDE_WORKDIR=/srv/claude-workspace
CLAUDE_SETTINGS_PATH=/srv/claude-workspace/.claude/settings.json

BOT_QUEUE_MAX_SIZE=100
PARTICIPANT_INPUT_QUEUE_SIZE=20
MAX_ACTIVE_AGENT_SESSIONS=2
AGENT_SESSION_IDLE_SECONDS=120
CLAUDE_TIMEOUT_SECONDS=90
CLAUDE_MAX_TURNS=8

PROGRESS_DELAY_SECONDS=6
PROGRESS_COOLDOWN_SECONDS=8
PROGRESS_MAX_MESSAGES=2
FEISHU_SEND_MAX_RETRIES=1
```

`FEISHU_APP_ID` / `FEISHU_APP_SECRET` 只保留为旧单 App 部署的兼容回退，不应继续
作为新配置。Calendar credential 必须成对提供，半配置会在启动阶段被拒绝，且错误
不会包含 Secret。

双 App 测试部署中，Lizzy 负责聊天、`/bind`、`/calendar` 命令入口、普通回复与
Warning；“喵学姐”负责 Calendar OAuth、Token 和 Calendar API。`open_id` 是
App-scoped identity，不同 App 的 `open_id` 不可 join；内部统一身份必须使用
`participant_id`。`feishu_bindings` 始终保存 Lizzy Bot identity。

不要继续保留 `DEEPSEEK_BASE_URL`、`DEEPSEEK_MODEL`、`BOT_HISTORY_LIMIT`、
`AGENT_MAX_TOOL_STEPS` 或 Direct API retry 配置。`DEEPSEEK_API_KEY` 是唯一 Key，
Runtime 在受控 subprocess 环境中把它映射为 `ANTHROPIC_AUTH_TOKEN`。
五个 Claude 模型字段分别映射为 `ANTHROPIC_MODEL`、三个
`ANTHROPIC_DEFAULT_*_MODEL` 和 `CLAUDE_CODE_SUBAGENT_MODEL`；Haiku 与
Subagent 不一致时 Runtime 会拒绝启动。当前生产权限仍禁止 `Agent/Task`。

注意：

```text
DATABASE_URL 中 host 必须是 postgres
```

不要写：

```text
localhost
127.0.0.1
```

因为 Bot 与 PostgreSQL 位于不同容器。

---

# 14. 生成安全参数

PostgreSQL 密码：

```bash
openssl rand -hex 32
```

Token encryption key：

```bash
cd /opt/feishu-agent/mindflow/mindflow-bot-runtime

../.venv/bin/python -c \
"from app.services.token_service import TokenEncryptionService; print(TokenEncryptionService.generate_key())"
```

不要泄露输出。

---

# 15. Docker Engine 安装

root：

```bash
apt remove -y \
  docker.io \
  docker-compose \
  docker-compose-v2 \
  docker-doc \
  podman-docker \
  containerd \
  runc

apt update
apt install -y ca-certificates curl

install -m 0755 -d /etc/apt/keyrings

curl -fsSL https://download.docker.com/linux/ubuntu/gpg \
  -o /etc/apt/keyrings/docker.asc

chmod a+r /etc/apt/keyrings/docker.asc

echo \
  "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu \
  $(. /etc/os-release && echo "${UBUNTU_CODENAME:-$VERSION_CODENAME}") stable" \
  > /etc/apt/sources.list.d/docker.list

apt update

apt install -y \
  docker-ce \
  docker-ce-cli \
  containerd.io \
  docker-buildx-plugin \
  docker-compose-plugin
```

检查：

```bash
docker --version
docker compose version
systemctl status docker --no-pager
```

---

# 16. agent 获得 Docker 权限

root：

```bash
usermod -aG docker agent
```

重新登录 agent，或：

```bash
su - agent
newgrp docker
```

检查：

```bash
whoami
docker ps
```

以后：

```text
docker compose ...
```

全部使用 agent。

---

# 17. Docker Hub / ACR 网络坑

本次部署遇到：

```text
postgres:<tag>: not found
```

实际根因与国内 Docker Hub / ACR Mirror 同步和访问链有关。

最终采用：

```text
Windows Docker Desktop
 -> pull Docker Hub official images
 -> tag
 -> push 到自己的 ACR repository
 -> ECS 从自己的 ACR pull
```

本次至少准备：

```text
postgres:16.14-alpine
python:3.11.15-slim
```

然后：

```yaml
compose.yaml:
image: <YOUR_ACR>/mindflow/postgres:16.14-alpine
```

Dockerfile：

```dockerfile
FROM <YOUR_ACR>/mindflow/python:3.11.15-slim
```

不要依赖实时 Docker Hub。

---

# 18. PyPI 网络坑

Docker build 中：

```text
pip install
```

如果访问官方 PyPI 超时，可以使用稳定国内源。

本次使用阿里云 PyPI：

```dockerfile
RUN pip install \
  --no-cache-dir \
  --index-url https://mirrors.aliyun.com/pypi/simple/ \
  --trusted-host mirrors.aliyun.com \
  -r requirements.txt
```

先测试：

```bash
curl -I --connect-timeout 10 --max-time 20 \
https://mirrors.aliyun.com/pypi/simple/
```

---

# 19. Docker Compose 配置检查

agent：

```bash
cd /opt/feishu-agent/mindflow/mindflow-bot-runtime
```

不要直接：

```bash
docker compose config
```

因为可能输出 Secret。

使用：

```bash
docker compose config --quiet
echo $?
```

预期：

```text
0
```

Compose 必须同时声明：

```text
postgres_data -> PostgreSQL
claude_state  -> /home/mindflow/.claude
```

`CLAUDE_WORKDIR=/srv/claude-workspace` 来自镜像，只保存稳定 Skill/settings；
participant transcript 保存于 `claude_state`，二者不要混用。

---

# 20. 构建 Bot

```bash
docker compose build --no-cache bot
```

检查：

```bash
docker compose images
```

---

# 21. PostgreSQL 启动验证

只启动数据库：

```bash
docker compose up -d postgres
docker compose ps
```

必须：

```text
healthy
```

检查：

```bash
docker compose exec postgres \
  pg_isready -U mindflow -d mindflow
```

SQL：

```bash
docker compose exec postgres \
  psql -U mindflow -d mindflow -c "SELECT 1;"
```

---

# 22. Runtime Settings 验证

不要先启动 bot。

```bash
docker compose run --rm --no-deps bot \
python - <<'PY'
from app.config import Settings

s = Settings.from_env()

print("Settings OK")
print("APP_ENV        =", s.app_env)
print("APP_TIMEZONE   =", s.timezone_name)
print("CLAUDE_MODEL   =", s.claude_model)
print("CLAUDE_URL     =", s.claude_anthropic_base_url)
print("CLAUDE_WORKDIR =", s.claude_workdir)
print("SKILL_PATH     =", s.care_skill_path)
print("DB_SCHEME      =", s.database_url.split(":", 1)[0])
PY
```

禁止打印：

```text
API Key
App Secret
DATABASE_URL 完整值
Token Encryption Key
```

---

# 23. Bot -> PostgreSQL 验证

```bash
docker compose run --rm bot \
python - <<'PY'
from sqlalchemy import create_engine, text
from app.config import Settings

s = Settings.from_env()
engine = create_engine(s.database_url)

with engine.connect() as conn:
    value = conn.execute(text("SELECT 1")).scalar_one()

print("Database connection OK:", value)
PY
```

---

# 24. Claude Agent SDK -> DeepSeek 验证

禁止使用 Direct `/chat/completions` 作为生产 smoke 或 fallback。正式测试链路必须是：

```text
Bot container
 -> Claude Agent SDK
 -> Claude Code Agent Harness
 -> DeepSeek Anthropic-compatible API
```

先检查容器依赖：

```bash
docker compose run --rm --no-deps bot \
  python -c "import claude_agent_sdk; print('Agent SDK import OK')"
```

然后使用隔离的测试 participant 完成两轮 `ClaudeSDKClient` 对话、一次 Tool Call
和一次 `interrupt()`；证据只记录模型名、脱敏 session 前缀、状态与延迟。

---

# 25. Alembic Migration

```bash
docker compose run --rm bot \
  alembic upgrade head
```

`0007_calendar_oauth_app_identity` 为 `feishu_oauth_tokens` 与
`feishu_device_flows` 增加 nullable `oauth_app_id`。已有 NULL token/flow 不删除、
不猜测归属；应用层将旧 token 报告为 `reconnect_required`，且不会用新 App credential
刷新旧 refresh token。

检查：

```bash
echo $?
```

应为：

```text
0
```

查看表：

```bash
docker compose exec postgres \
  psql -U mindflow -d mindflow -c "\dt"
```

查看 Alembic：

```bash
docker compose exec postgres \
  psql -U mindflow -d mindflow \
  -c "SELECT * FROM alembic_version;"
```

---

# 26. Skill 和算法 import 验证

Skill：

```bash
docker compose run --rm --no-deps bot python - <<'PY'
from app.config import Settings
from app.agent.skill_loader import SkillLoader

s = Settings.from_env()
loader = SkillLoader(s.care_skill_path)
loader.load()

print("Skill load OK")
PY
```

算法：

```bash
docker compose run --rm --no-deps bot python - <<'PY'
from mindflow_core.assessment import AssessmentModel

model = AssessmentModel()
print("AssessmentModel import OK")
print(type(model).__name__)
PY
```

---

# 27. 飞书 Gateway 已知故障

第一次正式启动：

```bash
docker compose up -d bot
docker compose logs -f --tail=100 bot
```

出现过：

```text
RuntimeError: This event loop is already running
```

调用链：

```text
main
 -> asyncio.run()
 -> gateway.start()
 -> lark ws.Client.start()
 -> SDK run_until_complete()
```

这不是：

```text
App ID 错
App Secret 错
DeepSeek 错
PostgreSQL 错
```

而是 Feishu Python SDK legacy WebSocket client 与 asyncio 生命周期冲突。

处理原则：

```text
优先迁移真正 async 的 Channel API
```

如果当前 pin 的 lark-oapi 没有该 API：

```text
升级并测试
```

或者：

```text
legacy WS receiver 独立 process
```

禁止：

```text
直接改 site-packages
```

---

# 28. 当前生产架构

```text
Feishu
 -> BotWorker
 -> ClaudeAgentRuntime
 -> Participant Session Manager
 -> ClaudeSDKClient
 -> Claude Code Harness
 -> DeepSeek
 -> MindFlow Tools
```

仓库已经删除旧 `AgentRuntime` 和 `DeepSeekClient`。如果生产代码再次出现 Direct
`/chat/completions` 调用，应视为架构回归并阻止上线。

---

# 29. 为什么使用 Claude Agent SDK

Claude Agent SDK 是把 Claude Code 的：

```text
agent loop
tools
context management
session
MCP
permissions
```

作为 Python/TypeScript 可编程组件使用。

聊天 Bot 推荐：

```text
ClaudeSDKClient
```

而不是每条消息：

```text
claude -p
```

因为需要：

```text
持续会话
多轮
消息排队
interrupt
streaming
```

---

# 30. 新的消息控制策略

Backend 必须控制：

```text
消息监听
消息排队
interrupt
progress
final reply
```

推荐：

```text
同 participant 忙
 -> 默认 queue

用户发送 /stop
 -> client.interrupt()

超过处理时间阈值
 -> Backend 固定 progress message

ToolUse event
 -> 可映射审核过的 progress message

ResultMessage
 -> final reply
```

不靠 Prompt 控制这些可靠性行为。

---

# 31. 不要给 Claude Code 服务器权限

生产 Agent 禁止：

```text
Bash
Read
Write
Edit
Glob
Grep
WebFetch
WebSearch
Agent
Task
TaskOutput
TaskStop
```

只开放：

```text
MindFlow business tools
必要的 Skill
```

participant identity 只能来自 Backend。

模型 Tool schema 中不能存在：

```text
participant_id
open_id
chat_id
token
secret
SQL
path
```

---

# 32. 飞书生产配置

飞书自建应用需要：

```text
机器人能力
事件订阅
长连接
im.message.receive_v1
```

业务权限至少根据代码需要审核：

```text
单聊消息读取
机器人发送消息
calendar readonly
offline access
```

改完权限/事件后创建并发布新版本。

使用长连接时无需把 Bot HTTP 端口暴露公网。

---

# 33. Participant 初始化流程

项目 README 当前定义：

```text
create participant
 -> profile
 -> LLM consent
 -> /bind
```

示例：

```bash
docker compose exec bot \
  python -m app.admin create-participant P001
```

复制画像：

```bash
docker compose cp \
  ./profiles/P001.json \
  bot:/tmp/P001.json
```

设置：

```bash
docker compose exec bot \
  python -m app.admin set-profile P001 /tmp/P001.json
```

授权：

```bash
docker compose exec bot \
  python -m app.admin set-llm-consent P001
```

然后参与者在飞书：

```text
/bind <一次性绑定码>
```

撤回：

```bash
docker compose exec bot \
  python -m app.admin set-llm-consent P001 --revoke
```

---

# 34. 日历授权

绑定后的 participant：

```text
/calendar
```

Backend 启动 Device Flow：

```text
verification_url
user_code
```

用户完成飞书授权。

双 App 测试时，用户看到的授权页属于“喵学姐”，聊天和回复仍由 Lizzy 完成。

Token 进入 PostgreSQL，并使用：

```text
TOKEN_ENCRYPTION_KEY
```

加密。

---

# 35. 正式启动检查

生产验收先使用 `restart=no` smoke override：

```bash
docker compose -f compose.yaml -f compose.smoke.yaml up -d --no-deps bot
```

推荐验收顺序：

```text
Git / image
-> Alembic
-> restart=no
-> Rules-only resource smoke
-> Lizzy ingress/reply smoke
-> create participant
-> /bind
-> /calendar
-> 喵学姐 OAuth
-> Rules-only Calendar Forecast
-> DeepSeek semantic smoke
-> Warning smoke
-> production restart policy
```

完成所有验收后再用主 Compose 启动，恢复生产 restart policy：

```bash
docker compose up -d
```

检查：

```bash
docker compose ps
```

日志：

```bash
docker compose logs -f --tail=200 bot
```

重启次数：

```bash
docker inspect \
  --format='status={{.State.Status}} restart_count={{.RestartCount}}' \
  "$(docker compose ps -q bot)"
```

生产目标：

```text
postgres = healthy
bot = running
restart_count = 0
```

---

# 36. 常用维护命令

进入：

```bash
cd /opt/feishu-agent/mindflow/mindflow-bot-runtime
```

状态：

```bash
docker compose ps
```

日志：

```bash
docker compose logs --tail=200 bot
docker compose logs --tail=200 postgres
```

重启 Bot：

```bash
docker compose restart bot
```

停止：

```bash
docker compose stop bot
```

完整停止但保留 volume：

```bash
docker compose down
```

**谨慎：**

```bash
docker compose down -v
```

会同时删除 PostgreSQL 和 Claude transcript volume。

生产环境不要随便使用；删除 `claude_state` 会使数据库中的 session_id 失去对应
transcript，后续只能创建新 session。

Compose resource limit 的修改不会保证已经运行的旧 container 自动应用新限制。
使用 `docker inspect` 检查实际 `HostConfig`。若 PostgreSQL 需要应用新限制：先备份
数据库、确认 named volume，再只 recreate postgres container；绝不能执行
`docker compose down -v`。

诊断 SQL 必须按真实 schema：身份查询 JOIN `participants`、`feishu_bindings`、
`feishu_oauth_tokens`；Warning 按 `updated_at`，Forecast 按 `generated_at`，Calendar
Snapshot 按 `updated_at` 排序。不要引用不存在的 `participants.feishu_open_id`、
`participants.calendar_connected_at` 或 `warning_schedules.created_at`。

---

# 37. 更新代码的一般流程

agent：

```bash
cd /opt/feishu-agent/mindflow

git status
git pull
```

如果依赖/Dockerfile 改动：

```bash
cd mindflow-bot-runtime

docker compose build bot
docker compose run --rm bot alembic upgrade head
docker compose up -d bot
docker compose logs -f --tail=100 bot
```

如果只改 Python 源码但源码 baked into image：

```text
仍然需要 rebuild image
```

---

# 38. 每次部署必须验证的清单

```text
[ ] SSH 正常
[ ] Swap 正常
[ ] agent 用户正常
[ ] Docker 正常
[ ] agent 可 docker ps
[ ] ACR 登录正常
[ ] PostgreSQL image 可拉
[ ] Python base image 可拉
[ ] pip source 可用
[ ] .env 被 Git ignore
[ ] .env chmod 600
[ ] PostgreSQL healthy
[ ] Bot -> DB SELECT 1
[ ] Alembic upgrade head
[ ] Skill load
[ ] AssessmentModel import
[ ] Claude Agent SDK -> DeepSeek
[ ] Feishu Gateway stable
[ ] bot restart_count=0
[ ] /bind
[ ] LLM consent gate
[ ] Tool/MCP identity 隔离
[ ] 普通消息 E2E
[ ] checkin E2E
[ ] assessment E2E
[ ] /calendar E2E
[ ] container restart 后 session/token/event 可恢复
```

---

# 39. 已知不要重复踩的坑

## Claude native installer 不通

现象：

```text
claude.ai/install.sh -> region unavailable
downloads.claude.ai -> 443 timeout
```

解决：

```text
宿主机开发环境使用 npm 安装
```

## Docker Hub / ACR mirror 拉不到 tag

不要反复猜 tag。

解决：

```text
Windows pull
 -> push 私有 ACR
 -> ECS pull 私有 ACR
```

## Docker build pip timeout

解决：

```text
配置可用 PyPI mirror
```

## agent 没有 sudo

这是正常的。

系统操作：

```text
root
```

项目 Docker：

```text
agent
```

## 飞书 WebSocket event loop 报错

不是 Secret 问题。

解决：

```text
async Channel / 独立 WS process
```

## Claude Code 没进入生产路径

仅在 ECS 安装：

```bash
claude
```

不等于 Bot 使用 Claude Code。

必须代码链明确：

```text
BotWorker
 -> Claude Agent SDK / Claude Code Harness
 -> DeepSeek
```

---

# 40. 最终设计原则

```text
Feishu Gateway
= 可靠消息入口

BotWorker
= identity / bind / consent / queue / delivery

Claude Agent SDK
= Claude Code 的程序化 Agent Harness

DeepSeek
= 模型提供方

MindFlow MCP / Tools
= 唯一业务能力边界

AssessmentModel
= 压力/活力算法

PostgreSQL
= participant / event / token / audit / session

Docker + ACR
= 生产部署与镜像供应
```

一句话：

> **不要让模型承担消息可靠性；不要让 Backend 重复造 Agent loop；不要让业务 Tool 接触不可信 participant identity。**
