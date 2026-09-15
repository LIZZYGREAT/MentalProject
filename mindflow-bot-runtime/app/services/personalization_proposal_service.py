"""Typed personalization staging and deterministic CardAction execution."""

from __future__ import annotations

from typing import Any
import uuid


class PersonalizationProposalService:
    def __init__(self, proposals: Any, memory: Any, preferences: Any) -> None:
        self.proposals = proposals
        self.memory = memory
        self.preferences = preferences

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
    ) -> dict[str, Any]:
        validated = self.preferences.validate_changes(
            style_changes=style_changes,
            support_changes=support_changes,
            identity_changes=identity_changes,
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

    def resolve(
        self,
        participant_id: uuid.UUID,
        proposal_id: uuid.UUID | str,
        *,
        confirmed: bool,
    ) -> dict[str, Any]:
        claim = self.proposals.claim(
            participant_id, proposal_id, confirmed=confirmed
        )
        if not claim.get("ok") or not confirmed:
            return claim
        proposal = dict(claim["proposal"])
        payload = dict(proposal.get("payload") or {})
        try:
            if proposal["domain"] == "memory":
                result = self._execute_memory(
                    participant_id, proposal["operation"], payload
                )
            elif proposal["domain"] in {
                "interaction_preferences", "support_preferences"
            } and proposal["operation"] == "update":
                preferences = self.preferences.update_preferences(
                    participant_id,
                    style_changes=payload.get("style_changes"),
                    support_changes=payload.get("support_changes"),
                    identity_changes=payload.get("identity_changes"),
                )
                result = {"operation": "update", "preferences": preferences}
            else:
                raise ValueError("unsupported personalization proposal")
        except (LookupError, ValueError) as exc:
            self.proposals.fail(
                participant_id, proposal_id, "personalization_effect_rejected"
            )
            return {
                "ok": False,
                "error": "personalization_effect_rejected",
                "detail": str(exc)[:200],
            }
        self.proposals.complete(participant_id, proposal_id, result)
        return {"ok": True, "status": "confirmed", **result}

    def _execute_memory(
        self, participant_id: uuid.UUID, operation: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        if operation == "remember":
            memory = self.memory.remember_explicit(
                participant_id,
                memory_type=str(payload.get("memory_type") or ""),
                memory_subtype=(
                    str(payload["memory_subtype"])
                    if payload.get("memory_subtype")
                    else None
                ),
                content=str(payload.get("content") or ""),
            )
            return {"operation": "remember", "memory_id": memory["id"]}
        if operation == "replace":
            memory = self.memory.replace(
                participant_id,
                uuid.UUID(str(payload.get("memory_id") or "")),
                content=str(payload.get("content") or ""),
            )
            if memory is None:
                raise LookupError("memory not found")
            return {"operation": "replace", "memory_id": memory["id"]}
        if operation == "delete":
            deleted = self.memory.delete(
                participant_id, uuid.UUID(str(payload.get("memory_id") or ""))
            )
            if not deleted:
                raise LookupError("memory not found")
            return {"operation": "delete", "deleted_count": 1}
        if operation == "clear":
            deleted = 0
            for item in list(payload.get("items") or []):
                if self.memory.delete(
                    participant_id,
                    uuid.UUID(str(dict(item).get("memory_id") or "")),
                ):
                    deleted += 1
            return {"operation": "clear", "deleted_count": deleted}
        raise ValueError("unsupported memory proposal")
