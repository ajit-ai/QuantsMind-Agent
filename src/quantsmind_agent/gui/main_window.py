"""Main window: sidebar + conversation + activity + composer + status bar.

The window wires widgets to :class:`~quantsmind_agent.app.ApplicationContext`
services. It contains no provider or agent logic of its own.
"""

from __future__ import annotations

import queue
import re
import threading
import time
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from PySide6.QtCore import Qt, QThread, QTimer, Slot
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QButtonGroup,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QSplitter,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from quantsmind_agent.agent import AgentOutcome
from quantsmind_agent.errors import QuantsMindError
from quantsmind_agent.gui.activity import ActivityView
from quantsmind_agent.gui.approval import ApprovalBridge, ApprovalDialog
from quantsmind_agent.gui.conversation import ConversationView
from quantsmind_agent.gui.settings_dialog import SettingsDialog
from quantsmind_agent.gui.sidebar import Sidebar
from quantsmind_agent.gui.task_panel import TaskPanel, WorkspaceScanBridge
from quantsmind_agent.gui.worker import AgentWorkerBridge
from quantsmind_agent.logging_setup import get_logger
from quantsmind_agent.permissions import ApprovalOutcome
from quantsmind_agent.services import TaskService
from quantsmind_agent.sessions import RecoveryVerdict
from quantsmind_agent.tasks import PlanRejected, TaskStatus
from quantsmind_agent.version import APP_NAME, __version__

if TYPE_CHECKING:  # pragma: no cover - typing only
    from quantsmind_agent.app import ApplicationContext

__all__ = ["MainWindow"]

logger = get_logger(__name__)


def _safe_filename(title: str) -> str:
    """A title usable as a filename, for a suggested export name."""

    cleaned = re.sub(r"[^\w \-]+", "", title, flags=re.UNICODE).strip()
    return re.sub(r"\s+", "-", cleaned)[:60] or "conversation"


def _clock(epoch: float) -> str:
    """The time something happened, for a replayed transcript line."""

    if not epoch or epoch <= 0:
        return ""
    return time.strftime("%H:%M:%S", time.localtime(epoch))


