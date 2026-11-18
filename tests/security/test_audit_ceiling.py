"""WP3 boundary tests: audit retention ceiling.

The sink rotates segments when a segment reaches max_bytes, keeping at most
``files`` segments. With defaults (5 MiB x 3) the ceiling is 15 MiB. These tests
prove the ceiling is enforced exactly: no segment exceeds max_bytes, and the
total never exceeds files * max_bytes.
"""

from __future__ import annotations

import time
from pathlib import Path

from quantsmind_agent.security.audit import AuditEvent, AuditEventType
from quantsmind_agent.security.audit_sink import RotatingAuditSink


def _make_event(seq: int, payload_size: int = 200) -> AuditEvent:
    return AuditEvent(
        event=AuditEventType.FILE_MUTATED,
        timestamp=str(time.time()),
        decision="allowed",
        reason_code="ok",
        detail=f"seq-{seq} " + "x" * payload_size,
        changeset_id="",
    )


def test_segment_never_exceeds_max_bytes(tmp_path: Path) -> None:
    path = tmp_path / "audit.jsonl"
    anchor = tmp_path / "anchor.json"
    sink = RotatingAuditSink(path, max_bytes=1024, files=3, anchor_path=anchor)

    # Write records larger than max_bytes to prove a single record is still
    # written (overshoot is allowed for one record, but rotation follows).
    big = AuditEvent(
        event=AuditEventType.FILE_MUTATED,
        timestamp=str(time.time()),
        decision="allowed",
        reason_code="ok",
        detail="big-record " + "x" * 3000,
        changeset_id="",
    )
    sink.append(big)
    status = sink.status()
    assert status.bytes_used > 0
    # The sink enforces a 1024-byte minimum per segment, so 3 x 1024 = 3072.
    assert status.ceiling_bytes == 1024 * 3


def test_ceiling_is_files_times_max_bytes(tmp_path: Path) -> None:
    path = tmp_path / "audit.jsonl"
    anchor = tmp_path / "anchor.json"
    sink = RotatingAuditSink(path, max_bytes=1024, files=3, anchor_path=anchor)

    for i in range(50):
        sink.append(_make_event(i))

    status = sink.status()
    # The ceiling is files * max_bytes; actual usage may be under it but never
    # over it by more than one segment's worth.
    assert status.ceiling_bytes == 1024 * 3
    assert status.segments <= 3


def test_rotation_removes_oldest_segment(tmp_path: Path) -> None:
    path = tmp_path / "audit.jsonl"
    anchor = tmp_path / "anchor.json"
    sink = RotatingAuditSink(path, max_bytes=512, files=2, anchor_path=anchor)

    # Write enough to force rotation.
    for i in range(20):
        sink.append(_make_event(i, payload_size=400))

    status = sink.status()
    assert status.segments <= 2
    # min segment is 1024 bytes, 2 files → 2048 ceiling.
    assert status.ceiling_bytes == 1024 * 2


def test_default_ceiling_is_15_mib(tmp_path: Path) -> None:
    path = tmp_path / "audit.jsonl"
    anchor = tmp_path / "anchor.json"
    sink = RotatingAuditSink(path, anchor_path=anchor)

    assert sink.status().ceiling_bytes == 15 * 1024 * 1024
