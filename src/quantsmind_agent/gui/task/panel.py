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
from typing import Any

from PySide6.QtCore import QObject, Qt, QThread, Signal
from PySide6.QtWidgets import (
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from quantsmind_agent.gui.shell.style import COLORS
from quantsmind_agent.services.tasks.models import Plan, StepStatus, TaskStatus
from quantsmind_agent.services.tasks.orchestrator import AdvanceResult, Orchestrator, TaskEvent

__all__ = [
    "TaskPanel",
    "TaskWorker",
    "TaskWorkerBridge",
    "WorkspaceScanBridge",
    "WorkspaceScanWorker",
]

logger = logging.getLogger(__name__)


class WorkspaceScanWorker(QObject):
    """Rebuilds a workspace snapshot away from the GUI thread.

    Discovery reads the filesystem. It is bounded, but a monorepo is still a
    monorepo: doing this in a click handler would freeze the window for as long
    as the walk takes.
    """

    finished = Signal(object)  # WorkspaceSnapshot
    failed = Signal(str)

    def __init__(self, intelligence: Any) -> None:
        super().__init__()
        self._intelligence = intelligence

    def run(self) -> None:
        try:
            snapshot = self._intelligence.refresh_snapshot()
        except Exception as exc:  # discovery failure is a result, not a crash
            logger.exception("workspace scan failed")
            self.failed.emit(str(exc))
            return
        self.finished.emit(snapshot)


class WorkspaceScanBridge(QObject):
    """Owns the scan thread and re-exposes its result to the window."""

    finished = Signal(object)
    failed = Signal(str)

    def __init__(self, intelligence: Any) -> None:
        super().__init__()
        self._thread = QThread()
        self._worker = WorkspaceScanWorker(intelligence)
        self._worker.moveToThread(self._thread)
        self._worker.finished.connect(self._on_finished)
        self._worker.failed.connect(self._on_failed)
        self._worker.finished.connect(self._thread.quit)
        self._worker.failed.connect(self._thread.quit)
        self._thread.started.connect(self._worker.run)

    @property
    def running(self) -> bool:
        return self._thread.isRunning()

    def start(self) -> None:
        if self._thread.isRunning():
            return
        self._thread.start()

    def shutdown(self, wait_ms: int = 10000) -> None:
        if self._thread.isRunning():
            self._thread.quit()
            self._thread.wait(wait_ms)

    def _on_finished(self, snapshot: Any) -> None:
        self.finished.emit(snapshot)

    def _on_failed(self, message: str) -> None:
        self.failed.emit(message)


class TaskWorker(QObject):
    """Runs orchestrator steps on a worker thread."""

    progressed = Signal(object)  # AdvanceResult
    task_event = Signal(object)  # TaskEvent
    verifying = Signal(object)  # VerificationOutcome
    failed = Signal(str)
    finished = Signal()

    def __init__(self, orchestrator: Orchestrator) -> None:
        super().__init__()
        self._orchestrator = orchestrator
        # The observer is the worker's, so events reach the GUI through a signal
        # rather than by touching widgets from the wrong thread.
        self._orchestrator.set_observer(self._on_event)

    def run(self, limit: int = 25) -> None:
        """Advance until the task stops making progress.

        Verification runs here, on the worker thread, because that is the thread
        that owns the task: running the project's checks on the GUI thread would
        freeze the window for as long as the suite takes, which is the exact
        reason execution left it in the first place.
        """

        try:
            for _ in range(limit):
                if self._orchestrator.cancelled or self._orchestrator.paused:
                    break
                result: AdvanceResult = self._orchestrator.advance()
                self.progressed.emit(result)
                if result.action == "verifying":
                    # The steps are done and the task promised evidence. Run it,
                    # then let the next advance settle on what was found.
                    self.verifying.emit(self._orchestrator.verify_current())
                    continue
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
    verifying = Signal(object)  # VerificationOutcome
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
        self._worker.verifying.connect(self.verifying.emit)
        self._worker.failed.connect(self.failed.emit)
        self._worker.finished.connect(self.finished.emit)
        self._worker.finished.connect(self._thread.quit)
        # Started from the thread, not from here. Calling worker.run() directly
        # would execute the whole task on the calling thread -- the UI thread --
        # which is precisely the freeze this class exists to avoid.
        self._thread.started.connect(self._worker.run)

    @property
    def running(self) -> bool:
        return self._thread.isRunning()

    def start(self) -> None:
        if self._thread.isRunning():
            return
        self._thread.start()

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
    pause_requested = Signal()
    cancel_requested = Signal()
    #: A task id picked from the history list.
    history_selected = Signal(str)
    #: The user asked to continue an inspected session.
    resume_requested = Signal()
    #: The user asked for the project summary to be rebuilt from disk.
    workspace_refresh_requested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._orchestrator: Orchestrator | None = None
        self._bridge: TaskWorkerBridge | None = None
        self._workspace_snapshot: Any = None
        self._build()

    def _build(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 8, 10, 10)
        layout.setSpacing(8)

        title = QLabel("Task")
        title.setStyleSheet("font-weight:600;")
        layout.addWidget(title)

        # The project summary belongs here rather than in a third top-level tab:
        # planning is project-aware, and the facts a plan is made against should
        # be visible beside the plan without becoming a separate application.
        project_box = QGroupBox("Project")
        project_layout = QVBoxLayout(project_box)
        project_layout.setContentsMargins(8, 6, 8, 6)
        self.project_summary = QLabel("No project open.")
        self.project_summary.setWordWrap(True)
        self.project_summary.setStyleSheet(f"color:{COLORS['muted']};font-size:11px;")
        project_layout.addWidget(self.project_summary)
        project_row = QHBoxLayout()
        self.project_refresh = QPushButton("Refresh project")
        self.project_refresh.clicked.connect(self.workspace_refresh_requested.emit)
        project_row.addWidget(self.project_refresh)
        project_row.addStretch(1)
        project_layout.addLayout(project_row)
        layout.addWidget(project_box)

        self.objective = QLineEdit()
        self.objective.setPlaceholderText("One bounded change, e.g. fix the failing cart test")
        layout.addWidget(self.objective)

        row = QHBoxLayout()
        self.plan_button = QPushButton("Propose plan")
        self.plan_button.clicked.connect(self._on_propose)
        row.addWidget(self.plan_button)
        self.preview_button = QPushButton("Preview (dry run)")
        self.preview_button.setToolTip("Validate and estimate cost without executing anything.")
        self.preview_button.clicked.connect(self._on_preview)
        row.addWidget(self.preview_button)
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

        self.cost_meter = QLabel("Cost: —")
        self.cost_meter.setObjectName("CostMeter")
        self.cost_meter.setStyleSheet(f"color:{COLORS['muted']};font-size:11px;")
        layout.addWidget(self.cost_meter)

        steps_box = QGroupBox("Steps")
        steps_layout = QVBoxLayout(steps_box)
        self.steps = QListWidget()
        steps_layout.addWidget(self.steps)
        layout.addWidget(steps_box)

        run_row = QHBoxLayout()
        self.run_button = QPushButton("Run")
        self.run_button.setEnabled(False)
        self.run_button.clicked.connect(self.run_requested.emit)
        run_row.addWidget(self.run_button)
        self.pause_button = QPushButton("Pause")
        self.pause_button.setEnabled(False)
        self.pause_button.clicked.connect(self.pause_requested.emit)
        run_row.addWidget(self.pause_button)
        self.cancel_button = QPushButton("Cancel")
        self.cancel_button.setEnabled(False)
        self.cancel_button.clicked.connect(self.cancel_requested.emit)
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
        self.status.setStyleSheet(f"color:{COLORS['muted']};font-size:11px;")
        layout.addWidget(self.status)

        # History is evidence, not a menu. A task that was interrupted shows as
        # such rather than as failed, because whether its last step took effect is
        # exactly what the user cannot tell from here.
        self.history = QListWidget()
        self.history.setMaximumHeight(90)
        self.history.setToolTip("Previous and interrupted tasks")
        self.history.itemSelectionChanged.connect(self._on_history_selected)
        layout.addWidget(self.history)
        #: The session being inspected, and what may be done with it. Read-only
        #: until the user asks otherwise.
        self.timeline = QPlainTextEdit()
        self.timeline.setReadOnly(True)
        self.timeline.setMaximumHeight(120)
        self.timeline.setPlaceholderText("Select a previous task to see what happened to it.")
        layout.addWidget(self.timeline)
        self.resume_button = QPushButton("Continue this task")
        self.resume_button.setEnabled(False)
        self.resume_button.clicked.connect(self.resume_requested.emit)
        layout.addWidget(self.resume_button)
        #: The summary behind each row, so selection can be acted upon.
        self._history_summaries: dict[str, Any] = {}

    # -- inspection --------------------------------------------------------
    @property
    def worker_running(self) -> bool:
        """Whether a worker thread is currently executing this task."""

        return bool(self._bridge is not None and self._bridge.running)

    def show_workspace(self, snapshot: Any) -> None:
        """Show the bounded project facts the next plan will be made against."""

        self._workspace_snapshot = snapshot
        if snapshot is None:
            self.project_summary.setText("No project open.")
            return
        lines = [snapshot.summary()]
        if getattr(snapshot, "ecosystems", ()):
            lines.append("Ecosystems: " + ", ".join(snapshot.ecosystems))
        if getattr(snapshot, "manifests", ()):
            lines.append("Manifests: " + ", ".join(snapshot.manifests[:4]))
        if getattr(snapshot, "checks", ()):
            lines.append("Checks: " + ", ".join(snapshot.check_ids[:6]))
        if not getattr(snapshot, "complete", True):
            lines.append("Summary is partial; Refresh to rebuild it.")
        self.project_summary.setText("\n".join(lines))

    @property
    def workspace_snapshot(self) -> Any:
        """The snapshot currently displayed, or ``None``."""

        return self._workspace_snapshot

    def show_timeline(self, task: Any) -> None:
        """Render a stored task's plan, checkpoints and trace as one timeline.

        Bounded by construction: the trace is already capped on the record, and
        only the most recent entries are rendered, so a long-running session does
        not become a wall of text the window has to lay out.
        """

        lines: list[str] = [f"{task.objective}  [{task.status.value}]", ""]
        plan = task.plan
        if plan is not None:
            lines.append("Plan:")
            for step in plan.steps:
                state = task.status_of(step.id).value
                lines.append(f"  {step.id}: {step.tool} -- {state}")
            lines.append("")
        checkpoints = list(getattr(task, "checkpoints", []) or [])
        if checkpoints:
            last = checkpoints[-1]
            lines.append(
                f"Checkpoint: {len(last.get('completed_steps', []))} step(s) done"
                + (
                    f", interrupted during {last.get('in_flight_step')}"
                    if last.get("in_flight_step")
                    else ""
                )
            )
            lines.append("")
        trace = list(getattr(task, "trace", []) or [])[-40:]
        if trace:
            lines.append("Timeline:")
            lines.extend(f"  {event.describe()}" for event in trace)
        verification = getattr(task, "verification", None)
        if verification is not None:
            lines.append("")
            lines.append(f"Verification: {verification.describe()}")
        self.timeline.setPlainText("\n".join(lines))

    def set_cost_meter(self, tokens: int = 0, tier: str = "") -> None:
        """Show the v1.2 cost/latency router meter (tokens + tier + est. cost)."""
        if not tokens:
            self.cost_meter.setText("Cost: —")
            return
        try:
            from quantsmind_agent.providers.cost_router import CostRouter

            router = CostRouter()
            chosen = router.choose(tokens, budget_usd=1.0)
            meter = router.meter(tokens, chosen)
            self.cost_meter.setText(
                f"Cost: ~{meter['cost_usd']} USD ({tokens} tokens, tier {chosen.name}, ~{meter['latency_ms']} ms)"
            )
        except Exception:
            self.cost_meter.setText(f"Cost: {tokens} tokens")

    def set_history_actions(
        self, *, verdict: Any = None, inspecting_id: str = "", running: bool = False
    ) -> None:
        """Enable continuing only when the verdict says it is safe, and idle.

        A refusal is shown as a disabled button with the reason, rather than a
        button that fails when pressed: the user asked "can this continue", and
        the answer should be visible before they try.
        """

        allowed = bool(verdict is not None and getattr(verdict, "resumable", False))
        self.resume_button.setEnabled(allowed and not running)
        if verdict is None:
            self.resume_button.setToolTip("Select a previous task first.")
            return
        self.resume_button.setToolTip(verdict.describe())
        self.resume_button.setText("Continue this task" if allowed else "Cannot continue")

    # -- history -----------------------------------------------------------
    def set_history(self, summaries: Any, *, interrupted_ids: frozenset[str] = frozenset()) -> None:
        """Show stored tasks, newest first, marking the interrupted ones."""

        self._history_summaries = {str(item.id): item for item in summaries}
        self.history.clear()
        for item in summaries:
            mark = "! " if str(item.id) in interrupted_ids else ""
            label = f"{mark}{item.objective} [{item.status}]"
            entry = QListWidgetItem(label)
            entry.setData(Qt.ItemDataRole.UserRole, str(item.id))
            entry.setToolTip(str(item.workspace))
            self.history.addItem(entry)

    def selected_task_id(self) -> str | None:
        """The task the user picked from the history, if any."""

        rows = self.history.selectedItems()
        if not rows:
            return None
        return str(rows[0].data(Qt.ItemDataRole.UserRole))

    def summary_for(self, task_id: str) -> Any:
        """The stored summary behind a history row."""

        return self._history_summaries.get(task_id)

    def _on_history_selected(self) -> None:
        task_id = self.selected_task_id()
        if task_id is not None:
            self.history_selected.emit(task_id)

    # -- wiring -----------------------------------------------------------
    @property
    def orchestrator(self) -> Orchestrator | None:
        """The orchestrator this panel drives, if one is attached.

        Exposed so the window can ask whether a task exists without reaching for
        the panel's private state.
        """

        return self._orchestrator

    def describe_controls(self) -> dict[str, Any]:
        """The panel's controls and whether they are usable right now.

        Reported as data so the *packaged* application can be inspected from the
        inside: an outside harness can see that a window appeared, but not that
        it carries the task workflow.
        """

        return {
            "controls": {
                button.text(): {"enabled": button.isEnabled(), "visible": button.isVisible()}
                for button in (
                    self.plan_button,
                    self.approve_button,
                    self.reject_button,
                    self.run_button,
                    self.pause_button,
                    self.cancel_button,
                )
            },
            "status": self.status.text(),
            "task_attached": self._orchestrator is not None,
            "worker_running": self._bridge is not None and self._bridge.running,
            "project": {
                "summary": self.project_summary.text(),
                "has_snapshot": self._workspace_snapshot is not None,
                "refresh_enabled": self.project_refresh.isEnabled(),
            },
        }

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
        """Stop the worker and cancel the task. Safe to call more than once.

        Closing the window has to *end* the task, not merely stop rendering it:
        a task left approved and running would claim to be in progress after the
        application that owned it had gone.
        """

        if self._bridge is not None:
            self._bridge.shutdown()
            self._bridge = None
        if self._orchestrator is not None and not self._orchestrator.task.terminal:
            self._orchestrator.cancel("the application closed")

    # -- interaction ------------------------------------------------------
    def _on_propose(self) -> None:
        objective = self.objective.text().strip()
        if objective:
            self.plan_requested.emit(objective)

    def _on_decide(self, approved: bool) -> None:
        self.approval_requested.emit(approved)

    def _on_preview(self) -> None:
        """Dry-run preview: validate + estimate, never execute (v1.6)."""
        objective = self.objective.text().strip()
        if not objective or self._orchestrator is None:
            self.cost_meter.setText("Preview: propose a plan first.")
            return
        plan = getattr(self._orchestrator.task, "plan", None)
        if plan is None:
            self.cost_meter.setText("Preview: no plan proposed yet.")
            return
        from quantsmind_agent.services.plan_preview import preview_plan

        preview = preview_plan(plan)
        if not preview.valid:
            self.cost_meter.setText(
                f"Preview invalid: {preview.errors[0] if preview.errors else 'unknown'}"
            )
            return
        self.set_cost_meter(preview.estimated_tokens)
        self.cost_meter.setText(
            self.cost_meter.text()
            + f" · {len(preview.step_ids)} steps, {len(preview.files)} files (dry run, nothing executed)"
        )

    def start_worker(self) -> None:
        if self._orchestrator is None:
            return
        if self._bridge is not None and self._bridge.running:
            # A second press (a double click, a queued signal) must not replace a
            # live bridge: the old thread would be orphaned, nothing would join
            # it, and it would keep the process alive after the window closed.
            return
        self._bridge = TaskWorkerBridge(self._orchestrator)
        self._bridge.progressed.connect(self._on_progress)
        self._bridge.task_event.connect(self._on_event)
        self._bridge.verifying.connect(self._on_verified)
        self._bridge.failed.connect(self._on_failed)
        self._bridge.finished.connect(self._on_finished)
        self.run_button.setEnabled(False)
        self.pause_button.setEnabled(True)
        self.cancel_button.setEnabled(True)
        self.status.setText("Running...")
        self._bridge.start()

    def request_pause(self) -> None:
        if self._bridge is not None:
            self._bridge.pause()
        elif self._orchestrator is not None:
            self._orchestrator.pause()

    def request_cancel(self) -> None:
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

    def _on_verified(self, outcome: Any) -> None:
        """Say what verification found, in the same words the record uses."""

        orchestrator = self._orchestrator
        record = getattr(orchestrator.task, "verification", None) if orchestrator else None
        self.status.setText(record.describe() if record is not None else f"verified: {outcome}")
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
