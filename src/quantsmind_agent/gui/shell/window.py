"""Main workbench window.

The window is a shell: activity bar, side panels (explorer, search, sessions),
an editor centre with a bottom panel, and the AI assistant panel on the right.
It wires widgets to :class:`~quantsmind_agent.app.ApplicationContext` services
and contains no provider or agent logic of its own.

AI context is never inferred: attaching a selection or a file is an explicit
user action, requires a trusted workspace, and the content travels only when the
user sends a message. The window is the only place that joins the editor to the
conversation, and it does so through the existing agent bridge.
"""

from __future__ import annotations

import contextlib
import queue
import re
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from PySide6.QtCore import Qt, QThread, QTimer, Signal, Slot
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QButtonGroup,
    QFileDialog,
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

from quantsmind_agent.diagnostics.logging import get_logger
from quantsmind_agent.diagnostics.memory import MemoryPressure
from quantsmind_agent.errors import QuantsMindError
from quantsmind_agent.gui.conversation.activity import ActivityView
from quantsmind_agent.gui.conversation.context import ContextAttachment, ContextBar
from quantsmind_agent.gui.conversation.transcript import ConversationView
from quantsmind_agent.gui.editor.tabs import (
    DEGRADED_OPEN_DOCUMENTS,
    MAX_OPEN_DOCUMENTS,
    EditorArea,
)
from quantsmind_agent.gui.security.approval import ApprovalBridge, ApprovalDialog
from quantsmind_agent.gui.security.trust_dialog import ask_workspace_trust
from quantsmind_agent.gui.settings.dialog import SettingsDialog
from quantsmind_agent.gui.shell.commands import Command, CommandPalette, CommandRegistry
from quantsmind_agent.gui.shell.explorer import WorkspaceExplorer
from quantsmind_agent.gui.shell.notifications import NotificationBar
from quantsmind_agent.gui.shell.quick_open import QuickOpenDialog
from quantsmind_agent.gui.shell.search_panel import SearchPanel
from quantsmind_agent.gui.shell.sidebar import Sidebar
from quantsmind_agent.gui.shell.status_bar import WorkbenchStatusBar
from quantsmind_agent.gui.shell.worker import AgentWorkerBridge
from quantsmind_agent.gui.shell.zones import Activity, ActivityBar, BottomPanel, TrustBanner
from quantsmind_agent.gui.studio.changesets import (
    ChangeCheck,
    ChangeFile,
    ChangeSetRecord,
    EngineeringChangeSets,
)
from quantsmind_agent.gui.studio.control_center import ControlCenter, ControlSnapshot
from quantsmind_agent.gui.studio.home import (
    EngineeringHome,
    HomeConversation,
    HomeSnapshot,
    HomeTask,
)
from quantsmind_agent.gui.studio.navigation import DESTINATIONS, StudioHeader
from quantsmind_agent.gui.task.panel import TaskPanel, WorkspaceScanBridge
from quantsmind_agent.persistence.editor_state import LayoutState
from quantsmind_agent.runtime.agent.core import AgentOutcome
from quantsmind_agent.security.permissions import ApprovalOutcome
from quantsmind_agent.security.workspace_trust import WorkspaceTrustState
from quantsmind_agent.services.tasks.models import PlanRejected, Task, TaskStatus
from quantsmind_agent.services.tasks.service import TaskService
from quantsmind_agent.services.workspace.watcher import WorkspaceWatcher
from quantsmind_agent.sessions import RecoveryVerdict
from quantsmind_agent.task_store import TaskSummary
from quantsmind_agent.version import APP_NAME, __version__

if TYPE_CHECKING:  # pragma: no cover - typing only
    from quantsmind_agent.app import ApplicationContext

__all__ = ["MainWindow"]

logger = get_logger(__name__)

