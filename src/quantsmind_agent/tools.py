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
failed results by :meth:`ToolRegistry.execute`.
"""

from __future__ import annotations

import json
import time
from typing import Any, Protocol, runtime_checkable

from quantsmind_agent.errors import (
    PermissionDeniedError,
    QuantsMindError,
    ToolFailedError,
    ToolNotFoundError,
)
from quantsmind_agent.events import AgentEventType, EventBus
from quantsmind_agent.logging_setup import get_logger
from quantsmind_agent.permissions import Permission, PermissionBroker, PermissionRequest

__all__ = ["Tool", "ToolRegistry", "ToolResult", "ToolSpec"]

logger = get_logger(__name__)


class ToolSpec:
    """Declarative metadata for one tool."""

    __slots__ = (
        "description",
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
    ) -> None:
        self.name = name
        self.description = description
        self.input_schema = input_schema
        self.permission = permission
        self.summary_template = summary_template
        self.read_only = read_only

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

    __slots__ = ("data", "duration_ms", "error", "ok", "summary", "tool")

    def __init__(
        self,
        tool: str,
        ok: bool,
        summary: str,
        data: dict[str, Any] | None = None,
        error: str | None = None,
        duration_ms: float = 0.0,
    ) -> None:
        self.tool = tool
        self.ok = ok
        self.summary = summary
        self.data: dict[str, Any] = dict(data or {})
        self.error = error
        self.duration_ms = duration_ms

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
    ) -> ToolResult:
        return cls(tool, True, summary, data, None, duration_ms)

    @classmethod
    def failure(
        cls,
        tool: str,
        summary: str,
        error: str,
        *,
        duration_ms: float = 0.0,
    ) -> ToolResult:
        return cls(tool, False, summary, None, error, duration_ms)

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"tool": self.tool, "ok": self.ok, "summary": self.summary}
        if self.data:
            payload["data"] = self.data
        if self.error:
            payload["error"] = self.error
        return payload

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


def _coerce_arguments(tool_name: str, arguments: Any) -> dict[str, Any]:
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


class ToolRegistry:
    """Name-to-tool registry that enforces permissions and emits events.

    The registry is the enforcement point: tools themselves never check
    permissions, so a mis-annotated tool cannot silently bypass policy.
    """

    def __init__(
        self,
        *,
        permissions: PermissionBroker | None = None,
        event_bus: EventBus | None = None,
        session_id: str = "",
    ) -> None:
        self._tools: dict[str, Tool] = {}
        self._permissions = permissions or PermissionBroker()
        self._events = event_bus or EventBus()
        self._session_id = session_id

    @property
    def permissions(self) -> PermissionBroker:
        return self._permissions

    # -- registration -----------------------------------------------------
    def register(self, tool: Tool) -> None:
        name = tool.spec.name
        if name in self._tools:
            raise ValueError(f"Tool {name!r} is already registered")
        self._tools[name] = tool

    def register_all(self, tools: Any) -> None:
        for tool in tools:
            self.register(tool)

    def get(self, name: str) -> Tool:
        try:
            return self._tools[name]
        except KeyError as exc:
            raise ToolNotFoundError(
                f"Unknown tool: {name!r}",
                user_message=f"The agent requested an unknown tool '{name}'.",
                details={"available": sorted(self._tools)},
            ) from exc

    @property
    def names(self) -> list[str]:
        return sorted(self._tools)

    def __contains__(self, name: object) -> bool:
        return isinstance(name, str) and name in self._tools

    def __len__(self) -> int:
        return len(self._tools)

    def specs(self) -> list[ToolSpec]:
        return [self._tools[name].spec for name in self.names]

    def openai_schemas(self) -> list[dict[str, Any]]:
        """Tool definitions in the OpenAI function-calling format."""

        return [spec.to_openai_schema() for spec in self.specs()]

    # -- execution --------------------------------------------------------
    def _emit(self, event_type: AgentEventType, payload: dict[str, Any]) -> None:
        self._events.emit(event_type, payload, session_id=self._session_id)

    def execute(self, name: str, raw_arguments: Any) -> ToolResult:
        """Run tool ``name``, enforcing permissions and emitting events.

        Returns a :class:`ToolResult` for both success and expected failure.
        Only a genuinely unknown tool raises (:class:`ToolNotFoundError`).
        """

        tool = self.get(name)
        spec = tool.spec
        started = time.perf_counter()

        try:
            arguments = _coerce_arguments(name, raw_arguments)
            validate_arguments(name, spec.input_schema, arguments)
        except QuantsMindError as exc:
            logger.debug("Rejected arguments for tool %s: %s", name, exc.message)
            summary = spec.summarize(raw_arguments if isinstance(raw_arguments, dict) else {})
            self._emit(
                AgentEventType.TOOL_FAILED,
                {"tool": name, "summary": summary, "error": exc.user_message},
            )
            return ToolResult.failure(name, summary, exc.user_message)

        summary = spec.summarize(arguments)
        self._emit(
            AgentEventType.TOOL_REQUESTED,
            {"tool": name, "summary": summary, "arguments": _redact_arguments(arguments)},
        )

        request = PermissionRequest(
            permission=spec.permission,
            tool=name,
            summary=summary,
            target=_permission_target(name, arguments),
            details=dict(arguments),
        )
        try:
            self._permissions.require(request)
        except PermissionDeniedError as exc:
            logger.info("Permission denied for tool %s", name)
            self._emit(
                AgentEventType.PERMISSION_DECISION,
                {"tool": name, "summary": summary, "decision": "DENY", "reason": exc.message},
            )
            self._emit(
                AgentEventType.TOOL_FAILED,
                {"tool": name, "summary": summary, "error": exc.user_message},
            )
            return ToolResult.failure(name, summary, exc.user_message)

        self._emit(
            AgentEventType.PERMISSION_DECISION,
            {"tool": name, "summary": summary, "decision": "ALLOW"},
        )
        self._emit(AgentEventType.TOOL_STARTED, {"tool": name, "summary": summary})

        try:
            result = tool.run(arguments)
        except QuantsMindError as exc:
            elapsed = (time.perf_counter() - started) * 1000
            logger.warning("Tool %s failed: %s", name, exc.message)
            self._emit(
                AgentEventType.TOOL_FAILED,
                {"tool": name, "summary": summary, "error": exc.user_message},
            )
            return ToolResult.failure(name, summary, exc.user_message, duration_ms=elapsed)
        except Exception:
            elapsed = (time.perf_counter() - started) * 1000
            logger.exception("Unhandled error in tool %s", name)
            self._emit(
                AgentEventType.TOOL_FAILED,
                {"tool": name, "summary": summary, "error": f"Internal error in tool '{name}'."},
            )
            return ToolResult.failure(
                name,
                summary,
                f"Internal error in tool '{name}'.",
                duration_ms=elapsed,
            )

        elapsed = (time.perf_counter() - started) * 1000
        result.duration_ms = elapsed
        if result.ok:
            self._emit(
                AgentEventType.TOOL_COMPLETED,
                {
                    "tool": name,
                    "summary": result.summary,
                    "duration_ms": round(elapsed, 1),
                },
            )
        else:
            self._emit(
                AgentEventType.TOOL_FAILED,
                {
                    "tool": name,
                    "summary": result.summary,
                    "error": result.error or "unknown tool error",
                    "duration_ms": round(elapsed, 1),
                },
            )
        return result


def _permission_target(name: str, arguments: dict[str, Any]) -> str:
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


def _redact_arguments(arguments: dict[str, Any]) -> dict[str, Any]:
    """Keep credential-like values out of the event stream."""

    from quantsmind_agent.logging_setup import safe_mapping

    return safe_mapping(arguments)
