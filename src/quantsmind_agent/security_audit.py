"""Security audit events (M3B-S1.1).

The audit record is a *security* record, not a transcript. It answers: which
provider was selected, which endpoint was authorised, which credential
identifier was involved, what was decided and why. It never answers "what was
the key", and it is built so that it *cannot* answer that:

* the record has no field for a secret value;
* :meth:`SecurityAuditLog.record` refuses any keyword that looks like one;
* every free-text field is passed through the application redactor;
* the identifier of a credential may be recorded, its value may not.

The log is deliberately plain: a list of immutable records plus an optional
sink. Encrypting the log and making it tamper-evident is a later milestone
(M3B-S1.6) and is *not* claimed here.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from urllib.parse import urlsplit

from quantsmind_agent.logging_setup import REDACTED, redact
from quantsmind_agent.provider_identity import EndpointPolicyError, canonicalize_endpoint

__all__ = [
    "AuditEvent",
    "AuditEventType",
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
    #: A command was executed inside the sandbox boundary (M3B-S1.4).
    SANDBOX_EXECUTION = "sandbox.execution"
    #: The sandbox refused to execute, because the boundary was insufficient.
    SANDBOX_DENIED = "sandbox.denied"


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
    """One immutable security record."""

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
        }

    def to_log_line(self) -> str:
        """A single redacted line, suitable for a text log."""

        return json.dumps(self.to_dict(), sort_keys=True)


class SecurityAuditLog:
    """Collects security records and hands them to an optional sink.

    The sink receives the rendered line, not the event, so a sink cannot
    accidentally serialise a field that was never set.
    """

    def __init__(
        self,
        *,
        sink: Callable[[str], None] | None = None,
        max_events: int = 1000,
        correlation_id: str = "",
    ) -> None:
        self._sink = sink
        self._max_events = max(1, int(max_events))
        self._events: list[AuditEvent] = []
        self._correlation_id = correlation_id

    @property
    def events(self) -> tuple[AuditEvent, ...]:
        return tuple(self._events)

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
        correlation_id: str = "",
        **extra: Any,
    ) -> AuditEvent:
        """Append one record and return it.

        Any keyword that names a secret is refused: the audit log records
        identifiers and decisions, never values.
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
        )
        self._events.append(entry)
        if len(self._events) > self._max_events:
            del self._events[: len(self._events) - self._max_events]
        if self._sink is not None:
            self._sink(entry.to_log_line())
        return entry

    def to_json_lines(self) -> str:
        """Every record as newline-delimited JSON."""

        return "".join(event.to_log_line() + "\n" for event in self._events)

    def events_of_type(self, event: AuditEventType) -> tuple[AuditEvent, ...]:
        return tuple(item for item in self._events if item.event is event)
