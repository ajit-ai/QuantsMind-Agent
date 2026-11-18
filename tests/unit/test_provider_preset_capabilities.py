"""A preset's capability record must reach the provider it describes.

``PROVIDER_PRESETS`` records what each service is *known* to support, and
some of those records are deliberately conservative: Ollama and LM Studio
declare their tool support as **unknown**, llama.cpp declares it as
**unsupported**, and OpenRouter declares structured output as unknown. The
capability system exists so that the application "never silently assumes the
optimistic answer".

Those records were unreachable. ``ProviderSettings.capabilities()`` looked the
preset up by ``provider.name``, but ``provider.name`` has to stay
``openai_compatible`` for the provider factory to resolve it, and no preset is
named that. The settings dialog copies a preset's base URL and model and
leaves the name alone, so every configuration -- including one the operator
just picked from the preset list -- fell back to the protocol's optimistic
defaults: tool schemas were sent to local servers that may not support them,
and a native structured-output request was sent to a service whose support
depends on the model.

These tests pin the records to the endpoint that identifies the service, and
pin that an explicit override still wins.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
if str(REPO / "scripts") not in sys.path:
    sys.path.insert(0, str(REPO / "scripts"))

from quantsmind_agent.app.settings import PROVIDER_PRESETS, ProviderSettings  # noqa: E402
from quantsmind_agent.providers import OPENAI_COMPAT, build_provider  # noqa: E402
from quantsmind_agent.providers.gateway import ProviderCapabilities  # noqa: E402


def _as_the_gui_configures_it(preset_key: str) -> ProviderSettings:
    """The settings a preset choice produces: base URL and model, name unchanged."""

    preset = PROVIDER_PRESETS[preset_key]
    return ProviderSettings(
        name=OPENAI_COMPAT,
        base_url=preset["base_url"],
        model=preset["model"],
    )


def _provider_for(settings: ProviderSettings) -> ProviderCapabilities:
    provider = build_provider(settings, api_key=lambda: "sk-not-a-real-key")
    capabilities = getattr(provider, "capabilities", None)
    assert isinstance(capabilities, ProviderCapabilities)
    return capabilities


class TestPresetRecordsReachTheProvider:
    @pytest.mark.parametrize("preset_key", sorted(PROVIDER_PRESETS))
    def test_the_preset_record_is_applied(self, preset_key: str) -> None:
        settings = _as_the_gui_configures_it(preset_key)
        declared = PROVIDER_PRESETS[preset_key]["capabilities"]
        assert settings.capabilities() == declared

    @pytest.mark.parametrize("preset_key", sorted(PROVIDER_PRESETS))
    def test_the_provider_is_told_what_the_preset_declares(self, preset_key: str) -> None:
        settings = _as_the_gui_configures_it(preset_key)
        declared = PROVIDER_PRESETS[preset_key]["capabilities"]
        capabilities = _provider_for(settings)
        for field in ("tools", "streaming", "structured_output"):
            assert getattr(capabilities, field) == declared[field], field

    def test_a_local_endpoint_with_unknown_tools_is_not_given_tool_schemas(self) -> None:
        """Ollama's record says unknown, so the agent must not send tools."""

        settings = _as_the_gui_configures_it("Local (Ollama)")
        capabilities = _provider_for(settings)
        assert capabilities.tools is None
        assert capabilities.supports("tools") is False

    def test_llama_cpp_is_told_it_cannot_call_tools(self) -> None:
        settings = _as_the_gui_configures_it("Local (llama.cpp)")
        capabilities = _provider_for(settings)
        assert capabilities.tools is False
        assert capabilities.supports("tools") is False

    def test_openrouter_structured_output_stays_unknown(self) -> None:
        """The record is unknown on purpose, so the application parses JSON itself."""

        settings = _as_the_gui_configures_it("OpenRouter")
        capabilities = _provider_for(settings)
        assert capabilities.structured_output is None
        assert capabilities.supports("structured_output") is False

    def test_the_default_openai_endpoint_is_unchanged(self) -> None:
        """The default configuration already behaved this way."""

        settings = ProviderSettings()
        capabilities = _provider_for(settings)
        assert capabilities.tools is True
        assert capabilities.streaming is True
        assert capabilities.structured_output is True


class TestPrecedence:
    def test_an_explicit_override_wins_over_the_preset(self) -> None:
        settings = ProviderSettings(
            name=OPENAI_COMPAT,
            base_url=PROVIDER_PRESETS["Local (Ollama)"]["base_url"],
            model="qwen2.5-coder",
            capability_overrides={"tools": True, "streaming": True, "structured_output": False},
        )
        assert settings.capabilities() == {
            "tools": True,
            "streaming": True,
            "structured_output": False,
        }

    def test_an_unknown_endpoint_keeps_the_protocol_defaults(self) -> None:
        settings = ProviderSettings(
            name=OPENAI_COMPAT,
            base_url="https://llm.internal.example/v1",
            model="internal-model",
        )
        assert settings.capabilities() is None
        capabilities = _provider_for(settings)
        assert capabilities.tools is True
        assert capabilities.supports("tools") is True

    def test_the_provider_name_is_unchanged_by_a_preset(self) -> None:
        """The name selects the factory, so a preset must not change it."""

        settings = _as_the_gui_configures_it("OpenRouter")
        assert settings.name == OPENAI_COMPAT
        build_provider(settings, api_key=lambda: "sk-not-a-real-key")
