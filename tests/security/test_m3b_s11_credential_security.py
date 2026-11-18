"""Adversarial security tests for the credential and endpoint boundary (M3B-S1.1).

Every test in this module is offline and deterministic:

* credentials are **fake canaries**, never the real stored key;
* HTTP is served by an in-process ``httpx`` mock transport;
* name resolution is injected, so no DNS query is ever made.

The module is organised as the twenty mandatory scenarios of the milestone,
one class per concern, plus the regression coverage for the OpenRouter
tool-call and denial behaviour demonstrated in M3B-1B.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from pydantic import SecretStr

from quantsmind_agent.app.settings import PROVIDER_PRESETS, ProviderSettings
from quantsmind_agent.errors import LLMProtocolError
from quantsmind_agent.providers.gateway import ChatRequest
from quantsmind_agent.providers.openai_compat import OpenAICompatProvider
from quantsmind_agent.schema import SystemMessage, UserMessage
from quantsmind_agent.security.audit import AuditEventType, SecurityAuditLog
from quantsmind_agent.security.auth_gateway import (
    AuthenticationGatewayError,
    AuthGateway,
    AuthorizationDecision,
)
from quantsmind_agent.security.credential_vault import (
    CredentialState,
    CredentialVault,
)
from quantsmind_agent.security.permissions import (
    Decision,
    PermissionBroker,
    StaticPermissionProvider,
)
from quantsmind_agent.security.provider_identity import (
    EndpointIdentity,
    EndpointPolicyError,
    ProviderIdentity,
    ProviderTrust,
    canonicalize_endpoint,
    rebind_explicit_provider_id,
    resolve_provider_identity,
)
from quantsmind_agent.security.secrets import SecretStore
from quantsmind_agent.services.workspace.containment import Workspace
from quantsmind_agent.workspace_tools import build_default_tools

#: Fake credentials. They are canaries: if one of these ever appears in a log,
#: an error message or a tool result, the test that checks for it fails.
OPENROUTER_KEY = "sk-canary-openrouter-0000000000"
OPENAI_KEY = "sk-canary-openai-1111111111"
GROQ_KEY = "sk-canary-groq-2222222222"
CUSTOM_KEY = "sk-canary-custom-3333333333"

OPENROUTER_BASE = "https://openrouter.ai/api/v1"
OPENAI_BASE = "https://api.openai.com/v1"
GROQ_BASE = "https://api.groq.com/openai/v1"
CUSTOM_BASE = "https://api.example.com/v1"


class FakeSecretStore(SecretStore):
    """An in-memory secret store that claims to be a secure one."""

    def __init__(self) -> None:
        self._values: dict[str, str] = {}

    @property
    def is_secure(self) -> bool:
        return True

    def get(self, key: str) -> SecretStr | None:
        value = self._values.get(key)
        return SecretStr(value) if value else None

    def set(self, key: str, value: SecretStr | str) -> None:
        raw = value.get_secret_value() if hasattr(value, "get_secret_value") else str(value)
        self._values[key] = raw

    def delete(self, key: str) -> bool:
        return self._values.pop(key, None) is not None

    def keys(self) -> list[str]:
        return sorted(self._values)


def _vault(
    store: FakeSecretStore | None = None, audit: SecurityAuditLog | None = None
) -> CredentialVault:
    return CredentialVault(
        store or FakeSecretStore(),
        metadata_path=None,
        audit=audit,
        require_secure=True,
    )


def _gateway(
    vault: CredentialVault,
    audit: SecurityAuditLog | None = None,
    **kwargs: Any,
) -> AuthGateway:
    options: dict[str, Any] = {
        "vault": vault,
        "audit": audit,
        # Every public host resolves to a public address, so the DNS check
        # never touches the network.
        "resolver": lambda host: ["93.184.216.34"],
    }
    options.update(kwargs)
    return AuthGateway(**options)


def _identity(
    base_url: str, provider_id: str | None = None
) -> tuple[ProviderIdentity, EndpointIdentity]:
    endpoint = canonicalize_endpoint(base_url)
    return (
        resolve_provider_identity(endpoint, explicit_provider_id=provider_id),
        endpoint,
    )


def _request() -> ChatRequest:
    return ChatRequest(
        messages=[SystemMessage(content="s"), UserMessage(content="hi")],
        model="test-model",
    )


class TestCrossProviderCredentialReuse:
    """Scenarios 1-4: one service's key must never reach another service."""

    def test_an_openrouter_key_cannot_be_used_for_openai(self) -> None:
        vault = _vault()
        vault.set_secret("provider:openrouter:default:api_key", OPENROUTER_KEY)
        gateway = _gateway(vault)

        identity, endpoint = _identity(OPENAI_BASE)
        assert identity.provider_id == "openai"
        assert gateway.credential_for(identity, endpoint, required=False) is None

    def test_an_openai_key_cannot_be_used_for_openrouter(self) -> None:
        vault = _vault()
        vault.set_secret("provider:openai:default:api_key", OPENAI_KEY)
        gateway = _gateway(vault)

        identity, endpoint = _identity(OPENROUTER_BASE)
        assert identity.provider_id == "openrouter"
        assert gateway.credential_for(identity, endpoint, required=False) is None

    def test_a_custom_endpoint_cannot_inherit_another_providers_key(self) -> None:
        vault = _vault()
        vault.set_secret("provider:openai:default:api_key", OPENAI_KEY)
        gateway = _gateway(vault, trust_custom_endpoints=True)

        identity, endpoint = _identity(CUSTOM_BASE)
        assert identity.trust is ProviderTrust.CUSTOM
        assert gateway.credential_for(identity, endpoint, required=False) is None

    def test_a_display_name_spoof_does_not_win_the_credential(self) -> None:
        """A configuration that *claims* to be OpenRouter is not OpenRouter."""

        vault = _vault()
        vault.set_secret("provider:openrouter:default:api_key", OPENROUTER_KEY)
        gateway = _gateway(vault)

        spoofed = ProviderSettings(
            name="OpenRouter",
            base_url=OPENAI_BASE,
            model="gpt-4o-mini",
        )
        identity = spoofed.identity()
        assert identity.provider_id == "openai"
        endpoint = canonicalize_endpoint(spoofed.base_url)
        assert gateway.credential_for(identity, endpoint, required=False) is None

    def test_the_credential_identifiers_of_two_services_differ(self) -> None:
        openrouter, _ = _identity(OPENROUTER_BASE)
        openai, _ = _identity(OPENAI_BASE)
        groq, _ = _identity(GROQ_BASE)
        assert openrouter.credential_id != openai.credential_id
        assert openai.credential_id != groq.credential_id
        assert openrouter.credential_id == "provider:openrouter:default:api_key"


