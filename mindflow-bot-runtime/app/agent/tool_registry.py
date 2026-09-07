"""Explicit business-tool allowlist with strict JSON Schema validation."""

from __future__ import annotations

import asyncio
import inspect
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Literal, Mapping

from jsonschema import Draft202012Validator, FormatChecker

from app.agent.context import AgentContext, CalendarMutationOperation, TurnEffectPolicy
from app.repositories import AgentRunRepository
from app.services.mutation_intent_verifier import (
    MutationIntentVerifier,
    redact_sensitive_text,
)


FORBIDDEN_FIELDS = {
    "participant_id",
    "user_id",
    "open_id",
    "chat_id",
    "access_token",
    "refresh_token",
    "app_secret",
    "secret",
    "token",
    "sql",
    "path",
    "url",
}

ToolHandler = Callable[[AgentContext, dict[str, Any]], Any | Awaitable[Any]]
AuthorizationContextResolver = Callable[
    [AgentContext, dict[str, Any]],
    dict[str, Any] | Awaitable[dict[str, Any]],
]
ToolEffect = Literal[
    "read",
    "compute",
    "ui_effect",
    "internal_write",
    "external_write",
    "destructive_external_write",
]
AuthorizationRequirement = Literal[
    "none",
    "direct_request",
    "explicit_destructive_request",
]

STATE_CHANGING_EFFECTS = frozenset(
    {"internal_write", "external_write", "destructive_external_write"}
)

_ALLOWED_EFFECTS_BY_TURN_POLICY: dict[
    TurnEffectPolicy, frozenset[ToolEffect]
] = {
    "verify_on_demand": frozenset(
        {
            "read",
            "compute",
            "ui_effect",
            "internal_write",
            "external_write",
            "destructive_external_write",
        }
    ),
    "read_compute_only": frozenset({"read", "compute"}),
    "deterministic_backend_action": frozenset(),
}

CALENDAR_MUTATION_TOOLS: dict[str, CalendarMutationOperation] = {
    "calendar_create_event": "create",
    "calendar_update_event": "update",
    "calendar_delete_event": "delete",
}


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    parameters: dict[str, Any]
    effect: ToolEffect
    authorization_requirement: AuthorizationRequirement
    handler: ToolHandler
    execution_mode: Literal["async", "sync_io"]
    authorization_context_resolver: AuthorizationContextResolver | None = None


@dataclass(frozen=True)
class ToolExecution:
    result: dict[str, Any]
    status: str


class AuthorizationContextResolutionError(RuntimeError):
    """A backend authorization target could not be safely resolved."""

    def __init__(self, reason_code: str) -> None:
        self.reason_code = str(reason_code)[:80]
        super().__init__(self.reason_code)


def _schema_fields(value: Any) -> set[str]:
    found: set[str] = set()
    if isinstance(value, dict):
        for key, child in value.items():
            if key == "properties" and isinstance(child, dict):
                found.update(str(name) for name in child)
            found.update(_schema_fields(child))
    elif isinstance(value, list):
        for child in value:
            found.update(_schema_fields(child))
    return found


def _safe_summary(value: Any, depth: int = 0) -> Any:
    if depth > 5:
        return "[truncated]"
    if isinstance(value, dict):
        return {
            str(key): "[redacted]"
            if str(key).lower() in FORBIDDEN_FIELDS
            or str(key).lower().endswith("_id")
            else _safe_summary(child, depth + 1)
            for key, child in list(value.items())[:30]
        }
    if isinstance(value, list):
        return [_safe_summary(item, depth + 1) for item in value[:20]]
    if isinstance(value, str):
        return redact_sensitive_text(value, max_length=500)
    return value


def _safe_authorization_context(value: Any, depth: int = 0) -> Any:
    """Remove identities and provider fields from external verifier context."""

    if depth > 3:
        return "[truncated]"
    if isinstance(value, dict):
        return {
            str(key): _safe_authorization_context(child, depth + 1)
            for key, child in list(value.items())[:30]
            if str(key).lower() not in FORBIDDEN_FIELDS
            and not str(key).lower().endswith("_id")
            and "provider" not in str(key).lower()
        }
    if isinstance(value, list):
        return [_safe_authorization_context(item, depth + 1) for item in value[:20]]
    if isinstance(value, str):
        return redact_sensitive_text(value, max_length=500)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return redact_sensitive_text(value, max_length=500)


