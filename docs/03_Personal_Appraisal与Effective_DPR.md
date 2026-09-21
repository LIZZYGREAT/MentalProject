# MindFlow Personal Appraisal 与 Effective D/P/R 建模

> 本文定义 participant-specific Personal Appraisal、其证据更新方式，以及 Event Potential 如何转换为 Effective Demand / Pressure / Recovery。本文默认事件层已经提供 $D_e^{pot}$、$U_{ddl,e}$、$U_{context,e}$、$D_{s,e}$、$R_e^{pot}$ 与 mechanism-specific lifecycle gates，不重复事件表示与 lifecycle 规则。

---

## 一、模块职责

事件层只描述：

> 这件事本身具有什么 Demand、Pressure structure 和 Recovery opportunity，以及它实际上发生到什么程度。

Appraisal 层回答：

> 对当前 participant 而言，这个 episode 的能力匹配、重要性、可控性、不确定性和恢复适配性如何？

最终将事件层输入转化为：

$$
\boxed{
D_{i,e}^{eff},
\quad
P_{i,e}^{eff},
\quad
R_{i,e}^{eff}
}
$$

其中：

- $i$：participant；
- $e$：event / episode；
- $D_{i,e}^{eff}$：participant-specific effective demand；
- $P_{i,e}^{eff}$：participant-specific effective pressure；
- $R_{i,e}^{eff}$：participant-specific effective recovery。

Personal Appraisal 是 personalization correction，不是模型运行的必要前提。即使大量 appraisal 为 Unknown，Event Potential 仍然可以正常驱动模型。

---

## 二、Personal Appraisal v1 变量

当前保留五个 appraisal 维度：

$$
\boxed{
Appraisal_{i,e}
=
[
C_{i,e}^{exec},
I_{i,e},
C_{i,e}^{out},
U_{i,e}^{perc},
F_{i,e}^{rec}
]
}
$$

| 变量 | 名称 | 含义 | 主要作用通道 |
|---|---|---|---|
| $C_{i,e}^{exec}$ | Execution Competence | 用户认为自己处理/执行该事件的能力与匹配程度 | Demand |
| $I_{i,e}$ | Personal Importance / Stakes | 事件或其结果对该用户的重要程度 | Pressure |
| $C_{i,e}^{out}$ | Outcome Control | 用户认为自己仍能多大程度影响结果 | Pressure |
| $U_{i,e}^{perc}$ | Perceived Uncertainty | 用户主观感受到的过程/结果不确定性 | Pressure |
| $F_{i,e}^{rec}$ | Personal Recovery Fit | 该类活动对该用户通常有多大实际恢复适配性 | Recovery |

---

## 三、Execution Competence 与 Objective Difficulty 的边界

$C^{exec}$ 表示 person-event capability fit，而不是事件本身“客观有多难”。

必须保持：

$$
\boxed{
ObjectiveDifficulty
\rightarrow
D_e^{pot}
}
$$

$$
\boxed{
PersonalCapabilityFit
\rightarrow
C_{i,e}^{exec}
}
$$

例如：

- “这门课内容本身很复杂”属于 $D_e^{pot}$；
- “我对这门课通常很擅长”属于 $C^{exec}$。

因此不再单独保留 `perceived difficulty` 作为 v1 动态变量，避免和 $D^{pot}$ 重复。

---

## 四、Outcome Control 与 Execution Competence 的区别

必须区分：

$$
\boxed{
C^{exec}
=
\text{Can I handle the activity?}
}
$$

与：

$$
\boxed{
C^{out}
=
\text{Can I influence the outcome?}
}
$$

例如：

- 已充分准备但结果受外部评审影响：$C^{exec}$ 高、$C^{out}$ 可能低；
- 自学任务本身较难但没有强制结果约束：$C^{exec}$ 低、$C^{out}$ 可以较高。

两者进入不同通道，不合并。

---

## 五、当前不进入 v1 Dynamics 的字段

### 1. Stress Relevance

不进入 v1 dynamics。

原因是它容易形成：

```text
“这件事让我很有压力”
        ↓
Stress Relevance ↑
        ↓
P_eff ↑
        ↓
S ↑
```

从而把一条 outcome-like stress expression 重新作为输入，形成隐式第二 EMA。

因此：

$$
\boxed{
StressRelevance
\notin
DynamicAppraisal
}
$$

