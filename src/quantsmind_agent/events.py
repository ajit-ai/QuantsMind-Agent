"""Structured agent events and a synchronous in-process event bus.

The Agent Core, tool registry, model gateway and permission layer publish
:class:`AgentEvent` instances. The GUI subscribes to the bus and renders them
as chat messages, tool activity rows and status-bar changes.

This module deliberately avoids Qt imports so that the core stays headless and
trivially testable.
"""

from __future__ import annotations

import itertools
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

__all__ = [
    "AgentEvent",
    "AgentEventType",
    "EventBus",
    "EventPayload",
    "new_event_id",
]


class AgentEventType(StrEnum):
    """Stable identifiers for every event the application emits."""

    USER_MESSAGE = "user_message"
    AGENT_STARTED = "agent_started"
    MODEL_REQUEST = "model_request"
    MODEL_RESPONSE = "model_response"
    TOOL_REQUESTED = "tool_requested"
    TOOL_STARTED = "tool_started"
    TOOL_COMPLETED = "tool_completed"
    TOOL_FAILED = "tool_failed"
    PERMISSION_DECISION = "permission_decision"
    PERMISSION_APPROVAL = "permission_approval"
    """A human answered an ``ASK`` prompt: allowed once, allowed for the session,
    or denied. Separate from ``PERMISSION_DECISION`` so the activity log can show
    what the *user* chose rather than only the resulting decision."""
    AGENT_COMPLETED = "agent_completed"
    AGENT_FAILED = "agent_failed"
    AGENT_CANCELLED = "agent_cancelled"
    WORKSPACE_OPENED = "workspace_opened"
    CONFIG_CHANGED = "config_changed"


#: Payload contract per event type. Kept as documentation + runtime validation
#: aid for the GUI; the bus itself does not enforce it.
EventPayload = dict[str, Any]


@dataclass(frozen=True, slots=True)
class AgentEvent:
    """An immutable, timestamped observation emitted by a subsystem."""

    type: AgentEventType
    payload: EventPayload = field(default_factory=dict)
    event_id: str = field(default_factory=lambda: new_event_id())
    timestamp: float = field(default_factory=time.time)
    session_id: str = ""

    def __str__(self) -> str:  # pragma: no cover - trivial
        return f"{self.type.value}@{self.timestamp:.3f}"


_counter = itertools.count(1)


def new_event_id() -> str:
    """Return a short, unique, human-readable event identifier."""

    return f"evt-{uuid.uuid4().hex[:8]}-{next(_counter):04d}"


class EventBus:
    """Minimal synchronous publish/subscribe bus.

    Handlers are invoked in registration order. An exception raised by a
    handler is swallowed and reported through ``on_handler_error`` so that one
    misbehaving subscriber (for example a GUI widget) can never break the
    agent loop.
    """

    def __init__(self) -> None:
        self._handlers: dict[AgentEventType, list[Callable[[AgentEvent], None]]] = {}
        self._any: list[Callable[[AgentEvent], None]] = []
        self._history: list[AgentEvent] = []
        self.max_history = 500

    def subscribe(
        self,
        event_type: AgentEventType,
        handler: Callable[[AgentEvent], None],
    ) -> Callable[[], None]:
        """Subscribe to a single event type; returns an unsubscribe callable."""

        self._handlers.setdefault(event_type, []).append(handler)

        def _unsubscribe() -> None:
            handlers = self._handlers.get(event_type)
            if handlers and handler in handlers:
                handlers.remove(handler)

        return _unsubscribe

    def subscribe_all(self, handler: Callable[[AgentEvent], None]) -> Callable[[], None]:
        """Subscribe to every event; returns an unsubscribe callable."""

        self._any.append(handler)

        def _unsubscribe() -> None:
            if handler in self._any:
                self._any.remove(handler)

        return _unsubscribe

    def publish(self, event: AgentEvent) -> None:
        """Publish ``event`` to all matching subscribers."""

        self._history.append(event)
        if len(self._history) > self.max_history:
            del self._history[: len(self._history) - self.max_history]

        for handler in (*self._any, *self._handlers.get(event.type, ())):
            try:
                handler(event)
            except Exception:
                self.on_handler_error(event)

    def emit(
        self,
        event_type: AgentEventType,
        payload: EventPayload | None = None,
        *,
        session_id: str = "",
    ) -> AgentEvent:
        """Convenience helper: build and publish an event in one call."""

        event = AgentEvent(
            type=event_type,
            payload=dict(payload or {}),
            session_id=session_id,
        )
        self.publish(event)
        return event

    @property
    def history(self) -> tuple[AgentEvent, ...]:
        """Read-only view of the events published so far."""

        return tuple(self._history)

    def clear_history(self) -> None:
        """Drop the recorded event history (used when a session is reset)."""

        self._history.clear()

    def on_handler_error(self, event: AgentEvent) -> None:
        """Called when a subscriber raises. Override to add logging."""

        _ = event
