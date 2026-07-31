# 心序 MindFlow Vue 前端部署与操作指南

> 文档版本：`2026-07-30.v2`  
> 适用范围：Vue 3 + Vite 前端、Flask API、Waitress 本地/服务器部署

---

## 1. 当前技术架构

项目现采用前后端分层但可同端口部署的结构：

```text
浏览器
  │
  ├─ 开发环境：http://127.0.0.1:5173
  │      ├─ Vue 3 + Vite 热更新
  │      └─ /api、/callback 自动代理到 Flask 5000
  │
  └─ 生产环境：http://127.0.0.1:8000
         └─ Waitress + Flask
                ├─ /、/login 返回 Vue 生产构建
                ├─ /assets/* 返回 Vite 哈希资源
                └─ /api/* 提供后端接口
```

前端主要技术：

- Vue 3 Composition API；
- Vue Router；
- Vite；
- 原生 CSS 主题系统；
- 原生 `fetch` 调用后端 API。

后端继续使用：

- Flask；
- SQLite；
- Waitress；
- 原有模型、问卷、画像、反馈和鉴权接口。

---

## 2. 目录说明

```text
Mental_project/
├─ frontend/
│  ├─ index.html
│  ├─ vite.config.js
│  └─ src/
│     ├─ main.js
│     ├─ App.vue
│     ├─ router.js
│     ├─ api.js
│     ├─ vue-overrides.css
│     ├─ views/
│     │  ├─ LoginView.vue
│     │  └─ HomeView.vue
│     └─ components/
│        ├─ OnboardingDialog.vue
│        └─ ApiKeyDialog.vue
├─ frontend_dist/          # npm run build 自动生成，不提交 Git
├─ static/app.css          # Vue 与旧模板共用的主题样式
├─ entry/app.py            # Flask API 与生产构建托管入口
├─ package.json
└─ package-lock.json
```

旧的 `templates/index.html`、`templates/login.html` 只作为“尚未构建 Vue 时”的后备页面。正常开发和生产运行均使用 Vue 页面。

---

## 3. 环境要求

### 3.1 Node.js

建议版本：

```text
Node.js >= 20.19
npm >= 10
```

本轮验证环境：

```text
Node.js 24.18.0
npm 11.16.0
```

检查命令：

```powershell
node --version
npm.cmd --version
```

Windows 如果执行 `npm` 时遇到 PowerShell 脚本策略限制，请统一使用：

```powershell
npm.cmd
npx.cmd
```

### 3.2 Conda 与 Python

建议版本：

```text
Conda >= 26
Python 3.11
```

本项目已创建以下 Conda 环境：

```text
环境名：MentalProject
环境路径：D:\Miniconda\envs\MentalProject
Python：3.11.15
```

第一次搭建时，在项目根目录执行：

```powershell
& "D:\Miniconda\Scripts\conda.exe" create -n MentalProject python=3.11 pip -y
conda activate MentalProject
python -m pip install -r requirements.txt -r requirements-dev.txt
```

以后每次开发或部署前，只需进入项目根目录并激活环境：

```powershell
conda activate MentalProject
```

如果当前 PowerShell 尚未初始化 Conda，出现“无法识别 conda”时，可先执行：

```powershell
& "D:\Miniconda\Scripts\conda.exe" init powershell
```

关闭并重新打开 PowerShell 后，再执行 `conda activate MentalProject`。不想初始化终端时，也可以直接运行：

```powershell
& "D:\Miniconda\Scripts\conda.exe" run -n MentalProject python --version
```

依赖有变化时重新同步：

```powershell
conda activate MentalProject
python -m pip install -r requirements.txt -r requirements-dev.txt
```

确认当前确实使用项目环境，并检查 Waitress：

```powershell
python -c "import sys; print(sys.executable)"
python -m waitress --help
```

第一条命令应输出：

```text
D:\Miniconda\envs\MentalProject\python.exe
```

完成开发或部署后可退出环境：

```powershell
conda deactivate
```

说明：Node.js 依赖仍安装在项目根目录的 `node_modules/`，不会安装到 Conda 环境。激活 `MentalProject` 后执行 npm 命令时，其中的后端脚本会自动使用该环境的 Python。

### 3.3 安装前端依赖

在项目根目录执行：

```powershell
npm.cmd install
```

依赖安装结果会写入：

- `node_modules/`；
- `package-lock.json`。

`node_modules/` 不提交 Git，`package-lock.json` 应提交。

---

## 4. 环境变量