### 2. Interest

`interest` 保留为 profile/context metadata，不直接进入 $D/P/R$。

必须保持：

$$
Interest
\neq
Demand
$$

$$
Interest
\neq
Pressure
$$

$$
Interest
\neq
Recovery
$$

后续只有在 Pilot/Synthetic 有充分证据时，才考虑作为 moderator。

---

## 六、Appraisal 的状态表示

每个 appraisal 不使用假精确连续真值，而维护 categorical belief：

$$
\boxed{
\boldsymbol{\pi}_{i,e}^{(k)}
=
[
P(Low),
P(Medium),
P(High)
]
}
$$

其中：

$$
k\in
\{
C^{exec},
I,
C^{out},
U^{perc},
F^{rec}
\}
$$

例如：

$$
\boldsymbol{\pi}_{i,e}^{exec}
=
[0.1,0.3,0.6]
$$

表示当前更倾向 High execution competence，但保留 uncertainty。

必须保持：

$$
\boxed{
Unknown
\neq
Medium
}
$$

Unknown 是 belief distribution；Medium 是 appraisal state。

---

## 七、Appraisal 的序数编码

在进入确定性函数时，将：

$$
Low,\ Medium,\ High
$$

映射为：

$$
\boxed{
\ell(a)
=
\begin{cases}
-1,&a=Low\\
0,&a=Medium\\
+1,&a=High
\end{cases}
}
$$

该编码只表达有序方向，不声称三个等级之间具有精确等距心理含义。

---

## 八、冷启动与大学生周期性先验

真实运行中，大量 appraisal 会长期 Unknown，因此冷启动只建立宽 prior，不试图一次问全。

推荐形成层级 prior：

$$
\boxed{
PopulationPrior
\rightarrow
EventClassPrior
\rightarrow
Course/TaskSpecificStablePrior
\rightarrow
EpisodePosterior
}
$$

大学生课程具有明显周期性，例如同一课程每周重复，因此少量历史 evidence 可以逐渐形成 course-specific stable prior。

冷启动可收集：

| 信息 | 用途 |
|---|---|
| 学业/课程总体 self-efficacy | $C^{exec}$ broad prior |
| 哪类课程/任务通常更吃力 | event-class competence prior |
| 学业/科研/社交等目标的重要程度 | $I$ broad prior |
| 常见且有效的恢复方式 | $F^{rec}$ prior |
| 活动偏好 | context metadata |

不建议在冷启动中大范围询问：

$$
C^{out},
\quad
U^{perc}
$$

因为二者高度 episode-specific。

---

## 九、Stable Profile 与 Episode Appraisal

Stable Profile 只提供 prior：

$$
\boxed{
StableProfile
\rightarrow
EpisodePrior
}
$$

Episode-specific evidence 提供当前 episode 更新：

$$
\boxed{
EpisodePosterior
\propto
StablePrior
\times
EpisodeEvidence
}
$$

不使用固定：

$$
0.7\times Stable+0.3\times Episode
$$

之类缺乏明确统计意义的加权平均。

一次异常 episode 不直接改写 stable profile。

只有重复一致且高质量 evidence 才慢更新：

$$
\boxed{
RepeatedConsistentEvidence
\rightarrow
SlowStableProfileUpdate
}
$$

---

## 十、哪些 Appraisal 适合 Stable Prior

| Appraisal | Stable prior 强度 | 原因 |
|---|---|---|
| $C^{exec}$ | 较强 | 同一课程/任务类型的能力匹配有重复性 |
| $I$ | 较强 | 个人长期目标与价值排序具有一定稳定性 |
| $F^{rec}$ | 较强 | 某些恢复活动对个人的适配性可通过重复经验学习 |
| $C^{out}$ | 弱 | 高度 episode-specific |
| $U^{perc}$ | 弱 | 高度依赖当前事件与当前信息 |

---

## 十一、Appraisal Evidence 的来源

推荐证据优先级：

$$
\boxed{
ExplicitEpisodeReport
>
ExplicitNaturalLanguageEvidence
>
StablePersonalProfile
>
Population/EventClassPrior
}
$$

### 1. Explicit Episode Report

例如系统询问：

> “这个任务你现在觉得还处理得来吗？”

用户直接回答，形成高质量 episode appraisal evidence。

