"""Tool-activity panel.

Renders one row per tool call with its state (``started`` / ``completed`` /
``failed``) so the user can see exactly what the agent touched while it works.
"""

from __future__ import annotations

import time

from PySide6.QtGui import QColor
from PySide6.QtWidgets import QListWidget, QListWidgetItem, QWidget

from quantsmind_agent.gui.style import COLORS

__all__ = ["ActivityView"]

_STATE_COLORS = {
    "started": COLORS["accent"],
    "completed": COLORS["success"],
    "failed": COLORS["error"],
    "notice": COLORS["muted"],
    "awaiting": COLORS["warning"],
}

_STATE_MARKS = {
    "started": "\u25b8",
    "completed": "\u2713",
    "failed": "\u2717",
    "notice": "\u00b7",
    "awaiting": "\u23f3",
}

_MAX_ROWS = 300


class ActivityView(QListWidget):
    """Bounded, newest-last list of tool activity."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setSelectionMode(QListWidget.SelectionMode.NoSelection)
        self.setAlternatingRowColors(False)
        self.setWordWrap(True)
        self.setMinimumHeight(90)

    def record(self, tool: str, summary: str, state: str) -> None:
        """Append one activity row."""

        stamp = time.strftime("%H:%M:%S")
        mark = _STATE_MARKS.get(state, "\u00b7")
        text = f"{mark} {stamp}  {tool}"
        if summary:
            text = f"{text} \u2014 {summary}"

        item = QListWidgetItem(text)
        item.setForeground(QColor(_STATE_COLORS.get(state, COLORS["muted"])))
        item.setToolTip(text)
        self.addItem(item)
        while self.count() > _MAX_ROWS:
            self.takeItem(0)
        self.scrollToBottom()

    def notice(self, text: str) -> None:
        """Append a non-tool line (permission decision, provider event...)."""

        self.record("activity", text, "notice")

    def clear_activity(self) -> None:
        self.clear()

    @property
    def row_count(self) -> int:
        return self.count()

    def texts(self) -> list[str]:
        """All row texts (used by GUI tests)."""

        return [self.item(index).text() for index in range(self.count())]

    def last_text(self) -> str:
        if self.count() == 0:
            return ""
        item = self.item(self.count() - 1)
        return item.text() if item else ""
