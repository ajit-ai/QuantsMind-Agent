"""Engineering ChangeSets: review, apply and rollback engineering task changes.

The view shows real task evidence: the plan a user approved, the files the tools
actually changed, the checks that ran, and whether verification was conclusive.
When a workspace is open and a ChangeSet has been approved, the Apply button
runs the transactional apply engine; when a ChangeSet has been applied, the
Rollback button restores the recorded pre-images. Neither button touches a file
without an explicit human decision, and both are permission-gated.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QScrollArea,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from quantsmind_agent.gui.studio.cards import Badge, Card, EmptyState, clear_layout, section_label

if TYPE_CHECKING:  # pragma: no cover - GUI only holds a loose reference
    pass

__all__ = [
    "ChangeCheck",
    "ChangeFile",
    "ChangeSetRecord",
    "EngineeringChangeSets",
]


def _when(epoch: float) -> str:
    if epoch <= 0:
        return "unknown time"
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(epoch))


def _size(count: int) -> str:
    if count < 1024:
        return f"{count} B"
    return f"{count / 1024:.1f} KB"


_STATUS_TONES = {
    "completed": "success",
    "failed": "error",
    "cancelled": "error",
    "running": "accent",
    "verifying": "accent",
    "repairing": "warning",
    "awaiting_approval": "warning",
    "approved": "accent",
    "paused": "warning",
    "blocked": "error",
}
_VERIFICATION_TONES = {"pass": "success", "fail": "error", "inconclusive": "warning"}


@dataclass(frozen=True, slots=True)
class ChangeFile:
    path: str
    operation: str
    bytes_before: int = 0
    bytes_after: int = 0
    existed_before: bool = False
    exists_after: bool = True

    def describe(self) -> str:
        arrows = f"{_size(self.bytes_before)} \u2192 {_size(self.bytes_after)}"
        if not self.existed_before:
            arrows = f"new file \u00b7 {_size(self.bytes_after)}"
        elif not self.exists_after:
            arrows = f"removed \u00b7 was {_size(self.bytes_before)}"
        return f"{self.operation or 'changed':<10} {self.path}  ({arrows})"


@dataclass(frozen=True, slots=True)
class ChangeCheck:
    check_id: str
    status: str
    exit_code: int | None = None
    detail: str = ""

    def describe(self) -> str:
        bits = [self.check_id or "check", self.status or "not run"]
        if self.exit_code is not None:
            bits.append(f"exit {self.exit_code}")
        if self.detail:
            bits.append(self.detail)
        return "  \u00b7  ".join(bits)


@dataclass(frozen=True, slots=True)
class ChangeSetRecord:
    task_id: str
    objective: str
    status: str
    workspace: str = ""
    updated_at: float = 0.0
    approval: str = "no decision"
    verification: str = "not recorded"
    plan_summary: str = ""
    expected_paths: tuple[str, ...] = ()
    files: tuple[ChangeFile, ...] = ()
    checks: tuple[ChangeCheck, ...] = ()
    recovery: tuple[str, ...] = ()

    @property
    def has_changes(self) -> bool:
        return bool(self.files) or bool(self.expected_paths)

    def label(self) -> str:
        kind = "actual" if self.files else "planned"
        return (
            f"{self.objective}\n{self.status.replace('_', ' ')} \u00b7 "
            f"{len(self.files)} {kind} file(s) \u00b7 {_when(self.updated_at)}"
        )


class EngineeringChangeSets(QWidget):
    """A review surface over recorded task evidence with apply and rollback.

    Apply is offered only for an approved ChangeSet; rollback only for one that
    has been applied. Both require the workspace to be open and trusted, and
    both are gated by the permission broker. Clicking a button emits a signal
    the main window handles by calling the context's ChangeSet engines.
    """

    task_open_requested = Signal(str)
    start_task_requested = Signal()
    refresh_requested = Signal()
    apply_requested = Signal(str)
    rollback_requested = Signal(str)
    file_selected = Signal(str)

    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        context: Any = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("StudioPage")
        self._context = context
        self._records: tuple[ChangeSetRecord, ...] = ()
        self._selected_id = ""
        self._file_index = 0
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(30, 24, 30, 24)
        outer.setSpacing(14)

        heading = QHBoxLayout()
        column = QVBoxLayout()
        column.setSpacing(3)
        title = QLabel("Engineering ChangeSets")
        title.setObjectName("PageTitle")
        column.addWidget(title)
        subtitle = QLabel(
            "What approved engineering tasks planned, what their tools actually "
            "changed, and what verification established."
        )
        subtitle.setObjectName("PageSubtitle")
        subtitle.setWordWrap(True)
        column.addWidget(subtitle)
        heading.addLayout(column, 1)
        refresh = QPushButton("Refresh")
        refresh.clicked.connect(self.refresh_requested.emit)
        heading.addWidget(refresh)
        start = QPushButton("Start an AI task")
        start.setObjectName("PrimaryButton")
        start.clicked.connect(self.start_task_requested.emit)
        heading.addWidget(start)
        outer.addLayout(heading)

        # The disclaimer is shown only when no transactional engine is
        # available. Once the context wires the applier and rollback engine,
        # the banner is replaced by Apply/Rollback actions.
        if self._context is None or self._context.changeset_applier is None:
            banner = QFrame()
            banner.setObjectName("Banner")
            banner_layout = QHBoxLayout(banner)
            banner_layout.setContentsMargins(14, 10, 14, 10)
            note = QLabel(
                "Apply, rollback and conflict resolution are not implemented in this "
                "release. These records are evidence for review; no Apply button is "
                "offered because there is no transactional ChangeSet applier behind it."
            )
            note.setObjectName("CardBody")
            note.setWordWrap(True)
            banner_layout.addWidget(note)
            outer.addWidget(banner)

        self.splitter = QSplitter(Qt.Orientation.Horizontal)
        self.splitter.setChildrenCollapsible(False)
        self.splitter.setHandleWidth(8)

        list_card = Card("Recorded changes")
        self.listing = QListWidget()
        self.listing.currentItemChanged.connect(self._on_selection)
        list_card.add(self.listing, stretch=1)
        self.splitter.addWidget(list_card)

        self.detail_card = Card("ChangeSet detail")
        self.detail_scroll = QScrollArea()
        self.detail_scroll.setWidgetResizable(True)
        self.detail_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.detail_host = QWidget()
        self.detail_layout = QVBoxLayout(self.detail_host)
        self.detail_layout.setContentsMargins(0, 0, 6, 0)
        self.detail_layout.setSpacing(8)
        self.detail_scroll.setWidget(self.detail_host)
        self.detail_card.add(self.detail_scroll, stretch=1)
        self.splitter.addWidget(self.detail_card)
        self.splitter.setStretchFactor(0, 1)
        self.splitter.setStretchFactor(1, 2)
        outer.addWidget(self.splitter, 1)

        self._paint_empty()

    # -- data --------------------------------------------------------------
    @property
    def records(self) -> tuple[ChangeSetRecord, ...]:
        return self._records

    def set_records(self, records: tuple[ChangeSetRecord, ...]) -> None:
        self._records = tuple(record for record in records if record.has_changes)
        selected = self._selected_id
        self.listing.clear()
        if not self._records:
            self._selected_id = ""
            self._paint_empty()
            return
        for record in self._records:
            item = QListWidgetItem(record.label())
            item.setData(Qt.ItemDataRole.UserRole, record.task_id)
            item.setToolTip(
                f"{record.workspace or 'no workspace'} \u00b7 {_when(record.updated_at)}"
            )
            self.listing.addItem(item)
        index = next(
            (row for row, record in enumerate(self._records) if record.task_id == selected),
            0,
        )
        self.listing.setCurrentRow(index)

    def _paint_empty(self) -> None:
        clear_layout(self.detail_layout)
        empty = EmptyState(
            "No ChangeSets recorded yet.",
            "When an engineering task finishes, the files it actually changed and the "
            "checks it ran are recorded here for review.",
            action="Start an AI task",
        )
        if empty.action is not None:
            empty.action.clicked.connect(self.start_task_requested.emit)
        self.detail_layout.addWidget(empty)
        self.detail_layout.addStretch(1)

    # -- rendering ---------------------------------------------------------
    def _on_selection(self, current: QListWidgetItem | None, _previous: object) -> None:
        task_id = str(current.data(Qt.ItemDataRole.UserRole) or "") if current else ""
        self._selected_id = task_id
        record = next((item for item in self._records if item.task_id == task_id), None)
        if record is None:
            self._paint_empty()
            return
        self._render(record)

    def _render(self, record: ChangeSetRecord) -> None:
        clear_layout(self.detail_layout)

        title = QLabel(record.objective or "Untitled engineering task")
        title.setObjectName("PageTitle")
        title.setWordWrap(True)
        self.detail_layout.addWidget(title)

        badges = QHBoxLayout()
        badges.setSpacing(6)
        status = Badge(record.status.replace("_", " "), tone=_STATUS_TONES.get(record.status, ""))
        badges.addWidget(status)
        approval_tone = {"approved": "success", "rejected": "error"}.get(record.approval, "")
        badges.addWidget(Badge(f"approval: {record.approval}", tone=approval_tone))
        badges.addWidget(
            Badge(
                f"verification: {record.verification}",
                tone=_VERIFICATION_TONES.get(record.verification, ""),
            )
        )
        badges.addStretch(1)
        self.detail_layout.addLayout(badges)

        for label, value in (
            ("Workspace", record.workspace or "none"),
            ("Recorded", _when(record.updated_at)),
            ("Task", record.task_id),
        ):
            row = QHBoxLayout()
            key = QLabel(label)
            key.setObjectName("StatusText")
            key.setMinimumWidth(80)
            val = QLabel(value)
            val.setObjectName("MonoText")
            val.setWordWrap(True)
            val.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            row.addWidget(key)
            row.addWidget(val, 1)
            self.detail_layout.addLayout(row)

        if record.plan_summary:
            summary = QLabel(record.plan_summary)
            summary.setObjectName("CardBody")
            summary.setWordWrap(True)
            self.detail_layout.addWidget(summary)

        self.detail_layout.addWidget(section_label("Planned scope"))
        if record.expected_paths:
            for path in record.expected_paths:
                line = QLabel(path)
                line.setObjectName("MonoText")
                line.setWordWrap(True)
                self.detail_layout.addWidget(line)
        else:
            self.detail_layout.addWidget(self._muted("The plan declared no paths."))

        self.detail_layout.addWidget(section_label("Actual file changes"))
        if record.files:
            nav_row = QHBoxLayout()
            prev_btn = QPushButton("◀ Prev file")
            prev_btn.setObjectName("GhostButton")
            prev_btn.clicked.connect(self.previous_file)
            nav_row.addWidget(prev_btn)
            next_btn = QPushButton("Next file ▶")
            next_btn.setObjectName("GhostButton")
            next_btn.clicked.connect(self.next_file)
            nav_row.addWidget(next_btn)
            nav_row.addStretch(1)
            self.detail_layout.addLayout(nav_row)
            for change in record.files:
                kind = self.file_kind(change)
                badge = Badge(kind, tone={"A": "success", "M": "warning", "D": "error"}[kind])
                row = QHBoxLayout()
                row.addWidget(badge)
                line = QLabel(change.describe())
                line.setObjectName("MonoText")
                line.setWordWrap(True)
                row.addWidget(line, 1)
                self.detail_layout.addLayout(row)
        else:
            self.detail_layout.addWidget(
                self._muted(
                    "This task recorded no file changes. The plan may still be waiting "
                    "for approval, or the work did not reach a write."
                )
            )

        self.detail_layout.addWidget(section_label("Execution and verification evidence"))
        if record.checks:
            for check in record.checks:
                tone = {
                    "pass": "success",
                    "passed": "success",
                    "ok": "success",
                    "fail": "error",
                    "failed": "error",
                }.get(check.status, "")
                row = QHBoxLayout()
                row.addWidget(Badge(check.status or "not run", tone=tone))
                line = QLabel(check.describe())
                line.setObjectName("MonoText")
                line.setWordWrap(True)
                row.addWidget(line, 1)
                self.detail_layout.addLayout(row)
            failed = [c for c in record.checks if c.status not in ("pass", "passed", "ok")]
            if failed:
                issue = self._muted(
                    f"{len(failed)} remaining issue(s): " + "; ".join(c.check_id for c in failed)
                )
                issue.setObjectName("ErrorText")
                self.detail_layout.addWidget(issue)
        else:
            self.detail_layout.addWidget(self._muted("No verification checks were recorded."))

        if record.recovery:
            self.detail_layout.addWidget(section_label("Recovery and rollback status"))
            for note in record.recovery:
                line = QLabel(note)
                line.setObjectName("ErrorText")
                line.setWordWrap(True)
                self.detail_layout.addWidget(line)

        self.detail_layout.addWidget(section_label("Actions"))
        action_row = QHBoxLayout()
        apply_button = QPushButton("Apply ChangeSet")
        apply_button.setObjectName("PrimaryButton")
        apply_button.setToolTip(
            "Apply the approved file changes to this workspace in a journaled, "
            "verified transaction."
        )
        apply_button.setEnabled(self._apply_enabled(record))
        apply_button.clicked.connect(
            lambda _checked=False, task_id=record.task_id: self.apply_requested.emit(task_id)
        )
        action_row.addWidget(apply_button)
        rollback_button = QPushButton("Rollback ChangeSet")
        rollback_button.setToolTip(
            "Restore the recorded pre-image of every applied file, undoing the ChangeSet."
        )
        rollback_button.setEnabled(self._rollback_enabled(record))
        rollback_button.clicked.connect(
            lambda _checked=False, task_id=record.task_id: self.rollback_requested.emit(task_id)
        )
        action_row.addWidget(rollback_button)
        action_row.addStretch(1)
        self.detail_layout.addLayout(action_row)

        footer = self._muted(
            "Apply and rollback are permission-gated and journaled. Nothing is "
            "overwritten without an explicit human decision and a matching approval."
        )
        self.detail_layout.addWidget(footer)

        open_button = QPushButton("Open task record")
        open_button.clicked.connect(
            lambda _checked=False, task_id=record.task_id: self.task_open_requested.emit(task_id)
        )
        self.detail_layout.addWidget(open_button)
        export_button = QPushButton("Export evidence pack…")
        export_button.setToolTip("Export audit slice + ChangeSet manifests + verification as ZIP.")
        export_button.clicked.connect(self._export_evidence_clicked)
        self.detail_layout.addWidget(export_button)
        self.detail_layout.addStretch(1)

    def _apply_enabled(self, record: ChangeSetRecord) -> bool:
        """Apply is enabled for an approved ChangeSet when the engine is wired."""

        if self._context is None or self._context.changeset_applier is None:
            return False
        return record.approval == "approved" and record.status in {
            "approved",
            "proposed",
            "conflicted",
        }

    def _rollback_enabled(self, record: ChangeSetRecord) -> bool:
        """Rollback is enabled for an applied ChangeSet when the engine is wired."""

        if self._context is None or self._context.changeset_rollback is None:
            return False
        return record.status in {
            "applied",
            "verified",
            "partially_applied",
            "failed",
        }

    @staticmethod
    def _muted(text: str) -> QLabel:
        label = QLabel(text)
        label.setObjectName("CardBody")
        label.setWordWrap(True)
        return label

    def describe(self) -> dict[str, object]:
        selected = next(
            (record for record in self._records if record.task_id == self._selected_id), None
        )
        added = sum(1 for r in self._records for f in r.files if not f.existed_before)
        modified = sum(
            1 for r in self._records for f in r.files if f.existed_before and f.exists_after
        )
        deleted = sum(1 for r in self._records for f in r.files if not f.exists_after)
        failed_checks = sum(
            1 for r in self._records for c in r.checks if c.status not in ("pass", "passed", "ok")
        )
        return {
            "records": len(self._records),
            "files": sum(len(record.files) for record in self._records),
            "checks": sum(len(record.checks) for record in self._records),
            "selected": self._selected_id,
            "added": added,
            "modified": modified,
            "deleted": deleted,
            "failed_checks": failed_checks,
            "recovery_notes": sum(len(r.recovery) for r in self._records),
            "file_index": self._file_index,
            "has_apply_button": any(
                button.text().lower().startswith("apply")
                for button in self.findChildren(QPushButton)
            ),
            "apply_enabled": bool(selected is not None and self._apply_enabled(selected)),
            "rollback_enabled": bool(selected is not None and self._rollback_enabled(selected)),
        }

    # -- WP6 review enhancements --------------------------------------------
    def _export_evidence_clicked(self) -> None:
        """Export the selected ChangeSet's evidence pack (v1.2)."""
        from pathlib import Path as _Path

        from PySide6.QtWidgets import QFileDialog

        record = self._selected_record()
        if record is None:
            return
        destination, _ = QFileDialog.getSaveFileName(
            self, "Export evidence pack", f"evidence-{record.task_id}.zip", "ZIP (*.zip)"
        )
        if not destination:
            return
        from dataclasses import asdict

        from quantsmind_agent.services.evidence import export_evidence_pack

        export_evidence_pack(
            _Path(destination),
            audit_events=[],
            changesets=[asdict(record)],
            verification=[asdict(c) for c in record.checks],
        )
        self.file_selected.emit(f"evidence exported: {destination}")

    @staticmethod
    def file_kind(change: ChangeFile) -> str:
        if not change.existed_before:
            return "A"
        if not change.exists_after:
            return "D"
        return "M"

    def set_loading(self, active: bool) -> None:
        """Loading state: disables actions and shows a placeholder."""
        if active:
            clear_layout(self.detail_layout)
            self.detail_layout.addWidget(self._muted("Loading ChangeSets…"))
            self.detail_layout.addStretch(1)

    def set_error(self, message: str) -> None:
        """Error state with retry via Refresh."""
        clear_layout(self.detail_layout)
        label = self._muted(message or "Could not load ChangeSets.")
        label.setObjectName("ErrorText")
        self.detail_layout.addWidget(label)
        retry = QPushButton("Retry")
        retry.clicked.connect(self.refresh_requested.emit)
        self.detail_layout.addWidget(retry)
        self.detail_layout.addStretch(1)

    def next_file(self) -> None:
        record = self._selected_record()
        if record is None or not record.files:
            return
        self._file_index = (self._file_index + 1) % len(record.files)
        self._emit_file(record)

    def previous_file(self) -> None:
        record = self._selected_record()
        if record is None or not record.files:
            return
        self._file_index = (self._file_index - 1) % len(record.files)
        self._emit_file(record)

    def _selected_record(self) -> ChangeSetRecord | None:
        return next((r for r in self._records if r.task_id == self._selected_id), None)

    def _emit_file(self, record: ChangeSetRecord) -> None:
        if record.files:
            self.file_selected.emit(record.files[self._file_index].path)

    def keyPressEvent(self, event: Any) -> None:
        if event.key() in (Qt.Key.Key_Right, Qt.Key.Key_PageDown):
            self.next_file()
            event.accept()
            return
        if event.key() in (Qt.Key.Key_Left, Qt.Key.Key_PageUp):
            self.previous_file()
            event.accept()
            return
        super().keyPressEvent(event)
