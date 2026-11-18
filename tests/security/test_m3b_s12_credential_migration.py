"""Security tests for the provider settings and the migration flow (M3B-S1.2).

Every test is offline: fake credentials only, an in-memory secret store, and
a socket guard that refuses any connection, so a test that accidentally
reached the network would fail rather than pass quietly.

The rules under test live in
:class:`~quantsmind_agent.gui.security.controller.SecurityController`, which
has no Qt dependency, so they can be qualified without a display.
"""

from __future__ import annotations

import json
import socket
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from tests.security.test_m3b_s11_credential_security import FakeSecretStore

from quantsmind_agent.app.settings import ConfigManager
from quantsmind_agent.gui.security.controller import (
    CredentialStatus,
    EndpointState,
    SecurityController,
)
from quantsmind_agent.persistence.paths import AppPaths
from quantsmind_agent.security.audit import AuditEventType, SecurityAuditLog
from quantsmind_agent.security.auth_gateway import (
    AuthenticationGatewayError,
    AuthGateway,
)
from quantsmind_agent.security.credential_vault import (
    CredentialVault,
    CredentialVaultError,
)
from quantsmind_agent.security.permissions import (
    Decision,
    PermissionBroker,
    StaticPermissionProvider,
)

OPENROUTER_KEY = "sk-canary-openrouter-0000000000"
OPENAI_KEY = "sk-canary-openai-1111111111"
CUSTOM_KEY = "sk-canary-custom-3333333333"

OPENROUTER_BASE = "https://openrouter.ai/api/v1"


