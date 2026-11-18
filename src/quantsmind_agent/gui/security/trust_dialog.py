"""Workspace trust prompt.

The decision is explicit and made by a person. The dialog never infers trust
from the folder's contents, and its default choice is the restrained one:
opening a project in restricted mode keeps browsing, editing and saving
available while withholding the agent, agent commands and indexing.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

__all__ = ["TrustDialog", "ask_workspace_trust"]


class TrustDialog(QDialog):
    """Ask whether a folder may be used by the agent."""

    def __init__(self, workspace: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.workspace = workspace
        #: ``True`` = trusted, ``False`` = restricted, ``None`` = cancelled.
        self.decision: bool | None = None
        self.setWindowTitle("Open this folder?")
        self.setModal(True)
        self.setMinimumWidth(520)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(10)

        heading = QLabel(f"Open {workspace}?")
        heading.setWordWrap(True)
        heading.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        heading.setStyleSheet("font-size:14px;font-weight:600;")
        layout.addWidget(heading)

        explanation = QLabel(
            "Trusting a folder lets the AI agent read its files, propose and make "
            "changes (each change still asks for approval where required), and index "
            "the project for navigation.\n\n"
            "Restricted mode still lets you browse, edit and save files yourself, and "
            "search or quick-open them locally. It disables the agent, agent commands "
            "and project indexing."
        )
        explanation.setWordWrap(True)
        layout.addWidget(explanation)

        note = QLabel(
            "Trust is decided by folder path. Project contents cannot grant it, and "
            "you can revoke it later from the command palette or Security."
        )
        note.setObjectName("StatusText")
        note.setWordWrap(True)
        layout.addWidget(note)

        self.restricted_button = QPushButton("Open in Restricted Mode")
        self.restricted_button.setToolTip("Browse and edit only; the agent stays disabled")
        self.restricted_button.clicked.connect(lambda: self._choose(False))
        layout.addWidget(self.restricted_button)

        self.trust_button = QPushButton("Trust and Open")
        self.trust_button.setObjectName("PrimaryButton")
        self.trust_button.setToolTip("Enable the agent and project indexing for this folder")
        self.trust_button.clicked.connect(lambda: self._choose(True))
        layout.addWidget(self.trust_button)

        self.cancel_button = QPushButton("Cancel")
        self.cancel_button.clicked.connect(self.reject)
        layout.addWidget(self.cancel_button)

        # Security-first default: Enter opens restricted mode.
        self.restricted_button.setDefault(True)
        self.restricted_button.setFocus()

    def _choose(self, trusted: bool) -> None:
        self.decision = trusted
        self.accept()


def ask_workspace_trust(workspace: str, parent: QWidget | None = None) -> bool | None:
    """Prompt for a decision. ``None`` means the user cancelled."""

    dialog = TrustDialog(workspace, parent=parent)
    dialog.exec()
    return dialog.decision
