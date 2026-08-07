# MindFlow 飞书机器人同事交付、检查与部署手册

## 1. 本次交付是什么

本次交付包含两部分：一份不含运行密钥和用户数据的项目源码快照；一个可安装到 Codex 的 `mindflow-feishu-bot-ops` 插件。插件内含检查部署 Skill、架构说明、安全审查清单、部署/回滚流程、验收矩阵和不会回显密钥的预检脚本。

这里没有额外包装一个 MCP Server。原因是当前需求是让同事检查已有代码并部署，不需要新建一套远程工具协议。项目运行时仍由 Web、飞书长连接网关和 Care Worker 组成；Codex Skill 只指导审查和运维，不参与生产消息处理，也不获得飞书操作权限。

项目内原有的 `skills/mental-health-care` 描述 DeepSeek 可使用的项目能力、对话规则和安全边界；它还需要由运行时代码转换为模型提示和 JSON 工具定义才能真正执行。本次 `mindflow-feishu-bot-ops` 是交付、审查与部署工作流。两者用途不同。

## 2. 交付物目录

```text
deliverables/
├─ mindflow-feishu-handoff/                 # 可作为团队本地 Marketplace
│  ├─ marketplace.json
│  └─ plugins/mindflow-feishu-bot-ops/
│     ├─ .codex-plugin/plugin.json
│     └─ skills/mindflow-feishu-bot-ops/
│        ├─ SKILL.md
│        ├─ agents/openai.yaml
│        ├─ references/
│        └─ scripts/mindflow_preflight.py
├─ mindflow-feishu-bot-ops-plugin-0.2.0.zip # 检查/部署插件（事件生命周期与主动关怀版）
├─ mental-health-care-skill-0.2.0.zip       # 飞书预测关怀运行时 Skill
└─ mindflow-source-handoff-20260807-v2.zip  # 本次脱敏源码快照
```

旧版 `0.1.0` 插件和首版源码快照仅用于历史对照；新部署与验收应使用上述 `0.2.0` / `v2` 包。

源码快照明确排除 `.env`、数据库、用户 Token、日志、缓存、构建产物、依赖目录和 Git 元数据。接收方必须自行创建 `.env`，不得要求发送方通过聊天传递真实密钥。

## 3. 接收方安装和调用 Skill

### 3.1 直接使用本地 Marketplace

解压插件包，保留 `marketplace.json` 与 `plugins/` 的相对位置。在 Codex 中添加该本地 Marketplace 后安装 `mindflow-feishu-bot-ops`。若使用 Codex 图形界面，可直接打开交付方提供的“查看插件”链接。

安装后在源码根目录发出以下请求：

```text
使用 $mindflow-feishu-bot-ops 检查这份 MindFlow 飞书机器人源码，先做只读审查和生产预检，输出 P0/P1/P2 问题及 GO/NO-GO 结论；不要部署，也不要读取或输出密钥。
```

确认审查结果并取得服务器、飞书应用、域名/TLS 等操作授权后，再发出：

```text
使用 $mindflow-feishu-bot-ops 按部署手册部署到当前已授权服务器。先备份，保持 DeepSeek 语义 API 关闭，完成基础验收后暂停并汇报。
```

只有在逐用户外部模型同意开关已经在真实调用链生效、且负责人明确批准后，才要求进行 DeepSeek 合成数据测试。

### 3.2 不安装插件时

也可直接把插件目录交给 Codex，并明确要求先读取其中的 `SKILL.md`。预检脚本可独立运行：

```text
python <插件目录>/skills/mindflow-feishu-bot-ops/scripts/mindflow_preflight.py --project-root . --env-file .env --run-tests --run-build --run-compose-check
```

脚本只报告配置项是否满足要求，不打印密钥值。`ERROR` 必须解决；`WARN` 必须由负责人书面接受或修复。预检通过不等于飞书线上验收通过。

## 4. 当前实现边界

确认后的目标链路是：飞书自建应用机器人接收用户消息，校验绑定身份和外部模型同意状态后，由 DeepSeek 理解意图并选择项目 Skill 工具；工具使用可信用户身份执行现有项目功能；结果经过安全检查后回复。飞书卡片按钮直接调用同一组工具，不依赖模型判断，但必须经过相同的身份、参数、归属、幂等和审计检查。