@pytest.fixture(autouse=True)
def _no_network(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Refuse every outbound connection for the duration of a test."""

    def refuse(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("a test attempted a network connection")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket, "getaddrinfo", refuse)
    yield


def _paths(tmp_path: Path) -> AppPaths:
    return AppPaths(
        config_dir=tmp_path / "config",
        log_dir=tmp_path / "logs",
        cache_dir=tmp_path / "cache",
        data_dir=tmp_path / "data",
    ).ensure()


def _manager(tmp_path: Path, store: Any = None) -> ConfigManager:
    paths = _paths(tmp_path)
    manager = ConfigManager(paths, secret_store=store or FakeSecretStore())
    (paths.config_dir / "config.toml").write_text(
        f'[provider]\nbase_url = "{OPENROUTER_BASE}"\n', encoding="utf-8"
    )
    manager.load()
    return manager


def _controller(
    tmp_path: Path,
    *,
    store: Any = None,
    secure_storage: bool = True,
) -> tuple[SecurityController, FakeSecretStore, SecurityAuditLog]:
    backend = store or FakeSecretStore()
    manager = _manager(tmp_path, backend)
    audit = SecurityAuditLog()
    vault = CredentialVault(backend, metadata_path=None, audit=audit, require_secure=True)
    gateway = AuthGateway(
        vault=vault,
        audit=audit,
        check_dns=False,
        trust_custom_endpoints=lambda: manager.config.provider.trust_custom_endpoint,
        allow_remote_local_models=lambda: manager.config.provider.allow_remote_local_models,
    )
    controller = SecurityController(
        manager,
        vault=vault,
        gateway=gateway,
        audit=audit,
        secure_storage=secure_storage,
    )
    return controller, backend, audit


class TestProviderIdentitySelection:
    """Scenarios 1, 2, 23."""

    def test_choosing_a_provider_sets_its_identity(self, tmp_path: Path) -> None:
        controller, _backend, _audit = _controller(tmp_path)
        option = controller.option_for("groq")
        assert option is not None
        controller.apply_provider(option)
        assert controller._manager.config.provider.provider_id == "groq"
        assert controller._manager.config.provider.base_url == option.base_url
        assert controller.current_identity().provider_id == "groq"

    def test_the_display_name_never_decides_the_identity(self, tmp_path: Path) -> None:
        controller, _backend, _audit = _controller(tmp_path)
        controller._manager.config.provider.name = "OpenRouter"
        controller._manager.config.provider.base_url = "https://api.openai.com/v1"
        controller._manager.config.provider.provider_id = None
        assert controller.current_identity().provider_id == "openai"

    def test_every_catalog_identity_resolves_to_itself(self, tmp_path: Path) -> None:
        controller, _backend, _audit = _controller(tmp_path)
        for option in controller.catalog:
            controller.apply_provider(option)
            assert controller.current_identity().provider_id == option.provider_id

    def test_a_local_option_is_marked_local_and_needs_no_credential(self, tmp_path: Path) -> None:
        controller, _backend, _audit = _controller(tmp_path)
        option = controller.option_for("ollama")
        assert option is not None
        assert option.local is True
        assert option.requires_credential is False
        assert "None" in option.authentication

    def test_an_existing_configuration_still_loads(self, tmp_path: Path) -> None:
        """A file written before M3B-S1.1 has no provider_id; it still works."""

        paths = _paths(tmp_path)
        (paths.config_dir / "config.toml").write_text(
            '[provider]\nbase_url = "https://openrouter.ai/api/v1"\nmodel = "m"\n',
            encoding="utf-8",
        )
        manager = ConfigManager(paths, secret_store=FakeSecretStore())
        config = manager.load()
        assert config.provider.provider_id is None
        assert config.provider.trust_custom_endpoint is False
        assert config.provider.identity().provider_id == "openrouter"

    def test_the_new_fields_round_trip_through_the_configuration_file(self, tmp_path: Path) -> None:
        controller, _backend, _audit = _controller(tmp_path)
        controller._manager.config.provider.provider_id = "openrouter"
        controller._manager.config.provider.trust_custom_endpoint = True
        controller._manager.save()
        reloaded = ConfigManager(controller._manager.paths, secret_store=FakeSecretStore())
        config = reloaded.load()
        assert config.provider.provider_id == "openrouter"
        assert config.provider.trust_custom_endpoint is True


class TestCredentialStatusAndOperations:
    """Scenarios 2, 3, 4, 5, 7, 8, 9, 10."""

    def test_a_fresh_provider_is_not_configured(self, tmp_path: Path) -> None:
        controller, _backend, _audit = _controller(tmp_path)
        assert controller.credential_status("openrouter") is CredentialStatus.NOT_CONFIGURED

    def test_a_stored_credential_is_active_for_the_current_provider(self, tmp_path: Path) -> None:
        controller, _backend, _audit = _controller(tmp_path)
        controller.store_credential("openrouter", OPENROUTER_KEY)
        assert controller.credential_status("openrouter") is CredentialStatus.ACTIVE

    def test_a_credential_for_another_provider_is_only_configured(self, tmp_path: Path) -> None:
        controller, _backend, _audit = _controller(tmp_path)
        controller.store_credential("groq", OPENAI_KEY)
        assert controller.credential_status("groq") is CredentialStatus.CONFIGURED
        assert controller.credential_status("openrouter") is CredentialStatus.NOT_CONFIGURED

    def test_a_cloud_credential_is_never_visible_to_another_cloud_provider(
        self, tmp_path: Path
    ) -> None:
        controller, _backend, _audit = _controller(tmp_path)
        controller.store_credential("openrouter", OPENROUTER_KEY)
        assert controller.masked_credential("openrouter")
        assert controller.masked_credential("groq") == ""
        assert controller.credential_status("groq") is CredentialStatus.NOT_CONFIGURED

    def test_a_cloud_credential_is_never_visible_to_a_local_provider(self, tmp_path: Path) -> None:
        controller, _backend, _audit = _controller(tmp_path)
        controller.store_credential("openrouter", OPENROUTER_KEY)
        assert controller.masked_credential("ollama") == ""
        assert controller.credential_status("ollama") is CredentialStatus.NOT_CONFIGURED

    def test_a_key_is_stored_under_the_provider_bound_slot(self, tmp_path: Path) -> None:
        controller, backend, _audit = _controller(tmp_path)
        controller.store_credential("openrouter", OPENROUTER_KEY)
        assert backend.get("provider:openrouter:default:api_key") is not None
        assert backend.get("provider:openai:default:api_key") is None

    def test_replacing_a_credential_replaces_only_that_one(self, tmp_path: Path) -> None:
        controller, backend, _audit = _controller(tmp_path)
        controller.store_credential("openrouter", OPENROUTER_KEY)
        controller.store_credential("groq", OPENAI_KEY)
        controller.store_credential("openrouter", CUSTOM_KEY)
        replaced = backend.get("provider:openrouter:default:api_key")
        assert replaced is not None
        assert replaced.get_secret_value() == CUSTOM_KEY
        other = backend.get("provider:groq:default:api_key")
        assert other is not None
        assert other.get_secret_value() == OPENAI_KEY

    def test_a_disabled_credential_cannot_be_used(self, tmp_path: Path) -> None:
        controller, _backend, _audit = _controller(tmp_path)
        controller.store_credential("openrouter", OPENROUTER_KEY)
        assert controller.disable_credential("openrouter") is CredentialStatus.DISABLED
        assert controller.masked_credential("openrouter") == ""

    def test_a_disabled_credential_can_be_re_enabled(self, tmp_path: Path) -> None:
        controller, _backend, _audit = _controller(tmp_path)
        controller.store_credential("openrouter", OPENROUTER_KEY)
        controller.disable_credential("openrouter")
        assert controller.enable_credential("openrouter") is CredentialStatus.ACTIVE
        assert controller.masked_credential("openrouter")

    def test_removing_a_credential_leaves_the_others_alone(self, tmp_path: Path) -> None:
        controller, backend, _audit = _controller(tmp_path)
        controller.store_credential("openrouter", OPENROUTER_KEY)
        controller.store_credential("groq", OPENAI_KEY)
        assert controller.remove_credential("openrouter") is True
        assert backend.get("provider:openrouter:default:api_key") is None
        assert backend.get("provider:groq:default:api_key") is not None

    def test_an_empty_key_is_refused(self, tmp_path: Path) -> None:
        controller, _backend, _audit = _controller(tmp_path)
        with pytest.raises(CredentialVaultError):
            controller.store_credential("openrouter", "   ")

    def test_without_secure_storage_a_write_fails_closed(self, tmp_path: Path) -> None:
        controller, backend, _audit = _controller(tmp_path, secure_storage=False)
        assert (
            controller.credential_status("openrouter")
            is CredentialStatus.SECURE_STORAGE_UNAVAILABLE
        )
        with pytest.raises(CredentialVaultError):
            controller.store_credential("openrouter", OPENROUTER_KEY)
        assert backend.get("provider:openrouter:default:api_key") is None


class _FailingReadBackStore(FakeSecretStore):
    """A store whose read-back of a *new* slot fails, as a broken backend would."""

    def get(self, key: str):  # type: ignore[no-untyped-def]
        if key.startswith("provider:openrouter:"):
            return None
        return super().get(key)


class TestLegacyMigration:
    """Scenarios 11, 12, 13, 14, 15."""

    def _legacy_store(self) -> FakeSecretStore:
        store = FakeSecretStore()
        store.set("provider:openai_compatible:api_key", OPENROUTER_KEY)
        return store

    def test_the_legacy_credential_is_detected_but_not_assumed(self, tmp_path: Path) -> None:
        store = self._legacy_store()
        controller, _backend, _audit = _controller(tmp_path, store=store)
        candidate = controller.legacy_candidate()
        assert candidate is not None
        assert candidate.legacy_slot == "provider:openai_compatible:api_key"
        # Reading the candidate changes nothing.
        assert store.get("provider:openai_compatible:api_key") is not None
        assert store.get("provider:openrouter:default:api_key") is None

    def test_the_status_reports_that_a_migration_is_required(self, tmp_path: Path) -> None:
        store = self._legacy_store()
        controller, _backend, _audit = _controller(tmp_path, store=store)
        assert controller.credential_status("openrouter") is CredentialStatus.MIGRATION_REQUIRED

    def test_a_confirmed_migration_moves_the_credential(self, tmp_path: Path) -> None:
        store = self._legacy_store()
        controller, _backend, audit = _controller(tmp_path, store=store)
        result = controller.confirm_migration("openrouter")
        assert result.migrated is True
        assert store.get("provider:openai_compatible:api_key") is None
        moved = store.get("provider:openrouter:default:api_key")
        assert moved is not None
        assert moved.get_secret_value() == OPENROUTER_KEY
        assert audit.events_of_type(AuditEventType.CREDENTIAL_MIGRATION_COMPLETED)

    def test_nothing_is_migrated_without_an_explicit_action(self, tmp_path: Path) -> None:
        """Selecting a provider is not consent."""

        store = self._legacy_store()
        controller, _backend, _audit = _controller(tmp_path, store=store)
        option = controller.option_for("openrouter")
        assert option is not None
        controller.apply_provider(option)
        controller.record_provider_selected(option)
        assert store.get("provider:openai_compatible:api_key") is not None
        assert store.get("provider:openrouter:default:api_key") is None
        assert controller.credential_status("openrouter") is CredentialStatus.MIGRATION_REQUIRED

    def test_a_cancelled_migration_changes_nothing(self, tmp_path: Path) -> None:
        store = self._legacy_store()
        controller, _backend, _audit = _controller(tmp_path, store=store)
        # Cancellation is the absence of the call: the preview is read-only.
        preview = controller.migration_preview()
        assert preview["legacy_slot"] == "provider:openai_compatible:api_key"
        assert store.get("provider:openai_compatible:api_key") is not None
        assert store.get("provider:openrouter:default:api_key") is None

    def test_a_wrong_provider_choice_migrates_to_that_provider(self, tmp_path: Path) -> None:
        store = self._legacy_store()
        controller, _backend, _audit = _controller(tmp_path, store=store)
        result = controller.confirm_migration("groq")
        assert result.migrated is True
        assert store.get("provider:groq:default:api_key") is not None
        assert store.get("provider:openrouter:default:api_key") is None

    def test_an_existing_destination_is_never_overwritten(self, tmp_path: Path) -> None:
        store = self._legacy_store()
        store.set("provider:openrouter:default:api_key", CUSTOM_KEY)
        controller, _backend, _audit = _controller(tmp_path, store=store)
        # The destination already holds a credential, so there is nothing to adopt.
        assert controller.legacy_candidate() is None
        result = controller.confirm_migration("openrouter")
        assert result.migrated is False
        kept = store.get("provider:openrouter:default:api_key")
        assert kept is not None
        assert kept.get_secret_value() == CUSTOM_KEY
        assert store.get("provider:openai_compatible:api_key") is not None

    def test_a_failed_migration_preserves_the_source(self, tmp_path: Path) -> None:
        store = _FailingReadBackStore()
        store.set("provider:openai_compatible:api_key", OPENROUTER_KEY)
        controller, _backend, audit = _controller(tmp_path, store=store)
        result = controller.confirm_migration("openrouter")
        assert result.migrated is False
        assert result.status == "failed"
        assert store.get("provider:openai_compatible:api_key") is not None
        assert audit.events_of_type(AuditEventType.CREDENTIAL_MIGRATION_FAILED)

    def test_the_migration_is_idempotent(self, tmp_path: Path) -> None:
        store = self._legacy_store()
        controller, _backend, _audit = _controller(tmp_path, store=store)
        first = controller.confirm_migration("openrouter")
        assert first.migrated is True
        second = controller.confirm_migration("openrouter")
        assert second.migrated is False
        assert second.status == "nothing_to_migrate"
        moved = store.get("provider:openrouter:default:api_key")
        assert moved is not None
        assert moved.get_secret_value() == OPENROUTER_KEY

    def test_the_state_survives_a_restart(self, tmp_path: Path) -> None:
        store = self._legacy_store()
        controller, _backend, _audit = _controller(tmp_path, store=store)
        controller.confirm_migration("openrouter")
        # A restart is a new controller over the same store.
        _second, _backend2, _audit2 = _controller(tmp_path, store=store)
        assert _second.credential_status("openrouter") is CredentialStatus.ACTIVE
        assert _second.masked_credential("openrouter")

    def test_the_migration_never_sends_a_request(self, tmp_path: Path) -> None:
        store = self._legacy_store()
        controller, _backend, audit = _controller(tmp_path, store=store)
        controller.confirm_migration("openrouter")
        # The audit trail for the migration names identifiers, never a request.
        for record in audit.events_of_type(AuditEventType.CREDENTIAL_MIGRATION_COMPLETED):
            assert record.decision == "migrated"
            assert OPENROUTER_KEY not in record.to_log_line()


class TestEndpointAuthorisationAndAudit:
    """Scenarios 16, 17, 18, 19, 20, 21, 24."""

    def test_a_custom_endpoint_is_refused_by_default(self, tmp_path: Path) -> None:
        controller, _backend, _audit = _controller(tmp_path)
        controller._manager.config.provider.base_url = "https://api.example.com/v1"
        controller._manager.config.provider.provider_id = None
        status = controller.endpoint_status()
        assert status.state is EndpointState.CUSTOM_REQUIRES_AUTHORIZATION
        assert status.allowed is False

    def test_a_custom_endpoint_can_be_trusted_explicitly(self, tmp_path: Path) -> None:
        controller, _backend, _audit = _controller(tmp_path)
        controller._manager.config.provider.base_url = "https://api.example.com/v1"
        controller._manager.config.provider.provider_id = None
        status = controller.trust_custom_endpoint(True)
        assert status.state is EndpointState.APPROVED_CLOUD
        assert controller._manager.config.provider.trust_custom_endpoint is True

    def test_a_blocked_endpoint_is_shown_as_blocked(self, tmp_path: Path) -> None:
        controller, _backend, _audit = _controller(tmp_path)
        controller._manager.config.provider.base_url = "https://192.168.1.20:8443/v1"
        controller._manager.config.provider.provider_id = None
        status = controller.endpoint_status()
        assert status.state is EndpointState.BLOCKED_PRIVATE
        assert status.allowed is False

    def test_an_insecure_endpoint_is_shown_as_blocked(self, tmp_path: Path) -> None:
        controller, _backend, _audit = _controller(tmp_path)
        controller._manager.config.provider.base_url = "http://openrouter.ai/api/v1"
        status = controller.endpoint_status()
        assert status.state is EndpointState.BLOCKED_INSECURE

    def test_an_invalid_endpoint_is_shown_as_invalid(self, tmp_path: Path) -> None:
        controller, _backend, _audit = _controller(tmp_path)
        controller._manager.config.provider.base_url = "https://user:pass@api.openai.com/v1"
        status = controller.endpoint_status()
        assert status.state is EndpointState.INVALID

    def test_an_approved_cloud_endpoint_is_shown_as_approved(self, tmp_path: Path) -> None:
        controller, _backend, _audit = _controller(tmp_path)
        status = controller.endpoint_status()
        assert status.state is EndpointState.APPROVED_CLOUD
        assert status.allowed is True

    def test_a_loopback_endpoint_is_shown_as_approved(self, tmp_path: Path) -> None:
        controller, _backend, _audit = _controller(tmp_path)
        option = controller.option_for("ollama")
        assert option is not None
        controller.apply_provider(option)
        status = controller.endpoint_status()
        assert status.state is EndpointState.APPROVED_LOOPBACK

    def test_the_gui_cannot_bypass_the_gateway(self, tmp_path: Path) -> None:
        """A denied endpoint yields no credential, whatever the interface does."""

        controller, _backend, _audit = _controller(tmp_path)
        controller._manager.config.provider.base_url = "https://api.example.com/v1"
        controller._manager.config.provider.provider_id = None
        identity = controller.current_identity()
        endpoint = controller.endpoint_status()
        with pytest.raises(AuthenticationGatewayError):
            controller._gateway.credential_for(identity, _endpoint_of(identity), required=False)
        assert endpoint.allowed is False

    def test_the_audit_trail_is_renderable(self, tmp_path: Path) -> None:
        controller, _backend, _audit = _controller(tmp_path)
        controller.store_credential("openrouter", OPENROUTER_KEY)
        rows = controller.audit_rows()
        assert rows
        rendered = [row.to_log_line() for row in rows]
        assert any("credential.created" in line for line in rendered)

    def test_the_audit_trail_never_carries_a_secret(self, tmp_path: Path) -> None:
        controller, _backend, audit = _controller(tmp_path)
        controller.store_credential("openrouter", OPENROUTER_KEY)
        controller.disable_credential("openrouter")
        rendered = audit.to_json_lines()
        assert OPENROUTER_KEY not in rendered
        assert OPENROUTER_KEY not in json.dumps([row.to_dict() for row in controller.audit_rows()])

    def test_the_audit_trail_can_be_filtered(self, tmp_path: Path) -> None:
        controller, _backend, _audit = _controller(tmp_path)
        controller.store_credential("openrouter", OPENROUTER_KEY)
        controller.store_credential("groq", OPENAI_KEY)
        only_openrouter = controller.audit_rows(provider_id="openrouter")
        assert only_openrouter
        assert all(row.provider_id == "openrouter" for row in only_openrouter)
        created = controller.audit_rows(event="credential.created")
        assert created
        assert all(str(row.event) == "credential.created" for row in created)

    def test_the_audit_trail_is_session_only(self, tmp_path: Path) -> None:
        controller, _backend, _audit = _controller(tmp_path)
        assert controller.audit_is_session_only() is True

    def test_the_permission_broker_is_unchanged(self, tmp_path: Path) -> None:
        broker = PermissionBroker(StaticPermissionProvider.for_v0_2())
        from quantsmind_agent.security.permissions import Permission, PermissionRequest

        request = PermissionRequest(
            permission=Permission.WRITE_WORKSPACE,
            tool="write_file",
            summary="write hello.txt",
            target="hello.txt",
        )
        assert broker.evaluate(request) is Decision.DENY


def _endpoint_of(identity: Any):  # type: ignore[no-untyped-def]
    from quantsmind_agent.security.provider_identity import canonicalize_endpoint

    return canonicalize_endpoint(identity.endpoint_id)
