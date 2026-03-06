## Master Flow（端到端总流程：一天事件 → S/E 曲线 → 预警 → 图像/画像/日志）

### 输入是什么
- **日期**：Web 由 `POST /api/simulate` 传入 `date`；CLI 由 `process_date(date_str)` 传入。
- **用户状态与策略**：由 `User` 持有（`params` + `course_strategy/rest_strategy/night_strategy` + `current_sleep_debt` + `resilience_index` + `epoc_level`）。
- **日程事件**：来自
  - **本地缓存**：`data/calendar_data/calendar_YYYYMMDD.json`
  - **飞书拉取**：`utils/calendar_tool.get_events_in_date_range(...)`（内部做周期事件“星期对齐校准”）

### 核心流转链条（必须按这个顺序理解）
1) **拉取/读取日程 JSON**
- Web：`app.py::simulate()`
  - 先尝试本地 `data/calendar_data/calendar_YYYYMMDD.json`（除非 `force_refresh=true`）
  - 否则用 `get_user_access_token(interactive=False)` + `FEISHU_CALENDAR_ID` 拉取
- CLI：`solve_stress_model_new.py::process_date()`
  - 同样优先本地缓存，不存在才拉取并写入缓存

2) **“真实事件”前置处理（Web 独有：屏蔽删除）**
- Web 接受 `shield_keywords`：
  - 对每条 `events_json` 的 `summary/name` 做包含匹配
  - 命中就直接从时间线删除，并把删除原因写入 `app_trace_logs`

3) **事件对象化：JSON → 领域事件（EventFactory 路由）**
- `EventFactory.create_from_json(events_json)` 会把每条日程路由成具体事件类：
  - 强提示 `event_type=course/rest` 直接创建对应事件
  - 否则用名称正则分类：`MealEvent / NapEvent / GymEvent / LibraryEvent / TaskEvent(T1~T5) / CourseEvent / TaskEvent(general兜底)`
- 这一步是后续“不同事件走不同 ΔS/ΔE 逻辑”的根入口

4) **沙盒注入（Web 独有：mock_events）**
- Web 接受 `mock_events`（前端“深度生态事件注入”面板）：
  - 先计算真实日程的占用块 `occupied_blocks`
  - 逐个 mock 做**重叠拒绝**（与真实块有交集就拒绝，并写日志）
  - 不重叠才注入为 `CourseEvent/TaskEvent/MealEvent/NapEvent/RestEvent`

5) **影子事件编织（RoutineWeaver：睡眠 + 三餐 + 午睡 + 睡眠债）**
- Web/CLI 都会调用：`inject_routine_events(events, date_str, user)`
- 本质是 `RoutineWeaver.inject_routine_events(...)`：
  - 扫描“高负荷占用块”（`course/task/gym/library`）
  - 自动生成 **SleepEvent**（晨间/夜间睡眠段）
  - 自动寻找午餐/晚餐/午睡的最佳缝隙并注入
  - 计算目标睡眠与实际睡眠差，写入 `user.current_sleep_debt`（睡眠债）

6) **进入核心仿真：StressSolver 离散推进一天（time_step=5min）**
- `StressSolver.simulate_day(final_events, init_S, init_E, date_str)` 输出：
  - `results`：每步 `{time,S,E,state,f_pen}`
  - `alerts, confidence_series`：预警与置信度曲线（AlertMonitor）
  - `trace_logs`：状态机诊断日志
  - `profile_list`：事件画像（每个事件 ΣΔS/ΣΔE、惩罚分解等）
  - `wake_s`：清晨首次进入 `t_wake` 时刻截取的 S 值（供双轨演化锚点）

7) **日级结算：生态演化 + 持久化**
- Web/CLI：用当日 `results` 算日均压力 `daily_mean_s`，并从 Solver 取出 `wake_s`（清晨唤醒时压力）与 `has_red_alert`（是否触发红警），调用 `User.evolve_daily_baseline(wake_s, daily_mean_s, has_red_alert)`
  - **双轨演化**：S\* 基于 `wake_s` 漂移；Threshold 基于 `daily_mean_s` 与 `has_red_alert` 做恶性磨损/良性锻炼/舒适区退化
- 保存日终：`data/stress_records.json`（Web 记录 `end_S/end_E/S_star/S_threshold`）

8) **可视化与回传（Web）**
- `stress_model.get_plot_image_base64(results, confidence_series, alerts, params, S_star, events)`
  - 画 `S(t)`、`E(t)`、阈值线、事件色块与标签、连轴惩罚 `f_pen` 阶梯填充、置信度曲线
- Web 返回 JSON：`image/end_S/end_E/new_S_star/new_threshold/alerts/trace_logs/event_profile/...`

---

## App Web 交互与设计（`app.py` + `templates/index.html`）

### 输入是什么
- `POST /api/simulate` 的 payload（前端自动拼装）：
  - `date, init_S, init_E`
  - 上帝之手：`K_resilience, fatigue_accel, Z_factor`
  - 沙盒：`mock_events`（course/task/rest 子类），`shield_keywords`
  - `force_refresh`（可选）

