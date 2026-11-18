"""Engineering Home: the startup destination.

The page shows only facts the application actually holds: the open project, the
agent's readiness, engineering tasks recorded on disk, conversations, and the
changes those tasks recorded. A fresh installation gets explicit empty states
that explain how to start, not sample projects or invented counts.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from quantsmind_agent.gui.studio.cards import (
    Card,
    EmptyState,
    StatCard,
    clear_layout,
)

__all__ = ["EngineeringHome", "HomeConversation", "HomeSnapshot", "HomeTask"]


def _when(epoch: float) -> str:
    if epoch <= 0:
        return ""
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(epoch))


@dataclass(frozen=True, slots=True)
class HomeTask:
    """One engineering task, as the home page lists it."""

    id: str
    objective: str
    status: str
    steps_done: int = 0
    steps_total: int = 0
    updated_at: float = 0.0
    workspace: str = ""

    def progress(self) -> str:
        label = self.status.replace("_", " ")
        if self.steps_total <= 0:
            return label
        return f"{label} \u00b7 {self.steps_done}/{self.steps_total} steps"


@dataclass(frozen=True, slots=True)
class HomeConversation:
    """One stored conversation, as the home page lists it."""

    id: str
    title: str
    updated_at: float = 0.0
    preview: str = ""


@dataclass(frozen=True, slots=True)
class HomeSnapshot:
    """The bounded facts the home page paints. No service is consulted here."""

    workspace: str = ""
    workspace_name: str = ""
    trust: str = "none"
    project_summary: str = ""
    ecosystems: tuple[str, ...] = ()
    model: str = ""
    credential: str = ""
    readiness: str = ""
    active_tasks: tuple[HomeTask, ...] = ()
    completed_tasks: tuple[HomeTask, ...] = ()
    review_count: int = 0
    interrupted_count: int = 0
    conversations: tuple[HomeConversation, ...] = ()
    recent_activity: tuple[str, ...] = ()
    security_events: int = 0

    @property
    def has_workspace(self) -> bool:
        return bool(self.workspace)


class EngineeringHome(QWidget):
    """The calm overview the application opens on."""

    open_workspace_requested = Signal()
    start_task_requested = Signal()
    review_changes_requested = Signal()
    development_requested = Signal()
    task_open_requested = Signal(str)
    conversation_open_requested = Signal(str)
    settings_requested = Signal()
    trust_requested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("StudioPage")
        self._snapshot = HomeSnapshot()
        self._empty_states = 0
        self._banner_action = ""

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QFrame.Shape.NoFrame)
        page = QWidget()
        page.setObjectName("StudioPage")
        self._layout = QVBoxLayout(page)
        self._layout.setContentsMargins(30, 24, 30, 30)
        self._layout.setSpacing(16)
        self._scroll.setWidget(page)
        outer.addWidget(self._scroll)

        self._build_heading()
        self._build_banner()
        self._build_stats()
        self._build_columns()
        self._layout.addStretch(1)

        self.set_snapshot(HomeSnapshot())

    # -- construction ------------------------------------------------------
    def _build_heading(self) -> None:
        row = QHBoxLayout()
        row.setSpacing(12)
        column = QVBoxLayout()
        column.setSpacing(3)
        title = QLabel("Engineering Home")
        title.setObjectName("PageTitle")
        column.addWidget(title)
        subtitle = QLabel(
            "Your project, the engineering tasks in flight and the changes waiting "
            "for your review \u2014 in one place."
        )
        subtitle.setObjectName("PageSubtitle")
        subtitle.setWordWrap(True)
        column.addWidget(subtitle)
        row.addLayout(column, 1)

        self.open_button = QPushButton("Open project")
        self.open_button.setObjectName("PrimaryButton")
        self.open_button.clicked.connect(self.open_workspace_requested.emit)
        row.addWidget(self.open_button)

        self.start_button = QPushButton("Start an AI task")
        self.start_button.clicked.connect(self.start_task_requested.emit)
        row.addWidget(self.start_button)
        self._layout.addLayout(row)

    def _build_banner(self) -> None:
        self.banner = QFrame()
        self.banner.setObjectName("WarningBanner")
        layout = QHBoxLayout(self.banner)
        layout.setContentsMargins(14, 10, 14, 10)
        self.banner_text = QLabel("")
        self.banner_text.setWordWrap(True)
        self.banner_text.setObjectName("CardBody")
        layout.addWidget(self.banner_text, 1)
        self.banner_button = QPushButton("")
        self.banner_button.setObjectName("GhostButton")
        self.banner_button.clicked.connect(self._on_banner_action)
        layout.addWidget(self.banner_button)
        self.banner.setVisible(False)
        self._layout.addWidget(self.banner)

    def _build_stats(self) -> None:
        row = QHBoxLayout()
        row.setSpacing(14)
        self.project_stat = StatCard("Project")
        self.tasks_stat = StatCard("Active tasks")
        self.review_stat = StatCard("Awaiting review")
        self.agent_stat = StatCard("Agent")
        for card in (self.project_stat, self.tasks_stat, self.review_stat, self.agent_stat):
            row.addWidget(card, 1)
        self._layout.addLayout(row)

    def _build_columns(self) -> None:
        columns = QHBoxLayout()
        columns.setSpacing(14)
        left = QVBoxLayout()
        left.setSpacing(14)
        right = QVBoxLayout()
        right.setSpacing(14)

        self.active_card = Card(
            "Active engineering tasks",
            subtitle="Plans, approvals and execution that have not finished yet.",
        )
        self.active_host = QWidget()
        self.active_layout = QVBoxLayout(self.active_host)
        self.active_layout.setContentsMargins(0, 0, 0, 0)
        self.active_layout.setSpacing(6)
        self.active_card.add(self.active_host)
        left.addWidget(self.active_card)

        self.review_card = Card(
            "ChangeSets awaiting review",
            subtitle=(
                "File changes recorded by finished engineering tasks. Review is "
                "read-only in this release."
            ),
        )
        self.review_host = QWidget()
        self.review_layout = QVBoxLayout(self.review_host)
        self.review_layout.setContentsMargins(0, 0, 0, 0)
        self.review_layout.setSpacing(6)
        self.review_card.add(self.review_host)
        left.addWidget(self.review_card)

        self.project_card = Card("Project")
        self.project_host = QWidget()
        self.project_layout = QVBoxLayout(self.project_host)
        self.project_layout.setContentsMargins(0, 0, 0, 0)
        self.project_layout.setSpacing(6)
        self.project_card.add(self.project_host)
        right.addWidget(self.project_card)

        self.conversations_card = Card("Recent conversations")
        self.conversations_host = QWidget()
        self.conversations_layout = QVBoxLayout(self.conversations_host)
        self.conversations_layout.setContentsMargins(0, 0, 0, 0)
        self.conversations_layout.setSpacing(4)
        self.conversations = QListWidget()
        self.conversations.setMaximumHeight(132)
        self.conversations.itemClicked.connect(self._on_conversation_clicked)
        self.conversations_card.add(self.conversations_host)
        right.addWidget(self.conversations_card)

        self.activity_card = Card("Recent activity")
        self.activity_host = QWidget()
        self.activity_layout = QVBoxLayout(self.activity_host)
        self.activity_layout.setContentsMargins(0, 0, 0, 0)
        self.activity_layout.setSpacing(4)
        self.activity_card.add(self.activity_host)
        right.addWidget(self.activity_card)

        columns.addLayout(left, 3)
        columns.addLayout(right, 2)
        self._layout.addLayout(columns)

    # -- data --------------------------------------------------------------
    @property
    def snapshot(self) -> HomeSnapshot:
        return self._snapshot

    def set_snapshot(self, snapshot: HomeSnapshot) -> None:
        self._snapshot = snapshot
        self._empty_states = 0
        self._paint_stats(snapshot)
        self._paint_banner(snapshot)
        self._paint_active(snapshot)
        self._paint_review(snapshot)
        self._paint_project(snapshot)
        self._paint_conversations(snapshot)
        self._paint_activity(snapshot)

    def scroll_to_top(self) -> None:
        """Show the heading when the destination is entered.

        Rebuilding snapshot cards can move focus and make Qt scroll the page
        down on its own; the user should always arrive at the page title.
        """

        self._scroll.verticalScrollBar().setValue(0)

    def _paint_stats(self, snapshot: HomeSnapshot) -> None:
        if snapshot.has_workspace:
            self.project_stat.set_value(
                snapshot.workspace_name or "Project",
                caption=snapshot.workspace,
            )
        else:
            self.project_stat.set_value("No project", caption="Open a folder to begin.")

        active = len(snapshot.active_tasks)
        if active:
            first = snapshot.active_tasks[0]
            self.tasks_stat.set_value(str(active), caption=first.progress())
        else:
            self.tasks_stat.set_value("None", caption="No task is in progress.")

        if snapshot.review_count:
            self.review_stat.set_value(
                str(snapshot.review_count),
                caption="recorded change set(s) to inspect",
            )
        else:
            self.review_stat.set_value("0", caption="Nothing is waiting for review.")

        if snapshot.readiness:
            self.agent_stat.set_value("Not ready", caption=snapshot.readiness)
        elif snapshot.has_workspace:
            self.agent_stat.set_value("Ready", caption=snapshot.model or "Configured")
        else:
            self.agent_stat.set_value(
                "Standby",
                caption="Open a project to use the agent.",
            )

    def _paint_banner(self, snapshot: HomeSnapshot) -> None:
        self._banner_action = ""
        if not snapshot.has_workspace:
            self.banner.setVisible(False)
            return
        if snapshot.trust == "restricted":
            self.banner_text.setText(
                "This workspace is open in restricted mode. Browsing, editing and saving "
                "remain available; the agent and project indexing are disabled."
            )
            self.banner_button.setText("Trust workspace")
            self._banner_action = "trust"
        elif snapshot.readiness:
            self.banner_text.setText(f"The agent is not ready: {snapshot.readiness}")
            self.banner_button.setText("Provider settings")
            self._banner_action = "settings"
        else:
            self.banner.setVisible(False)
            return
        self.banner.setVisible(True)

    def _on_banner_action(self) -> None:
        if self._banner_action == "trust":
            self.trust_requested.emit()
        elif self._banner_action == "settings":
            self.settings_requested.emit()

    def _empty(self, title: str, body: str, action: str = "") -> EmptyState:
        self._empty_states += 1
        return EmptyState(title, body, action=action)

    def _paint_active(self, snapshot: HomeSnapshot) -> None:
        clear_layout(self.active_layout)
        if not snapshot.active_tasks:
            state = self._empty(
                "No engineering task is in progress.",
                "Describe one bounded change in AI Engineering. Nothing runs before "
                "you approve the plan.",
                action="Start an AI task",
            )
            if state.action is not None:
                state.action.clicked.connect(self.start_task_requested.emit)
            self.active_layout.addWidget(state)
            return
        listing = QListWidget()
        listing.setMaximumHeight(min(168, 34 + 30 * len(snapshot.active_tasks)))
        for task in snapshot.active_tasks:
            item = QListWidgetItem(f"{task.objective}\n{task.progress()}")
            item.setData(Qt.ItemDataRole.UserRole, task.id)
            item.setToolTip(f"Opened {_when(task.updated_at)}")
            listing.addItem(item)
        listing.itemClicked.connect(self._on_task_clicked)
        self.active_layout.addWidget(listing)
        open_button = QPushButton("Open in AI Engineering")
        open_button.clicked.connect(self.start_task_requested.emit)
        self.active_layout.addWidget(open_button)

    def _paint_review(self, snapshot: HomeSnapshot) -> None:
        clear_layout(self.review_layout)
        summary = QLabel(
            f"{snapshot.review_count} recorded change set(s) can be inspected."
            if snapshot.review_count
            else "Finished tasks have not recorded any file changes yet."
        )
        summary.setObjectName("CardBody")
        summary.setWordWrap(True)
        self.review_layout.addWidget(summary)
        if snapshot.interrupted_count:
            interrupted = QLabel(
                f"{snapshot.interrupted_count} task(s) were interrupted when the "
                "application last closed. Their outcome is unknown; inspect them in "
                "AI Engineering before continuing."
            )
            interrupted.setObjectName("ErrorText")
            interrupted.setWordWrap(True)
            self.review_layout.addWidget(interrupted)
        button = QPushButton("Review ChangeSets")
        button.clicked.connect(self.review_changes_requested.emit)
        self.review_layout.addWidget(button)

    def _paint_project(self, snapshot: HomeSnapshot) -> None:
        clear_layout(self.project_layout)
        if not snapshot.has_workspace:
            state = self._empty(
                "No project is open.",
                "Open a folder to browse and edit its files, and to let the agent work inside it.",
                action="Open project",
            )
            if state.action is not None:
                state.action.clicked.connect(self.open_workspace_requested.emit)
            self.project_layout.addWidget(state)
            return
        path = QLabel(snapshot.workspace)
        path.setObjectName("MonoText")
        path.setWordWrap(True)
        path.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.project_layout.addWidget(path)

        if snapshot.project_summary:
            summary = QLabel(snapshot.project_summary)
            summary.setObjectName("CardBody")
            summary.setWordWrap(True)
            self.project_layout.addWidget(summary)
        elif snapshot.trust == "restricted":
            summary = QLabel("Project indexing is disabled while this workspace is restricted.")
            summary.setObjectName("CardBody")
            summary.setWordWrap(True)
            self.project_layout.addWidget(summary)
        else:
            summary = QLabel(
                "Project indexing has not run yet. Refresh the project from AI Engineering."
            )
            summary.setObjectName("CardBody")
            summary.setWordWrap(True)
            self.project_layout.addWidget(summary)

        if snapshot.ecosystems:
            ecosystems = QLabel("Ecosystems: " + ", ".join(snapshot.ecosystems))
            ecosystems.setObjectName("StatusText")
            ecosystems.setWordWrap(True)
            self.project_layout.addWidget(ecosystems)

        trust = QLabel(
            {
                "trusted": "Trusted: the agent may read and index this project.",
                "restricted": "Restricted: the agent is disabled for this project.",
            }.get(snapshot.trust, "No trust decision has been recorded for this project.")
        )
        trust.setObjectName("StatusText")
        trust.setWordWrap(True)
        self.project_layout.addWidget(trust)

        open_dev = QPushButton("Open in Development Studio")
        open_dev.clicked.connect(self.development_requested.emit)
        self.project_layout.addWidget(open_dev)

    def _paint_conversations(self, snapshot: HomeSnapshot) -> None:
        clear_layout(self.conversations_layout)
        self.conversations = QListWidget()
        self.conversations.setMaximumHeight(132)
        self.conversations.itemClicked.connect(self._on_conversation_clicked)
        for conversation in snapshot.conversations:
            item = QListWidgetItem(conversation.title)
            item.setData(Qt.ItemDataRole.UserRole, conversation.id)
            detail = _when(conversation.updated_at)
            if conversation.preview:
                detail = f"{detail} \u00b7 {conversation.preview[:88]}"
            item.setToolTip(detail)
            self.conversations.addItem(item)
        if snapshot.conversations:
            self.conversations_layout.addWidget(self.conversations)
            return
        empty = QLabel("No conversations yet. Start one in AI Engineering.")
        empty.setObjectName("EmptyBody")
        empty.setWordWrap(True)
        self.conversations_layout.addWidget(empty)

    def _paint_activity(self, snapshot: HomeSnapshot) -> None:
        clear_layout(self.activity_layout)
        if not snapshot.recent_activity:
            empty = QLabel("No activity has been recorded yet.")
            empty.setObjectName("EmptyBody")
            empty.setWordWrap(True)
            self.activity_layout.addWidget(empty)
            return
        for line in snapshot.recent_activity:
            label = QLabel(f"\u2022  {line}")
            label.setObjectName("CardBody")
            label.setWordWrap(True)
            self.activity_layout.addWidget(label)

    def _on_task_clicked(self, item: QListWidgetItem) -> None:
        task_id = str(item.data(Qt.ItemDataRole.UserRole) or "")
        if task_id:
            self.task_open_requested.emit(task_id)

    def _on_conversation_clicked(self, item: QListWidgetItem) -> None:
        conversation_id = str(item.data(Qt.ItemDataRole.UserRole) or "")
        if conversation_id:
            self.conversation_open_requested.emit(conversation_id)

    def describe(self) -> dict[str, object]:
        """The state a test can assert on, without inspecting pixels."""

        return {
            "workspace": self._snapshot.workspace,
            "trust": self._snapshot.trust,
            "workspace_chip": self.project_stat.value_label.text(),
            "active_tasks": len(self._snapshot.active_tasks),
            "review_count": self._snapshot.review_count,
            "conversations": len(self._snapshot.conversations),
            "empty_states": self._empty_states,
            "banner_visible": not self.banner.isHidden(),
            "banner_action": self._banner_action,
        }
