"""Background execution of the Agent Core.

Qt widgets may only be touched from the GUI thread, so the agent loop runs on a
:class:`QThread` and communicates back through Qt signals. This is what keeps
the window responsive during long model calls and tool runs.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from PySide6.QtCore import QObject, QThread, QTimer, Signal

from quantsmind_agent.agent import AgentCore, AgentOutcome, StreamEmitter
from quantsmind_agent.tools import ToolResult

__all__ = ["AgentWorker", "AgentWorkerBridge"]


class AgentWorker(QObject):
    """Runs one :meth:`AgentCore.run` call on a worker thread."""

    started_turn = Signal()
    text_delta = Signal(str)
    tool_activity = Signal(str, str, str)  # tool name, summary, state
    finished = Signal(object)  # AgentOutcome
    failed = Signal(str)

    def __init__(self, agent: AgentCore) -> None:
        super().__init__()
        self._agent = agent

    def run(self, user_text: str) -> None:
        """Slot executed on the worker thread."""

        emitter = StreamEmitter(
            on_text=self.text_delta.emit,
            on_tool_start=lambda name, args: self.tool_activity.emit(name, args, "started"),
            on_tool_end=lambda result: self._tool_finished(result),
        )
        try:
            self.started_turn.emit()
            outcome: AgentOutcome = self._agent.run(user_text, emitter=emitter)
        except Exception as exc:
            self.failed.emit(str(exc))
            return
        self.finished.emit(outcome)

    def _tool_finished(self, result: ToolResult) -> None:
        state = "completed" if result.ok else "failed"
        summary = result.summary if result.ok else (result.error or result.summary)
        self.tool_activity.emit(result.tool, summary, state)

    def cancel(self) -> None:
        """Ask the agent to stop at the next iteration boundary."""

        self._agent.cancel()


class AgentWorkerBridge(QObject):
    """Owns the thread and re-exposes the worker's signals to the GUI.

    The agent is resolved through a callable on every turn, because
    :class:`~quantsmind_agent.app.ApplicationContext` rebuilds the Agent Core
    whenever the workspace or configuration changes. Holding a direct
    reference would silently run the GUI against a stale agent with no tools.
    """

    text_delta = Signal(str)
    tool_activity = Signal(str, str, str)
    turn_started = Signal()
    turn_finished = Signal(object)
    turn_failed = Signal(str)
    busy_changed = Signal(bool)

    def __init__(self, agent: AgentCore | Callable[[], AgentCore]) -> None:
        super().__init__()
        self._resolve = agent if callable(agent) else (lambda a=agent: a)
        self._thread: QThread | None = None
        self._worker: AgentWorker | None = None

    @property
    def is_busy(self) -> bool:
        return self._thread is not None and self._thread.isRunning()

    def start(self, user_text: str) -> bool:
        """Start a turn. Returns ``False`` when one is already running."""

        if self.is_busy:
            return False

        # Discard any stale cancellation so a previous Stop never leaks into
        # this turn; a Stop pressed after this point is honoured.
        self._resolve().reset_cancel()

        thread = QThread()
        worker = AgentWorker(self._resolve())
        worker.moveToThread(thread)

        # ``QThread.started`` carries no argument, so the request text is
        # captured here and passed explicitly when the worker starts.
        thread.started.connect(lambda: worker.run(user_text))

        # NOTE: the worker's signals are connected to *methods*, never to another
        # signal's ``emit``. Cross-thread signal-to-signal connections silently
        # drop events in PySide6, which would lose tool activity. Connecting to
        # a real slot also guarantees queued (thread-safe) delivery.
        worker.text_delta.connect(self._on_text_delta)
        worker.tool_activity.connect(self._on_tool_activity)
        worker.started_turn.connect(self._on_turn_started)
        worker.finished.connect(self._on_finished)
        worker.failed.connect(self._on_failed)
        worker.finished.connect(thread.quit)
        worker.failed.connect(thread.quit)

        self._thread = thread
        self._worker = worker
        self.busy_changed.emit(True)
        thread.start()
        return True

    def cancel(self) -> None:
        """Ask the running turn to stop; ignored when nothing is running."""

        if self._worker is not None and self.is_busy:
            self._worker.cancel()

    def _cleanup(self) -> None:
        thread = self._thread
        worker = self._worker
        self._thread = None
        self._worker = None
        if thread is not None:
            thread.quit()
            thread.wait(5000)
            thread.deleteLater()
        if worker is not None:
            worker.deleteLater()
        self.busy_changed.emit(False)

    def _on_finished(self, outcome: Any) -> None:
        self._cleanup()
        # Defer to the event loop so tool-activity events that the worker
        # already posted are rendered *before* the final answer.
        QTimer.singleShot(0, lambda: self.turn_finished.emit(outcome))

    def _on_failed(self, message: str) -> None:
        self._cleanup()
        QTimer.singleShot(0, lambda: self.turn_failed.emit(message))

    def _on_text_delta(self, delta: str) -> None:
        self.text_delta.emit(delta)

    def _on_tool_activity(self, tool: str, summary: str, state: str) -> None:
        self.tool_activity.emit(tool, summary, state)

    def _on_turn_started(self) -> None:
        self.turn_started.emit()
