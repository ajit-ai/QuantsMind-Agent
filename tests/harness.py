"""Reusable wiring for integration and GUI tests.

``AgentHarness`` composes the real Context Engine, Model Gateway, Tool Registry
and Agent Core, but swaps the network provider for the scripted
:class:`tests.fakes.FakeModelProvider`. Everything below the provider is
production code, so these tests exercise the same code paths the GUI uses.
"""

from __future__ import annotations

from typing import Any

from tests.fakes import FakeModelProvider

from quantsmind_agent.agent import AgentCore, StreamEmitter
from quantsmind_agent.context import ContextEngine
from quantsmind_agent.events import AgentEvent, AgentEventType, EventBus
from quantsmind_agent.llm import ChatRequest, ModelGateway
from quantsmind_agent.permissions import PermissionBroker, StaticPermissionProvider
from quantsmind_agent.session import Session
from quantsmind_agent.tools import ToolRegistry, ToolResult
from quantsmind_agent.workspace import Workspace
from quantsmind_agent.workspace_tools import build_default_tools

__all__ = ["ActivityRecorder", "AgentHarness"]


class ActivityRecorder:
    """Collects the callbacks a GUI would render."""

    def __init__(self) -> None:
        self.deltas: list[str] = []
        self.started: list[tuple[str, str]] = []
        self.finished: list[ToolResult] = []
        self.iterations: list[int] = []

    def emitter(self) -> StreamEmitter:
        return StreamEmitter(
            on_text=self.deltas.append,
            on_tool_start=lambda name, args: self.started.append((name, args)),
            on_tool_end=self.finished.append,
            on_iteration=self.iterations.append,
        )

    @property
    def text(self) -> str:
        return "".join(self.deltas)


class AgentHarness:
    """A fully wired agent stack backed by a scripted provider."""

    def __init__(
        self,
        workspace: Workspace,
        provider: FakeModelProvider | None = None,
        *,
        max_iterations: int = 4,
        use_streaming: bool = False,
    ) -> None:
        self.events = EventBus()
        self.session = Session(workspace)
        self.provider = provider or FakeModelProvider()
        self.gateway = ModelGateway(self.provider, event_bus=self.events)
        self.registry = ToolRegistry(
            permissions=PermissionBroker(StaticPermissionProvider.for_v0_1()),
            event_bus=self.events,
            session_id=self.session.id,
        )
        self.registry.register_all(build_default_tools(workspace))
        self.context = ContextEngine(workspace)
        self.agent = AgentCore(
            self.gateway,
            self.registry,
            self.context,
            self.session,
            event_bus=self.events,
            max_iterations=max_iterations,
            use_streaming=use_streaming,
            request_options=self._options,
        )
        self.seen: list[AgentEventType] = []
        self.events.subscribe_all(self._record)
        self.recorder = ActivityRecorder()

    def _options(self) -> dict[str, Any]:
        return {
            "model": "fake-model",
            "temperature": 0.1,
            "max_output_tokens": 1024,
            "timeout_seconds": 30,
        }

    def _record(self, event: AgentEvent) -> None:
        self.seen.append(event.type)

    def events_of(self, event_type: AgentEventType) -> list[AgentEvent]:
        return [event for event in self.events.history if event.type is event_type]

    def run(self, user_text: str) -> Any:
        """Run one turn, recording GUI callbacks."""

        return self.agent.run(user_text, emitter=self.recorder.emitter())


class ScriptedProvider(FakeModelProvider):
    """A provider that cancels the agent after ``cancel_after`` round-trips."""

    def __init__(self, agent_ref: dict[str, AgentCore], cancel_after: int) -> None:
        super().__init__()
        self._agent_ref = agent_ref
        self._cancel_after = cancel_after
        self.calls = 0

    def complete(self, request: ChatRequest) -> Any:
        self.calls += 1
        if self.calls >= self._cancel_after:
            self._agent_ref["agent"].cancel()
        return super().complete(request)