class TestSettingsEditDoesNotCarryTheCredential:
    """The exact Phase 0.7 leak: editing Settings' base URL kept the old id.

    The Settings dialog used to preserve ``provider_id`` when the address was
    changed. A configuration that named OpenRouter while pointing at an
    arbitrary host therefore kept ``provider:openrouter:default:api_key``, and
    with custom endpoints trusted, another service's key went on the wire.
    """

    def _stale_settings(self) -> ProviderSettings:
        # What the old dialog saved: the endpoint was changed, the provider
        # binding was not, and the operator had trusted custom endpoints.
        return ProviderSettings(
            name="openai_compatible",
            base_url=CUSTOM_BASE,
            provider_id="openrouter",
            trust_custom_endpoint=True,
        )

    def test_a_retained_provider_id_on_another_endpoint_is_a_mismatch(self) -> None:
        identity = self._stale_settings().identity()

        assert identity.requested_provider_id == "openrouter"
        assert identity.provider_id != "openrouter"
        assert identity.credential_id != "provider:openrouter:default:api_key"
        assert identity.identity_mismatch is True

    def test_the_old_providers_key_is_never_released_to_the_new_endpoint(self) -> None:
        vault = _vault()
        vault.set_secret("provider:openrouter:default:api_key", OPENROUTER_KEY)
        gateway = _gateway(vault, trust_custom_endpoints=True)

        identity = self._stale_settings().identity()
        endpoint = canonicalize_endpoint(CUSTOM_BASE)

        # Trusting custom endpoints does not make another provider's
        # credential belong to this one. A mismatch is a policy refusal, so it
        # raises rather than looking like "no key is needed".
        for required in (False, True):
            with pytest.raises(AuthenticationGatewayError) as info:
                gateway.credential_for(identity, endpoint, required=required)
            assert info.value.details.get("reason_code") == "provider_endpoint_mismatch"
            assert OPENROUTER_KEY not in str(info.value)
            assert OPENROUTER_KEY not in str(info.value.user_message)

        resolution = gateway.resolve_credential(identity, endpoint, required=False)
        assert resolution.reason_code == "provider_endpoint_mismatch"
        assert resolution.value is None

    def test_a_matched_endpoint_keeps_its_binding(self) -> None:
        endpoint = canonicalize_endpoint(OPENROUTER_BASE)

        assert rebind_explicit_provider_id(endpoint, "openrouter") == "openrouter"
        assert rebind_explicit_provider_id(endpoint, "openai") is None

    def test_a_changed_endpoint_clears_the_binding(self) -> None:
        endpoint = canonicalize_endpoint(CUSTOM_BASE)

        # The value the Settings dialog now persists after an address edit.
        assert rebind_explicit_provider_id(endpoint, "openrouter") is None