可以基于 `.env.example` 配置运行环境。

开发环境最小配置：

```dotenv
APP_ENV=development
FLASK_SECRET_KEY=replace-with-a-long-random-secret
APP_DATABASE_PATH=data/app.sqlite3
SESSION_COOKIE_SECURE=false
```

飞书日历连接需要：

```dotenv
FEISHU_APP_ID=
FEISHU_APP_SECRET=
FEISHU_REDIRECT_URI=http://127.0.0.1:5000/callback
FEISHU_OAUTH_SCOPES=auth:user.id:read offline_access calendar:calendar:readonly
```

注意：

- 在飞书开放平台为应用开通 `offline_access` 和“获取日历、日程及忙闲信息（`calendar:calendar:readonly`）”，并发布新版本使权限生效；
- 在飞书开放平台“安全设置”中开启刷新 `user_access_token`；若后台没有该开关则无需额外处理；
- 开发模式的 Flask API 端口为 `5000`，所以飞书回调地址使用 `5000/callback`；
- Vue 多用户流程会使用各自的 user token 查询各自的主日历；用户无需填写 user ID、open_id、calendar_id 或 token；
- `FEISHU_REDIRECT_URI` 必须指向本项目的 `/callback`，不能使用飞书 API 调试台的 `open.feishu.cn/api-explorer/loading`；
- 出现飞书错误码 `20029` 时，按设置页展示的 App ID 找到同一个应用，并在“开发配置 → 安全设置 → 重定向 URL”中原样添加设置页展示的完整回调地址；
- `refresh_token` 只能使用一次。项目会在 access token 临近过期时通过 v2 OAuth 接口刷新，并跨线程、跨服务进程串行替换飞书返回的新 access token 与 refresh token；
- 生产环境必须将回调地址替换为所有用户都可访问的真实 HTTPS 域名；程序会拒绝在生产环境使用 `localhost` 或 `127.0.0.1` 回调；
- `APP_ENV=production` 时必须设置稳定的 `FLASK_SECRET_KEY`；
- 只有 HTTPS 环境才设置 `SESSION_COOKIE_SECURE=true`。

PowerShell 临时设置示例：

```powershell
$env:APP_ENV = "development"
$env:FLASK_SECRET_KEY = "replace-with-a-long-random-secret"
```

---

## 5. 开发模式启动

### 5.1 一条命令同时启动前后端

在项目根目录执行：

```powershell
conda activate MentalProject
npm.cmd run dev
```

该命令会同时启动：

```text
Flask API      http://127.0.0.1:5000
Vue + Vite     http://127.0.0.1:5173
```

日常开发请访问：

```text
http://127.0.0.1:5173
```

不要把 `5000` 当作 Vue 热更新地址。`5000` 主要用于 API、OAuth 回调和后端检查。

### 5.2 分开启动

只启动 Flask：

```powershell
npm.cmd run dev:api
```

只启动 Vue：

```powershell
npm.cmd run dev:web
```

适用于需要分别查看日志或调试单侧代码的情况。

### 5.3 停止开发服务

运行 `npm run dev` 的终端中按：

```text
Ctrl + C
```

`concurrently` 会同时停止 Flask 和 Vite。

---

## 6. 生产构建

执行：

```powershell
npm.cmd run build
```

构建输出：

```text
frontend_dist/index.html
frontend_dist/assets/index-*.css
frontend_dist/assets/index-*.js
```

文件名包含内容哈希，便于浏览器缓存和版本更新。

当前 Windows 受限环境中，Vite 使用：

```text
--configLoader runner
```

这是 `package.json` 已内置的参数，用于避免配置加载阶段扫描工作区外目录。无需手动追加。

单独检查构建：

```powershell
npm.cmd run check
```

---

## 7. 生产模式启动

### 7.1 本地或单机服务器

先设置生产环境：

```powershell
$env:APP_ENV = "production"
$env:FLASK_SECRET_KEY = "请替换为稳定且足够长的随机字符串"
$env:SESSION_COOKIE_SECURE = "false"
```

然后执行：

```powershell
conda activate MentalProject
npm.cmd start
```

`npm start` 会自动完成：

1. 执行 Vue 生产构建；
2. 启动 Waitress；
3. 由 Flask 同时提供 Vue 页面和 API。

默认访问地址：

```text
http://127.0.0.1:8000
```

若通过真实 HTTPS 反向代理上线：

```powershell
$env:SESSION_COOKIE_SECURE = "true"
$env:TRUST_PROXY_HEADERS = "true"
```

