import os
import json
import math
from datetime import datetime, timedelta
import numpy as np
import matplotlib.pyplot as plt
import matplotlib.dates as mdates

# === 导入 description_score 模块 ===
from description_score import score_description, convert_score_to_Flike

plt.rcParams['font.sans-serif'] = ['SimHei']
plt.rcParams['axes.unicode_minus'] = False

# =========================================================
# === 参数配置 =============================================
# =========================================================
DEFAULT_PARAMS = {
    "w1": 0.4, "w2": 0.15, "w3": 0.15,
    "lambda_like": 0.25,
    "time_weights": {(8, 10): 1.1, (10, 12): 0.9, (12, 14): 1.2,
                     (14, 16): 1.0, (16, 18): 0.9, (18, 20): 1.05,
                     (20, 24): 1.1},
    "k": 5.0, "c": 0.5,
    "eta": 0.85,
    "gamma": 0.040, "delta": 1.1, "theta": 0.05,
    "S_star_init": 50,
    "S_threshold": 85,
    "time_step": 5,
    "Z_awake": 0.5, "Z_avoid": 0.0, "Z_cogload": 0.2,
    "Z_info": 0.2, "Z_help": 0.1, "Z_valence": 0.0,
    "Z_factor": 0.5,
    "max_deltaS_course": 1.5,
    "random_seed": 42,
    "noise_scale_factor": 0.2
}

np.random.seed(DEFAULT_PARAMS["random_seed"])

# =========================================================
# === 分数 → λ_like 映射函数 ==============================
# =========================================================
def map_score_to_lambda(score: float, min_lambda=0.05, max_lambda=1.5) -> float:
    """
    将描述分 (1..10) 映射为 λ_like（正数）。
    设计原则：喜欢(高分) → λ小（惩罚弱）；讨厌(低分) → λ大（惩罚强）。
    """
    score = max(1.0, min(10.0, float(score)))
    t = (score - 1.0) / 9.0
    lam = max_lambda + (min_lambda - max_lambda) * t
    return float(lam)

# =========================================================
# === Z 类因素计算 =========================================
# =========================================================
def compute_Z(params=DEFAULT_PARAMS):
    awake = params.get("Z_awake", 0.5)
    avoid = params.get("Z_avoid", 0.0)
    cogload = params.get("Z_cogload", 0.3)
    info = params.get("Z_info", 0.2)
    help_seek = params.get("Z_help", 0.1)
    valence = params.get("Z_valence", 0.0)

    weights = np.array([0.2, 0.15, 0.25, 0.15, 0.15, 0.1])
    values = np.array([awake, avoid, cogload, info, help_seek, valence])
    z_raw = np.dot(weights, values)
    Z = 1 / (1 + np.exp(-z_raw))
    return Z * params.get("Z_factor", 0.5)

# =========================================================
# === 夜间恢复函数 =========================================
# =========================================================
def recover_overnight(S_end, sleep_hours=8, params=DEFAULT_PARAMS):
    S_star = params["S_star_init"]
    gamma, theta = params["gamma"], params["theta"]
    time_step = params["time_step"]

    for _ in range(int((sleep_hours*60)/time_step)):
        R_t = 1 - math.exp(-theta * time_step)
        Z = compute_Z(params)
        dS = -gamma * (S_end - S_star) * R_t * Z
        S_end += dS
        S_end = max(S_end, 0)
    return S_end

# =========================================================
# === compute_cis() =======================================
# =========================================================
def compute_cis(credit, hours, level, like=1.0, event_start=8, description=None, params=DEFAULT_PARAMS):
    """
    计算课程压力强度因子 CIS
    —— 自动根据 description 打分得到 λ_like（每门课独立）
    """
    w1, w2, w3 = params["w1"], params["w2"], params["w3"]
    base = (w1 * math.log1p(credit) + w2 * math.log1p(hours) + w3 * math.exp(level))

    # 根据 description 打分得到 λ_like
    try:
        score = score_description(description) if description else 5.0
    except Exception:
        score = 5.0
    lambda_like = map_score_to_lambda(score, min_lambda=0.05, max_lambda=1.5)

    F_like = math.exp(-lambda_like * like)

    # 时段修正
    F_time = 1.0
    for (start, end), val in params["time_weights"].items():
        if start <= event_start < end:
            F_time = val
            break

    return base * F_like * F_time

# =========================================================
# === 其他辅助函数 =========================================
# =========================================================
def compute_density(events, current_time):
    window_start = current_time - timedelta(hours=2)
    window_end = current_time + timedelta(hours=2)
    occupied = 0.0
    for ev in events:
        try:
            st = datetime.strptime(ev["start_time"], "%H:%M")
            et = datetime.strptime(ev["end_time"], "%H:%M")
            if et > window_start and st < window_end:
                occupied += (et - st).seconds / 3600
        except:
            continue
    return min(occupied / 4.0, 1.0)