def _verifier_proposal_summary(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """Describe proposal scope without sending provider/database identifiers."""

    exact_target = bool(arguments.get("event_id")) if name in {
        "calendar_update_event",
        "calendar_delete_event",
    } else None
    semantic_arguments = {
        str(key): _safe_summary(value)
        for key, value in arguments.items()
        if str(key).lower() not in FORBIDDEN_FIELDS
        and not str(key).lower().endswith("_id")
    }
    summary: dict[str, Any] = {"proposed_operation": str(name)[:128]}
    if exact_target is not None:
        summary["exact_target_supplied"] = exact_target
    if semantic_arguments:
        summary["requested_values"] = semantic_arguments
    return summary


def _verifier_semantic_turns(ctx: AgentContext) -> tuple[dict[str, str], ...]:
    turns: list[dict[str, str]] = []
    for item in ctx.authorization_semantic_context[-4:]:
        if isinstance(item, Mapping):
            role = item.get("role")
            value = item.get("text")
        else:
            role = getattr(item, "role", "")
            value = getattr(item, "text", "")
        turns.append({"role": str(role), "text": str(value)})
    return tuple(turns)


class ToolRegistry:
    def __init__(
        self,
        runs: AgentRunRepository | None = None,
        *,
        mutation_verifier: MutationIntentVerifier | None = None,
        sync_max_concurrency: int = 8,
    ):
        self._tools: dict[str, ToolSpec] = {}
        self.runs = runs
        self.mutation_verifier = mutation_verifier
        self._sync_slots = asyncio.Semaphore(max(1, int(sync_max_concurrency)))

    def register(
        self,
        name: str,
        description: str,
        parameters: dict[str, Any],
        handler: ToolHandler,
        *,
        effect: ToolEffect,
        authorization_requirement: AuthorizationRequirement,
        execution_mode: Literal["async", "sync_io"] | None = None,
        authorization_context_resolver: AuthorizationContextResolver | None = None,
    ) -> None:
        if name in self._tools:
            raise ValueError(f"duplicate tool: {name}")
        forbidden = _schema_fields(parameters) & FORBIDDEN_FIELDS
        if forbidden:
            raise ValueError(f"tool schema contains forbidden identity fields: {sorted(forbidden)}")
        schema = dict(parameters)
        if schema.get("type") != "object" or schema.get("additionalProperties") is not False:
            raise ValueError("tool schema must be an object with additionalProperties=false")
        Draft202012Validator.check_schema(schema)
        mode = execution_mode or (
            "async" if inspect.iscoroutinefunction(handler) else "sync_io"
        )
        if mode not in {"async", "sync_io"}:
            raise ValueError("execution_mode must be async or sync_io")
        if effect not in {
            "read",
            "compute",
            "ui_effect",
            "internal_write",
            "external_write",
            "destructive_external_write",
        }:
            raise ValueError("invalid tool effect")
        if authorization_requirement not in {
            "none",
            "direct_request",
            "explicit_destructive_request",
        }:
            raise ValueError("invalid authorization requirement")
        if effect in STATE_CHANGING_EFFECTS:
            expected = (
                "explicit_destructive_request"
                if effect == "destructive_external_write"
                else "direct_request"
            )
            if authorization_requirement != expected:
                raise ValueError(
                    f"{effect} requires authorization_requirement={expected}"
                )
        elif authorization_requirement != "none":
            raise ValueError(f"{effect} requires authorization_requirement=none")
        if (
            authorization_context_resolver is not None
            and effect not in STATE_CHANGING_EFFECTS
        ):
            raise ValueError(
                "authorization_context_resolver is only valid for state-changing tools"
            )
        self._tools[name] = ToolSpec(
            name,
            description,
            schema,
            effect,
            authorization_requirement,
            handler,
            mode,
            authorization_context_resolver,
        )

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(self._tools)

    @property
    def specs(self) -> tuple[ToolSpec, ...]:
        return tuple(self._tools.values())

    def schemas(self) -> list[dict[str, Any]]:
        return [
            {
                "type": "function",
                "function": {
                    "name": spec.name,
                    "description": spec.description,
                    "parameters": spec.parameters,
                },
            }
            for spec in self._tools.values()
        ]

    async def execute(
        self, ctx: AgentContext, name: str, arguments: Any
    ) -> ToolExecution:
        spec = self._tools.get(str(name))
        if spec is None:
            result = {"ok": False, "error": "invalid_tool"}
            await self._log(
                ctx,
                name,
                None,
                None,
                result,
                "invalid_tool",
                "not_evaluated",
                "invalid_tool",
            )
            return ToolExecution(result, "invalid_tool")
        if not isinstance(arguments, dict):
            result = {"ok": False, "error": "invalid_arguments"}
            await self._log(
                ctx,
                name,
                spec,
                None,
                result,
                "invalid_arguments",
                "not_evaluated",
                "arguments_not_object",
            )
            return ToolExecution(result, "invalid_arguments")
        errors = sorted(
            Draft202012Validator(
                spec.parameters, format_checker=FormatChecker()
            ).iter_errors(arguments),
            key=lambda error: list(error.path),
        )
        if errors:
            result = {
                "ok": False,
                "error": "invalid_arguments",
                "detail": errors[0].message[:300],
            }
            await self._log(
                ctx,
                name,
                spec,
                arguments,
                result,
                "invalid_arguments",
                "not_evaluated",
                "schema_validation_failed",
            )
            return ToolExecution(result, "invalid_arguments")

        allowed_effects = _ALLOWED_EFFECTS_BY_TURN_POLICY.get(
            ctx.turn_effect_policy, frozenset()
        )
        if spec.effect not in allowed_effects:
            reason_code = (
                "read_compute_only"
                if ctx.turn_effect_policy == "read_compute_only"
                else "deterministic_action_outside_agent_registry"
            )
            result = {
                "ok": False,
                "error": "tool_effect_not_authorized",
                "reason_code": reason_code,
            }
            await self._log(
                ctx,
                name,
                spec,
                arguments,
                result,
                "tool_effect_not_authorized",
                "deny",
                reason_code,
            )
            return ToolExecution(result, "tool_effect_not_authorized")

        authorization_decision = "not_required"
        reason_code = "authorization_not_required"
        if spec.effect in STATE_CHANGING_EFFECTS:
            proposal_summary = _verifier_proposal_summary(name, arguments)
            if spec.authorization_context_resolver is not None:
                try:
                    resolver = spec.authorization_context_resolver
                    if inspect.iscoroutinefunction(resolver):
                        authorization_context = resolver(ctx, arguments)
                    else:
                        async with self._sync_slots:
                            authorization_context = await asyncio.to_thread(
                                resolver, ctx, arguments
                            )
                    if inspect.isawaitable(authorization_context):
                        authorization_context = await authorization_context
                    if not isinstance(authorization_context, dict):
                        raise AuthorizationContextResolutionError(
                            "invalid_authorization_context"
                        )
                    safe_context = _safe_authorization_context(authorization_context)
                    if not isinstance(safe_context, dict) or not safe_context:
                        raise AuthorizationContextResolutionError(
                            "authorization_target_not_found"
                        )
                    if set(safe_context) & set(proposal_summary):
                        raise AuthorizationContextResolutionError(
                            "invalid_authorization_context"
                        )
                    proposal_summary.update(safe_context)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    reason_code = (
                        exc.reason_code
                        if isinstance(exc, AuthorizationContextResolutionError)
                        else "authorization_context_failure"
                    )
                    result = {
                        "ok": False,
                        "error": "mutation_authorization_unavailable",
                        "reason_code": reason_code,
                    }
                    await self._log(
                        ctx,
                        name,
                        spec,
                        arguments,
                        result,
                        "authorization_unavailable",
                        "unavailable",
                        reason_code,
                    )
                    return ToolExecution(result, "authorization_unavailable")
            if self.mutation_verifier is None:
                result = {
                    "ok": False,
                    "error": "mutation_authorization_unavailable",
                    "reason_code": "verifier_unavailable",
                }
                await self._log(
                    ctx,
                    name,
                    spec,
                    arguments,
                    result,
                    "authorization_unavailable",
                    "unavailable",
                    "verifier_unavailable",
                )
                return ToolExecution(result, "authorization_unavailable")
            try:
                decision = await self.mutation_verifier.verify(
                    user_request_text=ctx.user_request_text,
                    tool_name=spec.name,
                    tool_effect=spec.effect,
                    authorization_requirement=spec.authorization_requirement,
                    proposal_summary=proposal_summary,
                    source_kind=ctx.source_kind,
                    semantic_turn_context=_verifier_semantic_turns(ctx),
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                result = {
                    "ok": False,
                    "error": "mutation_authorization_unavailable",
                    "reason_code": "verifier_failure",
                }
                await self._log(
                    ctx,
                    name,
                    spec,
                    arguments,
                    result,
                    "authorization_unavailable",
                    "unavailable",
                    "verifier_failure",
                )
                return ToolExecution(result, "authorization_unavailable")
            authorization_decision = decision.decision
            reason_code = decision.reason_code
            if decision.decision == "deny":
                result = {
                    "ok": False,
                    "error": "tool_effect_not_authorized",
                    "reason_code": decision.reason_code,
                }
                await self._log(
                    ctx,
                    name,
                    spec,
                    arguments,
                    result,
                    "tool_effect_not_authorized",
                    authorization_decision,
                    reason_code,
                )
                return ToolExecution(result, "tool_effect_not_authorized")
            if decision.decision == "needs_clarification":
                result = {
                    "ok": False,
                    "error": "mutation_needs_clarification",
                    "reason_code": decision.reason_code,
                }
                await self._log(
                    ctx,
                    name,
                    spec,
                    arguments,
                    result,
                    "mutation_needs_clarification",
                    authorization_decision,
                    reason_code,
                )
                return ToolExecution(result, "mutation_needs_clarification")

        calendar_operation = CALENDAR_MUTATION_TOOLS.get(name)
        if (
            calendar_operation is not None
            and not ctx.allows_calendar_mutation(calendar_operation)
        ):
            result = {
                "ok": False,
                "error": "calendar_mutation_not_authorized",
                "reason_code": "calendar_operation_not_allowed",
            }
            await self._log(
                ctx,
                name,
                spec,
                arguments,
                result,
                "calendar_mutation_not_authorized",
                "deny",
                "calendar_operation_not_allowed",
            )
            return ToolExecution(result, "calendar_mutation_not_authorized")
        try:
            if spec.execution_mode == "async":
                value = spec.handler(ctx, arguments)
            else:
                async with self._sync_slots:
                    value = await asyncio.to_thread(spec.handler, ctx, arguments)
            if inspect.isawaitable(value):
                value = await value
            result = value if isinstance(value, dict) else {"value": value}
            safe = _safe_summary(result)
            await self._log(
                ctx,
                name,
                spec,
                arguments,
                safe,
                "succeeded",
                authorization_decision,
                reason_code,
            )
            return ToolExecution(safe, "succeeded")
        except Exception:
            result = {"ok": False, "error": "tool_exception"}
            await self._log(
                ctx,
                name,
                spec,
                arguments,
                result,
                "tool_exception",
                authorization_decision,
                reason_code,
            )
            return ToolExecution(result, "tool_exception")

    async def _log(
        self,
        ctx: AgentContext,
        name: str,
        spec: ToolSpec | None,
        arguments: dict[str, Any] | None,
        result: dict[str, Any],
        status: str,
        authorization_decision: str,
        reason_code: str,
    ) -> None:
        if self.runs is not None:
            audit = {
                "tool": str(name)[:128],
                "effect": spec.effect if spec is not None else None,
                "authorization_requirement": (
                    spec.authorization_requirement if spec is not None else None
                ),
                "authorization_decision": str(authorization_decision)[:64],
                "reason_code": str(reason_code)[:80],
                "status": str(status)[:32],
            }
            async with self._sync_slots:
                await asyncio.to_thread(
                    self.runs.tool_call,
                    ctx.agent_run_id,
                    name,
                    {
                        **audit,
                        "arguments": (
                            _safe_summary(arguments)
                            if arguments is not None
                            else None
                        ),
                    },
                    {**audit, "result": _safe_summary(result)},
                    status,
                )