### 2. Explicit Natural-Language Evidence

例如：

> “这门课我平时都跟得上，但今天完全听不懂。”

支持：

$$
C_{i,e}^{exec}\downarrow
$$

例如：

> “这个结果其实对我没那么重要。”

支持：

$$
I_{i,e}\downarrow
$$

例如：

> “我已经把能做的都做了，结果不由我。”

支持：

$$
C_{i,e}^{out}\downarrow
$$

---

## 十二、LLM 在 Appraisal 层的职责

LLM 只负责：

$$
\boxed{
Text
\rightarrow
StructuredAppraisalEvidence
}
$$

而不是：

$$
\boxed{
Context
\rightarrow
PsychologicalTruth
}
$$

推荐结构化输出：

```text
participant_id
event_id
appraisal_dimension
evidence_value
evidence_strength
evidence_span
source_type
observed_at
known_at
confidence
coder_version
```

不能根据课程类型、deadline 等背景直接输出：

```text
importance = 0.91
outcome_control = 0.43
```

---

## 十三、Evidence Strength

每条 appraisal evidence 记录：

$$
\boxed{
Strength
\in
\{
Strong,\ Moderate,\ Weak
\}
}
$$

推荐规则：

| Evidence | Strength |
|---|---|
| 用户直接回答 appraisal probe | Strong |
| 用户自发且明确陈述 | Strong |
| 明确但间接的语言证据 | Moderate |
| 模糊暗示 | Weak |
| 单纯 event metadata | 不作为直接 appraisal evidence |

Evidence strength 只控制 belief update 强度，不改变 appraisal 变量的定义。

---

## 十四、不能直接生成 Appraisal 的信息

以下信息不得直接确定 appraisal：

### Deadline 很近

说明：

$$
U_{ddl}\uparrow
$$

不能直接推出：

$$
U^{perc}\uparrow
$$

### 学分高 / 专业必修

可以形成：

$$
I
$$

的 event-class prior，但不能直接：

$$
I=High
$$

### 完成率低

属于 task/lifecycle evidence，不能自动：

$$
C^{exec}=Low
$$

### EMA 高

绝对不能：

$$
S\uparrow
\Rightarrow
I\uparrow
$$

或：

$$
S\uparrow
\Rightarrow
C^{out}\downarrow
$$

因此：

$$
\boxed{
EMA
\nrightarrow
AppraisalLabel
}
$$

---

## 十五、Clarification / Sensing 的触发

不对每个事件逐项询问 appraisal。

只在满足：

$$
\boxed{
HighImpact
\land
HighUncertainty
\land
DecisionRelevant
\land
Available
}
$$

时发起 clarification。

可以理解为近似：

$$
\boxed{
VOI
\propto
EventImpact
\times
CurrentUncertainty
\times
DecisionRelevance
}
$$

并保持：

$$
\boxed{
MechanismInactive
\Rightarrow
DoNotProbeItsAppraisal
}
$$

例如某事件没有 Pressure mechanism，则没有必要主动询问其 outcome control。

---

## 十六、统一 Appraisal 调制算子

需要一个有界、单调、能上下调整、同时允许 $x=1$ 被下调的算子。

定义：

$$
\boxed{
\mathcal T_{\epsilon}(x,m)
=
\begin{cases}
0,&x=0\\[2mm]
\sigma
\left[
\operatorname{logit}
\left(
\operatorname{clip}(x,\epsilon,1-\epsilon)
\right)
+\log m
\right],
&x>0
\end{cases}
}
$$

其中：

$$
\sigma(z)=\frac{1}{1+e^{-z}}
$$

$$
0<\epsilon\ll1
$$

$$
m>0
$$

该算子满足：

- 机制不存在时 $x=0$ 仍严格输出 0；
- 非零输入保持在 $(0,1)$；
- $m>1$ 时增强；
- $m<1$ 时减弱；
- 避免普通 odds-shift 在 $x=1$ 时无法下调的问题。

---

## 十七、统一 Appraisal Modulation Strength

定义：

$$
\boxed{
\rho_{app}>1
}
$$

v1 只保留一个 population-level representation hyperparameter：

$$
\boxed{
\rho_D
=
\rho_I
=
\rho_C
=
\rho_U
=
\rho_R
=
\rho_{app}
}
$$

避免在数据稀疏条件下分别估计多组 appraisal effect strength。

