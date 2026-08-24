"""Reviewed course-name query expansions.

Aliases expand a user's short form into a catalog search query.  They are not
course identities because one query can legitimately match several catalog
entries.
"""

COURSE_ALIASES = {
    "高数": "高等数学",
    "线代": "线性代数",
    "大物": "大学物理",
    "计组": "计算机组成原理",
    "毛概": "毛泽东思想和中国特色社会主义理论体系概论",
    "马原": "马克思主义基本原理",
    "概率统计": "概率论与数理统计",
    "概统": "概率论与数理统计",
    "离散": "离散数学",
    "数据库": "数据库",
    "数据结构": "数据结构",
    "数分": "数学分析",
    "常微": "常微分方程",
    "复变": "复变函数",
    "模电": "模拟电子技术",
    "数电": "数字电子技术",
    "大学英语": "大学英语",
}
