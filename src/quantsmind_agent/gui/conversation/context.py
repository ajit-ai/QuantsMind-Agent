"""Explicit code-context attachments for the assistant.

Attaching is the authorisation. Nothing here reads a file or a selection on its
own, nothing is attached automatically when the editor changes, and the
attachment only travels when the user sends a message. The bar therefore shows
exactly what will be sent.

File contents are kept in the attachment object and are never logged.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

__all__ = ["ContextAttachment", "ContextBar"]

_MAX_CONTENT_CHARS = 40_000


def _fence(content: str) -> str:
    """A fence longer than any backtick run in ``content``."""

    longest = max((len(match) for match in re.findall(r"`+", content)), default=0)
    return "`" * max(3, longest + 1)


@dataclass(frozen=True, slots=True)
class ContextAttachment:
    """One user-chosen piece of code or one whole file."""

    kind: str  # "selection" | "file"
    relative: str
    content: str
    start_line: int = 1
    end_line: int = 0
    note: str = ""

    @property
    def label(self) -> str:
        if self.kind == "selection":
            lines = (
                f"L{self.start_line}"
                if self.end_line in (0, self.start_line)
                else f"L{self.start_line}-L{self.end_line}"
            )
            return f"selection \u00b7 {self.relative}:{lines}"
        return f"file \u00b7 {self.relative}"

    @property
    def key(self) -> tuple[str, str, int, int]:
        return (self.kind, self.relative, self.start_line, self.end_line)

    def prompt_block(self) -> str:
        heading = self.label
        note = f" ({self.note})" if self.note else ""
        fence = _fence(self.content)
        return f"Attached context \u2014 {heading}{note}\n{fence}\n{self.content}\n{fence}"


class ContextBar(QWidget):
    """Chips for what will be sent, plus the two explicit attach actions."""

    attach_selection_requested = Signal()
    attach_current_file_requested = Signal()
    attachments_changed = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._attachments: list[ContextAttachment] = []
        self._available = False

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(3)

        row = QHBoxLayout()
        row.setSpacing(6)
        self.attach_selection_button = QPushButton("Attach Selection")
        self.attach_selection_button.setToolTip(
            "Send the selected code with your next message (Ctrl+Shift+A)"
        )
        self.attach_selection_button.clicked.connect(self.attach_selection_requested.emit)
        self.attach_file_button = QPushButton("Attach File")
        self.attach_file_button.setToolTip("Send the current editor file with your next message")
        self.attach_file_button.clicked.connect(self.attach_current_file_requested.emit)
        self.clear_button = QPushButton("Clear")
        self.clear_button.setToolTip("Remove every attached item")
        self.clear_button.clicked.connect(self.clear)
        row.addWidget(self.attach_selection_button)
        row.addWidget(self.attach_file_button)
        row.addWidget(self.clear_button)
        row.addStretch(1)
        layout.addLayout(row)

        self.chips = QListWidget()
        self.chips.setFlow(QListWidget.Flow.LeftToRight)
        self.chips.setWrapping(False)
        self.chips.setSelectionMode(QListWidget.SelectionMode.NoSelection)
        self.chips.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.chips.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.chips.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.chips.setFixedHeight(30)
        self.chips.setToolTip("Attached context. Double-click an item to remove it.")
        self.chips.itemDoubleClicked.connect(self._on_item_double_clicked)
        layout.addWidget(self.chips)
        self.chips.setVisible(False)

        self.hint = QLabel("")
        self.hint.setObjectName("StatusText")
        self.hint.setWordWrap(True)
        layout.addWidget(self.hint)

        self.set_available(False)
        self._refresh()

    # -- availability ------------------------------------------------------
    def set_available(self, available: bool) -> None:
        self._available = bool(available)
        self.attach_selection_button.setEnabled(self._available)
        self.attach_file_button.setEnabled(self._available)
        self.clear_button.setEnabled(bool(self._attachments))
        if not self._available:
            self.hint.setText(
                "Open and trust a folder to attach code. Nothing is sent automatically."
            )
        elif self._attachments:
            self.hint.setText("Attached context will be sent with your next message.")
        else:
            self.hint.setText(
                "Attach a selection or the current file to include it in your next message."
            )

    @property
    def available(self) -> bool:
        return self._available

    # -- contents ----------------------------------------------------------
    def add_attachment(self, attachment: ContextAttachment) -> bool:
        if not self._available:
            return False
        content = attachment.content
        note = attachment.note
        if len(content) > _MAX_CONTENT_CHARS:
            content = content[:_MAX_CONTENT_CHARS]
            note = f"{note}; truncated to {_MAX_CONTENT_CHARS} characters".strip("; ")
            attachment = ContextAttachment(
                kind=attachment.kind,
                relative=attachment.relative,
                content=content,
                start_line=attachment.start_line,
                end_line=attachment.end_line,
                note=note,
            )
        if any(existing.key == attachment.key for existing in self._attachments):
            return False
        self._attachments.append(attachment)
        self._refresh()
        self.attachments_changed.emit()
        return True

    def attachments(self) -> tuple[ContextAttachment, ...]:
        return tuple(self._attachments)

    def prompt_suffix(self) -> str:
        if not self._attachments:
            return ""
        return "\n\n" + "\n\n".join(item.prompt_block() for item in self._attachments)

    def remove(self, index: int) -> None:
        if 0 <= index < len(self._attachments):
            del self._attachments[index]
            self._refresh()
            self.attachments_changed.emit()

    def clear(self) -> None:
        if not self._attachments:
            return
        self._attachments.clear()
        self._refresh()
        self.attachments_changed.emit()

    # -- rendering ---------------------------------------------------------
    def _refresh(self) -> None:
        self.chips.clear()
        for attachment in self._attachments:
            item = QListWidgetItem(attachment.label)
            item.setToolTip(
                f"{attachment.label}\nDouble-click to remove."
                + (f"\n{attachment.note}" if attachment.note else "")
            )
            self.chips.addItem(item)
        self.chips.setVisible(bool(self._attachments))
        self.clear_button.setEnabled(bool(self._attachments) and self._available)
        if not self._available:
            self.hint.setText(
                "Open and trust a folder to attach code. Nothing is sent automatically."
            )
        elif self._attachments:
            self.hint.setText("Attached context will be sent with your next message.")
        else:
            self.hint.setText(
                "Attach a selection or the current file to include it in your next message."
            )

    def _on_item_double_clicked(self, item: QListWidgetItem) -> None:
        self.remove(self.chips.row(item))