### 关键判断/分支条件
- **上帝之手立即生效**：若 payload 包含 `K_resilience/fatigue_accel/Z_factor`，会转成 `god_params` 并 `current_user.update_params(god_params)`
  - 注意映射：`fatigue_accel` 写入参数名是 `fatigue_acceleration`
- **缓存优先**：本地 calendar 文件存在且未强刷 → 直接用缓存；否则飞书拉取
- **屏蔽删除**：`shield_keywords` 非空 → 先过滤 `events_json`
- **mock 注入**：与真实日程重叠则拒绝（只防真实，不防 mock 之间的重叠，前端已先做一次 mock 内部防重叠）
- **影子编织**：最终事件列表再进入 `inject_routine_events(...)`，补齐 sleep/meal/nap

### 状态变量如何更新
- `current_user.params`：被 `/api/config`、上帝之手、以及日均压力结算后的 `evolve_daily_baseline` 改写
- `data/stress_records.json`：写入当日终态与演化后的 `S_star/S_threshold`

### 输出被谁消费
- 返回 JSON 由前端消费：
  - `image` → 主图显示
  - `end_S/end_E/new_S_star/new_threshold` → 统计卡片
  - `alerts` → 预警卡片
  - `event_profile` → “全天事件画像”表格（基础压力、惩罚压力、累计贡献）
  - `trace_logs` → 底部 Console（状态机诊断）

---

## 事件系统（Event Layer：事件是什么、怎么被路由、怎么被 Solver 消费）

### 事件统一接口（`event/base.py::BaseEvent`）
- 必备字段：`event_id/start_time/end_time/name/description/metadata`
- 必备方法：
  - `get_event_type()`：返回 `"course/task/gym/library/meal/nap/sleep/rest"...`
  - `calculate_stress_impact_dual(user,S,E,current_time,time_step) -> (ΔS, ΔE)`
- 可选：`get_fatigue_weight()`（影响“连续负荷时长”的折算）

### EventFactory 路由规则（`utils/event_factory.py`）
输入：飞书事件字典（主要用 `summary/description/start_time/end_time`）
输出：具体事件类实例
- **强提示优先**：`event_type == course/rest`
- **正则智能路由**（基于 name/summary）：
  - 饭/餐/食堂 → `MealEvent`
  - 午休/nap/sleep → `NapEvent`
  - 健身/gym → `GymEvent`
  - 自习/图书馆 → `LibraryEvent`
  - 高压任务（TaskEvent 五级梯队）：
    - T1：考试/答辩/竞赛/面试… → `task_type="exam"`
    - T2：DDL/截止/提交/大作业… → `task_type="ddl"`
    - T3：会议/讨论/讲座… → `task_type="meeting"`
    - T4：作业/项目/编程… → `task_type="homework"`
    - 兜底：`task_type="general"`
  - 传统授课：名称在 `CLASS_INFO_DICT` 或包含“课” → `CourseEvent`

### Solver 的消费方式（核心差异点）
- Solver 不按“事件列表顺序”跑，而是每个 5 分钟：
  - 找**高负荷事件**集合：`course/task/gym/library`（`_get_active_high_load_events`）
  - 找**例程事件**（routine）单个：`meal/nap/sleep/rest`（命中一个就优先走它）
  - 两者都没有时：按状态机走“普通空档休息策略 / 夜间策略 / 凌晨空转”

---

## Task 模块（任务：`TaskEvent`，与 Course 的根本不同）

### 输入是什么
- 事件自身：`task_type`（exam/ddl/meeting/homework/general）
- 用户状态：`S_star_init, K_resilience, fatigue_acceleration, random_seed, Z_awake, Z_factor`
- 用户慢变量：`user.get_sleep_debt()`（睡眠债，小时）
- 当前时刻：用于
  - **昼夜节律惩罚**（00:00–06:00）
  - **确定性噪声**（用时间哈希 + event_id 生成可复现波动）

### 关键判断/分支条件
- **任务强度映射**：从 `user.params["task_base_intensities"]` 读（不存在就用默认字典）
- **睡眠债惩罚**：
  - 精力消耗倍率 `f_debt_drain = 1 + 0.05*debt`
  - 压力生成倍率 `f_debt_stress = 1 + 0.04*debt`
- **昼夜节律惩罚**：
  - 凌晨任务：耗能 *1.4，增压 *1.2
- **软截断**：
  - 用 `max_delta = course_strategy.get_strategy_max_delta()` 做 tanh 软饱和：避免单步爆炸

### 状态变量如何更新（ΔS/ΔE 结构）
- **ΔE（掉电）**：基础掉电 + 疲劳加速（低电更快掉）+ 策略耗能修饰 + 睡眠债 + 昼夜节律
- **ΔS（增压）**：不走 CIS（没有课程结构强度），而是走 `task_intensity`
  - 但**敏感曲面仍复用课程策略**：`course_strategy.f_s(S,E,S*)`
  - 再乘 `z_log_mapped`（Z 对数映射）与睡眠债/节律惩罚

