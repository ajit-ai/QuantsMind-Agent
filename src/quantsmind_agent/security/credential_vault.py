"""Credential vault and credential lifecycle (M3B-S1.1).

The vault is the only place in the application that may read a credential
value, and it hands that value out only to an authorised caller.

Design rules enforced here:

* A credential is addressed by a **provider-bound identifier** such as
  ``provider:openrouter:default:api_key``. Two services never share a slot.
* A credential that is *disabled* or *revoked* is not readable, so a
  disabled provider cannot be used even if something still holds the value.
* Metadata (which credentials exist, and in which state) is stored in a
  separate, secret-free file. Values live in the OS credential store.
* Legacy migration is explicit, confirmed, and reversible: the old value is
  only removed after the new location has been read back and verified.

The vault never logs a value, never returns one to a model, and never puts
one in an error message.
"""

from __future__ import annotations

import contextlib
import hmac
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from pydantic import SecretStr

from quantsmind_agent.errors import ConfigurationError, QuantsMindError
from quantsmind_agent.security.audit import AuditEventType, SecurityAuditLog
from quantsmind_agent.security.secrets import SecretStore, SecretStoreError, StorageStatus

__all__ = [
    "CredentialRecord",
    "CredentialState",
    "CredentialVault",
    "CredentialVaultError",
    "LegacyCredentialCandidate",
    "MigrationResult",
    "VaultCredentialRead",
]

_STORE_FAILURES = (SecretStoreError, ConfigurationError, OSError)


class CredentialVaultError(QuantsMindError):
    """A credential operation could not be completed safely."""

    code = "credential_vault_error"

    def __init__(
        self,
        message: str,
        user_message: str = "",
        *,
        details: dict[str, Any] | None = None,
        **extra: Any,
    ) -> None:
        super().__init__(
            message,
            user_message=user_message or message,
            details={**extra, **(details or {})},
        )


class CredentialState(StrEnum):
    """Where a credential is in its lifecycle."""

    UNCONFIGURED = "unconfigured"
    CONFIGURED = "configured"
    ACTIVE = "active"
    DISABLED = "disabled"
    ROTATED = "rotated"
    REVOKED = "revoked"


#: States in which the stored value may not be used.
_UNUSABLE_STATES = frozenset({CredentialState.DISABLED, CredentialState.REVOKED})


