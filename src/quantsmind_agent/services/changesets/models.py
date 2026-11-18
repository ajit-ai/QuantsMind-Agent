"""ChangeSet data model: the first-class artefact a task produces.

A ChangeSet is an intent-bound transaction over files, with its own lifecycle
(separate from :class:`~quantsmind_agent.services.changesets.lifecycle.ChangeSetStatus`)
and its own persistence layout (`services/changesets/storage.py`).

Nothing in this module imports Qt or the application GUI; it is a pure data
module so the engine can be tested without a display server.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from quantsmind_agent.services.changesets.lifecycle import (
    TERMINAL_STATES,
    UNSTABLE_STATES,
    ChangeSetStatus,
)

__all__ = [
    "ApplyReport",
    "ChangeSet",
    "ChangeSetApproval",
    "ChangeSetStatus",
    "ConflictRecord",
    "FileApplyResult",
    "FileIntent",
    "PreparedApply",
    "RollbackReport",
]

#: The five file operations a ChangeSet may authorise (CS-001 §6.2).
FILE_OPERATION_TYPES: frozenset[str] = frozenset(
    {
        "CREATE_FILE",
        "MODIFY_FILE",
        "DELETE_FILE",
        "RENAME_FILE",
        "RUN_COMMAND",
    }
)

#: Operations that change filesystem state. RUN_COMMAND affects verification
#: status only, never file state (§8.6).
FILE_STATE_OPERATIONS: frozenset[str] = frozenset(
    {"CREATE_FILE", "MODIFY_FILE", "DELETE_FILE", "RENAME_FILE"}
)


class ApplyOutcome(StrEnum):
    """Per-file outcome of applying or rolling back one operation."""

    APPLIED = "applied"
    VERIFIED = "verified"
    SKIPPED_UNCHANGED = "skipped_unchanged"
    CONFLICT = "conflict"
    FAILED = "failed"
    ROLLED_BACK = "rolled_back"
    ROLLBACK_FAILED = "rollback_failed"


@dataclass(frozen=True, slots=True)
class FileIntent:
    """One file-level intent inside a ChangeSet.

    ``diff_ref`` references a blob in the content-addressed store
    (`services/changesets/storage.py:BlobStore`) -- diff text is never stored
    inline in the manifest.
    """

    path: str
    operation: str
    baseline_sha256: str | None = None
    baseline_size: int | None = None
    baseline_mtime_ns: int | None = None
    existed_before: bool = False
    intended_sha256: str | None = None
    intended_size: int | None = None
    diff_ref: str | None = None
    step_id: str = ""


@dataclass(frozen=True, slots=True)
class ChangeSetApproval:
    """An approval bound to a ChangeSet, its plan digest and its workspace.

    Mirroring `services/tasks/orchestrator.py:299-308`, an approval is only
    valid for the exact changeset_id + plan_digest + workspace triple it was
    granted against. Amending the ChangeSet clears it.
    """

    changeset_id: str
    plan_digest: str
    workspace_key: str
    granted_at: float
    granted_by: str = "user"


@dataclass(frozen=True, slots=True)
class ConflictRecord:
    """One preflight conflict detected before any file is written."""

    path: str
    kind: str
    expected_baseline_sha256: str | None
    observed_sha256: str | None
    observed_mtime_ns: int | None
    observed_size: int | None
    intended_sha256: str | None
    detected_at: float


@dataclass(frozen=True, slots=True)
class FileApplyResult:
    """The outcome of applying one file operation."""

    path: str
    operation: str
    outcome: ApplyOutcome
    before_sha256: str | None
    after_sha256_expected: str | None
    after_sha256_observed: str | None
    bytes_written: int = 0
    duration_ms: float = 0.0
    error: str | None = None
    journal_ref: str | None = None


@dataclass(frozen=True, slots=True)
class ApplyReport:
    """The complete report of one apply transaction."""

    changeset_id: str
    job_id: str
    started_at: float
    finished_at: float | None
    per_file: tuple[FileApplyResult, ...] = ()
    conflicts: tuple[ConflictRecord, ...] = ()
    journal_dir: str = ""
    verified: bool = False
    interrupted: bool = False

    @property
    def applied_count(self) -> int:
        return sum(1 for r in self.per_file if r.outcome is ApplyOutcome.APPLIED)

    @property
    def failed_count(self) -> int:
        return sum(1 for r in self.per_file if r.outcome is ApplyOutcome.FAILED)

    @property
    def conflict_count(self) -> int:
        return len(self.conflicts)

    @property
    def success(self) -> bool:
        return self.finished_at is not None and self.failed_count == 0 and self.conflict_count == 0


@dataclass(frozen=True, slots=True)
class RollbackReport:
    """The complete report of one rollback transaction."""

    changeset_id: str
    job_id: str
    started_at: float
    finished_at: float | None
    per_file: tuple[FileApplyResult, ...] = ()

    @property
    def rolled_back_count(self) -> int:
        return sum(1 for r in self.per_file if r.outcome is ApplyOutcome.ROLLED_BACK)

    @property
    def failed_count(self) -> int:
        return sum(1 for r in self.per_file if r.outcome is ApplyOutcome.FAILED)

    @property
    def success(self) -> bool:
        return self.finished_at is not None and self.failed_count == 0


@dataclass(frozen=True, slots=True)
class PreparedApply:
    """The result of preflight validation: intents plus any conflicts."""

    changeset_id: str
    intents: tuple[FileIntent, ...]
    conflicts: tuple[ConflictRecord, ...]
    budget_ok: bool

    @property
    def ready(self) -> bool:
        return not self.conflicts and self.budget_ok


@dataclass(frozen=True, slots=True)
class ChangeSet:
    """A persisted ChangeSet artefact derived from task evidence."""

    changeset_id: str
    task_id: str
    workspace: str
    workspace_key: str
    status: ChangeSetStatus
    intents: tuple[FileIntent, ...]
    revision: int = 1
    created_at: float = field(default_factory=lambda: datetime.now(UTC).timestamp())
    updated_at: float = field(default_factory=lambda: datetime.now(UTC).timestamp())
    approval: ChangeSetApproval | None = None
    apply_report: ApplyReport | None = None
    plan_digest: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "changeset_id": self.changeset_id,
            "task_id": self.task_id,
            "workspace": self.workspace,
            "workspace_key": self.workspace_key,
            "status": self.status.value,
            "intents": [
                {
                    "path": i.path,
                    "operation": i.operation,
                    "baseline_sha256": i.baseline_sha256,
                    "baseline_size": i.baseline_size,
                    "baseline_mtime_ns": i.baseline_mtime_ns,
                    "existed_before": i.existed_before,
                    "intended_sha256": i.intended_sha256,
                    "intended_size": i.intended_size,
                    "diff_ref": i.diff_ref,
                    "step_id": i.step_id,
                }
                for i in self.intents
            ],
            "revision": self.revision,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "approval": (
                {
                    "changeset_id": self.approval.changeset_id,
                    "plan_digest": self.approval.plan_digest,
                    "workspace_key": self.approval.workspace_key,
                    "granted_at": self.approval.granted_at,
                    "granted_by": self.approval.granted_by,
                }
                if self.approval is not None
                else None
            ),
            "apply_report": (
                {
                    "changeset_id": self.apply_report.changeset_id,
                    "job_id": self.apply_report.job_id,
                    "started_at": self.apply_report.started_at,
                    "finished_at": self.apply_report.finished_at,
                    "verified": self.apply_report.verified,
                    "interrupted": self.apply_report.interrupted,
                }
                if self.apply_report is not None
                else None
            ),
            "plan_digest": self.plan_digest,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> ChangeSet:
        intents = tuple(
            FileIntent(
                path=item["path"],
                operation=item["operation"],
                baseline_sha256=item.get("baseline_sha256"),
                baseline_size=item.get("baseline_size"),
                baseline_mtime_ns=item.get("baseline_mtime_ns"),
                existed_before=bool(item.get("existed_before", False)),
                intended_sha256=item.get("intended_sha256"),
                intended_size=item.get("intended_size"),
                diff_ref=item.get("diff_ref"),
                step_id=item.get("step_id", ""),
            )
            for item in payload.get("intents", [])
        )
        approval = None
        if payload.get("approval") is not None:
            a = payload["approval"]
            approval = ChangeSetApproval(
                changeset_id=a["changeset_id"],
                plan_digest=a["plan_digest"],
                workspace_key=a["workspace_key"],
                granted_at=a["granted_at"],
                granted_by=a.get("granted_by", "user"),
            )
        apply_report = None
        if payload.get("apply_report") is not None:
            ar = payload["apply_report"]
            apply_report = ApplyReport(
                changeset_id=ar["changeset_id"],
                job_id=ar["job_id"],
                started_at=ar["started_at"],
                finished_at=ar.get("finished_at"),
                verified=bool(ar.get("verified", False)),
                interrupted=bool(ar.get("interrupted", False)),
            )
        created_at_raw = payload.get("created_at", datetime.now(UTC).timestamp())
        if isinstance(created_at_raw, str):
            created_at = datetime.fromisoformat(created_at_raw.replace("Z", "+00:00")).timestamp()
        else:
            created_at = float(created_at_raw)
        updated_at_raw = payload.get("updated_at", datetime.now(UTC).timestamp())
        if isinstance(updated_at_raw, str):
            updated_at = datetime.fromisoformat(updated_at_raw.replace("Z", "+00:00")).timestamp()
        else:
            updated_at = float(updated_at_raw)
        return cls(
            changeset_id=payload["changeset_id"],
            task_id=payload["task_id"],
            workspace=payload["workspace"],
            workspace_key=payload.get("workspace_key", ""),
            status=ChangeSetStatus(payload["status"]),
            intents=intents,
            revision=int(payload.get("revision", 1)),
            created_at=created_at,
            updated_at=updated_at,
            approval=approval,
            apply_report=apply_report,
            plan_digest=str(payload.get("plan_digest", "")),
        )

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL_STATES

    @property
    def is_unstable(self) -> bool:
        return self.status in UNSTABLE_STATES

    @property
    def has_file_evidence(self) -> bool:
        # §4.3: file evidence only exists in these states.
        return self.status in {
            ChangeSetStatus.APPLIED,
            ChangeSetStatus.VERIFIED,
            ChangeSetStatus.FAILED,
            ChangeSetStatus.ROLLED_BACK,
            ChangeSetStatus.ROLLBACK_FAILED,
            ChangeSetStatus.PARTIALLY_APPLIED,
        }
