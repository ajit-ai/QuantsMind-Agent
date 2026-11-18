"""WP1-F fault-injection suite for the ChangeSet engine boundaries.

These tests prove unsafe situations fail closed and preserve recoverable
evidence. They exercise real filesystem state, journal state, lifecycle state
and the blob pre-image store; they never assert that a mocked helper was
called.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from quantsmind_agent.services.changesets.lifecycle import (
    ChangeSetStatus,
)
from quantsmind_agent.services.changesets.storage import (
    BlobIntegrityError,
    BlobStore,
    ChangeSetPaths,
    ChangeSetStore,
    CorruptRecordError,
    Journal,
    StorageError,
)

WORKSPACE_KEY = "0123456789abcdef"
CHANGESET_ID = "cs-01J9Z6Q2K8V3M4T7R1W0X5Y6B7"


@pytest.fixture()
def store(tmp_path: Path) -> ChangeSetStore:
    return ChangeSetStore(
        changesets_dir=tmp_path / "changesets",
        blobs_dir=tmp_path / "blobs" / "sha256",
        locks_dir=tmp_path / "locks",
        quarantine_dir=tmp_path / "recovery" / "quarantine",
    ).ensure()


@pytest.fixture()
def paths(store: ChangeSetStore) -> ChangeSetPaths:
    return store.paths_for(WORKSPACE_KEY, CHANGESET_ID)


def _manifest(**overrides: object) -> dict[str, object]:
    payload = {
        "schema_version": 1,
        "changeset_id": CHANGESET_ID,
        "revision": 1,
        "status": ChangeSetStatus.DRAFT.value,
        "workspace_key": WORKSPACE_KEY,
        "root_ids": ["root-a"],
        "scope": ["src/"],
        "operations": [],
    }
    payload.update(overrides)
    return payload


def test_missing_non_auto_journal_field_is_refused(
    store: ChangeSetStore, paths: ChangeSetPaths
) -> None:
    """A record that does not carry an explicit required field is refused, not
    guessed."""
    journal = store.journal(paths)
    with pytest.raises(StorageError) as raised:
        # ``op_started`` requires ``op_id``/``type``/``path``/``destination_path``
        # plus ``base`` and ``result``; a call that omits ``path`` must be refused.
        journal.append(
            "op_started",
            {
                "op_id": "op-0001",
                "type": "MODIFY_FILE",
                "destination_path": None,
                "base": {},
                "result": {},
            },
        )
    assert "path" in str(raised.value)


def test_null_optional_field_is_stored(store: ChangeSetStore, paths: ChangeSetPaths) -> None:
    """A null is valid where the journal schema permits it; it must not be treated
    as an absent field."""
    journal = store.journal(paths)
    sequence = journal.append(
        "op_started",
        {
            "op_id": "op-0001",
            "type": "MODIFY_FILE",
            "path": "src/app.py",
            "destination_path": None,
            "base": {},
            "result": {},
        },
    )
    assert sequence == 1
    reopened = Journal(paths.journal)
    record = reopened.scan().records[0]
    assert record["destination_path"] is None


def test_corrupt_journal_is_reported_not_swallowed(
    store: ChangeSetStore, paths: ChangeSetPaths
) -> None:
    store.journal(paths).append(
        "apply_started",
        {
            "changeset_id": CHANGESET_ID,
            "revision": 1,
            "digest": "sha256:" + "0" * 64,
            "workspace_key": WORKSPACE_KEY,
            "op_ids": [],
            "pid": 12345,
        },
    )
    paths.journal.write_text("this is not json\n", encoding="utf-8")
    with pytest.raises(CorruptRecordError):
        Journal(paths.journal).scan()


def test_tampered_blob_refused_on_read(store: ChangeSetStore, tmp_path: Path) -> None:
    blob_root = tmp_path / "blobs" / "sha256"
    blob_store = BlobStore(blob_root)
    digest = blob_store.put(b"pre-image bytes")
    blob_file = blob_store.path_for(digest)
    blob_file.parent.mkdir(parents=True, exist_ok=True)
    blob_file.write_bytes(b"tampered")
    with pytest.raises(BlobIntegrityError):
        blob_store.read(digest)
