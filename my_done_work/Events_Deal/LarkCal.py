from utils.struct import serialize, unserialize, TypedList, field, REQUIRED, UNREQUIRED, FIXED, NODEFAULT, EMPTY, SERIAL_FAIL, component
import json

class time_info(component):
    '''
    date:开始时间，仅全天日程使用该字段，RFC 3339 格式，例如，2018-09-01。
        注意：该参数不能与 timestamp 同时指定。
    timestamp:秒级时间戳，用于设置具体的开始时间。例如，1602504000 表示 2020/10/12 20:00:00（UTC +8 时区）。
        注意：该参数不能与 date 同时指定。
        示例值："1602504000"
    timezone:时区。使用 IANA Time Zone Database 标准，例如 Asia/Shanghai。
        全天日程时区固定为UTC +0
        非全天日程时区默认为 Asia/Shanghai
    '''
    _is_concrete = True
    _meta_add = dict(date = field(UNREQUIRED,str,EMPTY),
                     timestamp = field(UNREQUIRED,str,EMPTY),
                     timezone = field(UNREQUIRED,str,EMPTY)
                    )
class event_location(component):
    '''
    name:地点名称
    address:地点地址
    latitude:地点坐标纬度信息。
        对于国内的地点，采用 GCJ-02 标准。
        对于海外的地点，采用 WGS84 标准。
    longitude:地点坐标经度信息。
        对于国内的地点，采用 GCJ-02 标准。
        对于海外的地点，采用 WGS84 标准。
    '''
    _is_concrete = True
    _meta_add = dict(name = field(UNREQUIRED,str,NODEFAULT),
                     address = field(UNREQUIRED,str,NODEFAULT),
                     latitude = field(UNREQUIRED,float,NODEFAULT),
                     longitude = field(UNREQUIRED,float,NODEFAULT)
                    )
class reminder(component):
    _is_concrete = True
    _meta_add = dict(minutes = field(UNREQUIRED,int,EMPTY))
Reminders = TypedList[reminder]

class schema(component):
    '''
    ui_name:UI 名称:
        ForwardIcon:日程转发按钮
        MeetingChatIcon:会议群聊按钮
        MeetingMinutesIcon:会议纪要按钮
        MeetingVideo:视频会议区域
        RSVP:接受、拒绝、待定区域
        Attendee:参与者区域
        OrganizerOrCreator:组织者或创建者区域
    ui_status:UI 项的状态。目前只支持选择 hide
    app_link:按钮点击后跳转的链接。
        注意：兼容性参数，只读，因此暂不支持传入该请求参数。
    '''
    _is_concrete = True
    _meta_add = dict(ui_name = field(UNREQUIRED,str,EMPTY),
                     ui_status = field(FIXED,str,'hide'),
                     app_link = field(UNREQUIRED,str,EMPTY)
                    )
Schemas = TypedList[schema]

class meeting_settings(component):
    '''
    owner_id:设置会议 owner 的用户 ID，ID 类型需和 user_id_type 保持一致。
该参数需满足以下全部条件才会生效:
        应用身份（tenant_access_token）请求，且在应用日历上操作日程。
        首次将日程设置为 VC 会议时，才能设置owner。
        owner 不能为非用户身份。
        owner 不能为外部租户用户身份。
    join_meeting_permission:设置入会范围。
        anyone_can_join:所有人可以加入会议
        only_organization_employees:仅企业内的用户可以加入会议
        only_event_attendees:仅日程参与者可以加入会议
    assign_hosts:通过用户 ID 指定主持人，ID 类型需和 user_id_type 保持一致。
        仅日程组织者可以指定主持人。
        主持人不能是非用户身份。
        主持人不能是外部租户用户身份。
        在应用日历上操作日程时，不允许指定主持人。
    auto_record:是否开启自动录制。
    open_lobby:是否开启等候室。
    allow_attendees_start:是否允许日程参与者发起会议。
        应用日历上操作日程时，该字段必须为 true，否则没有人能发起会议。
    '''
    _is_concrete = True
    _meta_add = dict(owner_id = field(UNREQUIRED,str,NODEFAULT),
                     join_meeting_permission = field(UNREQUIRED,str,'anyone_can_join'),
                     assign_hosts = field(UNREQUIRED,TypedList[str],EMPTY),
                     auto_record = field(UNREQUIRED,bool,False),
                     open_lobby = field(UNREQUIRED,bool,True),
                     allow_attendees_start = field(UNREQUIRED,bool,True)
                    )

