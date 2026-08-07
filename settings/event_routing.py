"""Keyword routing rules for converting calendar text into event classes."""

EXPLICIT_EVENT_TYPES = ("course", "rest", "gym", "library", "task")

ROUTINE_PATTERNS = {
    "meal": r"饭|餐|食堂|breakfast|lunch|dinner",
    "nap": r"午休|睡觉|打盹|nap|sleep",
    "gym": r"健身|锻炼|跑步|游泳|gym|workout",
    "library": r"自习|图书馆|复习|library|study",
}

TASK_PATTERNS = {
    # Include common abbreviations.  In particular, 数竞 is not tokenized as
    # 竞赛 by a plain substring rule and was previously routed as a general task.
    "exam": r"考|测验|期末|期中|数竞|奥赛|竞赛|比赛|面试|答辩",
    "ddl": r"ddl|截止|提交|汇报|大作业|实验|攻关",
    "meeting": r"会|讨论|例会|面谈|讲座|编程",
    "homework": r"作业|报告|项目|练习|培训",
}

COURSE_HINT_PATTERN = r"课"

