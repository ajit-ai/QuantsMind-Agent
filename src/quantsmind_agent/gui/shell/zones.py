"""Workbench zones: activity bar, trust banner and bottom panel."""

from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QButtonGroup,
    QComboBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QTabWidget,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from quantsmind_agent.gui.shell.terminal_widget import TerminalWidget

__all__ = ["ActivityBar", "BottomPanel", "TrustBanner"]


@dataclass(frozen=True, slots=True)
class Activity:
    id: str
    label: str
    tooltip: str
    enabled: bool = True


class ActivityBar(QFrame):
    """Secondary workbench navigation inside Development Studio.

    The product's primary navigation is the Studio header; these tabs switch the
    side panel within Development Studio (files, search, project and sessions).
    """

    activity_selected = Signal(str)

    def __init__(self, activities: list[Activity], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("ActivityBar")
        self.setFixedHeight(34)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(10, 0, 10, 0)
        layout.setSpacing(2)
        self._group = QButtonGroup(self)
        self._group.setExclusive(True)
        self._buttons: dict[str, QToolButton] = {}
        for activity in activities:
            button = QToolButton()
            button.setText(activity.label)
            button.setToolTip(activity.tooltip)
            button.setCheckable(True)
            button.setEnabled(activity.enabled)
            button.setObjectName("ActivityButton")
            button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)
            button.setMinimumWidth(78)
            button.setFixedHeight(32)
            button.clicked.connect(
                lambda _checked=False, name=activity.id: self.activity_selected.emit(name)
            )
            layout.addWidget(button)
            self._group.addButton(button)
            self._buttons[activity.id] = button
        layout.addStretch(1)

    def select(self, activity_id: str) -> None:
        button = self._buttons.get(activity_id)
        if button is not None and button.isEnabled():
            button.setChecked(True)

    def current(self) -> str | None:
        for activity_id, button in self._buttons.items():
            if button.isChecked():
                return activity_id
        return None


class TrustBanner(QFrame):
    """Persistent restricted-mode explanation with a trust action."""

    trust_requested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("TrustBanner")
        layout = QHBoxLayout(self)
        layout.setContentsMargins(10, 6, 10, 6)
        self.label = QLabel("")
        self.label.setWordWrap(True)
        layout.addWidget(self.label, 1)
        self.trust_button = QPushButton("Trust workspace")
        self.trust_button.clicked.connect(self.trust_requested.emit)
        layout.addWidget(self.trust_button)
        self.setVisible(False)

    def set_restricted(self, *, workspace: str | None) -> None:
        if workspace is None:
            self.setVisible(False)
            return
        self.label.setText(
            f"{workspace} is open in restricted mode. You can browse, edit and save files. "
            "The agent, agent commands and indexing are disabled until you trust this workspace."
        )
        self.setVisible(True)


