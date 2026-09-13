# MindFlow Stage 03：Reminder 语义理解重构完成记录

## 完成结论（2026-09-13）

本文件后续章节保留重构前的分析、设计与验收要求；这些内容是实现输入，不是运行时指令。
本轮重构已完成：

```text
PASS  app/tools/reminder.py 不再解析中文自然语言时间
PASS  Reminder handler 只校验 RFC3339、timezone-aware 与未来时间等确定性不变量
PASS  reminder_create 通过 MutationIntentVerifier 独立验证用户语义与 proposal
PASS  模糊 daypart 不允许 Agent 自行补造具体钟点
PASS  跨轮 clarification 只通过 recent semantic turns 交给 verifier 判断
PASS  Agent 与授权校验统一使用 BotEvent ingress reference time
PASS  authorization context 不包含 participant/open_id/chat_id 等身份字段
```

独立提交：

```text
ee42978 refactor(reminder): move temporal semantics out of backend parsing
0a10351 fix(agent): unify reminder reference time across agent and authorization
```

验证环境：

```text
D:\Miniconda\envs\MentalProject
```

验证结果：

```text
Reminder 与授权相邻测试：114 passed
Agent、Worker 与 Reminder 相关测试：169 passed
完整 tests：1501 passed, 27 skipped, 0 failed
```

最终判定：

```text
Stage 03 = PASS / freeze
Stage 04 = PASS / freeze
```

## 一、方案结论

当前分支：

```text
feature/optimize-interaction
```

当前 HEAD：

```text
b4289375683ab21bd9dc2ab25dae2990acd28d6a
docs(stage03): record reminder timing optimization completion
```

本方案**替代**此前继续为：

```text
大后天
本周三
下周三
这周三
```

追加 Regex 规则的方案。

不要继续把 `app/tools/reminder.py` 扩展成中文自然语言时间解析器。

最终架构调整为：

```text
用户自然语言
    ↓
Main Agent
负责理解自然语言时间
    ↓
结构化 reminder_create proposal
    ↓
MutationIntentVerifier
负责确认“当前用户语义是否授权这个具体 proposal”
    ↓
Backend deterministic validation
只验证系统不变量
    ↓
ReminderRepository
```

核心边界：

```text
LLM / Agent：
理解“用户在说什么”

Semantic Verifier：
确认“用户是否真的授权这个具体 Tool Proposal”

Backend：
决定“这件事能不能安全、合法、一致地落库”
```

Backend 不再重新实现：

```text
大后天
下周
月底
周末
下午三点
过两个礼拜
下个月开学后
```

等开放式自然语言时间语义。

---

# 二、为什么要调整

当前 `app/tools/reminder.py` 已经包含：

```text
_CHINESE_TIME_NUMBER
_WEEKDAYS
ReminderTimeGrounding
ParsedReminderClock

_normalize_time_words()
_clock_only_clarification()
has_exact_time_grounding()
_grounding_turns()
_daypart()
_apply_daypart()
_parse_clock()
_parse_local_date()
resolve_reminder_time_grounding()
```

这套代码已经开始承担：

```text
中文数字解析
daypart
相对日期
星期
跨轮 clarification
relative duration
12/24 小时制
```

如果继续补：

```text
大后天
本周
下周
下下周
月底
月初
周末
过几天
下个月
```

本质上就是在项目内部重新实现一个 Temporal NLP Engine。

这不是 MindFlow 的核心业务，也不是合理的 Agent 架构。

---

# 三、现有基础已经足够支持更合理的设计

当前项目已经存在：

```text
MutationIntentVerifier
```

而且所有 `internal_write / external_write / destructive_external_write`
都会经过 ToolRegistry 的 mutation authorization。

`MutationIntentVerifier` 已经拿到：

```text
user_request_text
tool_name
tool_effect
authorization_requirement
proposal_summary
recent semantic turns
```

其现有系统规则也已经要求：

```text
current request
recent turns
backend-resolved target
requested values

必须互相一致
```

因此 Reminder 不需要在 handler 内再独立实现一遍自然语言语义解释。

真正缺少的是：

```text
可信 reference time + timezone
```

没有被一起交给 verifier。

本轮应补这一层，而不是继续补 Regex。

---

# 四、目标架构

## 4.1 Main Agent

用户：

