# MindFlow 代码审计整改实施报告

实施日期：2026-08-25

分支：`production_runtime`

整改前基线：`695a67a`（`355 passed, 15 warnings`）

## 1. 实施结论

审计计划的 A–P 完成条件均已满足。整改关闭了 Observation 提交后的 Forecast/Warning
一致性窗口，把 Warning 投递限制收敛为单一配置事实源，删除了生产不可达的旧评估、旧模型、
旧语义引擎和低风险兼容写路径，并建立了可自动校验的当前架构文档。

本轮没有拆分 `repositories.py`，没有改造 ORM transaction architecture，没有引入全局
UnitOfWork，也没有进行 Python packaging/workspace 迁移。

## 2. Commit 清单

| Commit | 标题 | 阶段 |
|---|---|---|
| `f373587` | `fix: refresh forecast after observation commit` | Phase 1 |
| `d992b59` | `fix: unify warning delivery policy config` | Phase 2 |
| `d9bd73d` | `refactor: remove unreachable assessment and legacy ctssm predecessors` | Phase 3A |
| `334ad50` | `refactor: remove legacy semantic engine` | Phase 3B |
| `888dadd` | `refactor: retire legacy runtime compatibility paths` | Phase 4 |
| `5efef7e` | `docs: establish authoritative runtime architecture` | Phase 5 |

`last_error_class` 的成功、取消、过期清理及测试已存在于整改前基线提交 `695a67a`，因此
Phase 6 按计划没有重复修改。

## 3. 按阶段修改文件

### Phase 1：Observation 后立即刷新 Forecast

- `mindflow-bot-runtime/app/repositories.py`
- `mindflow-bot-runtime/app/services/observation_forecast_refresh.py`
- `mindflow-bot-runtime/app/services/card_action_service.py`
- `mindflow-bot-runtime/app/tools/care.py`
- `mindflow-bot-runtime/app/bootstrap.py`
- `mindflow-bot-runtime/app/main.py`
- Observation/Card/Forecast 相关回归测试

### Phase 2：Warning 配置单一事实源

- `mindflow-bot-runtime/app/contracts/warning.py`
- `mindflow-bot-runtime/app/bootstrap.py`
- `mindflow-bot-runtime/app/repositories.py`
- `mindflow-bot-runtime/app/services/warning_policy.py`
- `mindflow-bot-runtime/app/services/forecast_coordinator.py`
- `mindflow-bot-runtime/app/services/forecast_scheduler.py`
- Warning policy、sync、claim 回归测试

### Phase 3：删除生产不可达旧实现

- 精简 `core_engine/simulator.py`、`entity/user.py` 和 `event/*.py`
- 删除 `core_engine/markov_predictor.py`
- 删除 `strategy/` 旧策略栈
- 删除 `algorithm/high_load.py`、`integration.py`、`mental_models.py`、
  `micro_dynamics.py`、`physiology.py`、`recovery.py`
- 精简 `services/event_semantics.py`
- 删除 CareTools direct assessment fallback 及旧依赖注入
- 新增 `mindflow-bot-runtime/tests/test_ctssm_output_regression.py`

### Phase 4：收敛兼容路径

- Agent：`worker.py`、`claude_runtime.py`、`session_manager.py`、`sdk_adapter.py`
- 配置：`config.py`、`main.py`、`app/smoke/feishu_gateway.py`
- 回复：`repositories.py`
- 展示：`presentation/response_orchestrator.py`
- 对应 Agent、Feishu、回复恢复、Presentation 与 E2E 测试

### Phase 5：权威文档与漂移保护

- `.gitignore`
- `docs/CURRENT_ARCHITECTURE.md`
- `mindflow-bot-runtime/README.md`
- `mindflow-bot-runtime/PROJECT_TASKS.md`
- `mindflow-bot-runtime/.env.example`
- `mindflow-bot-runtime/tests/test_authoritative_docs.py`

## 4. Correctness 修复

### 4.1 Observation commit、Forecast invalidation 与 fail-closed Warning

ObservationRepository 现在返回新提交/重复提交状态。只有新提交成功后，系统才在一个数据库
事务中把同一参与者、同一 `local_date` 的全部 current Forecast 标为失效，同时取消其
active Warning、清空 claim 并记录取消原因。此事务先于异步重算，因此新 Forecast 产生前
不会继续使用旧 Forecast 或发送旧 Warning。

若重算失败，旧 Forecast 不会恢复为 current，旧 Warning 也不会重新激活；后续定时流程仍可
生成新快照。duplicate check-in 是 no-op，不触发失效或重算。CardActionService 和
`care_record_checkin` 使用同一提交与刷新服务，日期均来自已提交 Observation 的本地日期。

`ObservationForecastRefreshService` 是显式 start/close 的托管服务。它按
participant/`local_date` 合并快速连续请求，通过 generation 追踪确保运行期间到达的新提交
最终被重算，同时避免无界 task 和重复并发。

### 4.2 Warning 2/240 配置

`WARNING_MAX_DAILY_SENDS=2` 与 `WARNING_MIN_INTERVAL_MINUTES=240` 的默认值只在
`Settings` 中定义。Bootstrap 只创建一份无默认值的 `WarningDeliveryPolicyConfig`，并把同一
对象传给 WarningPolicy、WarningScheduleRepository、ForecastCoordinator 和 Scheduler。

