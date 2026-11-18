"""WP1-C: the canonical persistence layout, proven against the disk it wrote.

These tests read files back from the store rather than trusting return values,
because the requirement they check is "a successful return from an in-memory
operation is not evidence of durable persistence". Where a test wants to know
whether something survived a restart it builds a second store over the same
directories, which is the closest thing to a restart the test process can do.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

import pytest

from quantsmind_agent.persistence.paths import AppPaths
from quantsmind_agent.services.changesets.lifecycle import ChangeSetStatus
from quantsmind_agent.services.changesets.storage import (
    JOURNAL_EVENT_FIELDS,
    MAX_INLINE_PRE_IMAGE_BYTES,
    SCHEMA_VERSION,
    BlobIntegrityError,
    CanonicalJSONError,
    ChangeSetPaths,
    ChangeSetStore,
    CorruptRecordError,
    Journal,
    LayoutError,
    StorageError,
    canonical_json,
    content_digest,
    digest_of,
    new_changeset_id,
    sha256_hex,
    utc_now_stamp,
    verify_digest,
    workspace_key_for,
)

WORKSPACE_KEY = "0123456789abcdef"
CHANGESET_ID = "cs-01J9Z6Q2K8V3M4T7R1W0X5Y6B7"
OP_ID = "op-0001"


def manifest_payload(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
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


@pytest.fixture()
def data_dir(tmp_path: Path) -> Path:
    return tmp_path / "data"


@pytest.fixture()
def store(data_dir: Path) -> ChangeSetStore:
    return ChangeSetStore(
        changesets_dir=data_dir / "changesets",
        blobs_dir=data_dir / "blobs" / "sha256",
        locks_dir=data_dir / "locks",
        quarantine_dir=data_dir / "recovery" / "quarantine",
    ).ensure()


@pytest.fixture()
def paths(store: ChangeSetStore) -> ChangeSetPaths:
    return store.paths_for(WORKSPACE_KEY, CHANGESET_ID)


# -- canonical json and digests ---------------------------------------------
def test_canonical_json_sorts_keys_and_drops_insignificant_space() -> None:
    assert canonical_json({"b": 1, "a": [{"d": 2, "c": 3}]}) == b'{"a":[{"c":3,"d":2}],"b":1}'


def test_canonical_json_leaves_text_unescaped_so_digests_are_portable() -> None:
    assert "é".encode() in canonical_json({"k": "é"})


@pytest.mark.parametrize("material", [{"ratio": 0.5}, {"nested": [{"x": 1.0}]}, {1: "key"}, {"o": object()}])
def test_material_that_cannot_be_reproduced_is_refused(material: Any) -> None:
    """§7.1: integers and strings only. A float serialises differently per build."""

    with pytest.raises(CanonicalJSONError):
        canonical_json(material)


def test_the_digest_of_the_same_material_written_differently_is_the_same() -> None:
    first = digest_of({"a": 1, "b": [{"c": 2, "d": 3}]})
    second = digest_of({"b": [{"d": 3, "c": 2}], "a": 1})
    assert first == second
    assert re.fullmatch(r"sha256:[0-9a-f]{64}", first)


def test_a_single_digit_change_invalidates_the_digest() -> None:
    material = {"operations": [{"op_id": "op-0001", "order": 10}]}
    digest = digest_of(material)
    assert verify_digest(digest, material) is True
    material["operations"][0]["order"] = 11
    assert verify_digest(digest, material) is False


@pytest.mark.parametrize("digest", ["", "sha256:deadbeef", "deadbeef", "sha512:" + "0" * 128])
def test_a_malformed_digest_never_matches(digest: str) -> None:
    assert verify_digest(digest, {"a": 1}) is False


def test_content_digest_is_the_bare_hash_prefixed_once(content: bytes = b"payload") -> None:
    assert content_digest(content) == f"sha256:{hashlib.sha256(content).hexdigest()}"
    assert sha256_hex(content) == hashlib.sha256(content).hexdigest()


# -- identity ---------------------------------------------------------------
def test_the_workspace_key_is_sixteen_lowercase_hex_characters() -> None:
    key = workspace_key_for(["F:/work/repo"])
    assert re.fullmatch(r"[0-9a-f]{16}", key)


def test_the_same_root_set_written_differently_yields_the_same_key() -> None:
    forward = workspace_key_for(["F:/work/a", "F:/work/b"])
    backward = workspace_key_for(["F:\\work\\b\\", "F:/work/a"])
    assert forward == backward


def test_a_moved_workspace_is_a_different_key_so_its_approval_does_not_travel() -> None:
    assert workspace_key_for(["F:/work/a"]) != workspace_key_for(["F:/moved/a"])


def test_an_empty_root_set_cannot_produce_a_key() -> None:
    with pytest.raises(LayoutError):
        workspace_key_for([])


def test_changeset_ids_are_crockford_ulids_and_never_repeat() -> None:
    ids = {new_changeset_id() for _ in range(200)}
    assert len(ids) == 200
    for identifier in ids:
        assert re.fullmatch(r"cs-[0-9A-Z]{26}", identifier)
        assert not set(identifier[3:]) & set("ILOU")


def test_the_timestamp_part_of_the_id_sorts_the_created_part() -> None:
    assert new_changeset_id(now=1_700_000_000.0) < new_changeset_id(now=1_800_000_000.0)


# -- blob store -------------------------------------------------------------
def test_a_blob_lands_under_the_first_two_hex_characters_of_its_hash(store: ChangeSetStore) -> None:
    digest = store.blobs.put(b"pre-image")
    hex64 = digest.split(":", 1)[1]
    assert store.blobs.root / hex64[:2] / hex64 == store.blobs.path_for(digest)
    assert store.blobs.path_for(digest).is_file()


def test_staging_the_same_bytes_twice_is_one_blob_and_does_not_rewrite_it(
    store: ChangeSetStore,
) -> None:
    digest = store.blobs.put(b"same")
    before = store.blobs.path_for(digest).stat().st_mtime_ns
    assert store.blobs.put(b"same") == digest
    assert store.blobs.path_for(digest).stat().st_mtime_ns == before


def test_a_tampered_blob_is_refused_on_read_rather_than_restored(store: ChangeSetStore) -> None:
    digest = store.blobs.put(b"original")
    store.blobs.path_for(digest).write_bytes(b"tampered")
    with pytest.raises(BlobIntegrityError) as raised:
        store.blobs.read(digest)
    assert raised.value.reason_code == "blob_integrity"


def test_a_missing_pre_image_is_a_failure_not_a_none(store: ChangeSetStore) -> None:
    with pytest.raises(BlobIntegrityError):
        store.blobs.read(content_digest(b"never staged"))


@pytest.mark.parametrize(
    "digest",
    ["", "sha256:zz", f"sha256:{'0' * 63}", "md5:" + "0" * 32, "../escaped", "sha256:" + "../" + "0" * 62],
)
def test_a_blob_address_that_is_not_a_hash_cannot_become_a_path(store: ChangeSetStore, digest: str) -> None:
    with pytest.raises(LayoutError):
        store.blobs.path_for(digest)


def test_a_write_leaves_no_temporary_file_behind(
    data_dir: Path, store: ChangeSetStore, paths: ChangeSetPaths
) -> None:
    store.blobs.put(b"blob")
    store.save_manifest(paths, manifest_payload())
    store.stage(paths, OP_ID, b"pre-image", kind="base")
    assert [path.name for path in data_dir.rglob("*.tmp")] == []


# -- layout -----------------------------------------------------------------
def test_the_layout_matches_six_dot_five_exactly(paths: ChangeSetPaths) -> None:
    root = paths.root
    assert root.parts[-2:] == (WORKSPACE_KEY, CHANGESET_ID)
    relative = {
        paths.manifest: "manifest.json",
        paths.approval: "approval.json",
        paths.verification: "verification.json",
        paths.journal: "journal.jsonl",
        paths.diff_patch: "diff.patch",
        paths.op_file(OP_ID, "base.bin"): f"ops/{OP_ID}/base.bin",
        paths.op_file(OP_ID, "result.bin"): f"ops/{OP_ID}/result.bin",
    }
    for path, name in relative.items():
        assert str(path.relative_to(root)).replace("\\", "/") == name


@pytest.mark.parametrize(
    "key,identifier",
    [
        ("../0123456789abcdef", CHANGESET_ID),
        ("0123456789abcdeg", CHANGESET_ID),
        ("0123456789ABCDEF", CHANGESET_ID),
        ("", CHANGESET_ID),
        (WORKSPACE_KEY, "cs-01J9Z6Q2K8V3M4T7R1W0X5Y6B"),
        (WORKSPACE_KEY, "cs-short"),
        (WORKSPACE_KEY, ""),
        (WORKSPACE_KEY, "cs-01J9Z6Q2K8V3M4T7R1W0X5Y6B7/.."),
    ],
)
def test_an_identity_that_is_not_the_accepted_shape_never_becomes_a_directory(
    store: ChangeSetStore, key: str, identifier: str
) -> None:
    with pytest.raises(LayoutError):
        store.paths_for(key, identifier)


def test_an_operation_id_cannot_carry_a_path(store: ChangeSetStore) -> None:
    with pytest.raises(LayoutError):
        store.paths_for(WORKSPACE_KEY, CHANGESET_ID).op_dir("../../escape")


def test_only_the_two_per_operation_files_are_named(store: ChangeSetStore) -> None:
    with pytest.raises(LayoutError):
        store.paths_for(WORKSPACE_KEY, CHANGESET_ID).op_file(OP_ID, "secret.bin")


def test_the_lock_file_is_named_after_the_workspace_key(store: ChangeSetStore) -> None:
    assert store.lock_path(WORKSPACE_KEY).name == f"{WORKSPACE_KEY}.apply.lock"
    with pytest.raises(LayoutError):
        store.lock_path("../etc")


# -- journal ----------------------------------------------------------------
def journal_fields(event: str, **extra: Any) -> dict[str, Any]:
    """Minimum legal payload for ``event``; ``seq`` is assigned by the writer."""

    defaults: dict[str, Any] = {
        "changeset_id": CHANGESET_ID,
        "revision": 1,
        "digest": f"sha256:{'a' * 64}",
        "workspace_key": WORKSPACE_KEY,
        "pid": 4242,
        "op_ids": [OP_ID],
        "op_id": OP_ID,
        "type": "MODIFY_FILE",
        "path": "src/app.py",
        "destination_path": None,
        "base": {"exists": True, "sha256": "b" * 64, "size": 10},
        "result": {"exists": True, "sha256": "c" * 64, "size": 12},
        "actual_sha256": "c" * 64,
        "actual_exists": True,
        "code": "conflict",
        "message": "the file moved",
        "expected": "b" * 64,
        "actual": "d" * 64,
        "reason": "user",
        "decision": "ROLLBACK_SAFE",
        "classification": {"op_id": OP_ID, "state": "COMMITTED"},
    }
    fields = {name: defaults[name] for name in JOURNAL_EVENT_FIELDS[event] if name in defaults}
    fields.update(extra)
    return fields


def test_records_are_numbered_from_one_and_read_back_in_order(
    store: ChangeSetStore, paths: ChangeSetPaths
) -> None:
    journal = store.journal(paths)
    assert journal.append("apply_started", journal_fields("apply_started")) == 1
    assert journal.append("op_started", journal_fields("op_started")) == 2
    assert journal.append("apply_completed") == 3
    scan = Journal(paths.journal).scan()
    assert [record["seq"] for record in scan.records] == [1, 2, 3]
    assert [record["event"] for record in scan.records] == [
        "apply_started",
        "op_started",
        "apply_completed",
    ]


def test_a_second_writer_over_the_same_file_continues_the_sequence(
    store: ChangeSetStore, paths: ChangeSetPaths
) -> None:
    first = store.journal(paths)
    first.append("apply_started", journal_fields("apply_started"))
    reopened = Journal(paths.journal)
    assert reopened.next_seq() == 2
    assert reopened.append("op_started", journal_fields("op_started")) == 2


def test_an_unassigned_timestamp_is_filled_in_as_iso_utc(
    store: ChangeSetStore, paths: ChangeSetPaths
) -> None:
    store.journal(paths).append("apply_started", journal_fields("apply_started", ts=None))
    record = Journal(paths.journal).scan().records[0]
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?Z", record["ts"])
    assert utc_now_stamp().endswith("Z")


@pytest.mark.parametrize("event", sorted(JOURNAL_EVENT_FIELDS))
def test_every_documented_event_can_be_written(event: str, store: ChangeSetStore) -> None:
    paths = store.paths_for(WORKSPACE_KEY, new_changeset_id())
    store.journal(paths).append(event, journal_fields(event))
    assert Journal(paths.journal).scan().records[0]["event"] == event


def test_a_record_that_cannot_be_attributed_is_refused_before_it_is_written(
    store: ChangeSetStore, paths: ChangeSetPaths
) -> None:
    journal = store.journal(paths)
    with pytest.raises(StorageError) as raised:
        journal.append("op_committed", {"op_id": OP_ID})
    assert raised.value.reason_code == "journal_field_missing"
    assert paths.journal.exists() is False


def test_an_event_the_specification_does_not_define_is_refused(store: ChangeSetStore) -> None:
    with pytest.raises(StorageError) as raised:
        store.journal(store.paths_for(WORKSPACE_KEY, CHANGESET_ID)).append("did_something_fun", {})
    assert raised.value.reason_code == "journal_event_unknown"


@pytest.mark.parametrize("key", ["content", "stdout", "api_key", "prompt", "DATA"])
def test_the_journal_holds_paths_and_hashes_never_content_or_secrets(
    store: ChangeSetStore, key: str
) -> None:
    paths = store.paths_for(WORKSPACE_KEY, CHANGESET_ID)
    journal = store.journal(paths)
    journal.append("apply_started", journal_fields("apply_started"))
    before = paths.journal.read_bytes()
    with pytest.raises(StorageError) as raised:
        journal.append("op_started", journal_fields("op_started", **{key: "hunter2"}))
    assert raised.value.reason_code == "journal_field_forbidden"
    assert paths.journal.read_bytes() == before


def test_a_torn_final_line_is_kept_as_evidence_and_reopens_the_scan(
    store: ChangeSetStore, paths: ChangeSetPaths
) -> None:
    journal = store.journal(paths)
    journal.append("apply_started", journal_fields("apply_started"))
    with paths.journal.open("ab") as handle:
        handle.write(b'{"seq":2,"event":"op_s')  # the crash landed mid-write

    scan = journal.scan()
    assert [record["seq"] for record in scan.records] == [1]
    assert scan.torn_tail.startswith('{"seq":2')
    assert scan.needs_scan() is True


def test_a_broken_line_inside_the_journal_is_not_skipped_silently(
    store: ChangeSetStore, paths: ChangeSetPaths
) -> None:
    journal = store.journal(paths)
    journal.append("apply_started", journal_fields("apply_started"))
    paths.journal.write_bytes(b"{}\nnot json at all\n")
    with pytest.raises(CorruptRecordError):
        journal.scan()


def test_a_sequence_that_does_not_move_forward_is_corruption(
    store: ChangeSetStore, paths: ChangeSetPaths
) -> None:
    paths.root.mkdir(parents=True, exist_ok=True)
    paths.journal.write_text(
        '{"seq": 5, "event": "apply_started"}\n{"seq": 5, "event": "apply_completed"}\n',
        encoding="utf-8",
    )
    with pytest.raises(CorruptRecordError):
        store.journal(paths).scan()


def test_a_terminal_record_with_nothing_after_it_is_not_rescanned(
    store: ChangeSetStore, paths: ChangeSetPaths
) -> None:
    journal = store.journal(paths)
    journal.append("apply_started", journal_fields("apply_started"))
    journal.append("apply_completed")
    assert journal.scan().needs_scan() is False
    journal.append("rollback_started", journal_fields("rollback_started"))
    assert journal.scan().needs_scan() is True


def test_an_intent_without_an_outcome_is_the_op_started_only_row(
    store: ChangeSetStore, paths: ChangeSetPaths
) -> None:
    journal = store.journal(paths)
    journal.append("apply_started", journal_fields("apply_started"))
    journal.append("op_started", journal_fields("op_started"))
    journal.append("op_started", journal_fields("op_started", op_id="op-0002"))
    journal.append("op_committed", journal_fields("op_committed", op_id="op-0001"))
    scan = journal.scan()
    assert scan.unresolved_op_ids() == ("op-0002",)
    assert scan.committed_op_ids() == frozenset({"op-0001"})
    assert scan.op_record("op-0001", "op_committed")["actual_exists"] is True


# -- metadata ---------------------------------------------------------------
def test_a_manifest_round_trips_and_keeps_fields_this_build_does_not_know(
    store: ChangeSetStore, paths: ChangeSetPaths
) -> None:
    payload = manifest_payload(future_field={"kept": 1})
    store.save_manifest(paths, payload)
    reloaded = store.load_manifest(paths)
    assert reloaded is not None
    assert {key: reloaded[key] for key in payload} == payload
    assert reloaded["future_field"] == {"kept": 1}
    assert reloaded["schema_version"] == SCHEMA_VERSION
    assert reloaded["updated_at"].endswith("Z")


def test_a_manifest_written_without_a_schema_version_is_refused(
    store: ChangeSetStore, paths: ChangeSetPaths
) -> None:
    with pytest.raises(StorageError) as raised:
        store.save_manifest(paths, manifest_payload(schema_version=99))
    assert raised.value.reason_code == "manifest_schema_unsupported"
    assert paths.manifest.exists() is False


@pytest.mark.parametrize("field", ["changeset_id", "workspace_key", "status"])
def test_a_manifest_missing_a_required_identity_field_is_refused(
    store: ChangeSetStore, paths: ChangeSetPaths, field: str
) -> None:
    payload = manifest_payload(**{field: ""})
    with pytest.raises(StorageError) as raised:
        store.save_manifest(paths, payload)
    assert raised.value.reason_code == "manifest_field_missing"


def test_a_manifest_cannot_be_written_into_someone_elses_directory(
    store: ChangeSetStore,
) -> None:
    other = store.paths_for(WORKSPACE_KEY, "cs-01J9Z6Q2K8V3M4T7R1W0X5Y6B8")
    with pytest.raises(StorageError) as raised:
        store.save_manifest(other, manifest_payload())
    assert raised.value.reason_code == "manifest_identity_mismatch"


def test_a_corrupt_manifest_fails_closed_rather_than_looking_absent(
    store: ChangeSetStore, paths: ChangeSetPaths
) -> None:
    paths.root.mkdir(parents=True, exist_ok=True)
    paths.manifest.write_text("{ not json", encoding="utf-8")
    with pytest.raises(CorruptRecordError):
        store.load_manifest(paths)
    assert store.stored(WORKSPACE_KEY, CHANGESET_ID).error != ""


def test_a_manifest_that_disagrees_with_its_location_is_not_read_as_its_own(
    store: ChangeSetStore, paths: ChangeSetPaths
) -> None:
    paths.root.mkdir(parents=True, exist_ok=True)
    paths.manifest.write_text(
        json.dumps(manifest_payload(changeset_id="cs-01J9Z6Q2K8V3M4T7R1W0X5Y6B8")), encoding="utf-8"
    )
    with pytest.raises(CorruptRecordError):
        store.load_manifest(paths)


def test_an_absent_manifest_is_absence_not_an_error(
    store: ChangeSetStore, paths: ChangeSetPaths
) -> None:
    assert store.load_manifest(paths) is None
    record = store.stored(WORKSPACE_KEY, CHANGESET_ID)
    assert record.manifest is None
    assert record.error == ""


def test_approval_and_verification_evidence_land_on_the_declared_files(
    store: ChangeSetStore, paths: ChangeSetPaths
) -> None:
    store.save_approval(paths, {"records": [{"decision": "APPROVED"}]})
    store.save_verification(paths, {"summary": "two checks passed"})
    assert store.load_approval(paths) == {
        "schema_version": SCHEMA_VERSION,
        "records": [{"decision": "APPROVED"}],
    }
    assert store.load_verification(paths) == {
        "summary": "two checks passed",
        "schema_version": SCHEMA_VERSION,
    }
    assert paths.approval.is_file() and paths.verification.is_file()


def test_a_rendered_diff_is_stored_and_reported_by_digest(store: ChangeSetStore) -> None:
    digest = store.save_diff(paths := store.paths_for(WORKSPACE_KEY, CHANGESET_ID), "--- a\n+++ b\n")
    assert digest == content_digest(b"--- a\n+++ b\n")
    assert paths.diff_patch.read_text(encoding="utf-8") == "--- a\n+++ b\n"


# -- staging ----------------------------------------------------------------
def test_a_small_pre_image_is_a_blob_and_a_readable_copy_for_the_reviewer(
    store: ChangeSetStore, paths: ChangeSetPaths
) -> None:
    data = b"the old file"
    digest, inline = store.stage(paths, OP_ID, data, kind="base")
    assert inline is True
    assert digest == content_digest(data)
    assert paths.op_file(OP_ID, "base.bin").read_bytes() == data
    assert store.read_staged(digest) == data


def test_a_large_pre_image_lives_only_in_the_blob_store(
    store: ChangeSetStore, paths: ChangeSetPaths
) -> None:
    data = bytes(MAX_INLINE_PRE_IMAGE_BYTES + 1)
    digest, inline = store.stage(paths, OP_ID, data, kind="result")
    assert inline is False
    assert digest == content_digest(data)
    assert paths.op_file(OP_ID, "result.bin").exists() is False


# -- enumeration and scan ---------------------------------------------------
def test_the_store_creates_every_directory_six_dot_five_declares(
    data_dir: Path,
) -> None:
    store = ChangeSetStore(
        changesets_dir=data_dir / "changesets",
        blobs_dir=data_dir / "blobs" / "sha256",
        locks_dir=data_dir / "locks",
        quarantine_dir=data_dir / "recovery" / "quarantine",
    ).ensure()
    for directory in (
        store.changesets_dir,
        store.blobs.root,
        store.locks_dir,
        store.quarantine_dir,
    ):
        assert directory.is_dir()


def test_the_store_can_be_built_from_the_declared_app_paths(tmp_path: Path) -> None:
    app_paths = AppPaths(
        config_dir=tmp_path / "config",
        log_dir=tmp_path / "logs",
        cache_dir=tmp_path / "cache",
        data_dir=tmp_path / "data",
    ).ensure()
    assert app_paths.quarantine_dir == tmp_path / "data" / "recovery" / "quarantine"
    assert app_paths.changesets_dir.is_dir()
    assert app_paths.blobs_dir.is_dir()
    assert app_paths.locks_dir.is_dir()
    assert app_paths.quarantine_dir.is_dir()
    store = ChangeSetStore.from_app_paths(app_paths)
    assert store.changesets_dir == app_paths.changesets_dir


def test_stray_directories_in_the_store_are_ignored_not_enumerated(
    store: ChangeSetStore,
) -> None:
    (store.changesets_dir / "not-a-key").mkdir()
    (store.changesets_dir / "notes.txt").write_text("x", encoding="utf-8")
    store.paths_for(WORKSPACE_KEY, CHANGESET_ID).root.mkdir(parents=True)
    assert store.list_workspace_keys() == (WORKSPACE_KEY,)
    assert [record.paths.changeset_id for record in store.iter_stored()] == [CHANGESET_ID]


def needing_scan_case(
    store: ChangeSetStore, status: str, journal_events: tuple[str, ...]
) -> ChangeSetPaths:
    identifier = new_changeset_id()
    paths = store.paths_for(WORKSPACE_KEY, identifier)
    paths.root.mkdir(parents=True, exist_ok=True)
    store.save_manifest(paths, manifest_payload(status=status, changeset_id=identifier))
    journal = store.journal(paths)
    for event in journal_events:
        journal.append(event, journal_fields(event))
    return paths


def test_an_unstable_manifest_is_found_even_with_no_journal(store: ChangeSetStore) -> None:
    needing_scan_case(store, ChangeSetStatus.APPLYING.value, ())
    found = store.needing_scan()
    assert len(found) == 1
    assert found[0][1] == f"status:{ChangeSetStatus.APPLYING.value}"


def test_a_manifest_claiming_success_next_to_an_open_journal_is_still_scanned(
    store: ChangeSetStore,
) -> None:
    needing_scan_case(store, ChangeSetStatus.APPLIED.value, ("apply_started", "op_started"))
    found = store.needing_scan()
    assert [reason for _, reason in found] == ["journal_open"]


def test_a_clean_applied_changeset_is_left_alone(store: ChangeSetStore) -> None:
    needing_scan_case(store, ChangeSetStatus.APPLIED.value, ("apply_started", "apply_completed"))
    assert store.needing_scan() == ()


def test_a_journal_that_cannot_be_parsed_is_reported_not_swallowed(
    store: ChangeSetStore,
) -> None:
    paths = needing_scan_case(store, ChangeSetStatus.APPLIED.value, ())
    paths.journal.write_text("not json\n", encoding="utf-8")
    found = store.needing_scan()
    assert found[0][1].startswith("journal_corrupt:")


def test_quarantine_directories_are_addressed_per_operation(store: ChangeSetStore) -> None:
    target = store.quarantine_dir_for(WORKSPACE_KEY, CHANGESET_ID, OP_ID)
    assert str(target.relative_to(store.quarantine_dir)).replace("\\", "/") == (
        f"{WORKSPACE_KEY}/{CHANGESET_ID}/{OP_ID}"
    )
    with pytest.raises(LayoutError):
        store.quarantine_dir_for(WORKSPACE_KEY, CHANGESET_ID, "op-1")


# -- durability -------------------------------------------------------------
def test_everything_a_transaction_needs_to_recover_survives_a_reopen(
    data_dir: Path,
) -> None:
    """The restart proof: write with one store, read with another over the same disk."""

    store = ChangeSetStore(
        changesets_dir=data_dir / "changesets",
        blobs_dir=data_dir / "blobs" / "sha256",
        locks_dir=data_dir / "locks",
        quarantine_dir=data_dir / "recovery" / "quarantine",
    ).ensure()
    paths = store.paths_for(WORKSPACE_KEY, CHANGESET_ID)
    store.save_manifest(paths, manifest_payload(status=ChangeSetStatus.APPLYING.value))
    store.save_approval(paths, {"records": [{"decision": "APPROVED", "digest": f"sha256:{'a' * 64}"}]})
    digest, _ = store.stage(paths, OP_ID, b"pre-image bytes", kind="base")
    journal = store.journal(paths)
    journal.append("apply_started", journal_fields("apply_started"))
    journal.append("op_started", journal_fields("op_started"))

    reopened = ChangeSetStore(
        changesets_dir=data_dir / "changesets",
        blobs_dir=data_dir / "blobs" / "sha256",
        locks_dir=data_dir / "locks",
        quarantine_dir=data_dir / "recovery" / "quarantine",
    )
    reload_paths = reopened.paths_for(WORKSPACE_KEY, CHANGESET_ID)
    assert reopened.load_manifest(reload_paths)["status"] == ChangeSetStatus.APPLYING.value
    assert reopened.load_approval(reload_paths)["records"][0]["decision"] == "APPROVED"
    assert reopened.read_staged(digest) == b"pre-image bytes"
    scan = reopened.journal(reload_paths).scan()
    assert scan.unresolved_op_ids() == (OP_ID,)
    assert scan.needs_scan() is True
    assert reopened.needing_scan()[0][1] == f"status:{ChangeSetStatus.APPLYING.value};journal_open"
