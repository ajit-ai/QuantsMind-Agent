"""The branded Studio header and its five primary destinations.

The header carries the product identity and the navigation: Engineering Home,
AI Engineering, Development Studio, ChangeSets and Control Center. The labels are
the product's own, not generic IDE verbs, because the destinations are the
product's vocabulary.
"""

from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QButtonGroup,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

__all__ = ["DESTINATIONS", "DESTINATION_LABELS", "Destination", "StudioHeader"]


@dataclass(frozen=True, slots=True)
class Destination:
    id: str
    label: str
    tooltip: str
    shortcut: str


#: The five primary destinations, in navigation order.
DESTINATIONS: tuple[Destination, ...] = (
    Destination("home", "Home", "Your project, tasks and changes at a glance", "Ctrl+1"),
    Destination("ai", "AI Engineering", "Compose and run an AI engineering task", "Ctrl+2"),
    Destination("development", "Development Studio", "Browse and edit project files", "Ctrl+3"),
    Destination("changes", "ChangeSets", "Review changes recorded by engineering tasks", "Ctrl+4"),
    Destination(
        "control", "Control Center", "Providers, trust, permissions and security", "Ctrl+5"
    ),
)

DESTINATION_LABELS: dict[str, str] = {item.id: item.label for item in DESTINATIONS}


class StudioHeader(QFrame):
    """Brand block, destination tabs and the live workspace context chips."""

    destination_selected = Signal(str)
    open_workspace_requested = Signal()
    settings_requested = Signal()

    def __init__(
        self,
        destinations: tuple[Destination, ...] = DESTINATIONS,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("AppHeader")
        outer = QVBoxLayout(self)
        outer.setContentsMargins(18, 9, 18, 0)
        outer.setSpacing(0)

        top = QHBoxLayout()
        top.setSpacing(10)
        mark = QLabel("QM")
        mark.setObjectName("BrandMark")
        top.addWidget(mark)

        brand = QVBoxLayout()
        brand.setSpacing(0)
        name = QLabel("QUANTSMIND")
        name.setObjectName("BrandName")
        brand.addWidget(name)
        sub = QLabel("ENGINEERING STUDIO")
        sub.setObjectName("BrandSub")
        brand.addWidget(sub)
        top.addLayout(brand)
        top.addSpacing(10)

        self.workspace_chip = QLabel("No project")
        self.workspace_chip.setObjectName("WorkspaceChip")
        top.addWidget(self.workspace_chip)

        self.trust_chip = QLabel("No workspace")
        self.trust_chip.setObjectName("StatusChip")
        top.addWidget(self.trust_chip)

        top.addStretch(1)

        self.model_chip = QLabel("")
        self.model_chip.setObjectName("StatusChip")
        top.addWidget(self.model_chip)

        self.open_button = QPushButton("Open project")
        self.open_button.setToolTip("Open a project folder (Ctrl+O)")
        self.open_button.clicked.connect(self.open_workspace_requested.emit)
        top.addWidget(self.open_button)

        self.settings_button = QPushButton("Settings")
        self.settings_button.setObjectName("GhostButton")
        self.settings_button.setToolTip("Provider, credential and agent settings (Ctrl+K)")
        self.settings_button.clicked.connect(self.settings_requested.emit)
        top.addWidget(self.settings_button)

        outer.addLayout(top)

        nav = QHBoxLayout()
        nav.setSpacing(2)
        nav.setContentsMargins(0, 4, 0, 0)
        self._group = QButtonGroup(self)
        self._group.setExclusive(True)
        self._buttons: dict[str, QPushButton] = {}
        for destination in destinations:
            button = QPushButton(destination.label)
            button.setObjectName("DestinationButton")
            button.setCheckable(True)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.setToolTip(f"{destination.tooltip} ({destination.shortcut})")
            button.clicked.connect(
                lambda _checked=False, name=destination.id: self.destination_selected.emit(name)
            )
            nav.addWidget(button)
            self._group.addButton(button)
            self._buttons[destination.id] = button
        nav.addStretch(1)
        outer.addLayout(nav)

    def select(self, destination_id: str) -> None:
        button = self._buttons.get(destination_id)
        if button is not None:
            button.setChecked(True)

    def current(self) -> str | None:
        for destination_id, button in self._buttons.items():
            if button.isChecked():
                return destination_id
        return None

    def set_workspace(self, label: str, *, tooltip: str = "") -> None:
        self.workspace_chip.setText(label or "No project")
        self.workspace_chip.setToolTip(tooltip)

    def set_model(self, label: str) -> None:
        self.model_chip.setText(label)
        self.model_chip.setVisible(bool(label))

    def set_trust(self, state: str, *, detail: str = "") -> None:
        caption = {
            "trusted": "Trusted",
            "restricted": "Restricted",
            "none": "No workspace",
        }.get(state, state or "No workspace")
        tone = {"trusted": "success", "restricted": "warning"}.get(state, "")
        self.trust_chip.setText(caption)
        self.trust_chip.setToolTip(detail)
        self.trust_chip.setProperty("tone", tone)
        style = self.trust_chip.style()
        if style is not None:
            style.unpolish(self.trust_chip)
            style.polish(self.trust_chip)

    def describe(self) -> dict[str, object]:
        return {
            "destinations": list(self._buttons),
            "current": self.current(),
            "workspace": self.workspace_chip.text(),
            "trust": self.trust_chip.text(),
            "model": self.model_chip.text(),
        }