Repository 仍保留 durable claim、current Forecast、防重复、每日上限和最小间隔校验；变化
只是删除 Repository 内的第二份硬编码事实源。自定义 `3/60` 与 `1/300` 回归用例验证 policy、
sync 和 claim 都读取同一配置。

## 5. Legacy 删除与保留

### 已删除

- CareTools 直接调用 PredictionService 的 assessment fallback 与构造依赖；
- Simulator 的非 CTSSM 分支、旧 RK4/Markov/micro-dynamics 和完整 strategy stack；
- User 中只服务旧模型/策略的配置和行为；
- SQLite `EventSemanticEngine`、`SemanticInferenceCache`、全局 singleton、
  `assess_event_semantics` 与 `SEMANTIC_CACHE_PATH`；
- Runtime 全链的 `on_tool_use` 参数、桥接 callback 和 SDK 空分支；
- `FEISHU_APP_ID` / `FEISHU_APP_SECRET` Runtime 与 smoke fallback；
- legacy `pending_reply()` / `stage_reply()` 写入口；
- Presentation 内部 `presentation_agent_enabled` 布尔事实源。

### 有意保留

- 正式 CTSSM 动态状态模型、Timeline/StateMachine、AlertMonitor、事件 DTO 和参数容器；
- 纯事件语义规则、校验、融合函数和可选 OpenAI-compatible semantic client；
- Calendar credential 为空时回退到 Bot credential，这是当前正式双 App/单 App 部署策略；
- 历史单段 `reply_text` 的只读恢复，命中时记录 `legacy_reply_plan_recovered`；
- `PRESENTATION_AGENT_ENABLED` 的启动期解析映射：仅在 mode 未显式设置时映射
  `false -> off`、`true -> adaptive` 并只告警一次。内部运行逻辑只使用 mode。

## 6. CTSSM 删除前后回归对比

固定 fixture 在 legacy 删除前后完全一致：

| 项目 | 删除前 | 删除后 |
|---|---|---|
| MODEL_VERSION | `mindflow-ctssm-runtime-v6` | 相同 |
| model family | `stress-ctssm.m0` | 相同 |
| point count | `288` | 相同 |
| trajectory SHA-256 | `aef29bf7ee092bf19d76ee193834e16800f6a741f63925ffed60303f0197917e` | 相同 |
| alerts SHA-256 | `76589eb6ad7736b2c1470a13ac52d67c39558c79c99344586b88c5be9d2c82d0` | 相同 |
| warning windows SHA-256 | `bf605c640bb30feb352f6fc8912f68fd5d7364136abd86d1cd8fdf448a51ddda` | 相同 |
| confidence SHA-256 | `08ca5a9fb9fb688311a54a6e917c7b61d24a8eb33a42ab3eee3244bfeac4e295` | 相同 |
| terminal stress/vitality | `(4.98, 7.2)` | 相同 |
| active states / alerts | `("S",) / 2` | 相同 |

Fixture 同时覆盖 2 个 calendar events、1 个 observation point 和 1 个选中 warning window。

## 7. 验证结果

| 验证 | 真实结果 |
|---|---|
| 整改前基线 | `355 passed, 15 warnings in 96.47s` |
| Phase 1 专项 | `30 passed` |
| Phase 2 专项 | `155 passed, 14 warnings` |
| Phase 3 专项 | `136 passed, 14 warnings`，CTSSM hash 不变 |
| Phase 4 专项 | `64 passed` |
| Phase 4 全量 | `362 passed, 15 warnings in 96.91s` |
| Phase 5 文档/配置/迁移专项 | `25 passed` |
| 最终全量 | `365 passed, 15 warnings in 94.75s` |
| UTC 专项 | `62 passed, 1 warning in 14.85s` |
| compileall | 成功，无输出 |
| `git diff --check` | 通过，无输出 |

15 个全量 warning 来自 Starlette/httpx2 迁移提示和 matplotlib/pyparsing 弃用提示，未发现
本轮新增的 Runtime warning。

静态残留结果：

- 旧 Feishu env：0 production hits；测试中保留拒绝旧 fallback 的负向用例；
- 旧 SQLite semantic engine：0 production hits；测试中仅有“不存在旧入口”的断言；
- `on_tool_use`：0 hits；
- `stage_reply(`：0 hits；
- `legacy_model`：0 hits；
- Warning 大写配置名只在 `.env.example` 与 `Settings` 解析出现，Repository 无硬编码命中。

## 8. 剩余技术债（本轮有意不处理）

- `repositories.py` 仍较大，拆分应作为独立高风险重构；
- Python packaging / workspace 与 `sys.path`/`PYTHONPATH` 迁移未进行；
- 历史单段 reply 只读恢复仍保留，待确认旧 pending 数据完成迁移后删除；
- `PRESENTATION_AGENT_ENABLED` 环境解析映射仍保留一个发布周期，之后可独立删除；
- 云端真实 Feishu、DeepSeek、Docker 重启与备份证据仍属于 `PROJECT_TASKS.md` 的人工上线
  验收项，不属于本轮本地代码整改。
