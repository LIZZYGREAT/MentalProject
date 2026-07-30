# SQLite 用户登录、API 鉴权与服务器部署指南

本文说明当前分支 `codex/sqlite-auth-deployment` 新增的账号体系、数据库管理、虚拟环境、Docker 部署和外部服务器调用方式。

## 1. 当前实现

### 用户与权限

- 用户名 + 密码登录，密码只保存 Werkzeug 安全哈希；
- 浏览器登录后使用 Flask 签名 Session；
- 外部服务器使用 Bearer API Key；
- API Key 原文只在创建时返回一次，数据库只保存 SHA-256 哈希；
- 角色分为 `admin` 与 `user`；
- 支持停用用户、撤销 API Key、过期时间和审计日志；
- 每个用户拥有独立的仿真参数档案。

### SQLite

默认应用数据库：

```text
data/app.sqlite3
```

可以通过环境变量修改：

```text
APP_DATABASE_PATH=/absolute/path/app.sqlite3
```

数据库启用：

```text
PRAGMA journal_mode=WAL
PRAGMA synchronous=NORMAL
PRAGMA foreign_keys=ON
PRAGMA busy_timeout=5000
```

表：

| 表 | 用途 |
|---|---|
| `schema_migrations` | 数据库结构版本 |
| `users` | 用户、密码哈希、角色、状态 |
| `api_keys` | API Key 哈希、过期与撤销 |
| `user_profiles` | 每个用户的模型参数 JSON |
| `audit_logs` | 登录、密钥和管理员操作审计 |

原有校准数据库仍位于：

```text
data/calibration/calibration.sqlite3
```

应用账号数据库和校准数据库目前是两个 SQLite 文件。这样改造风险较低；后续迁移 PostgreSQL 时可以统一数据模型。

## 2. 本地虚拟环境

### Windows PowerShell

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

如果 PowerShell 阻止激活，可只对当前终端设置：

```powershell
Set-ExecutionPolicy -Scope Process Bypass
.\.venv\Scripts\Activate.ps1
```

### Linux/macOS

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

开发和测试额外安装：

```bash
python -m pip install -r requirements-dev.txt
```

### 配置

复制示例：

```powershell
Copy-Item .env.example .env
```

Linux：

```bash
cp .env.example .env
```

至少修改：

```text
FLASK_SECRET_KEY
BOOTSTRAP_ADMIN_PASSWORD
FEISHU_APP_ID
FEISHU_APP_SECRET
FEISHU_REDIRECT_URI
```

生成 Flask Secret：

```powershell
python -c "import secrets; print(secrets.token_urlsafe(48))"
```

不要把 `.env`、数据库、Token 或 API Key 提交到 Git。

## 3. 初始化数据库与管理员

### 方法一：管理命令

```powershell
python -m auth.manage init-db
python -m auth.manage create-user --username admin --role admin
```

命令会安全提示输入密码。密码至少 10 个字符。

创建普通用户：

```powershell
python -m auth.manage create-user --username alice --role user
```

### 方法二：首次启动环境变量

在 `.env` 设置：

```text
BOOTSTRAP_ADMIN_USERNAME=admin
BOOTSTRAP_ADMIN_PASSWORD=一个足够长的初始密码
```

应用首次启动且用户名不存在时会创建管理员。创建成功后应删除 `BOOTSTRAP_ADMIN_PASSWORD` 并重启，避免初始密码长期留在环境配置中。

## 4. 数据库管理命令

查看用户：

```powershell
python -m auth.manage list-users
```

重置密码：

```powershell
python -m auth.manage reset-password --username alice
```

停用用户：

```powershell
python -m auth.manage set-user-active --username alice --active false
```

重新启用：

```powershell
python -m auth.manage set-user-active --username alice --active true
```

查看数据库状态：

```powershell
python -m auth.manage db-stats
```

一致性备份：

```powershell
python -m auth.manage backup --output backups/app-20260730.sqlite3
```

备份使用 Python SQLite Backup API，可在应用运行期间生成一致性副本。生产环境还应定期备份校准数据库和飞书连接配置。

