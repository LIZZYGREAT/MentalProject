# ================================================================
# description_score.py
# ---------------------------------------------------------------
# 用途：将日程 JSON 中的 "description" 和 "summary" 字段转化为数值分（1–10）
#       并提供喜好因子 F_like ∈ [-1, 1] 的映射函数。
# 升级版：引入两阶段打分流水线 (Summary先验 + SnowNLP情感) 
#         及 细粒度三梯度规则熔断与积极压力(Eustress)判定机制 (宽松版)
# ================================================================

import re
import logging
from settings.description_keywords import (
    DEFAULT_DESCRIPTION_SCORE,
    HARDCORE_WORDS,
    NEGATIVE_WORDS,
    POSITIVE_WORDS,
    RELAXING_WORDS,
    TIER1_WORDS,
    TIER2_WORDS,
    TIER3_WORDS,
)

try:
    from snownlp import SnowNLP
    SNOWNLP_AVAILABLE = True
except ImportError:
    SNOWNLP_AVAILABLE = False
    logging.warning("未检测到 SnowNLP 库，文本情感分析将回退到纯字典规则模式。请使用 'pip install snownlp' 安装。")

# ---------------------------------------------------------------
# 2. 内部处理模块
# ---------------------------------------------------------------

def _score_summary(summary: str) -> float:
    """Stage 1: 根据课程名称提取先验基准分"""
    if not summary:
        return DEFAULT_DESCRIPTION_SCORE
    
    sum_lower = summary.lower()
    
    for word in HARDCORE_WORDS:
        if word in sum_lower:
            return 4.0
            
    for word in RELAXING_WORDS:
        if word in sum_lower:
            return 6.5
            
    return DEFAULT_DESCRIPTION_SCORE

def _score_snownlp(description: str) -> float:
    """Stage 2: 提取 SnowNLP 情感基准线并映射到 1-10"""
    if not description or not SNOWNLP_AVAILABLE:
        return DEFAULT_DESCRIPTION_SCORE
        
    try:
        s = SnowNLP(description)
        sentiment = s.sentiments # 返回 0.0 ~ 1.0，越接近 1 越积极
        return 1.0 + sentiment * 9.0
    except Exception as e:
        logging.warning(f"SnowNLP 解析失败，回退到默认分: {str(e)}")
        return DEFAULT_DESCRIPTION_SCORE

# ---------------------------------------------------------------
# 3. 核心外部调用接口
# ---------------------------------------------------------------

def score_description(description: str, summary: str = "") -> float:
    """
    根据日程的 summary 和 description 进行两阶段+规则熔断打分。
    
    参数:
        description (str): 用户对事件的主观描述
        summary (str): 事件/课程名称
    返回:
        score (float): 1–10 分之间的数值
    """
    desc_str = description.lower() if description and isinstance(description, str) else ""
    sum_str = summary.lower() if summary and isinstance(summary, str) else ""
    full_text = f"{sum_str} {desc_str}"

    # ==========================================
    # Stage 1 & 2: 基础分数融合
    # ==========================================
    score_sum = _score_summary(sum_str)
    
    if desc_str:
        score_desc = _score_snownlp(desc_str)
        # 按照 3:7 权重融合：客观事实占30%，主观情绪占70%
        raw_score = 0.3 * score_sum + 0.7 * score_desc
    else:
        # 如果没有描述，完全依赖客观先验
        raw_score = score_sum

    # ==========================================
    # Stage 3: 三梯度 Eustress / Distress 双轨判定机制 (宽松版)
    # ==========================================
    final_score = raw_score
    
    has_pos = any(word in full_text for word in POSITIVE_WORDS)
    has_neg = any(word in full_text for word in NEGATIVE_WORDS)

    # 确定匹配的最高梯度 (Tier 1 优先级最高)
    matched_tier = 0
    if any(word in full_text for word in TIER1_WORDS):
        matched_tier = 1
    elif any(word in full_text for word in TIER2_WORDS):
        matched_tier = 2
    elif any(word in full_text for word in TIER3_WORDS):
        matched_tier = 3

    hard_cap = 10.0 # 默认天花板

    if matched_tier == 1:
        if has_pos and not has_neg:
            # Eustress (积极压力) - 极限任务的期待
            final_score += (-0.5 + 1.5) # 扣0.5负荷，加1.5积极，净增+1.0
            hard_cap = 7.0
        else:
            # Distress (恶性压力) - 纯折磨
            final_score -= 2.5
            if has_neg: final_score -= 1.5
            hard_cap = 4.0
            
    elif matched_tier == 2:
        if has_pos and not has_neg:
            # Eustress - 中度任务的期待
            final_score += (0.0 + 1.5) # 不扣负荷，加1.5积极
            hard_cap = 8.0
        else:
            # Distress - 中度压力输出
            final_score -= 1.5
            if has_neg: final_score -= 1.5
            hard_cap = 5.5
            
    elif matched_tier == 3:
        if has_pos and not has_neg:
            # Eustress - 日常任务的期待 (奖励)
            final_score += (0.5 + 1.5) # 奖励0.5，加1.5积极
            hard_cap = 9.0
        else:
            # Distress - 日常微压
            final_score -= 0.5
            if has_neg: final_score -= 1.5
            hard_cap = 7.0
            
    else:
        # 常规情绪 (未命中任何高压事件)
        if has_neg:
            final_score -= 1.5
        if has_pos:
            final_score += 1.5

    # ==========================================
    # Final Stage: 安全边界约束
    # ==========================================
    final_score = min(final_score, hard_cap)
    return max(1.0, min(10.0, final_score))


# ---------------------------------------------------------------
# 4. 分数 → 喜好因子 F_like
# ---------------------------------------------------------------
def convert_score_to_Flike(score: float) -> float:
    """
    将 1–10 分映射到 [-1, 1]，用于压力建模。

    例如：
        1分  → -1.0 （极不喜欢 / 极高压）
        5分  →  0.0 （中性）
        10分 → +1.0 （非常喜欢 / 极轻松）

    参数:
        score (float): 1–10 分
    返回:
        F_like (float): [-1, 1]
    """
    score = max(1.0, min(10.0, score))
    return round((score - 5.0) / 5.0, 3)


# ---------------------------------------------------------------
# 5. 快速测试（独立运行时）
# ---------------------------------------------------------------
if __name__ == "__main__":
    samples = [
        {"summary": "音乐鉴赏", "description": "今天去听课，非常开心，很喜欢这门课！"},
        {"summary": "高等量子力学", "description": ""},
        {"summary": "大学物理", "description": "老师讲得挺好，但是公式太多了有点烦躁。"},
        {"summary": "计算机体系结构", "description": "明天就要期末考试了，疯狂熬夜补救，崩溃！"}, # Tier 1 Distress
        {"summary": "体育选修", "description": "去打球放松一下"},
        {"summary": "年度组会", "description": "满怀期待地准备明天的组会汇报"} # 命中Tier 2"汇报"，触发宽松 Eustress
    ]

    print("=" * 70)
    print(f"{'Summary':<15} | {'Description':<25} | {'Score':<5} | {'F_like':<6}")
    print("-" * 70)
    
    for item in samples:
        sc = score_description(item["description"], item["summary"])
        fl = convert_score_to_Flike(sc)
        
        summ_print = item['summary'][:13] + ".." if len(item['summary']) > 15 else item['summary']
        desc_print = item['description'][:23] + ".." if len(item['description']) > 25 else item['description']
        if not desc_print: desc_print = "[无描述]"
        
        print(f"{summ_print:<15} | {desc_print:<25} | {sc:4.1f}  | {fl:+.2f}")
