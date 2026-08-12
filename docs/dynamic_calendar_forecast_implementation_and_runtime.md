# 动态日历语义预处理、压力预测与预警闭环：实现与运行时序

## 1. 文档目的

本文记录本轮改造具体做了什么、各模块如何协作，以及真实业务场景下 DeepSeek API 在什么时间被调用。

核心原则是：

```text
Calendar
→ Calendar Snapshot
→ Rule Baseline
→ DeepSeek Optional Enrichment
→ Forecast Snapshot
→ Warning Schedule
```

DeepSeek 只负责事件文本的语义增强，不负责直接预测用户压力。任何时候 API 不可用，规则、压力预测和预警仍应继续工作。

---

## 2. 本轮主要修改

### 2.1 新增统一 ForecastCoordinator

文件：

```text
mindflow-bot-runtime/app/services/forecast_coordinator.py
```

统一以下入口：

- 每日预计算；
- 周期性 Calendar 同步；
- 用户主动查询压力曲线；
- DeepSeek enrichment 完成后的可选二次计算。

统一接口为：

```python
ensure_forecast(participant_id, local_date, reason)
```

它负责：

1. 按 participant/date 读取飞书日历；
2. 生成或更新 Calendar Snapshot；
3. 计算 `calendar_revision`；
4. 执行规则语义预处理；
5. 读取已持久化的 DeepSeek semantic cache；
6. 调用现有 `PredictionService/AssessmentModel`；
7. 保存 Forecast Snapshot；
8. 对未来 warning 做 diff；
9. 将缺少缓存的事件放入异步 enrichment；
10. 对同一个 participant/date 做 single-flight 合并。

### 2.2 新增 EventSemanticPreprocessor

文件：

```text
mindflow-bot-runtime/app/services/event_semantic_preprocessor.py
```

职责包括：

- deterministic rule baseline；
- event semantic fingerprint；
- PostgreSQL cache lookup；
- consent 检查；
- DeepSeek single-flight；
- external response validation；
- bounded rule/API fusion；
- semantic metadata 注入。

最终预测算法只读取已注入的：

```text
event.metadata.semantic
```

不会在算法运行期间访问 DeepSeek、数据库或 HTTP。

### 2.3 新增持久化版本模型

迁移：

```text
mindflow-bot-runtime/migrations/versions/0003_forecast_pipeline.py
```

新增四张表：

| 表 | 用途 |
|---|---|
| `calendar_snapshots` | 保存 participant/date 的稳定日历版本 |
| `event_semantic_cache` | 保存 participant/fingerprint 范围的 DeepSeek 结果 |
| `forecast_snapshots` | 保存完整曲线、峰值、版本和语义输入 |
| `warning_schedules` | 保存 pending/sent/cancelled warning，并支持重启恢复 |

版本关系：

```text
calendar_revision
+ semantic_revision
+ algorithm_version
→ forecast_version
```

### 2.4 新增 ForecastScheduler

文件：

```text
mindflow-bot-runtime/app/services/forecast_scheduler.py
```

负责：

- 每日 baseline Forecast preparation；
- 分钟级 Calendar polling；
- 同时维护今天和明天的 Forecast；
- 从 PostgreSQL 恢复 pending warning；
- warning 发送前重新检查 Forecast 是否仍然有效。

### 2.5 Care Tool 接入统一协调器

当前调用链变为：

```text
Claude
→ care_run_today_assessment
→ ForecastCoordinator.ensure_forecast(..., reason="user_curve_request")
→ bounded Calendar refresh
→ fresh Forecast fast path / local recompute
→ 立即返回 Curve
```

用户请求不会等待 DeepSeek。

### 2.6 SnowNLP 与启动资源修复

- requirements 删除 `snownlp`；
- `description_score.py` 改为纯 deterministic rule；
- `dynamic_state_model.py` 不再调用 semantic engine；
- `app.main` 的重型 import 移入 `run()`；
- receiver 子进程冷导入不加载 Assessment/Algorithm/SnowNLP；
- dispatcher 先启动，再恢复 durable Bot events，避免恢复数量超过队列容量时死锁；
- Compose 增加 CPU、RAM、memory+swap 和 PID 上限。

---

