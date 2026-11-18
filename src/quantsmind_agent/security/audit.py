"""Security audit events (M3B-S1.1).

The audit record is a *security* record, not a transcript. It answers: which
provider was selected, which endpoint was authorised, which credential
identifier was involved, what was decided and why. It never answers "what was
the key", and it is built so that it *cannot* answer that:

* the record has no field for a secret value;
* :meth:`SecurityAuditLog.record` refuses any keyword that looks like one;
* every free-text field is passed through the application redactor;
* the identifier of a credential may be recorded, its value may not.

Since v0.11.0 the records also reach a disk sink
(:class:`quantsmind_agent.security.audit_sink.RotatingAuditSink`), which chains
each line to the one before it. That makes a modified or removed middle line
**detectable**. It is detection, not proof: a writer who can open the file can
also truncate the tail, and nothing here is tamper-**proof**.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable
from urllib.parse import urlsplit

from quantsmind_agent.diagnostics.logging import REDACTED, redact
from quantsmind_agent.security.provider_identity import EndpointPolicyError, canonicalize_endpoint

if TYPE_CHECKING:
    from quantsmind_agent.security.audit_sink import SinkStatus

__all__ = [
    "AuditArchive",
    "AuditEvent",
    "AuditEventType",
    "AuditPersistenceError",
    "AuditSink",
    "SecurityAuditLog",
]


class AuditEventType(StrEnum):
    """The event names the security record is written in."""

    CREDENTIAL_CREATED = "credential.created"
    CREDENTIAL_MIGRATION_REQUESTED = "credential.migration_requested"
    CREDENTIAL_MIGRATION_COMPLETED = "credential.migration_completed"
    CREDENTIAL_MIGRATION_FAILED = "credential.migration_failed"
    CREDENTIAL_ROTATED = "credential.rotated"
    CREDENTIAL_DISABLED = "credential.disabled"
    CREDENTIAL_DELETED = "credential.deleted"
    PROVIDER_SELECTED = "provider.selected"
    ENDPOINT_AUTHORIZED = "endpoint.authorized"
    ENDPOINT_DENIED = "endpoint.denied"
    AUTHENTICATION_SUCCEEDED = "authentication.succeeded"
    AUTHENTICATION_FAILED = "authentication.failed"
    PROVIDER_IDENTITY_MISMATCH = "provider.identity_mismatch"
    SECURE_STORE_FAILED = "secure_store.failed"
    SECURITY_POLICY_DENIED = "security.policy_denied"
    #: The user trusted, restricted or revoked a workspace root (v0.10.0).
    WORKSPACE_TRUST_CHANGED = "workspace.trust_changed"
    #: A command was executed inside the sandbox boundary (M3B-S1.4).
    SANDBOX_EXECUTION = "sandbox.execution"
    #: The sandbox refused to execute, because the boundary was insufficient.
    SANDBOX_DENIED = "sandbox.denied"
    #: A human answered an approval prompt, or policy decided without asking.
    APPROVAL_DECISION = "approval.decision"
    #: A command ran through the controlled command path.
    COMMAND_EXECUTED = "command.executed"
    #: A file inside the workspace changed on disk.
    FILE_MUTATED = "file.mutated"
    #: A ChangeSet was proposed.
    CHANGESET_CREATED = "changeset.created"
    #: A ChangeSet was applied to disk.
    CHANGESET_APPLIED = "changeset.applied"
    #: A ChangeSet apply was denied by policy or permission.
    CHANGESET_APPLY_DENIED = "changeset.apply_denied"
    #: A ChangeSet apply failed after starting.
    CHANGESET_APPLY_FAILED = "changeset.apply_failed"
    #: A ChangeSet was rolled back.
    CHANGESET_ROLLED_BACK = "changeset.rolled_back"
    #: An interrupted transaction was reconciled at startup.
    CHANGESET_RECOVERY = "changeset.recovery"
    #: A persisted audit write failed, so records between the failure and this
    #: marker are in memory only. Emitted by the sink, never by a feature.
    AUDIT_DEGRADED = "audit.degraded"


#: Keyword fragments that must never appear as a field of an audit record.
#: A caller that tries to record one gets an error instead of a leak.
_FORBIDDEN_FIELDS = (
    "secret",
    "value",
    "token",
    "api_key",
    "apikey",
    "password",
    "authorization",
    "bearer",
    "credential_value",
    "private_key",
)

_URL_RE = re.compile(r"https?://[^\s\"'<>]+", re.IGNORECASE)

#: How many persistence-failure reasons the log keeps for the UI.
_MAX_LOST_REASONS = 20


def _strip_url_secrets(value: str) -> str:
    """Remove user-info, query and fragment from every URL in ``value``.

    An endpoint may legitimately be recorded; a credential embedded in one may
    not. Rebuilding from the parsed pieces drops ``user:pass@``, ``?token=``
    and ``#fragment`` rather than trying to recognise each parameter name.
    """

    def _clean(match: re.Match[str]) -> str:
        raw = match.group(0)
        try:
            parts = urlsplit(raw)
            host = parts.hostname or ""
            port = f":{parts.port}" if parts.port else ""
        except (ValueError, UnicodeError):
            return REDACTED
        if not host:
            return REDACTED
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        return f"{parts.scheme}://{host}{port}{parts.path or ''}"

    return _URL_RE.sub(_clean, value)


def _safe_text(value: Any) -> str:
    """Redact inline credentials and URL-borne secrets from a named field."""

    return redact(_strip_url_secrets(str(value)))


def _safe_endpoint(value: Any) -> str:
    """Record an endpoint as its canonical id, never as a raw secret-bearing URL."""

    if not value:
        return ""
    try:
        return canonicalize_endpoint(str(value)).endpoint_id
    except (EndpointPolicyError, ValueError):
        return _safe_text(value)


@dataclass(frozen=True, slots=True)
class AuditEvent:
    """One immutable security record.

    The field list is the whole vocabulary: a value has nowhere to go, because
    every field is an identifier, a decision or a redacted phrase. ``subject``
    names what an action touched -- a workspace-relative path, or an argv summary
    -- and is scrubbed like every other field.
    """

    event: AuditEventType
    timestamp: str
    correlation_id: str = ""
    provider_id: str = ""
    model: str = ""
    credential_id: str = ""
    endpoint_id: str = ""
    decision: str = ""
    reason_code: str = ""
    detail: str = ""
    subject: str = ""
    outcome: str = ""
    changeset_id: str = ""
    task_id: str = ""

    def to_dict(self) -> dict[str, str]:
        """A JSON-ready mapping. Contains identifiers, never values."""

        return {
            "event": str(self.event),
            "timestamp": self.timestamp,
            "correlation_id": self.correlation_id,
            "provider_id": self.provider_id,
            "model": self.model,
            "credential_id": self.credential_id,
            "endpoint_id": self.endpoint_id,
            "decision": self.decision,
            "reason_code": self.reason_code,
            "detail": self.detail,
            "subject": self.subject,
            "outcome": self.outcome,
            "changeset_id": self.changeset_id,
            "task_id": self.task_id,
        }

    def to_log_line(self) -> str:
        """A single redacted line, suitable for a text log."""

        return json.dumps(self.to_dict(), sort_keys=True)

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> AuditEvent | None:
        """Rebuild a record from a persisted line, or return ``None``.

        Reading is deliberately forgiving: a line whose event name the running
        build does not know is a *newer* record, not a corrupt one, and the
        caller reports it as unreadable rather than dropping the whole file.
        """

        name = payload.get("event")
        if not isinstance(name, str):
            return None
        try:
            kind = AuditEventType(name)
        except ValueError:
            return None
        fields = {
            field: _text(payload.get(field))
            for field in (
                "timestamp",
                "correlation_id",
                "provider_id",
                "model",
                "credential_id",
                "endpoint_id",
                "decision",
                "reason_code",
                "detail",
                "subject",
                "outcome",
                "changeset_id",
                "task_id",
            )
        }
        return cls(event=kind, **fields)


def _text(value: Any) -> str:
    """A persisted field as text, with anything non-string rendered safely."""

    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return _safe_text(value)


class AuditPersistenceError(RuntimeError):
    """A sink could not persist a record.

    Raised only where the failure must be felt: before an action's first byte,
    an unrecordable action is refused. Call the same record after the fact and
    the log keeps the event in memory, flags itself degraded and says so.
    """


@runtime_checkable
class AuditSink(Protocol):
    """Where a record goes when it must outlive the process.

    ``durable`` is the caller's way of saying "this line has to be on disk
    before I do the thing it describes". A sink that cannot honour that raises
    :class:`AuditPersistenceError`; a sink given a post-hoc record keeps the
    failure as a flag rather than turning a completed action into an error.
    """

    def append(self, event: AuditEvent, *, durable: bool = False) -> None:
        """Persist ``event``."""

    @property
    def degraded(self) -> bool:
        """True when records are being lost."""


@runtime_checkable
class AuditArchive(Protocol):
    """A sink that can also hand records back.

    Writing and reading are separate capabilities on purpose: the log needs only
    a pipe, while a view that shows records from earlier sessions needs a store.
    A caller asks with ``isinstance`` instead of assuming every sink can read.
    """

    def replay(self, *, limit: int = 500) -> list[AuditEvent]:
        """The records still held, oldest first, up to ``limit`` newest."""

    def status(self) -> SinkStatus:
        """Sizes, counts and the chain verdict for the store."""


class SecurityAuditLog:
    """Collects security records and hands them to an optional persistent sink.

    The sink sees the immutable record, not a pre-rendered line, so it can chain
    and sequence what it writes without losing the fields the record was built
    from. In-memory the log is a bounded ring; on disk the sink decides retention.
    """

    def __init__(
        self,
        *,
        sink: AuditSink | None = None,
        max_events: int = 1000,
        correlation_id: str = "",
    ) -> None:
        self._sink = sink
        self._max_events = max(1, int(max_events))
        self._events: list[AuditEvent] = []
        self._correlation_id = correlation_id
        #: A post-hoc write failed, so records are in memory only since.
        self._lost: list[str] = []

    @property
    def events(self) -> tuple[AuditEvent, ...]:
        return tuple(self._events)

    @property
    def sink(self) -> AuditSink | None:
        return self._sink

    @property
    def degraded(self) -> bool:
        """Whether anything the log was asked to record did not reach disk."""

        return bool(self._lost) or (self._sink is not None and self._sink.degraded)

    @property
    def lost_records(self) -> tuple[str, ...]:
        """Why the log is degraded, newest last. Reasons, never payloads."""

        return tuple(self._lost)

    def set_correlation_id(self, correlation_id: str) -> None:
        """Bind subsequent records to one session or request."""

        self._correlation_id = correlation_id

    def record(
        self,
        event: AuditEventType,
        *,
        provider_id: str = "",
        model: str = "",
        credential_id: str = "",
        endpoint_id: str = "",
        decision: str = "",
        reason_code: str = "",
        detail: str = "",
        subject: str = "",
        outcome: str = "",
        changeset_id: str = "",
        task_id: str = "",
        correlation_id: str = "",
        durable: bool = False,
        **extra: Any,
    ) -> AuditEvent:
        """Append one record and return it.

        Any keyword that names a secret is refused: the audit log records
        identifiers and decisions, never values.

        ``durable`` marks the records an action depends on -- an approval, a
        journal entry -- for which "could not be written" means "does not
        happen". Those raise :class:`AuditPersistenceError`. A post-hoc record
        does not: the action is already real, so losing its line is reported, not
        turned into a second failure.
        """

        offending = sorted(
            name
            for name in extra
            if any(fragment in name.lower() for fragment in _FORBIDDEN_FIELDS)
        )
        if offending:
            raise ValueError(
                "an audit record must not carry credential material; refused field(s): "
                + ", ".join(offending)
            )

        entry = AuditEvent(
            event=event,
            timestamp=datetime.now(UTC).isoformat(timespec="seconds"),
            correlation_id=_safe_text(correlation_id or self._correlation_id),
            provider_id=_safe_text(provider_id),
            model=_safe_text(model),
            credential_id=_safe_text(credential_id),
            endpoint_id=_safe_endpoint(endpoint_id),
            decision=_safe_text(decision),
            reason_code=_safe_text(reason_code),
            detail=_safe_text(detail),
            subject=_safe_text(subject),
            outcome=_safe_text(outcome),
            changeset_id=_safe_text(changeset_id),
            task_id=_safe_text(task_id),
        )
        self._events.append(entry)
        if len(self._events) > self._max_events:
            del self._events[: len(self._events) - self._max_events]
        if self._sink is not None:
            self._persist(entry, durable=durable)
        return entry

    def _persist(self, entry: AuditEvent, *, durable: bool) -> None:
        """Write through the sink, applying the split failure rule.

        A durable record that cannot be written raises: the caller's action has
        to stop. A post-hoc record is kept in memory, the reason is held for the
        UI, and the operation that already happened is not turned into a failure.
        """

        sink = self._sink
        if sink is None:
            return
        try:
            sink.append(entry, durable=durable)
        except AuditPersistenceError as exc:
            if durable:
                raise
            reason = _safe_text(exc)
            self._lost.append(reason)
            del self._lost[:-_MAX_LOST_REASONS]

    def to_json_lines(self) -> str:
        """Every record as newline-delimited JSON."""

        return "".join(event.to_log_line() + "\n" for event in self._events)

    def events_of_type(self, event: AuditEventType) -> tuple[AuditEvent, ...]:
        return tuple(item for item in self._events if item.event is event)