class vchat(component):
    '''
    vc_type:视频会议类型。如果无需视频会议，则必须传入 no_meeting。
        默认值:空，表示在首次添加日程参与人时，会自动生成飞书视频会议 URL。
        vc:飞书视频会议。取该类型时，vchat 内的其他字段均无效。
        third_party:第三方链接视频会议。取该类型时，仅生效 vchat 内的 icon_type、description、meeting_url 字段。
        no_meeting:无视频会议。取该类型时，vchat 内的其他字段均无效。
        lark_live:飞书直播。该值用于客户端，不支持通过 API 调用，只读。
        unknown:未知类型。该值用于客户端做兼容使用，不支持通过 API 调用，只读。
    icon_type:第三方视频会议的 icon 类型。
        vc:飞书视频会议 icon。
        live:直播视频会议 icon。
        default:默认 icon。
    description:第三方视频会议文案
    meeting_url:视频会议 URL
    meeting_settings:飞书视频会议（VC）的会前设置，需满足以下全部条件：
        当 vc_type 为 vc 时生效。
        需要有日程的编辑权限
    '''
    _is_concrete = True
    _meta_add = dict(vc_type = field(REQUIRED,str,'no_meeting'),
                     icon_type = field(UNREQUIRED,str,'default'),
                     description = field(UNREQUIRED,str,EMPTY),
                     meeting_url = field(UNREQUIRED,str,EMPTY),
                     meeting_settings = field(UNREQUIRED,meeting_settings,EMPTY)
                    )
                     

class check_in_time(component):
    '''
    time_type:偏移量(分钟)相对于的日程时间节点类型。
        before_event_start：日程开始前
        after_event_start：日程开始后
        after_event_end：日程结束后
    duration:相对于日程开始或者结束的偏移量(分钟)。
        目前取值只能为列表[0, 5, 15, 30, 60]之一，0表示立即开始。
        当time_type为before_event_start，duration不能取0
    '''
    _is_concrete = True
    _meta_add = dict(time_type = field(REQUIRED,str,NODEFAULT),
                     duration = field(REQUIRED,int,NODEFAULT)
                    )

class event_check_in(component):
    '''
    enable_check_in:是否启用日程签到。
    check_in_start_time:日程签到开始时间。
        签到开始时间不能大于或者等于签到结束时间。
    check_in_ent_time:日程签到结束时间
        注意：签到开始时间不能大于或者等于签到结束时间。
    need_notify_attendee:签到开始时是否自动发送签到通知给参与者
    '''
    _is_concrete = True
    _meta_add = dict(enable_check_in = field(REQUIRED,bool,NODEFAULT),
                     check_in_start_time = field(UNREQUIRED,check_in_time,EMPTY),
                     check_in_end_time = field(UNREQUIRED,check_in_time,EMPTY),
                     need_notify_attendees = field(UNREQUIRED,bool,False)
                    )

class attachment(component):
    _is_concrete = True
    _meta_add = dict(file_token = field(UNREQUIRED,str,EMPTY))
Attachments = TypedList[attachment]

