"""Transactional ChangeSet rollback.

Rollback is the inverse of apply, driven purely by the journal: for each
``applied`` file the recorded pre-image bytes are restored (or absence restored
for a creation), and each restoration is re-verified against the recorded
digest -- the same rule as `revisions.py:25-28`, now persisted.

Nothing here imports Qt.
"""

from __future__ import annotations

import hashlib
import time
from pathlib import Path

from quantsmind_agent.errors import QuantsMindError
from quantsmind_agent.security.audit import AuditEventType, SecurityAuditLog
from quantsmind_agent.security.permissions import (
    Decision,
    Permission,
    PermissionBroker,
    PermissionRequest,
)
from quantsmind_agent.services.changesets.models import (
    ApplyOutcome,
    FileApplyResult,
    RollbackReport,
)
from quantsmind_agent.services.changesets.storage import (
    BlobStore,
    ChangeSetStore,
)
from quantsmind_agent.services.workspace.containment import Workspace

__all__ = [
    "ReverseApplier",
]


def _now() -> float:
    return time.time()


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return f"sha256:{h.hexdigest()}"


class ReverseApplier:
    """Roll back an applied ChangeSet by restoring pre-images from the journal.

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
    """

    def __init__(
        self,
        *,
        workspace: Workspace,
        store: ChangeSetStore,
        permissions: PermissionBroker,
        audit: SecurityAuditLog,
    ) -> None:
        self._workspace = workspace
        self._store = store
        self._permissions = permissions
        self._audit = audit

    # -- rollback ----------------------------------------------------------
    def rollback(self, changeset_id: str, workspace_key: str) -> RollbackReport:
        """Roll back every applied file in the ChangeSet.

        Refuses unless the ChangeSet is in a state with file evidence
        (``APPLIED``, ``VERIFIED``, ``PARTIALLY_APPLIED``, ``FAILED`` with
        evidence). Restores each applied file's pre-image bytes, or restores
        absence for a creation, and verifies each restoration against the
        recorded digest. A mismatch leaves the file as-is and reports
        ``ROLLBACK_FAILED`` for that file.
        """

        stored = self._store.stored(workspace_key, changeset_id)
        manifest = stored.manifest
        if manifest is None:
            raise QuantsMindError(
                f"changeset {changeset_id} has no manifest",
                details={
                    "code": "changeset_not_found",
                    "changeset_id": changeset_id,
                },
            )
        from quantsmind_agent.services.changesets.models import ChangeSet

        change_set = ChangeSet.from_dict(manifest)

        if not change_set.has_file_evidence:
            raise QuantsMindError(
                f"changeset {changeset_id} is {change_set.status.value}; rollback "
                "requires file evidence",
                details={
                    "code": "changeset_not_rollbackable",
                    "changeset_id": changeset_id,
                    "status": change_set.status.value,
                },
            )

        # Permission gate.
        request = PermissionRequest(
            permission=Permission.CHANGESET_ROLLBACK,
            tool="changeset.rollback",
            target=workspace_key,
            summary=f"rollback changeset {changeset_id}",
        )
        if self._permissions.evaluate(request) is not Decision.ALLOW:
            self._audit.record(
                AuditEventType.CHANGESET_ROLLED_BACK,
                decision="denied",
                reason_code="permission_not_granted",
                detail=changeset_id,
            )
            raise QuantsMindError(
                f"permission CHANGESET_ROLLBACK not granted for changeset {changeset_id}",
                details={
                    "code": "changeset_rollback_denied",
                    "changeset_id": changeset_id,
                },
            )

        paths = self._store.paths_for(workspace_key, changeset_id)
        journal = self._store.journal(paths)
        scan = journal.scan()

        started_at = _now()
        results: list[FileApplyResult] = []

        # Walk the journal: for each op_committed, restore the pre-image
        # captured in the matching op_started.
        committed: dict[str, dict[str, object]] = {}
        started_ops: dict[str, dict[str, object]] = {}
        for record in scan.records:
            event = record.get("event")
            op_id = str(record.get("op_id", ""))
            if event == "op_started":
                started_ops[op_id] = record
            elif event == "op_committed":
                committed[op_id] = record

        for op_id in reversed(list(committed.keys())):
            started_record = started_ops.get(op_id, {})
            base = started_record.get("base", {})
            if not isinstance(base, dict):
                base = {}
            path = str(started_record.get("path", op_id))
            intended_sha = str(committed[op_id].get("actual_sha256", ""))

            try:
                result = self._rollback_one(path, base, intended_sha)
                results.append(result)
            except QuantsMindError as exc:
                results.append(
                    FileApplyResult(
                        path=path,
                        operation="MODIFY_FILE",
                        outcome=ApplyOutcome.ROLLBACK_FAILED,
                        before_sha256=None,
                        after_sha256_expected=None,
                        after_sha256_observed=None,
                        error=exc.message,
                    )
                )

        finished_at = _now()
        report = RollbackReport(
            changeset_id=changeset_id,
            job_id=f"rollback-{changeset_id}",
            started_at=started_at,
            finished_at=finished_at,
            per_file=tuple(results),
        )

        self._audit.record(
            AuditEventType.CHANGESET_ROLLED_BACK,
            decision="rolled_back" if report.success else "rollback_failed",
            reason_code="ok" if report.success else "rollback_failed",
            detail=changeset_id,
        )

        return report

    # -- one file ----------------------------------------------------------
    def _rollback_one(
        self, path: str, base: dict[str, object], intended_sha: str
    ) -> FileApplyResult:
        """Restore one file's pre-image (or absence) and verify the digest."""

        try:
            target = self._workspace.resolve(path)
            existed_before = bool(base)

            if not existed_before:
                # Creation: restore absence (delete the file if present).
                if target is not None and target.exists():
                    target.unlink()
                after_observed = None
            else:
                # Modification/delete: restore pre-image bytes from the blob store.
                pre_image_sha = str(base.get("sha256", ""))
                if not pre_image_sha:
                    raise QuantsMindError(
                        f"pre-image digest missing for {path}",
                        details={"code": "rollback_preimage_missing", "path": path},
                    )
                blob = BlobStore(self._store.blobs.root)
                if not blob.exists(pre_image_sha):
                    raise QuantsMindError(
                        f"pre-image {pre_image_sha} not found in blob store",
                        details={"code": "rollback_preimage_missing", "path": path},
                    )
                data = blob.read(pre_image_sha)

                self._workspace.write_atomic(path, data, expected_sha256=pre_image_sha)
                written = self._workspace.resolve(path)
                after_observed = (
                    _sha256_file(written) if written is not None and written.exists() else None
                )

            # Verify against the recorded result digest (the "after" hash from
            # apply). For a creation, the file should no longer exist, so there
            # is no digest to compare.
            if existed_before and intended_sha and after_observed != intended_sha:
                raise QuantsMindError(
                    f"post-rollback digest mismatch for {path}: expected {intended_sha}, "
                    f"observed {after_observed}",
                    details={"code": "rollback_digest_mismatch", "path": path},
                )

            return FileApplyResult(
                path=path,
                operation="MODIFY_FILE",
                outcome=ApplyOutcome.ROLLED_BACK,
                before_sha256=after_observed,
                after_sha256_expected=intended_sha or None,
                after_sha256_observed=after_observed,
            )
        except QuantsMindError as exc:
            return FileApplyResult(
                path=path,
                operation="MODIFY_FILE",
                outcome=ApplyOutcome.FAILED,
                before_sha256=None,
                after_sha256_expected=intended_sha or None,
                after_sha256_observed=None,
                error=exc.message,
            )
        except OSError as exc:
            return FileApplyResult(
                path=path,
                operation="MODIFY_FILE",
                outcome=ApplyOutcome.FAILED,
                before_sha256=None,
                after_sha256_expected=intended_sha or None,
                after_sha256_observed=None,
                error=str(exc),
            )
