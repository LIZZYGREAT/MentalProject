"""Typed personalization staging and deterministic CardAction execution."""

from __future__ import annotations

from typing import Any, Callable
import uuid


class PersonalizationProposalService:
    def __init__(
        self,
        proposals: Any,
        memory: Any,
        preferences: Any,
        care_preferences: Any = None,
        morning_brief_topics: Any = None,
        confirmed_effect_failpoint: Callable[[], None] | None = None,
    ) -> None:
        self.proposals = proposals
        self.memory = memory
        self.preferences = preferences
        self.care_preferences = care_preferences
        self.morning_brief_topics = morning_brief_topics
        self.confirmed_effect_failpoint = confirmed_effect_failpoint

    def stage_memory_remember(
        self,
        participant_id: uuid.UUID,
        *,
        memory_type: str,
        memory_subtype: str | None,
        content: str,
    ) -> dict[str, Any]:
        payload = self.memory.validate_explicit(
            memory_type=memory_type,
            memory_subtype=memory_subtype,
            content=content,
        )
        payload.pop("normalized_content", None)
        return self.proposals.stage(
            participant_id,
            domain="memory",
            operation="remember",
            payload=payload,
        )

    def stage_memory_target(
        self,
        participant_id: uuid.UUID,
        *,
        operation: str,
        memory_id: uuid.UUID | str,
        content: str | None = None,
    ) -> dict[str, Any] | None:
        target = next(
            (
                item
                for item in self.memory.list(participant_id)
                if item["id"] == str(memory_id)
            ),
            None,
        )
        if target is None:
            return None
        payload = {
            "memory_id": target["id"],
            "memory_type": target["memory_type"],
            "previous_content": target["content"],
        }
        if operation == "replace":
            validated = self.memory.validate_explicit(
                memory_type=target["memory_type"],
                memory_subtype=target.get("conflict_key"),
                content=str(content or ""),
            )
            payload["content"] = validated["content"]
        elif operation != "delete":
            raise ValueError("unsupported memory target operation")
        return self.proposals.stage(
            participant_id,
            domain="memory",
            operation=operation,
            payload=payload,
        )

    def stage_memory_clear(self, participant_id: uuid.UUID) -> dict[str, Any]:
        items = [
            {"memory_id": item["id"], "content": item["content"]}
            for item in self.memory.list(participant_id)
        ]
        return self.proposals.stage(
            participant_id,
            domain="memory",
            operation="clear",
            payload={"items": items, "item_count": len(items)},
        )

    def stage_preferences(
        self,
        participant_id: uuid.UUID,
        *,
        domain: str,
        style_changes: dict | None = None,
        support_changes: dict | None = None,
        identity_changes: dict | None = None,
        custom_rules: list[dict[str, str]] | None = None,
    ) -> dict[str, Any]:
        validated = self.preferences.validate_changes(
            style_changes=style_changes,
            support_changes=support_changes,
            identity_changes=identity_changes,
            custom_rules=custom_rules,
        )
        payload = {key: value for key, value in validated.items() if value}
        if not payload:
            raise ValueError("preference proposal has no changes")
        return self.proposals.stage(
            participant_id,
            domain=domain,
            operation="update",
            payload=payload,
        )

    def stage_preference_rule_delete(
        self, participant_id: uuid.UUID, *, rule_id: uuid.UUID | str
    ) -> dict[str, Any]:
        try:
            parsed_rule_id = uuid.UUID(str(rule_id))
        except ValueError as exc:
            raise ValueError("semantic rule id is invalid") from exc
        active = next(
            (
                item
                for item in list(
                    self.preferences.get(participant_id).get("semantic_rules") or []
                )
                if str(item.get("id")) == str(parsed_rule_id)
            ),
            None,
        )
        if active is None:
            raise LookupError("semantic rule not found")
        return self.proposals.stage(
            participant_id,
            domain="interaction_preferences",
            operation="delete_rule",
            payload={
                "rule_id": str(parsed_rule_id),
                "scope": str(active.get("scope") or ""),
                "instruction": str(active.get("instruction") or "")[:500],
            },
        )

    def stage_care_preferences(
        self,
        participant_id: uuid.UUID,
        changes: dict[str, Any],
    ) -> dict[str, Any]:
        if self.care_preferences is None:
            raise RuntimeError("care preference proposals are unavailable")
        validator = getattr(
            self.care_preferences, "validate_effective_changes", None
        ) or self.care_preferences.validate_changes
        validated = validator(participant_id, changes)
        return self.proposals.stage(
            participant_id,
            domain="care_preferences",
            operation="update",
            payload={"changes": validated},
        )

    def stage_morning_brief_topic(
        self,
        participant_id: uuid.UUID,
        *,
        operation: str,
        topic_label: str,
        query_hints: list[str] | tuple[str, ...] = (),
        source_kinds: list[str] | tuple[str, ...] = ("web",),
        priority: int = 0,
    ) -> dict[str, Any]:
        if self.morning_brief_topics is None:
            raise RuntimeError("morning brief topic preferences are unavailable")
        payload = self.morning_brief_topics.validate_topic_payload(
            topic_label=topic_label,
            query_hints=query_hints,
            source_kinds=source_kinds,
            priority=priority,
        )
        if operation not in {"add", "update", "remove"}:
            raise ValueError("unsupported morning brief topic operation")
        return self.proposals.stage(
            participant_id,
            domain="morning_brief_topics",
            operation=operation,
            payload=payload,
        )

    def resolve(
        self,
        participant_id: uuid.UUID,
        proposal_id: uuid.UUID | str,
        *,
        confirmed: bool,
    ) -> dict[str, Any]:
        if not confirmed:
            return self.proposals.cancel(participant_id, proposal_id)
        try:
            return self.proposals.resolve_confirmed_atomically(
                participant_id,
                proposal_id,
                execute=lambda session, proposal: self._execute_in_session(
                    session, participant_id, proposal
                ),
                before_terminal_status=self.confirmed_effect_failpoint,
            )
        except (LookupError, ValueError) as exc:
            self.proposals.fail_pending(
                participant_id, proposal_id, "personalization_effect_rejected"
            )
            return {
                "ok": False,
                "error": "personalization_effect_rejected",
                "detail": str(exc)[:200],
            }

    def _execute_in_session(
        self,
        session: Any,
        participant_id: uuid.UUID,
        proposal: dict[str, Any],
    ) -> dict[str, Any]:
        domain = str(proposal.get("domain") or "")
        operation = str(proposal.get("operation") or "")
        payload = dict(proposal.get("payload") or {})
        if domain == "memory":
            return self._execute_memory_in_session(
                session, participant_id, operation, payload
            )
        if domain in {
            "interaction_preferences", "support_preferences"
        } and operation == "update":
            validated = self.preferences.validate_changes(
                style_changes=payload.get("style_changes"),
                support_changes=payload.get("support_changes"),
                identity_changes=payload.get("identity_changes"),
                custom_rules=payload.get("custom_rules"),
            )
            if validated["custom_rules"]:
                validated["custom_rules"] = [
                    {
                        **item,
                        "source_proposal_id": uuid.UUID(str(proposal["id"])),
                    }
                    for item in validated["custom_rules"]
                ]
            self.preferences.repository.update_atomic_in_session(
                session,
                participant_id,
                style_changes=validated["style_changes"],
                support_changes=validated["support_changes"],
                identity_changes=validated["identity_changes"],
                custom_rules=validated["custom_rules"],
            )
            return {"operation": "update"}
        if (
            domain == "interaction_preferences"
            and operation == "delete_rule"
        ):
            return self.preferences.repository.delete_semantic_rule_in_session(
                session, participant_id, str(payload.get("rule_id") or "")
            )
        if (
            domain == "care_preferences"
            and operation == "update"
            and self.care_preferences is not None
        ):
            self.care_preferences.update_in_session(
                session, participant_id, dict(payload.get("changes") or {})
            )
            return {"operation": "care_update"}
        if (
            domain == "morning_brief_topics"
            and self.morning_brief_topics is not None
        ):
            result = self.morning_brief_topics.apply_in_session(
                session,
                participant_id,
                operation=operation,
                payload=payload,
            )
            return {"operation": "morning_brief_topic", **result}
        raise ValueError("unsupported personalization proposal")

    def _execute_memory_in_session(
        self,
        session: Any,
        participant_id: uuid.UUID,
        operation: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        repository = self.memory.repository
        if operation == "remember":
            validated = self.memory.validate_explicit(
                memory_type=str(payload.get("memory_type") or ""),
                memory_subtype=(
                    str(payload["memory_subtype"])
                    if payload.get("memory_subtype")
                    else None
                ),
                content=str(payload.get("content") or ""),
            )
            memory = repository.remember_in_session(
                session,
                participant_id,
                memory_type=str(validated["memory_type"]),
                content=str(validated["content"]),
                normalized_content=str(validated["normalized_content"]),
                conflict_key=validated.get("memory_subtype"),
                source="user_explicit",
                consent_basis="user_requested_memory",
                confidence=1.0,
            )
            return {"operation": "remember", "memory_id": memory["id"]}
        if operation == "replace":
            memory_id = uuid.UUID(str(payload.get("memory_id") or ""))
            active = repository.get_active_in_session(
                session, participant_id, memory_id
            )
            if active is None:
                raise LookupError("memory not found")
            validated = self.memory.validate_explicit(
                memory_type=str(active["memory_type"]),
                memory_subtype=active.get("conflict_key"),
                content=str(payload.get("content") or ""),
            )
            memory = repository.replace_in_session(
                session,
                participant_id,
                memory_id,
                content=str(validated["content"]),
                normalized_content=str(validated["normalized_content"]),
                conflict_key=validated.get("memory_subtype"),
            )
            if memory is None:
                raise LookupError("memory not found")
            return {"operation": "replace", "memory_id": memory["id"]}
        if operation == "delete":
            deleted = repository.delete_in_session(
                session,
                participant_id,
                uuid.UUID(str(payload.get("memory_id") or "")),
            )
            if not deleted:
                raise LookupError("memory not found")
            return {"operation": "delete", "deleted_count": 1}
        if operation == "clear":
            memory_ids = [
                uuid.UUID(str(dict(item).get("memory_id") or ""))
                for item in list(payload.get("items") or [])
            ]
            deleted = repository.clear_exact_in_session(
                session, participant_id, memory_ids
            )
            return {"operation": "clear", "deleted_count": deleted}
        raise ValueError("unsupported memory proposal")
