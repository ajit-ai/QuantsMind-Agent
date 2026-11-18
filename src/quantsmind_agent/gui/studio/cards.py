"""Small presentation primitives shared by the Studio destinations.

They are deliberately dumb: a card is a frame with a title and a body, a badge
is a label with a status tone. None of them reads a service, which keeps the
visual language in one place and the data decisions in the window.
"""

from __future__ import annotations

from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QLayout,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

__all__ = [
    "Badge",
    "Card",
    "EmptyState",
    "StatCard",
    "clear_layout",
    "section_label",
]


def section_label(text: str) -> QLabel:
    label = QLabel(text.upper())
    label.setObjectName("SectionLabel")
    return label


def clear_layout(layout: QLayout) -> None:
    """Remove and destroy every widget and nested item in ``layout``."""

    while layout.count():
        item = layout.takeAt(0)
        if item is None:
            continue
        widget = item.widget()
        if widget is not None:
            widget.setParent(None)
            widget.deleteLater()
            continue
        child = item.layout()
        if child is not None:
            clear_layout(child)
            child.deleteLater()


class Badge(QLabel):
    """A compact status pill. Tones map to success, warning, error and accent."""

    def __init__(self, text: str = "", *, tone: str = "", parent: QWidget | None = None) -> None:
        super().__init__(text, parent)
        self.setObjectName("Badge")
        self.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed)
        self.set_tone(tone)

    def set_tone(self, tone: str) -> None:
        self.setProperty("tone", tone)
        style = self.style()
        if style is not None:
            style.unpolish(self)
            style.polish(self)


class Card(QFrame):
    """A panel with an optional heading and a vertical body."""

    def __init__(
        self,
        title: str = "",
        *,
        subtitle: str = "",
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("Card")
        self.body = QVBoxLayout(self)
        self.body.setContentsMargins(16, 14, 16, 14)
        self.body.setSpacing(9)
        self.title_label: QLabel | None = None
        self.heading: QHBoxLayout | None = None
        self.subtitle_label: QLabel | None = None
        if title:
            heading = QHBoxLayout()
            heading.setSpacing(8)
            self.title_label = QLabel(title)
            self.title_label.setObjectName("CardTitle")
            heading.addWidget(self.title_label)
            heading.addStretch(1)
            self.heading = heading
            self.body.addLayout(heading)
        if subtitle:
            self.subtitle_label = QLabel(subtitle)
            self.subtitle_label.setObjectName("CardBody")
            self.subtitle_label.setWordWrap(True)
            self.body.addWidget(self.subtitle_label)

    def add(self, widget: QWidget, *, stretch: int = 0) -> None:
        self.body.addWidget(widget, stretch)

    def add_action(self, text: str, *, primary: bool = False) -> QPushButton:
        button = QPushButton(text)
        if primary:
            button.setObjectName("PrimaryButton")
        self.body.addWidget(button)
        return button


class StatCard(QFrame):
    """One number with a label and an honest caption."""

    def __init__(
        self,
        label: str,
        value: str = "",
        *,
        caption: str = "",
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("StatCard")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 13, 16, 13)
        layout.setSpacing(4)
        self.label = QLabel(label.upper())
        self.label.setObjectName("MetricLabel")
        layout.addWidget(self.label)
        self.value_label = QLabel(value)
        self.value_label.setObjectName("MetricValue")
        self.value_label.setWordWrap(True)
        layout.addWidget(self.value_label)
        self.caption_label = QLabel(caption)
        self.caption_label.setObjectName("StatusText")
        self.caption_label.setWordWrap(True)
        layout.addWidget(self.caption_label)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)

    def set_value(self, value: str, *, caption: str = "") -> None:
        self.value_label.setText(value)
        self.caption_label.setText(caption)


class EmptyState(QFrame):
    """An intentional explanation of what to do next, with one action."""

    def __init__(
        self,
        title: str,
        body: str,
        *,
        action: str = "",
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("EmptyState")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 16, 18, 16)
        layout.setSpacing(6)
        self.title_label = QLabel(title)
        self.title_label.setObjectName("EmptyTitle")
        layout.addWidget(self.title_label)
        self.body_label = QLabel(body)
        self.body_label.setObjectName("EmptyBody")
        self.body_label.setWordWrap(True)
        layout.addWidget(self.body_label)
        self.action = QPushButton(action) if action else None
        if self.action is not None:
            self.action.setObjectName("PrimaryButton")
            row = QHBoxLayout()
            row.addWidget(self.action)
            row.addStretch(1)
            layout.addLayout(row)