### Course 与 Task 的隔离（根本差异总结）
- **Course**：压力核心由 `CIS(课程结构强度)` 驱动（学分/学时/level/文本评分/时间偏好）
- **Task**：压力核心由 `task_intensity(任务类型强度)` 驱动（T1–T5）
- 两者共同点：都复用 `course_strategy.f_s(S,E,S*)` 作为“人在当前压力/精力下的敏感面”，并受“连续负荷惩罚”叠加影响

---

## Library 模块（图书馆自习：`LibraryEvent`）

### 输入是什么
- 事件自身：`study_intensity`（0.1~1.0，沙盒注入用给定值；真实日程基于 `total_duration_mins` 做**时长衰减**：`0.95 - hours*0.12`，不低于 0.4）
- 用户：`user.get_resilience_index()`（特质韧性指数，由四策略打分得出）、`K_resilience`、`sleep_debt`、`random_seed`
- 当前状态：`S, E, S_star`

### 关键判断/分支条件
- **沙盒 vs 真实**：`event_id` 含 `"mock"` 则用 `provided_intensity`；否则用时长衰减公式
- **心流动力学**：`resilience > 0.2` 为坚韧、`< -0.2` 为焦虑、否则中性
  - 高压时：坚韧者通过 `flow_relief = 0.008 * resilience * diff * intensity` 获得掌控感（降压）；焦虑者无 relief 甚至更易破防
- **睡眠债**：若 `raw_delta_S > 0` 且 `sleep_debt > 0`，增压再乘 `(1 + 0.03*sleep_debt)`；耗能再乘 `(1 + 0.04*sleep_debt)`
- **软截断**：`delta_S = max_s_step * tanh(raw_delta_S / max_s_step)` + 确定性噪声

### 状态变量如何更新（ΔS/ΔE 结构）
- **ΔS**：`base_stress_increase = 0.15*intensity` 减去 `flow_relief`，再乘睡眠债修正，加噪声
- **ΔE**：`drain_rate = (4.0*intensity)/K_resilience`，再乘睡眠债修正，微噪声
- **metadata**：写入 `detail`（专注度+特质）、`weight_factor`（用于画像）

### get_fatigue_weight（连续负荷折算）
- 返回 `0.4 + 0.4*intensity`（约 0.44~0.8）
- 与 Course/Task 不同：Library 对蓄水池的“进水”贡献较小，介于高负荷与纯休息之间

### 输出被谁消费
- Solver 将其视为**高负荷事件**，走 `_calc_high_load_impact`，参与 event_profile 画像与 trace 日志
- 绘图 `color_map` 中 `library` 对应紫蓝色 `#8A2BE2` 与标签“自习”

---

## Gym 模块（体育运动：`GymEvent`）

### 输入是什么
- 事件自身：`intensity`（0.1~1.0）
- 用户：`S_star`、`K_resilience`、`random_seed`
- 当前状态：`S, E`

### 关键判断/分支条件
- **压力 relief**：`stress_gap = max(0, S - S*)`，`delta_S = -relief_factor * stress_gap * (step/5) + noise`（微弱降压，高强时噪声更大）
- **防穿透**：若 `S + delta_S < S* - 5`，则截断到 `S* - 5`，避免一次运动压得过低
- **EPOC 注入**：每步向 `user.epoc_level` 累加 `(1.5 + 2.0*intensity)*(step/5)`，上限 100
  - 该 Buff 在**后续休息期**被 Solver 逐帧消费，转化为额外的 ΔE 回血与 ΔS 降压（见下节 EPOC 机制）

### 状态变量如何更新（ΔS/ΔE 结构）
- **ΔS**：负向（降压），强度与 `stress_gap` 和 `intensity` 正相关
- **ΔE**：负向（耗能），`drain_rate = 8.0*intensity/K_resilience`，模拟身体消耗
- **metadata**：`weight_factor` 记录 `get_fatigue_weight()`，`detail` 记录强度

### get_fatigue_weight（连轴冷却器）
- 返回 **负值**：`-2.0 * (1.0 + intensity)`（约 -2.2 ~ -4.0）
- **物理意义**：运动时 `continuous_load_hours` 会**减少**（`max(0, ... + step_hours * max_weight)`），相当于“强力排水”
- 运动后休息时，EPOC 被消费，带来额外恢复增益；坚韧者（高 `resilience_index`）吸收 EPOC 效果更好

### 输出被谁消费
- Solver 将其视为高负荷事件，参与 `_calc_high_load_impact` 与 event_profile
- 绘图 `color_map` 中 `gym` 对应深橙色 `#FF8C00` 与标签“运动”

---

## Course 模块（课程：`CourseEvent`）

### 输入是什么
- 事件自身：`credit/hours/level/course_name/description`
- 课程信息字典：`CLASS_INFO_DICT`（若命中课程名则补全 credit/hours）
- 用户参数：`w1,w2,w3,time_weights`
- NLP 评分：`score_description(description)`（SnowNLP + 规则熔断）
- 睡眠债/昼夜节律（与 Task 同一套倍率）
- 策略：`course_strategy.f_s`、`get_energy_drain_modifier`、`get_strategy_max_delta`

