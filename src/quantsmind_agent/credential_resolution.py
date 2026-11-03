"""The single authoritative credential-resolution decision (Phase 0.8).

Before this module there were two answers to "which key will be sent?":

* the request path read the credential vault, which understands the
  configured/active/disabled/revoked lifecycle;
* readiness, the Settings hint and ``--check-config`` read
  ``ConfigManager``, which read the secret store and the environment directly
  and knew nothing about the lifecycle.

The two could disagree -- a disabled key could still be reported as present --
and the environment fallback existed on one path only. This module replaces
both with one result model and one resolution order:

1. a provider/endpoint identity mismatch is refused before anything is read;
2. a disabled or revoked credential is refused, and no environment variable
   may silently take its place;
3. the provider-bound vault slot, then a *confirmed* legacy binding;
4. the provider-specific environment variable for that one provider;
5. the generic ``QM_API_KEY``-style variable only for the provider that owns
   it (the default provider), never for a different service.

Every consumer -- provider construction, the gateway, readiness, Settings,
diagnostics and the security panel -- asks this service. The value is carried
in a ``repr=False`` field; :meth:`CredentialResolution.diagnostic` and
:meth:`CredentialResolution.masked` are the only supported ways to describe a
resolution, and neither can reveal it.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from pydantic import SecretStr

from quantsmind_agent.credential_vault import CredentialState, CredentialVault
from quantsmind_agent.errors import ConfigurationError
from quantsmind_agent.provider_identity import ProviderIdentity
from quantsmind_agent.secrets import (
    SecretStore,
    SecretStoreError,
    StorageStatus,
    mask_secret,
)

__all__ = [
    "CredentialResolution",
    "CredentialResolutionService",
    "CredentialSource",
    "CredentialStatus",
]

_STORE_FAILURES = (SecretStoreError, ConfigurationError, OSError)


class CredentialSource(StrEnum):
    """Where the value came from."""

    VAULT = "vault"
    LEGACY = "legacy"
    ENVIRONMENT = "environment"
    NONE = "none"


class CredentialStatus(StrEnum):
    """Whether a usable credential was resolved, and why not if it was not."""

    RESOLVED = "resolved"
    DEGRADED = "degraded"
    """Usable, but kept in a store weaker than the selected secure backend."""

    MISSING = "missing"
    REJECTED = "rejected"
    """The credential exists but policy refuses to use it."""

    UNAVAILABLE = "unavailable"
    """The store could not be read, so the answer is unknown."""

    NOT_REQUIRED = "not_required"
    """The endpoint is local and needs no credential."""


@dataclass(frozen=True, slots=True)
class CredentialResolution:
    """One credential decision. Never renders the value."""

    provider_id: str
    credential_id: str
    status: CredentialStatus
    source: CredentialSource = CredentialSource.NONE
    storage_status: StorageStatus = StorageStatus.NOT_APPLICABLE
    reason_code: str = ""
    detail: str = ""
    authorization_status: str = "not_checked"
    required: bool = True
    _secret: SecretStr | None = field(default=None, repr=False)

    @property
    def usable(self) -> bool:
        """Whether a request may proceed with this resolution."""

        return self.status in (
            CredentialStatus.RESOLVED,
            CredentialStatus.DEGRADED,
            CredentialStatus.NOT_REQUIRED,
        )

    @property
    def value(self) -> str | None:
        """The plain value, for the request path only."""

        return self._secret.get_secret_value() if self._secret is not None else None

    def masked(self) -> str:
        """A UI- and log-safe description of the value."""

        return mask_secret(self.value)

    def diagnostic(self) -> dict[str, Any]:
        """A structured, secret-free description of the decision."""

        return {
            "provider_id": self.provider_id,
            "credential_id": self.credential_id,
            "status": self.status.value,
            "source": self.source.value,
            "storage_status": self.storage_status.value,
            "reason_code": self.reason_code,
            "detail": self.detail,
            "authorization_status": self.authorization_status,
            "required": self.required,
        }

    @classmethod
    def unavailable(
        cls,
        *,
        provider_id: str,
        credential_id: str,
        reason_code: str,
        detail: str = "",
    ) -> CredentialResolution:
        return cls(
            provider_id=provider_id,
            credential_id=credential_id,
            status=CredentialStatus.UNAVAILABLE,
            reason_code=reason_code,
            detail=detail,
        )


class CredentialResolutionService:
    """Resolve exactly one credential for exactly one provider identity.

    Parameters
    ----------
    vault:
        The lifecycle-aware credential vault. When absent (a bare
        ``ConfigManager`` used outside the composition root), the service reads
        the supplied secret store directly and never claims lifecycle states
        it cannot know.
    secret_store:
        Used only when ``vault`` is absent.
    environ:
        The environment mapping. ``None`` reads ``os.environ`` at call time, so
        tests can change it without rebuilding the service.
    provider_env_vars:
        Provider id to environment variable name. A name belongs to one
        service; it is never consulted for another.
    generic_env_owner:
        The single provider allowed to use ``configured_env_name``.
    """

    def __init__(
        self,
        vault: CredentialVault | None = None,
        *,
        secret_store: SecretStore | None = None,
        environ: Mapping[str, str] | None = None,
        provider_env_vars: Mapping[str, str] | None = None,
        generic_env_owner: str = "",
    ) -> None:
        self._vault = vault
        self._store = secret_store
        self._environ = environ
        self._provider_env_vars = dict(provider_env_vars or {})
        self._generic_env_owner = generic_env_owner

    def _environment(self) -> Mapping[str, str]:
        return self._environ if self._environ is not None else os.environ

    @property
    def storage_status(self) -> StorageStatus:
        """How securely the backing store keeps values."""

        return self._storage_status()

    def _storage_status(self) -> StorageStatus:
        if self._vault is not None:
            return self._vault.storage_status
        if self._store is not None:
            return getattr(self._store, "storage_status", StorageStatus.UNAVAILABLE)
        return StorageStatus.NOT_APPLICABLE

    def _read_stored(
        self, credential_id: str
    ) -> tuple[str, SecretStr | None, str, str, StorageStatus | None]:
        """Return ``(status, value, source, reason_code, storage)``."""

        if self._vault is not None:
            read = self._vault.resolve(credential_id)
            return (
                read.status,
                read.secret,
                read.source,
                read.reason_code,
                read.storage_status,
            )
        if self._store is None:
            return "missing", None, "", "credential_missing", None
        try:
            value, location = self._store.read(credential_id)
        except _STORE_FAILURES as exc:
            return (
                "unavailable",
                None,
                "",
                f"credential_store_unavailable:{type(exc).__name__}",
                None,
            )
        if value is not None:
            return "resolved", value, "vault", "credential_found", location
        return "missing", None, "", "credential_missing", None

    def resolve(
        self,
        identity: ProviderIdentity,
        *,
        required: bool = True,
        configured_env_name: str = "QM_API_KEY",
        authorization_status: str = "not_checked",
    ) -> CredentialResolution:
        """Resolve the credential for ``identity``, or explain why not."""

        credential_id = identity.credential_id
        if identity.identity_mismatch:
            return CredentialResolution(
                provider_id=identity.provider_id,
                credential_id=credential_id,
                status=CredentialStatus.REJECTED,
                storage_status=self._storage_status(),
                reason_code="provider_endpoint_mismatch",
                detail=("the configured provider identity does not belong to this endpoint"),
                authorization_status=authorization_status,
                required=required,
            )

        state = (
            self._vault.state(credential_id)
            if self._vault is not None
            else CredentialState.UNCONFIGURED
        )
        if state in (CredentialState.DISABLED, CredentialState.REVOKED):
            return CredentialResolution(
                provider_id=identity.provider_id,
                credential_id=credential_id,
                status=CredentialStatus.REJECTED,
                storage_status=self._storage_status(),
                reason_code=(
                    "credential_disabled"
                    if state is CredentialState.DISABLED
                    else "credential_revoked"
                ),
                detail="the credential is present but the operator disabled or revoked it",
                authorization_status=authorization_status,
                required=required,
            )

        read_status, value, source, reason, location = self._read_stored(credential_id)
        if read_status == "unavailable":
            # A store failure must stay visible. Falling through to the
            # environment would silently substitute a different credential
            # for the one the operator configured.
            return CredentialResolution(
                provider_id=identity.provider_id,
                credential_id=credential_id,
                status=CredentialStatus.UNAVAILABLE,
                storage_status=self._storage_status(),
                reason_code=reason,
                detail="the credential store could not be read",
                authorization_status=authorization_status,
                required=required,
            )

        resolved_source = CredentialSource.LEGACY if source == "legacy" else CredentialSource.VAULT
        if value is not None:
            return self._resolved(
                identity,
                credential_id,
                value,
                resolved_source,
                authorization_status,
                required,
                location if location is not None else self._storage_status(),
            )

        env_value, env_reason = self._environment_value(identity, configured_env_name)
        if env_value is not None:
            return CredentialResolution(
                provider_id=identity.provider_id,
                credential_id=credential_id,
                status=CredentialStatus.RESOLVED,
                source=CredentialSource.ENVIRONMENT,
                storage_status=StorageStatus.NOT_APPLICABLE,
                reason_code="environment_credential",
                detail=env_reason,
                authorization_status=authorization_status,
                required=required,
                _secret=SecretStr(env_value),
            )

        if not required:
            return CredentialResolution(
                provider_id=identity.provider_id,
                credential_id=credential_id,
                status=CredentialStatus.NOT_REQUIRED,
                storage_status=self._storage_status(),
                reason_code="credential_not_required",
                detail="this endpoint does not require a credential",
                authorization_status=authorization_status,
                required=False,
            )

        return CredentialResolution(
            provider_id=identity.provider_id,
            credential_id=credential_id,
            status=CredentialStatus.MISSING,
            storage_status=self._storage_status(),
            reason_code="credential_missing",
            detail="no stored or environment credential exists for this provider",
            authorization_status=authorization_status,
            required=True,
        )

    def _resolved(
        self,
        identity: ProviderIdentity,
        credential_id: str,
        value: SecretStr,
        source: CredentialSource,
        authorization_status: str,
        required: bool,
        storage: StorageStatus,
    ) -> CredentialResolution:
        degraded = storage in (StorageStatus.PLAINTEXT, StorageStatus.DEGRADED)
        return CredentialResolution(
            provider_id=identity.provider_id,
            credential_id=credential_id,
            status=(CredentialStatus.DEGRADED if degraded else CredentialStatus.RESOLVED),
            source=source,
            storage_status=storage,
            reason_code=(
                "credential_found_in_legacy_slot"
                if source is CredentialSource.LEGACY
                else "credential_found"
            ),
            detail=(
                "the credential is stored in a plaintext file"
                if storage is StorageStatus.PLAINTEXT
                else ""
            ),
            authorization_status=authorization_status,
            required=required,
            _secret=value,
        )

    def _environment_value(
        self, identity: ProviderIdentity, configured_env_name: str
    ) -> tuple[str | None, str]:
        """The one environment variable this identity is allowed to read."""

        provider_env = self._provider_env_vars.get(identity.provider_id)
        if provider_env:
            value = self._environment().get(provider_env, "")
            if value.strip():
                return value, f"provider-specific variable {provider_env}"
        if (
            self._generic_env_owner
            and identity.provider_id == self._generic_env_owner
            and configured_env_name
        ):
            value = self._environment().get(configured_env_name, "")
            if value.strip():
                return value, f"configured variable {configured_env_name}"
        return None, ""
