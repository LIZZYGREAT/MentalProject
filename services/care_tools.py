"""User-bound whitelist tools exposed to the deterministic router or future agent."""

from __future__ import annotations

from typing import Any, Callable, Dict

from services.care_service import CareService


class CareToolbox:
    """Bind user identity at construction so tool arguments cannot switch users."""

    def __init__(self, care_service: CareService, trusted_user_id: int):
        self._service = care_service
        self._user_id = int(trusted_user_id)
        self._tools: Dict[str, Callable[..., Dict[str, Any]]] = {
            "care_get_today_context": self.care_get_today_context,
            "care_record_checkin": self.care_record_checkin,
            "care_run_today_assessment": self.care_run_today_assessment,
            "care_get_support": self.care_get_support,
            "care_submit_review": self.care_submit_review,
            "care_update_preferences": self.care_update_preferences,
            "calendar_connection_status": self.calendar_connection_status,
        }

    @property
    def allowed_tools(self) -> tuple[str, ...]:
        return tuple(self._tools)

    def execute(self, tool_name: str, **arguments: Any) -> Dict[str, Any]:
        if "user_id" in arguments:
            raise ValueError("user_id 由可信运行时注入，不能作为工具参数")
        tool = self._tools.get(str(tool_name))
        if tool is None:
            raise ValueError("工具不在关怀白名单中")
        return tool(**arguments)

    def care_get_today_context(self, local_date=None):
        return self._service.get_today_context(self._user_id, local_date)

    def care_record_checkin(self, payload, source="feishu_bot"):
        return self._service.record_checkin(self._user_id, payload, source)

    def care_run_today_assessment(self, local_date=None):
        return self._service.run_today_assessment(self._user_id, local_date)

    def care_get_support(self, context=None):
        return self._service.get_support(self._user_id, context)

    def care_submit_review(self, delivery_id, payload):
        return self._service.submit_review(self._user_id, delivery_id, payload)

    def care_update_preferences(self, changes):
        return self._service.update_preferences(self._user_id, changes)

    def calendar_connection_status(self):
        return self._service.calendar_connection_status(self._user_id)
