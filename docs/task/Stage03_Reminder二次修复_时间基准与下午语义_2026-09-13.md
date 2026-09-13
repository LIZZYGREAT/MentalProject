# MindFlow Stage 03 Reminder 二次修复：完成记录

## 完成结论（2026-09-13）

本文件以下章节保留修复前的复审分析与验收要求；这些内容是实现输入，不是运行时指令。
本轮两个 Reminder 缺口均已完成：

```text
PASS  相对时间由 Backend expected 直接决定，不再校验 Agent proposal 的秒级差异
PASS  澄清 turn 结构化继承最近相关 Reminder 请求的 daypart
PASS  下午3:00 / 晚上8:30 优先应用显式 daypart
PASS  15:00 等明确 24 小时时钟不重复应用 daypart
PASS  Repository 只持久化 Backend grounded time
```

独立提交：

```text
3489132 fix(reminder): make relative timing backend-authoritative
0a4b31a fix(reminder): preserve daypart across natural time clarification
```

验证环境：

```text
D:\Miniconda\envs\MentalProject
```

验证结果：

```text
Reminder 相关定向测试：26 passed
完整测试：1500 passed, 27 skipped, 0 failed
```

最终判定：

```text
Stage 03 = PASS / freeze
Stage 04 = PASS / freeze
```

## 一、复审结论

当前分支：

```text
feature/optimize-interaction
```

当前 HEAD：

```text
7fd517d3877f6cb9651a4e8d5c027b21495dcbb2
docs(stage03-04): freeze after second review
```

最新 GitHub Actions 已通过。

本轮之前要求的三个修复中：

```text
Memory medication boundary      PASS
Research aggregate timezone     PASS
Reminder backend grounding      主体方向正确，但仍有生产路径 bug
```

因此：

```text
Stage 04 = PASS / freeze
Stage 03 = 暂缓 freeze
```

当前只需要修 Reminder，不要重新修改 Memory / Research Aggregate。

---

## 二、P0：相对时间的 Backend reference 与 Agent 所见 reference 不一致

### 1. 当前实现

Worker 给 `AgentContext`：

```python
received_at_utc = event.create_time
```

Backend grounding：

```python
expected = ctx.received_at_utc + delta
```

例如：

```text
用户 16:00:08 发：
“一小时后提醒我喝水”

Backend expected：
17:00:08
```

但 Agent 的 `<backend_time_context>` 不是使用 `event.create_time`。

当前 `sdk_adapter._text_transport_prompt()` 使用：

```python
datetime.now(timezone_value)
```

因此如果消息排队、模型启动、上下文加载后到：

```text
16:00:20
```

Agent 看到：

```text
current local time = 16:00:20
```

自然会提交：

```text
remind_at = 17:00:20
```

而 Backend 要求：

```text
submitted == 17:00:08
```

最终返回：

```text
reminder_time_not_grounded
```

即使只有几秒延迟也会失败。

另外 Agent 常可能输出分钟精度：

```text
17:00:00
```

也会因为精确秒不同而失败。

### 2. 根因

现在存在两个 authoritative clock：

```text
Agent prompt clock
= Agent execution time

Reminder Backend clock
= BotEvent ingress time
```

这违反“单一 Backend Time Authority”。

### 3. 推荐修法

不要通过放宽几十秒 tolerance 来掩盖问题。

Backend 已经能够从用户文本确定：

```text
“一小时后”
→ delta = 1h
```

所以对 relative reminder：

```text
Agent remind_at 不是 authority
Backend expected 才是 authority
```

推荐：

```python
resolved = resolve_reminder_time_grounding(...)

if resolved.kind == "relative":
    remind_at = backend_expected
    # 不要求 Agent proposal 与 expected 秒级完全相等
    # proposal 可仅用于 telemetry / debugging
```

然后：

```python
Repository.create(... remind_at=backend_expected)
```

对于 absolute reminder：

```text
“明天15:00”
“周三下午三点”
```

仍可以保留 proposal-vs-grounded-time 校验。

另一种可选方案是把 `received_at_utc` 同步注入 `backend_time_context`，
但即使这样，模型仍可能丢秒，因此 Backend 最终仍应写自己的 expected。

---