当前已有：Web 登录/引导；每位用户独立的飞书 OAuth；飞书账号绑定；WebSocket 长连接接收私聊和卡片事件；SQLite 持久队列、租约、重试和 Worker；绑定用户身份的 `CareToolbox`；确定性文字意图路由和按钮到工具的路由；事件生命周期与隐含义务抽取；完成确认卡片和显式主观评价；反馈后的前向预测版本；有界多日上下文；确定性主动完成确认/提前关怀；DeepSeek 兼容的日历事件语义抽取、格式校验、缓存、规则融合、逐用户同意门控和规则降级；Compose 三服务部署。

当前不能按“已经完成”交付的能力包括：DeepSeek 主对话 Agent 的运行时工具调用循环（加载 Skill 指令、生成工具 Schema、调用 DeepSeek、校验 tool call、执行工具、回传结果、生成回复、步数限制和安全降级）、生产级主动候选完整状态机与延迟重试、生产级 RAG、多机水平扩展、KMS 加密 Token、自动数据库迁移体系。因此，当前机器人可以在确定性路径完成预测、完成反馈和初版主动关怀，但还不是目标中的“DeepSeek 驱动 Skill 调用”完整版。

## 5. DeepSeek 接入原则

DeepSeek 有两类用途。第一类是机器人对话主模型：理解用户语言、选择白名单 Skill 工具、基于真实工具结果组织简短回复。第二类是有边界的日历语义辅助：例如把“高数”规范化为与“高等数学”相近的课程语义，并输出规则可消费的有限字段；错别字、简称、模糊描述也可按同样方式处理。

模型可以生成候选回复，但不得绕过输出安全检查直接发送，也不得直接决定诊断、心理风险、最终压力分、预警、干预时机或数据副作用。`user_id` 只能由飞书绑定运行时注入，绝不能成为模型工具参数。模型只能调用 `CareToolbox` 白名单；所有参数、工具结果和最终回复均需校验。超时、限流、模型不存在、格式错误和服务异常必须退回确定性路由/模板。按钮直接走同一 Toolbox，不能另建绕过鉴权的调用通道。

上线时先设置：

```text
SEMANTIC_API_ENABLED=false
CARE_AGENT_ENABLED=false
```

特别注意：当前源码同时预留 Care Agent 与日历语义配置，但通用 Care Agent 仍被明确关闭且没有完整 DeepSeek tool-calling 实现；全局语义 API 开关还需要确认用户的 `allow_external_llm` 是否真正控制所有聊天和日历外发调用点。在完整循环实现、代码审查和“拒绝同意时无外发请求”验收前，真实用户环境不得开启 DeepSeek。这是 P0 上线门槛。

`.env.example` 中的模型名只是配置示例，不代表目标 DeepSeek 账号一定支持。部署者需要用不含个人数据的“高数/高等数学/错别字/提示注入”样例验证实际 endpoint 和 model。不得把真实日历作为首次联调数据。

## 6. 飞书开放平台准备

使用企业自建应用，并在同一个应用内完成 Web OAuth 和机器人配置：

1. 启用机器人能力和长连接事件接收。
2. 配置源码所需的消息接收、机器人发消息权限，并发布应用版本。
3. 订阅 `im.message.receive_v1` 和卡片动作事件 `card.action.trigger`。
4. 发布 `.env.example` 中列出的用户身份与日历只读 OAuth 权限。
5. 将重定向地址配置为与 `FEISHU_REDIRECT_URI` 完全一致的公网 HTTPS `/callback`。
6. 使用测试账号完成 OAuth 和机器人绑定，不配置全局用户 `open_id`、`calendar_id` 或共享用户 Token。

具体权限名称可能随飞书控制台展示调整，部署者应以目标租户控制台和源码实际 API 为准，保存应用版本、权限和事件订阅截图，截图需遮盖凭证。

## 7. 服务器部署流程

### 7.1 前置条件

准备一台已授权服务器、Docker Engine 与 Compose v2、公网 HTTPS 域名、反向代理、持久磁盘、正确时区和受限服务账号。只有反向代理确实可信且只有一层时才开启代理头信任。

### 7.2 配置

在服务器从 `.env.example` 创建 `.env`，设置至少以下内容：

- 生产环境和不少于 32 字符的随机 `FLASK_SECRET_KEY`；
- `SESSION_COOKIE_SECURE=true`；
- 飞书 App ID/Secret、三个完全一致的公网 HTTPS origin/redirect/bind 地址；
- `FEISHU_BOT_ENABLED=true`、WebSocket transport、私聊限定和 Care Worker；
- `SEMANTIC_API_ENABLED=false`、`CARE_AGENT_ENABLED=false`；
- 独立的 Token 加密材料（在代码路径实际支持后启用），不能从飞书 App Secret 派生。