### CIS 如何组成（课程结构强度）
- **基础结构项**（对数防爆）：由 `credit/hours/level` 与 `w1,w2,w3` 组合得到 `base`
- **文本喜好项**：对 `description` 打分 `score∈[1,10]` → 线性映射
  - `F_like = 1.25 - 0.05*score`
- **时间偏好项**：用 `time_weights` 区间匹配当前开始小时
  - 早晨/下午/夜晚偏好由 `CourseStrategy._setup_time_strategy()`写回到 `params["time_weights"]`
- 最终 `CIS = base * F_like * F_time`

### 课程对 E 的消耗机制（掉电）
- 线性耗能：`(base_drain_rate + CIS) / K_resilience`
- 疲劳加速：`E` 越低，`(1-E/100)^{1.5}` 越大，掉电越快
- 策略耗能修饰：`course_strategy.get_energy_drain_modifier(E)`
- 睡眠债 + 昼夜节律倍率叠加（与 Task 一致）

### 课程对 S 的生成机制（增压）
- 敏感曲面：`f_s = course_strategy.f_s(S,E,S*)`（含护盾/噪声/限幅）
- Z 对数映射：`z_log_mapped = 0.8 + 0.4*log2(1+Z_awake*Z_factor)`
- 压力主项：`D_t * CIS * f_s * z_log_mapped` 再乘睡眠债/节律倍率
- **软截断**：用 `max_delta` 做 `tanh` 软饱和，避免尖刺

---

## 各种 Strategy（策略矩阵：课程敏感面 + 连轴惩罚 + 日间休息 + 夜间睡眠）

### 1）课程敏感函数曲面（`strategy/course_strategy.py`）

#### Sensitive（高敏破防，S 型曲线）
- **输入**：`diff = S - S*`、`E`
- **形态**：
  - `diff<=0`：轻微线性上升（“低压也会被推动”）
  - `diff>0`：Logistic/Sigmoid 破防曲线（到 midpoint 后陡峭）
- **精力护盾（反向护盾）**：`E` 越低 → 乘数越大；`E` 越高 → 乘数越小
- **限幅**：整体上限更低（强调“破防但仍受生理极限”）
- **噪声**：`_add_noise`（缩放噪声 + 基噪声），保证可复现但有扰动

#### Dull（迟钝耐压）
- **阈值区**：`diff<threshold` 近似常数，超过后线性增加
- **护盾**：有 safe_zone，超过后按比例压制
- **上限**：较低（更稳定）

#### Saturated（饱和）
- 以 `S*+20` 为中心的 Sigmoid：压力越高，增幅越小（进入饱和区）
- 护盾与噪声同上

#### BatteryDrain（电池耗竭曲面）
- **风险面**：`risk = (S-S*) - (0.5*E+5)`
  - 同样压力下，`E` 越低风险越高
- Logistic 输出 + 硬上限：
  - `E<20` 上限更高（残血更容易崩）

### 2）连续上课惩罚（C_strategy：`High/Low/Threshold`）
- `get_threshold()`：超过多少小时开始惩罚
- `get_recovery_rate()`：空档排水速度
- `calculate_fatigue_penalty(acc_hours, S*)`：把“连续负荷小时数”转成额外 ΔS（作为 `f_pen`）

### 3）日间休息策略（`strategy/rest_strategy.py`）
- Solver 在“无高负荷、无例程事件、白天”时调用：
  - `rest_strategy.calculate_flow_recovery(S,E,duration,time_step,S*)`
- **基类共性**：AR(1) 连续噪声（`_get_ar1_noise`，可配置 `rest_noise_rho`）替代白噪声；`_calculate_dynamics(S, S_star, duration, time_step)` 由子类实现；深层转化期有 **e_bonus**（E<30→0.9，E≤70→1.0，E>70 随 E 微增上限 1.15）；近稳态（diff≤2）时噪声缩放；防穿透截断到 S\*-5。
- **策略映射**：`relieved`/`balanced`→RelievedRestStrategy，`warmup`/`resilient`→WarmupRestStrategy，`anxious`→AnxiousRestStrategy，`burnout`→BurnoutRestStrategy。
- **四策略要点**：
  - **Relieved（释然型）**：阈值 (2,5)，效率 0.55；时间衰减 + 压差幂律，低压区平滑减速。
  - **Warmup（慢热型）**：阈值 (5,15)，效率 0.65；时间与压差解耦，`time_ratio**2.5` 慢热。
  - **Anxious（焦虑型）**：阈值 (10,15)，惯性耗能 -0.08，效率 0.35；约 25min 内难放松（sigmoid 松弛），死水区扣减 effective_diff。
  - **Burnout（倦怠型）**：阈值 (5,10)，效率 0.25；对数死水缓降；duration>60 有长时间精力耗散惩罚。

