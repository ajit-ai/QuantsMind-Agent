"""Transactional ChangeSet application.

The applier turns an approved ChangeSet into filesystem changes as a prepared,
journaled, verified transaction. Pre-images are captured and fsync'd before any
write; each write is verified against its intended digest; the journal is the
sole authority on what happened if the process crashes mid-apply.

Nothing here imports Qt. The applier runs on a service worker, never on the GUI
thread.
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import threading
import time
from pathlib import Path

from quantsmind_agent.diagnostics.governance import ResourceAction, ResourceGovernor
from quantsmind_agent.errors import QuantsMindError
from quantsmind_agent.security.audit import AuditEventType, SecurityAuditLog
from quantsmind_agent.security.permissions import (
    Decision,
    Permission,
    PermissionBroker,
    PermissionRequest,
)
from quantsmind_agent.services.changesets.lifecycle import ChangeSetStatus
from quantsmind_agent.services.changesets.models import (
    FILE_STATE_OPERATIONS,
    ApplyOutcome,
    ApplyReport,
    ChangeSet,
    ConflictRecord,
    FileApplyResult,
    FileIntent,
    PreparedApply,
)
from quantsmind_agent.services.changesets.storage import BlobStore, ChangeSetStore, Journal
from quantsmind_agent.services.workspace.containment import Workspace

__all__ = [
    "ChangeSetApplier",
    "LockHeld",
]

#: Per-file content cap (CS-001 §8.1, WP1 §1.8).
MAX_CONTENT_BYTES = 20 * 1024 * 1024

#: Maximum files per ChangeSet (WP1 §1.8).
MAX_FILES_PER_CHANGESET = 500

#: Maximum pre-image bytes per ChangeSet (WP1 §1.8).
MAX_PRE_IMAGE_BYTES = 64 * 1024 * 1024


class LockHeld(QuantsMindError):
    """Raised when the single-applier lease is already held for a workspace."""

    def __init__(self, workspace_key: str) -> None:
        super().__init__(
            f"an apply transaction is already in progress for workspace {workspace_key}",
            details={"code": "changeset_apply_lock_held", "workspace_key": workspace_key},
        )
        self.workspace_key = workspace_key


def _now() -> float:
    return time.time()


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return f"sha256:{h.hexdigest()}"


class ChangeSetApplier:
    """Apply an approved ChangeSet as a journaled, verified transaction.

    Parameters
    ----------
    workspace:
        The opened :class:`~quantsmind_agent.services.workspace.containment.Workspace`.
    store:
        The :class:`~quantsmind_agent.services.changesets.storage.ChangeSetStore`.
    permissions:
        The :class:`~quantsmind_agent.security.permissions.PermissionBroker`.
    audit:
        The :class:`~quantsmind_agent.security.audit.SecurityAuditLog`.
    governor:
        Optional resource governor for apply.
    cancel_event:
        Optional :class:`threading.Event`; once set after the first write, the
        applier stops after the current file and reports ``PARTIALLY_APPLIED``.
    """

    def __init__(
        self,
        *,
        workspace: Workspace,
        store: ChangeSetStore,
        permissions: PermissionBroker,
        audit: SecurityAuditLog,
        governor: ResourceGovernor | None = None,
        cancel_event: threading.Event | None = None,
    ) -> None:
        self._workspace = workspace
        self._store = store
        self._permissions = permissions
        self._audit = audit
        self._governor = governor
        self._cancel = cancel_event
        self._applying = False
        self._lease_path: Path | None = None

    @property
    def applying(self) -> bool:
        return self._applying

    # -- prepare -----------------------------------------------------------
    def prepare(self, changeset_id: str, workspace_key: str) -> PreparedApply:
        """Run preflight validation without touching the workspace.

        Returns intents plus any conflicts. If conflicts are non-empty the
        caller must transition the ChangeSet to ``CONFLICTED`` and must not
        call :meth:`apply`.
        """

        stored = self._store.stored(workspace_key, changeset_id)
        manifest = stored.manifest
        if manifest is None:
            raise QuantsMindError(
                f"changeset {changeset_id} has no manifest",
                details={"code": "changeset_not_found", "changeset_id": changeset_id},
            )
        change_set = ChangeSet.from_dict(manifest)

        if change_set.status not in {
            ChangeSetStatus.APPROVED,
            ChangeSetStatus.PROPOSED,
            ChangeSetStatus.CONFLICTED,
        }:
            raise QuantsMindError(
                f"changeset {changeset_id} is {change_set.status.value}; "
                "only APPROVED/PROPOSED/CONFLICTED may be applied",
                details={
                    "code": "changeset_not_applyable",
                    "changeset_id": changeset_id,
                    "status": change_set.status.value,
                },
            )

        if change_set.approval is None:
            raise QuantsMindError(
                f"changeset {changeset_id} has no approval; approve it before applying",
                details={
                    "code": "changeset_not_approved",
                    "changeset_id": changeset_id,
                },
            )

        if len(change_set.intents) > MAX_FILES_PER_CHANGESET:
            raise QuantsMindError(
                f"changeset {changeset_id} has {len(change_set.intents)} intents; "
                f"limit is {MAX_FILES_PER_CHANGESET}",
                details={
                    "code": "changeset_too_large",
                    "changeset_id": changeset_id,
                    "count": len(change_set.intents),
                    "limit": MAX_FILES_PER_CHANGESET,
                },
            )

        estimated = sum(
            (i.intended_size or 0) for i in change_set.intents if i.intended_size is not None
        )
        if self._governor is not None:
            decision = self._governor.decide(
                ResourceAction.CHANGESET_APPLY, estimated_bytes=estimated
            )
            if not decision.allowed:
                raise QuantsMindError(
                    f"resource governance refused apply: {decision.reason}",
                    details={
                        "code": "changeset_apply_refused",
                        "changeset_id": changeset_id,
                        "reason": decision.reason,
                    },
                )

        conflicts = self._validate_intents(change_set)
        budget_ok = self._budget_ok(change_set)

        return PreparedApply(
            changeset_id=changeset_id,
            intents=change_set.intents,
            conflicts=tuple(conflicts),
            budget_ok=budget_ok,
        )

    # -- apply -------------------------------------------------------------
    def apply(self, changeset_id: str, workspace_key: str, *, approval_token: str) -> ApplyReport:
        """Apply the ChangeSet as a journaled transaction.

        ``approval_token`` is the plan digest the approval was granted against;
        it must match the stored approval's plan digest and workspace.
        """

        stored = self._store.stored(workspace_key, changeset_id)
        manifest = stored.manifest
        if manifest is None:
            raise QuantsMindError(
                f"changeset {changeset_id} has no manifest",
                details={"code": "changeset_not_found", "changeset_id": changeset_id},
            )
        change_set = ChangeSet.from_dict(manifest)

        # Approval binding: changeset_id + plan_digest + workspace.
        if change_set.approval is None:
            raise QuantsMindError(
                f"changeset {changeset_id} has no approval",
                details={"code": "changeset_not_approved", "changeset_id": changeset_id},
            )
        if change_set.approval.changeset_id != changeset_id:
            raise QuantsMindError(
                f"approval changeset_id {change_set.approval.changeset_id} != {changeset_id}",
                details={
                    "code": "changeset_approval_mismatch",
                    "changeset_id": changeset_id,
                },
            )
        if change_set.approval.plan_digest != change_set.plan_digest:
            raise QuantsMindError(
                f"approval plan digest does not match changeset {changeset_id} plan digest",
                details={
                    "code": "changeset_digest_mismatch",
                    "changeset_id": changeset_id,
                },
            )
        if change_set.approval.workspace_key != workspace_key:
            raise QuantsMindError(
                f"approval workspace {change_set.approval.workspace_key} != {workspace_key}",
                details={
                    "code": "changeset_workspace_mismatch",
                    "changeset_id": changeset_id,
                },
            )
        if approval_token != change_set.approval.plan_digest:
            raise QuantsMindError(
                "the approval token does not match the stored plan digest",
                details={
                    "code": "changeset_approval_token_mismatch",
                    "changeset_id": changeset_id,
                },
            )

        # Single-applier lease.
        self._lease_path = self._store.lock_path(workspace_key)
        try:
            fd = os.open(str(self._lease_path), os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
            os.close(fd)
        except FileExistsError as exc:
            raise LockHeld(workspace_key) from exc
        try:
            return self._apply_locked(changeset_id, workspace_key, change_set)
        finally:
            if self._lease_path is not None:
                with contextlib.suppress(OSError):
                    os.unlink(str(self._lease_path))
                self._lease_path = None

    # -- cancel ------------------------------------------------------------
    def request_cancel(self) -> bool:
        """Request cancellation.

        Returns ``False`` once the first write has begun. Otherwise sets the
        cancel event so the applier stops after the current file.
        """

        if self._applying:
            return False
        if self._cancel is not None:
            self._cancel.set()
        return True

    # -- internals ---------------------------------------------------------
    def _apply_locked(
        self, changeset_id: str, workspace_key: str, change_set: ChangeSet
    ) -> ApplyReport:
        # Permission gate: CHANGESET_APPLY is ASK-by-default and never
        # session-grantable (same rule as delete/undo).
        request = PermissionRequest(
            permission=Permission.CHANGESET_APPLY,
            tool="changeset.apply",
            target=workspace_key,
            summary=f"apply changeset {changeset_id}",
        )
        if self._permissions.evaluate(request) is not Decision.ALLOW:
            self._audit.record(
                AuditEventType.CHANGESET_APPLY_DENIED,
                decision="denied",
                reason_code="permission_not_granted",
                detail=changeset_id,
            )
            raise QuantsMindError(
                f"permission CHANGESET_APPLY not granted for changeset {changeset_id}",
                details={
                    "code": "changeset_apply_denied",
                    "changeset_id": changeset_id,
                },
            )

        # Re-validate intents against current workspace state (re-fingerprint).
        conflicts = self._validate_intents(change_set)
        if conflicts:
            return ApplyReport(
                changeset_id=changeset_id,
                job_id=f"job-{changeset_id}",
                started_at=_now(),
                finished_at=_now(),
                conflicts=tuple(conflicts),
                journal_dir=str(self._store.changesets_dir / workspace_key / changeset_id),
                verified=False,
                interrupted=False,
            )

        self._applying = True
        started = _now()
        results: list[FileApplyResult] = []
        journal_dir = str(self._store.changesets_dir / workspace_key / changeset_id)
        paths = self._store.paths_for(workspace_key, changeset_id)
        journal = self._store.journal(paths)

        try:
            # Capture all pre-images first (§11.3).
            for intent in change_set.intents:
                if intent.operation not in FILE_STATE_OPERATIONS:
                    continue
                self._capture_pre_image(intent, journal)

            # Apply each file in deterministic order.
            for intent in change_set.intents:
                if self._cancel is not None and self._cancel.is_set() and results:
                    break
                result = self._apply_one(intent, journal)
                results.append(result)

            failed = [r for r in results if r.outcome is ApplyOutcome.FAILED]
            finished = _now()
            expected_ops = [i for i in change_set.intents if i.operation in FILE_STATE_OPERATIONS]
            report = ApplyReport(
                changeset_id=changeset_id,
                job_id=f"job-{changeset_id}",
                started_at=started,
                finished_at=finished,
                per_file=tuple(results),
                conflicts=(),
                journal_dir=journal_dir,
                verified=not failed and len(results) == len(expected_ops),
                interrupted=bool(self._cancel is not None and self._cancel.is_set()),
            )

            self._audit.record(
                AuditEventType.CHANGESET_APPLIED
                if report.success
                else AuditEventType.CHANGESET_APPLY_FAILED,
                decision="applied" if report.success else "failed",
                reason_code="ok" if report.success else "apply_failed",
                detail=changeset_id,
            )

            return report
        finally:
            self._applying = False

    # -- one file ----------------------------------------------------------
    def _apply_one(self, intent: FileIntent, journal: Journal) -> FileApplyResult:
        started = _now()
        path = intent.path
        before_sha: str | None = None

        try:
            target = self._workspace.resolve(path)
            existed = target is not None and target.exists()
            if existed:
                before_sha = _sha256_file(target)

            # Baseline check: current fingerprint must match recorded baseline.
            if (
                existed
                and intent.baseline_sha256 is not None
                and before_sha != intent.baseline_sha256
            ):
                return FileApplyResult(
                    path=path,
                    operation=intent.operation,
                    outcome=ApplyOutcome.CONFLICT,
                    before_sha256=before_sha,
                    after_sha256_expected=intent.intended_sha256,
                    after_sha256_observed=None,
                    error="baseline mismatch: current file does not match recorded baseline",
                )

            data = self._intended_bytes(intent)
            if data is None:
                return FileApplyResult(
                    path=path,
                    operation=intent.operation,
                    outcome=ApplyOutcome.FAILED,
                    before_sha256=before_sha,
                    after_sha256_expected=intent.intended_sha256,
                    after_sha256_observed=None,
                    error="no intended content available for operation",
                )

            self._workspace.write_atomic(path, data, expected_sha256=intent.intended_sha256)

            written = self._workspace.resolve(path)
            after_observed = (
                _sha256_file(written) if written is not None and written.exists() else None
            )
            outcome = (
                ApplyOutcome.APPLIED
                if after_observed == intent.intended_sha256
                else ApplyOutcome.FAILED
            )
            error = None if outcome is ApplyOutcome.APPLIED else "post-write digest mismatch"

            journal.append(
                "op_committed",
                {
                    "op_id": intent.step_id or path,
                    "actual_sha256": after_observed or "",
                    "actual_exists": True,
                },
            )

            return FileApplyResult(
                path=path,
                operation=intent.operation,
                outcome=outcome,
                before_sha256=before_sha,
                after_sha256_expected=intent.intended_sha256,
                after_sha256_observed=after_observed,
                bytes_written=len(data),
                duration_ms=(_now() - started) * 1000.0,
                error=error,
                journal_ref=str(path),
            )
        except QuantsMindError as exc:
            return FileApplyResult(
                path=path,
                operation=intent.operation,
                outcome=ApplyOutcome.FAILED,
                before_sha256=before_sha,
                after_sha256_expected=intent.intended_sha256,
                after_sha256_observed=None,
                bytes_written=0,
                duration_ms=(_now() - started) * 1000.0,
                error=exc.message,
            )
        except OSError as exc:
            return FileApplyResult(
                path=path,
                operation=intent.operation,
                outcome=ApplyOutcome.FAILED,
                before_sha256=before_sha,
                after_sha256_expected=intent.intended_sha256,
                after_sha256_observed=None,
                bytes_written=0,
                duration_ms=(_now() - started) * 1000.0,
                error=str(exc),
            )

    # -- helpers -----------------------------------------------------------
    def _validate_intents(self, change_set: ChangeSet) -> list[ConflictRecord]:
        from quantsmind_agent.errors import InvalidPathError

        conflicts: list[ConflictRecord] = []
        now = _now()
        for intent in change_set.intents:
            if intent.operation not in FILE_STATE_OPERATIONS:
                continue
            try:
                current = self._workspace.resolve(intent.path)
            except InvalidPathError:
                conflicts.append(
                    ConflictRecord(
                        path=intent.path,
                        kind="outside_scope",
                        expected_baseline_sha256=intent.baseline_sha256,
                        observed_sha256=None,
                        observed_mtime_ns=None,
                        observed_size=None,
                        intended_sha256=intent.intended_sha256,
                        detected_at=now,
                    )
                )
                continue
            if current.exists() and intent.operation == "CREATE_FILE":
                conflicts.append(
                    ConflictRecord(
                        path=intent.path,
                        kind="file_appeared",
                        expected_baseline_sha256=None,
                        observed_sha256=_sha256_file(current),
                        observed_mtime_ns=current.stat().st_mtime_ns,
                        observed_size=current.stat().st_size,
                        intended_sha256=intent.intended_sha256,
                        detected_at=now,
                    )
                )
            elif not current.exists() and intent.operation in {
                "MODIFY_FILE",
                "DELETE_FILE",
                "RENAME_FILE",
            }:
                conflicts.append(
                    ConflictRecord(
                        path=intent.path,
                        kind="file_missing",
                        expected_baseline_sha256=intent.baseline_sha256,
                        observed_sha256=None,
                        observed_mtime_ns=None,
                        observed_size=None,
                        intended_sha256=intent.intended_sha256,
                        detected_at=now,
                    )
                )
        return conflicts

    def _budget_ok(self, change_set: ChangeSet) -> bool:
        total = sum(
            (i.intended_size or 0) for i in change_set.intents if i.intended_size is not None
        )
        return total <= MAX_PRE_IMAGE_BYTES

    def _capture_pre_image(self, intent: FileIntent, journal: Journal) -> int:
        target = self._workspace.resolve(intent.path)
        if target is None or not target.exists():
            return journal.append(
                "op_started",
                {
                    "op_id": intent.step_id or intent.path,
                    "type": intent.operation,
                    "path": intent.path,
                    "destination_path": intent.path,
                    "base": {},
                    "result": {},
                },
            )
        digest = _sha256_file(target)
        return journal.append(
            "op_started",
            {
                "op_id": intent.step_id or intent.path,
                "type": intent.operation,
                "path": intent.path,
                "destination_path": intent.path,
                "base": {"sha256": digest, "size": target.stat().st_size},
                "result": {},
            },
        )

    def _intended_bytes(self, intent: FileIntent) -> bytes | None:
        if intent.operation == "DELETE_FILE":
            return b""
        if intent.diff_ref is not None:
            blob = BlobStore(self._store.blobs.root)
            if blob.exists(intent.diff_ref):
                return blob.read(intent.diff_ref)
        return None