class BottomPanel(QFrame):
    """Problems (LSP diagnostics + status), Git, and interactive terminal."""

    terminal_input_submitted = Signal(str)
    root_selected = Signal(str)
    profile_selected = Signal(str)
    add_root_requested = Signal()
    remove_root_requested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("BottomPanel")
        self.setMaximumHeight(200)
        self.setMinimumHeight(100)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.tabs = QTabWidget()
        self.tabs.setDocumentMode(True)
        self.problems = QLabel(
            "No problems. No language server is configured, so Tier-A AST "
            "intelligence is active; diagnostics are unavailable."
        )
        self.problems.setWordWrap(True)
        self.problems.setObjectName("StatusText")
        self.output = TerminalWidget()
        self.output.setObjectName("TerminalWidget")
        self.terminal_input = QLineEdit()
        self.terminal_input.setPlaceholderText("Type a command and press Enter (workspace shell)…")
        self.terminal_input.setObjectName("TerminalInput")
        self.terminal_input.returnPressed.connect(
            lambda: self.terminal_input_submitted.emit(self.terminal_input.text())
        )
        self.git_view = QLabel(
            "No Git status loaded. Open a trusted workspace to view repository status."
        )
        self.git_view.setWordWrap(True)
        self.git_view.setObjectName("GitStatus")
        self.root_selector = QComboBox()
        self.root_selector.setObjectName("RootSelector")
        self.root_selector.setToolTip("Project root (multi-root workspace)")
        self.root_selector.currentTextChanged.connect(self.root_selected.emit)
        self.add_root_button = QPushButton("Add Root…")
        self.add_root_button.setToolTip("Register another project root")
        self.add_root_button.clicked.connect(self.add_root_requested.emit)
        self.remove_root_button = QPushButton("Remove Root")
        self.remove_root_button.setToolTip("Remove the selected non-primary root")
        self.remove_root_button.clicked.connect(self.remove_root_requested.emit)
        self.root_status = QLabel("Roots: none")
        self.root_status.setObjectName("StatusText")
        self.root_status.setWordWrap(True)
        self.profile_selector = QComboBox()
        self.profile_selector.setObjectName("ProfileSelector")
        self.profile_selector.setToolTip("Engineering profile")
        self.profile_selector.currentTextChanged.connect(self.profile_selected.emit)
        self.problems_page = _page("Problems", self.problems)
        self.output_page = _terminal_page("Terminal", self.output, self.terminal_input)
        git_host = QWidget()
        git_layout = QVBoxLayout(git_host)
        git_layout.setContentsMargins(10, 8, 10, 8)
        git_selectors = QHBoxLayout()
        git_selectors.addWidget(QLabel("Root:"))
        git_selectors.addWidget(self.root_selector)
        git_selectors.addWidget(self.add_root_button)
        git_selectors.addWidget(self.remove_root_button)
        git_selectors.addWidget(QLabel("Profile:"))
        git_selectors.addWidget(self.profile_selector)
        git_selectors.addStretch(1)
        git_layout.addLayout(git_selectors)
        git_layout.addWidget(self.root_status)
        git_layout.addWidget(self.git_view, 1)
        self.git_page = git_host
        self.tabs.addTab(self.problems_page, "Problems")
        self.tabs.addTab(self.output_page, "Terminal")
        self.tabs.addTab(self.git_page, "Git")
        layout.addWidget(self.tabs)
        self.setVisible(False)

    def show_output(self, text: str) -> None:
        self.output.feed_text(text)
        self.tabs.setCurrentWidget(self.output_page)
        self.setVisible(True)

    def show_problems(self) -> None:
        self.tabs.setCurrentWidget(self.problems_page)
        self.setVisible(True)

    def show_git(self) -> None:
        self.tabs.setCurrentWidget(self.git_page)
        self.setVisible(True)

    def set_problems(
        self, diagnostics: dict[str, list[dict[str, object]]], *, status: str = ""
    ) -> None:
        """Replace the Problems tab with real diagnostics (or a truthful state)."""
        lines: list[str] = []
        if status:
            lines.append(status)
        total = 0
        for uri in sorted(diagnostics):
            items = diagnostics[uri]
            total += len(items)
            for diag in items:
                rng = diag.get("range", {}) if isinstance(diag, dict) else {}
                start = rng.get("start", {}) if isinstance(rng, dict) else {}
                loc = f"{start.get('line', 0) + 1}:{start.get('character', 0) + 1}"
                msg = diag.get("message", "") if isinstance(diag, dict) else ""
                sev = diag.get("severity", "") if isinstance(diag, dict) else ""
                lines.append(f"{uri.split('/')[-1]} {loc} [{sev}] {msg}")
        if total == 0:
            base = status or "No problems."
            if "No problems" not in base:
                base = f"{base}\nNo problems."
            self.problems.setText(base)
        else:
            self.problems.setText("\n".join(lines))

    def set_git_status(self, text: str) -> None:
        self.git_view.setText(text or "Clean working tree.")

    def set_roots(self, root_ids: list[str], current: str = "", *, primary: str = "") -> None:
        self.root_selector.blockSignals(True)
        self.root_selector.clear()
        for rid in root_ids:
            label = f"{rid} (primary)" if rid == primary else rid
            self.root_selector.addItem(label, rid)
        if current:
            index = next((i for i, rid in enumerate(root_ids) if rid == current), -1)
            if index >= 0:
                self.root_selector.setCurrentIndex(index)
        self.root_selector.blockSignals(False)
        if root_ids:
            primary_label = primary or current or root_ids[0]
            self.root_status.setText(
                f"Roots ({len(root_ids)}): {', '.join(root_ids)} · primary: {primary_label}"
            )
        else:
            self.root_status.setText("Roots: none — open a workspace to register roots.")
        # Remove is enabled only when more than one root exists (non-primary
        # removal never silently redirects operations: it only edits the list).
        self.remove_root_button.setEnabled(len(root_ids) > 1)

    def set_profiles(self, profile_ids: list[str], current: str = "") -> None:
        self.profile_selector.blockSignals(True)
        self.profile_selector.clear()
        self.profile_selector.addItems(profile_ids or ["(none)"])
        if current:
            self.profile_selector.setCurrentText(current)
        self.profile_selector.blockSignals(False)

    def toggle(self) -> None:
        # isVisible() is also False while an ancestor (the window) is hidden;
        # isHidden() reports only an explicit hide of this widget.
        self.setVisible(self.isHidden())


def _page(title: str, widget: QWidget) -> QWidget:
    page = QWidget()
    layout = QVBoxLayout(page)
    layout.setContentsMargins(10, 8, 10, 8)
    heading = QLabel(title)
    heading.setObjectName("SectionLabel")
    layout.addWidget(heading)
    layout.addWidget(widget)
    layout.addStretch(1)
    return page


def _terminal_page(title: str, widget: QWidget, input_widget: QWidget) -> QWidget:
    page = QWidget()
    layout = QVBoxLayout(page)
    layout.setContentsMargins(10, 8, 10, 8)
    heading = QLabel(title)
    heading.setObjectName("SectionLabel")
    layout.addWidget(heading)
    layout.addWidget(widget, 1)
    layout.addWidget(input_widget)
    return page