def compute_cost(D_t, params=DEFAULT_PARAMS):
    eta = params["eta"]
    return 1.0 if D_t*4 < 2.5 else math.exp(eta*(D_t*4-2.5))

def f_s(S, params=DEFAULT_PARAMS):
    k = params.get("k", 5)
    S_star = params.get("S_star_init", 40)
    return (np.tanh(k * (S - S_star)) + 1) / 2

def delta_S_course(S, CIS, D_t, C_t, params=DEFAULT_PARAMS):
    Z = compute_Z(params)
    base_delta = D_t * C_t * CIS * f_s(S, params) * Z
    S_star = params.get("S_star_init", 40)
    scale_factor = params.get("noise_scale_factor", 0.2)
    noise = np.random.normal(loc=0.0, scale=scale_factor * np.sqrt(S_star))
    delta = base_delta + noise
    max_delta = params.get("max_deltaS_course", 1.5)
    return max(min(delta, max_delta), 0)

def delta_S_rest(S, R_t, S_star, params=DEFAULT_PARAMS):
    Z = compute_Z(params)
    gamma, delta = params["gamma"], params["delta"]
    return -gamma * R_t * abs(S - S_star) ** delta * Z

def compute_R(t_rest, params=DEFAULT_PARAMS):
    theta = params["theta"]
    return 1 - math.exp(-theta * t_rest)

# =========================================================
# === 主压力仿真函数 =======================================
# =========================================================
def simulate_stress(events, prev_S_end=None, params=DEFAULT_PARAMS):
    time_step = params["time_step"]
    S_star = params["S_star_init"]
    S = recover_overnight(prev_S_end, params=params) if prev_S_end is not None else S_star

    results = []
    t_rest = 0
    start_time = datetime.strptime("08:00", "%H:%M")
    end_time = datetime.strptime("23:00", "%H:%M")
    current_time = start_time

    while current_time <= end_time:
        cur_str = current_time.strftime("%H:%M")
        active_events = []
        for ev in events:
            try:
                st = datetime.strptime(ev["start_time"], "%H:%M")
                et = datetime.strptime(ev["end_time"], "%H:%M")
                if st <= current_time < et:
                    active_events.append(ev)
            except:
                continue

        if active_events:
            total_deltaS = 0
            for ev in active_events:
                CIS = compute_cis(
                    credit=3, hours=2, level=1, like=1.0,
                    event_start=current_time.hour,
                    description=ev.get("description", ""),
                    params=params
                )
                D_t = compute_density(events, current_time)
                C_t = compute_cost(D_t, params)
                total_deltaS += delta_S_course(S, CIS, D_t, C_t, params) * 0.6
            deltaS = total_deltaS
            t_rest = 0
        else:
            t_rest += time_step
            R_t = compute_R(t_rest, params)
            deltaS = delta_S_rest(S, R_t, S_star, params)

        S += deltaS
        S = max(S, 0)
        results.append({"time": cur_str, "S": S})
        current_time += timedelta(minutes=time_step)

    return results, S

# =========================================================
# === 多级置信度警报机制 ===================================
# =========================================================
def check_stress_alerts(results, params=DEFAULT_PARAMS,
                        duration_threshold=45, alpha=1.2,
                        alpha_c=0.018, beta_c=0.025):
    alerts = []
    S_star = params["S_star_init"]
    S_thresh = params["S_threshold"]
    time_step = params["time_step"]

    S_values = [r["S"] for r in results]
    times = [r["time"] for r in results]
    max_S = max(S_values)

    C_t = 0.0
    confidence_series = []
    count_high = 0

    for i, S in enumerate(S_values):
        # 优化的置信度计算，根据压力超过阈值的程度调整增长速度
        if S >= S_thresh:
            # 压力越高，置信度增长越快
            exceed_ratio = (S - S_thresh) / S_thresh
            growth_rate = alpha_c * (1 + exceed_ratio)
            C_t = min(1.0, C_t + growth_rate)
            count_high += time_step
        else:
            # 压力远低于阈值时，置信度下降更快
            safe_ratio = (S_thresh - S) / S_thresh
            decay_rate = beta_c * (1 + min(safe_ratio, 1.0))
            C_t = max(0.0, C_t - decay_rate)
            count_high = 0
        confidence_series.append(C_t)

        if S >= S_thresh:
            if 0.4 <= C_t < 0.7:
                level = "[黄]轻度预警"
            elif 0.7 <= C_t < 0.9:
                level = "[橙]中度警报"
            elif C_t >= 0.9:
                level = "[红]严重警报"
            else:
                continue
            alerts.append({"type": level, "time": times[i], "S": S, "C": C_t})

        if count_high >= duration_threshold and C_t >= 0.7:
            alerts.append({"type": "[橙]持续高压(中度+)", "time": times[i], "S": S, "C": C_t})
            count_high = 0

    if max_S >= alpha * S_star:
        idx = np.argmax(S_values)
        alerts.append({"type": "[红]异常峰值(严重)", "time": times[idx], "S": max_S, "C": confidence_series[idx]})

    # 改进的恢复不足检测，考虑一天结束时的压力水平和阈值的关系
    end_pressure_diff = S_values[-1] - S_star
    if end_pressure_diff > 10 or (end_pressure_diff > 5 and S_values[-1] > S_thresh * 0.8):
        alerts.append({"type": "[橙]恢复不足(中度)", "time": times[-1], "S": S_values[-1], "C": None})

    if not alerts:
        print("✅ 无异常压力警报。")
    else:
        print("\n🚨 检测到多级压力警报：")
        # 按时间排序警报
        alerts.sort(key=lambda x: x['time'])
        
        # 去重连续的相同类型警报
        unique_alerts = []
        prev_type = None
        for a in alerts:
            if a['type'] != prev_type or (prev_type and '持续高压' not in prev_type):
                unique_alerts.append(a)
            prev_type = a['type']
        
        # 输出警报
        for a in unique_alerts:
            c_str = f" | 置信度={a['C']:.2f}" if a["C"] is not None else ""
            print(f" - {a['type']} 时间 {a['time']} 压力={a['S']:.2f}{c_str}")
        
        return unique_alerts, confidence_series