### 4）夜间睡眠策略（`strategy/night_strategy.py`）
- 统一入口：`night_strategy.calculate_step(S,E,current_time,time_step,elapsed_minutes)`
- 共性机制：
  - **非对称振幅**：`S>S*` 振幅随差距增长；`S<S*` 振幅收敛
  - **节律因子**：周期被动态拉伸 + 随机微扰
  - **AR(1) 噪声**：连续相关噪声，避免“白噪声抖动”
  - **精力恢复饱和**：`E` 越接近 100，回血越慢；高压时回血也会被指数衰减
- 三策略差异：
  - Normal：平稳、两阶段（初期更像指数回落，后期加振荡）
  - Deep：前段更猛（更快降压、更快回血）
  - Anxious：引入 resistance（焦虑阻力）+ 更高的“波形连续性修复”与 `E` 上限约束（不让焦虑型睡眠回血超过 96）

---

## 影子事件编织逻辑（`utils/routine_weaver.py`：睡眠 + 三餐 + 午睡 + 睡眠债）

### 输入是什么
- 当天“高负荷占用块”：只看 `course/task/gym/library`
- 用户默认作息参数：`default_wake_time/default_sleep_time`（未配则内部默认 07:30/23:30）
- 理想睡眠：`ideal_sleep_hours = 8.0`

### 关键判断/分支条件
- **占用块合并**：把重叠区间合并成不交叠块
- **晨间睡眠注入（0:00 → real_wake_min）**
  - 先推断 `real_wake_min`：
    - 若存在“熬夜占用”延伸到默认起床前：根据“熬夜结束”推迟起床，但受 `max_delay_wake_min(11:00)` 与“下一事件提前 30min”约束
  - 在晨间空档里生成 SleepEvent，但跳过“熬夜前摇”：
    - 第一段 gap 从 0 开始且在 02:30 前就结束的短空档，不算入睡（避免把熬夜拖延误当睡眠）
  - 非零开始的睡眠段会 **+15min 褪黑素缓冲**（紧接任务不能立刻睡着）
- **睡眠债计算**
  - `target_sleep = ideal_sleep + late_hours*0.25`
  - `sleep_debt = max(0, target - actual_sleep)` 写入 `user.current_sleep_debt`
- **夜间睡眠注入（default_sleep_min → 23:59）**
  - 在夜间空档追加 SleepEvent（同样有 +15min 缓冲）
- **午餐/晚餐/午睡寻找（贪婪+评分）**
  - 在边界内找最接近理想起止的 gap，且长度≥min_dur
  - 午睡时长随睡眠债变化：
    - 债大：理想午睡更长、min_dur 更大，并设置 `metadata.is_repaying_debt`
    - NapEvent 内部会以 **2x 效率**偿还睡眠债，并随“已入睡 elapsed”做前段加成/后段反噬

---

## 连续上课惩罚机制（Reservoir：Solver 内的 `continuous_load_hours`）

### 输入是什么
- 当前步是否处于高负荷事件（course/task/gym/library）
- 每个高负荷事件的 `get_fatigue_weight()`：
  - Course：1.0
  - Task：按类型映射（exam 1.1，general 0.85 等）
  - **Gym**：`-2.0 * (1 + intensity)`（负值，运动时蓄水池**排水**）
  - **Library**：`0.4 + 0.4 * intensity`（约 0.44~0.8，进水较弱）

### 关键判断/分支条件
- **进水/排水**（高负荷时）：
  - 每步变化 `step_hours * max_weight`（同一时刻多个事件取**最大**权重）
  - 正值（course/task/library）→ 进水；**负值（gym）** → 排水（`max(0, ...)` 防止蓄水池为负）
- **排水**（白天且无高负荷）：
  - gap ≥ `gap_tolerance_mins(5)` 才开始排水
  - 每步按 `course_strategy.get_penalty_recovery_rate()` 折算减少
- **清零**：
  - 进入 `RECOVERY_SLEEP/NIGHT_SLEEP/ROUTINE_MAINTENANCE` 直接清零（睡眠/午睡/就餐会断开连续负荷）

### 惩罚如何叠加、如何被记录
- 每个高负荷步，Solver 计算：
  - `fatigue_penalty = course_strategy.calculate_fatigue(continuous_load_hours)`
  - 把它加到基础 `total_ds_base` 上形成最终 ΔS
- 同时把 `fatigue_penalty` 写入：
  - `results[].f_pen`（绘图用阶梯填充）
  - `event_profile[].penalty_s`（画像表格“连轴疲劳惩罚”列）

---

## 日间休息的分化（普通空档 vs 吃饭 vs 午睡：完全不同的流转）

### 1）普通空档休息（RestStrategy + RestSession）
- 触发条件：当前步
  - 没有高负荷事件（`course/task/gym/library`）
  - 没有 `meal/nap/sleep/rest` 这类 routine 事件覆盖
  - 且不在夜间睡眠段
- 流转方式：
  - `RestSession.tick(time_step)` 维护连续空档分钟数
  - `rest_strategy.calculate_flow_recovery(S,E,duration,...)` 以 duration 作为分段状态机输入

### 2）吃饭（MealEvent）
- 触发条件：RoutineWeaver 注入的午餐/晚餐，或 Web mock 注入的 meal
- 流转方式：
  - 直接按 `meal_type` 输出固定结构的 ΔS/ΔE（带确定性噪声）
  - 不走 RestStrategy，也不走 NightStrategy
