"""Phase 0.8 regressions around the one authoritative credential decision.

The module covers four seams that used to disagree with one another:

* resolution precedence (stored value, then that provider's own environment);
* a secure store that cannot be read or written;
* readiness answering the same question as the request path;
* the audit trail recording decisions without credential material.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import SecretStr
from tests.security.test_m3b_s11_credential_security import (
    CUSTOM_BASE,
    OPENROUTER_BASE,
    OPENROUTER_KEY,
    FakeSecretStore,
)

from quantsmind_agent.app import ApplicationContext, ReadinessCode
from quantsmind_agent.auth_gateway import AuthenticationGatewayError
from quantsmind_agent.config import ConfigManager
from quantsmind_agent.credential_resolution import (
    CredentialResolutionService,
    CredentialSource,
    CredentialStatus,
)
from quantsmind_agent.credential_vault import CredentialVault, CredentialVaultError
from quantsmind_agent.gui.security_controller import (
    CredentialStatus as ControllerStatus,
)
from quantsmind_agent.gui.security_controller import SecurityController
from quantsmind_agent.paths import AppPaths
from quantsmind_agent.provider_identity import (
    ProviderIdentity,
    canonicalize_endpoint,
    resolve_provider_identity,
)
from quantsmind_agent.secrets import (
    FileSecretStore,
    SecretStoreError,
    StorageStatus,
)
from quantsmind_agent.security_audit import AuditEventType, SecurityAuditLog

OPENROUTER_SLOT = "provider:openrouter:default:api_key"
LEGACY_SLOT = "provider:openai_compatible:api_key"


def _identity() -> ProviderIdentity:
    return resolve_provider_identity(canonicalize_endpoint(OPENROUTER_BASE))


def _paths(tmp_path: Path) -> AppPaths:
    return AppPaths(
        config_dir=tmp_path / "config",
        log_dir=tmp_path / "logs",
        cache_dir=tmp_path / "cache",
        data_dir=tmp_path / "data",
    ).ensure()


def _manager(
    tmp_path: Path,
    store: FakeSecretStore | None = None,
    *,
    base_url: str = OPENROUTER_BASE,
    provider_id: str | None = "openrouter",
    trust_custom: bool = False,
) -> ConfigManager:
    paths = _paths(tmp_path)
    manager = ConfigManager(paths, secret_store=store or FakeSecretStore())
    binding = f'provider_id = "{provider_id}"\n' if provider_id else ""
    trust = "trust_custom_endpoint = true\n" if trust_custom else ""
    (paths.config_dir / "config.toml").write_text(
        f'[provider]\nbase_url = "{base_url}"\n' + binding + trust,
        encoding="utf-8",
    )
    manager.load()
    return manager


def _context(
    manager: ConfigManager,
    workspace: Path,
    *,
    resolver: object | None = None,
) -> ApplicationContext:
    context = ApplicationContext.create(
        paths=manager.paths,
        config_manager=manager,
        security_resolver=resolver or (lambda host: ["93.184.216.34"]),
    )
    context.open_workspace(workspace)
    return context


class _BrokenStore(FakeSecretStore):
    """A secure store whose backend fails on demand."""

    def __init__(self, *, reads: bool = False, writes: bool = False) -> None:
        super().__init__()
        self._fail_reads = reads
        self._fail_writes = writes

    def read(self, key: str) -> tuple[SecretStr | None, StorageStatus]:
        if self._fail_reads:
            raise SecretStoreError(
                "the credential backend is locked", details={"operation": "read"}
            )
        return super().read(key)

    def get(self, key: str) -> SecretStr | None:
        if self._fail_reads:
            raise SecretStoreError(
                "the credential backend is locked", details={"operation": "read"}
            )
        return super().get(key)

    def set(self, key: str, value: SecretStr | str) -> None:
        if self._fail_writes:
            raise SecretStoreError(
                "the credential backend refused the write",
                details={"operation": "write"},
            )
        super().set(key, value)


class TestResolutionPrecedence:
    def test_a_stored_value_wins_over_an_environment_variable(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        store = FakeSecretStore()
        store.set(OPENROUTER_SLOT, OPENROUTER_KEY)
        service = CredentialResolutionService(
            secret_store=store,
            provider_env_vars={"openrouter": "OPENROUTER_API_KEY"},
            generic_env_owner="openai",
        )
        monkeypatch.setenv("OPENROUTER_API_KEY", "sk-env-must-not-win")

        resolution = service.resolve(_identity())

        assert resolution.status is CredentialStatus.RESOLVED
        assert resolution.source is CredentialSource.VAULT
        assert resolution.value == OPENROUTER_KEY

    def test_only_the_providers_own_variable_is_read(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        service = CredentialResolutionService(
            secret_store=FakeSecretStore(),
            provider_env_vars={"openrouter": "OPENROUTER_API_KEY", "groq": "GROQ_API_KEY"},
            generic_env_owner="openai",
        )
        monkeypatch.setenv("GROQ_API_KEY", "sk-groq-env")
        monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)

        resolution = service.resolve(_identity())

        assert resolution.status is CredentialStatus.MISSING
        assert resolution.value is None

    def test_a_store_failure_never_falls_through_to_the_environment(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        service = CredentialResolutionService(
            secret_store=_BrokenStore(reads=True),
            provider_env_vars={"openrouter": "OPENROUTER_API_KEY"},
            generic_env_owner="openai",
        )
        monkeypatch.setenv("OPENROUTER_API_KEY", "sk-env-must-not-substitute")

        resolution = service.resolve(_identity())

        assert resolution.status is CredentialStatus.UNAVAILABLE
        assert resolution.value is None
        assert "credential_store_unavailable" in resolution.reason_code

    def test_a_disabled_credential_cannot_be_replaced_by_the_environment(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        store = FakeSecretStore()
        store.set(OPENROUTER_SLOT, OPENROUTER_KEY)
        vault = CredentialVault(store, metadata_path=None, audit=SecurityAuditLog())
        vault.disable(OPENROUTER_SLOT)
        service = CredentialResolutionService(
            vault,
            provider_env_vars={"openrouter": "OPENROUTER_API_KEY"},
            generic_env_owner="openai",
        )
        monkeypatch.setenv("OPENROUTER_API_KEY", "sk-env-must-not-bypass")

        resolution = service.resolve(_identity())

        assert resolution.status is CredentialStatus.REJECTED
        assert resolution.reason_code == "credential_disabled"
        assert resolution.value is None


class TestSecureStoreFailures:
    def test_a_read_failure_is_reported_as_unavailable(
        self, tmp_path: Path, sample_project: Path
    ) -> None:
        manager = _manager(tmp_path, _BrokenStore(reads=True))
        context = _context(manager, sample_project)

        report = context.readiness_report()

        assert report.ready is False
        assert report.code is ReadinessCode.CREDENTIAL_UNAVAILABLE
        assert report.credential is not None
        assert report.credential.status is CredentialStatus.UNAVAILABLE
        assert report.credential.value is None

    def test_a_write_failure_is_visible_and_stores_nothing(
        self, tmp_path: Path, sample_project: Path
    ) -> None:
        store = _BrokenStore(writes=True)
        manager = _manager(tmp_path, store)
        context = _context(manager, sample_project)

        with pytest.raises(CredentialVaultError) as info:
            context.security_controller().store_credential("openrouter", OPENROUTER_KEY)

        assert info.value.details.get("reason_code") == "secret_store_write_failed"
        assert OPENROUTER_KEY not in str(info.value)
        assert store.get(OPENROUTER_SLOT) is None
        assert context.readiness_report().code is ReadinessCode.CREDENTIAL_MISSING
        assert context.security_audit.events_of_type(AuditEventType.SECURE_STORE_FAILED)

    def test_a_write_failure_never_leaks_into_a_plaintext_file(self, tmp_path: Path) -> None:
        paths = _paths(tmp_path)
        store = _BrokenStore(writes=True)
        vault = CredentialVault(store, metadata_path=None, audit=SecurityAuditLog())

        with pytest.raises(CredentialVaultError):
            vault.set_secret(OPENROUTER_SLOT, OPENROUTER_KEY)

        assert store.get(OPENROUTER_SLOT) is None
        assert paths.secrets_file.exists() is False

    def test_a_plaintext_value_is_readable_but_degraded(self, tmp_path: Path) -> None:
        paths = _paths(tmp_path)
        FileSecretStore(paths.secrets_file).set(OPENROUTER_SLOT, SecretStr(OPENROUTER_KEY))
        manager = ConfigManager(paths, secret_store=FileSecretStore(paths.secrets_file))
        manager.load()

        resolution = CredentialResolutionService(
            secret_store=manager.secret_store,
            provider_env_vars={"openrouter": "OPENROUTER_API_KEY"},
            generic_env_owner="openai",
        ).resolve(_identity())

        assert resolution.value == OPENROUTER_KEY
        assert resolution.status is CredentialStatus.DEGRADED
        assert resolution.storage_status is StorageStatus.PLAINTEXT


class TestMigrationStoragePolicy:
    def _controller(
        self, tmp_path: Path, store: FakeSecretStore | FileSecretStore
    ) -> SecurityController:
        from quantsmind_agent.auth_gateway import AuthGateway

        paths = _paths(tmp_path)
        manager = ConfigManager(paths, secret_store=store)
        (paths.config_dir / "config.toml").write_text(
            f'[provider]\nbase_url = "{OPENROUTER_BASE}"\nprovider_id = "openrouter"\n',
            encoding="utf-8",
        )
        manager.load()
        audit = SecurityAuditLog()
        # A plaintext store can be read, but the vault refuses to write to it.
        vault = CredentialVault(
            store,
            metadata_path=None,
            audit=audit,
            require_secure=False,
            allow_insecure_writes=False,
        )
        gateway = AuthGateway(
            vault=vault,
            audit=audit,
            check_dns=False,
            trust_custom_endpoints=lambda: manager.config.provider.trust_custom_endpoint,
        )
        return SecurityController(
            manager,
            vault=vault,
            gateway=gateway,
            audit=audit,
            secure_storage=False,
        )

    def test_migration_refuses_a_read_only_store_and_preserves_the_source(
        self, tmp_path: Path
    ) -> None:
        paths = _paths(tmp_path)
        store = FileSecretStore(paths.secrets_file)
        store.set(LEGACY_SLOT, SecretStr(OPENROUTER_KEY))
        controller = self._controller(tmp_path, store)

        result = controller.confirm_migration("openrouter")

        assert result.migrated is False
        assert result.status == "failed"
        legacy = store.get(LEGACY_SLOT)
        assert legacy is not None
        assert legacy.get_secret_value() == OPENROUTER_KEY
        assert store.get(OPENROUTER_SLOT) is None

    def test_the_controller_reports_the_store_as_unavailable(self, tmp_path: Path) -> None:
        paths = _paths(tmp_path)
        store = FileSecretStore(paths.secrets_file)
        store.set(LEGACY_SLOT, SecretStr(OPENROUTER_KEY))
        controller = self._controller(tmp_path, store)

        assert (
            controller.credential_status("openrouter")
            is ControllerStatus.SECURE_STORAGE_UNAVAILABLE
        )
        with pytest.raises(CredentialVaultError):
            controller.store_credential("openrouter", OPENROUTER_KEY)


class TestReadinessMatchesTheRequestPath:
    def test_a_stored_key_makes_both_paths_agree(
        self, tmp_path: Path, sample_project: Path
    ) -> None:
        store = FakeSecretStore()
        manager = _manager(tmp_path, store)
        context = _context(manager, sample_project)
        context.credential_vault.set_secret(OPENROUTER_SLOT, OPENROUTER_KEY)

        report = context.readiness_report()
        identity = manager.config.provider.identity()
        endpoint = canonicalize_endpoint(manager.config.provider.base_url)
        released = context.auth_gateway.credential_for(identity, endpoint)

        assert report.ready is True
        assert report.code is ReadinessCode.READY
        assert released == OPENROUTER_KEY

    def test_a_missing_key_is_the_same_answer_in_both_paths(
        self, tmp_path: Path, sample_project: Path
    ) -> None:
        manager = _manager(tmp_path)
        context = _context(manager, sample_project)

        report = context.readiness_report()
        identity = manager.config.provider.identity()
        endpoint = canonicalize_endpoint(manager.config.provider.base_url)

        assert report.code is ReadinessCode.CREDENTIAL_MISSING
        with pytest.raises(AuthenticationGatewayError) as info:
            context.auth_gateway.credential_for(identity, endpoint)
        assert "credential_missing" in str(info.value)

    def test_an_identity_mismatch_is_refused_before_any_credential_read(
        self, tmp_path: Path, sample_project: Path
    ) -> None:
        store = FakeSecretStore()
        store.set(OPENROUTER_SLOT, OPENROUTER_KEY)
        manager = _manager(
            tmp_path,
            store,
            base_url=CUSTOM_BASE,
            provider_id="openrouter",
            trust_custom=True,
        )
        context = _context(manager, sample_project)

        report = context.readiness_report()

        assert report.ready is False
        assert report.code is ReadinessCode.IDENTITY_MISMATCH
        assert context.credential_resolution().value is None

    def test_a_disabled_credential_is_reported_as_disabled(
        self, tmp_path: Path, sample_project: Path
    ) -> None:
        manager = _manager(tmp_path)
        context = _context(manager, sample_project)
        context.credential_vault.set_secret(OPENROUTER_SLOT, OPENROUTER_KEY)
        context.credential_vault.disable(OPENROUTER_SLOT)

        report = context.readiness_report()

        assert report.ready is False
        assert report.code is ReadinessCode.CREDENTIAL_DISABLED
        assert report.credential is not None
        assert report.credential.value is None

    def test_an_untrusted_custom_endpoint_is_denied_in_both_paths(
        self, tmp_path: Path, sample_project: Path
    ) -> None:
        store = FakeSecretStore()
        manager = _manager(
            tmp_path, store, base_url=CUSTOM_BASE, provider_id=None, trust_custom=False
        )
        context = _context(manager, sample_project)
        identity = manager.config.provider.identity()
        context.credential_vault.set_secret(identity.credential_id, OPENROUTER_KEY)

        report = context.readiness_report()

        assert report.ready is False
        assert report.code is ReadinessCode.ENDPOINT_DENIED
        endpoint = canonicalize_endpoint(CUSTOM_BASE)
        with pytest.raises(AuthenticationGatewayError):
            context.auth_gateway.credential_for(identity, endpoint)

    def test_a_plaintext_store_is_ready_but_reported_as_degraded(
        self, tmp_path: Path, sample_project: Path
    ) -> None:
        paths = _paths(tmp_path)
        FileSecretStore(paths.secrets_file).set(OPENROUTER_SLOT, SecretStr(OPENROUTER_KEY))
        manager = ConfigManager(paths, secret_store=FileSecretStore(paths.secrets_file))
        (paths.config_dir / "config.toml").write_text(
            f'[provider]\nbase_url = "{OPENROUTER_BASE}"\nprovider_id = "openrouter"\n',
            encoding="utf-8",
        )
        context = _context(manager, sample_project)

        report = context.readiness_report()

        assert report.ready is True
        assert report.code is ReadinessCode.CREDENTIAL_STORAGE_DEGRADED
        assert context.secure_storage_available is False
        assert report.credential is not None
        assert report.credential.storage_status is StorageStatus.PLAINTEXT


class TestAuditRedaction:
    def test_authentication_failure_records_the_decision_not_the_value(
        self, tmp_path: Path, sample_project: Path
    ) -> None:
        store = FakeSecretStore()
        manager = _manager(tmp_path, store)
        context = _context(manager, sample_project)
        context.credential_vault.set_secret(OPENROUTER_SLOT, OPENROUTER_KEY)
        context.credential_vault.disable(OPENROUTER_SLOT)

        identity = manager.config.provider.identity()
        endpoint = canonicalize_endpoint(manager.config.provider.base_url)
        with pytest.raises(AuthenticationGatewayError):
            context.auth_gateway.credential_for(identity, endpoint)

        events = context.security_audit.events_of_type(AuditEventType.AUTHENTICATION_FAILED)
        assert events
        assert any(event.reason_code == "credential_disabled" for event in events)
        for event in context.security_audit.events:
            assert OPENROUTER_KEY not in event.to_log_line()

    def test_an_identifier_mismatch_is_audited_with_a_canonical_endpoint(
        self, tmp_path: Path, sample_project: Path
    ) -> None:
        store = FakeSecretStore()
        store.set(OPENROUTER_SLOT, OPENROUTER_KEY)
        manager = _manager(
            tmp_path,
            store,
            base_url=CUSTOM_BASE,
            provider_id="openrouter",
            trust_custom=True,
        )
        context = _context(manager, sample_project)
        identity = manager.config.provider.identity()
        endpoint = canonicalize_endpoint(CUSTOM_BASE)

        with pytest.raises(AuthenticationGatewayError):
            context.auth_gateway.credential_for(identity, endpoint)

        events = context.security_audit.events_of_type(AuditEventType.PROVIDER_IDENTITY_MISMATCH)
        assert events
        recorded = events[-1]
        assert recorded.reason_code == "provider_endpoint_mismatch"
        assert recorded.endpoint_id == endpoint.endpoint_id
        assert "api.example.com" in recorded.endpoint_id
        assert OPENROUTER_KEY not in recorded.to_log_line()

    def test_a_named_secret_field_is_refused(self) -> None:
        audit = SecurityAuditLog()

        with pytest.raises(ValueError):
            audit.record(
                AuditEventType.AUTHENTICATION_FAILED,
                provider_id="openrouter",
                api_key=OPENROUTER_KEY,
            )

    def test_url_borne_credentials_are_removed_from_free_text(self) -> None:
        audit = SecurityAuditLog()

        record = audit.record(
            AuditEventType.ENDPOINT_DENIED,
            provider_id="openrouter",
            endpoint_id="https://user:pass@openrouter.ai/api/v1?token=secret#x",
            detail="refused https://user:pass@openrouter.ai/cb?token=abc",
        )

        rendered = record.to_log_line()
        assert "pass" not in rendered
        assert "token=abc" not in rendered
        assert "secret" not in rendered