# =========================================================
# === 绘图增强 =============================================
# =========================================================
def plot_stress_with_alerts(results, confidence_series, alerts, params=DEFAULT_PARAMS, S_star=None, events_file=None):
    times = [datetime.strptime(r["time"], "%H:%M") for r in results]
    S_values = [r["S"] for r in results]

    fig, ax1 = plt.subplots(figsize=(12, 5))
    ax1.plot(times, S_values, color="royalblue", linewidth=2.0, label="压力值 S(t)")
    ax1.axhline(y=S_star, color="gray", linestyle=":", linewidth=1.2, label=f"S*={S_star}")
    ax1.axhline(y=params["S_threshold"], color="red", linestyle="--", linewidth=1.0, label=f"阈值={params['S_threshold']}")
    ax1.set_ylabel("压力值", color="royalblue")
    ax1.tick_params(axis="y", labelcolor="royalblue")

    ax2 = ax1.twinx()
    ax2.plot(times, confidence_series, color="orange", linestyle="--", linewidth=1.8, label="置信度 C(t)")
    ax2.set_ylim(0, 1.05)
    ax2.set_ylabel("置信度", color="orange")
    ax2.tick_params(axis="y", labelcolor="orange")

    max_y = max(S_values) + 15
    ax1.set_ylim(0, max_y)

    if events_file:
        with open(events_file, "r", encoding="utf-8") as f:
            events = json.load(f)
        for ev in events:
            try:
                st = datetime.strptime(ev["start_time"], "%H:%M")
                et = datetime.strptime(ev["end_time"], "%H:%M")
                ax1.axvspan(st, et, color="red", alpha=0.15)
            except:
                continue


    ax1.set_xlabel("时间")
    ax1.set_title("压力与置信度变化曲线（多级警报）")
    ax1.xaxis.set_major_formatter(mdates.DateFormatter('%H:%M'))
    plt.xticks(rotation=45)
    ax1.grid(True, linestyle="--", alpha=0.4)
    ax1.legend(loc="upper right")
    plt.tight_layout()
    plt.show()


# =========================================================
# === 主入口 ===============================================
# =========================================================
if __name__ == "__main__":
    json_files = [
        "calendar_data/calendar_20251027.json",
        "calendar_data/calendar_20251028.json"
    ]

    with open(json_files[0], "r", encoding="utf-8") as f:
        events_24 = json.load(f)

    res_24, S_end_24 = simulate_stress(events_24, prev_S_end=None, params=DEFAULT_PARAMS)
    alerts_24, conf_24 = check_stress_alerts(res_24, params=DEFAULT_PARAMS)

    plot_stress_with_alerts(res_24, conf_24, alerts_24, params=DEFAULT_PARAMS,
                            S_star=DEFAULT_PARAMS["S_star_init"], events_file=json_files[0])

    with open(json_files[1], "r", encoding="utf-8") as f:
        events_25 = json.load(f)
    res_25, S_end_25 = simulate_stress(events_25, prev_S_end=S_end_24, params=DEFAULT_PARAMS)
    alerts_25, conf_25 = check_stress_alerts(res_25, params=DEFAULT_PARAMS)

    plot_stress_with_alerts(res_25, conf_25, alerts_25, params=DEFAULT_PARAMS,
                            S_star=DEFAULT_PARAMS["S_star_init"], events_file=json_files[1])

    print("\n📊 两天压力曲线与警报检测完成。")