## 5. API Key

### 命令行创建

```powershell
python -m auth.manage create-api-key `
  --username alice `
  --name server-a `
  --expires-days 90
```

输出中的 `key` 只显示一次。

查看密钥元数据：

```powershell
python -m auth.manage list-api-keys --username alice
```

撤销：

```powershell
python -m auth.manage revoke-api-key --id 3
```

### 浏览器会话创建

登录后调用：

```http
POST /api/auth/api-keys
Content-Type: application/json

{
  "name": "server-a",
  "expires_days": 90
}
```

### 外部服务器调用

推荐请求头：

```http
Authorization: Bearer mhp_xxxxxxxxxxxxxxxxx
```

也兼容：

```http
X-API-Key: mhp_xxxxxxxxxxxxxxxxx
```

PowerShell：

```powershell
$headers = @{
  Authorization = "Bearer mhp_请替换为真实密钥"
}

Invoke-RestMethod `
  -Uri "https://api.example.com/api/config" `
  -Headers $headers
```

Python：

```python
import requests

response = requests.post(
    "https://api.example.com/api/simulate",
    headers={"Authorization": "Bearer mhp_请替换为真实密钥"},
    json={
        "date": "2026-07-07",
        "init_S": 50,
        "init_E": 100,
        "mock_events": [
            {
                "type": "task",
                "name": "服务端调用示例",
                "start": "14:00",
                "end": "15:00",
                "level": "general",
            }
        ],
    },
    timeout=180,
)
response.raise_for_status()
print(response.json())
```

服务器到服务器调用不受浏览器 CORS 限制。只有未来允许其他域名中的浏览器 JavaScript 直接访问时，才需要额外配置受限 CORS 白名单。

## 6. 认证相关 API

| 方法 | 路径 | 权限 |
|---|---|---|
| `POST` | `/api/auth/login` | 公共 |
| `POST` | `/api/auth/logout` | 浏览器 Session |
| `GET` | `/api/auth/me` | Session 或 API Key |
| `GET/POST` | `/api/auth/api-keys` | 浏览器 Session |
| `DELETE` | `/api/auth/api-keys/{id}` | 浏览器 Session |
| `GET/POST` | `/api/admin/users` | admin |
| `PATCH` | `/api/admin/users/{id}/active` | admin |
| `GET` | `/api/admin/database/stats` | admin |
| `GET` | `/api/health` | 公共 |

原有 `/api/config`、`/api/simulate`、反馈、评价、校准、飞书状态等接口现在都要求 Session 或 API Key。飞书浏览器 OAuth 发起和手工 code 提交要求浏览器 Session。

## 7. 不使用 Docker的服务器部署

Docker不是必须的。单台 Linux 服务器可以使用虚拟环境：

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
python -m auth.manage init-db
python -m auth.manage create-user --username admin --role admin
gunicorn --config gunicorn.conf.py wsgi:app
```

不要使用：

```text
python entry/app.py
flask run
```

作为生产服务器。它们适合开发，不适合公网的稳定性和安全要求。

Windows Server 可使用 Waitress：

```powershell
waitress-serve --host=0.0.0.0 --port=8000 wsgi:app
```

Linux 生产优先 Gunicorn。

## 8. Docker部署

### 为什么建议用

- 固定 Python 与系统依赖；
- Matplotlib 中文字体随镜像安装；
- 测试机与服务器运行方式一致；
- 数据通过 Volume 持久化；
- 便于升级、回滚和健康检查。

### 首次启动

```bash
cp .env.example .env
```

生产 `.env` 至少：

```text
APP_ENV=production
FLASK_SECRET_KEY=随机长密钥
SESSION_COOKIE_SECURE=true
TRUST_PROXY_HEADERS=true
BOOTSTRAP_ADMIN_USERNAME=admin
BOOTSTRAP_ADMIN_PASSWORD=初始管理员密码
FEISHU_REDIRECT_URI=https://api.example.com/callback
APP_PORT=8000
```

构建和启动：

```bash
docker compose up --build -d
```

查看状态：

