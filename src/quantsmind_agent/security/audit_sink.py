"""The durable, rotating, hash-chained home for security audit records.

Three properties matter here, and each has a cost attached:

* **Durable** -- a record that must be on disk before an action starts is
  fsync-ed, and a failure to do that is an error the caller must feel. The
  approval that cannot be written is the approval that does not happen.
* **Bounded** -- retention is measured in bytes, not goodwill. The store is a
  fixed number of segments, so a long-lived installation cannot grow the audit
  directory without limit.
* **Chained** -- every line carries the digest of the line before it, and the
  ends of the surviving window are mirrored in an anchor file kept outside the
  rotating set. That makes a modified line, a removed middle line, a trimmed
  tail and a trimmed head all *detectable*.

The last point deserves the caveat in full: this is tamper-**detection**, not
tamper-proofing. Anyone with write access to the segments can rewrite the whole
file *and* the anchor, and nothing here can stop that; the anchor is held
keyless and outside the rotating set so ordinary operation -- rotation, cleanup,
a user clearing the audit folder -- cannot silently disagree with it. What a
writer cannot do quietly is edit one line and leave its neighbours linking.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from quantsmind_agent.persistence.atomic import atomic_write_json, read_json_object
from quantsmind_agent.security.audit import (
    AuditEvent,
    AuditEventType,
    AuditPersistenceError,
)

__all__ = [
    "AUDIT_STORE_NAME",
    "ChainReport",
    "RotatingAuditSink",
    "SinkStatus",
]

#: The canonical logical store name (owner decision P-4, 2026-11-04).
AUDIT_STORE_NAME = "audit.jsonl"

#: One segment, and how many segments are retained. 5 MiB x 3 is the 15 MiB
#: ceiling the owner accepted (Q8); the count is *total* files, not extra copies.
#: Binary multiples, not decimal: the ceiling is what the rotation stops at, so a
#: segment must not reach 5,000,000 bytes and then keep going to the next MiB.
DEFAULT_MAX_BYTES = 5 * 1024 * 1024
DEFAULT_FILES = 3

GENESIS_HASH = "0" * 64

_ANCHOR_SCHEMA = 2


def _canonical(payload: dict[str, Any]) -> str:
    """The one serialisation a digest is ever taken over."""

    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class ChainReport:
    """What a verification pass found. ``ok`` is the only verdict the UI needs."""

    entries: int
    segments: int
    ok: bool
    first_bad_sequence: int | None = None
    reason: str = ""
    anchor_sequence: int = 0
    anchor_matches: bool = True

    def describe(self) -> str:
        if self.ok:
            return f"{self.entries} records across {self.segments} segment(s), chain intact"
        where = "?" if self.first_bad_sequence is None else f"#{self.first_bad_sequence}"
        return f"chain broken at record {where}: {self.reason}"


@dataclass(frozen=True, slots=True)
class SinkStatus:
    """Read-only view for Control Center. Paths and counts, never record bodies."""

    path: str
    store_name: str
    segments: int
    bytes_used: int
    ceiling_bytes: int
    records: int
    degraded: bool
    last_error: str
    chain_ok: bool
    chain_note: str
    #: A change made while the application was closed, found at startup.
    recovery_note: str = ""

    def describe(self) -> str:
        state = "degraded" if self.degraded else "healthy"
        return (
            f"{self.records} records in {self.segments} segment(s), "
            f"{self.bytes_used // 1024} KiB of {self.ceiling_bytes // 1024} KiB, {state}"
        )


@dataclass(frozen=True, slots=True)
class _Line:
    text: str
    offset: int


@dataclass(frozen=True, slots=True)
class _Window:
    """What the files actually hold: first record, last record, how many."""

    first: tuple[int, str, str] | None
    last: tuple[int, str] | None
    entries: int


class RotatingAuditSink:
    """Appends chained records to ``audit.jsonl`` and rotates the set.

    The sink holds the chain tip in memory, mirrors the two ends of the surviving
    window to ``anchor_path`` after each write, and rebuilds both from disk if the
    process restarts.
    """

    def __init__(
        self,
        path: Path,
        *,
        max_bytes: int = DEFAULT_MAX_BYTES,
        files: int = DEFAULT_FILES,
        anchor_path: Path | None = None,
        chain_cache_seconds: float = 30.0,
    ) -> None:
        self._path = Path(path)
        self._max_bytes = max(1024, int(max_bytes))
        self._files = max(1, int(files))
        self._directory = self._path.parent
        self._anchor_path = Path(anchor_path) if anchor_path else self._default_anchor()
        # A directory that cannot be made now is retried on every write rather
        # than treated as fatal at startup: losing the audit store is a reason to
        # refuse the actions that depend on it, never a reason the application
        # cannot open.
        with contextlib.suppress(OSError):
            self._directory.mkdir(parents=True, exist_ok=True)

        self._sequence = 0
        self._tip = GENESIS_HASH
        #: The oldest record the surviving files still hold. Anchored, so that
        #: trimming the head is as visible as trimming the tail.
        self._head_sequence = 0
        self._head_prev = GENESIS_HASH
        self._head_hash = GENESIS_HASH
        self._degraded = False
        self._last_error = ""
        #: Set when a write fails; the marker goes in with the next good line.
        self._pending_marker = ""
        #: Verification is a full read of the store, so its answer is held briefly.
        self._chain_report: ChainReport | None = None
        self._chain_report_at = 0.0
        self._chain_cache_seconds = max(0.05, float(chain_cache_seconds))
        #: Why the files did not match the witness when this process opened them.
        self._recovery_note = ""
        self._recover_window()

    # -- identity ----------------------------------------------------------
    @property
    def path(self) -> Path:
        return self._path

    @property
    def anchor_path(self) -> Path:
        return self._anchor_path

    @property
    def degraded(self) -> bool:
        """True when at least one record could not be written."""

        return self._degraded

    @property
    def last_error(self) -> str:
        return self._last_error

    @property
    def recovery_note(self) -> str:
        """What the witness and the files disagreed about when this process opened.

        Empty means they agreed at startup. Anything else is a change made while no
        application was running: the store keeps working, but presenting it as a
        clean trail would be a lie, so the UI says what was found.
        """

        return self._recovery_note

    @property
    def sequence(self) -> int:
        return self._sequence

    @property
    def tip(self) -> str:
        return self._tip

    @property
    def ceiling_bytes(self) -> int:
        return self._max_bytes * self._files

    def _default_anchor(self) -> Path:
        """The witness lives outside the rotating set, on the same volume.

        A sibling of the audit directory rather than a file inside it: rotating
        or clearing the segments then cannot take the anchor with them, which is
        the whole point of keeping it elsewhere.
        """

        return self._directory.parent / f"{self._path.stem}-chain-tip.json"

    def segments(self) -> list[Path]:
        """Existing segment files, oldest first."""

        found = [self._rotated(index) for index in range(self._files - 1, 0, -1)]
        found.append(self._path)
        return [item for item in found if item.exists()]

    def _rotated(self, index: int) -> Path:
        return self._path.with_name(f"{self._path.name}.{index}")

    # -- chain state -------------------------------------------------------
    def _chain_state(self) -> tuple[int, str, int, str, str]:
        """Tip, sequence and window head -- everything a failed write must undo."""

        return (self._sequence, self._tip, self._head_sequence, self._head_prev, self._head_hash)

    def _set_chain_state(self, state: tuple[int, str, int, str, str]) -> None:
        self._sequence, self._tip, self._head_sequence, self._head_prev, self._head_hash = state

    # -- writing -----------------------------------------------------------
    def append(self, event: AuditEvent, *, durable: bool = False) -> None:
        """Write one chained record.

        ``durable`` also fsync-ed the record. A failure raises
        :class:`AuditPersistenceError` so a pre-action caller can refuse; the
        sink remembers the failure either way and reports itself degraded.
        """

        # Stamping below advances the in-process chain, because each record's
        # digest depends on the one before it. If nothing reaches the disk, none of
        # that may survive: keeping it would anchor records that do not exist.
        state = self._chain_state()
        frames: list[bytes] = []
        if self._pending_marker:
            # The gap is recorded before the record that follows it, so a reader
            # cannot mistake the silence for "nothing happened".
            frames.append(self._frame(self._marker_payload(self._pending_marker)))

        payload: dict[str, Any] = dict(event.to_dict())
        sequence = self._sequence + 1
        payload["sequence"] = sequence
        payload["prev_hash"] = self._tip
        body = _canonical(payload)
        entry_hash = _digest(body)
        payload["entry_hash"] = entry_hash
        frames.append((_canonical(payload) + "\n").encode("utf-8"))
        if self._head_sequence == 0:
            self._head_sequence = sequence
            self._head_prev = str(payload["prev_hash"])
            self._head_hash = entry_hash
        encoded = b"".join(frames)

        if self._segment_full(len(encoded)):
            self._rotate()
        try:
            self._write(encoded, durable=durable)
        except OSError as exc:
            self._set_chain_state(state)
            self._fail(f"{type(exc).__name__}: {exc.strerror or exc}")
            raise AuditPersistenceError(self.last_error) from exc

        self._sequence += 1
        self._tip = entry_hash
        self._pending_marker = ""
        self._degraded = False
        self._write_anchor()

    def _segment_full(self, incoming: int) -> bool:
        """Whether the next record would take this segment past its ceiling.

        The room is asked for before the write, not checked after it. A segment
        that is allowed to overshoot makes the accepted 15 MiB total a suggestion
        rather than a limit, and the limit is the whole point of the ruling. An
        empty segment always takes the record: one audit line is never dropped to
        keep the arithmetic tidy.
        """

        try:
            size = self._path.stat().st_size
        except OSError:
            return False
        return size > 0 and size + incoming > self._max_bytes

    def _marker_payload(self, reason: str) -> dict[str, Any]:
        return AuditEvent(
            event=AuditEventType.AUDIT_DEGRADED,
            timestamp=datetime.now(UTC).isoformat(timespec="seconds"),
            decision="degraded",
            reason_code="audit_write_failed",
            detail=reason,
        ).to_dict() | {"sequence": self._sequence + 1, "prev_hash": self._tip}

    def _frame(self, payload: dict[str, Any]) -> bytes:
        """Seal and account for a line the sink wrote itself."""

        entry_hash = _digest(_canonical(payload))
        framed = payload | {"entry_hash": entry_hash}
        self._sequence += 1
        self._tip = entry_hash
        if self._head_sequence == 0:
            self._head_sequence = int(payload["sequence"])
            self._head_prev = str(payload["prev_hash"])
            self._head_hash = entry_hash
        return (_canonical(framed) + "\n").encode("utf-8")

    def _write(self, encoded: bytes, *, durable: bool) -> None:
        self._directory.mkdir(parents=True, exist_ok=True)
        with open(self._path, "ab") as handle:
            handle.write(encoded)
            handle.flush()
            if durable:
                os.fsync(handle.fileno())

    def _fail(self, reason: str) -> None:
        self._degraded = True
        self._last_error = reason
        self._pending_marker = reason

    def _rotate(self) -> None:
        """Shift the segment set, dropping the oldest, then start a new segment.

        The chain tip is already in memory, so the first line of the next
        segment still points at the last line of the one that moved out. When a
        file is dropped the surviving window gets a new head, and that head is
        anchored: losing history to the ceiling is normal, losing it unnoticed
        is not.
        """

        dropped = False
        oldest = self._rotated(self._files - 1)
        if oldest.exists():
            try:
                oldest.unlink()
                dropped = True
            except OSError as exc:
                self._fail(f"rotation could not drop {oldest.name}: {exc}")
                return
        for index in range(self._files - 2, 0, -1):
            source = self._rotated(index)
            if source.exists():
                try:
                    os.replace(source, self._rotated(index + 1))
                except OSError as exc:
                    self._fail(f"rotation could not move {source.name}: {exc}")
                    return
        try:
            os.replace(self._path, self._rotated(1))
        except OSError as exc:
            self._fail(f"rotation could not close {self._path.name}: {exc}")
            return
        if dropped or not self._head_sequence:
            self._reanchor_window_head()
        self._write_anchor()

    def _reanchor_window_head(self) -> None:
        """Remember the first surviving record, whatever file it now lives in."""

        for path in self.segments():
            for record in _iter_lines(path):
                payload = _parse_record(record.text)
                if payload is None:
                    continue
                self._head_sequence = _as_int(payload.get("sequence"))
                self._head_prev = str(payload.get("prev_hash") or "")
                self._head_hash = str(payload.get("entry_hash") or "")
                return
        self._head_sequence = 0

    # -- anchoring ---------------------------------------------------------
    def _write_anchor(self) -> None:
        payload = {
            "schema": _ANCHOR_SCHEMA,
            "store": self._path.name,
            "sequence": self._sequence,
            "last_entry_hash": self._tip,
            "first_sequence": self._head_sequence,
            "first_prev_hash": self._head_prev,
            "first_entry_hash": self._head_hash,
            "updated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        }
        try:
            atomic_write_json(self._anchor_path, payload)
        except OSError as exc:
            # The anchor is a witness, not a gate: losing it degrades detection,
            # it does not make an audited action unsafe.
            self._fail(f"chain anchor not written: {exc}")

    def read_anchor(self) -> dict[str, Any] | None:
        return read_json_object(self._anchor_path)

    def _recover_window(self) -> None:
        """Rebuild sequence, tip and window head from what is actually on disk.

        Doing this also re-anchors the store, so any disagreement between the
        witness and the files has to be judged *before* the witness is overwritten
        -- that gap is the window in which a store could have been edited while the
        application was closed, and it is the one a restarted process would
        otherwise paper over.
        """

        records = _read_all(self.segments())
        self._recovery_note = self._witness_disagreement(records)
        if not records:
            return
        self._head_sequence = _as_int(records[0].get("sequence"))
        self._head_prev = str(records[0].get("prev_hash") or "")
        self._head_hash = str(records[0].get("entry_hash") or "")
        self._sequence = _as_int(records[-1].get("sequence"))
        self._tip = str(records[-1].get("entry_hash") or GENESIS_HASH)
        if self._sequence:
            self._write_anchor()

    # -- reading -----------------------------------------------------------
    def replay(self, *, limit: int = 500) -> list[AuditEvent]:
        """The most recent readable records, oldest first.

        A line that cannot be parsed is skipped rather than fatal: one damaged
        record should not cost the operator the rest of the trail. The damage
        itself is what :meth:`verify_chain` reports.
        """

        payloads = _read_all(self.segments())
        if limit > 0:
            payloads = payloads[-limit:]
        events: list[AuditEvent] = []
        for payload in payloads:
            rebuilt = AuditEvent.from_dict(payload)
            if rebuilt is not None:
                events.append(rebuilt)
        return events

    def verify_chain(self, *, force: bool = False) -> ChainReport:
        """Recompute every digest and link, then compare the window to the anchor.

        Verification reads the whole store, so the answer is held briefly: a page
        that refreshes every few seconds should not turn a 15 MB ceiling into a
        15 MB hash per refresh. ``force`` is for the moments where a fresh read is
        the point -- a test, or an operator asking whether the trail is intact now.
        """

        now = time.monotonic()
        if (
            not force
            and self._chain_report is not None
            and now - self._chain_report_at < self._chain_cache_seconds
        ):
            return self._chain_report
        report = self._verify_chain()
        self._chain_report = report
        self._chain_report_at = now
        return report

    def _verify_chain(self) -> ChainReport:
        """Recompute every digest and link, then compare the window to the anchor."""

        segments = self.segments()
        lines: list[tuple[Path, _Line]] = []
        for path in segments:
            lines.extend((path, record) for record in _iter_lines(path))

        expected_prev: str | None = None
        expected_sequence = 0
        entries = 0
        first: tuple[int, str, str] | None = None
        last: tuple[int, str] | None = None
        for path, record in lines:
            payload = _parse_record(record.text)
            if payload is None:
                return ChainReport(
                    entries=entries,
                    segments=len(segments),
                    ok=False,
                    first_bad_sequence=None,
                    reason=f"unreadable line in {path.name} at byte {record.offset}",
                )
            sequence = _as_int(payload.get("sequence"))
            stated_hash = str(payload.get("entry_hash") or "")
            stated_prev = str(payload.get("prev_hash") or "")
            unsigned = {k: v for k, v in payload.items() if k != "entry_hash"}
            if _digest(_canonical(unsigned)) != stated_hash:
                return ChainReport(
                    entries=entries,
                    segments=len(segments),
                    ok=False,
                    first_bad_sequence=sequence,
                    reason=f"digest does not match the line in {path.name}",
                )
            if expected_prev is None:
                first = (sequence, stated_prev, stated_hash)
                expected_prev = stated_prev
            elif stated_prev != expected_prev:
                return ChainReport(
                    entries=entries,
                    segments=len(segments),
                    ok=False,
                    first_bad_sequence=sequence,
                    reason=f"link does not match the previous record in {path.name}",
                )
            if expected_sequence and sequence != expected_sequence + 1:
                return ChainReport(
                    entries=entries,
                    segments=len(segments),
                    ok=False,
                    first_bad_sequence=sequence,
                    reason=f"sequence jumps in {path.name}",
                )
            expected_sequence = sequence
            expected_prev = stated_hash
            last = (sequence, stated_hash)
            entries += 1

        anchor = self.read_anchor()
        if anchor is None:
            if entries:
                return ChainReport(
                    entries=entries,
                    segments=len(segments),
                    ok=False,
                    first_bad_sequence=last[0] if last else None,
                    reason="chain anchor is missing while records exist",
                )
            return ChainReport(entries=0, segments=len(segments), ok=True, anchor_matches=True)

        observed = _Window(first=first, last=last, entries=entries)
        bad = self._compare_anchor(anchor, observed)
        if bad:
            reason, where = bad
            return ChainReport(
                entries=entries,
                segments=len(segments),
                ok=False,
                first_bad_sequence=where,
                reason=reason,
                anchor_sequence=_as_int(anchor.get("sequence")),
                anchor_matches=False,
            )
        return ChainReport(
            entries=entries,
            segments=len(segments),
            ok=True,
            anchor_sequence=_as_int(anchor.get("sequence")),
            anchor_matches=True,
        )

    def _witness_disagreement(self, records: list[dict[str, Any]]) -> str:
        """Compare the files to the witness, as they were before this process ran."""

        anchor = self.read_anchor()
        if anchor is None or not records:
            return ""
        first, last = records[0], records[-1]
        window = _Window(
            first=(
                _as_int(first.get("sequence")),
                str(first.get("prev_hash") or ""),
                str(first.get("entry_hash") or ""),
            ),
            last=(_as_int(last.get("sequence")), str(last.get("entry_hash") or "")),
            entries=len(records),
        )
        bad = self._compare_anchor(anchor, window)
        return "" if bad is None else bad[0]

    def _compare_anchor(
        self, anchor: dict[str, Any], window: _Window
    ) -> tuple[str, int | None] | None:
        """Where the surviving records and the witness disagree, if anywhere.

        Everything here is read from the files, never from this process's memory:
        the anchor's job is to say what the store looked like the last time it was
        written, so an in-process opinion cannot vouch for it.
        """

        anchor_sequence = _as_int(anchor.get("sequence"))
        anchor_hash = str(anchor.get("last_entry_hash") or "")
        last = window.last
        first = window.first
        if last is None or first is None:
            if anchor_sequence:
                return (
                    f"{anchor_sequence} records were witnessed, none are on disk",
                    None,
                )
            return None
        last_sequence, last_hash = last
        if last_sequence > anchor_sequence or (
            last_sequence == anchor_sequence and last_hash != anchor_hash
        ):
            # Records past the witness would mean the anchor was rewritten; a
            # shorter tail than the witness means records were removed quietly.
            return (
                "the last record on disk is not the one the anchor witnessed",
                last_sequence,
            )
        if last_sequence < anchor_sequence:
            return (
                f"{anchor_sequence - last_sequence} record(s) are missing after the "
                "last one the anchor witnessed",
                last_sequence,
            )
        first_sequence, first_prev, first_hash = first
        anchor_first = _as_int(anchor.get("first_sequence"))
        if anchor_first and first_sequence != anchor_first:
            return (
                f"the window starts at record {first_sequence}, the anchor says {anchor_first}",
                first_sequence,
            )
        if str(anchor.get("first_entry_hash") or "") != first_hash:
            return (
                "the first surviving record is not the one the anchor witnessed",
                first_sequence,
            )
        if str(anchor.get("first_prev_hash") or "") != first_prev:
            return ("the window no longer links to its witnessed predecessor", first_sequence)
        return None

    def _window_records(self, report: ChainReport) -> int:
        """How many records the window holds, from the chain the sink maintains.

        The verdict is worth caching and the count is not: a page that asked the
        cached report would tell the operator they had fewer records than the ones
        it had just written. The distance between the tip and the head is the
        number of links still on disk, so the arithmetic costs nothing. If the head
        is unknown -- a window with no readable record at its start -- the verified
        count is the only figure that can be quoted.
        """

        if self._sequence and self._head_sequence:
            return self._sequence - self._head_sequence + 1
        return report.entries

    def status(self) -> SinkStatus:
        """A snapshot for the UI: sizes, counts and the chain verdict."""

        existing = self.segments()
        used = sum(path.stat().st_size for path in existing)
        report = self.verify_chain()
        return SinkStatus(
            path=str(self._directory),
            store_name=self._path.name,
            segments=len(existing),
            bytes_used=used,
            ceiling_bytes=self.ceiling_bytes,
            records=self._window_records(report),
            degraded=self._degraded,
            last_error=self._last_error,
            chain_ok=report.ok,
            chain_note=report.describe(),
            recovery_note=self._recovery_note,
        )

    def export(self, destination: Path, *, limit: int = 0) -> int:
        """Write a readable copy for an operator, deliberately outside the store.

        Export is an explicit action with an explicit destination: it never picks
        a location itself, because a copy of the audit trail is the sensitive
        artifact, not the routine one. Records come out as they were stored --
        already redacted -- with the chain report attached, so a recipient can see
        whether what they hold is a complete window.
        """

        destination = Path(destination)
        events = self.replay(limit=limit)
        report = self.verify_chain()
        payload = {
            "exported_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "store": str(self._path),
            "records": len(events),
            "chain": {
                "ok": report.ok,
                "note": report.describe(),
                "entries": report.entries,
                "segments": report.segments,
            },
            "events": [event.to_dict() for event in events],
        }
        atomic_write_json(destination, payload)
        return len(events)

    def close(self) -> None:
        """Refresh the witness on shutdown.

        An empty view never overwrites a populated anchor: a store cleared
        underneath the sink is exactly the case the anchor exists to catch, so
        writing a fresh tip here would destroy the evidence.
        """

        if self._sequence:
            self._write_anchor()


def _iter_lines(path: Path) -> list[_Line]:
    try:
        raw = path.read_bytes()
    except OSError:
        return []
    lines: list[_Line] = []
    offset = 0
    for chunk in raw.splitlines(keepends=True):
        stripped = chunk.strip()
        if stripped:
            lines.append(_Line(text=stripped.decode("utf-8", errors="replace"), offset=offset))
        offset += len(chunk)
    return lines


def _read_all(segments: list[Path]) -> list[dict[str, Any]]:
    """Every parseable record, oldest file first."""

    payloads: list[dict[str, Any]] = []
    for path in segments:
        for record in _iter_lines(path):
            parsed = _parse_record(record.text)
            if parsed is not None:
                payloads.append(parsed)
    return payloads


def _parse_record(text: str) -> dict[str, Any] | None:
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


def _as_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0
