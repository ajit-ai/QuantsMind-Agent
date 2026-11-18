"""Workbench status bar.

The bar reports facts the user can act on: the model, the workspace and its
trust state, the cursor, encoding, language and process memory. It contains no
policy of its own.
"""

from __future__ import annotations

from PySide6.QtWidgets import QLabel, QStatusBar, QWidget

__all__ = ["WorkbenchStatusBar"]


class WorkbenchStatusBar(QStatusBar):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.model_label = QLabel("")
        self.workspace_label = QLabel("")
        self.trust_label = QLabel("")
        self.cursor_label = QLabel("Ln 1, Col 1")
        self.encoding_label = QLabel("")
        self.language_label = QLabel("")
        self.memory_label = QLabel("")
        self.activity_label = QLabel("")
        for label in (
            self.model_label,
            self.workspace_label,
            self.trust_label,
            self.cursor_label,
            self.encoding_label,
            self.language_label,
            self.memory_label,
            self.activity_label,
        ):
            label.setObjectName("StatusText")
        self.addWidget(self.model_label, 2)
        self.addWidget(self.workspace_label, 2)
        self.addWidget(self.trust_label)
        self.addPermanentWidget(self.cursor_label)
        self.addPermanentWidget(self.encoding_label)
        self.addPermanentWidget(self.language_label)
        self.addPermanentWidget(self.memory_label)
        self.addPermanentWidget(self.activity_label)

    def set_model(self, text: str) -> None:
        self.model_label.setText(text)

    def set_workspace(self, text: str) -> None:
        self.workspace_label.setText(text)

    def set_trust(self, state: str) -> None:
        self.trust_label.setText(f"Workspace: {state}" if state else "")

    def set_cursor(self, line: int, column: int) -> None:
        self.cursor_label.setText(f"Ln {line}, Col {column}")

    def set_encoding(self, encoding: str, newline: str) -> None:
        self.encoding_label.setText(f"{encoding}  {newline}" if encoding else "")

    def set_language(self, language: str) -> None:
        self.language_label.setText(language)

    def set_memory(self, text: str) -> None:
        self.memory_label.setText(f"Memory {text}")

    def set_activity(self, text: str) -> None:
        self.activity_label.setText(text)