$\rho_{app}$ 不作为 person-specific parameter，也不通过 EMA 自由拟合。

由：

```text
Scenario Annotation
→ Synthetic
→ Pilot Sensitivity
→ Freeze
```

确定。

---

## 十八、Effective Demand

事件层已经提供：

$$
D_e^{pot}\in[0,1]
$$

与实际 execution exposure：

$$
Z_{i,e}^{exec}\in[0,1]
$$

定义基础执行 Demand：

$$
d_{i,e}^{0}
=
Z_{i,e}^{exec}D_e^{pot}
$$

Personal Appraisal 只使用：

$$
C_{i,e}^{exec}
$$

最终：

$$
\boxed{
D_{i,e}^{eff}
=
\mathcal T_{\epsilon}
\left(
Z_{i,e}^{exec}D_e^{pot},
\rho_{app}^{-\ell(C_{i,e}^{exec})}
\right)
}
$$

因此：

$$
C^{exec}\uparrow
\Rightarrow
D^{eff}\downarrow
$$

但高 competence 不会令真实执行 Demand 自动归零。

---

## 十九、Pressure 的机制结构

Pressure 保留三个 event-level mechanisms：

$$
\boxed{
Deadline/Scarcity
}
$$

$$
\boxed{
StructuralUncertainty
}
$$

$$
\boxed{
SocialEvaluation
}
$$

分别由事件层提供：

$$
U_{ddl,e},
\quad
U_{context,e},
\quad
D_{s,e}
$$

Personal Importance $I$ 不在每个 subchannel 中重复作用，而只在三类 Pressure 聚合后作用一次。

---

## 二十、Deadline Pressure

定义：

$$
\boxed{
p_{ddl,i,e}^{*}
=
\mathcal T_{\epsilon}
\left(
Z_{e}^{ddl}U_{ddl,e},
\rho_{app}^{-\ell(C_{i,e}^{out})}
\right)
}
$$

其中：

- $Z_e^{ddl}$：deadline mechanism lifecycle gate；
- $U_{ddl,e}$：time scarcity structure；
- $C_{i,e}^{out}$：participant perceived outcome control。

因此：

$$
C^{out}\uparrow
\Rightarrow
p_{ddl}^{*}\downarrow
$$

Deadline proximity、remaining effort 与 available capacity 已经通过 $U_{ddl}$ 表达，不再额外添加 `perceived urgency`。

---

## 二十一、Uncertainty Pressure

事件层提供：

$$
U_{context,e}
$$

表示 structural uncertainty。

Personal Appraisal 提供：

$$
U_{i,e}^{perc}
$$

与：

$$
C_{i,e}^{out}
$$

定义：

$$
\boxed{
p_{unc,i,e}^{*}
=
\mathcal T_{\epsilon}
\left(
Z_e^{unc}U_{context,e},
\rho_{app}^{
[\ell(U_{i,e}^{perc})-\ell(C_{i,e}^{out})]/2
}
\right)
}
$$

除以 2 用于避免两个 appraisal 在同一 subchannel 中简单叠加成过强调制，同时保持统一 $\rho_{app}$。

必须保持：

$$
\boxed{
U_{context}
\not\Rightarrow
U^{perc}
}
$$

两者共享主题但属于不同层级。

---

## 二十二、Social-Evaluative Pressure

事件层提供：

$$
D_{s,e}
$$

第一版不再额外加入 outcome control。

定义：

$$
\boxed{
p_{soc,i,e}^{*}
=
Z_e^{soc}D_{s,e}
}
$$

其作用表示 event 中实际暴露的 social-evaluative structure。

---

## 二十三、Pressure Structure 聚合

先对三个 mechanism 做 noisy-OR：

$$
\boxed{
P_{i,e}^{struct}
=
1-
(1-p_{ddl,i,e}^{*})
(1-p_{unc,i,e}^{*})
(1-p_{soc,i,e}^{*})
}
$$

因此：

$$
0\le P_{i,e}^{struct}\le1
$$

三个 mechanism 可以同时贡献，但不会线性无限叠加。

---

## 二十四、Personal Importance 只作用一次

Personal Importance 表示：

> 整个事件及其后果对该用户有多重要。

因此在三个 Pressure mechanisms 聚合后统一作用：