```text
“下周三下午三点提醒我交作业”
```

Agent 根据：

```xml
<backend_time_context>
timezone=Asia/Shanghai
reference_datetime=...
</backend_time_context>
```

生成：

```json
{
  "message": "交作业",
  "remind_at": "2026-09-23T15:00:00+08:00",
  "recurrence_type": "none"
}
```

用户：

```text
“下周三下午提醒我交作业”
```

时间仍有歧义。

Agent：

```text
“具体几点提醒你？”
```

而不是自行选择 15:00。

---

## 4.2 MutationIntentVerifier

Verifier 负责判断：

```text
用户原话
+
最近相关对话
+
可信 reference time
+
timezone
+
reminder_create proposal

是否一致
```

例如：

```text
用户：
“明天15:00提醒我交作业”

proposal：
后天16:00

→ deny
```

```text
用户：
“明天下午提醒我交作业”

proposal：
明天15:00

→ needs_clarification
```

```text
用户：
“下周三下午三点提醒我交作业”

proposal：
正确解析后的下周三15:00

→ allow
```

---

## 4.3 Backend Handler

Backend 不再解释：

```text
“下周三”
“大后天”
“三点半”
```

只做：

```text
1. remind_at 是合法 RFC3339 / timezone-aware datetime
2. remind_at 是未来时间
3. recurrence_type 属于 allowlist
4. participant identity 来自 Backend Context
5. message 长度合法
6. quiet-hours warning
7. durable write
```

---

# 五、统一 Backend Time Authority

这是本轮必须一起修改的关键点。

当前：

```text
AgentContext.received_at_utc
= BotEvent ingress time
```

但：

```text
sdk_adapter._text_transport_prompt()
```

仍默认使用：

```python
datetime.now(...)
```

构造 `<backend_time_context>`。

这会产生两个 reference clock。

应统一。

---

## 5.1 AgentTurnInput 增加可信 reference time

修改：

```text
app/contracts/agent_input.py
```

新增：

```python
reference_time_utc: datetime | None = None
```

含义：

```text
当前用户 turn 的 Backend authoritative reference timestamp
```

不是用户输入，也不是模型生成。

---

## 5.2 Worker 写入 ingress time

Worker 构造 `AgentTurnInput` 时：

```python
reference_time_utc = event.create_time
```

所有 text / vision → Agent 的路径保持一致。

---

## 5.3 sdk_adapter 使用 reference_time_utc

`_text_transport_prompt()`：

```text
优先：
turn_input.reference_time_utc

仅 legacy/test compatibility：
缺失时 datetime.now()
```

然后：

```python
reference_time_utc.astimezone(ZoneInfo(timezone_name))
```

生成：

```xml
<backend_time_context>
timezone=Asia/Shanghai
local_datetime=...
local_date=...
</backend_time_context>
```

这样：

```text
Main Agent
Mutation Verifier
Backend authorization
```

都能基于同一个时间锚点。

---

# 六、给 Reminder Tool 增加 Authorization Context Resolver

当前 `reminder_create` 注册时没有专门的：

```text
authorization_context_resolver
```

新增一个只返回非敏感 Backend Context 的 resolver。

建议：

```python
def _authorization_context(
    self,
    ctx: AgentContext,
    _args: dict[str, Any],
) -> dict[str, Any]:
    if ctx.received_at_utc is None:
        raise AuthorizationContextResolutionError(
            "reminder_reference_time_unavailable"
        )

    return {
        "reminder_time_context": {
            "reference_time_utc": ctx.received_at_utc.isoformat(),
            "timezone": self.timezone.key,
        },
        "reminder_semantic_contract": {
            "exact_time_required": True,
            "ambiguous_time_requires_clarification": True,
        },
    }
```

注册：

```python
registry.register(
    "reminder_create",
    ...,
    authorization_context_resolver=self._authorization_context,
)
```

注意：

```text
不要放：
participant_id
open_id
chat_id
message_id
```

这里只提供时间语义判断所需的安全上下文。

---

# 七、增强 MutationIntentVerifier 的 Reminder 契约

修改：

```text
app/services/mutation_intent_verifier.py
```

不要重新增加 Regex。

在 `SYSTEM_PROMPT` 中增加 Reminder-specific contract：

