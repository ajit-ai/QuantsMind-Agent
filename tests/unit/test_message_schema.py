"""Serialization contract for the conversation message schema.

``AssistantMessage`` and ``ToolMessage`` call the base implementation
explicitly. ``@dataclass(slots=True)`` replaces the class object it decorates,
so a zero-argument ``super()`` would still reference the discarded class on
Python 3.11 and 3.12 and raise ``TypeError``. These tests exercise the repaired
call path and pin the payload shape that stored conversations depend on.
"""

from __future__ import annotations

import json

import pytest

from quantsmind_agent.schema import (
    AssistantMessage,
    Message,
    SystemMessage,
    ToolCall,
    ToolMessage,
    UserMessage,
)
from quantsmind_agent.session import Session

ASSISTANT_KEYS = {
    "id",
    "role",
    "content",
    "created_at",
    "metadata",
    "tool_calls",
    "model",
}
TOOL_KEYS = {
    "id",
    "role",
    "content",
    "created_at",
    "metadata",
    "tool_name",
    "tool_call_id",
    "ok",
}


def test_assistant_message_to_dict_carries_base_fields() -> None:
    message = AssistantMessage(content="hello", model="test-model")

    payload = message.to_dict()

    assert set(payload) == ASSISTANT_KEYS
    assert payload["id"] == message.id
    assert payload["role"] == "assistant"
    assert payload["content"] == "hello"
    assert payload["created_at"] == message.created_at
    assert payload["metadata"] == {}
    assert payload["model"] == "test-model"


def test_assistant_message_to_dict_serialises_tool_calls() -> None:
    call = ToolCall(id="call-1", name="read_file", arguments={"path": "README.md"})

    payload = AssistantMessage(content="", tool_calls=[call]).to_dict()

    assert payload["tool_calls"] == [
        {"id": "call-1", "name": "read_file", "arguments": {"path": "README.md"}}
    ]


def test_tool_message_to_dict_carries_tool_fields() -> None:
    message = ToolMessage(
        content="failed",
        tool_name="run_command",
        tool_call_id="call-9",
        ok=False,
    )

    payload = message.to_dict()

    assert set(payload) == TOOL_KEYS
    assert payload["role"] == "tool"
    assert payload["content"] == "failed"
    assert payload["tool_name"] == "run_command"
    assert payload["tool_call_id"] == "call-9"
    assert payload["ok"] is False


def test_every_message_payload_is_json_serialisable() -> None:
    messages: list[Message] = [
        SystemMessage(content="sys"),
        UserMessage(content="q"),
        AssistantMessage(content="a", tool_calls=[ToolCall(id="call-1", name="search")]),
        ToolMessage(content="r", tool_name="search", tool_call_id="call-1"),
    ]

    for message in messages:
        encoded = json.dumps(message.to_dict())
        assert json.loads(encoded)["content"] == message.content


def test_assistant_message_round_trips_through_session() -> None:
    call = ToolCall(id="call-1", name="read_file", arguments={"path": "README.md"})
    original = AssistantMessage(
        content="done",
        tool_calls=[call],
        model="test-model",
        id="msg-fixed",
        created_at=1234.5,
    )
    session = Session()
    session.add_message(original)
    payload = session.to_dict()

    restored = Session()
    assert restored.restore_from(payload) is True
    assert len(restored.messages) == 1
    message = restored.messages[0]
    assert isinstance(message, AssistantMessage)
    assert message.id == "msg-fixed"
    assert message.created_at == 1234.5
    assert message.content == "done"
    assert message.model == "test-model"
    assert [(item.id, item.name, item.arguments) for item in message.tool_calls] == [
        ("call-1", "read_file", {"path": "README.md"})
    ]


@pytest.mark.parametrize(
    "message",
    [Message(), SystemMessage(), UserMessage(), AssistantMessage(), ToolMessage()],
)
def test_message_classes_keep_slots(message: Message) -> None:
    assert not hasattr(message, "__dict__")


def test_optional_fields_keep_their_defaults() -> None:
    assistant = AssistantMessage(content="x").to_dict()
    assert assistant["tool_calls"] == []
    assert assistant["model"] == ""
    assert "finish_reason" not in assistant
    assert "total_tokens" not in assistant

    tool = ToolMessage(content="r").to_dict()
    assert tool["tool_name"] == ""
    assert tool["tool_call_id"] == ""
    assert tool["ok"] is True