$$
\boxed{
P_{i,e}^{eff}
=
\mathcal T_{\epsilon}
\left(
P_{i,e}^{struct},
\rho_{app}^{\ell(I_{i,e})}
\right)
}
$$

这样避免：

$$
I
$$

在 deadline、uncertainty、social-evaluation 三个子通道中重复计算。

---

## 二十五、Effective Recovery

事件层提供：

$$
R_e^{pot}\in[0,1]
$$

actual occurrence：

$$
Z_e^{occ}\in[0,1]
$$

context compatibility：

$$
M_e^{context}\in[0,1]
$$

定义基础 recovery opportunity：

$$
r_{i,e}^{0}
=
Z_e^{occ}R_e^{pot}M_e^{context}
$$

Personal Recovery Fit：

$$
F_{i,e}^{rec}
$$

最终：

$$
\boxed{
R_{i,e}^{eff}
=
\mathcal T_{\epsilon}
\left(
Z_e^{occ}
R_e^{pot}
M_e^{context},
\rho_{app}^{\ell(F_{i,e}^{rec})}
\right)
}
$$

必须保持：

$$
\boxed{
RecoveryAffordance
\rightarrow
R^{pot}
}
$$

$$
\boxed{
PersonActivityFit
\rightarrow
F^{rec}
}
$$

避免将同一 recovery semantics 重复编码。

---

## 二十六、Recovery Fit 的长期学习

$F^{rec}$ 特别适合通过 repeated retrospective evidence 慢更新。

例如：

> “跑完之后确实轻松很多。”

可增加 running 的 recovery-fit evidence。

例如：

> “社交完反而更累。”

可降低该活动类别的 recovery-fit belief。

但新证据只能从其 `known_at` 之后用于未来 inference，不回填此前的 prospective forecast。

---

## 二十七、Unknown Appraisal 的运行时处理

大量 appraisal Unknown 时，不做硬插补。

在线计算可以对 categorical belief 做 exact discrete marginalization。

例如 Demand：

$$
\boxed{
E[D_{i,e}^{eff}]
=
\sum_c
P(C_{i,e}^{exec}=c)
D_{i,e}^{eff}(c)
}
$$

Pressure 中：

$$
(I,C^{out},U^{perc})
$$

最多有：

$$
3^3=27
$$

种组合，可以直接枚举。

Future scenario forecast 中，则可以从 belief distribution 抽取一组完整 appraisal state，并在同一 episode/scenario 内保持一致。

---

## 二十八、模块最终输出

本模块最终输出：

$$
\boxed{
D_{i,e}^{eff}
}
$$

$$
\boxed{
P_{i,e}^{eff}
}
$$

$$
\boxed{
R_{i,e}^{eff}
}
$$

以及：

- appraisal belief distributions；
- appraisal evidence provenance；
- appraisal uncertainty；
- stable-profile posterior；
- episode-specific posterior。

下一层只使用这些 effective inputs，不再重新解释 appraisal。

---

## 二十九、当前冻结边界

1. v1 Appraisal 只保留 $C^{exec},I,C^{out},U^{perc},F^{rec}$。
2. Stress Relevance 不进入 dynamics。
3. Interest 只作为 metadata / candidate moderator。
4. Unknown 是 belief distribution，不等于 Medium。
5. Cold Start 只建立宽 prior。
6. 大学生周期性课程用于形成 course-specific stable prior。
7. Stable Profile 只提供 prior，不代表 episode truth。
8. Episode-specific evidence 优先于 stable prior。
9. 重复一致 evidence 才慢更新 stable profile。
10. LLM 只抽取 structured evidence，不直接产生心理真值。
11. EMA 不反向生成 appraisal。
12. $C^{exec}$ 只调节 Demand。
13. $I,C^{out},U^{perc}$ 只调节 Pressure。
14. $F^{rec}$ 只调节 Recovery。
15. Personal Importance 在 Pressure 中只作用一次。
16. Structural Uncertainty 与 Perceived Uncertainty 必须分离。
17. v1 使用统一 $\rho_{app}$，不分别学习多组 appraisal strength。
18. $\rho_{app}$ 属于 representation hyperparameter，不通过 EMA 自由拟合。
19. $D/P/R$ 全部使用 boundary-safe $\mathcal T_\epsilon$。
20. 大量 Unknown 不阻止模型运行。
