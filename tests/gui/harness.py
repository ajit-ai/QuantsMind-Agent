"""Shared fixtures for main-window GUI tests.

:class:`GuiHarness` builds a real :class:`ApplicationContext`, swaps only the
network provider for the scripted ``FakeModelProvider``, and constructs the real
:class:`MainWindow` on top of it.
"""

from __future__ import annotations

import threading
from pathlib import Path

from quantsmind_agent.app import ApplicationContext
from quantsmind_agent.gui.shell.window import MainWindow
from quantsmind_agent.gui.shell.worker import AgentWorkerBridge
from quantsmind_agent.providers.gateway import ChatRequest, ChatResponse
from quantsmind_agent.security.permissions import ApprovalOutcome
from tests.fakes import FakeModelProvider

__all__ = ["GuiHarness"]

#: Upper bound on how long one ``processEvents`` call may block the GUI thread.
_DRAIN_BUDGET_MS = 20


class GuiHarness:
    """Application context + window + scripted provider."""

    def __init__(self, context: ApplicationContext, provider: FakeModelProvider) -> None:
        self.context = context
        self.provider = provider
        #: Outcome applied to every dialog by :meth:`answer_approvals`.
        self._auto_approval: ApprovalOutcome | None = None
        #: Set when the worker thread reports that the turn has begun.
        self._turn_started = threading.Event()
        # The window creates and owns the worker bridge; reach it through the
        # window rather than building a second one.
        self.window = MainWindow(context)
        self.window.bridge.turn_started.connect(self._turn_started.set)

    @property
    def bridge(self) -> AgentWorkerBridge:
        return self.window.bridge

    def use_provider(self, provider: FakeModelProvider) -> None:
        """Replace the provider mid-test (used for error-path tests)."""

        self.provider = provider
        self.install_provider()

    def install_provider(self) -> None:
        """Install the scripted provider on the current agent's gateway."""

        self.context.agent._gateway.set_provider(self.provider)

    def open_project(self, path: Path | str) -> None:
        """Open ``path`` as a project the user has explicitly chosen to trust.

        GUI tests cannot answer the modal trust prompt, so the harness records
        the same explicit decision the folder picker would persist before the
        window opens the folder. Opening a project rebuilds the agent stack, so
        the scripted provider is reinstalled on the new gateway afterwards.
        """

        candidate = Path(path)
        if candidate.exists():
            self.context.trust_store.decide(candidate.resolve(), trusted=True)
        self.window._on_open_workspace(str(path))
        self.install_provider()

    def ask(self, text: str) -> None:
        """Type ``text`` and press Send, then wait for the worker to finish."""

        self.window.input.setPlainText(text)
        self.window._on_send_clicked()
        self.pump()

    def _drain(self, iterations: int) -> None:
        """Process queued events, yielding the GIL so the worker can run.

        Two deliberate choices:

        * the max-time overload of `processEvents`. The no-argument form can
          block indefinitely once a dialog is on screen, which strands the pump
          loop; the overload is guaranteed to return within `_DRAIN_BUDGET_MS`.
        * no argument-less `sendPostedEvents()`. It dispatches to receivers in
          *every* thread and re-enters handlers, which deadlocks as soon as a
          dialog is open.
        """

        from PySide6.QtCore import QEventLoop, QThread
        from PySide6.QtWidgets import QApplication

        flags = QEventLoop.ProcessEventsFlag.AllEvents
        for _ in range(iterations):
            QApplication.processEvents(flags, _DRAIN_BUDGET_MS)
            QThread.msleep(2)

    def pump(self, limit_ms: int = 30000) -> None:
        """Drain the turn started by the caller to completion.

        Two facts drive this loop:

        * ``QThread.isRunning()`` can still be false immediately after
          ``start()``, so waiting on ``bridge.is_busy`` alone can return before
          the worker has done anything. Wait for ``turn_started`` -- a direct
          signal with no timer in its path -- before trusting ``is_busy``.
        * ``QTimer`` timeouts are unreliable to depend on here, so any approval
          dialog is answered inline while draining rather than from a timer.
        """

        from PySide6.QtCore import QElapsedTimer

        timer = QElapsedTimer()
        timer.start()

        # Phase 1: wait until the worker has actually begun, so `is_busy` is
        # meaningful below.
        while not self._turn_started.is_set() and timer.elapsed() < limit_ms:
            self._drain(1)
            self._answer_pending_dialog()
        self._turn_started.clear()

        # Phase 2: run the turn to completion.
        while self.bridge.is_busy and timer.elapsed() < limit_ms:
            self._drain(1)
            self._answer_pending_dialog()

        # A short tail so trailing tool-activity events render after the answer.
        for _ in range(60):
            self._drain(1)
            self._answer_pending_dialog()

    def settle(self) -> None:
        """Deliver queued signals for a short while (worker may still run)."""

        self._drain(20)

    def shutdown(self, limit_ms: int = 10000) -> None:
        """Stop any running turn and release a waiting worker.

        Without this, a turn that is blocked in an approval handshake keeps its
        QThread alive and the interpreter waits for it at exit, so a *failed*
        test would hang the whole session instead of reporting.
        """

        from PySide6.QtCore import QElapsedTimer

        self.window.cancel_pending_approval()
        self._drain(10)
        timer = QElapsedTimer()
        timer.start()
        while self.bridge.is_busy and timer.elapsed() < limit_ms:
            self._drain(1)
            self._answer_pending_dialog()
        self.window.close()
        self._drain(20)

    def block_next_turn(self) -> threading.Event:
        """Make the provider block until the returned event is set."""

        release = threading.Event()
        original = self.provider.complete

        def blocking(request: ChatRequest) -> ChatResponse:
            release.wait(timeout=20)
            return original(request)

        self.provider.complete = blocking  # type: ignore[method-assign]
        return release

    # -- approvals ---------------------------------------------------------
    def answer_approvals(self, outcome: ApprovalOutcome) -> None:
        """Answer every approval dialog that appears with ``outcome``.

        No Qt timer is involved. ``QTimer`` timeouts are not dispatched by the
        synthetic ``processEvents`` pump this harness uses, so the decision is
        applied inline while draining instead. That also removes the race where
        a timer fires before the dialog exists and nobody ever answers it.
        """

        self._auto_approval = outcome

    def stop_answering_approvals(self) -> None:
        self._auto_approval = None

    def _answer_pending_dialog(self) -> bool:
        """Click the matching button if a dialog is up. Returns True if it acted."""

        dialog = self.window._active_dialog
        if dialog is None or self._auto_approval is None:
            return False
        if self._auto_approval is ApprovalOutcome.ALLOW_ONCE:
            dialog.allow_button.click()
        elif self._auto_approval is ApprovalOutcome.ALLOW_FOR_SESSION:
            button = getattr(dialog, "allow_session_button", None)
            if button is None:
                return False
            button.click()
        else:
            dialog.deny_button.click()
        return True

    def pump_until_approval(self, limit_ms: int = 5000) -> bool:
        """Deliver events until an approval dialog is on screen."""

        from PySide6.QtCore import QElapsedTimer

        timer = QElapsedTimer()
        timer.start()
        while timer.elapsed() < limit_ms:
            self._drain(1)
            if self.window._active_dialog is not None:
                return True
        return False

    def pending_approval_text(self) -> str:
        """Full text of the approval dialog currently shown."""

        dialog = self.window._active_dialog
        return dialog.full_text() if dialog is not None else ""

    def active_dialog_labels(self) -> list[str]:
        """Visible button labels of the current dialog (or an empty list)."""

        dialog = self.window._active_dialog
        return dialog.button_labels() if dialog is not None else []

    @property
    def transcript(self) -> str:
        return str(self.window.conversation.toPlainText())

    @property
    def activity(self) -> list[str]:
        return [str(row) for row in self.window.activity.texts()]