class TestEndpointAuthorization:
    """Scenarios 5-11: where a credential may travel, and how."""

    def test_a_cross_host_redirect_is_refused(self) -> None:
        """A redirect is reported, not followed, so the key is not re-sent."""

        seen: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return httpx.Response(
                302,
                headers={"location": "https://evil.test/v1/chat/completions"},
                json={"error": {"message": "moved"}},
            )

        provider = OpenAICompatProvider(
            OPENROUTER_BASE,
            api_key=OPENROUTER_KEY,
            requires_api_key=True,
            transport=httpx.MockTransport(handler),
        )
        with pytest.raises(LLMProtocolError) as info:
            provider.complete(_request())
        assert "redirect" in info.value.message.lower()
        # Exactly one request was made, to the configured host, and the
        # redirect target was never contacted.
        assert len(seen) == 1
        assert str(seen[0].url.host) == "openrouter.ai"

    def test_an_http_downgrade_is_refused(self) -> None:
        vault = _vault()
        vault.set_secret("provider:openrouter:default:api_key", OPENROUTER_KEY)
        gateway = _gateway(vault)

        identity = resolve_provider_identity(canonicalize_endpoint(OPENROUTER_BASE))
        endpoint = canonicalize_endpoint("http://openrouter.ai/api/v1")
        decision = gateway.authorize(identity, endpoint)
        assert decision.decision is AuthorizationDecision.DENIED
        assert decision.reason_code == "scheme_downgrade"

    def test_a_localhost_destination_is_refused_for_a_cloud_credential(self) -> None:
        vault = _vault()
        vault.set_secret("provider:openrouter:default:api_key", OPENROUTER_KEY)
        gateway = _gateway(vault)

        identity = resolve_provider_identity(canonicalize_endpoint(OPENROUTER_BASE))
        endpoint = canonicalize_endpoint("https://localhost:8443/v1")
        decision = gateway.authorize(identity, endpoint)
        assert decision.decision is AuthorizationDecision.DENIED
        assert decision.reason_code in {"loopback_destination", "endpoint_not_bound"}

    def test_a_private_destination_is_refused_under_cloud_policy(self) -> None:
        vault = _vault()
        gateway = _gateway(vault)

        identity = resolve_provider_identity(canonicalize_endpoint(OPENROUTER_BASE))
        endpoint = canonicalize_endpoint("https://192.168.1.20:8443/v1")
        decision = gateway.authorize(identity, endpoint)
        assert decision.decision is AuthorizationDecision.DENIED
        assert decision.reason_code in {"private_destination", "endpoint_not_bound"}

    def test_a_metadata_endpoint_is_refused(self) -> None:
        vault = _vault()
        gateway = _gateway(vault)

        identity = resolve_provider_identity(canonicalize_endpoint(OPENROUTER_BASE))
        endpoint = canonicalize_endpoint("http://169.254.169.254/latest/meta-data")
        decision = gateway.authorize(identity, endpoint)
        assert decision.decision is AuthorizationDecision.DENIED
        assert decision.reason_code in {
            "metadata_endpoint",
            "link_local_destination",
            "scheme_downgrade",
        }

    def test_a_custom_endpoint_needs_an_explicit_trust_decision(self) -> None:
        vault = _vault()
        vault.set_secret(
            f"provider:custom-{_endpoint_hash(CUSTOM_BASE)}:default:api_key", CUSTOM_KEY
        )
        identity, endpoint = _identity(CUSTOM_BASE)

        strict = _gateway(vault)
        assert strict.authorize(identity, endpoint).decision is AuthorizationDecision.DENIED

        trusting = _gateway(vault, trust_custom_endpoints=True)
        assert trusting.authorize(identity, endpoint).decision is AuthorizationDecision.ALLOWED

    def test_a_local_provider_receives_no_cloud_key(self) -> None:
        vault = _vault()
        vault.set_secret("provider:openrouter:default:api_key", OPENROUTER_KEY)
        gateway = _gateway(vault)

        identity, endpoint = _identity("http://localhost:11434/v1")
        assert identity.trust is ProviderTrust.LOCAL
        headers = gateway.headers_for(identity, endpoint, required=False)
        assert headers == {}
        assert OPENROUTER_KEY not in json.dumps(headers)

    def test_a_remote_local_model_server_is_refused_by_default(self) -> None:
        vault = _vault()
        gateway = _gateway(vault)
        identity, endpoint = _identity("http://192.168.1.50:11434/v1")
        assert identity.trust is ProviderTrust.CUSTOM
        decision = gateway.authorize(identity, endpoint)
        assert decision.decision is AuthorizationDecision.DENIED

    def test_a_remote_local_model_server_can_be_authorised_explicitly(self) -> None:
        vault = _vault()
        gateway = _gateway(
            vault,
            trust_custom_endpoints=True,
            allow_remote_local_models=True,
        )
        identity = resolve_provider_identity(
            canonicalize_endpoint("http://192.168.1.50:11434/v1"),
            explicit_provider_id="ollama",
        )
        endpoint = canonicalize_endpoint("http://192.168.1.50:11434/v1")
        assert identity.trust is ProviderTrust.LOCAL
        assert gateway.authorize(identity, endpoint).decision is AuthorizationDecision.ALLOWED

    def test_a_nat64_address_is_judged_by_the_ipv4_it_carries(self) -> None:
        """A public host that publishes an AAAA record is still a public host.

        Python classifies ``64:ff9b::/96`` (NAT64) as private, so a naive
        check would refuse every provider that publishes an IPv6 record on an
        IPv6-enabled network.
        """

        vault = _vault()
        gateway = _gateway(vault, resolver=lambda host: ["64:ff9b::a29f:8cf5", "93.184.216.34"])
        identity, endpoint = _identity(OPENROUTER_BASE)
        assert gateway.authorize(identity, endpoint).decision is AuthorizationDecision.ALLOWED

    def test_a_host_that_resolves_to_a_private_address_is_refused(self) -> None:
        vault = _vault()
        gateway = _gateway(vault, resolver=lambda host: ["10.0.0.5"])
        identity, endpoint = _identity(OPENROUTER_BASE)
        decision = gateway.authorize(identity, endpoint)
        assert decision.decision is AuthorizationDecision.DENIED
        assert decision.reason_code == "dns_resolves_to_non_public"

    def test_a_host_that_does_not_resolve_is_refused(self) -> None:
        vault = _vault()
        gateway = _gateway(vault, resolver=lambda host: [])
        identity, endpoint = _identity(OPENROUTER_BASE)
        decision = gateway.authorize(identity, endpoint)
        assert decision.decision is AuthorizationDecision.DENIED
        assert decision.reason_code == "dns_unresolved"


