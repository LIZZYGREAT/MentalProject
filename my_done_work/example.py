import json

import lark_oapi as lark
from lark_oapi.api.calendar.v4 import *


# SDK 使用说明: https://open.feishu.cn/document/uAjLw4CM/ukTMukTMukTM/server-side-sdk/python--sdk/preparations-before-development
# 以下示例代码默认根据文档示例值填充，如果存在代码问题，请在 API 调试台填上相关必要参数后再复制代码使用
def main():
    # 创建client
    # 使用 user_access_token 需开启 token 配置, 并在 request_option 中配置 token
    client = lark.Client.builder() \
        .enable_set_token(True) \
        .log_level(lark.LogLevel.DEBUG) \
        .build()

    # 构造请求对象
    request: ListCalendarEventRequest = ListCalendarEventRequest.builder() \
        .calendar_id("feishu.cn_tbUzcWsrFrbgBxGm5dgoqc@group.calendar.feishu.cn") \
        .page_size(500) \
        .start_time("1761955200") \
        .end_time("1762009200") \
        .user_id_type("open_id") \
        .build()

    # 发起请求
    option = lark.RequestOption.builder().user_access_token("u-79KlBMOR55I8N6d5viimMBh559n0g0iXVO005lyEabgM").build()
    response: ListCalendarEventResponse = client.calendar.v4.calendar_event.list(request, option)

    # 处理失败返回
    if not response.success():
        lark.logger.error(
            f"client.calendar.v4.calendar_event.list failed, code: {response.code}, msg: {response.msg}, log_id: {response.get_log_id()}, resp: \n{json.dumps(json.loads(response.raw.content), indent=4, ensure_ascii=False)}")
        return

    # 处理业务结果
    lark.logger.info(lark.JSON.marshal(response.data, indent=4))


if __name__ == "__main__":
    main()