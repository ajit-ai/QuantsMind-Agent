"""The durable audit store (v0.11.0 WP3).

The accepted design promises three things and disclaims a fourth: records outlive
the process, the set stays inside a byte ceiling, a changed or removed record is
*detectable* -- and none of that is tamper-*proofing*. These tests are the
evidence for each claim separately, because a store that quietly stops writing,
or one that reports "intact" after someone deleted a line, would be worse than no
store at all: it would be believed.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from quantsmind_agent.security.audit import (
    AuditEvent,
    AuditEventType,
    AuditPersistenceError,
    SecurityAuditLog,
)
from quantsmind_agent.security.audit_sink import (
    AUDIT_STORE_NAME,
    DEFAULT_FILES,
    DEFAULT_MAX_BYTES,
    GENESIS_HASH,
    RotatingAuditSink,
)

CANARY = "sk-canary-audit-0000000000"


def _event(index: int, **fields: Any) -> AuditEvent:
    """A distinct record. Timestamps differ so replay order is checkable."""

    return AuditEvent(
        event=AuditEventType.SECURITY_POLICY_DENIED,
        timestamp=f"2026-11-05T09:00:{index % 60:02d}Z",
        decision="denied",
        reason_code="policy_denied",
        detail=f"record {index}",
        **fields,
    )


@pytest.fixture()
def store(tmp_path: Path) -> RotatingAuditSink:
    return RotatingAuditSink(
        tmp_path / "audit" / AUDIT_STORE_NAME,
        anchor_path=tmp_path / "audit-chain-tip.json",
    )


def _lines(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def _rewrite(path: Path, records: list[dict[str, Any]]) -> None:
    path.write_text("".join(json.dumps(item) + "\n" for item in records), encoding="utf-8")


# -- the shape of what is written -------------------------------------------
def test_the_logical_store_is_named_as_accepted() -> None:
    assert AUDIT_STORE_NAME == "audit.jsonl"


def test_each_record_is_one_chained_line(store: RotatingAuditSink, tmp_path: Path) -> None:
    store.append(_event(1))
    store.append(_event(2))

    records = _lines(store.path)
    assert [record["sequence"] for record in records] == [1, 2]
    assert records[0]["prev_hash"] == GENESIS_HASH
    assert records[1]["prev_hash"] == records[0]["entry_hash"]
    assert store.sequence == 2
    assert store.tip == records[1]["entry_hash"]


def test_the_witness_is_held_outside_the_rotating_set(store: RotatingAuditSink) -> None:
    store.append(_event(1))

    assert store.anchor_path.parent == store.path.parent.parent
    anchor = store.read_anchor()
    assert anchor is not None
    assert anchor["sequence"] == 1
    assert anchor["last_entry_hash"] == store.tip


def test_verification_passes_on_a_store_this_process_wrote(store: RotatingAuditSink) -> None:
    for index in range(5):
        store.append(_event(index))

    report = store.verify_chain(force=True)
    assert report.ok is True
    assert report.entries == 5
    assert "chain intact" in report.describe()


def test_replay_returns_the_records_oldest_first(store: RotatingAuditSink) -> None:
    for index in range(4):
        store.append(_event(index))

    replayed = store.replay()
    assert [record.detail for record in replayed] == [
        "record 0",
        "record 1",
        "record 2",
        "record 3",
    ]
    assert [record.detail for record in store.replay(limit=2)] == ["record 2", "record 3"]


def test_an_unknown_event_name_is_dropped_rather_than_guessed() -> None:
    assert AuditEvent.from_dict({"event": "credential.future_action", "timestamp": "x"}) is None
    assert AuditEvent.from_dict({"timestamp": "x"}) is None


# -- what a reader can detect -----------------------------------------------
def test_a_changed_field_is_detected(store: RotatingAuditSink) -> None:
    for index in range(4):
        store.append(_event(index))
    records = _lines(store.path)
    records[1]["detail"] = "record 1 was edited"
    _rewrite(store.path, records)

    report = store.verify_chain(force=True)
    assert report.ok is False
    assert "digest does not match" in report.reason
    assert report.first_bad_sequence == 2


def test_a_removed_middle_record_is_detected(store: RotatingAuditSink) -> None:
    for index in range(4):
        store.append(_event(index))
    records = _lines(store.path)
    del records[1]
    _rewrite(store.path, records)

    report = store.verify_chain(force=True)
    assert report.ok is False
    assert "link does not match" in report.reason


def test_a_trimmed_tail_is_detected(store: RotatingAuditSink) -> None:
    for index in range(5):
        store.append(_event(index))
    records = _lines(store.path)
    _rewrite(store.path, records[:-2])

    report = store.verify_chain(force=True)
    assert report.ok is False
    assert "missing after" in report.reason


def test_a_trimmed_head_is_detected(store: RotatingAuditSink) -> None:
    for index in range(5):
        store.append(_event(index))
    records = _lines(store.path)
    _rewrite(store.path, records[2:])

    report = store.verify_chain(force=True)
    assert report.ok is False
    assert "the window starts at record" in report.reason


def test_an_unreadable_line_is_reported_as_one(store: RotatingAuditSink) -> None:
    store.append(_event(1))
    with open(store.path, "ab") as handle:
        handle.write(b"this is not json\n")

    report = store.verify_chain(force=True)
    assert report.ok is False
    assert "unreadable line" in report.reason


def test_wiping_the_store_leaves_the_witness_saying_so(store: RotatingAuditSink) -> None:
    for index in range(3):
        store.append(_event(index))
    store.path.unlink()

    report = store.verify_chain(force=True)
    assert report.ok is False
    assert "were witnessed, none are on disk" in report.reason


def test_a_change_made_while_the_application_was_closed_is_flagged_at_startup(
    tmp_path: Path,
) -> None:
    """The gap the witness exists for: nobody was running to notice.

    A restarted sink has to rebuild its chain state from the files, so it rewrites
    the witness -- which would erase the evidence. The disagreement is therefore
    judged before that rewrite and kept for the life of the session.
    """

    path = tmp_path / "audit" / AUDIT_STORE_NAME
    first = RotatingAuditSink(path, anchor_path=tmp_path / "audit-chain-tip.json")
    for index in range(5):
        first.append(_event(index))
    _rewrite(path, _lines(path)[:-1])

    restarted = RotatingAuditSink(path, anchor_path=tmp_path / "audit-chain-tip.json")

    assert restarted.recovery_note != ""
    assert restarted.status().recovery_note == restarted.recovery_note
    # And the flag survives: appending now does not launder the earlier change.
    restarted.append(_event(9))
    assert restarted.status().recovery_note != ""


def test_a_clean_restart_reports_no_startup_divergence(tmp_path: Path) -> None:
    path = tmp_path / "audit" / AUDIT_STORE_NAME
    anchor = tmp_path / "audit-chain-tip.json"
    first = RotatingAuditSink(path, anchor_path=anchor)
    for index in range(3):
        first.append(_event(index))

    restarted = RotatingAuditSink(path, anchor_path=anchor)

    assert restarted.recovery_note == ""
    assert restarted.sequence == 3
    assert restarted.status().chain_ok is True


# -- bounded retention ------------------------------------------------------
def test_rotation_holds_the_segment_count_and_the_byte_ceiling(store: RotatingAuditSink) -> None:
    small = RotatingAuditSink(
        store.path,
        max_bytes=2048,
        files=3,
        anchor_path=store.anchor_path,
    )
    for index in range(60):
        small.append(_event(index))

    segments = small.segments()
    assert len(segments) <= 3
    assert sum(path.stat().st_size for path in segments) <= small.ceiling_bytes
    assert small.verify_chain(force=True).ok is True


def test_a_segment_is_filled_to_the_ceiling_but_never_past_it(store: RotatingAuditSink) -> None:
    """The ceiling is asked for before the write, so the boundary is exact.

    Rotating after the fact lets one record overshoot, and three segments that
    each overshoot push the accepted total past the number it was capped at. The
    point of the ruling is the number, so the check is per step and per file: a
    segment may end exactly full, and the record that would add one more byte is
    the one that starts the next segment.
    """

    segment_bytes = 4096
    small = RotatingAuditSink(
        store.path,
        max_bytes=segment_bytes,
        files=3,
        anchor_path=store.anchor_path,
    )
    active = small.path
    for index in range(40):
        before = active.stat().st_size if active.exists() else 0
        small.append(_event(index))
        after = active.stat().st_size

        if after < before:
            # The file was closed and reopened: only a record that could not fit
            # moves a segment, and the line now alone in the new file is the proof.
            assert before + after > segment_bytes
        assert after <= segment_bytes
        used = sum(path.stat().st_size for path in small.segments())
        assert used <= small.ceiling_bytes

    assert small.verify_chain(force=True).ok is True


def test_the_retention_ruling_is_pinned_in_the_constants() -> None:
    """15 MiB means 15,728,640 bytes: three binary-megabyte segments.

    Decimal 15,000,000 and binary 15 MiB are different promises, and the one
    accepted is the binary one. Pinned so a later edit to either number trips
    here rather than quietly widening the stored set.
    """

    assert DEFAULT_MAX_BYTES == 5 * 1024 * 1024 == 5_242_880
    assert DEFAULT_FILES == 3
    assert DEFAULT_MAX_BYTES * DEFAULT_FILES == 15_728_640


def test_history_older_than_the_ceiling_is_gone_and_says_so(store: RotatingAuditSink) -> None:
    small = RotatingAuditSink(
        store.path,
        max_bytes=2048,
        files=3,
        anchor_path=store.anchor_path,
    )
    for index in range(60):
        small.append(_event(index))

    status = small.status()
    assert status.records < 60
    assert status.records == len(small.replay(limit=0))
    assert small.verify_chain(force=True).ok is True


def test_status_reports_the_ceiling_it_was_given(store: RotatingAuditSink) -> None:
    store.append(_event(1))

    status = store.status()
    assert status.store_name == AUDIT_STORE_NAME
    assert status.ceiling_bytes == store.ceiling_bytes
    assert status.records == 1
    assert status.segments == 1
    assert status.degraded is False
    assert status.chain_ok is True


def test_the_count_a_page_reads_is_the_count_that_was_just_written(
    store: RotatingAuditSink,
) -> None:
    """A cached verdict must not age the live count.

    Verification digests the whole window, so its answer is held briefly. A page
    refreshing inside that moment would otherwise report fewer records than the
    application had already put on disk.
    """

    store.append(_event(1))
    assert store.status().records == 1

    for index in range(2, 6):
        store.append(_event(index))

    status = store.status()
    assert status.records == 5
    assert status.records == len(store.replay(limit=0))


def test_the_chain_continues_after_a_restart(tmp_path: Path) -> None:
    path = tmp_path / "audit" / AUDIT_STORE_NAME
    anchor = tmp_path / "audit-chain-tip.json"
    RotatingAuditSink(path, anchor_path=anchor).append(_event(1))

    restarted = RotatingAuditSink(path, anchor_path=anchor)
    restarted.append(_event(2))
    records = _lines(path)

    assert [record["sequence"] for record in records] == [1, 2]
    assert records[1]["prev_hash"] == records[0]["entry_hash"]
    assert restarted.verify_chain(force=True).ok is True


# -- failure policy ---------------------------------------------------------
def _blocked(tmp_path: Path) -> RotatingAuditSink:
    """A sink whose store directory cannot be created: a path component is a file."""

    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory", encoding="utf-8")
    return RotatingAuditSink(
        blocker / "audit" / AUDIT_STORE_NAME,
        anchor_path=tmp_path / "audit-chain-tip.json",
    )


def test_a_durable_write_that_fails_raises(tmp_path: Path) -> None:
    sink = _blocked(tmp_path)

    with pytest.raises(AuditPersistenceError):
        sink.append(_event(1), durable=True)
    assert sink.degraded is True
    assert sink.last_error != ""
    # Nothing reached the disk, so nothing may be witnessed either.
    assert sink.sequence == 0
    assert sink.tip == GENESIS_HASH
    assert sink.read_anchor() is None


def test_an_approval_cannot_be_recorded_is_not_an_approval(tmp_path: Path) -> None:
    """The gate: the caller that asked for durability feels the failure."""

    log = SecurityAuditLog(sink=_blocked(tmp_path))

    with pytest.raises(AuditPersistenceError):
        log.record(AuditEventType.APPROVAL_DECISION, decision="allow_once", durable=True)


def test_a_post_hoc_failure_is_kept_in_memory_and_flagged(tmp_path: Path) -> None:
    log = SecurityAuditLog(sink=_blocked(tmp_path))

    entry = log.record(AuditEventType.FILE_MUTATED, subject="a.py", detail="edited")

    assert entry in log.events
    assert log.degraded is True
    assert log.lost_records


def test_the_gap_is_written_as_a_marker_before_the_next_good_record(
    store: RotatingAuditSink,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Silence would read as "nothing happened", so the loss is recorded too."""

    log = SecurityAuditLog(sink=store)
    original = store._write
    attempts = {"count": 0}

    def flaky(encoded: bytes, *, durable: bool) -> None:
        attempts["count"] += 1
        if attempts["count"] == 1:
            raise OSError(28, "no space left on device")
        original(encoded, durable=durable)

    monkeypatch.setattr(store, "_write", flaky)
    log.record(AuditEventType.FILE_MUTATED, subject="a.py")
    log.record(AuditEventType.FILE_MUTATED, subject="b.py")

    events = store.replay()
    assert [event.event for event in events] == [
        AuditEventType.AUDIT_DEGRADED,
        AuditEventType.FILE_MUTATED,
    ]
    assert events[0].reason_code == "audit_write_failed"
    assert store.degraded is False
    assert store.verify_chain(force=True).ok is True