### 7.2 已构建时只启动服务器

如果 `frontend_dist/` 已经生成，不想重复构建：

```powershell
npm.cmd run start:server
```

### 7.3 部署别名

```powershell
npm.cmd run deploy
```

当前 `deploy` 与 `start` 流程一致：先构建，再启动 Waitress。

---

## 8. npm 命令速查

| 命令 | 作用 |
|---|---|
| `npm.cmd install` | 安装并锁定前端依赖 |
| `npm.cmd run dev` | 同时启动 Flask 5000 与 Vite 5173 |
| `npm.cmd run dev:api` | 只启动 Flask 开发服务 |
| `npm.cmd run dev:web` | 只启动 Vue/Vite |
| `npm.cmd run build` | 构建 Vue 到 `frontend_dist/` |
| `npm.cmd run preview` | 预览纯 Vite 构建，默认 4173 |
| `npm.cmd run check` | 执行一次正式构建检查 |
| `npm.cmd start` | 构建并使用 Waitress 启动 8000 |
| `npm.cmd run start:server` | 不重新构建，直接启动 Waitress |
| `npm.cmd run deploy` | 生产构建与启动别名 |

---

## 9. 前端功能操作流程

### 9.1 注册和登录

1. 打开 Vue 地址；
2. 选择“创建账号”；
3. 输入有效邮箱，或 5–32 位且至少包含一个数字的学号；
4. 密码至少 10 个字符；
5. 创建后自动进入个人空间。

登录会话由 Flask 保存，Vue 不在浏览器中保存密码或 API Key。

### 9.2 初始化问卷

1. 今日概览点击“开始问卷”；
2. 填写日常节律；
3. 填写压力与恢复感受；
4. 选择支持偏好；
5. 点击“生成我的画像”。

完成后会生成：

- 原始问卷记录；
- 画像推断运行；
- 画像快照；
- 参数先验；
- 当日作息计划；
- 当日上下文快照。

### 9.3 趋势预测

1. 进入“趋势预测”；
2. 选择日期；
3. 调整初始压力与精力参考；
4. 可添加任务、课程、运动、自习或休息事件；
5. 点击“生成今日趋势”。

结果会保存：

- 运行 ID；
- 模型/参数/特征版本；
- 随机种子；
- 输入指纹；
- 完整状态点；
- 结果摘要。

### 9.4 轻量反馈

“轻量反馈”支持：

- 早晨、中午、晚上时点反馈；
- 压力 `0–10`；
- 精力 `0–10`；
- 可选文字说明；
- 峰值复盘；
- 事件影响和纠错；
- 预警准确度；
- 关怀帮助程度；
- 作息纠错。

反馈会尽量关联最近一次预测运行，但不会自动覆盖用户画像。

### 9.5 飞书连接

1. 进入“设置”；
2. 点击“连接日历”；
3. 若提示缺少配置，先填写飞书环境变量；
4. 在飞书页面完成授权；
5. 授权窗口会把结果通知主页面并自动关闭，连接状态随即更新。

判断是否连接成功：

- “已连接 · 自动续期已开启”：本地授权有效并且包含 refresh token；
- “已连接 · 仅本次有效”：access token 可用，但没有取得 `offline_access`；
- “授权已失效”：需要重新完成授权；
- 点击“检测连接”后出现“已验证：主日历可正常读取”：代表系统已经实际调用飞书主日历接口成功，日历权限可用。

如果完成飞书授权后按钮仍显示“连接日历”，说明回调没有成功换取并保存 token。请检查 Flask `5000` 服务是否运行、回调地址是否完全一致，以及浏览器是否保留了当前登录 Session。不要把地址栏里的 `code` 手工填到页面中；重新点击连接即可生成新的 code。

首次授权后不需要复制或手工填写任何 token。服务端会：

1. 按当前应用用户隔离保存 `user_access_token` 与 `refresh_token`；
2. 在 access token 临近过期时自动使用 refresh token 续期；
3. 原子写回飞书返回的新 refresh token，避免重复使用已经失效的旧凭证；
4. 只有 refresh token 过期、被撤销、权限变更或用户主动解除授权时，才提示重新授权。

用户 token 当前按应用用户隔离保存：

```text
data/user_tokens/user_{id}.json
```

生产上线前仍建议改为数据库托管和静态加密。

“重新授权”按钮用于主动更换飞书账号或重新同意权限，日常使用不需要点击。

