"""Tool contracts.

A tool is the only way the agent touches the world. Each tool declares:

* a stable ``name`` (used in prompts and in the audit log),
* a ``description`` the model reads,
* an ``input_schema`` (JSON Schema) so the model can call it correctly,
* a ``permission`` requirement, and
* a :class:`ToolResult` return value with an explicit error model.

Tools never raise for expected failures: they return ``ToolResult.ok == False``
with a user-safe message, so the agent can recover on the next iteration.
Infrastructure failures (permission denial, path escape) raise
:class:`~quantsmind_agent.errors.ToolError` subclasses and are converted to
failed results by :class:`~quantsmind_agent.runtime.tools.registry.ToolRegistry`.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from typing import Any, Protocol, runtime_checkable

from quantsmind_agent.errors import (
    ToolFailedError,
)
from quantsmind_agent.security.permissions import Permission

__all__ = ["Tool", "ToolResult", "ToolSpec", "validate_arguments"]


class ToolSpec:
    """Declarative metadata for one tool."""

    __slots__ = (
        "description",
        "guard",
        "input_schema",
        "name",
        "permission",
        "read_only",
        "summary_template",
    )

    def __init__(
        self,
        name: str,
        description: str,
        input_schema: dict[str, Any],
        permission: Permission,
        *,
        summary_template: str = "{target}",
        read_only: bool = True,
        guard: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        self.name = name
        self.description = description
        self.input_schema = input_schema
        self.permission = permission
        self.summary_template = summary_template
        self.read_only = read_only
        #: Optional pre-permission check, evaluated by the registry *before* the
        #: approval prompt. Use it for refusals that are not the user's to make:
        #: offering "Allow" for a command the application forbids would turn the
        #: dialog into a speed bump rather than a control.
        self.guard = guard

    def __repr__(self) -> str:  # pragma: no cover - trivial
        return f"ToolSpec(name={self.name!r}, permission={self.permission.value!r})"

    def __eq__(self, other: object) -> bool:
        return isinstance(other, ToolSpec) and other.name == self.name

    def __hash__(self) -> int:
        return hash(self.name)

    def to_openai_schema(self) -> dict[str, Any]:
        """Render as an OpenAI-compatible function tool definition."""

        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.input_schema,
            },
        }

    def summarize(self, arguments: dict[str, Any]) -> str:
        """Build the human-readable activity line for a call."""

        try:
            return self.summary_template.format(**{**arguments, "arguments": arguments})
        except (KeyError, IndexError, ValueError):
            return str(arguments.get("target") or arguments.get("path") or self.name)


class ToolResult:
    """Structured outcome of a tool invocation."""

    __slots__ = ("created_at", "data", "duration_ms", "error", "ok", "summary", "tool")

    def __init__(
        self,
        tool: str,
        ok: bool,
        summary: str,
        data: dict[str, Any] | None = None,
        error: str | None = None,
        duration_ms: float = 0.0,
        *,
        created_at: float | None = None,
    ) -> None:
        self.tool = tool
        self.ok = ok
        self.summary = summary
        self.data: dict[str, Any] = dict(data or {})
        self.error = error
        self.duration_ms = duration_ms
        # When the result was produced. Tool traffic is not kept in the message
        # list, so without this a restored transcript cannot say where a tool call
        # sat relative to the assistant messages around it.
        self.created_at = time.time() if created_at is None else created_at

    def __repr__(self) -> str:  # pragma: no cover - trivial
        state = "ok" if self.ok else f"error={self.error!r}"
        return f"ToolResult(tool={self.tool!r}, ok={self.ok}, {state})"

    @classmethod
    def success(
        cls,
        tool: str,
        summary: str,
        data: dict[str, Any] | None = None,
        *,
        duration_ms: float = 0.0,
        created_at: float | None = None,
    ) -> ToolResult:
        return cls(tool, True, summary, data, None, duration_ms, created_at=created_at)

    @classmethod
    def failure(
        cls,
        tool: str,
        summary: str,
        error: str,
        *,
        duration_ms: float = 0.0,
        created_at: float | None = None,
    ) -> ToolResult:
        return cls(tool, False, summary, None, error, duration_ms, created_at=created_at)

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "tool": self.tool,
            "ok": self.ok,
            "summary": self.summary,
            "created_at": self.created_at,
        }
        if self.data:
            payload["data"] = self.data
        if self.error:
            payload["error"] = self.error
        return payload

    @classmethod
    def from_dict(cls, payload: Any) -> ToolResult | None:
        """Rebuild a stored result, or ``None`` when it is not one.

        Used when a conversation is reopened: a result that cannot be rebuilt is
        dropped rather than shown as an empty line in the transcript.
        """

        if not isinstance(payload, dict):
            return None
        tool = str(payload.get("tool") or "")
        if not tool:
            return None
        data = payload.get("data")
        created_at = payload.get("created_at")
        return cls(
            tool,
            bool(payload.get("ok", False)),
            str(payload.get("summary") or ""),
            data if isinstance(data, dict) else None,
            str(payload["error"]) if payload.get("error") else None,
            float(payload.get("duration_ms") or 0.0),
            created_at=float(created_at) if isinstance(created_at, (int, float)) else None,
        )

    def to_observation(self, max_chars: int = 8000) -> str:
        """Render the result as text fed back to the model."""

        if not self.ok:
            return f"TOOL ERROR ({self.tool}): {self.error or self.summary}"
        text = json.dumps(self.data, indent=2, default=str, ensure_ascii=False)
        if len(text) > max_chars:
            text = f"{text[:max_chars]}\n... [truncated, {len(text)} characters total]"
        return f"TOOL RESULT ({self.tool}): {self.summary}\n{text}"


@runtime_checkable
class Tool(Protocol):
    """The contract every tool implements."""

    spec: ToolSpec

    def run(self, arguments: dict[str, Any]) -> ToolResult:
        """Execute the tool. Must not raise for expected failures."""


_TYPE_MAP: dict[str, tuple[type, ...]] = {
    "string": (str,),
    "number": (int, float),
    "integer": (int,),
    "boolean": (bool,),
    "array": (list, tuple),
    "object": (dict,),
}


def validate_arguments(tool_name: str, schema: dict[str, Any], arguments: dict[str, Any]) -> None:
    """Validate required keys and primitive types without a schema library."""

    properties: dict[str, Any] = schema.get("properties", {})
    required: list[str] = list(schema.get("required", []))

    missing = [key for key in required if key not in arguments]
    if missing:
        raise ToolFailedError(
            f"Tool {tool_name!r} is missing required argument(s): {', '.join(missing)}",
            user_message=(
                f"The agent called '{tool_name}' without the required "
                f"parameter(s): {', '.join(missing)}."
            ),
            details={"missing": missing},
        )

    for key, value in arguments.items():
        spec = properties.get(key)
        if not isinstance(spec, dict):
            continue
        expected = spec.get("type")
        if not isinstance(expected, str) or value is None:
            continue
        allowed = _TYPE_MAP.get(expected, ())
        if allowed == (int,) and isinstance(value, bool):
            # bool is a subclass of int, but a JSON boolean is never an integer.
            raise ToolFailedError(
                f"Tool {tool_name!r} argument {key!r} must be of type {expected}, not a boolean",
                user_message=(
                    f"The agent passed a boolean where tool '{tool_name}' expected an "
                    f"integer for '{key}'."
                ),
                details={"argument": key, "expected": expected},
            )
        if allowed and not isinstance(value, allowed):
            raise ToolFailedError(
                f"Tool {tool_name!r} argument {key!r} must be of type {expected}",
                user_message=(
                    f"The agent passed an invalid value for '{key}' in tool '{tool_name}'."
                ),
                details={"argument": key, "expected": expected},
            )


def coerce_arguments(tool_name: str, arguments: Any) -> dict[str, Any]:
    """Normalise model-supplied arguments into a plain dict."""

    if arguments is None:
        return {}
    if isinstance(arguments, dict):
        return {str(key): value for key, value in arguments.items()}
    if isinstance(arguments, str):
        try:
            parsed = json.loads(arguments)
        except json.JSONDecodeError as exc:
            raise ToolFailedError(
                f"Tool {tool_name!r} received malformed JSON arguments: {exc}",
                user_message="The agent produced invalid tool arguments. Please retry.",
            ) from exc
        if isinstance(parsed, dict):
            return {str(key): value for key, value in parsed.items()}
    raise ToolFailedError(
        f"Tool {tool_name!r} expects an object of arguments, got {type(arguments).__name__}",
        user_message="The agent produced invalid tool arguments. Please retry.",
    )


def redact_arguments(arguments: dict[str, Any]) -> dict[str, Any]:
    """Keep credential-like values out of the event stream."""

    from quantsmind_agent.diagnostics.logging import safe_mapping

    return safe_mapping(arguments)


def permission_target(name: str, arguments: dict[str, Any]) -> str:
    """The subject of a tool call, for permission rules and the approval dialog.

    A permission rule can match on this value with a glob, and the approval
    prompt shows it, so it has to name the thing that would actually change:
    the file for a filesystem tool, the search text for a search, and the whole
    command line for command execution.
    """

    if name == "run_command":
        command = arguments.get("command")
        extra = arguments.get("arguments")
        if isinstance(command, str) and isinstance(extra, list) and extra:
            return " ".join([command, *[str(item) for item in extra]])
        return str(command or "")
    for key in ("path", "pattern", "target"):
        value = arguments.get(key)
        if isinstance(value, str) and value:
            return value
    return ""


__all__ += ["coerce_arguments", "permission_target", "redact_arguments"]