#: How often the status bar samples process memory.
_MEMORY_INTERVAL_MS = 5_000
#: Debounce for storing open tabs after edits/open/close.
_STATE_SAVE_DELAY_MS = 500
#: Debounce for storing splitter sizes after a drag.
_LAYOUT_SAVE_DELAY_MS = 700
#: How many recently used files quick open ranks first.
_MAX_RECENT_FILES = 25
#: How many task records the home and ChangeSet views inspect.
_STUDIO_TASK_LIMIT = 12
#: Statuses that mean an engineering task has not reached a terminal state.
_ACTIVE_TASK_STATUSES = frozenset(
    {
        TaskStatus.DRAFT.value,
        TaskStatus.AWAITING_APPROVAL.value,
        TaskStatus.APPROVED.value,
        TaskStatus.RUNNING.value,
        TaskStatus.VERIFYING.value,
        TaskStatus.REPAIRING.value,
        TaskStatus.PAUSED.value,
        TaskStatus.BLOCKED.value,
    }
)


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

    #: Emitted from the watcher thread; always consumed on the GUI thread.
    workspace_changed = Signal(object)

    def __init__(self, context: ApplicationContext, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._context = context
        self.setWindowTitle(f"{APP_NAME} {__version__}")
        self.resize(1440, 900)
        self.setMinimumSize(1000, 640)

        # The window owns the worker bridge: threading is a GUI concern, and
        # resolving the agent per turn keeps it correct across rebuilds.
        self.bridge = AgentWorkerBridge(context.current_agent)
        context.attach_bridge(self.bridge)
        # The approval bridge is what turns an ASK permission into a dialog.
        # It is attached before the UI is built so a turn started immediately
        # after construction still reaches a human.
        self.approval = ApprovalBridge(self)
        context.attach_approval(self.approval)
        #: The application service that owns the task lifecycle.
        self._task_service = TaskService(
            data_dir=context.paths.data_dir,
            registry_factory=lambda _workspace: context.tools,
        )
        #: The approval dialog currently on screen, if any.
        self._active_dialog: ApprovalDialog | None = None
        #: The plan proposed and awaiting a human decision, or ``None``.
        self._pending_plan: Any = None
        #: The stored task being inspected from history, if any. Read-only.
        self._inspecting: Any = None
        #: The bounded snapshot the pending plan was proposed against.
        self._pending_snapshot: Any = None
        #: The plan-proposal thread so it can be joined on close.
        self._plan_thread: threading.Thread | None = None
        #: The one background workspace scan, when one is running.
        self._workspace_bridge: WorkspaceScanBridge | None = None
        #: Releases the agent-event subscription. ``None`` once released.
        self._unsubscribe_events: Callable[[], None] | None = None

        # -- workbench state ------------------------------------------------
        #: Canonical root the side/editor panels are currently bound to.
        self._panel_root = ""
        #: The selected activity page id.
        self._activity = "explorer"
        #: The primary destination currently on screen.
        self._destination = "home"
        #: The agent width from the persisted layout, kept for round-tripping.
        self._last_agent_width = 360
        #: Recently opened files, newest first, for quick-open ranking.
        self._recent_files: list[str] = []
        self._watcher: WorkspaceWatcher | None = None
        self._command_registry = CommandRegistry()
        self._command_palette: CommandPalette | None = None
        self._quick_open: QuickOpenDialog | None = None
        self._shortcuts: list[QShortcut] = []
        self._memory_warned = False
        self._loading_layout = False
        #: Set from ``busy_changed``. The bridge emits it before its thread is
        #: running, so ``bridge.is_busy`` is briefly false at that moment.
        self._busy = False
        #: Text streamed so far in the current turn.
        self._streamed: list[str] = []
        #: Agent events waiting to be rendered on the GUI thread.
        self._event_queue: queue.Queue[Any] = queue.Queue()

        self._build()
        self._connect()
        self._resume_previous_conversation()
        self._refresh_from_context()
        self._show_readiness_hint()
        self._show_interrupted_if_any()

    def _resume_previous_conversation(self) -> None:
        """Reopen the last conversation when the user asked for that.

        Only the transcript comes back: no approval grants, no undo history. If
        the project it was working in has moved, that is said plainly rather than
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

        self.header = StudioHeader(DESTINATIONS)
        outer.addWidget(self.header)

        self.trust_banner = TrustBanner()
        outer.addWidget(self.trust_banner)

        self.notification_bar = NotificationBar()
        outer.addWidget(self.notification_bar)

        self._destinations = QStackedWidget()
        self._destination_pages: dict[str, QWidget] = {}

        self.home_view = EngineeringHome()
        self._add_destination("home", self.home_view)

        self._agent_panel = self._build_agent_panel()
        ai_page = QWidget()
        ai_layout = QVBoxLayout(ai_page)
        ai_layout.setContentsMargins(0, 0, 0, 0)
        ai_layout.setSpacing(0)
        ai_layout.addWidget(self._agent_panel)
        self._add_destination("ai", ai_page)

        self._main_splitter = QSplitter(Qt.Orientation.Horizontal)
        self._main_splitter.setChildrenCollapsible(False)
        self._main_splitter.setHandleWidth(6)

        self.explorer = WorkspaceExplorer()
        self.search_panel = SearchPanel()
        self.sidebar = Sidebar()
        self._side_stack = QStackedWidget()
        self._side_stack.addWidget(self.explorer)
        self._side_stack.addWidget(self.search_panel)
        self._side_stack.addWidget(self.sidebar)
        self._activity_pages: dict[str, QWidget] = {
            "explorer": self.explorer,
            "search": self.search_panel,
            "sessions": self.sidebar,
        }
        self._main_splitter.addWidget(self._side_stack)

        self._center_splitter = QSplitter(Qt.Orientation.Vertical)
        self._center_splitter.setChildrenCollapsible(False)
        self._center_splitter.setHandleWidth(6)
        self.editor_area = EditorArea()
        self.bottom_panel = BottomPanel()
        self._center_splitter.addWidget(self.editor_area)
        self._center_splitter.addWidget(self.bottom_panel)
        # WP4/WP6: LSP manager (Tier-A fallback) + interactive PTY session.
        from quantsmind_agent.services.lsp.manager import LSPManager

        self._lsp_manager = LSPManager(None)
        self.editor_area.set_lsp_manager(self._lsp_manager)
        self._pty_session: Any = None
        self._pty_timer = QTimer(self)
        self._pty_timer.setInterval(100)
        self._pty_timer.timeout.connect(self._pump_pty_output)
        self.bottom_panel.terminal_input_submitted.connect(self._on_terminal_input)
        self.bottom_panel.add_root_requested.connect(self._on_add_root_requested)
        self.bottom_panel.remove_root_requested.connect(self._on_remove_root_requested)
        self.bottom_panel.root_selected.connect(self._on_root_selected)
        self._main_splitter.addWidget(self._center_splitter)
        self._main_splitter.setStretchFactor(0, 0)
        self._main_splitter.setStretchFactor(1, 1)
        self._center_splitter.setStretchFactor(0, 1)
        self._center_splitter.setStretchFactor(1, 0)
        self.bottom_panel.setMaximumHeight(200)

        self.activity_bar = ActivityBar(
            [
                Activity("explorer", "Files", "Browse and edit project files"),
                Activity("search", "Search", "Search file contents"),
                Activity("sessions", "Sessions", "Project, model and conversations"),
            ]
        )
        dev_page = QWidget()
        dev_layout = QVBoxLayout(dev_page)
        dev_layout.setContentsMargins(0, 0, 0, 0)
        dev_layout.setSpacing(0)
        dev_layout.addWidget(self.activity_bar)
        dev_layout.addWidget(self._main_splitter, 1)
        self._add_destination("development", dev_page)

        self.changesets_view = EngineeringChangeSets(context=self._context)
        self._add_destination("changes", self.changesets_view)

        self.control_center = ControlCenter()
        self._add_destination("control", self.control_center)

        outer.addWidget(self._destinations, 1)
        self.setCentralWidget(central)

        self.status_bar = WorkbenchStatusBar(self)
        self.setStatusBar(self.status_bar)
        # Legacy surfaces: the status labels are the same labels, not copies, so
        # an existing caller that writes to ``_status_model`` updates the bar.
        self._status_model = self.status_bar.model_label
        self._status_workspace = self.status_bar.workspace_label
        self._status_activity = self.status_bar.activity_label

        self._state_timer = QTimer(self)
        self._state_timer.setSingleShot(True)
        self._state_timer.setInterval(_STATE_SAVE_DELAY_MS)
        self._state_timer.timeout.connect(self._save_editor_state)

        self._layout_timer = QTimer(self)
        self._layout_timer.setSingleShot(True)
        self._layout_timer.setInterval(_LAYOUT_SAVE_DELAY_MS)
        self._layout_timer.timeout.connect(self._save_layout)

        self._memory_timer = QTimer(self)
        self._memory_timer.setInterval(_MEMORY_INTERVAL_MS)
        self._memory_timer.timeout.connect(self._update_memory_status)

        self._register_commands()
        self._install_shortcuts()
        self._apply_layout(self._context.editor_state.layout())
        self.show_destination("home")

    def _add_destination(self, destination_id: str, page: QWidget) -> None:
        self._destination_pages[destination_id] = page
        self._destinations.addWidget(page)

    def show_destination(self, destination_id: str) -> None:
        """Switch the primary destination and refresh its bounded facts."""

        page = self._destination_pages.get(destination_id)
        if page is None:
            destination_id = "home"
            page = self._destination_pages["home"]
        self._destination = destination_id
        self._destinations.setCurrentWidget(page)
        self.header.select(destination_id)
        if destination_id == "ai" and self._agent_panel.isHidden():
            self._agent_panel.setVisible(True)
        if destination_id == "home":
            self._refresh_home()
            # Rebuilding the cards can steal focus and scroll the page; entering
            # Home must always reveal the page heading. The deferred call runs
            # after Qt has laid the refreshed page out.
            self.home_view.scroll_to_top()
            QTimer.singleShot(0, self.home_view.scroll_to_top)
        elif destination_id == "changes":
            self._refresh_changesets()
        elif destination_id == "control":
            self._refresh_control_center()
        elif destination_id == "development":
            self.activity_bar.select(self._activity)

    def _build_agent_panel(self) -> QWidget:
        """The assistant panel: chat and task are views of the same project."""

        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(10, 12, 10, 10)
        layout.setSpacing(8)

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
            switcher.addWidget(button)
        self._view_switch.idClicked.connect(self._on_view_changed)
        switcher.addStretch(1)
        layout.addLayout(switcher)

        self._views = QStackedWidget()
        layout.addWidget(self._views, stretch=1)

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

        self.context_bar = ContextBar()
        chat_layout.addWidget(self.context_bar)

        chat_layout.addWidget(self._composer())
        self._views.addWidget(chat_page)

        self.task_panel = TaskPanel()
        self._views.addWidget(self.task_panel)
        self._connect_task_panel()

        panel.setMinimumWidth(320)
        return panel

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

        self._activate_agent_view(1)
        self.task_panel.refresh()

    def show_chat_view(self) -> None:
        self._activate_agent_view(0)

    def _activate_agent_view(self, index: int) -> None:
        self.show_destination("ai")
        self._agent_panel.setVisible(True)
        self._views.setCurrentIndex(index)
        button = self._view_switch.button(index)
        if button is not None:
            button.setChecked(True)

    @property
    def task_orchestrator(self) -> Any:
        """The orchestrator the panel is driving, if any."""

        return self.task_panel.orchestrator

    def describe_structure(self) -> dict[str, Any]:
        """What this window is made of, as data rather than pixels.

        The acceptance harness watches the process from the outside, so it can
        prove a window appeared but not what is inside it. This is how the
        *shipped binary* is shown to carry the workbench, rather than only the
        source tree: the same code path that draws the window reports its own
        composition.
        """

        tabs = self.editor_area.tab_states()
        return {
            "title": self.windowTitle(),
            "destination": self._destination,
            "destinations": list(self._destination_pages),
            "home": self.home_view.describe(),
            "changesets": self.changesets_view.describe(),
            "control_center": self.control_center.describe(),
            "views": [
                str(button.text()) for button in self._view_switch.buttons() if button is not None
            ],
            "current_view": ("Chat", "Task")[self._views.currentIndex()]
            if 0 <= self._views.currentIndex() <= 1
            else f"index {self._views.currentIndex()}",
            "task_panel": self.task_panel.describe_controls(),
            "workbench": {
                "activities": ["explorer", "search", "sessions"],
                "activity": self._activity,
                "explorer": self.explorer.describe(),
                "editor_tabs": len(tabs),
                "editor_paths": [tab.path for tab in tabs],
                "bottom_panel_visible": not self.bottom_panel.isHidden(),
                "agent_visible": not self._agent_panel.isHidden(),
                "restricted": self._context.workspace_restricted,
                "attachments": len(self.context_bar.attachments()),
                "searching": self.search_panel.is_searching,
                "memory": self._context.resources.snapshot().describe(),
            },
        }

    def _on_task_plan_requested(self, objective: str) -> None:
        """Ask the model for a plan through the structured-output path.

        The panel cannot invent a plan, so "Propose plan" asks the configured
        model for one, and the answer is validated here before anyone sees it.
        A malformed answer is a stated failure with reasons, not something that
        has to be spotted in a paragraph.

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
        if not self._context.workspace_trusted:
            self.task_panel.status.setText("Trust this workspace before planning a task.")
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

        from quantsmind_agent.services.tasks.planner import TaskPlanner

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
        self._refresh_studio_snapshots()

    def _on_task_run(self) -> None:
        orchestrator = self.task_orchestrator
        if orchestrator is None:
            self.task_panel.status.setText("There is no task to run.")
            return
        task = orchestrator.task
        if task.terminal or orchestrator.cancelled:
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
            self._refresh_studio_snapshots()

    def _on_task_pause(self) -> None:
        orchestrator = self.task_orchestrator
        if orchestrator is not None:
            orchestrator.pause()
            self.task_panel.refresh()
            self._refresh_studio_snapshots()

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
        """Open a stored task for inspection. Never starts execution."""

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
        """Resume the inspected task, after an explicit confirmation."""

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

        from quantsmind_agent.services.tasks.service import RecoveryRefused

        try:
            orchestrator = self._task_service.resume(task, workspace)
        except RecoveryRefused as exc:
            self.task_panel.status.setText(f"Cannot continue. {exc.verdict.describe()}.")
            return
        self._inspecting = None
        self.task_panel.attach(orchestrator)
        self.task_panel.status.setText(verdict.describe())
        self._refresh_studio_snapshots()

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
        self._hint_label.setWordWrap(True)
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

    # -- workbench commands -------------------------------------------------
    def _register_commands(self) -> None:
        registry = self._command_registry

        def add(
            identifier: str,
            title: str,
            callback: Callable[[], object],
            *,
            category: str = "",
            shortcut: str = "",
            enabled: Callable[[], bool] | None = None,
            dangerous: bool = False,
            keywords: tuple[str, ...] = (),
        ) -> Command:
            return registry.register(
                Command(
                    id=identifier,
                    title=title,
                    callback=callback,
                    category=category,
                    shortcut=shortcut,
                    enabled=enabled,
                    dangerous=dangerous,
                    keywords=keywords,
                )
            )

        def has_workspace() -> bool:
            return self._context.workspace is not None

        def has_tab() -> bool:
            return self.editor_area.current_tab() is not None

        def trusted() -> bool:
            return self._context.workspace_trusted

        def restricted() -> bool:
            return self._context.workspace_restricted

        add(
            "workspace.open",
            "Open Folder\u2026",
            self._pick_workspace,
            category="Workspace",
            shortcut="Ctrl+O",
            keywords=("project", "directory"),
        )
        add(
            "workspace.close",
            "Close Folder",
            self._on_close_workspace,
            category="Workspace",
            enabled=has_workspace,
        )
        add(
            "workspace.refresh",
            "Refresh Project Index",
            self._start_workspace_scan,
            category="Workspace",
            enabled=trusted,
        )
        add(
            "workspace.trust",
            "Trust This Workspace",
            self._on_trust_requested,
            category="Workspace",
            enabled=restricted,
            keywords=("agent", "enable"),
        )
        add(
            "workspace.restrict",
            "Restrict This Workspace",
            self._on_restrict_workspace,
            category="Workspace",
            enabled=trusted,
            dangerous=True,
        )

        add(
            "file.quick_open",
            "Quick Open File\u2026",
            self._show_quick_open,
            category="File",
            shortcut="Ctrl+P",
            enabled=has_workspace,
            keywords=("find", "name", "go to"),
        )
        add(
            "file.new",
            "New File",
            lambda: self.explorer.create_file(),
            category="File",
            enabled=has_workspace,
        )
        add(
            "file.new_folder",
            "New Folder",
            lambda: self.explorer.create_folder(),
            category="File",
            enabled=has_workspace,
        )
        add(
            "file.save",
            "Save",
            self.editor_area.save_current,
            category="File",
            shortcut="Ctrl+S",
            enabled=has_tab,
        )
        add(
            "file.save_as",
            "Save As\u2026",
            self.editor_area.save_as_current,
            category="File",
            shortcut="Ctrl+Shift+S",
            enabled=has_tab,
        )
        add(
            "file.save_all",
            "Save All",
            self.editor_area.save_all,
            category="File",
            enabled=has_tab,
        )
        add(
            "file.close_editor",
            "Close Editor",
            self.editor_area.close_current,
            category="File",
            shortcut="Ctrl+W",
            enabled=has_tab,
        )

        add(
            "view.explorer",
            "Show Files",
            lambda: self._show_activity("explorer"),
            category="View",
            shortcut="Ctrl+Shift+E",
            keywords=("explorer", "tree"),
        )
        add(
            "view.search",
            "Show Search",
            lambda: self._show_activity("search"),
            category="View",
            shortcut="Ctrl+Shift+F",
            keywords=("find in files",),
        )
        add(
            "view.sessions",
            "Show Sessions",
            lambda: self._show_activity("sessions"),
            category="View",
            keywords=("conversations", "project"),
        )
        add(
            "view.find",
            "Find in File",
            self.editor_area.show_find,
            category="View",
            shortcut="Ctrl+F",
            enabled=has_tab,
        )
        add(
            "view.replace",
            "Replace in File",
            lambda: self.editor_area.show_find(replace=True),
            category="View",
            shortcut="Ctrl+H",
            enabled=has_tab,
        )
        add(
            "view.split",
            "Split Editor",
            self.editor_area.split,
            category="View",
            shortcut="Ctrl+\\",
            enabled=has_tab,
        )
        add(
            "view.close_split",
            "Close Split",
            self.editor_area.close_split,
            category="View",
        )
        add(
            "view.toggle_bottom",
            "Toggle Problems/Output",
            self._toggle_bottom_panel,
            category="View",
            shortcut="Ctrl+J",
        )
        add(
            "view.toggle_agent",
            "Toggle Assistant Panel",
            self._toggle_agent_panel,
            category="View",
        )
        add(
            "view.toggle_side",
            "Toggle Side Panel",
            self._toggle_side_panel,
            category="View",
            shortcut="Ctrl+B",
        )

        add(
            "assistant.attach_selection",
            "Attach Selection to Chat",
            self._attach_selection,
            category="Assistant",
            shortcut="Ctrl+Shift+A",
            enabled=trusted,
        )
        add(
            "assistant.attach_file",
            "Attach Current File to Chat",
            self._attach_current_file,
            category="Assistant",
            enabled=trusted,
        )
        add(
            "assistant.send",
            "Send Message",
            self._on_send_clicked,
            category="Assistant",
            shortcut="Ctrl+Enter",
            enabled=trusted,
        )
        add(
            "conversation.new",
            "New Conversation",
            self._on_new_conversation,
            category="Conversation",
            shortcut="Ctrl+N",
        )
        add(
            "go.home",
            "Go to Engineering Home",
            lambda: self.show_destination("home"),
            category="Navigate",
            shortcut="Ctrl+1",
            keywords=("start", "overview", "project"),
        )
        add(
            "go.ai",
            "Go to AI Engineering",
            lambda: self.show_destination("ai"),
            category="Navigate",
            shortcut="Ctrl+2",
            keywords=("assistant", "chat", "task"),
        )
        add(
            "go.development",
            "Go to Development Studio",
            lambda: self.show_destination("development"),
            category="Navigate",
            shortcut="Ctrl+3",
            keywords=("editor", "files", "code"),
        )
        add(
            "go.changesets",
            "Go to Engineering ChangeSets",
            lambda: self.show_destination("changes"),
            category="Navigate",
            shortcut="Ctrl+4",
            keywords=("review", "diff", "changes"),
        )
        add(
            "go.control",
            "Go to Control Center",
            lambda: self.show_destination("control"),
            category="Navigate",
            shortcut="Ctrl+5",
            keywords=("provider", "security", "trust", "settings"),
        )
        add("settings.open", "Settings\u2026", self._on_settings, category="Settings")
        add("settings.security", "Security\u2026", self._on_security, category="Settings")
        add(
            "palette.commands",
            "Command Palette\u2026",
            self._show_command_palette,
            category="View",
            shortcut="Ctrl+Shift+P",
            keywords=("actions", "commands"),
        )
        add(
            "command.run",
            "Run Command\u2026",
            self._on_command_run,
            category="Command",
            enabled=has_workspace,
            dangerous=True,
            keywords=("shell", "process", "execute", "terminal"),
        )
        add(
            "onboarding.show",
            "Getting Started Wizard\u2026",
            self._show_onboarding,
            category="Help",
            keywords=("welcome", "setup", "first run"),
        )
        add(
            "shortcuts.show",
            "Keyboard Shortcuts\u2026",
            self._show_shortcuts,
            category="Help",
            shortcut="Ctrl+?",
            keywords=("keys", "bindings"),
        )
        add(
            "evidence.export",
            "Export Evidence Pack\u2026",
            self._export_evidence,
            category="ChangeSets",
            enabled=has_workspace,
        )

    def _install_shortcuts(self) -> None:
        bindings: tuple[tuple[str, Callable[[], object]], ...] = (
            ("Ctrl+Return", self._on_send_clicked),
            ("Ctrl+O", self._pick_workspace),
            ("Ctrl+K", self.sidebar.settings_requested.emit),
            ("Ctrl+?", self._show_shortcuts),
            ("Ctrl+N", self._on_new_conversation),
            ("Ctrl+P", self._show_quick_open),
            ("Ctrl+Shift+P", self._show_command_palette),
            ("Ctrl+Shift+F", lambda: self._show_activity("search")),
            ("Ctrl+Shift+E", lambda: self._show_activity("explorer")),
            ("Ctrl+S", self.editor_area.save_current),
            ("Ctrl+Shift+S", self.editor_area.save_as_current),
            ("Ctrl+W", self.editor_area.close_current),
            ("Ctrl+\\", self.editor_area.split),
            ("Ctrl+F", self.editor_area.show_find),
            ("Ctrl+H", lambda: self.editor_area.show_find(replace=True)),
            ("Ctrl+J", self._toggle_bottom_panel),
            ("Ctrl+B", self._toggle_side_panel),
            ("Ctrl+Shift+A", self._attach_selection),
            ("Ctrl+1", lambda: self.show_destination("home")),
            ("Ctrl+2", lambda: self.show_destination("ai")),
            ("Ctrl+3", lambda: self.show_destination("development")),
            ("Ctrl+4", lambda: self.show_destination("changes")),
            ("Ctrl+5", lambda: self.show_destination("control")),
        )
        for sequence, callback in bindings:
            shortcut = QShortcut(QKeySequence(sequence), self)
            shortcut.activated.connect(callback)
            self._shortcuts.append(shortcut)

    # -- layout ------------------------------------------------------------
    def _apply_layout(self, layout: LayoutState) -> None:
        """Restore zone sizes, visibility and the selected activity."""

        self._loading_layout = True
        try:
            self._main_splitter.setSizes([layout.sidebar_width, 900])
            self._center_splitter.setSizes([520, max(120, layout.bottom_height)])
            self._last_agent_width = layout.agent_width
            self.bottom_panel.setVisible(layout.bottom_visible)
            self._agent_panel.setVisible(layout.agent_visible)
            self._side_stack.setVisible(layout.sidebar_visible)
            activity = layout.activity if layout.activity in self._activity_pages else "explorer"
            self._activity = activity
            self._side_stack.setCurrentWidget(self._activity_pages[activity])
            self.activity_bar.select(activity)
        finally:
            self._loading_layout = False

    def _schedule_layout_save(self) -> None:
        if self._loading_layout:
            return
        self._layout_timer.start()

    def _save_layout(self) -> None:
        self._layout_timer.stop()
        main = self._main_splitter.sizes()
        center = self._center_splitter.sizes()
        layout = LayoutState(
            sidebar_width=max(220, main[0]) if main else 300,
            agent_width=self._last_agent_width,
            bottom_height=max(120, center[1]) if len(center) > 1 else 180,
            sidebar_visible=not self._side_stack.isHidden(),
            agent_visible=not self._agent_panel.isHidden(),
            bottom_visible=not self.bottom_panel.isHidden(),
            activity=self._activity,
        )
        self._context.editor_state.save_layout(layout)

    def _show_activity(self, activity_id: str) -> None:
        self.show_destination("development")
        if activity_id not in self._activity_pages:
            activity_id = "explorer"
        self._activity = activity_id
        self._side_stack.setCurrentWidget(self._activity_pages[activity_id])
        if self._side_stack.isHidden():
            self._side_stack.setVisible(True)
        self.activity_bar.select(activity_id)
        if activity_id == "search":
            self.search_panel.query_edit.setFocus()
        elif activity_id == "sessions":
            self._refresh_conversations()
        self._schedule_layout_save()

    def _toggle_bottom_panel(self) -> None:
        self.bottom_panel.toggle()
        self._schedule_layout_save()

    def _toggle_agent_panel(self) -> None:
        if self._destination != "ai":
            self.show_destination("ai")
            return
        self._agent_panel.setVisible(self._agent_panel.isHidden())
        self._schedule_layout_save()

    def _toggle_side_panel(self) -> None:
        self._side_stack.setVisible(self._side_stack.isHidden())
        self._schedule_layout_save()

    # -- persistence -------------------------------------------------------
    def _schedule_state_save(self) -> None:
        self._state_timer.start()

    def _save_editor_state(self) -> None:
        workspace = self._context.workspace
        if workspace is None:
            return
        self._context.editor_state.save_tabs(workspace.root, self.editor_area.tab_states())

    def _restore_editor_tabs(self, workspace: Any) -> None:
        restorable, missing = self._context.editor_state.restore_tabs(
            workspace.root, workspace.is_file
        )
        if restorable:
            missing = list(dict.fromkeys(missing + self.editor_area.restore_tabs(restorable)))
        if missing:
            shown = ", ".join(missing[:3])
            if len(missing) > 3:
                shown += ", \u2026"
            self._notify(
                f"{len(missing)} editor tab(s) could not be reopened because the "
                f"file is no longer available: {shown}",
                kind="info",
            )

    # -- notifications -----------------------------------------------------
    def _notify(self, text: str, *, kind: str = "info") -> None:
        if not text:
            return
        self.notification_bar.show_message(text, kind=kind)

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

        self.sidebar.open_workspace_requested.connect(self._on_sidebar_open_requested)
        self.sidebar.close_workspace_requested.connect(self._on_close_workspace)
        self.sidebar.settings_requested.connect(self._on_settings)
        self.sidebar.security_requested.connect(self._on_security)
        self.sidebar.new_conversation_requested.connect(self._on_new_conversation)
        self.sidebar.conversation_selected.connect(self._on_conversation_selected)
        self.sidebar.conversation_delete_requested.connect(self._on_conversation_delete_requested)
        self.sidebar.conversation_rename_requested.connect(self._on_conversation_rename_requested)
        self.sidebar.conversation_export_requested.connect(self._on_conversation_export_requested)

        self.activity_bar.activity_selected.connect(self._show_activity)
        self.header.destination_selected.connect(self.show_destination)
        self.header.open_workspace_requested.connect(self._pick_workspace)
        self.header.settings_requested.connect(self._on_settings)
        self._connect_studio_views()
        self.trust_banner.trust_requested.connect(self._on_trust_requested)
        self.context_bar.attach_selection_requested.connect(self._attach_selection)
        self.context_bar.attach_current_file_requested.connect(self._attach_current_file)

        self.explorer.file_activated.connect(self._on_explorer_file_activated)
        self.explorer.file_created.connect(self._on_explorer_file_created)
        self.explorer.path_deleted.connect(self.editor_area.close_relative)
        self.explorer.path_renamed.connect(self.editor_area.apply_rename)
        self.explorer.notice.connect(self._notify)
        self.explorer.expanded_changed.connect(self._update_watcher_directories)

        self.search_panel.open_requested.connect(self._on_search_open)
        self.search_panel.notice.connect(self._notify)

        self.editor_area.document_saved.connect(self._on_document_saved)
        self.editor_area.dirty_changed.connect(self._on_dirty_changed)
        self.editor_area.active_document_changed.connect(self._on_active_document_changed)
        self.editor_area.cursor_changed.connect(self.status_bar.set_cursor)
        self.editor_area.external_change_detected.connect(self._on_external_change_detected)
        self.editor_area.notice.connect(self._notify)
        self.editor_area.tabs_changed.connect(self._schedule_state_save)

        self._main_splitter.splitterMoved.connect(lambda _pos, _index: self._schedule_layout_save())
        self._center_splitter.splitterMoved.connect(
            lambda _pos, _index: self._schedule_layout_save()
        )
        self.workspace_changed.connect(
            self._on_workspace_changed, Qt.ConnectionType.QueuedConnection
        )

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

        self._memory_timer.start()
        self._update_memory_status()

    def _connect_studio_views(self) -> None:
        """Wire the Studio destinations to the window's existing services."""

        self.home_view.open_workspace_requested.connect(self._pick_workspace)
        self.home_view.start_task_requested.connect(lambda: self.show_destination("ai"))
        self.home_view.review_changes_requested.connect(lambda: self.show_destination("changes"))
        self.home_view.development_requested.connect(lambda: self.show_destination("development"))
        self.home_view.task_open_requested.connect(self._on_studio_task_open)
        self.home_view.conversation_open_requested.connect(self._on_conversation_selected)
        self.home_view.settings_requested.connect(self._on_settings)
        self.home_view.trust_requested.connect(self._on_trust_requested)

        self.changesets_view.task_open_requested.connect(self._on_studio_task_open)
        self.changesets_view.start_task_requested.connect(lambda: self.show_destination("ai"))
        self.changesets_view.refresh_requested.connect(self._refresh_studio_snapshots)
        self.changesets_view.apply_requested.connect(self._on_changeset_apply)
        self.changesets_view.rollback_requested.connect(self._on_changeset_rollback)

        self.control_center.settings_requested.connect(self._on_settings)
        self.control_center.security_requested.connect(self._on_security)
        self.control_center.trust_requested.connect(self._on_trust_requested)
        self.control_center.restrict_requested.connect(self._on_restrict_workspace)
        self.control_center.revoke_requested.connect(self._on_workspace_trust_revoked)
        self.control_center.open_workspace_requested.connect(self._pick_workspace)
        self.control_center.refresh_requested.connect(self._refresh_studio_snapshots)
        self.control_center.audit_export_requested.connect(self._on_audit_export)
        self.control_center.support_pack_requested.connect(self._on_support_pack)
        self.control_center.backup_requested.connect(self._on_backup)
        self.control_center.restore_requested.connect(self._on_restore)
        self.control_center.performance_requested.connect(self._on_performance)

    def _on_studio_task_open(self, task_id: str) -> None:
        """Inspect a stored task from Home or ChangeSets, never start it."""

        self.show_destination("ai")
        self._on_task_history_selected(task_id)

    def _on_changeset_apply(self, task_id: str) -> None:
        """Apply an approved ChangeSet from the ChangeSets page."""

        if self._context.workspace is None:
            self._notify("Open a project before applying a ChangeSet.", kind="error")
            return
        try:
            report = self._context.apply_changeset(task_id)
        except QuantsMindError as exc:
            self._notify(exc.user_message or str(exc), kind="error")
            return
        self._notify(
            f"Applied {report.applied_count} file(s) "
            f"({'verified' if report.verified else 'not verified'}).",
            kind="info",
        )
        self._refresh_studio_snapshots()

    def _on_changeset_rollback(self, task_id: str) -> None:
        """Roll back an applied ChangeSet from the ChangeSets page."""

        if self._context.workspace is None:
            self._notify("Open a project before rolling back a ChangeSet.", kind="error")
            return
        try:
            report = self._context.rollback_changeset(task_id)
        except QuantsMindError as exc:
            self._notify(exc.user_message or str(exc), kind="error")
            return
        self._notify(
            f"Rolled back {report.rolled_back_count} file(s).",
            kind="info",
        )
        self._refresh_studio_snapshots()

    def _on_command_run(self) -> None:
        """Run a command in the bottom panel terminal for the open workspace."""

        if self._context.workspace is None:
            self._notify("Open a project before running a command.", kind="error")
            return

        self.bottom_panel.setVisible(True)
        self.bottom_panel.tabs.setCurrentWidget(self.bottom_panel.output_page)

    def _show_onboarding(self) -> None:
        """Open the first-run onboarding wizard (v1.1)."""
        if self._context.workspace is None:
            self._notify("Open a project before onboarding.", kind="error")
            return
        from quantsmind_agent.gui.shell.onboarding import OnboardingDialog

        dialog = OnboardingDialog(str(self._context.workspace.root), self)
        dialog.exec()

    def _show_shortcuts(self) -> None:
        """Show the keyboard shortcut map (v1.3)."""
        from quantsmind_agent.gui.shell.shortcuts import ShortcutMapDialog

        dialog = ShortcutMapDialog(self)
        dialog.exec()

    def _export_evidence(self) -> None:
        """Export the evidence pack for the open workspace (v1.2)."""
        workspace = self._context.workspace
        if workspace is None:
            self._notify("Open a project before exporting evidence.", kind="error")
            return
        destination, _ = QFileDialog.getSaveFileName(
            self, "Export evidence pack", str(workspace.root / "evidence.zip"), "ZIP (*.zip)"
        )
        if not destination:
            return
        try:
            from dataclasses import asdict

            from quantsmind_agent.services.evidence import export_evidence_pack

            changesets_view = getattr(self, "changesets_view", None)
            records = list(getattr(changesets_view, "records", ()) or ())
            export_evidence_pack(
                destination,
                audit_events=[],
                changesets=[asdict(r) if not isinstance(r, dict) else r for r in records],
                verification=[],
            )
            self._notify(f"Evidence pack written: {destination}")
        except Exception as exc:
            self._notify(f"Evidence export failed: {exc}", kind="error")

    # -- studio snapshots --------------------------------------------------
    def _refresh_studio_snapshots(self) -> None:
        """Rebuild Home, ChangeSets and Control Center from bounded live facts."""

        history = self._task_service.history(limit=_STUDIO_TASK_LIMIT)
        changesets = self._change_records(history)
        self._refresh_home(history, changesets)
        self.changesets_view.set_records(changesets)
        self._refresh_control_center()

    def _refresh_home(
        self,
        history: list[TaskSummary] | None = None,
        changesets: tuple[ChangeSetRecord, ...] | None = None,
    ) -> None:
        if history is None:
            history = self._task_service.history(limit=_STUDIO_TASK_LIMIT)
        if changesets is None:
            changesets = self._change_records(history)

        workspace = self._context.workspace
        config = self._context.config_manager.config
        report = self._context.readiness_report()
        trust = self._trust_name()

        active = tuple(
            HomeTask(
                id=summary.id,
                objective=summary.objective,
                status=str(summary.status),
                steps_done=summary.steps_done,
                steps_total=summary.steps_total,
                updated_at=summary.updated_at,
                workspace=summary.workspace,
            )
            for summary in history
            if str(summary.status) in _ACTIVE_TASK_STATUSES
        )

        conversations = tuple(
            HomeConversation(
                id=summary.id,
                title=summary.title or summary.preview or "Untitled conversation",
                updated_at=summary.updated_at,
                preview=summary.preview,
            )
            for summary in self._context.list_conversations()[:5]
        )

        summary_text = ""
        ecosystems: tuple[str, ...] = ()
        intelligence = self._context.workspace_intel
        cached = intelligence.cached_snapshot if intelligence is not None else None
        if workspace is not None and self._context.workspace_trusted and cached is not None:
            summary_text = cached.summary()
            ecosystems = tuple(cached.ecosystems)

        readiness = ""
        if workspace is not None and not report.ready:
            readiness = report.reason

        recent: list[tuple[float, str]] = []
        for item in history[:6]:
            recent.append(
                (
                    item.updated_at,
                    f"{time.strftime('%Y-%m-%d %H:%M', time.localtime(item.updated_at))}"
                    f" \u00b7 task {str(item.status).replace('_', ' ')}: {item.objective[:70]}",
                )
            )
        for conversation in conversations:
            recent.append(
                (
                    conversation.updated_at,
                    f"{time.strftime('%Y-%m-%d %H:%M', time.localtime(conversation.updated_at))}"
                    f" \u00b7 conversation: {conversation.title[:70]}",
                )
            )
        if not recent:
            for event in self._context.security_audit.events[-4:]:
                stamp = event.timestamp.replace("T", " ")[:16]
                detail = event.detail or event.decision or event.reason_code
                recent.append((0.0, f"{stamp} \u00b7 {event.event}: {detail}"))
        recent.sort(key=lambda item: item[0], reverse=True)

        snapshot = HomeSnapshot(
            workspace=str(workspace.root) if workspace is not None else "",
            workspace_name=(workspace.root.name or str(workspace.root)) if workspace else "",
            trust=trust,
            project_summary=summary_text,
            ecosystems=ecosystems,
            model=config.provider.model,
            credential=self._context.masked_credential(),
            readiness=readiness,
            active_tasks=active,
            review_count=sum(1 for record in changesets if record.files),
            interrupted_count=len(self._interrupted()),
            conversations=conversations,
            recent_activity=tuple(text for _stamp, text in recent[:6]),
            security_events=len(self._context.security_audit.events),
        )
        self.home_view.set_snapshot(snapshot)

    def _refresh_changesets(self) -> None:
        history = self._task_service.history(limit=_STUDIO_TASK_LIMIT)
        self.changesets_view.set_records(self._change_records(history))

    def _change_records(self, history: list[TaskSummary]) -> tuple[ChangeSetRecord, ...]:
        """Task evidence as ChangeSet records. Never invents a transition."""

        records: list[ChangeSetRecord] = []
        for summary in history:
            task = self._task_service.store.load(summary.id)
            if task is not None:
                records.append(self._change_record(task))
        return tuple(records)

    @staticmethod
    def _change_record(task: Task) -> ChangeSetRecord:
        plan = task.plan
        expected: list[str] = []
        if plan is not None:
            expected.extend(plan.scope_paths)
            for step in plan.steps:
                expected.extend(step.expected_paths)
        expected_paths = tuple(dict.fromkeys(path for path in expected if path))

        approval = "no decision"
        if task.approval is not None:
            approval = "approved" if task.approval.approved else "rejected"

        verification = (
            str(task.verification.outcome) if task.verification is not None else "not recorded"
        )
        plan_summary = ""
        if plan is not None:
            plan_summary = f"{len(plan.steps)} planned step(s)"
            if plan.acceptance_criteria:
                plan_summary += f" \u00b7 {len(plan.acceptance_criteria)} acceptance criterion(s)"

        files = tuple(
            ChangeFile(
                path=change.path,
                operation=change.operation,
                bytes_before=change.bytes_before,
                bytes_after=change.bytes_after,
                existed_before=change.existed_before,
                exists_after=change.exists_after,
            )
            for change in task.file_changes
        )
        checks = tuple(
            ChangeCheck(
                check_id=check.check_id,
                status=str(check.status),
                exit_code=check.exit_code,
                detail=check.detail,
            )
            for check in task.checks
        )
        recovery = tuple(
            decision.detail or f"{decision.kind}: {decision.subject}"
            for decision in task.recovery
            if not decision.resolved
        )
        return ChangeSetRecord(
            task_id=task.id,
            objective=task.objective,
            status=str(task.status),
            workspace=task.workspace,
            updated_at=task.updated_at,
            approval=approval,
            verification=verification,
            plan_summary=plan_summary,
            expected_paths=expected_paths,
            files=files,
            checks=checks,
            recovery=recovery,
        )

    def _refresh_control_center(self) -> None:
        config = self._context.config_manager.config
        provider = config.provider
        agent = config.agent
        workspace = self._context.workspace
        report = self._context.readiness_report()
        events = self._context.security_audit.events
        denials = sum(
            1
            for event in events
            if event.decision == "denied" or str(event.event).endswith(".denied")
        )
        resource_status = self._context.resources.status()
        status_memory = self._context.resources.snapshot().describe()
        audit_status = self._context.audit_sink.status()
        self.control_center.set_snapshot(
            ControlSnapshot(
                provider_name=provider.name,
                provider_id=provider.provider_id or "",
                model=provider.model,
                base_url=provider.base_url,
                credential=self._context.masked_credential(),
                readiness="" if report.ready else report.reason,
                secure_storage=bool(self._context.secure_storage_available),
                workspace=str(workspace.root) if workspace is not None else "",
                trust=self._trust_name(),
                max_iterations=agent.max_iterations,
                max_context_files=agent.max_context_files,
                max_file_bytes=agent.max_file_bytes,
                command_timeout=agent.command_timeout_seconds,
                auto_approve_reads=agent.auto_approve_reads,
                auto_approve_writes=agent.auto_approve_writes,
                audit_events=len(events),
                audit_denials=denials,
                config_dir=str(self._context.paths.config_dir),
                data_dir=str(self._context.paths.data_dir),
                log_dir=str(self._context.paths.log_dir),
                memory=status_memory,
                resource_pressure=resource_status.pressure.value,
                resource_decisions=resource_status.decisions,
                resource_refusals=resource_status.refusals,
                resource_degraded=", ".join(resource_status.degradable_running),
                resource_last_refusal=resource_status.last_refusal,
                audit_path=str(Path(audit_status.path) / audit_status.store_name),
                audit_records=audit_status.records,
                audit_bytes_used=audit_status.bytes_used,
                audit_ceiling_bytes=audit_status.ceiling_bytes,
                audit_chain_ok=audit_status.chain_ok,
                audit_chain_note=audit_status.chain_note,
                audit_degraded=audit_status.degraded,
                audit_last_error=audit_status.last_error,
                audit_recovery_note=audit_status.recovery_note,
            )
        )

    def _on_support_pack(self) -> None:
        """Write the v1.4 diagnostic support pack where the user chooses."""
        workspace = self._context.workspace
        if workspace is None:
            self._notify("Open a project before exporting a support pack.", kind="error")
            return
        destination, _ = QFileDialog.getSaveFileName(
            self, "Export support pack", str(workspace.root / "support-pack.json"), "JSON (*.json)"
        )
        if destination:
            from quantsmind_agent.services.support_pack import write_support_pack

            write_support_pack(workspace.root, destination)
            self._notify(f"Support pack written: {destination}")

    def _on_backup(self) -> None:
        workspace = self._context.workspace
        if workspace is None:
            self._notify("Open a project before backup.", kind="error")
            return
        destination, _ = QFileDialog.getSaveFileName(
            self, "Backup workspace state", str(workspace.root / "backup.zip"), "ZIP (*.zip)"
        )
        if destination:
            from quantsmind_agent.services.backup import backup_workspace

            backup_workspace(workspace.root, destination)
            self._notify(f"Backup written: {destination}")

    def _on_restore(self) -> None:
        workspace = self._context.workspace
        if workspace is None:
            self._notify("Open a project before restore.", kind="error")
            return
        source, _ = QFileDialog.getOpenFileName(
            self, "Restore workspace state", str(workspace.root), "ZIP (*.zip)"
        )
        if source:
            from quantsmind_agent.services.backup import restore_workspace

            restored = restore_workspace(source, workspace.root)
            self._notify(f"Restored: {', '.join(restored) or 'nothing'}")

    def _on_performance(self) -> None:
        """Show the performance governor state (v1.4)."""
        try:
            governor = self._context.resources
            pressure = governor.pressure() if hasattr(governor, "pressure") else "normal"
            from quantsmind_agent.services.performance import govern_budget

            name = str(getattr(pressure, "value", pressure))
            budget = govern_budget(
                pressure=name if name in ("low", "moderate", "high", "critical") else "low"
            )
            self.control_center.ops_status.setText(
                f"Resource pressure: {name} · governed timeout {budget.timeout_seconds:.0f}s, output {budget.max_output_chars} chars"
            )
        except Exception as exc:
            self.control_center.ops_status.setText(f"Performance governor unavailable: {exc}")

    def _on_audit_export(self) -> None:
        """Write the operator's copy of the audit trail where they choose.

        The dialog is the only place the destination comes from: an export that
        picked its own path would scatter copies of the security trail across the
        machine without anyone agreeing to it.
        """

        sink = self._context.audit_sink
        destination, _filter = QFileDialog.getSaveFileName(
            self,
            "Export audit evidence",
            str(Path(self._context.paths.data_dir) / "audit-export.json"),
            "JSON (*.json)",
        )
        if not destination:
            self._notify("Audit export cancelled. Nothing was written.")
            return
        try:
            records = sink.export(Path(destination))
        except (OSError, QuantsMindError) as exc:
            logger.warning("audit export failed: %s", exc)
            detail = exc.user_message if isinstance(exc, QuantsMindError) else str(exc)
            self._notify(f"Audit export failed: {detail}", kind="error")
            return
        self._notify(
            f"{records} audit record(s) written to {destination}, with the chain report "
            "attached so a recipient can see whether the window is complete."
        )

    def _trust_name(self) -> str:
        if self._context.workspace is None:
            return "none"
        if self._context.workspace_restricted:
            return "restricted"
        if self._context.workspace_trusted:
            return "trusted"
        return "undecided"

    # -- context -----------------------------------------------------------
    def _refresh_from_context(self) -> None:
        config = self._context.config_manager.config
        provider = config.provider
        workspace = self._context.workspace
        self.sidebar.set_model(
            provider.name,
            provider.model,
            provider.base_url,
            self._context.masked_credential(),
        )
        self.sidebar.set_workspace(str(workspace.root) if workspace else None)
        self.sidebar.set_status(self._status_text())
        self.header.set_workspace(
            (workspace.root.name or str(workspace.root)) if workspace is not None else "No project",
            tooltip=str(workspace.root) if workspace is not None else "No project is open",
        )
        self.header.set_model(provider.model)
        self.header.set_trust(
            self._trust_name(),
            detail=str(workspace.root) if workspace is not None else "",
        )
        self._status_model.setText(f"{provider.model} @ {provider.base_url}")
        self._status_workspace.setText(str(workspace.root) if workspace else "No project open")
        self.status_bar.set_model(f"{provider.model} @ {provider.base_url}")
        self.status_bar.set_workspace(str(workspace.root) if workspace else "No project open")
        self._sync_workspace_panels()
        self._apply_trust_state()
        self._update_window_title()
        self._refresh_workspace_summary()
        self._update_session_counts()
        self._update_memory_status()
        self._refresh_studio_snapshots()

    def _sync_workspace_panels(self) -> None:
        """Bind the side and editor panels to the open root, once per change."""

        workspace = self._context.workspace
        root = str(workspace.root) if workspace is not None else ""
        if root == self._panel_root:
            return
        previous = self._panel_root
        self._panel_root = root
        if previous and not root:
            self.context_bar.clear()
            self._recent_files.clear()
        self.explorer.set_workspace(workspace)
        self.search_panel.set_workspace(self.explorer.service)
        self.editor_area.set_workspace(workspace)
        if workspace is None:
            self._stop_watcher()
            return
        self._restart_watcher()
        self._restore_editor_tabs(workspace)

    def _apply_trust_state(self) -> None:
        workspace = self._context.workspace
        restricted = self._context.workspace_restricted
        self.trust_banner.set_restricted(
            workspace=str(workspace.root) if restricted and workspace is not None else None
        )
        self.status_bar.set_trust(
            "restricted" if restricted else ("trusted" if workspace is not None else "")
        )
        if restricted:
            self.context_bar.clear()
        self._thread_composer_state()

    def _thread_composer_state(self) -> None:
        """Whether the composer may be used, for the reason that applies now."""

        restricted = self._context.workspace_restricted
        busy = self._busy or self.bridge.is_busy
        blocked = self._active_dialog is not None
        enabled = not restricted and not busy and not blocked
        self.input.setEnabled(enabled)
        self.send_button.setEnabled(enabled)
        self.context_bar.set_available(self._context.workspace_trusted)
        if restricted:
            self._hint_label.setText(
                "This workspace is open in restricted mode. Trust it to use the agent. "
                "You can still browse, edit and save files."
            )
        elif busy:
            self._hint_label.setText("The agent is working \u2014 press Stop to cancel.")
        else:
            self._hint_label.setText(
                "The agent can read and change files in this project; "
                "it asks before writing, deleting or running a command."
            )

    def _update_window_title(self) -> None:
        title = f"{APP_NAME} {__version__}"
        workspace = self._context.workspace
        if workspace is not None:
            title = f"{title} \u2014 {workspace.root.name or workspace.root}"
        if self.editor_area.has_dirty():
            title += " \u25cf"
        self.setWindowTitle(title)

    def _refresh_workspace_summary(self) -> None:
        """Paint whatever project facts are already known; never scan here."""

        workspace = self._context.workspace
        if workspace is None:
            self.task_panel.show_workspace(None)
            return
        if not self._context.workspace_trusted:
            self.task_panel.show_workspace(None)
            self.task_panel.project_summary.setText(
                "Project indexing is disabled while this workspace is restricted."
            )
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

        workspace = self._context.workspace
        if workspace is not None and not self._context.workspace_trusted:
            self.task_panel.project_summary.setText(
                "Project indexing is disabled while this workspace is restricted."
            )
            return
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
        if self._pending_plan is None:
            self._pending_snapshot = snapshot
        self._refresh_home()

    def _on_workspace_scan_failed(self, message: str) -> None:
        if self._context.workspace is None:
            return
        self.task_panel.project_summary.setText(f"Project scan failed: {message}")

    # -- watcher -----------------------------------------------------------
    def _restart_watcher(self) -> None:
        self._stop_watcher()
        workspace = self._context.workspace
        if workspace is None:
            return
        watcher = WorkspaceWatcher(workspace, self._watcher_changed)
        self._watcher = watcher
        watcher.start()
        watcher.set_directories(self.explorer.expanded_directories())

    def _stop_watcher(self) -> None:
        watcher, self._watcher = self._watcher, None
        if watcher is not None:
            watcher.stop()

    def _watcher_changed(self, changed: list[str]) -> None:
        """Runs on the watcher thread; the signal crosses to the GUI thread."""

        self.workspace_changed.emit(list(changed))

    def _update_watcher_directories(self) -> None:
        if self._watcher is None:
            return
        self._watcher.set_directories(self.explorer.expanded_directories())

    @Slot(object)
    def _on_workspace_changed(self, changed: object) -> None:
        if self._context.workspace is None:
            return
        directories = [str(item) for item in changed] if isinstance(changed, list) else []
        self.explorer.refresh_directories(directories)
        self.editor_area.check_external_changes()
        self._update_watcher_directories()

    # -- workspace ---------------------------------------------------------
    def _on_sidebar_open_requested(self) -> None:
        chosen = self.sidebar.chosen_directory
        if chosen:
            self._on_open_workspace(chosen, from_gui=True)

    def _pick_workspace(self) -> None:
        workspace = self._context.workspace
        start_at = str(workspace.root) if workspace is not None else ""
        chosen = self.sidebar.choose_directory(start_at)
        if chosen:
            self._on_open_workspace(chosen, from_gui=True)

    def _on_open_workspace(self, directory: str | None = None, *, from_gui: bool = False) -> None:
        """Open a project folder.

        ``directory`` is supplied by the CLI (``--workspace``), by the harness and
        by tests; those callers keep the baseline trusted behaviour. The desktop
        folder picker asks for an explicit trust decision when the root has never
        been decided, and always passes the answer to the context.
        """

        chosen = directory or self.sidebar.chosen_directory
        if not chosen:
            return
        asking = from_gui or directory is None

        workspace = self._context.workspace
        try:
            new_root = Path(chosen).expanduser().resolve(strict=False)
        except (OSError, ValueError):
            new_root = Path(chosen)
        same_root = workspace is not None and workspace.root == new_root

        decision: bool | None = None
        if asking:
            state = self._context.trust_store.state_for(chosen)
            if state is WorkspaceTrustState.UNDECIDED:
                decision = ask_workspace_trust(chosen, self)
                if decision is None:
                    return

        if workspace is not None and not same_root:
            # Persist where the user was, then let them decide about unsaved
            # buffers before their paths stop making sense.
            self._save_editor_state()
            if not self.editor_area.request_close():
                return
            self._stop_watcher()

        self._stop_workspace_scan()
        try:
            self._context.open_workspace(chosen, trusted=decision)
        except QuantsMindError as exc:
            self.sidebar.set_error(exc.user_message)
            self.conversation.append_notice(exc.user_message, kind="error")
            self._notify(exc.user_message, kind="error")
            return
        self.sidebar.set_error("")
        self.conversation.append_notice(f"Project opened: {chosen}")
        self._refresh_from_context()
        self._show_activity("explorer")
        # WP6: LSP manager for this workspace + Git/Problems panels.
        try:
            from quantsmind_agent.services.lsp.manager import LSPManager

            self._lsp_manager.shutdown()
            self._lsp_manager = LSPManager(chosen)
            self.editor_area.set_lsp_manager(self._lsp_manager)
        except Exception:
            pass
        self._refresh_git_panel()
        self._refresh_problems_panel()
        self._refresh_roots_and_profiles()
        if self._context.workspace_trusted:
            self._start_workspace_scan()

    def _on_close_workspace(self) -> None:
        workspace = self._context.workspace
        if workspace is not None:
            self._save_editor_state()
            if not self.editor_area.request_close():
                return
            self._stop_watcher()
        self._stop_pty_session()
        self._stop_workspace_scan()
        self._pending_snapshot = None
        self.context_bar.clear()
        self._recent_files.clear()
        self._context.close_workspace()
        with contextlib.suppress(Exception):
            self._lsp_manager.shutdown()
        self.conversation.append_notice("Project closed.")
        self._refresh_from_context()

    # -- terminal / LSP / Git integration (WP4/WP6) -------------------------
    def _stop_pty_session(self) -> None:
        self._pty_timer.stop()
        session, self._pty_session = self._pty_session, None
        if session is not None:
            with contextlib.suppress(Exception):
                session.terminate()

    def _pump_pty_output(self) -> None:
        session = self._pty_session
        if session is None:
            self._pty_timer.stop()
            return
        try:
            out = session.read(timeout=0.05)
        except Exception:
            out = None
        if out is not None and out.text:
            self.bottom_panel.show_output(out.text)
        if out is not None and out.exited:
            code = out.exit_code
            self.bottom_panel.show_output(f"\r\n[process exited: {code}]\r\n")
            self._pty_timer.stop()
            self._pty_session = None
        if session.is_alive is False and out is None:
            self._pty_timer.stop()

    def _on_terminal_input(self, text: str) -> None:
        workspace = self._context.workspace
        if workspace is None or not self._context.workspace_trusted:
            self.bottom_panel.show_output(
                "\r\n[terminal unavailable: open a trusted workspace]\r\n"
            )
            return
        typed = text.strip()
        if not typed:
            return
        self.bottom_panel.terminal_input.clear()
        self.bottom_panel.show_output(f"\r\n> {typed}\r\n")
        if self._pty_session is None:
            try:
                from quantsmind_agent.runtime.tools.builtin.pty_backend import PtyBackend

                shell = "cmd.exe" if __import__("sys").platform == "win32" else "sh"
                self._pty_session = PtyBackend().spawn(
                    [shell], cwd=str(workspace.root), timeout_seconds=600.0
                )
                self._pty_timer.start()
            except Exception as exc:
                self.bottom_panel.show_output(f"\r\n[terminal failed to start: {exc}]\r\n")
                self._pty_session = None
                return
        try:
            data = (typed + "\r\n").encode("utf-8", errors="replace")
            self._pty_session.write(data)
        except Exception as exc:
            self.bottom_panel.show_output(f"\r\n[terminal write failed: {exc}]\r\n")

    def _refresh_git_panel(self) -> None:
        workspace = self._context.workspace
        if workspace is None or not self._context.workspace_trusted:
            self.bottom_panel.set_git_status("No trusted workspace; Git unavailable.")
            return
        try:
            from quantsmind_agent.git_tools import GitDiffTool, GitStatusTool
            from quantsmind_agent.runtime.tools import ToolRegistry

            reg = ToolRegistry()
            reg.register_all([GitStatusTool(workspace), GitDiffTool(workspace)])
            status = reg.execute("git_status", {})
            if not status.ok:
                self.bottom_panel.set_git_status(f"Git unavailable: {status.error}")
                return
            d = status.data
            lines = [f"Branch: {d.get('branch')}  Clean: {d.get('clean')}"]
            for entry in d.get("staged", []):
                lines.append(f"S  {entry['path']}")
            for entry in d.get("unstaged", []):
                lines.append(f" M {entry['path']}")
            for path in d.get("untracked", []):
                lines.append(f"?? {path}")
            self.bottom_panel.set_git_status("\n".join(lines))
        except Exception as exc:
            self.bottom_panel.set_git_status(f"Git unavailable: {exc}")

    def _refresh_problems_panel(self) -> None:
        try:
            diags = self._lsp_manager.all_diagnostics()
            self.bottom_panel.set_problems(diags, status=self._lsp_manager.status)
        except Exception:
            pass

    def _refresh_roots_and_profiles(self) -> None:
        """WP1/WP2: root + profile selectors for the open workspace."""
        workspace = self._context.workspace
        if workspace is None:
            self.bottom_panel.set_roots([], "")
            self.bottom_panel.set_profiles([], "")
            return
        try:
            from quantsmind_agent.services.profiles import ProfileStore
            from quantsmind_agent.services.workspace.multi_root import MultiRootWorkspace

            multi = getattr(self, "_multi_root", None)
            primary_path = getattr(self, "_multi_primary_path", None)
            if multi is None or primary_path != str(workspace.root):
                multi = MultiRootWorkspace(workspace.root, trusted=True)
                self._multi_root = multi
                self._multi_primary_path = str(workspace.root)
            self.bottom_panel.set_roots(
                list(multi.root_ids()), multi.primary_id(), primary=multi.primary_id()
            )
            store = ProfileStore(workspace.root)
            self._profile_store = store
            profiles = [p.profile_id for p in store.list()]
            self.bottom_panel.set_profiles(profiles, profiles[0] if profiles else "")
        except Exception:
            pass

    def _on_add_root_requested(self) -> None:
        """WP1: register another root via native directory selection."""
        workspace = self._context.workspace
        if workspace is None:
            self.bottom_panel.set_git_status("Open a workspace before adding a root.")
            return
        initial = str(workspace.root)
        chosen = QFileDialog.getExistingDirectory(self, "Add project root", initial)
        if not chosen:
            return
        from pathlib import Path as _Path

        multi = getattr(self, "_multi_root", None)
        if multi is None:
            self._refresh_roots_and_profiles()
            multi = self._multi_root
        slug = (
            "".join(c if c.isalnum() or c in "-_" else "-" for c in _Path(chosen).name).strip("-")
            or "root"
        )
        base, root_id = slug, slug
        suffix = 2
        while root_id in multi.root_ids():
            root_id = f"{base}-{suffix}"
            suffix += 1
        try:
            multi.add_root(root_id, chosen)
        except Exception as exc:
            self.bottom_panel.set_git_status(f"Root rejected: {exc}")
            return
        # Refresh root-dependent UI without redirecting operations.
        self._refresh_roots_and_profiles()
        self.bottom_panel.set_git_status(f"Root registered: {root_id} ({chosen})")

    def _on_remove_root_requested(self) -> None:
        """WP1: remove the selected root; primary removal promotes a peer."""
        multi = getattr(self, "_multi_root", None)
        if multi is None or len(multi.root_ids()) <= 1:
            self.bottom_panel.set_git_status("Nothing to remove: only one root registered.")
            return
        data = self.bottom_panel.root_selector.currentData()
        rid = str(data or self.bottom_panel.root_selector.currentText() or "")
        if rid not in multi.root_ids():
            self.bottom_panel.set_git_status(f"Unknown root: {rid!r}")
            return
        removed = multi.remove_root(rid)
        self._refresh_roots_and_profiles()
        self.bottom_panel.set_git_status(
            f"Root {rid} removed; primary is now {multi.primary_id()}."
            if removed
            else "Root not removed."
        )

    def _on_root_selected(self, _label: str) -> None:
        """Root selection is view context only; it never silently redirects."""
        multi = getattr(self, "_multi_root", None)
        if multi is None:
            return
        rid = str(self.bottom_panel.root_selector.currentData() or "")
        if rid not in multi.root_ids():
            return
        self.bottom_panel.set_git_status(
            f"Viewing root: {rid} (primary: {multi.primary_id()}). Operations still target the open workspace."
        )

    # -- trust -------------------------------------------------------------
    def _on_trust_requested(self) -> None:
        workspace = self._context.workspace
        if workspace is None:
            return
        box = QMessageBox(self)
        box.setWindowTitle("Trust this workspace?")
        box.setText(
            f"Trust {workspace.root}?\n\n"
            "The agent will be able to read its files and index the project. "
            "Writes, deletions and commands still ask for approval where required."
        )
        trust = box.addButton("Trust Workspace", QMessageBox.ButtonRole.AcceptRole)
        cancel = box.addButton("Cancel", QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(cancel)
        box.exec()
        if box.clickedButton() is not trust:
            return
        try:
            self._context.set_workspace_trust(True)
        except QuantsMindError as exc:
            self._notify(exc.user_message, kind="error")
            return
        self._refresh_from_context()
        self._start_workspace_scan()
        self._notify("Workspace trusted. The agent and project index are enabled.")

    def _on_restrict_workspace(self) -> None:
        workspace = self._context.workspace
        if workspace is None:
            return
        box = QMessageBox(self)
        box.setWindowTitle("Restrict this workspace?")
        box.setText(
            f"Restrict {workspace.root}?\n\n"
            "A running turn is cancelled, and the agent, agent commands and project "
            "indexing are disabled. Browsing, editing and saving stay available."
        )
        restrict = box.addButton("Restrict Workspace", QMessageBox.ButtonRole.DestructiveRole)
        cancel = box.addButton("Cancel", QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(cancel)
        box.exec()
        if box.clickedButton() is not restrict:
            return
        try:
            self._context.set_workspace_trust(False)
        except QuantsMindError as exc:
            self._notify(exc.user_message, kind="error")
            return
        self._stop_workspace_scan()
        self._refresh_from_context()
        self._notify("Workspace restricted. The agent is disabled for this folder.")

    def _on_workspace_trust_revoked(self) -> None:
        try:
            self._context.revoke_workspace_trust()
        except QuantsMindError as exc:
            self._notify(exc.user_message, kind="error")
            return
        self._stop_workspace_scan()
        self._refresh_from_context()
        self._notify("Workspace trust was revoked. The folder is now restricted.")

    # -- explorer / search -------------------------------------------------
    def _on_explorer_file_activated(self, relative: str) -> None:
        self._open_relative(relative, preview=True)

    def _on_explorer_file_created(self, relative: str) -> None:
        self._open_relative(relative, preview=False)

    def _on_search_open(self, relative: str, line: int) -> None:
        self._open_relative(relative, preview=True, line=line)

    def _open_relative(self, relative: str, *, preview: bool, line: int | None = None) -> None:
        try:
            tab = self.editor_area.open_relative(relative, preview=preview, line=line)
        except QuantsMindError as exc:
            self._notify(exc.user_message, kind="error")
            return
        if tab is not None:
            self._show_activity("explorer")
            self._remember_recent(tab.document.relative)

    # -- editor ------------------------------------------------------------
    def _on_document_saved(self, path: str) -> None:
        self._remember_recent(self._relative_for_saved(path))
        self._schedule_state_save()

    def _relative_for_saved(self, path: str) -> str:
        workspace = self._context.workspace
        if workspace is None:
            return path.replace("\\", "/")
        try:
            relative = workspace.relative_to_root(Path(path))
        except Exception:
            return path.replace("\\", "/")
        return relative.replace("\\", "/")

    def _on_dirty_changed(self, _dirty: bool) -> None:
        self._update_window_title()
        self._schedule_state_save()

    def _on_active_document_changed(self, info: object) -> None:
        if info is None:
            self.status_bar.set_encoding("", "")
            self.status_bar.set_language("")
            return
        self.status_bar.set_encoding(getattr(info, "encoding", ""), getattr(info, "newline", ""))
        self.status_bar.set_language(getattr(info, "language", ""))
        relative = getattr(info, "relative", "")
        if relative:
            self._remember_recent(relative)

    def _on_external_change_detected(self, key: str) -> None:
        self.editor_area.show_external_change_dialog(key)
        self._schedule_state_save()

    def _remember_recent(self, relative: str) -> None:
        normalized = relative.replace("\\", "/")
        if not normalized:
            return
        self._recent_files = [normalized] + [
            path for path in self._recent_files if path != normalized
        ]
        del self._recent_files[_MAX_RECENT_FILES:]

    # -- AI context --------------------------------------------------------
    def _require_trusted(self) -> bool:
        workspace = self._context.workspace
        if workspace is None:
            self._notify("Open a folder before attaching code.", kind="error")
            return False
        if not self._context.workspace_trusted:
            self._notify(
                "This workspace is restricted. Trust it before sending code to the agent.",
                kind="error",
            )
            return False
        return True

    def _attach_selection(self) -> None:
        if not self._require_trusted():
            return
        tab = self.editor_area.current_tab()
        if tab is None:
            self._notify("Open a file and select some code first.", kind="error")
            return
        content = tab.editor.selected_text()
        if not content.strip():
            self._notify("Select some code in the editor first.", kind="error")
            return
        start_line, end_line = tab.editor.selected_line_range()
        attachment = ContextAttachment(
            kind="selection",
            relative=tab.document.relative,
            content=content,
            start_line=start_line,
            end_line=end_line,
        )
        if self.context_bar.add_attachment(attachment):
            self._notify(f"Attached {attachment.label}. It is sent only when you send a message.")
        else:
            self._notify("That selection is already attached.")

    def _attach_current_file(self) -> None:
        if not self._require_trusted():
            return
        tab = self.editor_area.current_tab()
        if tab is None:
            self._notify("Open a file first.", kind="error")
            return
        if tab.document.binary or tab.document.unsupported:
            self._notify(
                "This file is binary or unsupported, so its contents are not attached.",
                kind="error",
            )
            return
        content = tab.editor.plain_text()
        if not content.strip():
            self._notify("This file is empty, so there is nothing to attach.")
            return
        note = "unsaved editor buffer, not the version on disk" if tab.document.dirty else ""
        attachment = ContextAttachment(
            kind="file",
            relative=tab.document.relative,
            content=content,
            note=note,
        )
        if self.context_bar.add_attachment(attachment):
            self._notify(f"Attached {attachment.label}. It is sent only when you send a message.")
        else:
            self._notify("This file is already attached.")

    # -- quick open and palette -------------------------------------------
    def _show_quick_open(self) -> None:
        if self._context.workspace is None:
            self._notify("Open a folder before using quick open.", kind="error")
            return
        existing = self._quick_open
        if existing is not None:
            existing.raise_()
            existing.activateWindow()
            return
        dialog = QuickOpenDialog(self)
        dialog.file_selected.connect(self._on_quick_open_selected)
        dialog.finished.connect(lambda _code, bound=dialog: self._release_quick_open(bound))
        self._quick_open = dialog
        dialog.set_workspace(self.explorer.service, self._recent_files)
        dialog.show()
        dialog.raise_()
        dialog.activateWindow()
        dialog.input.setFocus()

    def _release_quick_open(self, dialog: QuickOpenDialog) -> None:
        if self._quick_open is dialog:
            self._quick_open = None
        dialog.shutdown()
        dialog.deleteLater()

    def _on_quick_open_selected(self, relative: str) -> None:
        self._open_relative(relative, preview=True)

    def _show_command_palette(self) -> None:
        existing = self._command_palette
        if existing is not None:
            existing.raise_()
            existing.activateWindow()
            return
        palette = CommandPalette(self._command_registry, self)
        palette.finished.connect(lambda _code, bound=palette: self._release_palette(bound))
        self._command_palette = palette
        palette.show()
        palette.raise_()
        palette.activateWindow()
        palette.input.setFocus()

    def _release_palette(self, palette: CommandPalette) -> None:
        if self._command_palette is palette:
            self._command_palette = None
        palette.deleteLater()

    # -- memory ------------------------------------------------------------
    def _update_memory_status(self) -> None:
        governor = self._context.resources
        pressure = governor.pressure()
        self.status_bar.set_memory(governor.snapshot().describe())
        if pressure in (MemoryPressure.CRITICAL, MemoryPressure.EXHAUSTED):
            # The governor owns the number; the editor only obeys it. Dirty
            # buffers are never dropped by ``trim`` -- it refuses them.
            budget = governor.editor_buffer_budget(
                normal=MAX_OPEN_DOCUMENTS, degraded=DEGRADED_OPEN_DOCUMENTS
            )
            released = self.editor_area.trim(budget)
            if not self._memory_warned:
                self._memory_warned = True
                detail = (
                    f" Closed {released} inactive editor buffer(s) to free memory."
                    if released
                    else ""
                )
                refused = governor.status().last_refusal
                refusal_note = f" Last refusal: {refused}." if refused else ""
                self._notify(
                    f"Memory use is high ({governor.snapshot().describe()}). Workspace "
                    f"indexing and background analysis are running with reduced budgets; "
                    f"permissions, approvals, audit writes and any transaction in progress "
                    f"are not affected.{detail}{refusal_note}",
                    kind="error",
                )
        elif pressure is MemoryPressure.NORMAL:
            self._memory_warned = False

    # -- settings ----------------------------------------------------------
    def _on_settings(self) -> None:
        dialog = SettingsDialog(
            self._context.config_manager,
            parent=self,
            security=self._context.security_controller(),
        )
        if dialog.exec():
            self._context.apply_configuration()
            self._refresh_from_context()
            self.conversation.append_notice("Settings saved.")
            self.activity.notice("configuration updated")

    def _on_security(self) -> None:
        """Open the security panel: providers, credentials, trust and audit."""

        from quantsmind_agent.gui.security.center import SecurityDialog

        workspace = self._context.workspace
        dialog = SecurityDialog(
            self._context.security_controller(),
            parent=self,
            workspace=str(workspace.root) if workspace is not None else "",
            trusted=(self._context.workspace_trusted if workspace is not None else None),
        )
        dialog.workspace_trust_revoked.connect(self._on_workspace_trust_revoked)
        dialog.exec()

    def _on_new_conversation(self) -> None:
        self._context.reset_conversation()
        self.conversation.clear_transcript()
        self.activity.clear_activity()
        self.context_bar.clear()
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
            self._thread_composer_state()
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
        self._thread_composer_state()
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
        if self._context.workspace_restricted:
            self._notify(
                "This workspace is restricted, so the agent cannot be used. "
                "Trust it first or keep editing locally.",
                kind="error",
            )
            return
        # The attachment is part of *this* request only. The transcript shows
        # what the user typed; the agent receives the explicit context too.
        payload = text + self.context_bar.prompt_suffix()
        self.input.clear()
        self.conversation.append_user(text)
        if not self.bridge.start(payload):
            self.conversation.append_notice("A request is already running.", kind="error")
            return
        self.context_bar.clear()
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
        self._busy = busy
        self.cancel_button.setVisible(busy)
        self.cancel_button.setEnabled(True)
        self.sidebar.set_enabled(not busy)
        self._thread_composer_state()

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
    def _shutdown_panels(self) -> None:
        for timer in (self._state_timer, self._layout_timer, self._memory_timer):
            timer.stop()
        self.search_panel.shutdown()
        dialog, self._quick_open = self._quick_open, None
        if dialog is not None:
            dialog.shutdown()
            dialog.deleteLater()
        palette, self._command_palette = self._command_palette, None
        if palette is not None:
            palette.deleteLater()

    def _release_event_subscription(self) -> None:
        """Stop consuming agent events. Safe to call more than once."""

        unsubscribe, self._unsubscribe_events = self._unsubscribe_events, None
        if unsubscribe is not None:
            unsubscribe()
        if getattr(self, "_event_timer", None) is not None:
            self._event_timer.stop()

    def closeEvent(self, event: Any) -> None:
        # Deny first: a waiting worker must be released before the thread is
        # asked to quit, or the turn would block on an unanswered dialog.
        self.cancel_pending_approval()
        if not self.editor_area.request_close():
            event.ignore()
            return
        # A task is stopped, not merely hidden: the window closing is the end of
        # the work, and the record must say so. Cancelling before persisting
        # state keeps shutdown prompt -- a disk write here could let a short
        # task run to completion instead of being stopped.
        self._release_task()
        self._stop_workspace_scan()
        self._stop_watcher()
        # Persist where the user was; editor tabs are only saved once the
        # unsaved-buffer question has been answered.
        self._save_layout()
        self._save_editor_state()
        # Then stop consuming events and the timer that drains them, so a closed
        # window neither keeps a 50ms timer alive nor keeps mutating its own
        # widgets on the agent's thread boundary.
        self._release_event_subscription()
        self._shutdown_panels()
        self._context.shutdown()
        super().closeEvent(event)
