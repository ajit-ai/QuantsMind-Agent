"""Onboarding wizard dialog (WP2.4 of v1.1)."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from quantsmind_agent.services.onboarding import OnboardingState, OnboardingStore

__all__ = ["OnboardingDialog"]


class OnboardingDialog(QDialog):
    def __init__(self, root: str | Path, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("OnboardingDialog")
        self.store = OnboardingStore(root)
        self.state: OnboardingState = self.store.load()
        self.setWindowTitle("Getting started")
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("QuantsMind-Agent onboarding — three quick steps."))
        self.provider_input = QLineEdit()
        self.provider_input.setPlaceholderText(
            "Provider API key (optional now, or set in Settings)…"
        )
        layout.addWidget(QLabel("1 · Provider (BYOK; skip and set later in Settings)"))
        layout.addWidget(self.provider_input)
        self.trust_check = QPushButton(
            "I understand: open this folder trusted? (use workspace trust, not this dialog)"
        )
        self.trust_check.setCheckable(True)
        layout.addWidget(QLabel("2 · Workspace trust (use the Trust action in the header)"))
        self.profile_list = QListWidget()
        self.profile_list.addItem("(no profile yet — create one in Dev Studio)")
        layout.addWidget(QLabel("3 · Engineering profile (select or create in Dev Studio)"))
        layout.addWidget(self.profile_list)
        self.status = QLabel(f"Current step: {self.state.current}")
        layout.addWidget(self.status)
        buttons = QHBoxLayout()
        self.skip_button = QPushButton("Skip for now")
        self.skip_button.clicked.connect(self._skip)
        buttons.addWidget(self.skip_button)
        self.done_button = QPushButton("Mark steps complete")
        self.done_button.clicked.connect(self._mark_done)
        buttons.addWidget(self.done_button)
        layout.addLayout(buttons)

    def _skip(self) -> None:
        self.state = OnboardingState(provider_done=True, trust_done=True, profile_done=True)
        self.store.save(self.state)
        self.accept()

    def _mark_done(self) -> None:
        self.state.provider_done = True
        if self.trust_check.isChecked():
            self.state.trust_done = True
        self.state.profile_done = True
        self.store.save(self.state)
        self.status.setText(f"Current step: {self.state.current}")
        if self.state.complete:
            self.accept()