```bash
docker compose ps
docker compose logs -f app
```

健康检查：

```bash
curl http://127.0.0.1:8000/api/health
```

成功后从 `.env` 删除 `BOOTSTRAP_ADMIN_PASSWORD`：

```bash
docker compose up -d
```

### 数据持久化

Compose 使用命名卷：

```text
app_data:/app/data
```

删除容器不会删除数据库；执行 `docker compose down -v` 会删除卷和其中数据，不应在生产环境随意使用。

容器中备份：

```bash
docker compose exec app \
  python -m auth.manage backup --output /app/data/backups/app.sqlite3
```

还需将命名卷备份到服务器之外，例如对象存储或另一台备份机。

## 9. Nginx、域名和 HTTPS

`deploy/nginx.conf.example` 提供反向代理示例：

```text
Client -> HTTPS/Nginx -> Gunicorn:8000 -> Flask
```

生产环境建议：

1. Gunicorn 只监听内网或 `127.0.0.1`；
2. Nginx 对外开放 80/443；
3. 使用可信 CA 证书和 HTTPS；
4. `SESSION_COOKIE_SECURE=true`；
5. 只有确实经过一个可信反向代理时才设置 `TRUST_PROXY_HEADERS=true`；
6. 防火墙不直接开放 SQLite 文件或 Gunicorn 管理端口。

反向代理超时应大于最长仿真/校准请求时间。示例设置为 180 秒。大型校准任务后续应改为异步任务队列，而不是长期占用 HTTP 请求。

## 10. SQLite是否适合服务器

当前阶段适合：

- 单台服务器；
- 一个应用实例或少量同机 worker；
- 用户规模和写入量较低；
- 主要负载是读取和 CPU 仿真；
- 写事务短。

当前 WAL 模式允许读写更好地并行，但 SQLite 对同一数据库文件仍然只有一个并发写者。

应迁移 PostgreSQL 的信号：

- 需要两台及以上应用服务器；
- Docker/Kubernetes 多副本横向扩容；
- 登录、审计、反馈有大量并发写入；
- 经常出现 `database is locked`；
- 需要数据库级权限、远程运维、主从和高可用；
- 数据库需要独立部署与备份恢复演练。

SQLite 数据文件不能放在多个容器跨主机共享的普通网络文件系统上来模拟数据库集群。迁移到 PostgreSQL 前，部署应保持单机持久卷。

## 11. 生产上线检查表

- [ ] 独立 `codex/sqlite-auth-deployment` 分支通过测试后再合并；
- [ ] `.env` 未提交；
- [ ] `FLASK_SECRET_KEY` 为随机长值；
- [ ] 初始管理员密码已移除或轮换；
- [ ] HTTPS 已启用；
- [ ] `SESSION_COOKIE_SECURE=true`；
- [ ] 飞书回调地址与公网域名完全一致；
- [ ] API Key 按调用方分别创建，不多人共用；
- [ ] API Key 设置过期时间并有轮换流程；
- [ ] 数据库和 Volume 有异机备份；
- [ ] `/api/health` 接入监控；
- [ ] Nginx/Gunicorn 日志有轮转；
- [ ] 外部只开放需要的端口；
- [ ] 校准接口有调用频率与并发限制；
- [ ] 做过从备份恢复数据库的演练。

## 12. 当前仍需注意

1. 飞书 Token 仍沿用项目原有的单个本地文件，尚未按用户隔离。多用户分别绑定飞书账号前，需要增加加密的 per-user OAuth Token 存储。
2. API Key 当前按用户授权，没有细分 `simulate/read/calibrate/admin` scope。对更多外部合作方开放前应增加权限范围。
3. 密码登录没有验证码、速率限制和账户锁定。公网部署应在 Nginx/API Gateway 增加限流，并考虑应用级失败次数策略。
4. 长时间校准仍是同步请求。后续应使用 Celery/RQ 等任务队列。
5. 当前 SQLite 迁移框架是轻量版本表；复杂演进后建议引入 SQLAlchemy + Alembic，并为 PostgreSQL 迁移做准备。