首次创建管理员后删除 bootstrap 管理员密码。限制 `.env`、SQLite、用户 Token 目录和备份只能由服务账号访问。

### 7.3 部署前检查

运行 Skill 预检，再运行项目测试、前端构建和 Compose 配置检查。记录源码压缩包 SHA-256、提交号（如有）、镜像 ID、检查时间和执行人。

首次部署也要建立备份流程；升级部署必须先得到一致性备份。SQLite 和 Token 目录共享于 `app_data`，备份时应短暂停止写入或使用经验证的一致性快照。确认备份可读后再升级。

### 7.4 启动与观察

构建并启动 Compose 项目，确认 `app`、`feishu_bot`、`care_worker` 三个服务都处于预期状态。分别从服务器本机和公网 HTTPS 检查 `/api/health`。检查启动、心跳、队列和重试日志，但不得在报告中复制 Token、Cookie、密钥、真实消息或完整日历标题。

## 8. 必做验收

使用两个测试用户、测试飞书账号和合成日历，至少完成：

1. 未登录接口被拒绝，HTTPS Session Cookie 安全属性正确。
2. 两位用户分别 OAuth，只能读取自己的主日历。
3. 一次性机器人绑定链接正常，复用、过期和错误用户失败。
4. 私聊消息只入队一次、只处理一次、只回复一次。
5. 开启私聊限定时，群聊不返回个人状态。
6. 合法卡片操作生效一次，篡改和重放不产生重复副作用。
7. 网关或 Worker 中断重启后，队列恢复且不重复回复。
8. 两位用户不能互访 Token、日历、绑定、预测和队列数据。
9. DeepSeek 关闭时所有核心规则功能继续工作。
10. 在获批后，用自然语言请求验证 DeepSeek 只选择预期的白名单项目工具，不能传入 `user_id`、Token、Calendar ID 或他人的 `delivery_id`。
11. 点击相同功能的卡片按钮，验证它直接调用同一 Toolbox、无需 DeepSeek 且重放不产生重复副作用。
12. 用合成“高数/高等数学/错别字”测试日历语义归一化，并验证规则保留最终决定权。
13. 模拟超时、429、5xx、非法 tool call、非法 JSON、过多调用步骤和不存在模型，全部退回确定性路由/规则而不阻断请求。
14. 不同意外部模型的用户不会产生任何 DeepSeek 出站请求，但按钮和允许的确定性功能仍可用。
15. 管理状态接口和日志只显示脱敏队列/心跳信息。

任何跨用户访问、同意绕过、密钥泄漏、重复外发、模型直接控制安全决策或不可恢复的数据风险均为 P0，结论必须是 `NO-GO`。

## 9. 回滚

出现故障时先关闭对应功能：机器人问题关闭 `FEISHU_BOT_ENABLED`，语义问题关闭 `SEMANTIC_API_ENABLED`。应用回滚到已记录的上一源码/镜像，但保留 `app_data`。

禁止执行 `docker compose down -v`。只有在数据迁移或损坏确实要求时才恢复备份；恢复前停止所有写入、核对目标卷和备份时间、保留故障数据库副本，并在恢复后重新执行健康、跨用户隔离和幂等性验收。

## 10. 同事最终报告模板

```text
项目：MindFlow 飞书机器人
审查人 / 时间 / 环境：
源码包与 SHA-256（或 commit）：
部署域名与飞书应用版本（不含密钥）：

自动检查：
- Python 测试：
- 前端构建：
- Compose 配置：
- Skill 预检：

飞书验收：
- OAuth / 绑定 / 私聊 / 群聊限制 / 卡片 / 重启恢复：
- 双用户隔离：

DeepSeek 验收：
- 实际 endpoint 与 model 标签：
- 自然语言 -> Skill 工具调用循环：
- 按钮 -> 同一 Toolbox 直接调用：
- 用户同意门控：
- 合成语义样例：
- 异常降级：

问题：
- P0：
- P1：
- P2：

备份位置（不得含凭证）与恢复验证：
回滚版本与步骤：
最终结论：GO / CONDITIONAL GO / NO-GO
负责人签字与遗留项截止时间：
```