- 系统意义：
  - 它既是恢复事件，也是“断连轴”的硬切换点（Solver 会把连续负荷清零）

### 3）午睡（NapEvent）
- 触发条件：RoutineWeaver 在午餐后寻找缝隙注入，或 Web mock 注入 nap
- 流转方式：
  - 更强的降压与回血
  - 若 `metadata.is_repaying_debt=true`：
    - 每步用 `(time_step/60)*2` 还睡眠债（2x 效率）
    - 前 40min 强化回血/降压，超过 60min 可能出现“越睡越醒”型反噬（ΔS 转正）

### 4）EPOC 后燃 Buff 消费（Solver 内每步拦截）
- **触发条件**：当前状态为休息类（`RECOVERY_SLEEP / NIGHT_SLEEP / ROUTINE_MAINTENANCE`）或 `DAY_ACTIVE` 且无高负荷，且 `user.epoc_level > 0`
- **消费速率**：每步消耗 `min(epoc_level, 1.5*(step/5))`，`user.epoc_level` 递减
- **转化增益**：
  - `epoc_de = consume * (0.6 + 0.2 * res_idx)`（回血）
  - `epoc_ds = -consume * (0.08 + 0.05 * res_idx)`（降压）
  - `res_idx = user.get_resilience_index()`：坚韧者吸收 EPOC 效果更强
- **日志**：当 `epoc_level` 从 >0.01 降至 ≤0.01 时，输出“后燃结束”trace

### 优先级规则（同一时刻谁生效）
- Solver 每步优先判定：
  1) 高负荷事件（course/task/gym/library）存在 → 走高负荷路径
  2) 否则如果 routine_ev 存在（meal/nap/sleep/rest）→ 走 routine 事件自身的 ΔS/ΔE
  3) 否则 → 走普通空档休息或夜间策略
- 若处于休息且 `epoc_level>0`，在步长结算**之后**再叠加 EPOC 转化增益

---

## 夜晚休息判定与处理（睡眠识别、碎片化、起夜惩罚、恢复计算）

### 输入是什么
- 被编织出的 `SleepEvent` 列表（可能有晨间睡眠段、夜间睡眠段）
- 当天高负荷事件结束时间（用于判定“睡眠被打断”）
- `night_strategy`（normal/deep/anxious）

### 如何识别白天/夜间/熬夜/起夜（Solver 状态机）
- `StressSolver._analyze_daily_schedule(events,date)`：
  - 从 `sleep` 事件中推断：
    - `wake_time`：晨间睡眠段最晚结束（否则默认 07:30）
    - `night_sleep_start`：夜间睡眠段最早开始（否则默认 23:30）
  - 从 `course/task/gym` 的结束时间推断：
    - `late_night_active_end`：若某负荷结束时间在 wake_time 前，则作为“夜间负荷活动最晚截止”
- 每步状态由三类信号共同决定：
  - 是否有高负荷
  - 是否被 routine（尤其 sleep）覆盖
  - 当前时间与 `t_wake/t_sleep_2` 的相对关系
- 状态名关键含义：
  - `RECOVERY_SLEEP`：早晨睡眠（起床前的 sleep）
  - `NIGHT_SLEEP`：夜间睡眠（night_sleep_start 之后的 sleep 或无事件时的夜间段）
  - `LATE_NIGHT_ACTIVE`：起床前却在负荷/清醒（熬夜/起夜开机）
  - `NIGHT_OVERTIME`：夜间睡眠段之后仍在负荷（夜间加班）

### 睡眠被打断与碎片化折损（核心细节）
- **起夜开机惩罚**：
  - 若上一状态是 `RECOVERY_SLEEP/NIGHT_SLEEP`，当前状态变成 `LATE_NIGHT_ACTIVE/NIGHT_OVERTIME`
  - 触发一次：`E -5, S +2`，并记次数 `sleep_interruptions += 1`
- **碎片化折损系数 sleep_eff**：
  - 每打断一次，后续睡眠恢复效率下降，最低到 0.5
  - 只在进入睡眠段时记录诊断日志
- **折损如何应用**：
  - 对睡眠产生的恢复量进行折损：
    - 若 `ds<0`（在降压）→ `ds *= sleep_eff`
    - 若 `de>0`（在回血）→ `de *= sleep_eff`
  - 既适用于 `SleepEvent`（例程睡眠），也适用于“无 sleep 事件但进入夜间策略”的夜间恢复

---

## 生态化演进优化（`User.evolve_daily_baseline` 双轨引擎）

### 输入是什么
- `wake_s`：清晨唤醒时刻的压力值（Solver 在首次进入 `current_time >= t_wake` 时截取并写入 trace）
- `daily_mean_stress`：当日 `results` 中所有 S 的均值
- `has_red_alert`：当日是否触发过红警（`"红" in type` 或 `"严重" in type`）
- 用户慢变量：`sleep_debt`（睡眠债，小时）

