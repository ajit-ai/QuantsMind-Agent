"""Workspace trust: explicit, per canonical root, and revocable.

The decision is a human one. It is stored locally, keyed by the canonical path
of the root, and is never inferred from repository content, configuration inside
the project, or a model. Unknown roots are *undecided*; callers treat that as
Restricted Mode until a valid stored decision or an explicit trusted flag grants
access.

The store is deliberately small and dependency-free. A corrupt file is moved
aside and treated as empty rather than preventing the application from starting.
"""

from __future__ import annotations

import os
import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path

from quantsmind_agent.diagnostics.logging import get_logger
from quantsmind_agent.errors import QuantsMindError
from quantsmind_agent.persistence.atomic import (
    atomic_write_json,
    quarantine_file,
    read_json_object,
)

__all__ = [
    "WorkspaceTrustRecord",
    "WorkspaceTrustState",
    "WorkspaceTrustStore",
    "canonical_root",
    "root_key",
]

logger = get_logger(__name__)

SCHEMA_VERSION = 1


class WorkspaceTrustState(StrEnum):
    """What the user has decided about one workspace root."""

    TRUSTED = "trusted"
    RESTRICTED = "restricted"
    UNDECIDED = "undecided"


def canonical_root(path: str | os.PathLike[str]) -> Path:
    """The absolute path a trust decision is keyed by."""

    return Path(path).expanduser().resolve(strict=False)


def root_key(path: str | os.PathLike[str]) -> str:
    """A case-normalised key, so one root cannot be trusted twice on Windows."""

    return os.path.normcase(str(canonical_root(path)))


@dataclass(frozen=True, slots=True)
class WorkspaceTrustRecord:
    """One stored decision."""

    root: str
    state: WorkspaceTrustState
    decided_at: str

    def to_dict(self) -> dict[str, str]:
        return {
            "path": self.root,
            "state": self.state.value,
            "decided_at": self.decided_at,
        }


class WorkspaceTrustStore:
    """Reads and writes per-root trust decisions.

    The store never raises for a missing or corrupt file: startup must not fail
    because a small preference file is unreadable. It also never silently
    trusts a root; an absent record is :attr:`WorkspaceTrustState.UNDECIDED`.
    """

    def __init__(self, path: str | os.PathLike[str]) -> None:
        self.path = Path(path)
        self._lock = threading.RLock()
        self._records: dict[str, WorkspaceTrustRecord] | None = None
        #: Set when the file belongs to a newer schema. Saves are refused so a
        #: newer application's state is not overwritten by this one.
        self._future_schema = False

    # -- reading -----------------------------------------------------------
    def _load(self) -> dict[str, WorkspaceTrustRecord]:
        with self._lock:
            if self._records is not None:
                return self._records
            self._records = {}
            payload = read_json_object(self.path)
            if payload is None:
                if self.path.exists():
                    moved = quarantine_file(self.path, reason="corrupt")
                    logger.warning("Workspace trust file was corrupt; moved to %s", moved)
                return self._records
            version = payload.get("schema_version")
            if not isinstance(version, int) or version > SCHEMA_VERSION:
                self._future_schema = True
                logger.warning(
                    "Workspace trust file uses schema %r; this version reads schema %d",
                    version,
                    SCHEMA_VERSION,
                )
                return self._records
            roots = payload.get("roots")
            if not isinstance(roots, dict):
                return self._records
            for key, raw in roots.items():
                if not isinstance(raw, dict):
                    continue
                root = raw.get("path")
                state = raw.get("state")
                if not isinstance(root, str) or not root:
                    continue
                try:
                    parsed = WorkspaceTrustState(str(state))
                except ValueError:
                    continue
                decided_at = raw.get("decided_at")
                self._records[str(key)] = WorkspaceTrustRecord(
                    root=root,
                    state=parsed,
                    decided_at=str(decided_at or ""),
                )
            return self._records

    def state_for(self, path: str | os.PathLike[str]) -> WorkspaceTrustState:
        """The stored decision, or ``UNDECIDED`` when there is none."""

        record = self._load().get(root_key(path))
        return record.state if record is not None else WorkspaceTrustState.UNDECIDED

    def record_for(self, path: str | os.PathLike[str]) -> WorkspaceTrustRecord | None:
        return self._load().get(root_key(path))

    def records(self) -> list[WorkspaceTrustRecord]:
        """Every stored decision, newest path order for a stable Security Center."""

        return sorted(self._load().values(), key=lambda record: record.root.lower())

    # -- writing -----------------------------------------------------------
    def decide(self, path: str | os.PathLike[str], *, trusted: bool) -> WorkspaceTrustRecord:
        """Store an explicit decision for ``path``.

        Raises if the decision cannot be persisted; a trust decision that did not
        reach the trust file is not treated as made.
        """

        record = WorkspaceTrustRecord(
            root=str(canonical_root(path)),
            state=WorkspaceTrustState.TRUSTED if trusted else WorkspaceTrustState.RESTRICTED,
            decided_at=datetime.now(tz=UTC).isoformat(timespec="seconds"),
        )
        self._put(record)
        return record

    def revoke(self, path: str | os.PathLike[str]) -> bool:
        """Remove a decision. Returns whether one existed.

        A failed write restores the in-memory record and raises, so a deletion
        that did not reach the file is never reported as successful revocation.
        """

        with self._lock:
            records = self._load()
            key = root_key(path)
            removed = records.pop(key, None)
            if removed is None:
                return False
            try:
                self._write(strict=True)
            except QuantsMindError:
                records[key] = removed
                raise
            return True

    def _put(self, record: WorkspaceTrustRecord) -> None:
        with self._lock:
            records = self._load()
            if self._future_schema:
                # Refuse the decision without pretending it was stored. No
                # in-memory trust is granted either.
                logger.warning("Refusing to store a workspace trust decision: newer file")
                return
            key = root_key(record.root)
            previous = records.get(key)
            records[key] = record
            try:
                self._write(strict=True)
            except QuantsMindError:
                if previous is None:
                    records.pop(key, None)
                else:
                    records[key] = previous
                raise

    def _write(self, *, strict: bool = False) -> None:
        if self._future_schema:
            # A newer file is refused, not overwritten and never read as
            # trusted. Callers that require persistence check for a valid
            # record afterwards or receive an OSError-shaped failure.
            logger.warning("Refusing to overwrite a newer workspace trust file")
            return
        records = self._records or {}
        payload = {
            "schema_version": SCHEMA_VERSION,
            "roots": {key: record.to_dict() for key, record in records.items()},
        }
        try:
            atomic_write_json(self.path, payload)
        except OSError as exc:  # pragma: no cover - disk failure
            logger.warning("Could not persist workspace trust: %s", exc)
            if strict:
                raise QuantsMindError(
                    f"Could not persist workspace trust: {exc}",
                    user_message=(
                        "The stored workspace trust decision could not be removed. "
                        "Nothing was changed; check that the application data folder "
                        "is writable and try again."
                    ),
                    details={"path": str(self.path)},
                    cause=exc,
                ) from exc