## 3. Calendar 和 Semantic 如何判定“是否需要重新计算”

### 3.1 Calendar revision

日历事件先规范化、排序，再做 SHA256。以下变化会改变 `calendar_revision`：

- event create/delete；
- start/end 变化；
- summary/description 变化；
- duration 变化；
- event/task type 或相关 metadata 变化。

### 3.2 Semantic fingerprint

每个事件的语义 fingerprint 包括：

```text
summary
description
event_type
task_type
duration_minutes
semantic schema version
prompt version
DeepSeek model
```

它故意不包含事件开始时间。因此：

| 修改 | Calendar revision | Semantic fingerprint | DeepSeek |
|---|---:|---:|---:|
| 10:00 改为 11:00，时长不变 | 改变 | 不变 | 不调用 |
| 描述改变 | 改变 | 改变 | 新 cache miss 才调用 |
| 时长改变 | 改变 | 改变 | 新 cache miss 才调用 |
| 删除事件 | 改变 | 不适用 | 不调用 |

---

## 4. 真实问题一：新的一天开始，DeepSeek 什么时候预计算课程？

### 4.1 当前 Scheduler 的真实行为

默认配置为：

```text
APP_TIMEZONE=Asia/Shanghai
FORECAST_DAILY_PREPARE_LOCAL_TIME=07:30
FORECAST_CALENDAR_SYNC_INTERVAL_SECONDS=300
```

Scheduler 启动后会立即执行一次循环，并且每 300 秒再执行一次。每轮都会读取：

```text
今天
+ 明天
```

的 Calendar。

当天第一次满足：

```text
current local time >= 07:30
```

时，今天的 reason 标记为 `daily_prepare`。如果 Bot 在 07:30 后重启，启动后的第一轮就会执行当天 daily preparation，不需要等到第二天。

它不是独立的精确 cron。Scheduler 按同步间隔检查时间，因此默认情况下 daily preparation 会在 07:30 之后的下一轮检查执行，通常落在 07:30–07:35 之间；如果 07:32 重启，则启动第一轮立即执行。

需要注意：当前代码在 07:30 之前也会进行 periodic poll。因此，如果 Bot 一直在线，新一天 00:00 后的第一个 polling 周期通常已经会读取当天和次日 Calendar。07:30 是明确的每日准备标记，不是系统第一次允许生成 Forecast 的唯一时间。

### 4.2 一门课程是否真的调用 DeepSeek

读取课程后先立即计算规则，不等待 API：

```text
Calendar
→ Rule semantics
→ baseline Forecast
→ baseline Warning
```

然后对每个课程计算 semantic fingerprint，并查：

```text
event_semantic_cache
```

只有同时满足以下条件才会调用 DeepSeek：

1. `SEMANTIC_API_ENABLED=true`；
2. 配置了 DeepSeek API key；
3. participant 的 `external_llm_consent_at` 不为空；
4. 事件不是休息、吃饭、睡眠、健身等无需 enrichment 的类型；
5. PostgreSQL 中不存在该 participant/fingerprint/schema/prompt/model 的有效缓存；
6. 同一 fingerprint 当前没有 in-flight job；
7. circuit breaker 没有打开。

因此并不是“每天所有课程都调用一次 DeepSeek”。准确行为是：

```text
每天都同步 Calendar
但只对此前没见过的 semantic fingerprint 调用 DeepSeek
```

### 4.3 周期性课程如何复用上周结果

例如上周一和本周一都有：

```text
高等数学
描述相同
event/task type 相同
时长相同
prompt/schema/model 未升级
```

即使：

- 飞书 event ID 不同；
- 日期不同；
- 开始时间发生平移；

semantic fingerprint 仍可以相同。系统会从 PostgreSQL 读取上周已经产生的 semantic cache，直接注入本周课程，不再调用 DeepSeek。

本周 Calendar Snapshot 和 Forecast 仍会重新生成，因为日期、当天事件组合和时间位置影响压力曲线；复用的是“这门课文本是什么意思”，不是复用上周整条压力曲线。

如果本周新增了一门课程：

```text
旧课程 → cache hit，不调用 API
新增课程 → cache miss，只为新增 fingerprint 调用一次 API
```

如果只把高数从 10:00 移到 11:00：

