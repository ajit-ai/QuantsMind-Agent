"""Security controller for the provider settings (M3B-S1.2).

This module holds the *rules* of the secure provider settings, with no Qt
dependency at all. The widgets in :mod:`quantsmind_agent.gui.security.center`
are a thin view over this class, which means every rule can be tested
headlessly and the same rules apply whichever surface asks.

What it owns
------------
* the catalogue of provider identities the user may choose from;
* the credential status of each identity, never a credential value;
* the credential operations (add, replace, disable, re-enable, remove);
* the legacy-credential migration confirmation flow;
* the endpoint authorisation status, taken from the production gateway;
* a filtered, sanitised view of the security audit trail.

The rules it enforces
---------------------
* A credential belongs to one provider identity. Selecting a different
  provider never surfaces, reuses or transfers the previous one's credential.
* A local model server never receives a cloud credential.
* A legacy credential is never assumed to belong to the provider that happens
  to be selected. Migration requires an explicit provider choice *and* an
  explicit confirmation action.
* A destination that already holds a credential is never overwritten silently.
* The gateway is the final authority. This controller asks it; it never
  second-guesses it.
* If secure storage is unavailable, credential writes fail closed.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

from quantsmind_agent.app.settings import ConfigManager, ProviderSettings
from quantsmind_agent.security.audit import (
    AuditArchive,
    AuditEvent,
    AuditEventType,
    SecurityAuditLog,
)
from quantsmind_agent.security.audit_sink import SinkStatus
from quantsmind_agent.security.auth_gateway import (
    AuthGateway,
    AuthorizationDecision,
)
from quantsmind_agent.security.credential_resolution import (
    CredentialResolution,
    CredentialSource,
)
from quantsmind_agent.security.credential_resolution import (
    CredentialStatus as ResolutionStatus,
)
from quantsmind_agent.security.credential_vault import (
    CredentialVault,
    CredentialVaultError,
    LegacyCredentialCandidate,
    MigrationResult,
)
from quantsmind_agent.security.provider_identity import (
    EndpointPolicyError,
    ProviderIdentity,
    ProviderTrust,
    canonicalize_endpoint,
    resolve_provider_identity,
)

__all__ = [
    "CredentialStatus",
    "EndpointState",
    "EndpointStatus",
    "ProviderOption",
    "SecurityController",
    "provider_catalog",
]


class CredentialStatus(StrEnum):
    """What the user is told about one provider's credential."""

    NOT_CONFIGURED = "not_configured"
    CONFIGURED = "configured"
    ACTIVE = "active"
    DISABLED = "disabled"
    MIGRATION_REQUIRED = "migration_required"
    SECURE_STORAGE_UNAVAILABLE = "secure_storage_unavailable"
    ENVIRONMENT = "environment"
    DEGRADED = "degraded"
    REJECTED = "rejected"
    UNAVAILABLE = "unavailable"

    @property
    def label(self) -> str:
        return {
            CredentialStatus.NOT_CONFIGURED: "Not configured",
            CredentialStatus.CONFIGURED: "Configured",
            CredentialStatus.ACTIVE: "Active",
            CredentialStatus.DISABLED: "Disabled",
            CredentialStatus.MIGRATION_REQUIRED: "Migration required",
            CredentialStatus.SECURE_STORAGE_UNAVAILABLE: "Secure storage unavailable",
            CredentialStatus.ENVIRONMENT: "From environment",
            CredentialStatus.DEGRADED: "Configured (storage degraded)",
            CredentialStatus.REJECTED: "Unusable",
            CredentialStatus.UNAVAILABLE: "Credential store unavailable",
        }[self]


