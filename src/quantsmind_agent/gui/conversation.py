"""Conversation rendering.

:class:`ConversationView` renders the session transcript as HTML so that roles,
streaming updates and tool notices stay visually distinct without pulling in a
Markdown renderer. The GUI owns this widget; it holds no agent logic.
"""

from __future__ import annotations

import html
import time

from PySide6.QtWidgets import QTextBrowser, QWidget

from quantsmind_agent.gui.style import ROLE_COLORS
from quantsmind_agent.schema import AssistantMessage, Message, UserMessage
from quantsmind_agent.tools import ToolResult

__all__ = ["ConversationView"]

_PLACEHOLDER = (
    "<p style='color:#6a7280'>"
    "Open a project, check your model in <b>Settings</b>, then ask something "
    "like &ldquo;Explain the structure of this project.&rdquo;"
    "</p>"
)


class ConversationView(QTextBrowser):
    """Append-only transcript of the current session."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setOpenExternalLinks(False)
        self.setReadOnly(True)
        self.setMinimumHeight(200)
        self._has_content = False
        self._streaming = False
        self._insert_placeholder()

    # -- state -------------------------------------------------------------
    @property
    def has_content(self) -> bool:
        return self._has_content

    def clear_transcript(self) -> None:
        """Reset to the empty placeholder (used by "New conversation")."""

        self.clear()
        self._has_content = False
        self._streaming = False
        self._insert_placeholder()

    # -- rendering ---------------------------------------------------------
    def append_user(self, text: str, *, stamp: str = "") -> None:
        self._ensure_placeholder()
        self._append_block("You", text, "user", stamp=stamp)
        self._has_content = True

    def append_assistant(self, text: str, *, meta: str = "", stamp: str = "") -> None:
        self._ensure_placeholder()
        suffix = (
            f"<br/><span style='color:#6a7280;font-size:11px'>{html.escape(meta)}</span>"
            if meta
            else ""
        )
        self._append_block("Assistant", text, "assistant", extra=suffix, stamp=stamp)
        self._has_content = True

    def begin_stream(self) -> None:
        """Start an assistant block that will be extended in place."""

        self._ensure_placeholder()
        self._streaming = True
        self._append_block("Assistant", "", "assistant")

    def append_stream_delta(self, delta: str) -> None:
        """Append streamed text to the in-place block."""

        if not self._streaming:
            self.begin_stream()
        cursor = self.textCursor()
        cursor.movePosition(cursor.MoveOperation.End)
        cursor.insertHtml(self._inline(html.escape(delta)))
        self.ensureCursorVisible()

    def end_stream(self, *, meta: str = "") -> None:
        """Close the in-place block."""

        if not self._streaming:
            return
        self._streaming = False
        if meta:
            cursor = self.textCursor()
            cursor.movePosition(cursor.MoveOperation.End)
            cursor.insertHtml(
                f"<br/><span style='color:#6a7280;font-size:11px'>{html.escape(meta)}</span>"
            )
            self.ensureCursorVisible()

    def append_tool_notice(self, name: str, summary: str, state: str, *, stamp: str = "") -> None:
        """Show one line of tool activity inside the transcript."""

        self._ensure_placeholder()
        colour = {
            "started": ROLE_COLORS["tool"],
            "completed": COLOURS_SUCCESS,
            "failed": ROLE_COLORS["error"],
        }.get(state, ROLE_COLORS["tool"])
        icon = {"started": "→", "completed": "✓", "failed": "✗"}.get(state, "•")
        body = html.escape(f"{icon} {name} — {summary}" if summary else f"{icon} {name}")
        self._append_block("Tool", body, "tool", accent=colour, stamp=stamp)
        self._has_content = True

    def append_notice(self, text: str, *, kind: str = "info") -> None:
        """Show a system-level notice (errors, cancellations, hints)."""

        self._ensure_placeholder()
        colour = ROLE_COLORS["error"] if kind == "error" else ROLE_COLORS["system"]
        label = "Error" if kind == "error" else "Notice"
        self._append_block(label, html.escape(text), "system", accent=colour)
        self._has_content = True

    def append_message(self, message: Message) -> None:
        """Render a stored :class:`Message` (used when restoring a session)."""

        if isinstance(message, UserMessage):
            self.append_user(message.content)
        elif isinstance(message, AssistantMessage):
            self.append_assistant(
                message.content,
                meta=message.model or "",
            )

    def append_tool_result(self, result: ToolResult, *, stamp: str = "") -> None:
        """Render a persisted tool result.

        One line: the tool, its outcome and how long it took. The structured
        payload is deliberately left out -- it can hold whole file contents or
        command output, which is not what someone reopening a conversation is
        reading. The full value stays in the record and in the JSON export.
        """

        if result.ok:
            summary = result.summary
        else:
            summary = result.error or result.summary
        if result.duration_ms:
            summary = f"{summary} ({result.duration_ms:.0f} ms)"
        self.append_tool_notice(
            result.tool,
            summary,
            "completed" if result.ok else "failed",
            stamp=stamp,
        )

    def append_interrupted_notice(self, outcome: str, *, detail: str = "") -> None:
        """Say how the last stored turn actually ended.

        A reopened conversation whose last turn never finished says so, rather than
        ending on the last thing that did succeed and leaving the reader to assume
        the request was answered.
        """

        text = {
            "cancelled": "You stopped this turn before it finished. It was interrupted, "
            "not completed, and nothing from it was replayed.",
            "failed": "This turn ended in an error. It is shown as it happened, not "
            "as a successful result.",
            "interrupted": "The application closed while this turn was running, so it "
            "was interrupted and never finished. Nothing from it was replayed.",
        }.get(outcome, "")
        if not text:
            return
        if detail:
            text = f"{text} {detail}"
        self.append_notice(text, kind="info")

    # -- internals ---------------------------------------------------------
    def _insert_placeholder(self) -> None:
        cursor = self.textCursor()
        cursor.insertHtml(_PLACEHOLDER)
        self.ensureCursorVisible()

    def _ensure_placeholder(self) -> None:
        """Guarantee the empty-state hint is the only content when idle."""

        if self._has_content:
            return
        if self.toPlainText().strip() in ("", _PLACEHOLDER_STRIPPED):
            self.clear()
            self._insert_placeholder()

    def _append_block(
        self,
        label: str,
        body_html: str,
        role: str,
        *,
        extra: str = "",
        accent: str | None = None,
        stamp: str = "",
    ) -> None:
        colour = accent or ROLE_COLORS.get(role, ROLE_COLORS["system"])
        shown_stamp = stamp or time.strftime("%H:%M:%S")
        html_block = (
            f"<table width='100%' cellspacing='0' cellpadding='4' "
            f"style='margin-top:10px'>"
            f"<tr><td style='color:{colour};font-weight:600;font-size:12px'>"
            f"{html.escape(label)} <span style='color:#9aa0a9;font-weight:400'>"
            f"{html.escape(shown_stamp)}</span>"
            f"</td></tr>"
            f"<tr><td style='color:#1c1f24'>{body_html}{extra}</td></tr></table>"
        )
        self._ensure_placeholder()
        cursor = self.textCursor()
        cursor.movePosition(cursor.MoveOperation.End)
        cursor.insertHtml(html_block)
        self.ensureCursorVisible()

    @staticmethod
    def _inline(text: str) -> str:
        """Wrap escaped text in a span that keeps inherited styling."""

        return f"<span style='color:{ROLE_COLORS['assistant']}'>{text}</span>"


COLOURS_SUCCESS = "#1f7a45"
_PLACEHOLDER_STRIPPED = (
    "Open a project, check your model in Settings, then ask something like "
    "\u201cExplain the structure of this project.\u201d"
)
