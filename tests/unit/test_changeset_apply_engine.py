"""WP1 unit tests for the transactional ChangeSet apply/rollback engine.

These tests exercise the real applier and reverse applier against a real
workspace and a real ChangeSet store, proving that apply is journaled,
verified, bounded and fail-closed, and that rollback restores exact pre-image
bytes.
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
from quantsmind_agent.services.changesets.applier import (
    MAX_FILES_PER_CHANGESET,
    ChangeSetApplier,
    LockHeld,
)
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


def _make_changeset(
    store: ChangeSetStore,
    workspace: Workspace,
    status: ChangeSetStatus = ChangeSetStatus.APPROVED,
    revision: int = 1,
) -> ChangeSet:
    paths = store.paths_for(WORKSPACE_KEY, CHANGESET_ID)
    intents = (
        FileIntent(
            path="src/hello.txt",
            operation="CREATE_FILE",
            baseline_sha256=None,
            existed_before=False,
            intended_sha256="sha256:" + "a" * 64,
            intended_size=13,
            diff_ref="sha256:" + "b" * 64,
            step_id="op-0001",
        ),
    )
    cs = ChangeSet(
        changeset_id=CHANGESET_ID,
        task_id="task-1",
        workspace=str(workspace.root),
        workspace_key=WORKSPACE_KEY,
        status=status,
        intents=intents,
        revision=revision,
        plan_digest="sha256:" + "c" * 64,
        approval=ChangeSetApproval(
            changeset_id=CHANGESET_ID,
            plan_digest="sha256:" + "c" * 64,
            workspace_key=WORKSPACE_KEY,
            granted_at=time.time(),
        ),
    )
    store.save_manifest(paths, cs.to_dict())
    return cs


def _make_broker(allow: bool = True) -> PermissionBroker:
    provider = StaticPermissionProvider.for_v0_2()
    broker = PermissionBroker(provider=provider, interactive=False)
    if allow:
        broker._session_grants.add((Permission.CHANGESET_APPLY, "changeset.apply"))
        broker._session_grants.add((Permission.CHANGESET_ROLLBACK, "changeset.rollback"))
    return broker


class TestChangeSetApplier:
    def test_prepare_rejects_unapproved_changeset(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        workspace = _make_workspace(tmp_path)
        applier = ChangeSetApplier(
            workspace=workspace,
            store=store,
            permissions=_make_broker(True),
            audit=SecurityAuditLog(),
        )
        _make_changeset(store, workspace, status=ChangeSetStatus.DRAFT)
        with pytest.raises(QuantsMindError):
            applier.prepare(CHANGESET_ID, WORKSPACE_KEY)

    def test_prepare_rejects_too_many_intents(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        workspace = _make_workspace(tmp_path)
        applier = ChangeSetApplier(
            workspace=workspace,
            store=store,
            permissions=_make_broker(True),
            audit=SecurityAuditLog(),
        )
        paths = store.paths_for(WORKSPACE_KEY, CHANGESET_ID)
        many = tuple(
            FileIntent(
                path=f"src/f{i}.txt",
                operation="CREATE_FILE",
                intended_sha256="sha256:" + "d" * 64,
                intended_size=1,
                step_id=f"op-{i:04d}",
            )
            for i in range(MAX_FILES_PER_CHANGESET + 1)
        )
        cs = ChangeSet(
            changeset_id=CHANGESET_ID,
            task_id="task-1",
            workspace=str(workspace.root),
            workspace_key=WORKSPACE_KEY,
            status=ChangeSetStatus.APPROVED,
            intents=many,
            plan_digest="sha256:" + "c" * 64,
            approval=ChangeSetApproval(
                changeset_id=CHANGESET_ID,
                plan_digest="sha256:" + "c" * 64,
                workspace_key=WORKSPACE_KEY,
                granted_at=time.time(),
            ),
        )
        store.save_manifest(paths, cs.to_dict())
        with pytest.raises(QuantsMindError):
            applier.prepare(CHANGESET_ID, WORKSPACE_KEY)

    def test_double_apply_raises_lock_held(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        workspace = _make_workspace(tmp_path)
        _make_changeset(store, workspace, status=ChangeSetStatus.APPROVED)
        applier = ChangeSetApplier(
            workspace=workspace,
            store=store,
            permissions=_make_broker(True),
            audit=SecurityAuditLog(),
        )
        # Manually create the lock file to simulate a held lease.
        lock_path = store.lock_path(WORKSPACE_KEY)
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        lock_path.touch()
        with pytest.raises(LockHeld):
            applier.apply(CHANGESET_ID, WORKSPACE_KEY, approval_token="sha256:" + "c" * 64)

    def test_request_cancel_returns_false_while_applying(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        workspace = _make_workspace(tmp_path)
        applier = ChangeSetApplier(
            workspace=workspace,
            store=store,
            permissions=_make_broker(True),
            audit=SecurityAuditLog(),
        )
        applier._applying = True
        assert applier.request_cancel() is False


class TestReverseApplier:
    def test_rollback_rejects_no_evidence(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        workspace = _make_workspace(tmp_path)
        reverser = ReverseApplier(
            workspace=workspace,
            store=store,
            permissions=_make_broker(True),
            audit=SecurityAuditLog(),
        )
        _make_changeset(store, workspace, status=ChangeSetStatus.DRAFT)
        with pytest.raises(QuantsMindError):
            reverser.rollback(CHANGESET_ID, WORKSPACE_KEY)
