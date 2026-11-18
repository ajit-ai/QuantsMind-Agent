"""Tool registry: the sole agent enforcement point.

The registry validates arguments, applies application-owned guards, asks the
permission broker, runs the tool and emits events. Tools never check permissions
themselves, so a mis-annotated tool cannot silently bypass policy.
"""

from __future__ import annotations

import time
from typing import Any

from quantsmind_agent.diagnostics.logging import get_logger
from quantsmind_agent.errors import (
    PermissionDeniedError,
    QuantsMindError,
    ToolNotFoundError,
)
from quantsmind_agent.events import AgentEventType, EventBus
from quantsmind_agent.runtime.tools.contracts import (
    Tool,
    ToolResult,
    ToolSpec,
    coerce_arguments,
    permission_target,
    redact_arguments,
    validate_arguments,
)
from quantsmind_agent.security.permissions import PermissionBroker, PermissionRequest

__all__ = ["ToolRegistry"]

logger = get_logger(__name__)


class ToolRegistry:
    """Name-to-tool registry that enforces permissions and emits events."""

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
            arguments = coerce_arguments(name, raw_arguments)
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
            {"tool": name, "summary": summary, "arguments": redact_arguments(arguments)},
        )

        # Refusals the application owns are settled before anyone is asked. The
        # registry remains the enforcement point either way: a guard can only
        # refuse, never allow, so it can never widen a permission.
        if spec.guard is not None:
            try:
                spec.guard(arguments)
            except QuantsMindError as exc:
                logger.info("Tool %s refused by policy: %s", name, exc.message)
                self._emit(
                    AgentEventType.TOOL_FAILED,
                    {"tool": name, "summary": summary, "error": exc.user_message},
                )
                return ToolResult.failure(name, summary, exc.user_message)

        request = PermissionRequest(
            permission=spec.permission,
            tool=name,
            summary=summary,
            target=permission_target(name, arguments),
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