class vevent(component):
    '''
    summary:日程标题
    description:日程描述。支持解析Html标签。
        可以通过Html标签来实现部分富文本格式，但是客户端生成的富文本格式并不是通过Html标签实现
        如果通过客户端生成富文本描述后，再通过API更新描述，会导致客户端原来的富文本格式丢失。
    need_notification:更新日程时，是否给日程参与人发送 Bot 通知。
        true：发送通知
        false：不发送通知
    start_time:日程开始时间
    end_time:日程结束时间
    vchat:视频会议信息
    visibility:日程公开范围，新建日程默认为 default。
        该参数仅在新建日程时，对所有参与人生效。如果后续更新日程修改了该参数值，则仅对当前身份生效。
        default:默认权限，即跟随日历权限，默认仅向他人显示是否忙碌
        public:公开，显示日程详情
        private:私密，仅自己可见详情
    attendee_ability:参与人权限。
        none:无法编辑日程、无法邀请其他参与人、无法查看参与人列表
        can_see_others:无法编辑日程、无法邀请其他参与人、可以查看参与人列表
        can_invite_others:无法编辑日程、可以邀请其他参与人、可以查看参与人列表
        can_modify_event:可以编辑日程、可以邀请其他参与人、可以查看参与人列表
    free_busy_status:日程占用的忙闲状态，新建日程默认为 busy。
        该参数仅在新建日程时，对所有参与人生效。如果后续更新日程时修改了该参数值，则仅对当前身份生效。
        busy:忙碌
        free:空闲
    location:日程地点，不传值则默认为空。
    color:日程颜色，取值通过颜色 RGB 值的 int32 表示。
        该参数仅对当前身份生效。
        客户端展示时会映射到色板上最接近的一种颜色。
        取值为 0 或 -1 时，默认跟随日历颜色。
    reminders:日程提醒列表。不传值则默认为空。
    recurrence:重复日程的重复性规则，规则设置方式参考rfc5545。
        默认值：空，表示当前日程不是重复日程。
        COUNT 和UNTIL 不支持同时出现。
        预定会议室重复日程长度不得超过两年。
        示例值："FREQ=DAILY;INTERVAL=1"
    schemas:日程自定义信息，控制日程详情页的 UI 展示。不传值则默认为空。
    attachements:日程附件。
    event_check_in:日程签到设置，为空则不进行日程签到设置。
    '''
    _is_concrete = True
    _meta_add = dict(summary = field(UNREQUIRED,str,EMPTY),
                     description = field(UNREQUIRED,str,EMPTY),
                     need_notification = field(UNREQUIRED,bool,False),
                     start_time = field(REQUIRED,time_info,True),
                     end_time = field(REQUIRED,time_info,True),
                     vchat = field(UNREQUIRED,vchat,EMPTY),
                     visibility = field(UNREQUIRED,str,'default'),
                     attendee_ability = field(UNREQUIRED,str,'none'),
                     free_busy_status = field(UNREQUIRED,str,'busy'),
                     location = field(UNREQUIRED,event_location,EMPTY),
                     color = field(UNREQUIRED,int,-1),
                     reminders = field(UNREQUIRED,Reminders,EMPTY),
                     recurrence = field(UNREQUIRED,str,EMPTY),
                     schemas = field(UNREQUIRED,Schemas,EMPTY),
                     attachments = field(UNREQUIRED,Attachments,EMPTY),
                     event_check_in = field(UNREQUIRED,event_check_in,EMPTY)
                    )

if __name__ == '__main__':
    example_event = vevent(attendee_ability = 'can_see_others',
                        color = -1,
                        description = '日程描述',
                        free_busy_status = 'busy',
                        need_notification = False,
                        recurrence = 'FREQ=DAILY;INTERVAL=1',
                        summary = '日程标题',
                        visibility = 'default')
    example_event.attachments = example_event.get_type('attachments')([attachment(file_token='xAAAAA')])
    example_event.start_time = time_info(date='2018-09-01',timestamp="1602504000",timezone="Asia/Shanghai")
    example_event.end_time = time_info(date='2018-09-01',timestamp="1602504000",timezone="Asia/Shanghai")
    example_event.event_check_in = event_check_in(check_in_end_time = check_in_time(duration=0,time_type='after_event_end'),
                                                check_in_start_time = check_in_time(duration=15,time_type='before_event_start'),
                                                enable_check_in = True,
                                                need_notify_attendees = False)
    example_event.__setattr__('location',event_location(**{"address": "地点地址",
                                                        "latitude": 1.100000023841858,
                                                        "longitude": 2.200000047683716,
                                                        "name": "地点名称"}))
    example_event.reminders=TypedList[reminder]()
    example_event.reminders.append(reminder(minutes=5))
    example_event.schemas = Schemas([schema(app_link='applink',ui_name='ForwardIcon',ui_status='hide')])
    #print(example_event.serialize())
    assert isinstance(unserialize(example_event.serialize(),vevent),vevent)