# MindFlow 后续开发规范：Agent 语义理解与 Backend 执行边界

> 适用范围：MindFlow 后续所有功能开发、重构、Bug 修复、Code Review、Codex/Agent 自动修改。  
> 本规范属于项目级长期约束。新功能设计与代码实现必须优先遵守，不应因“实现简单”“方便测试”而绕开。
>
> 核心原则：
>
> **Agent 理解，Backend 落地，用户审核，Backend 执行。**

---

## 一、最高优先级原则

### 1. 禁止用硬编码规则替代自然语言语义理解

对于用户自然语言中的：

- 意图；
- 指代；
- 范围；
- 时间含义；
- 修改对象；
- 修改字段；
- 偏好；
- 支持风格；
- 操作目的；
- 上下文关系；

原则上应由 Agent 完成语义理解，并转换成结构化参数。

不得因为某几个测试样例能通过，就不断增加：

```python
if "以后" in text:
    ...

if "每次" in text:
    ...

if re.search(r"后面都|以后都|每一个", text):
    ...

if "截止" in text:
    changed_field = "end_time"
```

这类实现只能覆盖少量表述，会迅速演变为脆弱的中文关键词系统。

项目后续开发中，**禁止通过持续堆叠 Regex、关键词表、substring、if/elif 分支来“模拟自然语言理解”**。

---

## 二、统一交互架构

所有涉及自然语言理解并可能产生状态修改的功能，原则上遵循：

```text
用户自然语言
    ↓
Agent
理解用户真正想做什么
    ↓
Structured Proposal
把语义转成结构化字段
    ↓
Backend
绑定真实对象
校验权限
校验状态
校验业务不变量
计算真实影响范围
    ↓
Resolved Proposal / Preview
把最终将发生的事情展示给用户
    ↓
用户
确认 / 修改 / 取消
    ↓
Backend
执行真正的持久化或外部操作
```

简化为：

```text
Agent 理解
→ Backend 落地
→ 用户审核
→ Backend 执行
```

这是 MindFlow 后续交互设计的默认模式。

---

## 三、四类 Authority 必须明确分离

### 1. Agent：Semantic Authority

Agent 负责回答：

```text
用户这句话是什么意思？
```

包括：

- 用户想创建、修改、删除还是查询；
- “这个课”指向当前上下文中的哪个语义对象；
- “以后都这样”表示什么范围；
- “明天下午三点”是什么时间；
- “截止时间改到 15:40”修改的是哪个字段；
- “回答短一点”属于什么偏好；
- “不要一次给我很多建议”属于什么支持风格；
- 用户当前话语与上一轮对话之间是什么关系。

Agent 输出的是：

```text
结构化意图
```

而不是直接执行副作用。

---

### 2. Backend：Object / Invariant Authority

Backend 负责回答：

```text
Agent 理解出来的意图，具体在系统中对应什么？
这个操作实际上能不能做？
```

包括：

- participant 身份；
- ownership；
- event / course / reminder / memory 的真实 ID；
- 当前数据库状态；
- 当前 Calendar 状态；
- start/end 是否有效；
- occurrence 数量；
- recurrence 是否有效；
- semester 范围；
- Tool Schema；
- 状态机；
- 幂等性；
- 数据一致性；
- Provider capability；
- 数据库约束。

Backend 不负责重新猜用户原话是什么意思。

---

### 3. User：Effect Authority

对于会产生持久化影响或外部影响的操作，用户应能看到：

```text
系统最终理解成了什么
最终会修改什么
影响范围是什么
```

然后用户选择：

```text
确认
修改
取消
```

用户确认的是：

```text
Resolved Proposal
```

而不是确认一段模糊自然语言。

---

### 4. Backend：Final Execution Authority

用户确认后，真正的副作用仍由 Backend 执行：

- 写 PostgreSQL；
- 写 Calendar；
- 删除 Calendar；
- 创建 Reminder；
- 修改 durable preference；
- 删除 durable memory；
- 执行 Course import / revert；
- 更新状态机。

Agent 不应直接持有 Provider 写权限。

---

# 四、Regex / 硬编码规则的明确使用边界

Regex 本身不是禁止项。

禁止的是：

> **用 Regex 承担开放式自然语言语义理解。**

---

## 允许使用 Regex 的场景

### 1. 格式校验

例如：

