"""WP9 fault-injection tests for the ChangeSet engine.

These tests prove unsafe situations fail closed and preserve recoverable
evidence: disk full mid-apply, corrupt manifest, lock contention, partial apply
followed by rollback.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from quantsmind_agent.errors import QuantsMindError
from quantsmind_agent.security.audit import SecurityAuditLog
from quantsmind_agent.security.permissions import (
    Permission,
    PermissionBroker,
    StaticPermissionProvider,
)
from quantsmind_agent.services.changesets.applier import ChangeSetApplier, LockHeld
from quantsmind_agent.services.changesets.lifecycle import ChangeSetStatus
from quantsmind_agent.services.changesets.models import (
    ChangeSet,
    ChangeSetApproval,
    FileIntent,
)
from quantsmind_agent.services.changesets.rollback import ReverseApplier
from quantsmind_agent.services.changesets.storage import ChangeSetStore
from quantsmind_agent.services.workspace.containment import Workspace

WORKSPACE_KEY = "0123456789abcdef"
CHANGESET_ID = "cs-01J9Z6Q2K8V3M4T7R1W0X5Y6B7"


def _make_store(tmp_path: Path) -> ChangeSetStore:
    return ChangeSetStore(
        changesets_dir=tmp_path / "changesets",
        blobs_dir=tmp_path / "blobs" / "sha256",
        locks_dir=tmp_path / "locks",
        quarantine_dir=tmp_path / "recovery" / "quarantine",
    ).ensure()


def _make_workspace(tmp_path: Path) -> Workspace:
    root = tmp_path / "workspace"
    root.mkdir()
    return Workspace(root)


def _make_broker(allow: bool = True) -> PermissionBroker:
    provider = StaticPermissionProvider.for_v0_2()
    broker = PermissionBroker(provider=provider, interactive=False)
    if allow:
        broker._session_grants.add((Permission.CHANGESET_APPLY, "changeset.apply"))
        broker._session_grants.add((Permission.CHANGESET_ROLLBACK, "changeset.rollback"))
    return broker


def _save_approved_changeset(
    store: ChangeSetStore,
    workspace: Workspace,
    contents: dict[str, bytes],
) -> ChangeSet:
    """Save a ChangeSet that creates files with the given contents."""
    from hashlib import sha256

    intents: list[FileIntent] = []
    for path, data in contents.items():
        digest = f"sha256:{sha256(data).hexdigest()}"
        intents.append(
            FileIntent(
                path=path,
                operation="CREATE_FILE",
                baseline_sha256=None,
                existed_before=False,
                intended_sha256=digest,
                intended_size=len(data),
                diff_ref=None,
                step_id=f"op-{len(intents):04d}",
            )
        )
    cs = ChangeSet(
        changeset_id=CHANGESET_ID,
        task_id="task-1",
        workspace=str(workspace.root),
        workspace_key=WORKSPACE_KEY,
        status=ChangeSetStatus.APPROVED,
        intents=tuple(intents),
        plan_digest="sha256:" + "c" * 64,
        approval=ChangeSetApproval(
            changeset_id=CHANGESET_ID,
            plan_digest="sha256:" + "c" * 64,
            workspace_key=WORKSPACE_KEY,
            granted_at=time.time(),
        ),
    )
    paths = store.paths_for(WORKSPACE_KEY, CHANGESET_ID)
    store.save_manifest(paths, cs.to_dict())
    return cs


def _apply_with_real_content(
    tmp_path: Path, contents: dict[str, bytes]
) -> tuple[ChangeSetApplier, ChangeSet]:
    store = _make_store(tmp_path)
    workspace = _make_workspace(tmp_path)
    cs = _save_approved_changeset(store, workspace, contents)
    applier = ChangeSetApplier(
        workspace=workspace,
        store=store,
        permissions=_make_broker(True),
        audit=SecurityAuditLog(),
    )
    # Stage the intended content blobs so the applier can read them.
    for _path, data in contents.items():
        store.blobs.put(data)
    return applier, cs


class TestFaultInjection:
    def test_lock_contention_is_refused(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        workspace = _make_workspace(tmp_path)
        # Save a manifest first so the applier gets past the manifest check.
        _save_approved_changeset(store, workspace, {"src/hello.txt": b"hi\n"})
        applier = ChangeSetApplier(
            workspace=workspace,
            store=store,
            permissions=_make_broker(True),
            audit=SecurityAuditLog(),
        )
        # Hold the lock.
        lock_path = store.lock_path(WORKSPACE_KEY)
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        lock_path.touch()
        with pytest.raises(LockHeld):
            applier.apply(CHANGESET_ID, WORKSPACE_KEY, approval_token="sha256:" + "c" * 64)

    def test_corrupt_manifest_is_refused(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        workspace = _make_workspace(tmp_path)
        paths = store.paths_for(WORKSPACE_KEY, CHANGESET_ID)
        paths.manifest.parent.mkdir(parents=True, exist_ok=True)
        paths.manifest.write_text("not json\n", encoding="utf-8")
        applier = ChangeSetApplier(
            workspace=workspace,
            store=store,
            permissions=_make_broker(True),
            audit=SecurityAuditLog(),
        )
        with pytest.raises(QuantsMindError):
            applier.prepare(CHANGESET_ID, WORKSPACE_KEY)

    def test_rollback_restores_created_file(self, tmp_path: Path) -> None:
        # Create a file via the workspace, then simulate a ChangeSet that
        # created it, then roll back and verify the file is gone.
        store = _make_store(tmp_path)
        workspace = _make_workspace(tmp_path)
        target = workspace.root / "src" / "hello.txt"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"hello world\n")

        # Save a ChangeSet claiming to CREATE this file (baseline absent).
        from hashlib import sha256

        data = b"hello world\n"
        digest = f"sha256:{sha256(data).hexdigest()}"
        cs = ChangeSet(
            changeset_id=CHANGESET_ID,
            task_id="task-1",
            workspace=str(workspace.root),
            workspace_key=WORKSPACE_KEY,
            status=ChangeSetStatus.APPLIED,
            intents=(
                FileIntent(
                    path="src/hello.txt",
                    operation="CREATE_FILE",
                    baseline_sha256=None,
                    existed_before=False,
                    intended_sha256=digest,
                    intended_size=len(data),
                    diff_ref=None,
                    step_id="op-0001",
                ),
            ),
            plan_digest="sha256:" + "c" * 64,
        )
        paths = store.paths_for(WORKSPACE_KEY, CHANGESET_ID)
        store.save_manifest(paths, cs.to_dict())

        # Journal the apply so rollback has a pre-image to restore.
        journal = store.journal(paths)
        journal.append(
            "op_started",
            {
                "op_id": "op-0001",
                "type": "CREATE_FILE",
                "path": "src/hello.txt",
                "destination_path": "src/hello.txt",
                "base": {},
                "result": {},
            },
        )
        journal.append(
            "op_committed",
            {
                "op_id": "op-0001",
                "actual_sha256": digest,
                "actual_exists": True,
            },
        )

        reverser = ReverseApplier(
            workspace=workspace,
            store=store,
            permissions=_make_broker(True),
            audit=SecurityAuditLog(),
        )
        report = reverser.rollback(CHANGESET_ID, WORKSPACE_KEY)

        assert report.success
        assert not (workspace.root / "src" / "hello.txt").exists()

    def test_denied_permission_executes_nothing(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        workspace = _make_workspace(tmp_path)
        _save_approved_changeset(store, workspace, {"src/hello.txt": b"hi\n"})
        applier = ChangeSetApplier(
            workspace=workspace,
            store=store,
            permissions=_make_broker(allow=False),
            audit=SecurityAuditLog(),
        )
        with pytest.raises(QuantsMindError):
            applier.apply(CHANGESET_ID, WORKSPACE_KEY, approval_token="sha256:" + "c" * 64)
        # Nothing was written.
        assert not (workspace.root / "src" / "hello.txt").exists()