```text
semantic cache hit
0 次新 DeepSeek 调用
Forecast 因时间位置改变而重算
Warning 重新 diff/reschedule
```

### 4.4 当前真实部署配置的重要说明

当前项目实际 `.env` 尚未配置 `SEMANTIC_API_ENABLED`，所以使用代码默认值：

```text
SEMANTIC_API_ENABLED=false
```

也就是说，按当前真实配置：

```text
每日 Calendar/Rule/Forecast/Warning 正常工作
但不会调用 DeepSeek semantic API
```

若要开启，至少需要在 `.env` 中显式配置：

```text
SEMANTIC_API_ENABLED=true
SEMANTIC_API_MODEL=deepseek-v4-flash
SEMANTIC_API_TIMEOUT_SECONDS=8
SEMANTIC_MAX_CONCURRENCY=2
SEMANTIC_MATERIALITY_THRESHOLD=0.03
```

同时必须确保参与者已授权 external LLM consent。

---

## 5. 真实问题二：早上新增日程，什么时候调用 DeepSeek？

当前没有飞书 Calendar create/update/delete webhook，因此系统不能承诺修改后立即收到服务端推送。变化通过两条路径发现。

### 5.1 Periodic Sync

默认每 300 秒同步一次。

示例：

```text
08:02 用户新增“10:00 项目答辩”
08:05 左右下一轮 Calendar poll 读取到变化
→ calendar_revision 改变
→ 规则立即计算
→ baseline Forecast 立即重算
→ Warning 立即 diff
→ fingerprint cache lookup
→ cache miss 时后台调用一次 DeepSeek
```

在没有 webhook、轮询正常的前提下，发现延迟通常不超过一个 sync interval，加上 Calendar API 请求时间。

DeepSeek 慢或失败不会影响 08:05 的 baseline Curve 和 Warning。

### 5.2 On-demand Freshness Check

如果用户在下一轮 poll 前主动询问：

```text
08:03 “今天压力曲线怎么样？”
```

Care Tool 会立即执行 bounded Calendar refresh，因此可能在 08:03 就发现新增事件：

```text
用户请求
→ refresh Calendar
→ revision changed
→ rule baseline
→ Forecast/Warning
→ Curve 立即返回
→ DeepSeek cache miss 后后台调用
```

用户仍然不会等待 API。

### 5.3 新增的是历史上已有的周期课程

如果早上新增的课程语义内容与历史课程一致，例如重新添加了一节：

```text
高等数学 / 90 分钟 / 相同描述
```

则系统发现 Calendar 变化后：

```text
PostgreSQL semantic cache hit
→ 不调用 DeepSeek
→ 直接使用历史语义结果
→ 因新时间位置重新计算 Forecast
```

### 5.4 连续修改的当前限制

当前 polling 模式没有独立 debounce timer，原先未生效的
`FORECAST_CHANGE_DEBOUNCE_SECONDS` 已删除，避免配置项误导。实际避免重复工作的机制是：

- Calendar 仅按轮询/按需刷新发现变化；
- 同 participant/date Forecast 使用 single-flight；
- 同 participant/fingerprint DeepSeek 使用 single-flight；
- 相同 revision 走 no-op/fast path；
- API 结果写入 durable cache。

因此短时间内“改标题→改时间→改描述”如果都发生在同一个轮询周期内，通常只会看到最终状态；但如果每次修改恰好跨过多个轮询周期，仍可能发生多次 baseline Forecast。后续若接入 Calendar webhook，再设计真正的 per participant/date debounce。

### 5.5 API batch 的当前限制

当前已经实现：

- 每个 participant/fingerprint 的 single-flight；
- `SEMANTIC_MAX_CONCURRENCY` 并发限制；
- durable cache；
- circuit breaker。

但当前 DeepSeek client 仍是逐事件 HTTP 调用，不是把多个事件放进同一个 HTTP batch。日程数量较多时会受 `SEMANTIC_MAX_CONCURRENCY` 控制并发并逐项 enrichment，不会阻塞 baseline Forecast。若后续优化成本或吞吐，应增加带 item identity 的批量 prompt/response schema，并保留每个 fingerprint 独立缓存和校验。

---