```text
HH:MM
UUID
RFC3339
课程节次格式
Card element_id
固定枚举编码
```

例如：

```python
r"^(?:[01]\d|2[0-3]):[0-5]\d$"
```

这是合理的。

---

### 2. 已结构化数据的 deterministic normalization

例如 Vision 已经输出：

```json
{
  "weekday": "周二",
  "start_time": "8：00"
}
```

Backend 将：

```text
周二 → 2
8：00 → 08:00
```

这是格式归一化，不属于自然语言意图理解。

---

### 3. Security / Privacy Guard

例如：

- API key；
- token；
- password；
- secret；
- SSRF；
- path traversal；
- prompt injection 的明显危险模式；
- 日志脱敏。

这类规则属于安全边界。

---

### 4. 固定协议解析

例如：

- RRULE；
- HTTP header；
- provider error code；
- CardKit callback payload；
- migration/version identifier。

这些不是用户语义。

---

## 禁止使用 Regex 的场景

不得用 Regex 决定：

```text
用户到底想做什么
```

例如：

```python
if re.search(r"以后|后面|每次|都", text):
    scope = "all_future"
```

禁止。

---

不得用 Regex 决定：

```text
用户修改的是哪个业务字段
```

例如：

```python
if "截止" in text:
    target = "end_time"
```

禁止。

---

不得用 Regex 决定：

```text
这是 Memory 还是 Preference
```

禁止。

---

不得用 Regex 决定：

```text
用户是不是在请求 Reminder
```

禁止。

---

不得用 Regex 决定：

```text
用户是否真的授权了 Agent 刚刚理解出的复杂语义
```

如果最终操作本来就会展示 Confirmation Card，则不应再维护第二套 Regex/LLM 去重做自然语言理解。

---

# 五、避免“第二套语义系统”

一个重要反模式是：

```text
Main Agent 理解用户
↓
生成 structured args
↓
Backend 又通过 Regex / 第二个 LLM
重新判断用户原话是不是这个意思
```

除非属于独立 Safety / Privacy / Policy 风险，否则原则上禁止这种“双重语义解释”。

原因：

1. Main Agent 和 verifier 可能产生不同理解；
2. false deny；
3. 同一句话需要维护两套 prompt / rule；
4. 用户明明还要最终确认，却在确认前先被 verifier 卡死；
5. 系统复杂度快速增加。

如果操作具有最终用户确认环节，推荐：

```text
Agent 解释
→ Backend validate
→ 用户确认
```

而不是：

```text
Agent 解释
→ 第二个 Agent 再解释
→ Backend
→ 用户确认
```

---

# 六、Structured Proposal 是 Agent 与 Backend 的边界

Agent 不应把自己的自然语言 reasoning 直接传给 Backend。

Agent 应产生严格 schema。

例如用户说：

```text
“这个课后面每次都改成 15:40 下课。”
```

推荐：

```json
{
  "operation": "update_course_schedule",
  "target": {
    "reference": "current_course"
  },
  "scope": "current_semester_remainder",
  "changes": {
    "end_time": "15:40"
  }
}
```

不要：

```json
{
  "raw_user_text": "这个课后面每次都改成15:40下课"
}
```

然后让 Backend 再 Regex 解析一次。

---

# 七、Backend 使用 PATCH Semantics，不强迫 Agent 重复未修改字段

如果用户只要求修改：

```text
end_time
```

Agent 就只提交：

```json
{
  "changes": {
    "end_time": "15:40"
  }
}
```

Backend 负责：

```text
current object
+ changes
→ complete proposed object
```

未出现的字段默认：

```text
保持原值
```

禁止为了满足 Backend schema 强迫 Agent：

```text
重新读取并复制所有未修改字段
```

否则容易造成：

- Tool loop 增多；
- token 增多；
- Agent 无意覆盖旧值；
- “只改一个字段”变成“重新提交整个对象”。

---

# 八、必须区分 Draft、Proposal 与 Effect

后续 ToolEffect 建议按语义进一步区分。

## `read`

只读。

---

## `compute`

计算、预测、模拟。

---

## `draft_write`

修改短期 Draft。

特征：

```text
可逆
尚未产生真实 Provider effect
之后还有 Preview/Confirm
```

例如：

- 课程表识别 Draft 修正；
- Preview 中修改单门课程时间。

这种操作一般无需第二次 semantic authorization。

---

## `proposal_stage`

保存：

