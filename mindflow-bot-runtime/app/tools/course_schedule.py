"""Participant-bound tools for reviewing and correcting schedule drafts."""

from __future__ import annotations

from datetime import date, time
from typing import Any

from app.agent.context import AgentContext
from app.agent.tool_registry import (
    AuthorizationContextResolutionError,
    ToolRegistry,
)
from app.integrations.feishu.cards import course_schedule_preview_card
from app.repositories_course_schedule import (
    CourseCorrectionAmbiguityError,
    CourseScheduleImportAmbiguityError,
)


def _empty_schema() -> dict[str, Any]:
    return {"type": "object", "properties": {}, "additionalProperties": False}


def _selector_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "course_name": {"type": "string", "minLength": 1, "maxLength": 200},
            "selector_weekday": {"type": "integer", "minimum": 1, "maximum": 7},
        },
        "minProperties": 1,
        "additionalProperties": False,
    }


def _updates_schema() -> dict[str, Any]:
    time_schema = {
        "type": "string",
        "pattern": "^(?:[01]\\d|2[0-3]):[0-5]\\d$",
    }
    return {
        "type": "object",
        "properties": {
            "new_weekday": {"type": "integer", "minimum": 1, "maximum": 7},
            "period_start": {"type": "integer", "minimum": 1, "maximum": 30},
            "period_end": {"type": "integer", "minimum": 1, "maximum": 30},
            "start_time": time_schema,
            "end_time": time_schema,
            "week_start": {"type": "integer", "minimum": 1, "maximum": 60},
            "week_end": {"type": "integer", "minimum": 1, "maximum": 60},
            "odd_even": {"type": "string", "enum": ["all", "odd", "even"]},
            "explicit_weeks": {
                "type": "array",
                "items": {"type": "integer", "minimum": 1, "maximum": 60},
                "minItems": 1,
                "maxItems": 60,
                "uniqueItems": True,
            },
            "location": {"type": "string", "minLength": 1, "maxLength": 300},
        },
        "minProperties": 1,
        "additionalProperties": False,
    }


def _cancel_selector_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "latest": {"type": "boolean"},
            "course_name": {"type": "string", "minLength": 1, "maxLength": 200},
            "created_date": {"type": "string", "format": "date"},
        },
        "minProperties": 1,
        "maxProperties": 1,
        "additionalProperties": False,
    }


def _public_import_candidate(candidate: dict[str, Any]) -> dict[str, Any]:
    """Return timestamps in the import's local timezone, never raw UTC."""
    return {
        "status": candidate.get("status"),
        "created_local_datetime": candidate.get("created_local_datetime"),
        "created_local_date": candidate.get("created_local_date"),
        "timezone": candidate.get("timezone"),
        "course_names": list(candidate.get("course_names") or []),
        "has_provider_effect": bool(candidate.get("has_provider_effect")),
    }


def _public_draft(draft: dict[str, Any]) -> dict[str, Any]:
    structured = dict(draft.get("structured_result") or {})
    metadata = dict(structured.get("_metadata") or {})
    sources = list(metadata.get("course_time_sources") or [])
    courses: list[dict[str, Any]] = []
    for index, raw in enumerate(structured.get("courses") or []):
        course = dict(raw)
        courses.append(
            {
                "course_name": course.get("course_name"),
                "weekday": course.get("weekday"),
                "period_start": course.get("period_start"),
                "period_end": course.get("period_end"),
                "start_time": course.get("start_time"),
                "end_time": course.get("end_time"),
                "time_source": sources[index] if index < len(sources) else None,
                "week_rule": dict(course.get("week_rule") or {}),
                "location": course.get("location"),
                "uncertain_fields": list(course.get("uncertain_fields") or []),
            }
        )
    return {
        "status": draft.get("status"),
        "semester_start_date": draft.get("semester_start_date"),
        "missing_context": list(structured.get("missing_context") or []),
        "courses": courses,
    }