class EndpointState(StrEnum):
    """How the endpoint behind the current configuration is classified."""

    APPROVED_CLOUD = "approved_cloud"
    APPROVED_LOOPBACK = "approved_loopback"
    CUSTOM_REQUIRES_AUTHORIZATION = "custom_requires_authorization"
    BLOCKED_INSECURE = "blocked_insecure"
    BLOCKED_PRIVATE = "blocked_private"
    INVALID = "invalid"
    IDENTITY_MISMATCH = "identity_mismatch"
    UNKNOWN = "unknown"

    @property
    def label(self) -> str:
        return {
            EndpointState.APPROVED_CLOUD: "Approved cloud endpoint",
            EndpointState.APPROVED_LOOPBACK: "Approved loopback endpoint",
            EndpointState.CUSTOM_REQUIRES_AUTHORIZATION: "Custom endpoint requiring authorization",
            EndpointState.BLOCKED_INSECURE: "Blocked insecure endpoint",
            EndpointState.BLOCKED_PRIVATE: "Blocked private-network destination",
            EndpointState.INVALID: "Invalid endpoint configuration",
            EndpointState.IDENTITY_MISMATCH: "Provider identity mismatch",
            EndpointState.UNKNOWN: "Unknown",
        }[self]


@dataclass(frozen=True, slots=True)
class EndpointStatus:
    """The endpoint's classification, with the reason it was reached."""

    state: EndpointState
    reason_code: str = ""
    detail: str = ""
    endpoint_id: str = ""
    provider_id: str = ""

    @property
    def label(self) -> str:
        return self.state.label

    @property
    def allowed(self) -> bool:
        return self.state in (EndpointState.APPROVED_CLOUD, EndpointState.APPROVED_LOOPBACK)


@dataclass(frozen=True, slots=True)
class ProviderOption:
    """One provider identity the user may choose."""

    provider_id: str
    display_name: str
    base_url: str
    model: str
    trust: ProviderTrust
    local: bool = False

    @property
    def requires_credential(self) -> bool:
        return self.trust is not ProviderTrust.LOCAL

    @property
    def authentication(self) -> str:
        if self.trust is ProviderTrust.LOCAL:
            return "None (local model server)"
        return "API key (bearer token)"

    def to_settings(self) -> ProviderSettings:
        """The configuration this option describes."""

        return ProviderSettings(
            name="openai_compatible",
            base_url=self.base_url,
            model=self.model,
            provider_id=self.provider_id,
        )


def provider_catalog() -> list[ProviderOption]:
    """The provider identities offered by the settings.

    The list is deliberately explicit: the identity comes first, the display
    name is presentation, and the two are never confused.
    """

    return [
        ProviderOption(
            provider_id="openrouter",
            display_name="OpenRouter",
            base_url="https://openrouter.ai/api/v1",
            model="openai/gpt-4o-mini",
            trust=ProviderTrust.CLOUD,
        ),
        ProviderOption(
            provider_id="openai",
            display_name="OpenAI",
            base_url="https://api.openai.com/v1",
            model="gpt-4o-mini",
            trust=ProviderTrust.CLOUD,
        ),
        ProviderOption(
            provider_id="groq",
            display_name="Groq",
            base_url="https://api.groq.com/openai/v1",
            model="llama-3.3-70b-versatile",
            trust=ProviderTrust.CLOUD,
        ),
        ProviderOption(
            provider_id="deepseek",
            display_name="DeepSeek",
            base_url="https://api.deepseek.com/v1",
            model="deepseek-chat",
            trust=ProviderTrust.CLOUD,
        ),
        ProviderOption(
            provider_id="minimax",
            display_name="MiniMax",
            base_url="https://api.minimax.chat/v1",
            model="minimax-m3.1-flash",
            trust=ProviderTrust.CLOUD,
        ),
        ProviderOption(
            provider_id="ollama",
            display_name="Ollama (local)",
            base_url="http://localhost:11434/v1",
            model="qwen2.5-coder",
            trust=ProviderTrust.LOCAL,
            local=True,
        ),
        ProviderOption(
            provider_id="lmstudio",
            display_name="LM Studio (local)",
            base_url="http://localhost:1234/v1",
            model="local-model",
            trust=ProviderTrust.LOCAL,
            local=True,
        ),
        ProviderOption(
            provider_id="llamacpp",
            display_name="llama.cpp (local)",
            base_url="http://localhost:8080/v1",
            model="local-model",
            trust=ProviderTrust.LOCAL,
            local=True,
        ),
        ProviderOption(
            provider_id="vllm",
            display_name="vLLM (local)",
            base_url="http://localhost:8000/v1",
            model="local-model",
            trust=ProviderTrust.LOCAL,
            local=True,
        ),
    ]