```text
“如果用户确认，将做什么”
```

但本身不得产生最终副作用。

例如：

- Calendar update plan；
- Reminder proposal；
- Memory update proposal；
- destructive revert proposal。

Agent 可以 stage proposal。

---

## `confirmed_effect`

真正：

- 写 Provider；
- 写 durable state；
- destructive action。

原则上：

```text
不直接暴露给 Agent
```

而通过：

```text
CardAction / Form Submit / explicit confirmation state
```

进入。

---

# 九、User Review Card 的要求

确认卡必须让普通用户看得懂。

不要展示：

```text
event_id
UUID
UTC ISO string
internal enum
raw RRULE
database status code
```

应该展示：

```text
课程：操作系统
原时间：14:00–14:45
修改后：14:00–15:40
范围：从今天开始，本学期剩余 12 次
```

并提供：

```text
[确认修改]
[修改信息]
[取消]
```

---

## 用户审核的对象必须是“最终解析结果”

不是：

```text
“你确定要修改吗？”
```

而是：

```text
你将修改什么
改成什么
影响多少对象
```

---

# 十、不要制造“确认疲劳”

并不是任何 Agent 行为都需要卡片。

推荐：

### 一般不需要确认

- 查询；
- Search；
- Forecast；
- Explanation；
- Simulation；
- Draft 内部修正；
- UI navigation；
- ephemeral conversational style。

### 通常需要确认

- Calendar create/update/delete；
- Course import 最终写入；
- Course import revert；
- Reminder 创建/取消；
- durable Memory 写入/删除；
- durable Preference 修改；
- destructive operation。

### Form Submit 本身即可视为确认

例如：

- check-in；
- daily review；
- settings form。

不需要 Form Submit 后再弹一次“你确定吗”。

---

# 十一、Backend 遇到歧义时不要自行猜测

如果 Agent 提交：

```text
“修改高数”
```

而 Backend 解析到两个真实对象：

```text
高等数学 A
高等数学 B
```

Backend 应返回：

```json
{
  "ok": false,
  "error": "target_ambiguous",
  "candidates": [...]
}
```

然后：

```text
Agent 向用户提出一个必要的澄清问题
```

不要：

```text
candidates[0]
```

不要通过：

```text
Regex / fuzzy score
```

偷偷选一个执行。

---

# 十二、Agent 自主权不等于 Agent 有最终权限

“给 Agent 更多自主权”只表示：

```text
更多语义理解权
```

不表示：

```text
更多安全权
更多身份权
更多数据库权
更多 Provider 写权限
```

Agent 不得决定：

- participant_id；
- user ownership；
- OAuth token；
- database primary key；
- provider identity；
- research authorization；
- privacy consent；
- safety bypass；
- arbitrary URL；
- arbitrary SQL；
- filesystem path。

---

# 十三、安全、隐私与研究边界是特殊例外

以下原则高于：

```text
Agent 理解 → 用户审核
```

包括：

- Safety；
- Privacy；
- Consent；
- Research access control；
- Identity；
- SSRF/security；
- clinical-data policy。

即使：

```text
Agent 认为可以
用户也点击确认
```

Backend 仍必须：

```text
deny
```

如果违反 policy。

---

# 十四、Safety 不允许简单“全部交给 Agent”

Safety 可以增加 context-aware semantic classifier，减少关键词误判。

但最终流程应是：

```text
Safety semantic understanding
↓
Backend Safety Policy
↓
fixed / controlled response behavior
```

而不是：

```text
Main Agent 自己判断是否执行安全规则
```

主 Agent 不具备关闭 Safety Gate 的权限。

---

# 十五、研究模型中的规则不能与交互语义规则混为一谈

MindFlow 中存在一些：

- workload lexical prior；
- difficulty floor；
- appraisal prior；
- event semantic rules；
- CTSSM 参数规则。

它们属于：

```text
研究模型设计
```

不是：

```text
对话交互 NLP
```

因此不能因为本规范反对 Regex NLP，就直接删除这些模型规则。

它们应通过：

- ablation；
- sensitivity analysis；
- rolling-origin validation；
- versioned model comparison；

决定是否保留。

---

# 十六、新功能设计前必须先回答 6 个问题

任何新的自然语言功能，在写代码前必须明确：

### 1. 哪部分是用户语义？

应该由 Agent 负责。

### 2. Agent 输出什么结构化 Schema？