def test_a_missing_anchor_is_reported_but_never_stops_a_write(store: RotatingAuditSink) -> None:
    store.append(_event(1))
    store.anchor_path.unlink()

    report = store.verify_chain(force=True)
    assert report.ok is False
    assert "anchor is missing" in report.reason

    store.append(_event(2))
    assert store.read_anchor() is not None


# -- keeping secrets out ----------------------------------------------------
def test_a_credential_never_reaches_the_store(store: RotatingAuditSink) -> None:
    log = SecurityAuditLog(sink=store)

    log.record(
        AuditEventType.CREDENTIAL_CREATED,
        provider_id="openrouter",
        detail="stored key sk-canary-audit-0000000000 for openrouter",
        subject=f"api_key={CANARY}",
    )

    written = store.path.read_text(encoding="utf-8")
    assert CANARY not in written
    assert "openrouter" in written


def test_export_writes_a_copy_with_the_chain_verdict(
    store: RotatingAuditSink, tmp_path: Path
) -> None:
    log = SecurityAuditLog(sink=store)
    for index in range(3):
        log.record(AuditEventType.SECURITY_POLICY_DENIED, detail=f"denied {index} record {index}")

    destination = tmp_path / "evidence.json"
    count = store.export(destination)

    payload = json.loads(destination.read_text(encoding="utf-8"))
    assert count == 3 == payload["records"]
    assert payload["chain"]["ok"] is True
    assert [event["detail"] for event in payload["events"]][-1].endswith("record 2")
    assert "store" in payload and "exported_at" in payload
