"""Unit tests for session state and the event bus."""

from __future__ import annotations

import json

from quantsmind_agent.events import AgentEvent, AgentEventType, EventBus
from quantsmind_agent.schema import AssistantMessage, MessageRole, ToolMessage, UserMessage
from quantsmind_agent.session import Session
from quantsmind_agent.tools import ToolResult
from quantsmind_agent.workspace import Workspace


class TestSession:
    def test_starts_empty(self) -> None:
        session = Session()
        assert session.messages == []
        assert session.turns == []
        assert not session.has_workspace
        assert session.id.startswith("conv-")

    def test_workspace_can_be_attached(self, workspace: Workspace) -> None:
        session = Session(workspace)
        assert session.has_workspace
        session.set_workspace(None)
        assert not session.has_workspace

    def test_messages_are_copied_on_read(self, workspace: Workspace) -> None:
        session = Session(workspace)
        session.add_message(UserMessage(content="hi"))
        messages = session.messages
        messages.append(UserMessage(content="injected"))
        assert len(session.messages) == 1

    def test_conversation_is_an_alias(self) -> None:
        session = Session()
        session.add_message(UserMessage(content="q"))
        assert session.conversation == session.messages

    def test_turn_lifecycle(self) -> None:
        session = Session()
        record = session.begin_turn("question")
        # Capture the property once: narrowing it twice would make the second
        # assertion contradictory from the type checker's point of view.
        active = session.active_turn
        assert active is record
        session.end_turn(record)
        assert session.active_turn is None
        assert record.finished_at is not None
        assert record.succeeded
        assert record.duration_ms >= 0
        assert record.index == 1

    def test_failed_turn_is_not_successful(self) -> None:
        record = Session().begin_turn("q")
        record.error = "boom"
        assert not record.succeeded

    def test_cancelled_turn_is_not_successful(self) -> None:
        record = Session().begin_turn("q")
        record.cancelled = True
        assert not record.succeeded

    def test_tool_results_are_attached_to_the_active_turn(self) -> None:
        session = Session()
        record = session.begin_turn("q")
        result = ToolResult.success("read_file", "read a.py")
        session.record_tool_result(result)
        assert record.tool_results == [result]
        assert session.tool_results == [result]

    def test_history_excludes_tool_traffic(self) -> None:
        session = Session()
        session.add_message(UserMessage(content="q1"))
        session.add_message(AssistantMessage(content="a1"))
        session.add_message(ToolMessage(content="TOOL RESULT", tool_name="read_file"))
        assert [m.role for m in session.history_for_context()] == [
            MessageRole.USER,
            MessageRole.ASSISTANT,
        ]

    def test_history_excludes_empty_messages(self) -> None:
        session = Session()
        session.add_message(UserMessage(content="   "))
        assert session.history_for_context() == []

    def test_reset_keeps_the_workspace(self, workspace: Workspace) -> None:
        session = Session(workspace)
        session.add_message(UserMessage(content="q"))
        session.begin_turn("q")
        session.reset()
        assert session.messages == []
        assert session.has_workspace
        assert session.turns == []

    def test_reset_can_drop_the_workspace(self, workspace: Workspace) -> None:
        session = Session(workspace)
        session.reset(keep_workspace=False)
        assert not session.has_workspace

    def test_events_are_bounded(self) -> None:
        session = Session()
        for index in range(600):
            session.record_event(
                AgentEvent(type=AgentEventType.MODEL_REQUEST, payload={"i": index})
            )
        assert len(session.events) == 500

    def test_to_dict_is_serialisable(self, workspace: Workspace) -> None:
        session = Session(workspace)
        session.add_message(UserMessage(content="q"))
        payload = session.to_dict()
        assert json.dumps(payload)
        assert payload["workspace"] == str(workspace.root)

    def test_to_dict_has_no_credentials(self, workspace: Workspace) -> None:
        session = Session(workspace)
        assert "api_key" not in json.dumps(session.to_dict())

    def test_summary_mentions_the_project(self, workspace: Workspace) -> None:
        assert workspace.root.name in Session(workspace).summary()

    def test_turn_dict_shape(self) -> None:
        assert set(Session().begin_turn("q").to_dict()) == {
            "index",
            "user_text",
            "started_at",
            "finished_at",
            "duration_ms",
            "iterations",
            "tools_used",
            "final_text",
            "error",
            "cancelled",
        }


