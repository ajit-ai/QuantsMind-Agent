"""Conversation message schema shared by the agent, gateway and GUI.

Messages are plain dataclasses so they can be rendered by Qt widgets, written to
JSON and compared in tests without any framework-specific types.

Wire format follows the OpenAI chat-completions shape closely enough that
:mod:`quantsmind_agent.providers.openai_compat` can serialise it directly.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

__all__ = [
    "AssistantMessage",
    "Message",
    "MessageRole",
    "SystemMessage",
    "ToolMessage",
    "UserMessage",
    "make_conversation_id",
    "new_message_id",
]


class MessageRole(StrEnum):
    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"


def new_message_id() -> str:
    return f"msg-{uuid.uuid4().hex[:12]}"


def make_conversation_id() -> str:
    """Identifier for one GUI session's conversation."""

    return f"conv-{uuid.uuid4().hex[:12]}"


@dataclass(slots=True)
class Message:
    """Base message: an id, a role, content and a timestamp."""

    content: str = ""
    role: MessageRole = MessageRole.USER
    id: str = field(default_factory=new_message_id)
    created_at: float = field(default_factory=time.time)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "role": self.role.value,
            "content": self.content,
            "created_at": self.created_at,
            "metadata": dict(self.metadata),
        }

    def to_wire(self) -> dict[str, Any]:
        return {"role": self.role.value, "content": self.content}

    @property
    def is_empty(self) -> bool:
        return not self.content.strip()


@dataclass(slots=True)
class SystemMessage(Message):
    role: MessageRole = MessageRole.SYSTEM


@dataclass(slots=True)
class UserMessage(Message):
    role: MessageRole = MessageRole.USER


@dataclass(slots=True)
class ToolCall:
    """One tool invocation requested by the model."""

    id: str
    name: str
    arguments: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "name": self.name, "arguments": dict(self.arguments)}

    def to_wire(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "type": "function",
            "function": self.name,
            "arguments": self.arguments,
        }


@dataclass(slots=True)
class AssistantMessage(Message):
    role: MessageRole = MessageRole.ASSISTANT
    tool_calls: list[ToolCall] = field(default_factory=list)
    model: str = ""
    finish_reason: str | None = None
    total_tokens: int | None = None

    def to_dict(self) -> dict[str, Any]:
        payload = super().to_dict()
        payload["tool_calls"] = [call.to_dict() for call in self.tool_calls]
        payload["model"] = self.model
        return payload

    def to_wire(self) -> dict[str, Any]:
        wire: dict[str, Any] = {"role": "assistant", "content": self.content}
        if self.tool_calls:
            wire["tool_calls"] = [
                {
                    "id": call.id,
                    "type": "function",
                    "function": {
                        "name": call.name,
                        "arguments": _dump_arguments(call.arguments),
                    },
                }
                for call in self.tool_calls
            ]
        return wire


@dataclass(slots=True)
class ToolMessage(Message):
    role: MessageRole = MessageRole.TOOL
    tool_name: str = ""
    tool_call_id: str = ""
    ok: bool = True

    def to_dict(self) -> dict[str, Any]:
        payload = super().to_dict()
        payload["tool_name"] = self.tool_name
        payload["tool_call_id"] = self.tool_call_id
        payload["ok"] = self.ok
        return payload

    def to_wire(self) -> dict[str, Any]:
        return {
            "role": "tool",
            "tool_call_id": self.tool_call_id,
            "content": self.content,
        }


def _dump_arguments(arguments: dict[str, Any]) -> str:
    import json

    return json.dumps(arguments, ensure_ascii=False)