class CourseScheduleTools:
    """Expose active drafts without ever accepting participant or import identity."""

    def __init__(
        self,
        imports: Any,
        presentations: Any = None,
        *,
        image_sessions: Any = None,
        recent_image_importer: Any = None,
    ) -> None:
        self.imports = imports
        self.presentations = presentations
        self.image_sessions = image_sessions
        self.recent_image_importer = recent_image_importer

    def register(self, registry: ToolRegistry) -> None:
        registry.register(
            "course_schedule_get_active_draft",
            "Read this participant's latest active course-schedule draft. Use it for schedule questions, status, and before selecting a course to correct. It never writes Calendar data.",
            _empty_schema(),
            self.get_active_draft,
            effect="read",
            authorization_requirement="none",
        )
        registry.register(
            "course_schedule_get_last_failure",
            "Read the latest retained course-schedule image failure for this participant and chat, including its stable error code and safe parse-report summary.",
            _empty_schema(),
            self.get_last_failure,
            effect="read",
            authorization_requirement="none",
        )
        registry.register(
            "course_schedule_import_from_recent_image",
            "Retry the latest retained participant-owned course-schedule image and create or resend its reviewed Preview. This never writes Calendar data.",
            _empty_schema(),
            self.import_from_recent_image,
            effect="internal_write",
            authorization_requirement="direct_request",
            authorization_context_resolver=(
                self.resolve_recent_image_import_authorization_context
            ),
        )
        registry.register(
            "course_schedule_update_active_draft",
            "Correct exactly one course in this participant's latest active draft and stage a refreshed fixed Preview card. The selector identifies the existing course; new_weekday is the replacement weekday. Use only for a direct correction request, never for a question or hypothetical. This never writes Calendar data.",
            {
                "type": "object",
                "properties": {
                    "selector": _selector_schema(),
                    "updates": _updates_schema(),
                },
                "required": ["selector", "updates"],
                "additionalProperties": False,
            },
            self.update_active_draft,
            effect="internal_write",
            authorization_requirement="direct_request",
        )
        registry.register(
            "course_schedule_cancel_pending_draft",
            "Cancel this participant's latest pending course-schedule draft. Use only when the user directly asks to cancel the pending import. This does not delete Calendar data.",
            _empty_schema(),
            self.cancel_pending_draft,
            effect="internal_write",
            authorization_requirement="direct_request",
            authorization_context_resolver=(
                self.resolve_pending_cancel_authorization_context
            ),
        )
        registry.register(
            "course_schedule_get_recent_imports",
            "Read this participant's recent course-schedule imports and their safe cancellation status. It never mutates Calendar data and never accepts an import id.",
            _empty_schema(),
            self.get_recent_imports,
            effect="read",
            authorization_requirement="none",
        )
        registry.register(
            "course_schedule_cancel_or_revert_import",
            "Cancel a pending/running course-schedule import or revert one completed/partially completed import. Resolve exactly one participant-owned import using latest, course_name, or created_date. This starts a durable cleanup Saga and never accepts a raw import id.",
            {
                "type": "object",
                "properties": {"selector": _cancel_selector_schema()},
                "required": ["selector"],
                "additionalProperties": False,
            },
            self.cancel_or_revert_import,
            effect="internal_write",
            authorization_requirement="direct_request",
            authorization_context_resolver=(
                self.resolve_cancel_or_revert_authorization_context
            ),
        )
        registry.register(
            "course_schedule_update_active_context",
            "Fill context for this participant's latest active schedule draft, such as the first Monday of the semester or a user-provided school period-time mapping. Use only for a direct correction or context-provision request. This never writes Calendar data.",
            {
                "type": "object",
                "properties": {
                    "semester_start_date": {"type": "string", "format": "date"},
                    "period_time_mapping": {
                        "type": "array",
                        "minItems": 1,
                        "maxItems": 30,
                        "items": {
                            "type": "object",
                            "properties": {
                                "period_start": {"type": "integer", "minimum": 1, "maximum": 30},
                                "period_end": {"type": "integer", "minimum": 1, "maximum": 30},
                                "start_time": {
                                    "type": "string",
                                    "pattern": "^(?:[01]\\d|2[0-3]):[0-5]\\d$",
                                },
                                "end_time": {
                                    "type": "string",
                                    "pattern": "^(?:[01]\\d|2[0-3]):[0-5]\\d$",
                                },
                            },
                            "required": [
                                "period_start",
                                "period_end",
                                "start_time",
                                "end_time",
                            ],
                            "additionalProperties": False,
                        },
                    },
                },
                "minProperties": 1,
                "additionalProperties": False,
            },
            self.update_active_context,
            effect="internal_write",
            authorization_requirement="direct_request",
        )

    def get_active_draft(
        self, ctx: AgentContext, _arguments: dict[str, Any]
    ) -> dict[str, Any]:
        draft = self.imports.drafts.latest_pending_context(ctx.participant_id)
        if draft is None:
            return {"ok": False, "error": "active_draft_not_found"}
        result = {"ok": True, "draft": _public_draft(draft)}
        image_session = self._latest_image_session(ctx)
        if image_session is not None:
            result["image_session"] = self._public_image_session(image_session)
        return result

    def get_last_failure(
        self, ctx: AgentContext, _arguments: dict[str, Any]
    ) -> dict[str, Any]:
        if self.image_sessions is None:
            return {"ok": False, "error": "image_session_unavailable"}
        failure = self.image_sessions.last_failure(
            ctx.participant_id, chat_id=ctx.chat_id
        )
        if failure is None:
            return {"ok": False, "error": "schedule_image_failure_not_found"}
        return {"ok": True, "image_session": self._public_image_session(failure)}

    async def import_from_recent_image(
        self, ctx: AgentContext, _arguments: dict[str, Any]
    ) -> dict[str, Any]:
        image_session = self._latest_image_session(ctx)
        if image_session is None:
            return {"ok": False, "error": "recent_schedule_image_not_found"}
        if not callable(self.recent_image_importer):
            return {"ok": False, "error": "schedule_image_import_unavailable"}
        result = self.recent_image_importer(ctx, image_session)
        if hasattr(result, "__await__"):
            result = await result
        return dict(result or {})

    def resolve_recent_image_import_authorization_context(
        self, ctx: AgentContext, _arguments: dict[str, Any]
    ) -> dict[str, Any]:
        image_session = self._latest_image_session(ctx)
        if image_session is None:
            raise AuthorizationContextResolutionError(
                "authorization_target_not_found"
            )
        return {
            "server_bound_participant_target": True,
            "operation_intent": "schedule_image_import",
            "has_provider_effect": False,
            "target": {
                "status": image_session.get("status"),
                "created_at": image_session.get("created_at"),
                "last_error_code": image_session.get("last_error_code"),
            },
        }

    def _latest_image_session(self, ctx: AgentContext) -> dict[str, Any] | None:
        if self.image_sessions is None:
            return None
        return self.image_sessions.latest(
            ctx.participant_id, chat_id=ctx.chat_id
        )

    @staticmethod
    def _public_image_session(value: dict[str, Any]) -> dict[str, Any]:
        report = dict(value.get("parse_report") or {})
        quarantined = []
        for item in list(report.get("quarantined") or [])[:10]:
            if isinstance(item, dict):
                quarantined.append(
                    {
                        "course_name": item.get("course_name"),
                        "reason": item.get("reason"),
                    }
                )
            else:
                quarantined.append({"course_name": None, "reason": str(item)})
        return {
            "status": value.get("status"),
            "created_at": value.get("created_at"),
            "updated_at": value.get("updated_at"),
            "last_error_code": value.get("last_error_code"),
            "error_detail": value.get("error_detail"),
            "parse_report": {
                "fixed_count": len(report.get("fixed") or []),
                "dropped_count": len(report.get("dropped") or []),
                "quarantined": quarantined,
                "missing": list(report.get("missing") or []),
            },
        }

    def update_active_draft(
        self, ctx: AgentContext, arguments: dict[str, Any]
    ) -> dict[str, Any]:
        draft = self.imports.drafts.latest_pending_context(ctx.participant_id)
        if draft is None:
            return {"ok": False, "error": "active_draft_not_found"}
        selector = dict(arguments.get("selector") or {})
        updates = dict(arguments.get("updates") or {})
        try:
            corrected = self.imports.drafts.apply_correction(
                ctx.participant_id,
                draft["id"],
                course_name=selector.get("course_name"),
                selector_weekday=selector.get("selector_weekday"),
                **updates,
            )
        except CourseCorrectionAmbiguityError as exc:
            return {
                "ok": False,
                "error": "ambiguous_course" if exc.candidates else "course_not_found",
                "candidates": list(exc.candidates),
            }
        except ValueError as exc:
            return {
                "ok": False,
                "error": "invalid_course_correction",
                "detail": str(exc)[:200],
            }
        if self.presentations is not None:
            self.presentations.stage_card(
                ctx.agent_run_id, course_schedule_preview_card(corrected)
            )
        return {"ok": True, "draft": _public_draft(corrected), "preview_staged": True}

    def cancel_pending_draft(
        self, ctx: AgentContext, _arguments: dict[str, Any]
    ) -> dict[str, Any]:
        draft = self.imports.drafts.latest_pending_context(ctx.participant_id)
        if draft is None:
            return {"ok": False, "error": "active_draft_not_found"}
        result = self.imports.cancel(ctx.participant_id, draft["id"])
        return {
            "ok": bool(result.get("ok")),
            "status": result.get("status"),
            "reply_text": result.get("reply_text"),
        }

    def resolve_pending_cancel_authorization_context(
        self, ctx: AgentContext, _arguments: dict[str, Any]
    ) -> dict[str, Any]:
        draft = self.imports.drafts.latest_pending_context(ctx.participant_id)
        if draft is None:
            raise AuthorizationContextResolutionError("authorization_target_not_found")
        return {
            "server_bound_participant_target": True,
            "operation_intent": "cancel_or_revert",
            "has_provider_effect": False,
            "target": {
                "status": draft.get("status"),
                "created_at": draft.get("created_at"),
                "course_names": [
                    str(course.get("course_name") or "")[:80]
                    for course in list(
                        (draft.get("structured_result") or {}).get("courses") or []
                    )[:10]
                ],
            },
        }

    def get_recent_imports(
        self, ctx: AgentContext, _arguments: dict[str, Any]
    ) -> dict[str, Any]:
        candidates = self.imports.drafts.recent_cancel_candidates(
            ctx.participant_id
        )
        return {
            "ok": True,
            "imports": [
                _public_import_candidate(candidate) for candidate in candidates
            ],
        }

    def cancel_or_revert_import(
        self, ctx: AgentContext, arguments: dict[str, Any]
    ) -> dict[str, Any]:
        selector = dict(arguments.get("selector") or {})
        try:
            result = self.imports.cancel_or_revert(
                ctx.participant_id,
                selector,
                status_card_chat_id=ctx.chat_id,
            )
        except CourseScheduleImportAmbiguityError as exc:
            return {
                "ok": False,
                "error": "ambiguous_import",
                "candidates": [
                    _public_import_candidate(candidate)
                    for candidate in exc.candidates
                ],
            }
        except LookupError:
            return {"ok": False, "error": "import_not_found"}
        except ValueError as exc:
            return {
                "ok": False,
                "error": "invalid_import_selector",
                "detail": str(exc)[:200],
            }
        return {
            "ok": bool(result.get("ok")),
            "status": result.get("status"),
            "cancel_mode": result.get("cancel_mode"),
            "already_cancelled": bool(result.get("already_cancelled")),
            "reply_text": result.get("reply_text"),
        }

    def resolve_cancel_or_revert_authorization_context(
        self, ctx: AgentContext, arguments: dict[str, Any]
    ) -> dict[str, Any]:
        selector = dict(arguments.get("selector") or {})
        try:
            candidate = self.imports.drafts.resolve_cancel_selector(
                ctx.participant_id, selector
            )
        except CourseScheduleImportAmbiguityError as exc:
            raise AuthorizationContextResolutionError(
                "authorization_target_ambiguous"
            ) from exc
        except (LookupError, ValueError) as exc:
            raise AuthorizationContextResolutionError(
                "authorization_target_not_found"
            ) from exc
        return {
            "server_bound_participant_target": True,
            "operation_intent": "cancel_or_revert",
            "has_provider_effect": bool(candidate.get("has_provider_effect")),
            "target": {
                "status": candidate.get("status"),
                "created_local_datetime": candidate.get(
                    "created_local_datetime"
                ),
                "created_local_date": candidate.get("created_local_date"),
                "timezone": candidate.get("timezone"),
                "course_names": list(candidate.get("course_names") or [])[:10],
            },
        }

    def update_active_context(
        self, ctx: AgentContext, arguments: dict[str, Any]
    ) -> dict[str, Any]:
        draft = self.imports.drafts.latest_pending_context(ctx.participant_id)
        if draft is None:
            return {"ok": False, "error": "active_draft_not_found"}
        try:
            semester_value = arguments.get("semester_start_date")
            semester_monday = (
                date.fromisoformat(str(semester_value))
                if semester_value is not None
                else None
            )
            entries = list(arguments.get("period_time_mapping") or [])
            mapping: dict[int | tuple[int, int], tuple[time, time]] = {}
            if entries:
                for entry in entries:
                    first = int(entry["period_start"])
                    last = int(entry["period_end"])
                    if last < first:
                        raise ValueError("period mapping range is reversed")
                    key: int | tuple[int, int] = first if first == last else (first, last)
                    start_clock = time.fromisoformat(str(entry["start_time"]))
                    end_clock = time.fromisoformat(str(entry["end_time"]))
                    if end_clock <= start_clock:
                        raise ValueError("period mapping time range is invalid")
                    mapping[key] = (start_clock, end_clock)
            draft = self.imports.drafts.apply_context_update(
                ctx.participant_id,
                draft["id"],
                semester_start_date=semester_monday,
                period_time_mapping=mapping or None,
            )
        except ValueError as exc:
            return {
                "ok": False,
                "error": "invalid_schedule_context",
                "detail": str(exc)[:200],
            }
        if self.presentations is not None:
            self.presentations.stage_card(
                ctx.agent_run_id, course_schedule_preview_card(draft)
            )
        return {"ok": True, "draft": _public_draft(draft), "preview_staged": True}