```text
For reminder_create, compare the proposed remind_at with the participant's
actual time expression using the backend-provided reminder_time_context.

The proposed datetime must be semantically supported by the current request
and, only when necessary, the immediately relevant clarification turns.

Relative expressions such as “in one hour”, “tomorrow”, “next Wednesday”,
or equivalent Chinese expressions must be interpreted relative to the
backend-provided reference time and timezone.

If the participant provides only a broad daypart such as “tomorrow afternoon”
without an exact clock time, do not authorize a proposal that invents a clock
time; return needs_clarification.

Do not borrow unrelated dates or times from older conversation turns.
Do not modify or repair the proposed datetime yourself.
```

保持 verifier：

```text
temperature = 0
JSON only
allow / deny / needs_clarification
fail closed
```

---

# 八、简化 ReminderTools

修改：

```text
app/tools/reminder.py
```

## 8.1 删除 Temporal NLP Parser

删除仅为自然语言时间解析服务的代码：

```text
_CLOCK
_DATE
_DURATION
_REMINDER_INTENT
_CHINESE_TIME_NUMBER
_CHINESE_DIGITS
_WEEKDAYS

ReminderTimeGrounding
ParsedReminderClock

_chinese_number
_normalize_time_words
_clock_only_clarification
has_exact_time_grounding
_grounding_turns
_daypart
_apply_daypart
_parse_clock
_parse_local_date
resolve_reminder_time_grounding
```

如果有测试/其他模块引用：

```text
同步移除或改成新的 verifier contract test
```

不要保留“未来可能有用”的 dead parser。

---

## 8.2 `create()` 只做 deterministic validation

建议逻辑：

```python
def create(self, ctx, args):
    raw = str(args["remind_at"]).strip()

    try:
        remind_at = parse_rfc3339(raw)
    except ...:
        return invalid_reminder_datetime

    if remind_at.tzinfo is None:
        return reminder_timezone_required

    now = datetime.now(timezone.utc)

    if remind_at.astimezone(timezone.utc) <= now:
        return reminder_time_in_past

    row = self.reminders.create(
        ctx.participant_id,
        message=args["message"],
        remind_at=remind_at,
        recurrence_type=args["recurrence_type"],
    )

    ...
```

注意：

```text
ToolRegistry 的 MutationIntentVerifier 已经在 handler 前运行。
```

所以 handler 不再需要：

```text
has_exact_time_grounding()
resolve_reminder_time_grounding()
```

---

# 九、不要让 Agent 提供“是否明确”的自我声明

不要增加：

```json
{
  "time_is_exact": true
}
```

或：

```json
{
  "grounding": "exact"
}
```

然后 Backend 信任它。

这是模型自己给自己授权，没有价值。

最终语义一致性仍由：

```text
MutationIntentVerifier
```

独立验证。

---

# 十、不要给每个中文时间表达写 Backend Unit Test

旧路线会不断增加：

```text
大后天
下周三
本周三
月底
下个月
过两个礼拜
……
```

的 parser tests。

重构后测试目标改成：

```text
Agent / Verifier Contract
+
Backend Invariants
```

而不是：

```text
Backend 中文 NLP 覆盖率
```

---

# 十一、测试重构

## 11.1 ReminderTools deterministic tests

保留/增加：

```text
timezone-aware future datetime
→ create

naive datetime
→ reject

past datetime
→ reject

invalid datetime
→ reject

participant-bound
→ PASS

quiet-hours warning
→ 保持
```

不再测试：

```text
_parse_clock("下午三点")
_parse_local_date("大后天")
```

---

## 11.2 ToolRegistry + Verifier contract

用 Fake MutationIntentClient 检查 verifier payload。

### Case A：精确提醒

```text
user:
“明天15:00提醒我交作业”

proposal:
正确 ISO datetime

payload 必须包含：
- user_request_text
- proposal remind_at
- backend reference time
- timezone
```

fake verifier：

```text
allow
```

handler 才执行。

---

### Case B：模糊时间

```text
user:
“明天下午提醒我交作业”

proposal:
15:00
```

fake verifier：

```text
needs_clarification
```

断言：

```text
ReminderRepository.create = 0 call
```

---

### Case C：错误 proposal

```text
user:
“明天15:00提醒我交作业”

proposal:
后天16:00
```

fake verifier：

```text
deny
```

断言：

```text
Repository 0 write
```