def _endpoint_hash(base_url: str) -> str:
    from quantsmind_agent.security.provider_identity import _endpoint_hash as digest

    return digest(canonicalize_endpoint(base_url))


class TestInvalidEndpointsFailClosed:
    """Scenario 10: an endpoint the application cannot parse is refused."""

    @pytest.mark.parametrize(
        "bad",
        [
            "ftp://api.openai.com/v1",
            "https://user:pass@api.openai.com/v1",
            "https://api.openai.com:99999/v1",
            "https://api .openai.com/v1",
            "https://api.openai.com/../v1",
            "api.openai.com",
            "",
        ],
    )
    def test_malformed_endpoints_are_refused(self, bad: str) -> None:
        with pytest.raises(EndpointPolicyError):
            canonicalize_endpoint(bad)

    def test_a_malformed_endpoint_never_produces_a_credential(self) -> None:
        vault = _vault()
        vault.set_secret("provider:openai:default:api_key", OPENAI_KEY)
        gateway = _gateway(vault)
        with pytest.raises(EndpointPolicyError):
            endpoint = canonicalize_endpoint("https://user:pass@api.openai.com/v1")
            identity = resolve_provider_identity(endpoint)
            gateway.credential_for(identity, endpoint)


class TestCredentialLifecycle:
    """Scenarios 12-14: deletion, rotation and migration."""

    def test_deletion_affects_only_the_intended_provider(self) -> None:
        vault = _vault()
        vault.set_secret("provider:openrouter:default:api_key", OPENROUTER_KEY)
        vault.set_secret("provider:openai:default:api_key", OPENAI_KEY)

        assert vault.delete_secret("provider:openrouter:default:api_key") is True
        assert vault.get_secret("provider:openrouter:default:api_key") is None
        assert vault.get_secret("provider:openai:default:api_key") is not None
        assert vault.state("provider:openrouter:default:api_key") is CredentialState.REVOKED

    def test_rotation_does_not_corrupt_other_credentials(self) -> None:
        vault = _vault()
        vault.set_secret("provider:openrouter:default:api_key", OPENROUTER_KEY)
        vault.set_secret("provider:openai:default:api_key", OPENAI_KEY)

        vault.rotate_secret("provider:openrouter:default:api_key", "sk-canary-rotated")
        rotated = vault.get_secret("provider:openrouter:default:api_key")
        assert rotated is not None
        assert rotated.get_secret_value() == "sk-canary-rotated"
        untouched = vault.get_secret("provider:openai:default:api_key")
        assert untouched is not None
        assert untouched.get_secret_value() == OPENAI_KEY

    def test_a_disabled_credential_cannot_be_used(self) -> None:
        vault = _vault()
        vault.set_secret("provider:openrouter:default:api_key", OPENROUTER_KEY)
        vault.disable("provider:openrouter:default:api_key")
        assert vault.get_secret("provider:openrouter:default:api_key") is None
        assert vault.has_secret("provider:openrouter:default:api_key") is False

    def test_a_failed_migration_preserves_the_original_credential(self) -> None:
        store = FakeSecretStore()
        store.set("provider:openai_compatible:api_key", OPENROUTER_KEY)
        vault = CredentialVault(store, metadata_path=None, require_secure=True)
        identity, endpoint = _identity(OPENROUTER_BASE)
        candidate = vault.detect_legacy_credential(
            "provider:openai_compatible:api_key",
            provider_id=identity.provider_id,
            endpoint_id=endpoint.endpoint_id,
            provider_is_known=True,
        )
        assert candidate is not None

        # A migration that fails half way must leave the old value in place.
        def exploding_confirm(_candidate: Any) -> bool:
            raise RuntimeError("the confirmation dialog failed")

        result = vault.migrate_legacy_credential(candidate, confirm=exploding_confirm)
        assert result.migrated is False
        assert result.status == "failed"
        assert store.get("provider:openai_compatible:api_key") is not None
        assert store.get("provider:openrouter:default:api_key") is None

    def test_a_migration_without_confirmation_does_nothing(self) -> None:
        store = FakeSecretStore()
        store.set("provider:openai_compatible:api_key", OPENROUTER_KEY)
        vault = CredentialVault(store, metadata_path=None, require_secure=True)
        identity, endpoint = _identity(OPENROUTER_BASE)
        candidate = vault.detect_legacy_credential(
            "provider:openai_compatible:api_key",
            provider_id=identity.provider_id,
            endpoint_id=endpoint.endpoint_id,
            provider_is_known=True,
        )
        assert candidate is not None
        result = vault.migrate_legacy_credential(candidate)
        assert result.status == "confirmation_required"
        assert store.get("provider:openai_compatible:api_key") is not None

    def test_a_confirmed_migration_moves_the_credential_safely(self) -> None:
        store = FakeSecretStore()
        store.set("provider:openai_compatible:api_key", OPENROUTER_KEY)
        vault = CredentialVault(store, metadata_path=None, require_secure=True)
        identity, endpoint = _identity(OPENROUTER_BASE)
        candidate = vault.detect_legacy_credential(
            "provider:openai_compatible:api_key",
            provider_id=identity.provider_id,
            endpoint_id=endpoint.endpoint_id,
            provider_is_known=True,
        )
        assert candidate is not None
        result = vault.migrate_legacy_credential(candidate, confirm=lambda _c: True)
        assert result.migrated is True
        assert store.get("provider:openai_compatible:api_key") is None
        migrated = store.get("provider:openrouter:default:api_key")
        assert migrated is not None
        assert migrated.get_secret_value() == OPENROUTER_KEY

    def test_confirming_the_legacy_owner_is_non_destructive(self) -> None:
        """The remedy an existing installation needs: nothing moves."""

        store = FakeSecretStore()
        store.set("provider:openai_compatible:api_key", OPENROUTER_KEY)
        vault = CredentialVault(store, metadata_path=None, require_secure=True)
        identity, endpoint = _identity(OPENROUTER_BASE)

        vault.confirm_legacy_ownership(
            identity.credential_id,
            "provider:openai_compatible:api_key",
            provider_id=identity.provider_id,
            endpoint_id=endpoint.endpoint_id,
        )
        # The value stays where it was ...
        assert store.get("provider:openai_compatible:api_key") is not None
        # ... and is read through the provider-bound identity.
        resolved = vault.get_secret(identity.credential_id)
        assert resolved is not None
        assert resolved.get_secret_value() == OPENROUTER_KEY
        assert vault.state(identity.credential_id) is CredentialState.CONFIGURED