class TestEventBus:
    def test_publish_reaches_subscribers(self) -> None:
        bus = EventBus()
        seen: list[str] = []
        bus.subscribe(AgentEventType.MODEL_REQUEST, lambda event: seen.append(event.payload["m"]))
        bus.emit(AgentEventType.MODEL_REQUEST, {"m": "a"})
        bus.emit(AgentEventType.MODEL_REQUEST, {"m": "b"})
        assert seen == ["a", "b"]

    def test_subscribe_all_sees_everything(self) -> None:
        bus = EventBus()
        seen: list[AgentEventType] = []
        bus.subscribe_all(lambda event: seen.append(event.type))
        bus.emit(AgentEventType.AGENT_STARTED)
        bus.emit(AgentEventType.AGENT_COMPLETED)
        assert seen == [AgentEventType.AGENT_STARTED, AgentEventType.AGENT_COMPLETED]

    def test_unsubscribe_stops_delivery(self) -> None:
        bus = EventBus()
        seen: list[AgentEvent] = []
        bus.subscribe(AgentEventType.MODEL_REQUEST, seen.append)()
        bus.emit(AgentEventType.MODEL_REQUEST)
        assert seen == []

    def test_unsubscribe_all(self) -> None:
        bus = EventBus()
        seen: list[AgentEvent] = []
        bus.subscribe_all(seen.append)()
        bus.emit(AgentEventType.MODEL_REQUEST)
        assert seen == []

    def test_a_failing_subscriber_cannot_break_the_agent(self) -> None:
        bus = EventBus()
        delivered: list[str] = []

        def boom(event: AgentEvent) -> None:
            raise RuntimeError("widget is gone")

        bus.subscribe(AgentEventType.MODEL_REQUEST, boom)
        bus.subscribe(AgentEventType.MODEL_REQUEST, lambda event: delivered.append("ok"))
        bus.emit(AgentEventType.MODEL_REQUEST)
        assert delivered == ["ok"]

    def test_history_is_bounded(self) -> None:
        bus = EventBus()
        bus.max_history = 10
        for index in range(25):
            bus.emit(AgentEventType.MODEL_REQUEST, {"i": index})
        assert len(bus.history) == 10

    def test_history_is_immutable(self) -> None:
        bus = EventBus()
        bus.emit(AgentEventType.MODEL_REQUEST)
        assert isinstance(bus.history, tuple)

    def test_clear_history(self) -> None:
        bus = EventBus()
        bus.emit(AgentEventType.MODEL_REQUEST)
        bus.clear_history()
        assert bus.history == ()

    def test_events_carry_metadata(self) -> None:
        event = AgentEvent(type=AgentEventType.AGENT_STARTED, session_id="s1")
        assert event.event_id.startswith("evt-")
        assert event.timestamp > 0
        assert event.session_id == "s1"
        assert str(event).startswith("agent_started@")

    def test_event_ids_are_unique(self) -> None:
        ids = {AgentEvent(type=AgentEventType.AGENT_STARTED).event_id for _ in range(50)}
        assert len(ids) == 50

    def test_all_documented_event_types_exist(self) -> None:
        required = {
            "user_message",
            "agent_started",
            "model_request",
            "model_response",
            "tool_requested",
            "tool_started",
            "tool_completed",
            "tool_failed",
            "agent_completed",
            "agent_failed",
        }
        assert required <= {event.value for event in AgentEventType}

    def test_emit_returns_the_event(self) -> None:
        event = EventBus().emit(AgentEventType.AGENT_STARTED, {"a": 1}, session_id="s")
        assert event.payload == {"a": 1}
        assert event.session_id == "s"
