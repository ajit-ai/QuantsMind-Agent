"""Unit tests for configuration models and the TOML writer."""

from __future__ import annotations

import tomllib

import pytest
from pydantic import ValidationError

from quantsmind_agent.config import (
    PROVIDER_PRESETS,
    AgentSettings,
    ConfigManager,
    Configuration,
    ProviderSettings,
    default_configuration,
)
from quantsmind_agent.errors import ConfigurationError
from quantsmind_agent.toml_writer import dumps


class TestProviderSettings:
    def test_defaults_are_sane(self) -> None:
        provider = ProviderSettings()
        assert provider.base_url == "https://api.openai.com/v1"
        assert provider.timeout_seconds > 0
        assert provider.requires_api_key()

    def test_base_url_is_normalised(self) -> None:
        assert ProviderSettings(base_url="https://x.test/v1/").base_url == "https://x.test/v1"

    @pytest.mark.parametrize("bad", ["", "ftp://x", "api.openai.com", "   "])
    def test_bad_base_url_rejected(self, bad: str) -> None:
        with pytest.raises(ValidationError):
            ProviderSettings(base_url=bad)

    def test_blank_model_rejected(self) -> None:
        with pytest.raises(ValidationError):
            ProviderSettings(model="   ")

    @pytest.mark.parametrize(
        "url",
        [
            "http://localhost:11434/v1",
            "http://127.0.0.1:8080/v1",
            "http://0.0.0.0:8000/v1",
        ],
    )
    def test_local_endpoints_do_not_require_a_key(self, url: str) -> None:
        assert ProviderSettings(base_url=url).requires_api_key() is False

    def test_ranges_are_enforced(self) -> None:
        with pytest.raises(ValidationError):
            ProviderSettings(timeout_seconds=0)
        with pytest.raises(ValidationError):
            ProviderSettings(temperature=9)
        with pytest.raises(ValidationError):
            ProviderSettings(max_output_tokens=0)


class TestConfiguration:
    def test_defaults(self) -> None:
        config = default_configuration()
        assert config.schema_version == 1
        assert config.agent.max_iterations >= 1
        assert config.logging.level.value in {"DEBUG", "INFO", "WARNING", "ERROR"}

    def test_to_dict_omits_none(self) -> None:
        data = default_configuration().to_dict()
        assert "organization" not in data["provider"]

    def test_toml_round_trip(self) -> None:
        config = default_configuration()
        config.provider.model = "gpt-4o"
        config.provider.base_url = "https://example.test/v1"
        config.last_workspace = r"C:\dev\project"
        parsed = tomllib.loads(config.to_toml())
        assert parsed["provider"]["model"] == "gpt-4o"
        assert parsed["provider"]["base_url"] == "https://example.test/v1"
        assert parsed["last_workspace"] == r"C:\dev\project"
        restored = Configuration.from_dict(parsed)
        assert restored.provider.model == config.provider.model
        assert restored.provider.timeout_seconds == config.provider.timeout_seconds
        assert restored.agent.max_file_bytes == config.agent.max_file_bytes

    def test_toml_never_contains_a_credential(self) -> None:
        text = default_configuration().to_toml()
        assert "sk-" not in text
        assert "Bearer" not in text
        # Only the *name* of the environment variable may appear.
        assert text.count("api_key") <= 2

    def test_from_dict_rejects_bad_values(self) -> None:
        with pytest.raises(ConfigurationError):
            Configuration.from_dict({"provider": {"timeout_seconds": -1}})

    def test_agent_settings_bounds(self) -> None:
        with pytest.raises(ValidationError):
            AgentSettings(max_iterations=0)
        with pytest.raises(ValidationError):
            AgentSettings(history_turns=-1)
        with pytest.raises(ValidationError):
            AgentSettings(max_file_bytes=0)


class TestTomlWriter:
    def test_nested_tables_and_scalars(self) -> None:
        text = dumps({"a": 1, "t": {"b": "x", "c": True, "d": 1.5}, "e": None})
        assert tomllib.loads(text) == {"a": 1, "t": {"b": "x", "c": True, "d": 1.5}}

    def test_lists_and_quotes(self) -> None:
        text = dumps({"x": ["a", "b"], "y": 'say "hi"'})
        assert tomllib.loads(text) == {"x": ["a", "b"], "y": 'say "hi"'}

    def test_keys_with_spaces_are_quoted(self) -> None:
        assert tomllib.loads(dumps({"my key": 1})) == {"my key": 1}

    def test_unsupported_type_raises(self) -> None:
        with pytest.raises(TypeError):
            dumps({"x": object()})

    def test_empty_document(self) -> None:
        assert dumps({}) == ""


class TestConfigManager:
    def test_load_missing_file_returns_defaults(self, config_manager: ConfigManager) -> None:
        assert config_manager.load() == default_configuration()

    def test_save_and_load(self, config_manager: ConfigManager) -> None:
        config = config_manager.config
        config.provider.model = "my-model"
        path = config_manager.save(config)
        assert path.exists()
        fresh = ConfigManager(config_manager.paths, config_manager.secret_store)
        assert fresh.load().provider.model == "my-model"

    def test_load_rejects_corrupt_file(self, config_manager: ConfigManager) -> None:
        config_manager.config_file.parent.mkdir(parents=True, exist_ok=True)
        config_manager.config_file.write_text("this is = not = toml", encoding="utf-8")
        with pytest.raises(ConfigurationError) as info:
            config_manager.load()
        assert "unreadable" in info.value.user_message

    def test_reset_restores_defaults(self, config_manager: ConfigManager) -> None:
        config_manager.config.provider.model = "changed"
        assert config_manager.reset().provider.model == default_configuration().provider.model

    def test_api_key_prefers_environment(
        self, config_manager: ConfigManager, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("QM_API_KEY", "env-key-value")
        secret = config_manager.resolve_api_key()
        assert secret is not None
        assert secret.get_secret_value() == "env-key-value"

    def test_api_key_falls_back_to_store(self, config_manager: ConfigManager) -> None:
        config_manager.set_api_key("stored-key-value")
        secret = config_manager.resolve_api_key()
        assert secret is not None
        assert secret.get_secret_value() == "stored-key-value"

    def test_masked_key_never_reveals_the_value(self, config_manager: ConfigManager) -> None:
        config_manager.set_api_key("super-secret-key-123456")
        masked = config_manager.masked_api_key()
        assert "super-secret" not in masked
        assert masked.startswith("sup")
        assert masked.endswith("56")

    def test_clear_api_key(self, config_manager: ConfigManager) -> None:
        config_manager.set_api_key("value-to-remove")
        assert config_manager.clear_api_key() is True
        assert config_manager.clear_api_key() is False
        assert config_manager.has_api_key() is False

    def test_empty_api_key_rejected(self, config_manager: ConfigManager) -> None:
        with pytest.raises(ConfigurationError):
            config_manager.set_api_key("   ")

    def test_secret_key_is_namespaced_by_provider(self, config_manager: ConfigManager) -> None:
        assert config_manager.api_key_secret_key().startswith("provider:")

    def test_describe_includes_model(self, config_manager: ConfigManager) -> None:
        assert "QuantsMind-Agent" in config_manager.describe()


class TestPresets:
    def test_all_presets_are_valid(self) -> None:
        for name, preset in PROVIDER_PRESETS.items():
            assert preset["base_url"].startswith(("http://", "https://")), name
            assert preset["model"].strip(), name