禁止只传 raw text 给 Backend 再解析。

### 3. Backend 需要绑定哪些真实对象？

例如：

- participant；
- event；
- course；
- reminder；
- memory。

### 4. Backend 有哪些 deterministic invariant？

例如：

- ownership；
- time range；
- recurrence；
- limit；
- state transition。

### 5. 该操作是否产生 durable / external effect？

如果是，设计用户审核。

### 6. 用户审核后由哪个 Backend executor 执行？

不能让 Agent 直接越过确认层。

---

# 十七、Code Review 强制检查项

后续任何 PR / Codex 修改都必须检查以下内容。

## Semantic Boundary

- [ ] 是否新增了用于理解用户意图的 Regex？
- [ ] 是否新增了用户语义关键词表？
- [ ] 是否新增了大量 `if "中文词" in text`？
- [ ] 是否让 Backend 再次解释 Agent 已经结构化过的语义？
- [ ] 是否可以改为 Agent structured output？

只要任一答案为“是”，必须说明原因。

---

## Backend Boundary

- [ ] participant identity 是否完全 Backend-bound？
- [ ] object ID 是否由 Backend resolver 确认？
- [ ] schema 是否足够严格？
- [ ] invariant 是否由 Backend 校验？
- [ ] 是否存在 Agent 自己生成数据库/Provider identity？

---

## User Review

- [ ] 是否属于 durable/external effect？
- [ ] 用户是否能看到最终解析结果？
- [ ] 是否清楚展示影响范围？
- [ ] 是否提供确认/修改/取消？
- [ ] 是否避免重复确认和确认疲劳？

---

## Execution

- [ ] 真正 effect 是否只发生在用户确认后？
- [ ] confirmed executor 是否未暴露给 Agent？
- [ ] 是否具有 idempotency？
- [ ] 是否具有 participant ownership check？
- [ ] 是否正确处理 retry / recovery？

---

# 十八、测试规范

测试不应只测试：

```text
“这句话 Regex 能不能匹配”
```

重点测试系统边界。

---

## Semantic tests

给 Agent 多样表达：

```text
“这个课以后都晚半小时下课”
“从今天开始，这门课后面都延长到三点四十”
“本学期剩下的操作系统都改成15:40结束”
```

要求：

```text
得到等价 Structured Proposal
```

而不是要求 Backend Regex 同时覆盖这三句话。

---

## Backend tests

直接输入 structured args：

```text
Backend 是否正确绑定对象
Backend 是否拒绝越权
Backend 是否保留未修改字段
Backend 是否正确计算 scope
```

---

## Review tests

验证：

```text
确认前无 effect
取消后无 effect
修改 proposal 后按最新 proposal 执行
重复确认幂等
```

---

# 十九、明确禁止的开发方式

以下行为视为架构倒退。

### 禁止 1

遇到一个新自然语言失败样例，就：

```text
再补一个 Regex
```

---

### 禁止 2

为了“保险”：

```text
Main Agent 理解一次
Backend Regex 理解一次
Verifier LLM 再理解一次
```

---

### 禁止 3

Tool Schema 为方便 Backend：

```text
要求 Agent 重复填写未修改字段
```

---

### 禁止 4

Agent 直接持有：

```text
external destructive executor
```

---

### 禁止 5

Backend 发现语义歧义：

```text
自行选择第一个候选
```

---

### 禁止 6

用户只看到：

```text
“是否确认？”
```

却看不到系统最终准备执行的内容。

---

# 二十、推荐实现方式

遇到新需求时，优先考虑：

```text
Agent Tool Schema
+
Backend Resolver
+
Domain Validator
+
Proposal Repository
+
Preview / Confirmation Card
+
Confirmed Executor
```

而不是：

```text
raw text
+
Regex parser
+
一堆 if/elif
+
直接执行
```

---

# 二十一、一句话开发准则

后续所有开发者、Codex、AI Agent 都必须牢记：

> **不要试图让 Backend 通过 Regex 学会“听懂人话”。**
>
> **自然语言交给 Agent，确定性边界交给 Backend。**
>
> **Agent 先提出结构化理解，Backend 将其绑定成真实可执行方案，用户审核最终方案，Backend 再执行真正副作用。**

即：

```text
Agent 理解
Backend 落地
用户审核
Backend 执行
```

这应作为 MindFlow 后续长期开发的默认架构原则。