### 轨线一：S\* 静息底线漂移
- **锚点**：基于 `wake_s`（清晨醒来时的压力）而非日均压力
- **公式**：`new_s_star = old_s_star + alpha_star * (wake_s - old_s_star)`，`alpha_star = 0.015`
- **裁剪**：`new_s_star ∈ [40, 70]`
- **物理意义**：睡了一觉后的压力反映“体质基线”，比日均更贴近静息状态

### 轨线二：Threshold 破防天花板磨损
- **恶性磨损**：若 `has_red_alert` 或 `sleep_debt > 1.5` → `new_threshold -= 0.25`（防线被击穿或高睡眠债反噬）
- **良性锻炼**：若 `daily_mean_stress > old_s_star + 10` 且无红警 → `new_threshold += 0.10`（走出舒适区且安全度过）
- **舒适区退化**：否则 → `new_threshold -= 0.05`（缺乏压力刺激，天花板轻微下降）
- **硬约束**：`new_threshold ∈ [new_s_star + 20, 110]`

### 输出被谁消费
- 更新 `params["S_star_init"]`、`params["S_threshold"]`
- 调用 `save_config()` 持久化
- 下次仿真与预警系统使用新阈值

---

## 用户四策略打分与韧性指数（`User._calculate_resilience_index`）

### 输入是什么
- 用户当前配置：`f_strategy`、`C_strategy`、`night_strategy`、`rest_strategy`
- 在 `_init_strategies()` 及任意策略变更时自动调用

### 打分规则（累加制，范围约 -1.0 ~ 1.0）
| 策略维度 | 选项 | 得分 |
|----------|------|------|
| **f_strategy** | dull | +0.4 |
| | saturated | +0.2 |
| | sensitive | -0.3 |
| | batterydrain | -0.2 |
| **C_strategy** | low | +0.2 |
| | threshold | +0.1 |
| | high | -0.2 |
| **night_strategy** | deep | +0.3 |
| | normal | 0 |
| | anxious | -0.3 |
| **rest_strategy** | warmup | +0.2 |
| | relieved | +0.1 |
| | burnout | -0.1 |
| | anxious | -0.2 |

- 最终 `resilience_index = clamp(score, -1.0, 1.0)`

### 输出被谁消费
- `LibraryEvent`：心流动力学 `flow_relief = 0.008 * resilience * diff * intensity`（坚韧者高压时更易降压）
- Solver EPOC 吸收：`epoc_de = consume * (0.6 + 0.2 * res_idx)`，`epoc_ds = -consume * (0.08 + 0.05 * res_idx)`（坚韧者吸收 EPOC 效果更好）
- `metadata["detail"]`：Library 画像中展示特质标签（坚韧/焦虑/中性）

### 全局配置基座（`config.py::GLOBAL_DEFAULT_CONFIG`）
- User 实例化时以 `GLOBAL_DEFAULT_CONFIG` 为基座深拷贝，再叠加本地配置与显式传参
- 关键参数：`w1,w2,w3`、`time_weights`、`S_star_init`、`S_threshold`、`task_base_intensities`、夜间/休息策略常数等
- 消除对本地 JSON 的强依赖，便于云端 Agent 部署

---

## 监控预警系统（`utils/alert_monitor.py::AlertMonitor`）

### 输入是什么
- `results`：每步包含 `S/E/state/time`，以及（若 Solver 写入）`delta_S`、`continuous_hours`、`current_events`、`dominant_stressors`
- 参数：`S_threshold`、`S_star_init`；**缓冲带** `buffer_zone = max(10, S_thresh - S_star)`，用于动态百分比分区

### 双引擎与积分
- **积分**：`auc_level` 在“压力消耗 80% 缓冲带”（S > S_thresh - 0.2*buffer_zone）时上涨；休息且 ΔS<0 时略降；否则衰减。
- **引擎一（绝对值水位）**：按 S 所在区间定 `intensity_tier`（critical / breached / approaching / safe）与 `intensity_zone`。
- **引擎二（疲劳积分）**：按 `auc_level` 与 E 定 `duration_tier`（auc≥100→3，≥80 或 (≥50 且 E<25)→2，≥50→1）。
- **综合**：`target_tier = max(intensity_tier, duration_tier)`；仅当 `target_tier > current_alert_tier` 时写入一条报警（阶梯只升不降，防刷屏）。

### 静默拦截与报警内容
- **休息静默**：若当前在休息且压力在回落（`delta_S<0`）且绝对值未破防（`intensity_tier < 2`），则仅由疲劳积分触发的报警可被拦截不输出。
- 报警条目前端字段：`type, time, S, E, state, trigger_source`（intensity_spike / duration_buildup）、`intensity_zone`、`continuous_hours`、`current_events`、`dominant_stressors`、`C`（= target_tier/3）。

### 置信度与日终兜底
- `C_t = (auc_level / auc_limit) ** 1.8`
- 日终：若未达红档且日终 S≥S_thresh+0.35*buffer → 补一条红；若 auc>80 且当日未达橙以上 → 补一条橙“高危积压”。

