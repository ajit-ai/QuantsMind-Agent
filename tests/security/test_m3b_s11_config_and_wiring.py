"""Configuration and application-wiring security tests (M3B-S1.1).

These cover the seam between the configuration, the credential store and the
provider factory: the places where a key is resolved and where the decision
about *which* key to use is actually made.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

import pytest
from tests.security.test_m3b_s11_credential_security import (
    OPENAI_BASE,
    OPENROUTER_BASE,
    FakeSecretStore,
)

from quantsmind_agent.config import ConfigManager
from quantsmind_agent.credential_vault import CredentialVault, CredentialVaultError
from quantsmind_agent.paths import AppPaths
from quantsmind_agent.secrets import FileSecretStore, KeyringSecretStore

OPENROUTER_KEY = "sk-canary-openrouter-0000000000"
OPENAI_KEY = "sk-canary-openai-1111111111"


def _paths(tmp_path: Path) -> AppPaths:
    return AppPaths(
        config_dir=tmp_path / "config",
        log_dir=tmp_path / "logs",
        cache_dir=tmp_path / "cache",
        data_dir=tmp_path / "data",
    ).ensure()


def _manager(
    tmp_path: Path,
    store: Any = None,
    base_url: str = OPENROUTER_BASE,
    provider_id: str | None = "openrouter",
) -> ConfigManager:
    paths = _paths(tmp_path)
    manager = ConfigManager(paths, secret_store=store or FakeSecretStore())
    # The configuration is persisted, because ApplicationContext.create()
    # re-reads it: an in-memory edit would not survive that reload.
    binding = f'provider_id = "{provider_id}"\n' if provider_id else ""
    (paths.config_dir / "config.toml").write_text(
        f'[provider]\nbase_url = "{base_url}"\n' + binding,
        encoding="utf-8",
    )
    manager.load()
    return manager


class TestProviderBoundCredentialResolution:
    def test_the_credential_slot_is_provider_bound(self, tmp_path: Path) -> None:
        manager = _manager(tmp_path)
        assert manager.api_key_secret_key() == "provider:openrouter:default:api_key"
        assert manager.legacy_api_key_secret_key() == "provider:openai_compatible:api_key"

    def test_a_stored_key_is_found_under_the_provider_bound_slot(self, tmp_path: Path) -> None:
        store = FakeSecretStore()
        manager = _manager(tmp_path, store)
        store.set("provider:openrouter:default:api_key", OPENROUTER_KEY)
        assert manager.api_key_value() == OPENROUTER_KEY

    def test_the_legacy_shared_slot_does_not_authenticate_a_provider(self, tmp_path: Path) -> None:
        store = FakeSecretStore()
        manager = _manager(tmp_path, store)
        store.set("provider:openai_compatible:api_key", OPENROUTER_KEY)
        # The shared slot is not reassigned to a provider on its own.
        assert manager.api_key_value() is None

    def test_switching_providers_does_not_reuse_the_key(self, tmp_path: Path) -> None:
        store = FakeSecretStore()
        manager = _manager(tmp_path, store)
        store.set("provider:openrouter:default:api_key", OPENROUTER_KEY)
        assert manager.api_key_value() == OPENROUTER_KEY

        # The operator points the application at a different service.
        manager.config.provider.base_url = OPENAI_BASE
        manager.config.provider.provider_id = "openai"
        assert manager.api_key_value() is None

    def test_a_provider_specific_environment_variable_is_used(self, tmp_path: Path) -> None:
        manager = _manager(tmp_path)
        with pytest.MonkeyPatch.context() as patch:
            patch.setenv("OPENROUTER_API_KEY", OPENROUTER_KEY)
            assert manager.api_key_value() == OPENROUTER_KEY

    def test_the_generic_environment_variable_is_not_sent_to_another_provider(
        self, tmp_path: Path
    ) -> None:
        manager = _manager(tmp_path)
        with pytest.MonkeyPatch.context() as patch:
            patch.setenv("QM_API_KEY", OPENAI_KEY)
            patch.delenv("OPENROUTER_API_KEY", raising=False)
            # The generic variable belongs to the default provider only.
            assert manager.api_key_value() is None


class TestVaultStorageRules:
    def test_the_vault_refuses_a_plaintext_store(self, tmp_path: Path) -> None:
        store = FileSecretStore(_paths(tmp_path).secrets_file)
        with pytest.raises(CredentialVaultError):
            CredentialVault(store, metadata_path=None, require_secure=True)

    def test_a_plaintext_store_is_readable_but_not_writable(self, tmp_path: Path) -> None:
        store = FileSecretStore(_paths(tmp_path).secrets_file)
        vault = CredentialVault(
            store, metadata_path=None, require_secure=False, allow_insecure_writes=False
        )
        with pytest.raises(CredentialVaultError):
            vault.set_secret("provider:openai:default:api_key", OPENAI_KEY)

    def test_a_secure_store_is_writable(self, tmp_path: Path) -> None:
        vault = CredentialVault(FakeSecretStore(), metadata_path=None, require_secure=True)
        vault.set_secret("provider:openai:default:api_key", OPENAI_KEY)
        assert vault.has_secret("provider:openai:default:api_key") is True

    def test_the_file_store_is_not_secure_and_the_keyring_is(self, tmp_path: Path) -> None:
        file_store = FileSecretStore(_paths(tmp_path).secrets_file)
        assert file_store.is_secure is False
        keyring_store = KeyringSecretStore(file_store)
        # Secure only when the OS credential manager is actually available.
        assert keyring_store.is_secure in (True, False)


class TestApplicationWiring:
    def test_the_provider_credential_is_released_through_the_gateway(self, tmp_path: Path) -> None:
        from quantsmind_agent.app import ApplicationContext

        store = FakeSecretStore()
        store.set("provider:openrouter:default:api_key", OPENROUTER_KEY)
        manager = _manager(tmp_path, store)
        manager.config.provider.base_url = OPENROUTER_BASE
        manager.config.provider.provider_id = "openrouter"

        context = ApplicationContext.create(
            paths=manager.paths,
            config_manager=manager,
            security_resolver=lambda host: ["93.184.216.34"],
        )
        provider = context.gateway.provider
        # The provider holds a callable, never the value.
        source = cast(Callable[[], str | None], cast(Any, provider)._api_key_source)
        assert not isinstance(source, str)
        assert source() == OPENROUTER_KEY

    def test_a_refused_endpoint_yields_no_credential(self, tmp_path: Path) -> None:
        from quantsmind_agent.app import ApplicationContext
        from quantsmind_agent.auth_gateway import AuthenticationGatewayError

        store = FakeSecretStore()
        store.set("provider:openrouter:default:api_key", OPENROUTER_KEY)
        # A custom endpoint is not trusted by default.
        manager = _manager(tmp_path, store, base_url="https://api.example.com/v1", provider_id=None)

        context = ApplicationContext.create(
            paths=manager.paths,
            config_manager=manager,
            security_resolver=lambda host: ["93.184.216.34"],
        )
        provider = context.gateway.provider
        with pytest.raises(AuthenticationGatewayError):
            cast(Callable[[], str | None], cast(Any, provider)._api_key_source)()

    def test_the_command_tool_environment_excludes_credential_variables(
        self, tmp_path: Path
    ) -> None:
        from quantsmind_agent.command_tools import sanitized_environment

        with pytest.MonkeyPatch.context() as patch:
            patch.setenv("OPENROUTER_API_KEY", OPENROUTER_KEY)
            patch.setenv("QM_API_KEY", OPENAI_KEY)
            env = sanitized_environment()
        assert "OPENROUTER_API_KEY" not in env
        assert "QM_API_KEY" not in env
        assert OPENROUTER_KEY not in "".join(env.values())
