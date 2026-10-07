"""Context Engine: turns session state into a bounded model prompt.

Responsibilities:

* describe the workspace (root, project name, a shallow structure listing),
* include a bounded amount of conversation history,
* include the tool schemas the model may call,
* keep the whole prompt inside a configurable character budget, and
* expose a stable :class:`ContextProvider` interface so v0.2 can swap in
  indexing/ranking/retrieval without touching the Agent Core.

Design rule: never dump a whole repository into a prompt. The engine starts with
a bounded structure summary and lets the model pull specifics with tools.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from quantsmind_agent.logging_setup import get_logger
from quantsmind_agent.schema import Message, UserMessage
from quantsmind_agent.workspace import Workspace

__all__ = [
    "DEFAULT_SYSTEM_PROMPT",
    "ContextBundle",
    "ContextEngine",
    "ContextProvider",
]

logger = get_logger(__name__)

DEFAULT_SYSTEM_PROMPT = """\
You are QuantsMind-Agent, an AI coding assistant working inside a user's local
project on their own machine.

Operating rules:
- Work only inside the project folder that has been opened. Never claim to have
  read a file you have not read with a tool.
- Use the provided tools to inspect the project before answering questions about
  it. Prefer `project_structure` for orientation, `read_file` for contents,
  `search_content` for finding symbols, and `list_directory` for quick checks.
- Paths in tool arguments must be relative to the workspace root.
- If a tool fails, read the error and correct the arguments; do not repeat an
  identical failing call more than once.
- When you have enough information, answer directly and concisely. Cite files as
  `path/to/file.py:42` when referring to specific code.
- You cannot run commands, modify files, use git or access the network in this
  version. Say so plainly if asked to.
"""


@dataclass(slots=True)
class ContextBundle:
    """Everything the model is told for one turn."""

    system_prompt: str
    messages: list[Message]
    tools: list[dict[str, Any]] = field(default_factory=list)
    workspace_summary: str = ""
    estimated_chars: int = 0

    def describe(self) -> str:
        return (
            f"{len(self.messages)} messages, {len(self.tools)} tools, "
            f"{self.estimated_chars} chars of context"
        )


@runtime_checkable
class ContextProvider(Protocol):
    """Supplies a system prompt and the workspace summary for a turn."""

    def system_prompt(self) -> str:
        """Return the base instructions for the model."""

    def workspace_summary(self) -> str:
        """Return a bounded description of the active workspace."""


class ContextEngine:
    """Builds :class:`ContextBundle` objects from a session.

    Parameters
    ----------
    workspace:
        The active project, or ``None`` when no project is open.
    custom_prompt:
        Operator-supplied instructions appended after the base prompt.
    max_context_chars:
        Soft budget for history + workspace summary.
    history_turns:
        How many recent user/assistant pairs to keep.
    """

    def __init__(
        self,
        workspace: Workspace | None = None,
        *,
        custom_prompt: str = "",
        max_context_chars: int = 24_000,
        history_turns: int = 12,
        max_structure_entries: int = 120,
    ) -> None:
        self._workspace = workspace
        self._custom_prompt = custom_prompt.strip()
        self._max_context_chars = max_context_chars
        self._history_turns = history_turns
        self._max_structure_entries = max_structure_entries
        self._cached_summary: str | None = None

    # -- configuration ---------------------------------------------------
    @property
    def workspace(self) -> Workspace | None:
        return self._workspace

    def set_workspace(self, workspace: Workspace | None) -> None:
        self._workspace = workspace
        self._cached_summary = None

    def set_custom_prompt(self, prompt: str) -> None:
        self._custom_prompt = prompt.strip()

    # -- ContextProvider --------------------------------------------------
    def system_prompt(self) -> str:

        sections = [DEFAULT_SYSTEM_PROMPT]
        if self._custom_prompt:
            sections.append("Operator instructions:\n" + self._custom_prompt)
        prompt = "\n\n".join(sections)

        summary = self.workspace_summary()
        if summary:
            prompt = f"{prompt}\n\nCurrent workspace:\n{summary}"
        return prompt

    def workspace_summary(self) -> str:
        """A short, bounded orientation block (cached per workspace)."""

        if self._workspace is None:
            return "No project is currently open."
        if self._cached_summary is not None:
            return self._cached_summary

        root = self._workspace.root
        lines = [f"Root: {root}", f"Project folder: {root.name}"]
        try:
            entries = self._workspace.list_dir(".")
        except Exception as exc:
            logger.debug("Could not summarise workspace: %s", exc)
            self._cached_summary = "\n".join(lines)
            return self._cached_summary

        shown = entries[: self._max_structure_entries]
        lines.append(f"Top-level entries ({len(entries)} total):")
        for entry in shown:
            marker = "/" if entry.is_dir else ""
            lines.append(f"  {entry.name}{marker}")
        if len(entries) > len(shown):
            lines.append(f"  ... and {len(entries) - len(shown)} more")
        lines.append("Use project_structure / read_file / search_content for details.")
        self._cached_summary = "\n".join(lines)
        return self._cached_summary

    # -- assembly ---------------------------------------------------------
    def build(
        self,
        user_message: str,
        *,
        history: list[Message] | None = None,
        tools: list[dict[str, Any]] | None = None,
        include_workspace: bool = True,
    ) -> ContextBundle:
        """Assemble the messages and tools for one agent turn.

        ``history`` is expected to contain completed turns only (no tool
        results); the Agent Core appends those itself.
        """

        from quantsmind_agent.schema import SystemMessage

        system_text = DEFAULT_SYSTEM_PROMPT
        extra: list[str] = []
        if self._custom_prompt:
            extra.append("Operator instructions:\n" + self._custom_prompt)
        if include_workspace:
            extra.append(f"Current workspace:\n{self.workspace_summary()}")
        if extra:
            system_text = system_text + "\n\n" + "\n\n".join(extra)

        messages: list[Message] = [SystemMessage(content=system_text)]
        messages.extend(self._trim_history(history or []))
        messages.append(UserMessage(content=user_message))

        bundle = ContextBundle(
            system_prompt=system_text,
            messages=messages,
            tools=list(tools or []),
            workspace_summary=self.workspace_summary(),
            estimated_chars=self._estimate(messages),
        )
        return bundle

    def _trim_history(self, history: list[Message]) -> list[Message]:
        """Keep the most recent ``history_turns`` user/assistant exchanges."""

        if self._history_turns <= 0:
            return []
        # Walk backwards collecting user/assistant turns, then restore order.
        kept: list[Message] = []
        users = 0
        for message in reversed(history):
            role = message.role.value
            if role == "user":
                if users >= self._history_turns:
                    break
                users += 1
            elif role not in ("assistant", "system", "tool"):
                continue
            kept.append(message)
            if self._total_chars(kept) > self._max_context_chars:
                kept.pop()
                break
        kept.reverse()
        return kept

    def _total_chars(self, messages: list[Message]) -> int:
        return sum(len(message.content) for message in messages)

    def _estimate(self, messages: list[Message]) -> int:
        # ~4 characters per token is a safe upper bound for budget planning.
        return self._total_chars(messages) // 4