### 输出被谁消费
- Web：预警卡片 + 绘图置信度曲线；`has_red_alert` 由 `"红" in type` 判定，供 `evolve_daily_baseline` 使用。

---

## SnowNLP 情感计算系统（`utils/description_score.py`）

### 输入是什么
- `summary`（事件名称）与 `description`（事件描述）
- SnowNLP 可用性：若未安装 `snownlp`，自动回退为“规则/先验为主”

### 两阶段打分流水线（再加规则熔断）
1) **Summary 先验**（Stage 1）
- 课程偏硬核词 → 先验分更低（更难）
- 偏放松词 → 先验分更高（更轻松）

2) **SnowNLP 情感**（Stage 2）
- `sentiments∈[0,1]` 映射到 `1~10`

3) **融合**（Stage 1/2 权重）
- 有 description：`raw = 0.3*summary_prior + 0.7*snownlp`
- 无 description：只用 summary_prior（否则默认中性 5）

4) **规则熔断 + 正负词 + 三梯度任务词（Tier1~3）**
- 命中高压关键词时，会把分数按“积极压力(Eustress)/恶性压力(Distress)”双轨修正，并设置硬上限 hard_cap
- 最终 clamp 到 `[1,10]`

### 当前代码里它实际影响了什么
- `CourseEvent._compute_cis`：只传 `description`（不传 summary），所以主要靠 SnowNLP+规则在 description 上生效
- `stress_model.compute_cis`（若被调用）：会传 `summary=course_name`，则 Summary 先验也会参与

---

## 可视化（`stress_model.py` 绘图：S/E/事件块/惩罚/置信度）

### 输入是什么
- `results`：每步 `time,S,E,state,f_pen`
- `confidence_series`：预警置信度曲线
- `alerts`：预警点
- `events`：最终事件列表（含影子睡眠/三餐/午睡/以及任务/健身/自习等）

### 事件区块与标签如何生成
- 对每个事件绘制时间跨度的半透明区块（`axvspan`），`sleep` 类型透明度更高（0.3）
- 标签名称提取有多层回退：
  - `ev.name` → `ev.course_name` → `ev.metadata.summary/name` → 最后兜底
- **颜色映射**（`stress_model._draw_core_plot` 的 `color_map`）：
  - `course`→皇家蓝/课程、`task`→猩红/任务、`sleep`→午夜蓝/睡眠、`nap`→浅海绿/午休
  - `meal`→中海绿/就餐、`rest`→暗卡其/休息、`gym`→深橙/运动、`library`→蓝紫/自习
  - 未匹配类型走默认灰并标成“其他”

### 连轴转惩罚为什么用“阶梯/断崖”
- `results[].f_pen` 被缩放后用 `fill_between(..., step="post")`
- 意义：惩罚在“连续负荷达到阈值”的某个时刻触发，并在解除时刻瞬间消失，阶梯能避免视觉误读为“渐变缓降”

### 置信度叠加展示
- 压力图上用第二 y 轴画 `confidence_series` 虚线 + 淡填充
- 让“压力值”和“报警热度”同时可读

---

## 当前代码的扩展点清单（只列真实存在的入口）

- **新增事件类型**
  - 新建 `event/xxx_event.py` 继承 `BaseEvent`，实现 `get_event_type` + `calculate_stress_impact_dual`
  - 在 `utils/event_factory.py` 添加正则路由或强提示分支
  - 在 `stress_model.py` 的 `color_map` 增加类型配色与中文标签（否则会显示为“其他”）

- **新增/调整 Task 分级**
  - `utils/event_factory.py` 的 TaskEvent 正则梯队（T1~T5）
  - `event/task_event.py` 的 `intensity_map` 默认与 `get_fatigue_weight` 权重映射

- **连续负荷惩罚策略**
  - `strategy/course_strategy.py` 新增 `ContinuousPenaltyStrategy` 子类并注册到 `CourseStrategy._create_C_strategy`

- **课程敏感曲面（f_strategy）**
  - `strategy/course_strategy.py` 新增 `StressFunctionStrategy` 子类并注册到 `CourseStrategy._create_f_strategy`

- **影子编织规则**
  - `utils/routine_weaver.py`：
    - 睡眠段识别、+15min 缓冲、“熬夜前摇”过滤
    - 午餐/午睡/晚餐的理想窗口与最小持续时间
    - 睡眠债目标与偿还机制联动（NapEvent）

- **夜间恢复动力学**
  - `strategy/night_strategy.py` 三策略（normal/deep/anxious）与共用工具（非对称振幅、节律、AR1 噪声、饱和回血）

- **Library / Gym 特性**
  - `event/library_event.py`：`study_intensity` 时长衰减、`get_resilience_index()` 心流动力学
  - `event/gym_event.py`：`get_fatigue_weight()` 负值（连轴冷却）、`user.epoc_level` 注入与 Solver 内消费逻辑

- **生态演化与韧性**
  - `user.py`：`evolve_daily_baseline(wake_s, daily_mean_s, has_red_alert)` 双轨逻辑
  - `user.py`：`_calculate_resilience_index()` 四策略打分表