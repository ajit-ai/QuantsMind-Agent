"""Shared fixtures for main-window GUI tests.

:class:`GuiHarness` builds a real :class:`ApplicationContext`, swaps only the
network provider for the scripted ``FakeModelProvider``, and constructs the real
:class:`MainWindow` on top of it.
"""

from __future__ import annotations

import threading
from pathlib import Path

from quantsmind_agent.app import ApplicationContext
from quantsmind_agent.gui.main_window import MainWindow
from quantsmind_agent.gui.worker import AgentWorkerBridge
from quantsmind_agent.llm import ChatRequest, ChatResponse
from tests.fakes import FakeModelProvider

__all__ = ["GuiHarness"]


class GuiHarness:
    """Application context + window + scripted provider."""

    def __init__(self, context: ApplicationContext, provider: FakeModelProvider) -> None:
        self.context = context
        self.provider = provider
        # The window creates and owns the worker bridge; reach it through the
        # window rather than building a second one.
        self.window = MainWindow(context)

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
        """Open ``path`` exactly as the folder-picker would.

        Opening a project rebuilds the agent stack, so the scripted provider is
        reinstalled on the new gateway afterwards.
        """

        self.window._on_open_workspace(str(path))
        self.install_provider()

    def ask(self, text: str) -> None:
        """Type ``text`` and press Send, then wait for the worker to finish."""

        self.window.input.setPlainText(text)
        self.window._on_send_clicked()
        self.pump()

    def _drain(self, iterations: int) -> None:
        """Process queued events, yielding the GIL so the worker can run."""

        from PySide6.QtCore import QCoreApplication, QThread

        for _ in range(iterations):
            QCoreApplication.processEvents()
            QCoreApplication.sendPostedEvents()
            QThread.msleep(2)

    def pump(self) -> None:
        """Deliver queued signals until the worker thread is done."""

        from PySide6.QtCore import QElapsedTimer

        timer = QElapsedTimer()
        timer.start()
        while self.bridge.is_busy and timer.elapsed() < 20000:
            self._drain(1)
        self._drain(40)

    def settle(self) -> None:
        """Deliver queued signals for a short while (worker may still run)."""

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

    @property
    def transcript(self) -> str:
        return str(self.window.conversation.toPlainText())

    @property
    def activity(self) -> list[str]:
        return [str(row) for row in self.window.activity.texts()]
