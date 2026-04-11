# visualization/plotter.py
import os
import io
import base64
from datetime import datetime
import matplotlib
matplotlib.use('Agg')  
import matplotlib.pyplot as plt
import matplotlib.dates as mdates

from entry.config import GLOBAL_DEFAULT_CONFIG

# 设置中文字体
plt.rcParams['font.sans-serif'] = ['SimHei']
plt.rcParams['axes.unicode_minus'] = False

def _draw_core_plot(results, confidence_series, alerts, params=None, S_star=None, events=None, is_web=False):
    """
    纯粹的绘图引擎：接收推演结果，输出高对比度双轴状态图
    """
    params = params or GLOBAL_DEFAULT_CONFIG
    times = [datetime.strptime(r["time"], "%H:%M") for r in results]
    S_values = [r["S"] for r in results]
    E_values = [r.get("E", 100.0) for r in results] 

    fig, (ax1, ax3) = plt.subplots(2, 1, figsize=(14, 9), sharex=True, gridspec_kw={'height_ratios': [3, 1]})
    
    # === 上图：压力 S ===
    ax1.plot(times, S_values, color="royalblue", linewidth=2.5, label="压力值 S(t)")
    if S_star:
        ax1.axhline(y=S_star, color="gray", linestyle=":", linewidth=1.5, label=f"平衡值 S*={S_star}")
    
    S_thresh = params.get("S_threshold", 85.0)
    ax1.axhline(y=S_thresh, color="red", linestyle="--", linewidth=1.5, label=f"报警阈值={S_thresh}")
    
    min_s_val = min(S_values) if S_values else 0
    max_s_val = max(S_values) if S_values else 100
    
    y_lower_limit = max(0, min_s_val - 10)
    y_range = max_s_val - y_lower_limit
    if y_range < 10: y_range = 10
    
    y_upper_limit = max_s_val + y_range * 0.45
    if y_upper_limit < S_thresh + 10:
        y_upper_limit = S_thresh + 10
        y_range = y_upper_limit - y_lower_limit
        
    ax1.set_ylim(y_lower_limit, y_upper_limit)
    ax1.set_ylabel("心理压力 (S)", color="royalblue", fontsize=13, weight='bold')
    ax1.tick_params(axis="y", labelcolor="royalblue")
    ax1.grid(True, linestyle="--", alpha=0.3)

    trans = ax1.get_xaxis_transform()

    # === 事件色块渲染 ===
    if events:
        color_map = {
            "course": ("#4169E1", "课程"),   # 皇家蓝
            "task": ("#DC143C", "任务"),     # 猩红色
            "sleep": ("#191970", "睡眠"),    # 午夜蓝
            "nap": ("#20B2AA", "午休"),      # 浅海绿
            "meal": ("#3CB371", "就餐"),     # 中海绿
            "rest": ("#BDB76B", "休息"),     # 暗卡其
            "gym": ("#FF8C00", "运动"),      # 深橙色
            "library": ("#8A2BE2", "自习")   # 蓝紫色
        }
        relative_y_offsets = [0.95, 0.86]  
        
        for i, ev in enumerate(events):
            try:
                st_str = ev.start_time if isinstance(ev.start_time, str) else ev.start_time.strftime("%H:%M")
                et_str = ev.end_time if isinstance(ev.end_time, str) else ev.end_time.strftime("%H:%M")
                if ' ' in st_str: st_str = st_str.split(' ')[-1][:5]
                if ' ' in et_str: et_str = et_str.split(' ')[-1][:5]

                st = datetime.strptime(st_str, "%H:%M")
                et = datetime.strptime(et_str, "%H:%M")
                
                ev_type = ev.get_event_type() if hasattr(ev, 'get_event_type') else "other"
                
                name = getattr(ev, 'name', '')
                if not name or name == "未知": 
                    name = getattr(ev, 'course_name', '')
                if not name or name == "未知":
                    if hasattr(ev, 'metadata') and ev.metadata:
                        name = ev.metadata.get('summary', '') or ev.metadata.get('name', '')
                if not name or name == "未知": 
                    name = "常规事件"
                
                color, type_name = color_map.get(ev_type, ("#7f7f7f", "其他"))
                alpha_val = 0.3 if ev_type == "sleep" else 0.2
                
                ax1.axvspan(st, et, color=color, alpha=alpha_val)
                ax3.axvspan(st, et, color=color, alpha=alpha_val)
                
                mid_time = st + (et - st) / 2
                y_pos = relative_y_offsets[i % 2]
                label = f"[{type_name}] {name}\n{st_str}-{et_str}"
                
                ax1.text(mid_time, y_pos, label, transform=trans, ha='center', va='top', fontsize=9,
                         color=color, weight='bold',
                         bbox=dict(facecolor='white', alpha=0.9, edgecolor=color, boxstyle='round,pad=0.3'))
            except Exception:
                pass

    # === 惩罚阶梯渲染 ===
    f_pen_values = [r.get("f_pen", 0.0) for r in results]
    if any(p > 0 for p in f_pen_values):
        max_pen = max(f_pen_values)
        scale_factor = (y_range * 0.20) / max_pen if max_pen > 0 else 0
        scaled_pen = [p * scale_factor + y_lower_limit for p in f_pen_values]
        
        ax1.fill_between(times, y_lower_limit, scaled_pen, color="crimson", alpha=0.25, label="连轴转惩罚生效区", step="post")
        
        is_active = False
        for i, p in enumerate(f_pen_values):
            if p > 0 and not is_active:
                arrow_y = scaled_pen[i]
                ax1.annotate('惩罚触发', xy=(times[i], arrow_y), xytext=(times[i], arrow_y + y_range*0.08),
                             arrowprops=dict(facecolor='crimson', shrink=0.05, width=1.5, headwidth=6),
                             fontsize=10, color='crimson', weight='bold')
                is_active = True
            elif p == 0:
                is_active = False

    # === 预警点位渲染 ===
    if alerts:
        for a in alerts:
            try:
                a_time_str = a["time"]
                if ' ' in a_time_str: a_time_str = a_time_str.split(' ')[1]
                a_time = datetime.strptime(a_time_str, "%H:%M")
                a_s = a["S"]
                a_type = a["type"]
                ax1.plot(a_time, a_s, marker='o', color='red', markersize=7, zorder=5)
                ax1.annotate(a_type, xy=(a_time, a_s), xytext=(a_time, a_s + y_range*0.05),
                             color='red', fontsize=10, weight='bold', zorder=6,
                             arrowprops=dict(arrowstyle='->', color='red'))
            except:
                pass

    ax1.legend(loc="center left", bbox_to_anchor=(0.01, 0.75), fontsize=10)

    # === 双轴绘制置信度 ===
    ax2 = ax1.twinx()
    ax2.fill_between(times, confidence_series, color="orange", alpha=0.1)
    ax2.plot(times, confidence_series, color="orange", linestyle="--", linewidth=1.5, alpha=0.6, label="警报置信度")
    ax2.set_ylim(0, 1.05)
    ax2.set_ylabel("置信度 (0-1)", color="orange", fontsize=11)
    ax2.tick_params(axis="y", labelcolor="orange")
    ax2.legend(loc="center left", bbox_to_anchor=(0.01, 0.65), fontsize=10)

    # === 下图：精力 E ===
    ax3.plot(times, E_values, color="mediumseagreen", linewidth=2.5, label="认知精力 E(t)")
    E_crit = params.get("E_critical", 20.0)
    ax3.axhline(y=E_crit, color="crimson", linestyle="-.", linewidth=1.5, label=f"耗竭阈值={E_crit}")
    ax3.fill_between(times, 0, E_crit, color="red", alpha=0.1)
    
    ax3.set_ylabel("精力值 (E)", color="mediumseagreen", fontsize=13, weight='bold')
    ax3.set_ylim(0, 105)
    ax3.grid(True, linestyle="--", alpha=0.3)
    ax3.legend(loc="lower left", fontsize=10)

    ax3.set_xlabel("时间 (24h)", fontsize=12)
    ax3.xaxis.set_major_formatter(mdates.DateFormatter('%H:%M'))
    plt.xticks(rotation=45)
    
    plt.suptitle("心理压力(S)与精力(E)双变量演化模型", fontsize=16, weight='bold')
    plt.tight_layout()
    
    return fig

def plot_stress_with_alerts(results, confidence_series, alerts, params=None, S_star=None, events=None):
    if not results:
        print("无数据可绘图")
        return
    fig = _draw_core_plot(results, confidence_series, alerts, params, S_star, events, is_web=False)
    plt.show()

def get_plot_image_base64(results, confidence_series, alerts, params=None, S_star=None, events=None):
    """将推演序列与告警绘成 PNG，返回 base64 字符串供 Web 嵌入。"""
    if not results:
        return None
    try:
        fig = _draw_core_plot(results, confidence_series, alerts, params, S_star, events, is_web=True)
        img = io.BytesIO()
        plt.savefig(img, format='png', bbox_inches='tight', dpi=120)
        plt.close(fig) 
        img.seek(0)
        return base64.b64encode(img.getvalue()).decode('utf-8')
    except Exception as e:
        print(f"绘图失败: {e}")
        return None