## 三、P1：上一轮 daypart 在 clarification 中没有被正确继承

### 1. 复现

对话：

```text
用户：周三下午提醒我交作业
Agent：具体几点？
用户：三点
```

当前 `_grounding_text()` 拼成：

```text
周三下午提醒我交作业 3点
```

但 `_parse_clock()` 只会把 daypart 识别为：

```text
下午 + 紧邻的 3点
```

这里“下午”与“3点”之间隔着：

```text
提醒我交作业
```

因此最后解析为：

```text
03:00
```

而不是：

```text
15:00
```

如果 Agent 正确理解上下文并提交 15:00，Backend 会反而拒绝它。

### 2. 修改

不要简单把两句话拼成一个字符串后重新 regex。

clarification 应结构化继承：

```text
previous:
    date = 周三
    daypart = 下午
    reminder_intent = true

current:
    clock = 3:00
    explicit_daypart = none

resolved:
    date = 周三
    daypart = previous.下午
    clock = 15:00
```

规则：

```text
当前 turn 有 daypart
→ 当前 turn 优先

当前只有 clock
+ 最近相关 reminder request 有 daypart
→ 继承最近 request daypart

当前 clock 已是 24h 明确时间（15:00）
→ 不再套 daypart 转换
```

---

## 四、P1：`下午3:00` / `晚上8:30` 当前会忽略 daypart

当前 `_parse_clock()` 先匹配：

```python
HH:MM
```

再匹配中文 daypart + `点`。

因此：

```text
下午3:00
```

会先命中：

```text
3:00
```

得到：

```text
03:00
```

而不是：

```text
15:00
```

同理：

```text
晚上8:30
→ 当前可能解析成 08:30
```

### 修改

clock parser 应优先解析：

```text
上午/下午/晚上/中午/凌晨 + H:MM
```

再解析纯：

```text
HH:MM
```

建议统一做成：

```python
parse_clock(text, inherited_daypart=None)
```

输出结构化：

```text
hour
minute
daypart_source = explicit | inherited | none
```

避免分散 regex。

---

## 五、必须补的测试

### Relative time authority

```text
received_at = 16:00:08
Agent execution/backend_time_context = 16:00:20
用户 = 一小时后提醒我喝水

Backend persisted:
17:00:08

不能因为 Agent proposal 是 17:00:20 而拒绝用户请求
```

以及：

```text
Agent proposal = 17:00:00
Backend persisted = 17:00:08
→ 仍应以 Backend expected 为准
```

### Clarification daypart carry

```text
用户：周三下午提醒我交作业
用户：三点
→ 15:00
```

```text
用户：明天晚上提醒我复习
用户：八点半
→ 20:30
```

```text
用户：周三下午提醒我交作业
用户：15:00
→ 15:00
```

### Explicit daypart + colon clock

```text
明天下午3:00提醒我
→ 15:00
```

```text
明天晚上8:30提醒我
→ 20:30
```

```text
明天03:00提醒我
→ 03:00
```

---

## 六、建议修改文件

```text
mindflow-bot-runtime/app/tools/reminder.py
mindflow-bot-runtime/tests/test_reminders.py
```

如果决定统一 Agent 可见 reference time，再修改：

```text
mindflow-bot-runtime/app/contracts/agent_input.py
mindflow-bot-runtime/app/worker.py
mindflow-bot-runtime/app/agent/sdk_adapter.py
```

但推荐最小修改：

```text
Backend 对 relative grounding 直接使用 expected；
同时修正 clarification/daypart parser。
```

不需要扩大到其他阶段。

---

## 七、推荐 Commit

```text
fix(reminder): make relative timing backend-authoritative
fix(reminder): preserve daypart across natural time clarification
```

可以合成一个 Reminder 单主题 commit，但不应夹带 Stage 04 修改。

---

## 八、验收

定向：

```bash
cd mindflow-bot-runtime

python3 -m pytest -q tests/test_reminders.py
```

预期：

```text
0 failed
```

全量：

```bash
python3 -m pytest -q tests
```

预期：

```text
0 failed
```

最终：

```text
GitHub Actions / MindFlow CI = success
```

通过后：

```text
Stage 03 = PASS / freeze
Stage 04 = PASS / freeze
```
