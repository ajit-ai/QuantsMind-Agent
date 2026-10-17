"""Session state for the current application run.

A :class:`Session` is deliberately in-memory only: it holds the workspace
reference, the conversation, the last agent events and every tool result
produced during the run. Nothing is persisted to disk in v0.1, which keeps the
product free of databases and makes "no data leaves the machine except the LLM
request" easy to state and to verify.

The Agent Core is session-agnostic; the GUI owns one session and resets it when
the user asks for a new conversation.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from quantsmind_agent.events import AgentEvent
from quantsmind_agent.logging_setup import get_logger
from quantsmind_agent.revisions import RevisionJournal
from quantsmind_agent.schema import (
    AssistantMessage,
    Message,
    MessageRole,
    SystemMessage,
    ToolCall,
    ToolMessage,
    UserMessage,
    make_conversation_id,
)
from quantsmind_agent.tools import ToolResult
from quantsmind_agent.workspace import Workspace

__all__ = ["HistoryEntry", "Session", "TurnRecord"]

logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class HistoryEntry:
    """One line of a conversation in the order it actually happened.

    ``message`` is set for user/assistant traffic and ``tool_result`` for a tool
    call that produced no message of its own. Tool traffic is kept out of the
    message list on purpose -- it is not context the model should be fed back --
    so a transcript that shows what the agent did has to interleave the two
    sources by time to get the order right.
    """

    created_at: float
    message: Message | None = None
    tool_result: ToolResult | None = None

    @property
    def is_tool(self) -> bool:
        return self.tool_result is not None


def _message_from_dict(payload: dict[str, Any]) -> Message | None:
    """Rebuild one stored message, tolerating a role this version does not know.

    An unrecognised role is dropped rather than guessed at: a transcript with one
    unreadable line in it is still worth resuming.

    The stored id and timestamp are carried across. They are not cosmetic: without
    them every message in a reopened conversation would look like it had just
    happened, and the next autosave would overwrite the real ones with that lie.
    """

    role = str(payload.get("role") or "")
    content = str(payload.get("content") or "")
    metadata = payload.get("metadata")
    common: dict[str, Any] = {}
    message_id = str(payload.get("id") or "")
    if message_id:
        common["id"] = message_id
    created_at = payload.get("created_at")
    if isinstance(created_at, (int, float)):
        common["created_at"] = float(created_at)
    if isinstance(metadata, dict):
        common["metadata"] = metadata
    if role == MessageRole.USER.value:
        return UserMessage(content=content, **common)
    if role == MessageRole.ASSISTANT.value:
        calls = []
        for call in payload.get("tool_calls", []) or []:
            if not isinstance(call, dict):
                continue
            arguments = call.get("arguments")
            calls.append(
                ToolCall(
                    id=str(call.get("id") or ""),
                    name=str(call.get("name") or ""),
                    arguments=arguments if isinstance(arguments, dict) else {},
                )
            )
        return AssistantMessage(
            content=content,
            tool_calls=calls,
            model=str(payload.get("model") or ""),
            **common,
        )
    if role == MessageRole.TOOL.value:
        return ToolMessage(
            content=content,
            tool_name=str(payload.get("tool_name") or ""),
            tool_call_id=str(payload.get("tool_call_id") or ""),
            ok=bool(payload.get("ok", True)),
            **common,
        )
    if role == MessageRole.SYSTEM.value:
        return SystemMessage(content=content, **common)
    return None


def _turn_from_dict(payload: Any) -> TurnRecord | None:
    """Rebuild a stored turn summary. Tool calls are not rebuilt from it.

    The stored ``tools_used`` names are a list of strings, not results, so the
    transcript's tool lines come from ``tool_results`` instead. Rebuilding the
    summary keeps the turn counter and the durations honest.
    """

    if not isinstance(payload, dict):
        return None
    started_at = payload.get("started_at")
    finished_at = payload.get("finished_at")
    return TurnRecord(
        index=int(payload.get("index") or 0),
        user_text=str(payload.get("user_text") or ""),
        started_at=float(started_at) if isinstance(started_at, (int, float)) else time.time(),
        finished_at=(float(finished_at) if isinstance(finished_at, (int, float)) else None),
        iterations=int(payload.get("iterations") or 0),
        final_text=str(payload.get("final_text") or ""),
        error=str(payload["error"]) if payload.get("error") else None,
        cancelled=bool(payload.get("cancelled", False)),
    )


@dataclass(slots=True)
class TurnRecord:
    """One user request and everything the agent did in response."""

    index: int
    user_text: str
    started_at: float = field(default_factory=time.time)
    finished_at: float | None = None
    iterations: int = 0
    tool_results: list[ToolResult] = field(default_factory=list)
    final_text: str = ""
    error: str | None = None
    cancelled: bool = False

    @property
    def duration_ms(self) -> float:
        end = self.finished_at if self.finished_at is not None else time.time()
        # Clamped: a record written by an older version, or edited by hand, can carry
        # a finish before its start, and "-1792214787671 ms" is a number no one
        # should be shown rather than evidence of anything.
        return max(0.0, (end - self.started_at) * 1000)

    @property
    def succeeded(self) -> bool:
        return self.error is None and not self.cancelled

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "user_text": self.user_text,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "duration_ms": round(self.duration_ms, 1),
            "iterations": self.iterations,
            "tools_used": [result.tool for result in self.tool_results],
            "final_text": self.final_text,
            "error": self.error,
            "cancelled": self.cancelled,
        }


class Session:
    """Mutable state for one GUI session."""

    def __init__(self, workspace: Workspace | None = None) -> None:
        self.id = make_conversation_id()
        self.created_at = time.time()
        self._workspace = workspace
        self._messages: list[Message] = []
        self._turns: list[TurnRecord] = []
        self._events: list[AgentEvent] = []
        self._tool_results: list[ToolResult] = []
        self._active_turn: TurnRecord | None = None
        #: Pre-images for the session's undo list. Owned here, not by the tools,
        #: so the journal outlives the tool instances: the application rebuilds
        #: every tool on each workspace or settings change, and a write from the
        #: previous turn must still be undoable afterwards. Cleared by
        #: :meth:`reset`, because a new conversation is a new trust boundary.
        self.revisions = RevisionJournal()

    # -- workspace --------------------------------------------------------
    @property
    def workspace(self) -> Workspace | None:
        return self._workspace

    def set_workspace(self, workspace: Workspace | None) -> None:
        """Point the session at a project. The conversation is preserved.

        The undo list is *not* preserved across a workspace change: its entries
        name paths relative to the project that recorded them, so carrying them
        into another project would offer to restore content from somewhere the
        user is no longer looking.
        """

        changed = (workspace.root if workspace else None) != (
            self._workspace.root if self._workspace else None
        )
        self._workspace = workspace
        if changed:
            self.revisions.clear()

    @property
    def has_workspace(self) -> bool:
        return self._workspace is not None

    # -- conversation -----------------------------------------------------
    @property
    def messages(self) -> list[Message]:
        return list(self._messages)

    @property
    def conversation(self) -> list[Message]:
        """Alias for :attr:`messages` (same list, copy)."""

        return list(self._messages)

    @property
    def turns(self) -> list[TurnRecord]:
        return list(self._turns)

    @property
    def tool_results(self) -> list[ToolResult]:
        return list(self._tool_results)

    @property
    def events(self) -> list[AgentEvent]:
        return list(self._events)

    def add_message(self, message: Message) -> Message:
        self._messages.append(message)
        return message

    def add_messages(self, messages: list[Message]) -> None:
        self._messages.extend(messages)

    def history_for_context(self) -> list[Message]:
        """User/assistant turns only; tool traffic is excluded."""

        return [
            message
            for message in self._messages
            if message.role in (MessageRole.USER, MessageRole.ASSISTANT) and not message.is_empty
        ]

    # -- turns ------------------------------------------------------------
    @property
    def active_turn(self) -> TurnRecord | None:
        return self._active_turn

    def begin_turn(self, user_text: str) -> TurnRecord:
        record = TurnRecord(index=len(self._turns) + 1, user_text=user_text)
        self._turns.append(record)
        self._active_turn = record
        return record

    def end_turn(self, record: TurnRecord) -> None:
        record.finished_at = time.time()
        self._active_turn = None

    def record_tool_result(self, result: ToolResult) -> None:
        self._tool_results.append(result)
        if self._active_turn is not None:
            self._active_turn.tool_results.append(result)

    def record_event(self, event: AgentEvent) -> None:
        self._events.append(event)
        # Bound memory: keep the most recent 500 events for the session view.
        if len(self._events) > 500:
            del self._events[: len(self._events) - 500]

    # -- resumption --------------------------------------------------------
    def restore_from(self, payload: dict[str, Any]) -> bool:
        """Replace this session's content with a stored record.

        Content comes back: messages with their ids and timestamps, the tool
        results they were interleaved with, and the turn summaries. What does not
        come back is anything session-scoped or dangerous to replay -- the undo
        journal (a restored pre-image would outlive the session that captured it),
        approval grants, and any tool call that had not finished.

        Returns ``False`` when the record holds nothing restorable, so the caller
        can start fresh rather than presenting an empty transcript as if it were
        the conversation.
        """

        rebuilt: list[Message] = []
        for item in payload.get("messages", []) or []:
            if not isinstance(item, dict):
                continue
            message = _message_from_dict(item)
            if message is not None:
                rebuilt.append(message)
        results = [
            result
            for result in (
                ToolResult.from_dict(item) for item in payload.get("tool_results", []) or []
            )
            if result is not None
        ]
        turns = [
            turn
            for turn in (_turn_from_dict(item) for item in payload.get("turns", []) or [])
            if turn is not None
        ]
        if not rebuilt and not results:
            return False

        self.id = str(payload.get("id") or self.id)
        self.created_at = float(payload.get("created_at") or self.created_at)
        self._messages.clear()
        self.add_messages(rebuilt)
        self._tool_results.clear()
        self._tool_results.extend(results)
        self._turns.clear()
        self._turns.extend(turns)
        self._active_turn = None
        logger.info(
            "Restored conversation %s with %d messages, %d tool results, %d turns",
            self.id,
            len(rebuilt),
            len(results),
            len(turns),
        )
        return True

    def ordered_history(self) -> list[HistoryEntry]:
        """Messages and tool results merged into the order they happened.

        Sorted by the time each thing was produced. Equal timestamps keep messages
        ahead of tool results, because the assistant message that asked for a tool
        call is always created before it runs; within one kind, insertion order is
        preserved.
        """

        entries: list[tuple[float, int, int, HistoryEntry]] = []
        for index, message in enumerate(self._messages):
            entries.append(
                (message.created_at, 0, index, HistoryEntry(message.created_at, message))
            )
        for index, result in enumerate(self._tool_results):
            entries.append(
                (result.created_at, 1, index, HistoryEntry(result.created_at, None, result))
            )
        entries.sort(key=lambda item: (item[0], item[1], item[2]))
        return [item[3] for item in entries]

    def last_turn_outcome(self) -> str:
        """How the final turn actually ended: ``ok``, ``cancelled``, ``failed``
        or ``interrupted``.

        Used to tell the truth about a reopened conversation instead of implying
        the last request was answered when it was not.
        """

        if not self._turns:
            return "ok"
        last = self._turns[-1]
        if last.cancelled:
            return "cancelled"
        if last.error:
            return "failed"
        if last.finished_at is None:
            return "interrupted"
        return "ok"

    # -- lifecycle --------------------------------------------------------
    def reset(self, *, keep_workspace: bool = True) -> None:
        """Clear the conversation but keep the project open."""

        self.id = make_conversation_id()
        self._messages.clear()
        self._turns.clear()
        self._events.clear()
        self._tool_results.clear()
        self._active_turn = None
        # The undo list dies with the conversation: revisions restore writes the
        # user already saw, and a fresh conversation must not be able to reach
        # back into the previous one's filesystem changes.
        self.revisions.clear()
        if not keep_workspace:
            self._workspace = None

    def summary(self) -> str:
        """One-line description used in the status bar and by tests."""

        workspace = self._workspace.root.name if self._workspace else "no project"
        return (
            f"session {self.id} | {workspace} | {len(self._messages)} messages | "
            f"{len(self._turns)} turns"
        )

    def to_dict(self) -> dict[str, Any]:
        """Serialisable snapshot (contains no credentials by construction)."""

        return {
            "id": self.id,
            "created_at": self.created_at,
            "workspace": str(self._workspace.root) if self._workspace else None,
            "messages": [message.to_dict() for message in self._messages],
            "turns": [turn.to_dict() for turn in self._turns],
            "tool_results": [result.to_dict() for result in self._tool_results],
        }