## 6. DeepSeek 完成后的处理

DeepSeek 完成后不会无条件重算整条 Curve。

系统通过统一的 `semantic_model_inputs()` projection 比较 enrichment 前后真正被模型消费的输入：10 个 objective dimensions 加上归一化为 `F_like [-1, 1]` 的 fused appraisal。审计字段、reasoning、provider、evidence tags 不参与版本或 materiality：

```text
semantic delta < SEMANTIC_MATERIALITY_THRESHOLD
→ 保留当前 Forecast 和 Warning

semantic delta >= threshold
→ semantic_revision 改变
→ Forecast 重算
→ Warning diff/reschedule
```

该 materiality 决定会被 Forecast fast path 持续遵守，不会在下一次用户查询时因为 minor semantic change 又重新计算。

---

## 7. Warning 正确性

Warning 不再只是算法输出，而是 PostgreSQL 中的 durable schedule。

Forecast 变化时：

```text
unchanged → keep
obsolete → cancel
new → create
changed → reschedule
```

每条预警保存 `risk_time`、`valid_until`、重试次数、下次重试时间和 claim lease。真正发送前会再次检查：

- warning 是否仍为 pending；
- 绑定的 Forecast 是否 valid；
- Forecast version 是否匹配；
- 是否超过 `valid_until`；
- 是否达到最大尝试次数。

同一风险 episode 使用稳定的触发源/事件身份和当日 ordinal 标识，不包含精确风险分钟或 Forecast version。风险时间在漂移窗口内（默认 15 分钟）移动不会重复发送；已发送 episode 的 tier 升级允许一次 escalation；明显超出漂移窗口的后续风险则创建新 occurrence。该机制是 `best-effort dedupe + claim lease + episode dedupe`，不声称跨 HTTP/数据库提交边界的严格 exactly-once。

---

## 8. 配置建议

生产环境建议显式写入 `.env`，不要长期依赖代码默认值：

```text
FORECAST_DAILY_PREPARE_LOCAL_TIME=07:30
FORECAST_CALENDAR_SYNC_INTERVAL_SECONDS=300
FORECAST_MAX_CONCURRENCY=1

SEMANTIC_API_ENABLED=true
SEMANTIC_API_MODEL=deepseek-v4-flash
SEMANTIC_API_TIMEOUT_SECONDS=8
SEMANTIC_MAX_CONCURRENCY=2
SEMANTIC_MATERIALITY_THRESHOLD=0.03

WARNING_POLL_INTERVAL_SECONDS=15
WARNING_LEAD_MINUTES=20
WARNING_LATE_GRACE_MINUTES=10
WARNING_MAX_ATTEMPTS=5
WARNING_RETRY_BASE_SECONDS=60
WARNING_CLAIM_LEASE_SECONDS=120
WARNING_EPISODE_DRIFT_MINUTES=15
```

如果更重视日程修改响应速度，可将 Calendar interval 调到 120–300 秒，但不建议秒级轮询。

---

## 9. 部署步骤

```text
1. 检查并补全 .env
2. alembic upgrade head
3. docker compose config
4. 重启 bot/postgres 服务
5. 检查 settings/database/business/gateway/scheduler 分阶段 RSS 日志
6. 验证 daily_prepare、periodic_poll、user_curve_request
7. 验证无 consent 时 0 次 semantic API 调用
8. 验证重复周期课程命中 event_semantic_cache
```

---

## 10. 已验证测试

在 `MentalProject` Conda 环境运行：

```text
pytest: 64 passed
compileall: passed
git diff --check: passed
docker compose config --quiet: passed
```

覆盖了 Calendar revision、time-only update、description update、rules-only、consent、durable cache、single-flight、API invalid response/circuit breaker、appraisal/model projection、Curve fast path、materiality、Scheduler 启动顺序和并发上限、warning lease/backoff/expiry/episode/escalation、Today Context、日志凭据脱敏以及 spawn-safe import。

说明：上述是本地 SQLite/Fake client 自动化验证和 Compose 静态配置校验。真实 PostgreSQL migration、ECS steady-state RSS、真实 DeepSeek cache miss/hit 和真实 Warning 发送仍必须按生产验收清单单独执行，不能用本地测试替代。