@dataclass(frozen=True, slots=True)
class CredentialRecord:
    """The metadata of one credential. Never holds the value."""

    credential_id: str
    provider_id: str = ""
    endpoint_id: str = ""
    state: CredentialState = CredentialState.UNCONFIGURED
    created_at: str = ""
    updated_at: str = ""

    def to_dict(self) -> dict[str, str]:
        return {
            "credential_id": self.credential_id,
            "provider_id": self.provider_id,
            "endpoint_id": self.endpoint_id,
            "state": str(self.state),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> CredentialRecord:
        return cls(
            credential_id=str(payload.get("credential_id", "")),
            provider_id=str(payload.get("provider_id", "")),
            endpoint_id=str(payload.get("endpoint_id", "")),
            state=CredentialState(str(payload.get("state", "unconfigured"))),
            created_at=str(payload.get("created_at", "")),
            updated_at=str(payload.get("updated_at", "")),
        )


@dataclass(frozen=True, slots=True)
class LegacyCredentialCandidate:
    """A credential found in the old shared slot, and who may own it."""

    legacy_slot: str
    provider_id: str
    endpoint_id: str
    unambiguous: bool
    reason: str = ""

    @property
    def requires_confirmation(self) -> bool:
        """A shared legacy slot is never migrated without an explicit decision.

        The slot was shared by every provider, so the application cannot prove
        whose key it holds. A known provider endpoint plus an explicit
        confirmation is the only safe path.
        """

        return True


@dataclass(frozen=True, slots=True)
class MigrationResult:
    """What a migration attempt did."""

    status: str
    credential_id: str = ""
    legacy_slot: str = ""
    detail: str = ""
    migrated: bool = False

    @property
    def ok(self) -> bool:
        return self.migrated


@dataclass(frozen=True, slots=True)
class VaultCredentialRead:
    """One credential read, with its lifecycle state and storage quality.

    ``status`` is ``resolved``, ``missing``, ``unusable`` or ``unavailable``.
    The value is held in a ``repr=False`` field so no diagnostic path can
    render it by accident.
    """

    status: str
    credential_id: str
    source: str = ""
    state: CredentialState = CredentialState.UNCONFIGURED
    storage_status: StorageStatus = StorageStatus.UNAVAILABLE
    reason_code: str = ""
    secret: SecretStr | None = field(default=None, repr=False)

    @property
    def usable(self) -> bool:
        return self.status == "resolved" and self.secret is not None


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class CredentialVault:
    """Provider-bound credential storage over an OS credential store.

    Parameters
    ----------
    store:
        The backend that holds the values. It must be a secure store
        (Windows Credential Manager through ``keyring``) unless
        ``require_secure`` is turned off, because the file-backed store keeps
        values in plaintext JSON.
    metadata_path:
        Where the secret-free credential metadata is kept. Optional: without
        it the vault still works, but states are not persisted.
    audit:
        Optional audit log. Every state change is recorded in it.
    """

    def __init__(
        self,
        store: SecretStore,
        *,
        metadata_path: Path | None = None,
        audit: SecurityAuditLog | None = None,
        require_secure: bool = True,
        allow_insecure_writes: bool = False,
    ) -> None:
        if require_secure and not getattr(store, "is_secure", False):
            raise CredentialVaultError(
                "the configured secret store is not an OS credential store",
                user_message=(
                    "This system has no OS credential store available, so API keys cannot "
                    "be stored safely. Install the platform credential service and try again."
                ),
                details={"store": type(store).__name__},
            )
        self._store = store
        self._metadata_path = Path(metadata_path) if metadata_path else None
        self._audit = audit
        self._records: dict[str, CredentialRecord] = {}
        self._legacy_bindings: dict[str, str] = {}
        self._writes_allowed = bool(getattr(store, "is_secure", False) or allow_insecure_writes)
        self._load_metadata()

    def _require_writable(self) -> None:
        if self._writes_allowed:
            return
        raise CredentialVaultError(
            "the secret store is not an OS credential store, so credentials will not be "
            "written to it",
            user_message=(
                "This system has no OS credential store available, so API keys cannot be "
                "stored safely. Install the platform credential service and try again."
            ),
            details={"store": type(self._store).__name__},
        )

    def _record_store_failure(
        self,
        credential_id: str,
        provider_id: str,
        endpoint_id: str,
        exc: BaseException,
    ) -> None:
        """Record a backend failure without recording what it was doing it to."""

        if self._audit is None:
            return
        self._audit.record(
            AuditEventType.SECURE_STORE_FAILED,
            provider_id=provider_id,
            credential_id=credential_id,
            endpoint_id=endpoint_id,
            decision="failed",
            reason_code=f"secret_store_error:{type(exc).__name__}",
        )

    def _store_write(
        self,
        credential_id: str,
        secret: SecretStr,
        *,
        provider_id: str,
        endpoint_id: str,
    ) -> None:
        try:
            self._store.set(credential_id, secret)
        except _STORE_FAILURES as exc:
            self._record_store_failure(credential_id, provider_id, endpoint_id, exc)
            raise CredentialVaultError(
                "the credential store refused the write",
                user_message=(
                    "The OS credential store could not save the API key. Nothing was "
                    "written to disk."
                ),
                details={
                    "reason_code": "secret_store_write_failed",
                    "store": type(self._store).__name__,
                },
            ) from exc

    # -- metadata ---------------------------------------------------------
    def _load_metadata(self) -> None:
        if self._metadata_path is None or not self._metadata_path.exists():
            return
        try:
            payload = json.loads(self._metadata_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        entries = payload.get("credentials") if isinstance(payload, dict) else None
        if isinstance(entries, list):
            for entry in entries:
                if isinstance(entry, dict):
                    record = CredentialRecord.from_dict(entry)
                    if record.credential_id:
                        self._records[record.credential_id] = record
        bindings = payload.get("legacy_bindings") if isinstance(payload, dict) else None
        if isinstance(bindings, dict):
            self._legacy_bindings = {str(key): str(value) for key, value in bindings.items()}

    def _save_metadata(self) -> None:
        if self._metadata_path is None:
            return
        payload = {
            "schema": "quantsmind.credential_metadata/1",
            "credentials": [
                record.to_dict()
                for record in sorted(self._records.values(), key=lambda item: item.credential_id)
            ],
            "legacy_bindings": dict(sorted(self._legacy_bindings.items())),
        }
        self._metadata_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._metadata_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        tmp.replace(self._metadata_path)

    def _record_for(self, credential_id: str) -> CredentialRecord:
        return self._records.get(
            credential_id,
            CredentialRecord(credential_id=credential_id, state=CredentialState.UNCONFIGURED),
        )

    def _set_state(
        self,
        credential_id: str,
        state: CredentialState,
        *,
        provider_id: str = "",
        endpoint_id: str = "",
    ) -> CredentialRecord:
        previous = self._record_for(credential_id)
        stamp = _now()
        record = CredentialRecord(
            credential_id=credential_id,
            provider_id=provider_id or previous.provider_id,
            endpoint_id=endpoint_id or previous.endpoint_id,
            state=state,
            created_at=previous.created_at or stamp,
            updated_at=stamp,
        )
        self._records[credential_id] = record
        self._save_metadata()
        return record

    # -- reads ------------------------------------------------------------
    def records(self) -> tuple[CredentialRecord, ...]:
        """Every known credential's metadata, sorted by identifier."""

        return tuple(sorted(self._records.values(), key=lambda item: item.credential_id))

    def state(self, credential_id: str) -> CredentialState:
        """The lifecycle state of one credential."""

        return self._record_for(credential_id).state

    @property
    def storage_status(self) -> StorageStatus:
        """How securely the configured backend keeps values."""

        return getattr(self._store, "storage_status", StorageStatus.UNAVAILABLE)

    @property
    def writes_allowed(self) -> bool:
        """Whether the vault may persist a credential at all."""

        return self._writes_allowed

    def _legacy_source(self, credential_id: str) -> str | None:
        """The legacy slot an operator has confirmed as this credential's source."""

        slot = self._legacy_bindings.get(credential_id)
        if not slot:
            return None
        try:
            return slot if self._store.get(slot) is not None else None
        except _STORE_FAILURES:
            return None

    def resolve(self, credential_id: str) -> VaultCredentialRead:
        """Read one credential without hiding its state or storage quality."""

        state = self.state(credential_id)
        if state in _UNUSABLE_STATES:
            reason = (
                "credential_disabled" if state is CredentialState.DISABLED else "credential_revoked"
            )
            return VaultCredentialRead(
                status="unusable",
                credential_id=credential_id,
                state=state,
                storage_status=self.storage_status,
                reason_code=reason,
            )
        try:
            stored, stored_location = self._store.read(credential_id)
        except _STORE_FAILURES as exc:
            return VaultCredentialRead(
                status="unavailable",
                credential_id=credential_id,
                state=state,
                storage_status=self.storage_status,
                reason_code=f"credential_store_unavailable:{type(exc).__name__}",
            )
        if stored is not None:
            return VaultCredentialRead(
                status="resolved",
                credential_id=credential_id,
                source="vault",
                state=state,
                storage_status=stored_location,
                reason_code="credential_found",
                secret=stored,
            )
        slot = self._legacy_bindings.get(credential_id)
        if slot:
            try:
                legacy, legacy_location = self._store.read(slot)
            except _STORE_FAILURES as exc:
                return VaultCredentialRead(
                    status="unavailable",
                    credential_id=credential_id,
                    state=state,
                    storage_status=self.storage_status,
                    reason_code=f"credential_store_unavailable:{type(exc).__name__}",
                )
            if legacy is not None:
                return VaultCredentialRead(
                    status="resolved",
                    credential_id=credential_id,
                    source="legacy",
                    state=state,
                    storage_status=legacy_location,
                    reason_code="legacy_credential_bound",
                    secret=legacy,
                )
        return VaultCredentialRead(
            status="missing",
            credential_id=credential_id,
            state=state,
            storage_status=self.storage_status,
            reason_code="credential_missing",
        )

    def has_secret(self, credential_id: str) -> bool:
        """Whether a *usable* credential is stored under this identifier."""

        return self.resolve(credential_id).usable

    def get_secret(self, credential_id: str) -> SecretStr | None:
        """The value, for authorised callers only.

        A disabled or revoked credential returns ``None``: disabling must make
        the credential unusable, not merely hidden from the settings screen.
        """

        read = self.resolve(credential_id)
        return read.secret if read.usable else None

    def confirm_legacy_ownership(
        self,
        credential_id: str,
        legacy_slot: str,
        *,
        provider_id: str = "",
        endpoint_id: str = "",
    ) -> CredentialRecord:
        """Record that a legacy credential belongs to this provider identity.

        This is the explicit decision the old shared slot needs. It moves
        nothing and deletes nothing: the legacy value stays where it is and is
        read through the binding until a real migration is performed.
        """

        try:
            present = self._store.get(legacy_slot) is not None
        except _STORE_FAILURES as exc:
            self._record_store_failure(credential_id, provider_id, endpoint_id, exc)
            raise CredentialVaultError(
                "the credential store could not be read",
                user_message="The OS credential store could not be read.",
            ) from exc
        if not present:
            raise CredentialVaultError(
                "there is no credential in the legacy slot to confirm",
                user_message="There is nothing to confirm; no legacy credential was found.",
            )
        self._legacy_bindings[credential_id] = legacy_slot
        record = self._set_state(
            credential_id,
            CredentialState.CONFIGURED,
            provider_id=provider_id,
            endpoint_id=endpoint_id,
        )
        self._save_metadata()
        if self._audit is not None:
            self._audit.record(
                AuditEventType.CREDENTIAL_MIGRATION_REQUESTED,
                provider_id=record.provider_id,
                credential_id=credential_id,
                endpoint_id=record.endpoint_id,
                decision="ownership_confirmed",
                reason_code="legacy_ownership_confirmed",
            )
        return record

    # -- writes -----------------------------------------------------------
    def set_secret(
        self,
        credential_id: str,
        secret: str | SecretStr,
        *,
        provider_id: str = "",
        endpoint_id: str = "",
        state: CredentialState = CredentialState.CONFIGURED,
    ) -> CredentialRecord:
        """Store a credential under a provider-bound identifier."""

        value = secret.get_secret_value() if isinstance(secret, SecretStr) else str(secret)
        if not value.strip():
            raise CredentialVaultError(
                "the credential is empty",
                user_message="Enter a non-empty API key.",
            )
        self._require_writable()
        self._store_write(
            credential_id,
            SecretStr(value),
            provider_id=provider_id,
            endpoint_id=endpoint_id,
        )
        # A value stored under the provider-bound slot supersedes any legacy
        # source, so the binding is dropped.
        self._legacy_bindings.pop(credential_id, None)
        record = self._set_state(
            credential_id, state, provider_id=provider_id, endpoint_id=endpoint_id
        )
        if self._audit is not None:
            self._audit.record(
                AuditEventType.CREDENTIAL_CREATED,
                provider_id=record.provider_id,
                credential_id=credential_id,
                endpoint_id=record.endpoint_id,
                decision="stored",
                reason_code="credential_created",
            )
        return record

    def rotate_secret(
        self,
        credential_id: str,
        secret: str | SecretStr,
        *,
        provider_id: str = "",
        endpoint_id: str = "",
    ) -> CredentialRecord:
        """Replace one credential's value, leaving every other credential alone."""

        previous = self._record_for(credential_id)
        value = secret.get_secret_value() if isinstance(secret, SecretStr) else str(secret)
        if not value.strip():
            raise CredentialVaultError(
                "the replacement credential is empty",
                user_message="Enter a non-empty API key.",
            )
        self._require_writable()
        self._store_write(
            credential_id,
            SecretStr(value),
            provider_id=provider_id,
            endpoint_id=endpoint_id,
        )
        self._set_state(
            credential_id,
            CredentialState.ROTATED,
            provider_id=provider_id,
            endpoint_id=endpoint_id,
        )
        record = self._set_state(
            credential_id,
            CredentialState.CONFIGURED,
            provider_id=provider_id or previous.provider_id,
            endpoint_id=endpoint_id or previous.endpoint_id,
        )
        if self._audit is not None:
            self._audit.record(
                AuditEventType.CREDENTIAL_ROTATED,
                provider_id=record.provider_id,
                credential_id=credential_id,
                endpoint_id=record.endpoint_id,
                decision="rotated",
                reason_code="credential_rotated",
            )
        return record

    def disable(self, credential_id: str) -> CredentialRecord:
        """Make a credential unusable without deleting it."""

        record = self._set_state(credential_id, CredentialState.DISABLED)
        if self._audit is not None:
            self._audit.record(
                AuditEventType.CREDENTIAL_DISABLED,
                provider_id=record.provider_id,
                credential_id=credential_id,
                endpoint_id=record.endpoint_id,
                decision="disabled",
                reason_code="credential_disabled",
            )
        return record

    def enable(self, credential_id: str) -> CredentialRecord:
        """Make a disabled credential usable again."""

        record = self._set_state(credential_id, CredentialState.CONFIGURED)
        if self._audit is not None:
            self._audit.record(
                AuditEventType.CREDENTIAL_CREATED,
                provider_id=record.provider_id,
                credential_id=credential_id,
                endpoint_id=record.endpoint_id,
                decision="enabled",
                reason_code="credential_enabled",
            )
        return record

    def delete_secret(self, credential_id: str) -> bool:
        """Remove one credential's value and mark it revoked.

        Only the named identifier is touched. A shared legacy slot is *not*
        removed by this call, because it may belong to another provider.
        """

        self._require_writable()
        try:
            removed = self._store.delete(credential_id)
        except _STORE_FAILURES as exc:
            existing = self._record_for(credential_id)
            self._record_store_failure(
                credential_id,
                existing.provider_id,
                existing.endpoint_id,
                exc,
            )
            raise CredentialVaultError(
                "the credential store refused the delete",
                user_message="The OS credential store could not remove the API key.",
                details={"reason_code": "secret_store_delete_failed"},
            ) from exc
        record = self._set_state(credential_id, CredentialState.REVOKED)
        if self._audit is not None:
            self._audit.record(
                AuditEventType.CREDENTIAL_DELETED,
                provider_id=record.provider_id,
                credential_id=credential_id,
                endpoint_id=record.endpoint_id,
                decision="deleted" if removed else "already_absent",
                reason_code="credential_deleted",
            )
        return removed

    # -- legacy migration -------------------------------------------------
    def detect_legacy_credential(
        self,
        legacy_slot: str,
        *,
        provider_id: str,
        endpoint_id: str,
        provider_is_known: bool,
    ) -> LegacyCredentialCandidate | None:
        """Look for a credential in the old shared slot.

        The legacy slot was shared by every provider, so the application
        cannot prove whose key it holds. The candidate is therefore marked
        unambiguous only when the active configuration resolves to one known
        provider; even then it still requires an explicit confirmation
        before anything moves.
        """

        try:
            stored = self._store.get(legacy_slot)
        except _STORE_FAILURES:
            return None
        if stored is None:
            return None
        reason = (
            "the legacy slot is shared by every provider, so ownership must be confirmed"
            if provider_is_known
            else "the configured endpoint is not a known provider, so ownership is ambiguous"
        )
        return LegacyCredentialCandidate(
            legacy_slot=legacy_slot,
            provider_id=provider_id,
            endpoint_id=endpoint_id,
            unambiguous=bool(provider_is_known),
            reason=reason,
        )

    def migrate_legacy_credential(
        self,
        candidate: LegacyCredentialCandidate,
        *,
        confirm: Callable[[LegacyCredentialCandidate], bool] | None = None,
        remove_source: bool = True,
    ) -> MigrationResult:
        """Move a legacy credential into its provider-bound slot, safely.

        The order matters and is the whole point of the routine:

        1. the decision is confirmed explicitly;
        2. the value is copied to the new slot;
        3. the new slot is read back and compared with the original;
        4. only then is the old slot removed, and only if asked for.

        Any failure before step 4 leaves the original credential exactly where
        it was, so the application can still authenticate.
        """

        if confirm is None:
            if self._audit is not None:
                self._audit.record(
                    AuditEventType.CREDENTIAL_MIGRATION_REQUESTED,
                    provider_id=candidate.provider_id,
                    credential_id=candidate.legacy_slot,
                    endpoint_id=candidate.endpoint_id,
                    decision="refused",
                    reason_code="confirmation_required",
                )
            return MigrationResult(
                status="confirmation_required",
                legacy_slot=candidate.legacy_slot,
                detail="the legacy credential needs an explicit confirmation before it moves",
            )

        if self._audit is not None:
            self._audit.record(
                AuditEventType.CREDENTIAL_MIGRATION_REQUESTED,
                provider_id=candidate.provider_id,
                credential_id=candidate.legacy_slot,
                endpoint_id=candidate.endpoint_id,
                decision="accepted",
                reason_code="migration_requested",
            )

        new_slot = f"provider:{candidate.provider_id}:default:api_key"
        wrote_target = False
        target_preexisted = False
        try:
            # The migration writes a credential, so the same storage policy as
            # set_secret applies. A read-only vault must not migrate.
            self._require_writable()
            if not confirm(candidate):
                raise CredentialVaultError(
                    "the migration was not confirmed",
                    user_message="The credential was left where it was.",
                )
            source = self._store.get(candidate.legacy_slot)
            if source is None:
                raise CredentialVaultError(
                    "the legacy credential disappeared before it could be migrated",
                    user_message="The credential was left where it was.",
                )

            existing = self._store.get(new_slot)
            target_preexisted = existing is not None
            if existing is not None:
                if not hmac.compare_digest(existing.get_secret_value(), source.get_secret_value()):
                    # Overwriting would destroy a credential the user may still
                    # need, so a populated destination is a refusal, not a
                    # merge. The source stays where it is.
                    raise CredentialVaultError(
                        "the destination already holds a different credential",
                        user_message=(
                            "A different credential is already stored for this provider. "
                            "Nothing was changed."
                        ),
                    )
                # Already migrated: verify and finish without rewriting.
            else:
                self._store.set(new_slot, source)
                wrote_target = True

            written = self._store.get(new_slot)
            if written is None or not hmac.compare_digest(
                written.get_secret_value(), source.get_secret_value()
            ):
                raise CredentialVaultError(
                    "the migrated credential did not read back correctly",
                    user_message="The credential was left where it was.",
                )
        except Exception as exc:
            # Roll back only a value this call created. A pre-existing target
            # is never deleted, and the legacy source is never touched here.
            if wrote_target and not target_preexisted:
                with contextlib.suppress(Exception):  # pragma: no cover - best effort
                    self._store.delete(new_slot)
            if isinstance(exc, _STORE_FAILURES):
                self._record_store_failure(
                    new_slot,
                    candidate.provider_id,
                    candidate.endpoint_id,
                    exc,
                )
            if self._audit is not None:
                self._audit.record(
                    AuditEventType.CREDENTIAL_MIGRATION_FAILED,
                    provider_id=candidate.provider_id,
                    credential_id=candidate.legacy_slot,
                    endpoint_id=candidate.endpoint_id,
                    decision="failed",
                    reason_code=type(exc).__name__,
                )
            return MigrationResult(
                status="failed",
                legacy_slot=candidate.legacy_slot,
                detail=type(exc).__name__,
            )

        self._set_state(
            new_slot,
            CredentialState.CONFIGURED,
            provider_id=candidate.provider_id,
            endpoint_id=candidate.endpoint_id,
        )
        if remove_source:
            try:
                self._store.delete(candidate.legacy_slot)
            except _STORE_FAILURES as exc:
                # The new slot is verified and usable; failing to clean up the
                # old one is a degraded outcome, not a lost credential.
                self._record_store_failure(
                    candidate.legacy_slot,
                    candidate.provider_id,
                    candidate.endpoint_id,
                    exc,
                )
        if self._audit is not None:
            self._audit.record(
                AuditEventType.CREDENTIAL_MIGRATION_COMPLETED,
                provider_id=candidate.provider_id,
                credential_id=new_slot,
                endpoint_id=candidate.endpoint_id,
                decision="migrated",
                reason_code="migration_completed",
            )
        return MigrationResult(
            status="migrated",
            credential_id=new_slot,
            legacy_slot=candidate.legacy_slot,
            migrated=True,
        )
