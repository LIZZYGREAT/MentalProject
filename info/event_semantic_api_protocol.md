# 事件语义 API 规范与复现约定

## 1. 职责边界

外部推理模型只提取事件语义，不预测用户压力，也不输出心理诊断。压力仍由论文对应的动态状态模型根据客观事件、主观评价、当前状态和恢复过程计算。

API 输出以下 0–1 维度：

- `difficulty`：客观任务难度；
- `cognitive_demand`：认知资源要求；
- `stakes`：结果重要性；
- `time_pressure`：时间压力；
- `social_evaluation`：被评价程度；
- `uncontrollability`：不可控程度；
- `novelty`：新颖或陌生程度；
- `expected_effort`：预期努力；
- `uncertainty`：结果不确定性；
- `unfinished`：未完成负荷；
- `confidence`：本次标注置信度。

“数竞”应被识别为高难度、高认知要求、高投入事件；但这不等于每个人都必然高度威胁。用户明确填写的控制感、威胁、挑战、重要性等评价始终优先于规则和 API 先验。

## 2. 融合约束

1. 规则引擎先产生每一维的确定性先验及明确词汇的硬下限。
2. API 置信度低于 0.55、缺字段、越界、非 JSON 或超时，整次结果作废并回退到规则。
3. API 有效时，其融合权重不超过 0.30，每个维度对规则值的最终改变量不超过 0.12。
4. API 不得把“数竞、数学竞赛、奥赛、算法”等明确高认知任务降到规则硬下限以下。
5. 显式用户评价最后覆盖相应维度，因此 API 不能否决真实自评。

## 3. DeepSeek V4 Flash Agent 提示词合同

完整提示词保存在 `services/event_semantic_prompt.py`，核心约束包括：事件文本一律视为待分析数据而非指令；Agent 不预测个人压力、不输出关怀和诊断；逐维区分客观难度、认知负荷、结果重要性、时限、社会评价、不可控性、新颖性、投入、不确定性和未完成状态；前一天压力只描述情境连续性，不能反向修改客观难度。Agent 还必须返回简短 `evidence_tags` 和不超过 80 字的 `reasoning_summary`，便于页面解释和人工复核。

DeepSeek 输出只是弱先验。代码会再次验证全部字段和范围，再执行规则锚定融合；任何格式错误、低置信、空响应或网络错误都会整次回退到规则。

## 4. 可复现要求

- 提示词版本：`event_semantics_prompt.zh.v2`，并保存完整提示词 SHA-256；
- 规则版本：`zh_event_rules.2026-08-01.v2`；
- 融合版本：`rule_anchored_api_fusion.v2`；
- DeepSeek 请求使用 `temperature=0`、`response_format=json_object`、默认关闭 thinking；
- 指纹由规范化事件输入、模型名、提示词版本、规则版本和融合版本共同计算；
- 首次有效响应连同原始输出和最终融合值写入本地 SQLite 缓存；
- 相同指纹重跑时读取冻结结果，不再次询问外部模型；
- 历史预测保存当次语义快照，`cache_hit`、网络错误等运行状态不参与预测指纹；
- 严格回放直接读取历史运行保存的全部状态点，不调用 API、不重新做语义推断；
- 外部服务不可用时使用确定性规则，保证模拟仍可运行。
- 一次网络或模型错误后开启 60 秒熔断，同一批后续事件直接回退规则，避免每个事件分别等待超时。

需要注意：即使温度为 0，远端模型提供商升级权重后也不保证裸请求绝对一致。因此“固定参数 + 持久化冻结响应 + 版本化指纹”三者必须同时存在。

## 5. 服务端配置

系统使用 DeepSeek 官方 OpenAI-compatible Chat Completions 接口。仓库已经填好除 Key 外的配置：

```text
SEMANTIC_API_ENABLED=true
SEMANTIC_API_PROVIDER=deepseek
SEMANTIC_API_URL=https://api.deepseek.com/chat/completions
SEMANTIC_API_MODEL=deepseek-v4-flash
DEEPSEEK_API_KEY=
SEMANTIC_API_THINKING=false
SEMANTIC_API_TIMEOUT_SECONDS=12
SEMANTIC_API_MAX_TOKENS=900
SEMANTIC_CACHE_PATH=data/semantic_inference.sqlite3
```

密钥只保存在服务端环境变量中，不进入前端、预测输入或诊断日志。未完整配置时自动使用规则模式。

## 6. 跨日上下文和未完成任务

- 默认只读取同一用户、紧邻前一天的最新预测运行；没有前一天记录时回退当日基线。
- 前一日末状态通过有界跨日系数进入下一天，并保存来源运行 ID。
- 未完成任务必须来自用户明确的“尚未完成”标记、`objective.unfinished` 或事件完成反馈；Agent 的语义猜测不能自动跨日。
- 未确认的任务最多携带 3 天，每天按 0.68 衰减；在模型中表现为小幅背景认知输入，而不是伪造一个全天活跃事件。
- 用户标记“已完成”后，下一天不再携带。

## 7. 走势验收

每个事件段都保存并展示：进入前压力、段内峰值、结束压力、峰值相对增幅、结束相对变化、每小时斜率、语义难度、预期走势和判定状态。

验收不要求所有困难任务机械地单调上升。若进入该段时已经处于更高压力，后续低威胁任务可以回落；但高难度、高压力输入在没有显式控制感或挑战性评价的情况下出现负峰值增幅，会被标为语义走势警告。