class TestSecretSafeDiagnostics:
    """Scenarios 15-16: a credential never reaches a log, an error or a tool."""

    def test_an_audit_record_refuses_to_carry_a_secret(self) -> None:
        audit = SecurityAuditLog()
        with pytest.raises(ValueError):
            audit.record(
                AuditEventType.CREDENTIAL_CREATED,
                credential_id="provider:openrouter:default:api_key",
                secret=OPENROUTER_KEY,
            )

    def test_audit_records_carry_identifiers_and_no_values(self) -> None:
        audit = SecurityAuditLog()
        audit.record(
            AuditEventType.ENDPOINT_AUTHORIZED,
            provider_id="openrouter",
            credential_id="provider:openrouter:default:api_key",
            endpoint_id=OPENROUTER_BASE,
            decision="allowed",
            reason_code="cloud_endpoint_authorized",
        )
        rendered = audit.to_json_lines()
        assert OPENROUTER_KEY not in rendered
        assert "provider:openrouter:default:api_key" in rendered
        assert json.loads(rendered.strip())["event"] == "endpoint.authorized"

    def test_an_exception_does_not_disclose_the_credential(self) -> None:
        vault = _vault()
        vault.set_secret("provider:openrouter:default:api_key", OPENROUTER_KEY)
        gateway = _gateway(vault)
        identity, endpoint = _identity(OPENAI_BASE)
        with pytest.raises(AuthenticationGatewayError) as info:
            gateway.credential_for(identity, endpoint, required=True)
        rendered = f"{info.value} {info.value.user_message} {info.value.details}"
        assert OPENROUTER_KEY not in rendered

    def test_the_redactor_removes_a_canary_from_free_text(self) -> None:
        from quantsmind_agent.diagnostics.logging import redact

        assert OPENROUTER_KEY not in redact(f"the key is {OPENROUTER_KEY} here")

    def test_no_tool_schema_or_tool_result_exposes_the_vault(self, tmp_path: Path) -> None:
        """The model-facing surface has no route to a credential."""

        vault = _vault()
        vault.set_secret("provider:openrouter:default:api_key", OPENROUTER_KEY)
        workspace = Workspace(tmp_path)
        tools = build_default_tools(workspace)
        rendered = json.dumps([tool.spec.to_openai_schema() for tool in tools])
        assert OPENROUTER_KEY not in rendered
        for word in ("vault", "credential", "secret", "api_key", "token"):
            assert word not in rendered.lower()
        for tool in tools:
            assert not hasattr(tool, "vault")
            assert not hasattr(tool, "secret_store")

    def test_a_tool_result_cannot_read_the_credential_store(self, tmp_path: Path) -> None:
        """A file the model can read does not contain the credential."""

        store = FakeSecretStore()
        store.set("provider:openrouter:default:api_key", OPENROUTER_KEY)
        workspace = Workspace(tmp_path)
        tools = {tool.spec.name: tool for tool in build_default_tools(workspace)}
        (tmp_path / "notes.txt").write_text("nothing secret here", encoding="utf-8")
        result = tools["read_file"].run({"path": "notes.txt"})
        assert OPENROUTER_KEY not in result.to_observation()