class SecurityController:
    """The rules behind the secure provider settings.

    Parameters
    ----------
    manager:
        The configuration manager. The controller reads and writes the saved
        configuration through it.
    vault:
        The credential vault. It is the only component that touches values.
    gateway:
        The authorisation gateway. The controller asks it about endpoints and
        never answers that question itself.
    audit:
        The security audit log the controller writes to.
    secure_storage:
        Whether an OS credential store is actually available. When it is not,
        credential writes fail closed.
    """

    def __init__(
        self,
        manager: ConfigManager,
        *,
        vault: CredentialVault,
        gateway: AuthGateway,
        audit: SecurityAuditLog,
        secure_storage: bool = True,
    ) -> None:
        self._manager = manager
        self._vault = vault
        self._gateway = gateway
        self._audit = audit
        self._secure_storage = bool(secure_storage)

    # -- identity ---------------------------------------------------------
    @property
    def catalog(self) -> list[ProviderOption]:
        return provider_catalog()

    def current_identity(self) -> ProviderIdentity:
        """The identity the saved configuration describes."""

        return self._manager.config.provider.identity()

    def option_for(self, provider_id: str) -> ProviderOption | None:
        for option in self.catalog:
            if option.provider_id == provider_id:
                return option
        return None

    def legacy_slot(self) -> str:
        """The old shared slot, named for the protocol factory it was written with."""

        return self._manager.legacy_api_key_secret_key()

    # -- credential status ------------------------------------------------
    def credential_resolution(self, provider_id: str) -> CredentialResolution:
        """The authoritative decision for one catalogue identity.

        The gateway owns the decision, so the panel, the readiness report and
        the request path can never disagree about whether a key exists.
        Authorisation is not repeated here: this answers "is there a usable
        credential for this provider", while :meth:`endpoint_status` answers
        whether the endpoint may receive it.
        """

        option = self.option_for(provider_id)
        if option is None:
            return CredentialResolution.unavailable(
                provider_id=provider_id,
                credential_id=f"provider:{provider_id}:default:api_key",
                reason_code="unknown_provider",
                detail="the provider is not in the catalogue",
            )
        endpoint = canonicalize_endpoint(option.base_url)
        identity = resolve_provider_identity(endpoint, explicit_provider_id=provider_id)
        return self._gateway.resolve_credential(
            identity,
            endpoint,
            required=option.requires_credential,
            record=False,
            authorize=False,
        )

    def credential_status(self, provider_id: str) -> CredentialStatus:
        """The status of one provider's credential. Never the value."""

        return self._status_from(self.credential_resolution(provider_id))

    def _status_from(self, resolution: CredentialResolution) -> CredentialStatus:
        status = resolution.status
        if status is ResolutionStatus.RESOLVED:
            if resolution.source is CredentialSource.ENVIRONMENT:
                # An environment credential needs no secure store and is
                # usable exactly where the request path would use it.
                return CredentialStatus.ENVIRONMENT
            if not self._secure_storage:
                return CredentialStatus.DEGRADED
            if self._is_current(resolution.provider_id):
                return CredentialStatus.ACTIVE
            return CredentialStatus.CONFIGURED
        if status is ResolutionStatus.DEGRADED:
            return CredentialStatus.DEGRADED
        if status is ResolutionStatus.REJECTED:
            if resolution.reason_code == "credential_disabled":
                return CredentialStatus.DISABLED
            return CredentialStatus.REJECTED
        if status is ResolutionStatus.UNAVAILABLE:
            if not self._secure_storage:
                return CredentialStatus.SECURE_STORAGE_UNAVAILABLE
            return CredentialStatus.UNAVAILABLE
        if not self._secure_storage:
            return CredentialStatus.SECURE_STORAGE_UNAVAILABLE
        if self._is_current(resolution.provider_id) and self.legacy_candidate() is not None:
            return CredentialStatus.MIGRATION_REQUIRED
        return CredentialStatus.NOT_CONFIGURED

    def _is_current(self, provider_id: str) -> bool:
        try:
            return self.current_identity().provider_id == provider_id
        except EndpointPolicyError:
            return False

    def masked_credential(self, provider_id: str) -> str:
        """A masked description of a credential, or an empty string."""

        resolution = self.credential_resolution(provider_id)
        # A local endpoint needs no credential and a refused one has none to
        # describe, so neither gets the "(not set)" placeholder here.
        if not resolution.usable or resolution.value is None:
            return ""
        return resolution.masked()

    # -- credential operations --------------------------------------------
    def store_credential(self, provider_id: str, secret: str) -> CredentialStatus:
        """Store a credential for one catalogue provider identity."""

        return self.store_credential_for(self._identity_for(provider_id), secret)

    def store_credential_for(self, identity: ProviderIdentity, secret: str) -> CredentialStatus:
        """Store a credential under an already-validated identity.

        The identity is supplied by the caller (the Settings dialog resolves it
        from the *edited* endpoint), so a key can never be written under a
        different provider than the one on screen.

        Fails closed when secure storage is unavailable, and never writes a
        value into a plaintext store.
        """

        if not self._secure_storage:
            raise CredentialVaultError(
                "no OS credential store is available",
                user_message=(
                    "This system has no secure credential store, so API keys cannot be "
                    "stored safely. Nothing was changed."
                ),
            )
        if identity.identity_mismatch:
            raise CredentialVaultError(
                "the provider identity does not belong to the endpoint",
                user_message=(
                    "The selected provider does not match the model address. Choose the "
                    "provider again before entering an API key; nothing was stored."
                ),
            )
        value = secret.strip()
        if not value:
            raise CredentialVaultError(
                "the credential is empty",
                user_message="Enter a non-empty API key.",
            )
        credential_id = identity.credential_id
        if self._vault.has_secret(credential_id):
            self._vault.rotate_secret(
                credential_id,
                value,
                provider_id=identity.provider_id,
                endpoint_id=identity.endpoint_id,
            )
        else:
            self._vault.set_secret(
                credential_id,
                value,
                provider_id=identity.provider_id,
                endpoint_id=identity.endpoint_id,
            )
        return self._status_after_store(identity)

    def _status_after_store(self, identity: ProviderIdentity) -> CredentialStatus:
        if not self._vault.has_secret(identity.credential_id):
            return CredentialStatus.REJECTED
        if not self._secure_storage:
            return CredentialStatus.DEGRADED
        if self._is_current(identity.provider_id):
            return CredentialStatus.ACTIVE
        return CredentialStatus.CONFIGURED

    def disable_credential(self, provider_id: str) -> CredentialStatus:
        credential_id = f"provider:{provider_id}:default:api_key"
        self._vault.disable(credential_id)
        return self.credential_status(provider_id)

    def enable_credential(self, provider_id: str) -> CredentialStatus:
        credential_id = f"provider:{provider_id}:default:api_key"
        self._vault.enable(credential_id)
        return self.credential_status(provider_id)

    def remove_credential(self, provider_id: str) -> bool:
        return self._vault.delete_secret(f"provider:{provider_id}:default:api_key")

    def remove_credential_for(self, identity: ProviderIdentity) -> bool:
        """Remove only the credential bound to ``identity``."""

        return self._vault.delete_secret(identity.credential_id)

    def _identity_for(self, provider_id: str) -> ProviderIdentity:
        """The identity a provider option describes, without touching the config."""

        option = self.option_for(provider_id)
        if option is None:
            raise CredentialVaultError(
                f"unknown provider identity: {provider_id!r}",
                user_message="Choose a provider from the list.",
            )
        endpoint = canonicalize_endpoint(option.base_url)
        return resolve_provider_identity(endpoint, explicit_provider_id=provider_id)

    # -- legacy migration -------------------------------------------------
    def legacy_candidate(self) -> LegacyCredentialCandidate | None:
        """The legacy credential that could be adopted, if there is one.

        The candidate is *not* assumed to belong to any provider: the caller
        has to say which one it belongs to.
        """

        try:
            identity = self.current_identity()
            endpoint = canonicalize_endpoint(self._manager.config.provider.base_url)
        except EndpointPolicyError:
            return None
        slot = self.legacy_slot()
        if self._vault.get_secret(f"provider:{identity.provider_id}:default:api_key"):
            # The destination already holds a credential; there is nothing to
            # adopt and nothing to overwrite.
            return None
        return self._vault.detect_legacy_credential(
            slot,
            provider_id=identity.provider_id,
            endpoint_id=endpoint.endpoint_id,
            provider_is_known=identity.trust is ProviderTrust.CLOUD,
        )

    def migration_preview(self) -> dict[str, str]:
        """What the confirmation dialog tells the user. Never the value."""

        candidate = self.legacy_candidate()
        if candidate is None:
            return {}
        return {
            "legacy_slot": candidate.legacy_slot,
            "provider_id": candidate.provider_id,
            "endpoint_id": candidate.endpoint_id,
            "reason": candidate.reason,
        }

    def confirm_migration(self, provider_id: str) -> MigrationResult:
        """Adopt the legacy credential for the provider the user named.

        The user has already chosen the provider and pressed the button; this
        is the call that performs it. The source is only removed after the
        destination has been read back and compared.
        """

        candidate = self.legacy_candidate()
        if candidate is None:
            return MigrationResult(status="nothing_to_migrate")
        if provider_id != candidate.provider_id:
            # The user named a different provider than the one configured.
            # The legacy slot's owner is decided by the user's explicit
            # choice, so the migration is performed for *that* provider.
            identity = self._identity_for(provider_id)
            candidate = LegacyCredentialCandidate(
                legacy_slot=candidate.legacy_slot,
                provider_id=identity.provider_id,
                endpoint_id=identity.endpoint_id,
                unambiguous=candidate.unambiguous,
                reason=candidate.reason,
            )
        return self._vault.migrate_legacy_credential(candidate, confirm=lambda _c: True)

    # -- endpoint authorisation -------------------------------------------
    def endpoint_status(self) -> EndpointStatus:
        """How the configured endpoint classifies itself, per the gateway."""

        provider = self._manager.config.provider
        try:
            endpoint = canonicalize_endpoint(provider.base_url)
        except EndpointPolicyError as exc:
            return EndpointStatus(
                state=EndpointState.INVALID,
                reason_code="invalid_endpoint",
                detail=str(exc),
            )
        try:
            identity = provider.identity()
        except EndpointPolicyError as exc:
            return EndpointStatus(
                state=EndpointState.INVALID,
                reason_code="invalid_endpoint",
                detail=str(exc),
            )
        decision = self._gateway.authorize(identity, endpoint, record=False)
        state = self._state_for(decision, identity)
        return EndpointStatus(
            state=state,
            reason_code=decision.reason_code,
            detail=decision.detail,
            endpoint_id=endpoint.endpoint_id,
            provider_id=identity.provider_id,
        )

    @staticmethod
    def _state_for(decision: Any, identity: ProviderIdentity) -> EndpointState:
        if decision.decision is AuthorizationDecision.ALLOWED:
            if identity.trust is ProviderTrust.LOCAL:
                return EndpointState.APPROVED_LOOPBACK
            return EndpointState.APPROVED_CLOUD
        reason = decision.reason_code
        if reason == "custom_endpoint_not_trusted":
            return EndpointState.CUSTOM_REQUIRES_AUTHORIZATION
        if reason in {
            "provider_endpoint_mismatch",
            "identity_endpoint_mismatch",
            "custom_identity_not_endpoint_bound",
            "endpoint_identity_changed",
            "endpoint_not_bound",
            "provider_not_recognised",
        }:
            return EndpointState.IDENTITY_MISMATCH
        if reason in {"scheme_downgrade"}:
            return EndpointState.BLOCKED_INSECURE
        if reason in {
            "private_destination",
            "loopback_destination",
            "link_local_destination",
            "metadata_endpoint",
            "remote_local_endpoint",
            "dns_resolves_to_non_public",
            "dns_unresolved",
        }:
            return EndpointState.BLOCKED_PRIVATE
        return EndpointState.UNKNOWN

    def trust_custom_endpoint(self, trusted: bool) -> EndpointStatus:
        """Record the operator's explicit trust decision for a custom endpoint.

        The decision is stored in the configuration, so it survives a restart
        and is visible in the file the operator can inspect.
        """

        self._manager.config.provider.trust_custom_endpoint = bool(trusted)
        return self.endpoint_status()

    def apply_provider(self, option: ProviderOption) -> None:
        """Point the configuration at a provider identity.

        The credential is *not* touched: choosing a provider never moves,
        copies or reuses a credential.
        """

        provider = self._manager.config.provider
        provider.name = "openai_compatible"
        provider.base_url = option.base_url
        provider.model = option.model
        provider.provider_id = option.provider_id
        if option.local:
            provider.trust_custom_endpoint = False

    # -- audit ------------------------------------------------------------
    def audit_rows(
        self,
        *,
        provider_id: str = "",
        event: str = "",
        result: str = "",
        include_persisted: bool = False,
    ) -> list[AuditEvent]:
        """The audit trail, filtered. Sanitised by construction.

        ``include_persisted`` adds the records held on disk, which is how a view
        shows what happened before this session started. The current session's own
        records are already in memory, so the stored copy of them is not repeated.
        """

        records: list[AuditEvent] = list(self._audit.events)
        if include_persisted:
            seen = {_audit_key(record) for record in records}
            earlier = [
                record for record in self.persisted_audit_rows() if _audit_key(record) not in seen
            ]
            records = earlier + records
        rows: list[AuditEvent] = []
        for record in records:
            if provider_id and record.provider_id != provider_id:
                continue
            if event and str(record.event) != event:
                continue
            if result and record.decision != result:
                continue
            rows.append(record)
        return rows

    def persisted_audit_rows(self, *, limit: int = 500) -> list[AuditEvent]:
        """The records still on disk, oldest first. Empty when nothing persists."""

        archive = self._audit_archive()
        return [] if archive is None else archive.replay(limit=limit)

    def audit_is_session_only(self) -> bool:
        """True only when the trail genuinely has no durable home.

        With a sink attached the records outlive the run, and saying otherwise in
        the UI would understate what the operator is looking at.
        """

        return self._audit.sink is None

    def audit_store_note(self) -> str:
        """One line about where the records go and whether the chain still holds."""

        status = self.audit_store_status()
        if status is None:
            return "Records live in memory only and are not written to disk."
        state = "chain intact" if status.chain_ok else f"chain broken: {status.chain_note}"
        if status.recovery_note:
            state = f"changed while closed: {status.recovery_note}"
        return (
            f"{status.records} record(s) in {status.segments} segment(s) at "
            f"{Path(status.path) / status.store_name} \u2014 {state}"
            + ("; some records could not be written" if status.degraded else "")
        )

    def audit_store_status(self) -> SinkStatus | None:
        """The store's own numbers, or None when the log has no store behind it."""

        archive = self._audit_archive()
        return None if archive is None else archive.status()

    def _audit_archive(self) -> AuditArchive | None:
        """The sink, but only when it is one that can also hand records back.

        An append-only pipe is a valid sink and has nothing to read; asking it for
        history would be a silent ``AttributeError`` rather than an honest empty.
        """

        sink: object = self._audit.sink
        return sink if isinstance(sink, AuditArchive) else None

    def record_provider_selected(self, option: ProviderOption) -> None:
        try:
            endpoint_id = canonicalize_endpoint(option.base_url).endpoint_id
        except EndpointPolicyError:
            endpoint_id = ""
        self._audit.record(
            AuditEventType.PROVIDER_SELECTED,
            provider_id=option.provider_id,
            model=option.model,
            endpoint_id=endpoint_id,
            decision="selected",
            reason_code="provider_selected",
        )


def _audit_key(record: AuditEvent) -> tuple[str, ...]:
    """Identity of a record, for matching the memory copy to the stored copy.

    Every field of the record is part of its identity; the two copies differ only
    in the chain bookkeeping the sink adds, which is not a field of the event.
    """

    payload = record.to_dict()
    return tuple(payload[name] for name in sorted(payload))