class MainWindow(QMainWindow):
    """The single window of QuantsMind-Agent."""

    def __init__(self, context: ApplicationContext, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._context = context
        self.setWindowTitle(f"{APP_NAME} {__version__}")
        self.resize(1180, 800)
        self.setMinimumSize(940, 620)
        # The window owns the worker bridge: threading is a GUI concern, and
        # resolving the agent per turn keeps it correct across rebuilds.
        self.bridge = AgentWorkerBridge(context.current_agent)
        context.attach_bridge(self.bridge)
        # The approval bridge is what turns an ASK permission into a dialog.
        # It is attached before the UI is built so a turn started immediately
        # after construction still reaches a human.
        self.approval = ApprovalBridge(self)
        context.attach_approval(self.approval)
        #: The application service that owns the task lifecycle. The window asks
        #: it what is true and renders the answer; it does not build tasks, store
        #: orchestrators or decide what is authorised. That used to happen here,
        #: which made every security decision in the product a line of widget code.
        #: Holds the current plan-proposal thread so it can be joined on close.
        self._plan_thread: threading.Thread | None = None
        #: The bounded snapshot the pending plan was proposed against. Carried
        #: to the task record so the evidence and the plan cannot disagree.
        self._pending_snapshot: Any = None
        #: The one background workspace scan, when one is running.
        self._workspace_bridge: WorkspaceScanBridge | None = None
        self._task_service = TaskService(
            data_dir=context.paths.data_dir,
            # Resolved per call rather than captured: the application rebuilds its
            # tools when the workspace or configuration changes, and a captured
            # registry would run a task against a stale agent with no tools.
            registry_factory=lambda _workspace: context.tools,
        )
        #: The approval dialog currently on screen, if any.
        self._active_dialog: ApprovalDialog | None = None
        #: The plan proposed and awaiting a human decision, or ``None``. A
        #: proposal is never an approval: it is shown, and nothing runs until the
        #: user says so.
        self._pending_plan: Any = None
        #: The stored task being inspected from history, if any. Read-only.
        self._inspecting: Any = None
        #: Releases the agent-event subscription. ``None`` once released.
        self._unsubscribe_events: Callable[[], None] | None = None
        self._build()
        self._connect()
        self._resume_previous_conversation()
        self._refresh_from_context()
        self._show_readiness_hint()
        self._show_interrupted_if_any()

    def _resume_previous_conversation(self) -> None:
        """Reopen the last conversation when the user asked for that.

        Only the transcript comes back: no approval grants, no undo history. If the
        project it was working in has moved, that is said plainly rather than
        quietly opening a different folder.
        """

        self._refresh_conversations()
        already = self._context.resumed_conversation_id
        resumed = True if already else self._context.resume_on_startup(honour_setting=True)
        if not resumed:
            return
        self._restore_transcript()
        unavailable = self._context.unavailable_workspace
        if unavailable:
            self.conversation.append_notice(
                f"The project this conversation used is not available: {unavailable}. "
                "Open it again to continue working.",
                kind="error",
            )
        self._refresh_conversations()
        self.conversation.append_notice(
            "Reopened your last conversation."
            if not already
            else "Continuing the conversation you were last working in."
        )

    @property
    def is_busy(self) -> bool:
        """Whether a turn is currently running."""

        return self.bridge.is_busy

    # -- startup hint ------------------------------------------------------
    def _show_readiness_hint(self) -> None:
        """Tell the user what is missing before they type anything."""

        ready, reason = self._context.readiness()
        if not ready:
            self.conversation.append_notice(reason)

    # -- construction ------------------------------------------------------
    def _build(self) -> None:
        central = QWidget()
        outer = QVBoxLayout(central)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.setChildrenCollapsible(False)
        splitter.setHandleWidth(8)

        self.sidebar = Sidebar()
        splitter.addWidget(self.sidebar)

        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(10, 12, 10, 10)
        right_layout.setSpacing(8)

        # Chat and Task are two views of the same project, not two applications:
        # the switcher replaces the pane rather than adding a window, so the
        # conversation, the tool activity and the task share one status bar and
        # one workspace.
        self._view_switch = QButtonGroup(self)
        self._view_switch.setExclusive(True)
        switcher = QHBoxLayout()
        switcher.setSpacing(6)
        for index, caption in enumerate(("Chat", "Task")):
            button = QPushButton(caption)
            button.setCheckable(True)
            button.setObjectName("ViewTab")
            button.setChecked(index == 0)
            self._view_switch.addButton(button, index)
            self._view_switch.idClicked.connect(self._on_view_changed)
            switcher.addWidget(button)
        switcher.addStretch(1)
        right_layout.addLayout(switcher)

        self._views = QStackedWidget()
        right_layout.addWidget(self._views, stretch=1)

        chat_page = QWidget()
        chat_layout = QVBoxLayout(chat_page)
        chat_layout.setContentsMargins(0, 0, 0, 0)
        chat_layout.setSpacing(8)

        self.conversation = ConversationView()
        chat_layout.addWidget(self.conversation, stretch=1)

        self.activity_heading = QLabel("Tool activity")
        self.activity_heading.setObjectName("SectionLabel")
        chat_layout.addWidget(self.activity_heading)

        self.activity = ActivityView()
        self.activity.setMaximumHeight(190)
        chat_layout.addWidget(self.activity)

        chat_layout.addWidget(self._composer())
        self._views.addWidget(chat_page)

        self.task_panel = TaskPanel()
        self._views.addWidget(self.task_panel)
        self._connect_task_panel()

        splitter.addWidget(right)
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([300, 880])

        outer.addWidget(splitter)
        self.setCentralWidget(central)

        self._status_model = QLabel("")
        self._status_workspace = QLabel("")
        self._status_activity = QLabel("")
        #: Text streamed so far in the current turn.
        self._streamed: list[str] = []
        #: Agent events waiting to be rendered on the GUI thread.
        self._event_queue: queue.Queue[Any] = queue.Queue()
        for label in (self._status_model, self._status_workspace, self._status_activity):
            label.setObjectName("StatusText")
        self.statusBar().addWidget(self._status_model, 2)
        self.statusBar().addWidget(self._status_workspace, 2)
        self.statusBar().addPermanentWidget(self._status_activity)

        QShortcut(QKeySequence("Ctrl+Return"), self, self._on_send_clicked)
        QShortcut(QKeySequence("Ctrl+O"), self, self._on_open_workspace)
        QShortcut(QKeySequence("Ctrl+K"), self, self.sidebar.settings_requested.emit)
        QShortcut(QKeySequence("Ctrl+N"), self, self._on_new_conversation)

    # -- v0.4 tasks ----------------------------------------------------------
    def _connect_task_panel(self) -> None:
        """Wire the task panel to the orchestrator.

        Approval stays a hard gate: the panel can only *ask*, and the decision
        is recorded as an :class:`ApprovalRecord` bound to this plan's digest and
        this workspace. Nothing here executes a tool, and nothing here can widen
        a scope the user did not grant.
        """

        panel = self.task_panel
        panel.plan_requested.connect(self._on_task_plan_requested)
        panel.approval_requested.connect(self._on_task_approval)
        panel.run_requested.connect(self._on_task_run)
        panel.cancel_requested.connect(self._on_task_cancel)
        panel.pause_requested.connect(self._on_task_pause)
        panel.history_selected.connect(self._on_task_history_selected)
        panel.resume_requested.connect(self._on_task_resume)
        panel.workspace_refresh_requested.connect(self._on_workspace_refresh_requested)

    def _on_view_changed(self, index: int) -> None:
        self._views.setCurrentIndex(index)
        if index == 1:
            self.task_panel.refresh()

    def show_task_view(self) -> None:
        """Bring the task workflow into view, from anywhere in the window."""

        self._views.setCurrentIndex(1)
        checked = self._view_switch.checkedButton()
        if checked is not None:
            checked.setChecked(True)

    @property
    def task_orchestrator(self) -> Any:
        """The orchestrator the panel is driving, if any."""

        return self.task_panel.orchestrator

    def describe_structure(self) -> dict[str, Any]:
        """What this window is made of, as data rather than pixels.

        The acceptance harness watches the process from the outside, so it can
        prove a window appeared but not what is inside it. This is how the
        *shipped binary* is shown to carry the task workflow, rather than only
        the source tree: the same code path that draws the window reports its
        own composition.
        """

        return {
            "title": self.windowTitle(),
            "views": [
                str(button.text()) for button in self._view_switch.buttons() if button is not None
            ],
            "current_view": ("Chat", "Task")[self._views.currentIndex()]
            if 0 <= self._views.currentIndex() <= 1
            else f"index {self._views.currentIndex()}",
            "task_panel": self.task_panel.describe_controls(),
        }

    def _on_task_plan_requested(self, objective: str) -> None:
        """Ask the model for a plan through the structured-output path.

        The panel cannot invent a plan, so "Propose plan" asks the configured
        model for one, and the answer is validated here before anyone sees it.
        What used to happen was a request for a fenced JSON block in prose and a
        hope; the difference is that a malformed answer is now a stated failure
        with reasons, not something that has to be spotted in a paragraph.

        The call runs on a worker thread: it is a model call, and the GUI thread
        is for drawing.
        """

        self.show_chat_view()
        text = objective.strip()
        if len(text) < 4:
            self.task_panel.status.setText("Describe the change a little more fully first.")
            return
        if not self._context.workspace:
            self.task_panel.status.setText("Open a project before planning a task.")
            return
        self.task_panel.status.setText("Asking the model for a plan...")

        def ask() -> None:
            try:
                # Discovery runs here, on the planning thread, and its bounded
                # rendering travels with the request. The GUI never walks the
                # project to draw a plan.
                snapshot = self._context.workspace_snapshot()
                project_context = snapshot.format_for_prompt() if snapshot is not None else ""
                proposal = self._planner(project_context=project_context).propose(text)
            except Exception as exc:  # a planner that raises is still just a failure
                self._proposal_failed(str(exc))
                return
            self._proposal_ready(proposal, snapshot)

        self._plan_thread = threading.Thread(target=ask, name="plan-proposal", daemon=True)
        self._plan_thread.start()

    def _planner(self, *, project_context: str = "") -> Any:
        """A planner bound to the live gateway, workspace and tool set."""

        from quantsmind_agent.planner import TaskPlanner

        workspace = self._context.workspace
        return TaskPlanner(
            gateway=self._context.gateway,
            known_tools=frozenset(self._context.tools.names),
            workspace=str(workspace.root) if workspace is not None else "",
            project_context=project_context,
            model=self._context.config_manager.config.provider.model,
        )

    def _proposal_ready(self, proposal: Any, snapshot: Any = None) -> None:
        """Called with whatever the model produced."""

        if not proposal.ok:
            self._proposal_failed("; ".join(proposal.problems[:3]))
            return
        self.submit_task_plan(proposal.plan, snapshot=snapshot)

    def _proposal_failed(self, reason: str) -> None:
        self.task_panel.status.setText(f"No usable plan: {reason}")
        self.show_task_view()
        logger.info("plan proposal refused: %s", reason[:200])

    def show_chat_view(self) -> None:
        self._views.setCurrentIndex(0)
        checked = self._view_switch.checkedButton()
        if checked is not None:
            checked.setChecked(True)

    def submit_task_plan(self, plan: Any, *, snapshot: Any = None) -> None:
        """Offer a proposed plan for review. Never approves it."""

        self._pending_plan = plan
        self._pending_snapshot = snapshot
        self.show_task_view()
        self.task_panel.show_plan(plan)
        self.task_panel.status.setText("Plan ready for review. Nothing runs until you approve it.")

    def _on_task_approval(self, approved: bool) -> None:
        plan = self._pending_plan
        if plan is None:
            self.task_panel.status.setText("There is no plan to approve.")
            return
        workspace = self._context.workspace
        if workspace is None:
            self.task_panel.status.setText("Open a project before running a task.")
            return
        snapshot = self._pending_snapshot
        if snapshot is None:
            # An externally submitted plan has no snapshot of its own. Reuse
            # whatever the background scan already built; never walk the
            # filesystem here, on the GUI thread.
            intelligence = self._context.workspace_intel
            snapshot = intelligence.cached_snapshot if intelligence is not None else None
        try:
            orchestrator = self._task_service.propose(plan, workspace, snapshot=snapshot)
            orchestrator = self._task_service.decide(approved)
        except PlanRejected as exc:
            self.task_panel.status.setText(f"That decision was refused: {exc}")
            return
        self.task_panel.attach(orchestrator)
        self._pending_plan = None
        self._pending_snapshot = None
        self._show_interrupted_if_any()

    def _on_task_run(self) -> None:
        orchestrator = self.task_orchestrator
        if orchestrator is None:
            self.task_panel.status.setText("There is no task to run.")
            return
        task = orchestrator.task
        if task.terminal or orchestrator.cancelled:
            # A cancelled or finished task is not resumed by pressing Run. The
            # policy would refuse the step anyway, but flipping the status back
            # to approved would report a cancelled task as live.
            self.task_panel.status.setText(f"This task is {task.status.value}.")
            return
        if task.approved_plan is None:
            self.task_panel.status.setText("Approve the plan before running it.")
            return
        self.task_panel.start_worker()

    def _on_task_cancel(self) -> None:
        orchestrator = self.task_orchestrator
        if orchestrator is not None:
            orchestrator.cancel("cancelled from the task panel")
            self.task_panel.refresh()

    def _on_task_pause(self) -> None:
        orchestrator = self.task_orchestrator
        if orchestrator is not None:
            orchestrator.pause()
            self.task_panel.refresh()

    def _release_task(self) -> None:
        """Stop any running task worker and detach the panel."""

        self._pending_plan = None
        self._pending_snapshot = None
        self.task_panel.shutdown()
        self.task_panel.attach(None)
        self._task_service.release()

    # -- interrupted work --------------------------------------------------
    def _show_interrupted_if_any(self) -> None:
        """Tell the user about work that was in flight when the app last closed.

        Reported, never resumed: the outcome of an interrupted task is unknown,
        and the only thing the application may do about it automatically is say so.
        """

        self._refresh_task_history()
        interrupted = self._interrupted()
        if not interrupted:
            return
        newest = interrupted[0]
        self.task_panel.status.setText(
            f"{len(interrupted)} task(s) were interrupted when the application closed. "
            f"{newest.describe()} Select it in the list to review."
        )
        for item in interrupted:
            logger.warning("interrupted task %s: %s", item.task.id, item.describe())

    def _interrupted(self) -> list[Any]:
        """Tasks that were mid-flight when the application last closed."""

        return self._task_service.interrupted_tasks(self._context.workspace)

    def _refresh_task_history(self) -> None:
        """Fill the panel's history list from the store."""

        interrupted = frozenset(str(item.task.id) for item in self._interrupted())
        self.task_panel.set_history(self._task_service.history(), interrupted_ids=interrupted)

    def _on_task_history_selected(self, task_id: str) -> None:
        """Open a stored task for inspection. Never starts execution.

        Opening history is a read: it shows what happened, and the buttons that
        could run something are disabled until the user asks for a resume and is
        told what resuming would do.
        """

        task = self._task_service.store.load(task_id)
        if task is None:
            self._inspecting = None
            self.task_panel.status.setText("That task's record could not be read.")
            self.task_panel.set_history_actions()
            self._refresh_task_history()
            return
        self._inspecting = task
        self.task_panel.show_timeline(task)
        verdict = self._verdict_for(task)
        self.task_panel.set_history_actions(
            verdict=verdict,
            inspecting_id=task.id,
            running=bool(self.task_panel.worker_running),
        )
        self.show_task_view()

    def _verdict_for(self, task: Any) -> Any:
        """Whether the selected task may continue, computed against the world now."""

        from quantsmind_agent.sessions import RecoveryDecision

        if task.status is TaskStatus.COMPLETED:
            return RecoveryVerdict(
                RecoveryDecision.NOT_RESUMABLE, ("this task is already complete",)
            )
        return self._task_service.recovery_verdict(task, self._context.workspace)

    def _on_task_resume(self) -> None:
        """Resume the inspected task, after an explicit confirmation.

        The confirmation is not ceremony: continuing may re-run an operation whose
        outcome is unknown, and that is the user's call, not the application's.
        """

        task = self._inspecting
        if task is None:
            return
        workspace = self._context.workspace
        if workspace is None:
            self.task_panel.status.setText("Open the task's project before continuing it.")
            return
        verdict = self._verdict_for(task)
        if not verdict.resumable:
            self.task_panel.status.setText(f"This task cannot continue. {verdict.describe()}.")
            self.task_panel.set_history_actions(verdict=verdict, inspecting_id=task.id)
            return

        box = QMessageBox(self)
        box.setWindowTitle("Continue this task?")
        box.setText(
            f"{task.objective}\n\n{verdict.describe()}\n\n"
            "Continuing may run tools again. Nothing runs without your approval."
        )
        box.setStandardButtons(QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        box.setDefaultButton(QMessageBox.StandardButton.No)
        if box.exec() != QMessageBox.StandardButton.Yes:
            self.task_panel.status.setText("Not continued.")
            return

        from quantsmind_agent.services import RecoveryRefused

        try:
            orchestrator = self._task_service.resume(task, workspace)
        except RecoveryRefused as exc:
            self.task_panel.status.setText(f"Cannot continue. {exc.verdict.describe()}.")
            return
        self._inspecting = None
        self.task_panel.attach(orchestrator)
        self.task_panel.status.setText(verdict.describe())

    def _composer(self) -> QWidget:
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)

        from PySide6.QtWidgets import QTextEdit

        self.input = QTextEdit()
        self.input.setPlaceholderText(
            "Ask about this project, e.g. \u201cExplain how the application starts.\u201d "
            "(Ctrl+Enter to send)"
        )
        self.input.setMinimumHeight(66)
        self.input.setMaximumHeight(150)
        self.input.setAcceptRichText(False)
        layout.addWidget(self.input)

        row = QHBoxLayout()
        row.setSpacing(8)
        self._hint_label = QLabel(
            "The agent can read and change files in this project; "
            "it asks before writing, deleting or running a command."
        )
        self._hint_label.setObjectName("StatusText")
        row.addWidget(self._hint_label)
        row.addStretch(1)

        self.cancel_button = QPushButton("Stop")
        self.cancel_button.setObjectName("DangerButton")
        self.cancel_button.setVisible(False)
        self.cancel_button.clicked.connect(self._on_cancel_clicked)
        row.addWidget(self.cancel_button)

        self.send_button = QPushButton("Send")
        self.send_button.setObjectName("PrimaryButton")
        self.send_button.setToolTip("Send the request to the model (Ctrl+Enter)")
        self.send_button.clicked.connect(self._on_send_clicked)
        row.addWidget(self.send_button)

        layout.addLayout(row)
        return panel

    # -- wiring ------------------------------------------------------------
    def _connect(self) -> None:
        bridge = self.bridge
        bridge.text_delta.connect(self._on_text_delta)
        bridge.tool_activity.connect(self._on_tool_activity)
        bridge.turn_started.connect(self._on_turn_started)
        bridge.turn_finished.connect(self._on_turn_finished)
        bridge.turn_failed.connect(self._on_turn_failed)
        bridge.busy_changed.connect(self._on_busy_changed)

        # An explicit QueuedConnection guarantees the slot runs on the GUI thread
        # even if the decorator is ever lost; either alone is insufficient,
        # which is exactly the blank-dialog failure this replaces.
        self.approval.prompt_ready.connect(
            self._on_prompt_ready, Qt.ConnectionType.QueuedConnection
        )

        self.sidebar.open_workspace_requested.connect(self._on_open_workspace)
        self.sidebar.close_workspace_requested.connect(self._on_close_workspace)
        self.sidebar.settings_requested.connect(self._on_settings)
        self.sidebar.new_conversation_requested.connect(self._on_new_conversation)
        self.sidebar.conversation_selected.connect(self._on_conversation_selected)
        self.sidebar.conversation_delete_requested.connect(self._on_conversation_delete_requested)
        self.sidebar.conversation_rename_requested.connect(self._on_conversation_rename_requested)
        self.sidebar.conversation_export_requested.connect(self._on_conversation_export_requested)

        events = self._context.events
        # Keep the unsubscribe callable. ``subscribe_all`` stores a strong
        # reference to the bound method, which forms a cycle
        # context -> bus -> bound method -> window -> context. Nothing can then
        # reclaim the window by reference counting, so closing it would leave a
        # live QMainWindow, a running 50ms timer and a subscription that keeps
        # delivering events into a closed window's widgets. Releasing it on
        # close breaks the cycle and hands the window back to normal teardown.
        self._unsubscribe_events = events.subscribe_all(self._queue_agent_event)
        # Agent events are published on whichever thread raised them -- usually
        # the agent's worker thread -- and this handler mutates QWidgets. The
        # subscription above therefore only enqueues; this timer is what makes
        # the actual work happen on the GUI thread.
        self._event_timer = QTimer(self)
        self._event_timer.setInterval(50)
        self._event_timer.timeout.connect(self._drain_agent_events)
        self._event_timer.start()

    def _refresh_from_context(self) -> None:
        config = self._context.config_manager.config
        provider = config.provider
        workspace = self._context.workspace
        self.sidebar.set_model(
            provider.name,
            provider.model,
            provider.base_url,
            self._context.config_manager.masked_api_key(),
        )
        self.sidebar.set_workspace(str(workspace.root) if workspace else None)
        self.sidebar.set_status(self._status_text())
        self._status_model.setText(f"{provider.model} @ {provider.base_url}")
        self._status_workspace.setText(str(workspace.root) if workspace else "No project open")
        self._refresh_workspace_summary()
        self._update_session_counts()

    def _refresh_workspace_summary(self) -> None:
        """Paint whatever project facts are already known; never scan here."""

        workspace = self._context.workspace
        if workspace is None:
            self.task_panel.show_workspace(None)
            return
        intelligence = self._context.workspace_intel
        snapshot = intelligence.cached_snapshot if intelligence is not None else None
        self.task_panel.show_workspace(snapshot)
        if snapshot is None:
            self.task_panel.project_summary.setText(
                f"{workspace.root.name or str(workspace.root)} is open. "
                "Refresh project to summarise it."
            )

    def _on_workspace_refresh_requested(self) -> None:
        self._start_workspace_scan()

    def _stop_workspace_scan(self) -> None:
        """Stop the background scan, if any, and detach its result."""

        bridge = self._workspace_bridge
        self._workspace_bridge = None
        if bridge is not None:
            bridge.shutdown()

    def _start_workspace_scan(self) -> None:
        """Rebuild the snapshot on a worker thread, never in a click handler."""

        intelligence = self._context.workspace_intel
        if intelligence is None:
            self.task_panel.project_summary.setText("Open a project first.")
            return
        if self._workspace_bridge is not None and self._workspace_bridge.running:
            return
        self._stop_workspace_scan()
        self.task_panel.project_summary.setText("Scanning project…")
        bridge = WorkspaceScanBridge(intelligence)
        bridge.finished.connect(self._on_workspace_scan_finished)
        bridge.failed.connect(self._on_workspace_scan_failed)
        self._workspace_bridge = bridge
        bridge.start()

    def _on_workspace_scan_finished(self, snapshot: Any) -> None:
        workspace = self._context.workspace
        if workspace is None or str(snapshot.workspace) != str(workspace.root):
            # The user changed projects while the scan was running. The facts
            # belong to the old one, so they must not be shown or carried into
            # the next plan.
            return
        self.task_panel.show_workspace(snapshot)
        self._pending_snapshot = snapshot

    def _on_workspace_scan_failed(self, message: str) -> None:
        if self._context.workspace is None:
            return
        self.task_panel.project_summary.setText(f"Project scan failed: {message}")

    # -- workspace ---------------------------------------------------------
    def _on_open_workspace(self, directory: str | None = None) -> None:
        """Open a project folder.

        ``directory`` is supplied by the CLI (``--workspace``) and by tests; the
        GUI path takes the folder the sidebar's dialog produced.
        """

        chosen = directory or self.sidebar.chosen_directory
        if not chosen:
            return
        self._stop_workspace_scan()
        try:
            self._context.open_workspace(chosen)
        except QuantsMindError as exc:
            self.sidebar.set_error(exc.user_message)
            self.conversation.append_notice(exc.user_message, kind="error")
            return
        self.sidebar.set_error("")
        self.conversation.append_notice(f"Project opened: {chosen}")
        self._refresh_from_context()
        self._start_workspace_scan()

    def _on_close_workspace(self) -> None:
        self._stop_workspace_scan()
        self._pending_snapshot = None
        self._context.close_workspace()
        self.conversation.append_notice("Project closed.")
        self._refresh_from_context()

    # -- settings ----------------------------------------------------------
    def _on_settings(self) -> None:
        dialog = SettingsDialog(self._context.config_manager, parent=self)
        if dialog.exec():
            self._context.apply_configuration()
            self._refresh_from_context()
            self.conversation.append_notice("Settings saved.")
            self.activity.notice("configuration updated")

    def _on_new_conversation(self) -> None:
        self._context.reset_conversation()
        self.conversation.clear_transcript()
        self.activity.clear_activity()
        self._refresh_from_context()
        self.conversation.append_notice("Started a new conversation.")
        self._refresh_conversations()
        # A task is not part of the conversation, and a new conversation must not
        # leave a running one behind.
        self._release_task()

    def _refresh_conversations(self) -> None:
        """Mirror the stored conversations into the sidebar list."""

        summaries = self._context.list_conversations()
        self.sidebar.set_conversations(
            [(summary.id, summary.label()) for summary in summaries],
            active_id=self._context.session.id,
        )

    @Slot(str)
    def _on_conversation_selected(self, conversation_id: str) -> None:
        """Reopen a stored conversation, with nothing leaking from this one."""

        if conversation_id == self._context.session.id:
            return
        if not self._context.open_conversation(conversation_id):
            self.conversation.append_notice(
                "That conversation could not be opened; it may have been moved.",
                kind="error",
            )
            self._refresh_conversations()
            return
        self.conversation.clear_transcript()
        self.activity.clear_activity()
        self._restore_transcript()
        unavailable = self._context.unavailable_workspace
        if unavailable:
            self.conversation.append_notice(
                f"The project this conversation used is not available: {unavailable}. "
                "Open it again to continue working.",
                kind="error",
            )
        self._refresh_from_context()
        self._refresh_conversations()
        self.conversation.append_notice("Reopened the conversation.")

    def _restore_transcript(self) -> None:
        """Replay the restored conversation into the view.

        Replayed from the ordered history: user requests, assistant answers and the
        tool calls in between, in the order they actually happened, each with the
        time it happened rather than the time the window was opened. An assistant
        message that only asked for tools is skipped, because the tool lines it
        leads to already say what it did.
        """

        for entry in self._context.session.ordered_history():
            stamp = _clock(entry.created_at)
            if entry.tool_result is not None:
                self.conversation.append_tool_result(entry.tool_result, stamp=stamp)
                continue
            message = entry.message
            if message is None:
                continue
            role = getattr(message.role, "value", "")
            if role == "user":
                self.conversation.append_user(message.content, stamp=stamp)
            elif role == "assistant" and message.content.strip():
                self.conversation.append_assistant(
                    message.content,
                    meta=getattr(message, "model", "") or "",
                    stamp=stamp,
                )
        self._explain_restored_state()

    def _explain_restored_state(self) -> None:
        """Admit anything the stored record cannot honestly show as complete."""

        outcome = self._context.resumed_outcome
        if outcome in ("cancelled", "failed", "interrupted"):
            self.conversation.append_interrupted_notice(outcome)
        dropped = self._context.resumed_dropped_items
        if dropped:
            self.conversation.append_notice(
                f"Older history was dropped when this conversation was stored to stay "
                f"within its size limit ({dropped} items). What is shown is the recent "
                "part, not the whole conversation.",
                kind="info",
            )

    @Slot(str, str)
    def _on_conversation_rename_requested(self, conversation_id: str, title: str) -> None:
        """Rename a stored conversation and show the new name immediately."""

        if not title.strip():
            self.conversation.append_notice(
                "A conversation needs a name; the rename was not applied.",
                kind="error",
            )
            return
        renamed = self._context.rename_conversation(conversation_id, title)
        self._refresh_conversations()
        if renamed:
            self.activity.notice("conversation renamed")
            if conversation_id == self._context.session.id:
                self._refresh_from_context()
        else:
            self.conversation.append_notice(
                "That conversation could not be renamed; it may have been deleted.",
                kind="error",
            )

    @Slot(str)
    def _on_conversation_export_requested(self, conversation_id: str) -> None:
        """Write a conversation out as Markdown or JSON."""

        title = ""
        summary = next(
            (item for item in self._context.list_conversations() if item.id == conversation_id),
            None,
        )
        if summary is not None:
            title = summary.title
        path, fmt = self.sidebar.choose_export_destination(f"{_safe_filename(title)}.md")
        if not path:
            return
        written = self._context.export_conversation(path, conversation_id=conversation_id, fmt=fmt)
        if written is None:
            self.conversation.append_notice(
                "That conversation could not be exported; it may have been deleted.",
                kind="error",
            )
            return
        self.activity.notice("conversation exported")
        self.conversation.append_notice(f"Exported to {written}")

    @Slot(str)
    def _on_conversation_delete_requested(self, conversation_id: str) -> None:
        deleted = self._context.delete_conversation(conversation_id)
        self._refresh_conversations()
        self.conversation.append_notice(
            "Conversation deleted." if deleted else "Nothing to delete.",
            kind="info" if deleted else "error",
        )

    # -- approval -----------------------------------------------------------
    @Slot(object)
    def _on_prompt_ready(self, prompt: Any) -> None:
        """Show one approval prompt.

        This **must** run on the GUI thread. The ``@Slot(object)`` decorator and
        the explicit ``QueuedConnection`` in :meth:`_connect` are both required:
        connecting a Qt signal to a plain Python method leaves PySide6 to invoke
        it directly on the emitting thread, and the agent's worker thread is
        where the prompt originates. Building the dialog and touching the
        activity list and composer from there produces a blank window that never
        paints and never pumps events -- the "blank, not responding" failure.

        Every failure path denies. A dialog that cannot be built, or a request
        that arrives off-thread, must not silently authorise anything.
        """

        if not self._on_gui_thread():
            # Fail closed rather than manipulate widgets from another thread.
            logger.error("Approval request arrived off the GUI thread; denying")
            self.approval.deny_prompt()
            return

        if self._active_dialog is not None:
            # One decision at a time. A second prompt cannot legitimately
            # arrive while the first is unanswered; deny it rather than leak a
            # second window.
            logger.warning("Approval already pending; denying the new request")
            self.approval.deny_prompt()
            return

        try:
            dialog = ApprovalDialog(prompt, parent=self)
        except Exception:
            logger.exception("Could not build the approval dialog; denying")
            self.approval.deny_prompt()
            return

        self._active_dialog = dialog
        dialog.finished.connect(lambda _code: self._approval_finished(dialog))
        try:
            self.activity.record(prompt.tool, f"waiting for approval: {prompt.summary}", "awaiting")
            self._status_activity.setText(f"approval required: {prompt.tool}")
            self.input.setEnabled(False)
            self.send_button.setEnabled(False)
        except Exception:  # pragma: no cover - defensive
            logger.exception("Could not update the activity panel; denying")
            dialog.reject()
            return

        # show() rather than open()/exec(): exec() starts a nested event loop
        # that blocks this slot until answered, which is what made the window
        # appear unresponsive. show() + raise_ + activateWindow keeps the GUI
        # thread free to paint and to handle Stop.
        dialog.show()
        dialog.raise_()
        dialog.activateWindow()

    def _on_gui_thread(self) -> bool:
        """Whether the caller is on the window's own thread."""

        return QThread.currentThread() is self.thread()

    def _approval_finished(self, dialog: Any) -> None:
        """Release the waiting worker with the outcome the user chose."""

        self._active_dialog = None

        # Read everything needed from the dialog *before* scheduling its
        # deletion. deleteLater() only posts an event: the C++ object can be
        # gone at the next event-loop pass, and dereferencing it afterwards is
        # a use-after-free that crashes the interpreter mid-decision.
        outcome = dialog.outcome
        tool = dialog.prompt.tool

        # Break the reference cycle between the dialog's signal and the closure
        # that captured the dialog. Left connected, the Python wrapper outlives
        # the C++ object and PySide6 aborts at collection time with
        # "none_dealloc: deallocating None" or a segmentation fault.
        try:
            dialog.finished.disconnect()
        except (RuntimeError, TypeError):  # pragma: no cover - already gone
            logger.debug("Approval dialog signal was already disconnected")

        dialog.deleteLater()

        self.approval.resolve(outcome)
        self.input.setEnabled(True)
        self.send_button.setEnabled(True)
        state = "completed" if outcome is not ApprovalOutcome.DENY else "failed"
        note = (
            "user allowed once"
            if outcome is ApprovalOutcome.ALLOW_ONCE
            else "user allowed for session"
            if outcome is ApprovalOutcome.ALLOW_FOR_SESSION
            else "user denied"
        )
        self.activity.record(tool, note, state)
        self.conversation.append_tool_notice(tool, note, state)
        self._status_activity.setText("")

    def cancel_pending_approval(self) -> None:
        """Deny a pending prompt without a dialog answer (used by Stop/close)."""

        dialog = self._active_dialog
        if dialog is not None:
            dialog.reject()
        self.approval.cancel_pending()

    # -- composer ----------------------------------------------------------
    def _on_send_clicked(self) -> None:
        text = self.input.toPlainText().strip()
        if not text:
            return
        self.input.clear()
        self.conversation.append_user(text)
        if not self.bridge.start(text):
            self.conversation.append_notice("A request is already running.", kind="error")
            return
        self.conversation.begin_stream()
        self.activity.record("agent", "request sent", "started")

    def _on_cancel_clicked(self) -> None:
        # A pending approval is a request the user has not answered yet, so
        # Stop must answer it: denying is the only non-committal outcome.
        self.cancel_pending_approval()
        self.bridge.cancel()
        self._context.cancel()
        self.activity.record("agent", "cancellation requested", "started")

    # -- worker callbacks --------------------------------------------------
    def _on_text_delta(self, delta: str) -> None:
        self._streamed.append(delta)
        self.conversation.append_stream_delta(delta)

    def _on_tool_activity(self, tool: str, summary: str, state: str) -> None:
        self.activity.record(tool, summary, state)
        self.conversation.append_tool_notice(tool, summary, state)
        self._status_activity.setText(f"{tool}: {state}")

    def _on_turn_started(self) -> None:
        self._streamed = []
        self.sidebar.set_status("Working\u2026")

    def _on_turn_finished(self, outcome: Any) -> None:
        if not isinstance(outcome, AgentOutcome):
            return
        streamed = "".join(self._streamed)
        self.conversation.end_stream()
        if outcome.cancelled:
            self.conversation.append_notice("Request cancelled.")
            self.activity.record("agent", "cancelled", "failed")
        elif outcome.error:
            self.conversation.append_notice(outcome.error, kind="error")
            self.activity.record("agent", outcome.error, "failed")
        else:
            self._show_missing_text(streamed, outcome.text)
            self.conversation.end_stream(meta=self._response_meta(outcome))
            self.activity.record(
                "agent",
                f"{outcome.iterations} iteration(s), {len(outcome.tool_results)} tool call(s)",
                "completed",
            )
        self._streamed = []
        self.sidebar.set_status(self._status_text())
        self._status_activity.setText("")
        self._update_session_counts()
        # The conversation was saved as the turn ended; the list must show it.
        self._refresh_conversations()

    def _show_missing_text(self, streamed: str, final: str) -> None:
        """Render any part of the answer the stream did not deliver.

        A provider may stream nothing and still answer (the agent falls back to
        a single non-streamed request), so the window must not assume the
        transcript already contains the response.
        """

        if not final.strip():
            if not streamed.strip():
                self.conversation.append_stream_delta("\n_(empty response)_")
            return
        if final == streamed:
            return
        if streamed and final.startswith(streamed):
            self.conversation.append_stream_delta(final[len(streamed) :])
            return
        if not streamed:
            self.conversation.append_stream_delta(final)
            return
        self.conversation.append_stream_delta(f"\n{final}")

    def _on_turn_failed(self, message: str) -> None:
        self.conversation.end_stream()
        self.conversation.append_notice(message, kind="error")
        self.activity.record("agent", message, "failed")
        self.sidebar.set_status(self._status_text())
        self._update_session_counts()
        self._refresh_conversations()

    def _on_busy_changed(self, busy: bool) -> None:
        self.send_button.setEnabled(not busy)
        self.cancel_button.setVisible(busy)
        self.sidebar.set_enabled(not busy)
        self._hint_label.setText(
            "The agent is working \u2014 press Stop to cancel."
            if busy
            else "The agent can read and change files in this project; "
            "it asks before writing, deleting or running a command."
        )

    def _queue_agent_event(self, event: Any) -> None:
        """Enqueue a bus event. Called on the *publishing* thread.

        :class:`~quantsmind_agent.events.EventBus` is a plain Python callback
        list with no Qt involvement, so a subscriber runs on whichever thread
        published the event. Handling it inline would mutate the activity panel
        and status bar from the agent's worker thread, which Qt does not support
        and which manifests as an unstable or frozen window.
        """

        self._event_queue.put(event)

    def _drain_agent_events(self) -> None:
        """Handle queued events. Runs on the GUI thread via the timer."""

        while True:
            try:
                event = self._event_queue.get_nowait()
            except queue.Empty:
                return
            try:
                self._on_agent_event(event)
            except Exception:  # pragma: no cover - a widget must never kill the timer
                logger.exception("Failed to render an agent event")

    def _on_agent_event(self, event: Any) -> None:
        """Mirror interesting agent events into the activity panel."""

        name = getattr(event.type, "value", "")
        if name == "agent_started":
            self.sidebar.set_status("Working\u2026")
        elif name == "model_request":
            payload = event.payload
            self._status_activity.setText(f"requesting {payload.get('model', '')}")
        elif name == "permission_decision":
            payload = event.payload
            self.activity.notice(
                f"permission {payload.get('decision', '')}: {payload.get('tool', '')}"
            )
        elif name == "permission_approval":
            payload = event.payload
            approval = str(payload.get("approval", "")).lower()
            label = {
                "allow_once": "user allowed once",
                "allow_for_session": "user allowed for session",
                "deny": "user denied",
            }.get(approval, approval)
            self.activity.notice(f"{payload.get('tool', '')}: {label}")

    # -- helpers -----------------------------------------------------------
    def _response_meta(self, outcome: AgentOutcome) -> str:
        bits = [f"{outcome.iterations} iteration(s)"]
        if outcome.tool_results:
            bits.append(f"{len(outcome.tool_results)} tool call(s)")
        if outcome.tokens:
            bits.append(f"{outcome.tokens} tokens")
        return ", ".join(bits)

    def _status_text(self) -> str:
        return self._context.status_line()

    def _update_session_counts(self) -> None:
        session = self._context.session
        self.sidebar.set_session_counts(
            len(session.messages), len(session.turns), len(session.tool_results)
        )

    # -- lifecycle ---------------------------------------------------------
    def _release_event_subscription(self) -> None:
        """Stop consuming agent events. Safe to call more than once."""

        unsubscribe, self._unsubscribe_events = self._unsubscribe_events, None
        if unsubscribe is not None:
            unsubscribe()
        if self._event_timer is not None:
            self._event_timer.stop()

    def closeEvent(self, event: Any) -> None:
        # Deny first: a waiting worker must be released before the thread is
        # asked to quit, or the turn would block on an unanswered dialog.
        self.cancel_pending_approval()
        # A task is stopped, not merely hidden: the window closing is the end of
        # the work, and the record must say so.
        self._release_task()
        self._stop_workspace_scan()
        # Then stop consuming events and the timer that drains them, so a closed
        # window neither keeps a 50ms timer alive nor keeps mutating its own
        # widgets on the agent's thread boundary.
        self._release_event_subscription()
        self._context.shutdown()
        super().closeEvent(event)