class TestFallbackAndMissingCredentials:
    """Scenarios 17-18: a fallback cannot bypass the policy."""

    def test_a_fallback_endpoint_cannot_borrow_the_primary_credential(self) -> None:
        vault = _vault()
        vault.set_secret("provider:openrouter:default:api_key", OPENROUTER_KEY)
        gateway = _gateway(vault)

        fallback_identity, fallback_endpoint = _identity(GROQ_BASE)
        assert fallback_identity.provider_id == "groq"
        assert gateway.credential_for(fallback_identity, fallback_endpoint, required=False) is None

    def test_a_missing_credential_produces_a_safe_actionable_error(self) -> None:
        vault = _vault()
        gateway = _gateway(vault)
        identity, endpoint = _identity(OPENROUTER_BASE)
        with pytest.raises(AuthenticationGatewayError) as info:
            gateway.credential_for(identity, endpoint, required=True)
        assert info.value.details["reason_code"] == "credential_missing"
        assert "Settings" in info.value.user_message
        assert OPENROUTER_KEY not in info.value.user_message


class TestCompatibility:
    """Scenarios 19-20: the existing product behaviour still holds."""

    def test_the_provider_presets_still_resolve(self) -> None:
        assert PROVIDER_PRESETS["OpenAI"]["base_url"] == OPENAI_BASE
        assert PROVIDER_PRESETS["OpenRouter"]["base_url"] == OPENROUTER_BASE
        settings = ProviderSettings(base_url=OPENROUTER_BASE)
        assert settings.identity().provider_id == "openrouter"
        assert settings.requires_api_key() is True
        assert settings.capabilities() == PROVIDER_PRESETS["OpenRouter"]["capabilities"]

    def test_local_presets_still_need_no_credential(self) -> None:
        for url in (
            "http://localhost:11434/v1",
            "http://127.0.0.1:8080/v1",
            "http://0.0.0.0:8000/v1",
        ):
            settings = ProviderSettings(base_url=url)
            assert settings.requires_api_key() is False
            assert settings.identity().trust is ProviderTrust.LOCAL

    def test_the_permission_broker_still_asks_before_a_write(self) -> None:
        broker = PermissionBroker(StaticPermissionProvider.for_v0_2())
        from quantsmind_agent.security.permissions import Permission, PermissionRequest

        request = PermissionRequest(
            permission=Permission.WRITE_WORKSPACE,
            tool="write_file",
            summary="write hello.txt",
            target="hello.txt",
        )
        assert broker.evaluate(request) is Decision.DENY
        assert broker.history[-1][1] is Decision.DENY

    def test_the_openrouter_rehearsal_behaviour_is_unchanged(self) -> None:
        """The M3B-1B demonstration still holds: a structured call, then DENY."""

        from quantsmind_agent.security.permissions import (
            ApprovalOutcome,
            Permission,
            PermissionRequest,
        )

        broker = PermissionBroker(
            StaticPermissionProvider.for_v0_2(), approver=lambda _r: ApprovalOutcome.DENY
        )
        request = PermissionRequest(
            permission=Permission.WRITE_WORKSPACE,
            tool="write_file",
            summary="write hello.txt",
            target="hello.txt",
        )
        assert broker.evaluate(request) is Decision.DENY
        assert broker.approvals[-1][1] is ApprovalOutcome.DENY