当前项目读取日历使用的是用户身份 `user_access_token`。飞书“发送消息”接口可以使用用户身份或应用身份 token，但本阶段没有启用主动发消息功能，因此不会额外申请消息权限，也不会让用户填写消息 token。

### 9.6 API Key

1. 进入“设置”；
2. 点击“管理密钥”；
3. 输入密钥名称和有效期；
4. 创建后立即复制；
5. 页面关闭后不再显示完整密钥；
6. 不再使用时点击“撤销”。

调用示例：

```http
Authorization: Bearer mhp_xxxxxxxxx
```

---

## 10. 开发修改位置

### 登录与注册

```text
frontend/src/views/LoginView.vue
```

### 今日概览、画像、预测、反馈和设置

```text
frontend/src/views/HomeView.vue
```

### 问卷组件

```text
frontend/src/components/OnboardingDialog.vue
```

问卷题目定义不在 Vue 中，实际题库位于：

```text
services/onboarding.py
```

Vue 会调用：

```text
GET /api/onboarding/questionnaire
```

因此后续新增题库版本时不需要重新编写固定问卷 HTML。

### API Key 弹窗

```text
frontend/src/components/ApiKeyDialog.vue
```

### API 调用封装

```text
frontend/src/api.js
```

### 视觉样式

```text
static/app.css
frontend/src/vue-overrides.css
```

---

## 11. 常见问题

### 11.1 `npm` 无法执行 `.ps1`

使用：

```powershell
npm.cmd run dev
```

而不是：

```powershell
npm run dev
```

### 11.2 5173 无法访问，但 5000 正常

单独运行：

```powershell
npm.cmd run dev:web
```

查看 Vite 错误。确认已经执行：

```powershell
npm.cmd install
```

### 11.3 API 返回 401

- 浏览器会话可能已过期；
- 重新进入 `/login`；
- API 客户端需要使用有效 Bearer API Key。

### 11.4 `/assets/*` 返回 404

说明尚未生成 Vue 生产构建：

```powershell
npm.cmd run build
```

开发模式不需要 Flask 提供 `/assets`，直接访问 Vite `5173`。

### 11.5 `No module named waitress`

```powershell
python -m pip install -r requirements.txt
```

或：

```powershell
python -m pip install waitress==3.0.2
```

### 11.6 端口被占用

默认端口：

```text
5000 Flask 开发 API
5173 Vite
4173 Vite preview
8000 Waitress
```

先停止旧终端中的服务，再重新启动。

### 11.7 修改 Vue 后生产页面没有变化

开发时使用 `5173` 可以热更新。

生产模式必须重新执行：

```powershell
npm.cmd run build
```

然后重启：

```powershell
npm.cmd run start:server
```

---

## 12. 上线前检查清单

- [ ] `APP_ENV=production`；
- [ ] 已激活 `MentalProject` Conda 环境；
- [ ] 使用稳定随机的 `FLASK_SECRET_KEY`；
- [ ] HTTPS 环境开启 `SESSION_COOKIE_SECURE=true`；
- [ ] 仅在可信反向代理下开启 `TRUST_PROXY_HEADERS=true`；
- [ ] 已执行 `npm.cmd install`；
- [ ] 已执行 `npm.cmd run build`；
- [ ] `python -m waitress --help` 可用；
- [ ] `/api/health` 返回 `status=ok`；
- [ ] `/login` 返回 Vue 页面；
- [ ] `/assets/*` 返回 200；
- [ ] 注册、登录、问卷、预测、反馈均完成一次烟雾测试；
- [ ] 飞书回调地址与生产域名一致；
- [ ] 数据库和 token 目录已纳入备份及权限管理；
- [ ] 生产日志不输出 token、密钥或用户原始心理文本。

---

## 13. 本轮实测结果

本轮已经实际执行并通过：

```text
conda create -n MentalProject python=3.11 pip
pip install -r requirements.txt -r requirements-dev.txt
npm install
npm run build
npm run dev
npm start
```

验证结果：

```text
Vue 开发页面 5173：HTTP 200
Vite → Flask API 代理：HTTP 200
Waitress 生产页面 8000：HTTP 200
Vite 哈希 JS 资源：HTTP 200
生产 /api/health：HTTP 200
MentalProject Python：3.11.15
MentalProject pytest：18 / 18
```

浏览器交互验证覆盖：

- Vue 注册；
- Vue 登录；
- 今日概览；
- 初始化问卷打开与第一步渲染；
- 桌面端 1440px 布局。
