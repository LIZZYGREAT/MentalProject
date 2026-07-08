import os
# ==========================================
# 1. 飞书应用核心凭证 (App ID & App Secret)
# ==========================================
FEISHU_APP_ID = os.getenv("FEISHU_APP_ID", "")
FEISHU_APP_SECRET = os.getenv("FEISHU_APP_SECRET", "")
# ==========================================
# 2. 目标日历配置 (Calendar ID & Open ID)
# ==========================================
FEISHU_CALENDAR_ID = os.getenv("FEISHU_CALENDAR_ID", "")
FEISHU_OPEN_ID = os.getenv("FEISHU_OPEN_ID", "")
