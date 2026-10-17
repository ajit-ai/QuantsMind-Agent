"""The task panel: plan review, progress, approval and the final report.

This is a review-and-control surface, not a second chat window. Everything it
shows comes from the orchestrator's records -- step statuses, changed files,
check outcomes -- rather than from text the model produced, because the point of
showing progress is that it is evidence.

Threading follows the existing worker convention: the panel never touches the
orchestrator while it is running. It hands it to a :class:`TaskWorker` on a
:class:`QThread` and receives signals, so a slow step cannot freeze the window
and a closed window cannot be written to.

Every control reflects real state. Run is disabled without a valid approval,
Pause is disabled once the task is terminal, and Reject exists because "approve
everything" is not a choice.
"""

from __future__ import annotations

import logging

from PySide6.QtCore import QObject, QThread, Signal
from PySide6.QtWidgets import (
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from quantsmind_agent.orchestrator import AdvanceResult, Orchestrator, TaskEvent
from quantsmind_agent.tasks import Plan, StepStatus, TaskStatus

__all__ = ["TaskPanel", "TaskWorker", "TaskWorkerBridge"]

logger = logging.getLogger(__name__)


class TaskWorker(QObject):
    """Runs orchestrator steps on a worker thread."""

    progressed = Signal(object)  # AdvanceResult
    task_event = Signal(object)  # TaskEvent
    failed = Signal(str)
    finished = Signal()

    def __init__(self, orchestrator: Orchestrator) -> None:
        super().__init__()
        self._orchestrator = orchestrator
        # The observer is the worker's, so events reach the GUI through a signal
        # rather than by touching widgets from the wrong thread.
        self._orchestrator.set_observer(self._on_event)

    def run(self, limit: int = 25) -> None:
        """Advance until the task stops making progress."""

        try:
            for _ in range(limit):
                if self._orchestrator.cancelled or self._orchestrator.paused:
                    break
                result: AdvanceResult = self._orchestrator.advance()
                self.progressed.emit(result)
                if result.action in {"finished", "blocked", "cancelled", "paused", "waiting"}:
                    break
        except Exception as exc:
            logger.exception("task worker failed")
            self.failed.emit(str(exc))
        finally:
            self.finished.emit()

    def cancel(self) -> None:
        self._orchestrator.cancel("cancelled from the interface")

    def pause(self) -> None:
        self._orchestrator.pause()

    def _on_event(self, event: TaskEvent) -> None:
        self.task_event.emit(event)


class TaskWorkerBridge(QObject):
    """Owns the thread and re-exposes the worker's signals."""

    progressed = Signal(object)
    task_event = Signal(object)
    failed = Signal(str)
    finished = Signal()

    def __init__(self, orchestrator: Orchestrator) -> None:
        super().__init__()
        self._orchestrator = orchestrator
        self._thread = QThread()
        self._worker = TaskWorker(orchestrator)
        self._worker.moveToThread(self._thread)
        self._worker.progressed.connect(self.progressed.emit)
        self._worker.task_event.connect(self.task_event.emit)
        self._worker.failed.connect(self.failed.emit)
        self._worker.finished.connect(self.finished.emit)
        self._worker.finished.connect(self._thread.quit)

    @property
    def running(self) -> bool:
        return self._thread.isRunning()

    def start(self) -> None:
        if self._thread.isRunning():
            return
        self._thread.start()
        self._worker.run()

    def cancel(self) -> None:
        self._worker.cancel()

    def pause(self) -> None:
        self._worker.pause()

    def shutdown(self, wait_ms: int = 10000) -> None:
        """Stop the thread on window close: a running QThread outlives the app."""

        self._worker.cancel()
        if self._thread.isRunning():
            self._thread.quit()
            self._thread.wait(wait_ms)


# __PART2__


class TaskPanel(QWidget):
    """Objective, plan, approval, progress, changes and report."""

    plan_requested = Signal(str)  # objective
    approval_requested = Signal(bool)  # approved
    run_requested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._orchestrator: Orchestrator | None = None
        self._bridge: TaskWorkerBridge | None = None
        self._build()

    def _build(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 8, 10, 10)
        layout.setSpacing(8)

        title = QLabel("Task")
        title.setStyleSheet("font-weight:600;")
        layout.addWidget(title)

        self.objective = QLineEdit()
        self.objective.setPlaceholderText("One bounded change, e.g. fix the failing cart test")
        layout.addWidget(self.objective)

        row = QHBoxLayout()
        self.plan_button = QPushButton("Propose plan")
        self.plan_button.clicked.connect(self._on_propose)
        row.addWidget(self.plan_button)
        self.approve_button = QPushButton("Approve")
        self.approve_button.setEnabled(False)
        self.approve_button.clicked.connect(lambda: self._on_decide(True))
        row.addWidget(self.approve_button)
        self.reject_button = QPushButton("Reject")
        self.reject_button.setEnabled(False)
        self.reject_button.clicked.connect(lambda: self._on_decide(False))
        row.addWidget(self.reject_button)
        layout.addLayout(row)

        self.plan_view = QPlainTextEdit()
        self.plan_view.setReadOnly(True)
        self.plan_view.setPlaceholderText(
            "The plan appears here for review. Nothing runs before you approve it."
        )
        layout.addWidget(self.plan_view, 1)

        steps_box = QGroupBox("Steps")
        steps_layout = QVBoxLayout(steps_box)
        self.steps = QListWidget()
        steps_layout.addWidget(self.steps)
        layout.addWidget(steps_box)

        run_row = QHBoxLayout()
        self.run_button = QPushButton("Run")
        self.run_button.setEnabled(False)
        self.run_button.clicked.connect(self._on_run)
        run_row.addWidget(self.run_button)
        self.pause_button = QPushButton("Pause")
        self.pause_button.setEnabled(False)
        self.pause_button.clicked.connect(self._on_pause)
        run_row.addWidget(self.pause_button)
        self.cancel_button = QPushButton("Cancel")
        self.cancel_button.setEnabled(False)
        self.cancel_button.clicked.connect(self._on_cancel)
        run_row.addWidget(self.cancel_button)
        layout.addLayout(run_row)

        self.changes = QListWidget()
        self.changes.setMaximumHeight(80)
        layout.addWidget(self.changes)

        self.report = QPlainTextEdit()
        self.report.setReadOnly(True)
        self.report.setMaximumHeight(100)
        layout.addWidget(self.report)

        self.status = QLabel("No task.")
        self.status.setStyleSheet("color:#6a7280;font-size:11px;")
        layout.addWidget(self.status)

    # -- wiring -----------------------------------------------------------
    def attach(self, orchestrator: Orchestrator | None) -> None:
        """Point the panel at a task, releasing any previous worker."""

        self.shutdown()
        self._orchestrator = orchestrator
        for widget in (self.steps, self.changes):
            widget.clear()
        self.report.clear()
        for button in (
            self.approve_button,
            self.reject_button,
            self.run_button,
            self.pause_button,
            self.cancel_button,
        ):
            button.setEnabled(False)
        if orchestrator is not None:
            self.objective.setText(orchestrator.task.objective)
            self.refresh()

    def shutdown(self) -> None:
        """Stop and release the worker thread. Safe to call more than once."""

        if self._bridge is not None:
            self._bridge.shutdown()
            self._bridge = None

    # -- interaction ------------------------------------------------------
    def _on_propose(self) -> None:
        objective = self.objective.text().strip()
        if objective:
            self.plan_requested.emit(objective)

    def _on_decide(self, approved: bool) -> None:
        self.approval_requested.emit(approved)

    def _on_run(self) -> None:
        if self._orchestrator is None:
            return
        self._bridge = TaskWorkerBridge(self._orchestrator)
        self._bridge.progressed.connect(self._on_progress)
        self._bridge.task_event.connect(self._on_event)
        self._bridge.failed.connect(self._on_failed)
        self._bridge.finished.connect(self._on_finished)
        self.run_button.setEnabled(False)
        self.pause_button.setEnabled(True)
        self.cancel_button.setEnabled(True)
        self.status.setText("Running...")
        self._bridge.start()

    def _on_pause(self) -> None:
        if self._bridge is not None:
            self._bridge.pause()
        elif self._orchestrator is not None:
            self._orchestrator.pause()

    def _on_cancel(self) -> None:
        if self._bridge is not None:
            self._bridge.cancel()
        elif self._orchestrator is not None:
            self._orchestrator.cancel("cancelled from the interface")

    # __PART3__

    # -- rendering --------------------------------------------------------
    def show_plan(self, plan: Plan) -> None:
        """Render a plan for review: what it will do, and what it may touch."""

        lines = [f"Objective: {plan.objective}", ""]
        for step in plan.steps:
            depends = f" (after {', '.join(step.depends_on)})" if step.depends_on else ""
            lines.append(f"{step.id}: {step.description} [{step.tool}, {step.risk}]{depends}")
            for path in step.expected_paths:
                lines.append(f"    will touch: {path}")
        if plan.acceptance_criteria:
            lines.append("")
            lines.append("Done when:")
            for criterion in plan.acceptance_criteria:
                lines.append(f"  - {criterion.description}")
        self.plan_view.setPlainText("\n".join(lines))
        self.approve_button.setEnabled(True)
        self.reject_button.setEnabled(True)

    def refresh(self) -> None:
        """Re-read the task and update every control from its actual state."""

        orchestrator = self._orchestrator
        if orchestrator is None:
            return
        task = orchestrator.task
        self.steps.clear()
        plan = task.plan
        for step in plan.steps if plan else ():
            status = task.status_of(step.id).value
            self.steps.addItem(f"{step.id}: {status} - {step.description}")
        self.changes.clear()
        for change in task.file_changes:
            self.changes.addItem(f"{change.operation}: {change.path}")
        if task.report is not None:
            self.report.setPlainText(_render_report(task))
        approved = task.approved_plan is not None
        self.run_button.setEnabled(approved and not task.terminal)
        self.pause_button.setEnabled(not task.terminal and self._bridge is not None)
        self.cancel_button.setEnabled(not task.terminal)
        self.status.setText(f"Task {task.status.value}")

    def _on_progress(self, result: AdvanceResult) -> None:
        if result.reason:
            self.status.setText(f"{result.action}: {result.reason}")
        self.refresh()

    def _on_event(self, event: TaskEvent) -> None:
        if event.kind in {"blocked", "repair_refused", "budget_exhausted"}:
            self.status.setText(f"{event.kind}: {event.detail}")
        self.refresh()

    def _on_failed(self, message: str) -> None:
        self.status.setText(f"Task stopped: {message}")
        self.report.setPlainText(message)
        self.refresh()

    def _on_finished(self) -> None:
        self.pause_button.setEnabled(False)
        self.cancel_button.setEnabled(False)
        self.refresh()


def _render_report(task: object) -> str:
    """The completion report, as text, from the recorded evidence."""

    report = getattr(task, "report", None)
    if report is None:
        return ""
    lines = [
        f"Status: {report.status.value}",
        report.summary,
        f"Steps: {report.steps_completed} completed, {report.steps_failed} failed",
        f"Checks: {report.checks_passed} passed, {report.checks_failed} failed",
    ]
    if report.files_changed:
        lines.append("Changed files: " + ", ".join(report.files_changed))
    if report.unverified:
        lines.append("Unverified: " + "; ".join(report.unverified))
    if report.blockers:
        lines.append("Blockers: " + "; ".join(report.blockers))
    return "\n".join(lines)


def status_label(status: TaskStatus | StepStatus) -> str:
    """A short, stable label for a task or step status."""

    return status.value
