"""A single-line, non-modal notification strip."""

from __future__ import annotations

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QPushButton, QWidget

__all__ = ["NotificationBar"]


class NotificationBar(QFrame):
    """Shows one message at a time, with a close button.

    Errors persist until dismissed; informational messages fade after a few
    seconds. Nothing here opens a modal dialog, so a notification can never
    block the workbench.
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("NotificationBar")
        self.setVisible(False)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(8, 4, 8, 4)
        layout.setSpacing(8)
        self._label = QLabel("")
        self._label.setWordWrap(True)
        layout.addWidget(self._label, 1)
        self._close = QPushButton("Dismiss")
        self._close.clicked.connect(self.clear)
        layout.addWidget(self._close)
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self.clear)
        self._kind = "info"

    def show_message(self, text: str, *, kind: str = "info", timeout_ms: int = 6000) -> None:
        self._kind = kind
        self._label.setText(text)
        self._label.setProperty("kind", kind)
        self.setProperty("kind", kind)
        self.style().unpolish(self)
        self.style().polish(self)
        self.setVisible(bool(text))
        self._timer.stop()
        if kind != "error" and timeout_ms > 0:
            self._timer.start(timeout_ms)

    def clear(self) -> None:
        self._timer.stop()
        self._label.setText("")
        self.setVisible(False)

    def current_text(self) -> str:
        return self._label.text()

    @property
    def kind(self) -> str:
        return self._kind