---

### Case D：跨轮 clarification

```text
user:
“下周三下午提醒我交作业”

assistant:
“具体几点？”

user:
“三点”
```

验证：

```text
semantic_turn_context
```

正确传给 verifier。

具体中文时间如何映射由 Agent / verifier 负责，不由 Backend Regex 单测。

---

## 11.3 Backend time context

新增测试：

```text
event.create_time = T0
AgentTurnInput.reference_time_utc = T0

即使真正执行 _text_transport_prompt 的 wall clock = T0 + 20s

<backend_time_context>
仍然必须显示 T0 对应的本地时间
```

确保：

```text
一个 turn 只有一个 authoritative time reference
```

---

## 11.4 Verifier Prompt Contract

增加静态 contract test，锁住：

```text
reminder_create
exact proposed datetime
backend-provided reference time
timezone
ambiguous → needs_clarification
unrelated history cannot supply time
```

避免以后又把 Reminder 语义责任移回 Backend Regex。

---

# 十二、建议修改文件

核心：

```text
mindflow-bot-runtime/app/tools/reminder.py
mindflow-bot-runtime/app/services/mutation_intent_verifier.py
mindflow-bot-runtime/app/contracts/agent_input.py
mindflow-bot-runtime/app/worker.py
mindflow-bot-runtime/app/agent/sdk_adapter.py
```

测试：

```text
mindflow-bot-runtime/tests/test_reminders.py
mindflow-bot-runtime/tests/test_mutation_intent_verifier.py
mindflow-bot-runtime/tests/test_agent_runtime.py
```

实际 Agent runtime 测试文件名以仓库现有命名为准。

如有必要同步：

```text
claude-runtime/plugins/mindflow-care/skills/mental-health-care/SKILL.md
docs/CURRENT_ARCHITECTURE.md
```

---

# 十三、必须保持的现有能力

本轮是简化语义职责，不允许回归：

```text
1. Reminder participant-bound。
2. reminder_create 仍是 internal_write + direct_request。
3. vague request 仍必须 clarification。
4. provider delivery retry/backoff 不变。
5. delivery_failed 用户可见能力不变。
6. user reminder 不计 system proactive budget。
7. quiet-hours 对 user reminder 仍只 warning，不静默阻止。
8. Reminder sender 继续 asyncio.to_thread。
9. recurrence allowlist 不变。
10. tool schema 继续禁止 participant_id / open_id 等身份字段。
```

---

# 十四、不要做的事情

不要：

```text
继续增加“大后天” Regex
继续增加“本周/下周/下下周” Parser
引入完整中文 Temporal NLP Engine
在 Backend 写同义词/方言时间词大全
让 Agent 自报 time_is_exact=true
完全裸信 Main Agent proposal
删除 MutationIntentVerifier
```

正确路线是：

```text
Main Agent 语义理解
+
独立 Semantic Mutation Verifier
+
Backend deterministic invariants
```

---

# 十五、建议 Commit

建议拆两次：

```text
1. refactor(reminder): move temporal semantics out of backend regex parsing
2. fix(agent): unify reminder reference time across agent and authorization
```

如果 Codex 能保证改动清楚，也可以一个单主题 commit：

```text
refactor(reminder): use agent semantics with backend authorization invariants
```

---

# 十六、验收命令

定向：

```bash
cd mindflow-bot-runtime

python3 -m pytest -q \
  tests/test_reminders.py \
  tests/test_mutation_intent_verifier.py
```

再跑 Agent/runtime 相关测试。

最后：

```bash
python3 -m pytest -q tests
```

预期：

```text
0 failed
```

GitHub：

```text
MindFlow CI = success
```

---

# 十七、完成定义

完成后 Reminder 架构应满足：

```text
自然语言理解：
Agent / Semantic Verifier

身份与权限：
Backend

准确的 turn reference time：
Backend

状态修改授权：
MutationIntentVerifier + ToolRegistry

datetime 合法性：
Backend

未来时间约束：
Backend

持久化：
ReminderRepository

delivery retry：
ReminderScheduler
```

代码层面最明显的验收信号是：

```text
app/tools/reminder.py
不再存在一套持续扩张的中文时间 NLP parser。
```

最终：

```text
Stage 03 = PASS / freeze
Stage 04 = PASS / freeze
```
