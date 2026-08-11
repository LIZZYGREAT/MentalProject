"""Explicit business-tool allowlist with strict JSON Schema validation."""

from __future__ import annotations

import inspect
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from jsonschema import Draft202012Validator

from app.agent.context import AgentContext
from app.repositories import AgentRunRepository


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


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    parameters: dict[str, Any]
    handler: ToolHandler


@dataclass(frozen=True)
class ToolExecution:
    result: dict[str, Any]
    status: str


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
    if depth > 3:
        return "[truncated]"
    if isinstance(value, dict):
        return {
            str(key): "[redacted]"
            if str(key).lower() in FORBIDDEN_FIELDS
            else _safe_summary(child, depth + 1)
            for key, child in list(value.items())[:30]
        }
    if isinstance(value, list):
        return [_safe_summary(item, depth + 1) for item in value[:20]]
    if isinstance(value, str):
        return value[:500]
    return value


class ToolRegistry:
    def __init__(self, runs: AgentRunRepository | None = None):
        self._tools: dict[str, ToolSpec] = {}
        self.runs = runs

    def register(
        self,
        name: str,
        description: str,
        parameters: dict[str, Any],
        handler: ToolHandler,
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
        self._tools[name] = ToolSpec(name, description, schema, handler)

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
            self._log(ctx, name, None, result, "invalid_tool")
            return ToolExecution(result, "invalid_tool")
        if not isinstance(arguments, dict):
            result = {"ok": False, "error": "invalid_arguments"}
            self._log(ctx, name, None, result, "invalid_arguments")
            return ToolExecution(result, "invalid_arguments")
        errors = sorted(
            Draft202012Validator(spec.parameters).iter_errors(arguments),
            key=lambda error: list(error.path),
        )
        if errors:
            result = {
                "ok": False,
                "error": "invalid_arguments",
                "detail": errors[0].message[:300],
            }
            self._log(ctx, name, arguments, result, "invalid_arguments")
            return ToolExecution(result, "invalid_arguments")
        try:
            value = spec.handler(ctx, arguments)
            if inspect.isawaitable(value):
                value = await value
            result = value if isinstance(value, dict) else {"value": value}
            safe = _safe_summary(result)
            self._log(ctx, name, arguments, safe, "succeeded")
            return ToolExecution(safe, "succeeded")
        except Exception:
            result = {"ok": False, "error": "tool_exception"}
            self._log(ctx, name, arguments, result, "tool_exception")
            return ToolExecution(result, "tool_exception")

    def _log(
        self,
        ctx: AgentContext,
        name: str,
        arguments: dict[str, Any] | None,
        result: dict[str, Any],
        status: str,
    ) -> None:
        if self.runs is not None:
            self.runs.tool_call(
                ctx.agent_run_id,
                name,
                _safe_summary(arguments) if arguments is not None else None,
                _safe_summary(result),
                status,
            )
