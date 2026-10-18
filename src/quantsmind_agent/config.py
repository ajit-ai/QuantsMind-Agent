"""Application configuration (non-secret settings).

Settings are stored as TOML in the per-user configuration directory, e.g. on
Windows 11::

    %LOCALAPPDATA%\\QuantsMind-Agent\\QuantsMind-Agent\\config.toml

.. code-block:: toml

    [provider]
    name = "openai_compatible"
    base_url = "https://api.openai.com/v1"
    model = "gpt-4o-mini"
    timeout_seconds = 120.0
    max_output_tokens = 4096
    temperature = 0.2
    api_key_env = "QM_API_KEY"

    [agent]
    max_iterations = 8
    max_file_bytes = 256000
    auto_approve_reads = true
    auto_approve_writes = false
    command_timeout_seconds = 120.0
    git_enabled = true

    [logging]
    level = "INFO"

An API key is **never** stored here. It lives in the secret store (Windows
Credential Manager or ``secrets.json``) or in the environment variable named by
:attr:`ProviderSettings.api_key_env`.
"""

from __future__ import annotations

import os
import tomllib
from enum import StrEnum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError, field_validator

from quantsmind_agent.errors import ConfigurationError
from quantsmind_agent.paths import AppPaths, default_paths
from quantsmind_agent.secrets import SecretStore, build_secret_store, mask_secret
from quantsmind_agent.toml_writer import dumps as toml_dumps
from quantsmind_agent.version import APP_NAME, __version__

__all__ = [
    "PROVIDER_PRESETS",
    "AgentSettings",
    "ConfigManager",
    "Configuration",
    "LogLevel",
    "LoggingSettings",
    "ProviderSettings",
    "default_configuration",
]

_LOCAL_HOST_MARKERS = ("localhost", "127.0.0.1", "0.0.0.0", "host.docker.internal", "[::1]")


class LogLevel(StrEnum):
    """Log verbosity levels exposed in Settings."""

    DEBUG = "DEBUG"
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"


class ProviderSettings(BaseModel):
    """Connection settings for the language-model gateway."""

    model_config = ConfigDict(validate_assignment=True)

    name: str = "openai_compatible"
    base_url: str = "https://api.openai.com/v1"
    model: str = "gpt-4o-mini"
    timeout_seconds: float = Field(default=120.0, gt=0, le=3600)
    max_output_tokens: int = Field(default=4096, gt=0)
    temperature: float = Field(default=0.2, ge=0.0, le=2.0)
    api_key_env: str = "QM_API_KEY"
    organization: str | None = None
    #: Capability overrides for this endpoint, as declared by the chosen preset.
    #: ``None`` means "no opinion", not "unsupported".
    capability_overrides: dict[str, Any] | None = None

    def capabilities(self) -> dict[str, Any] | None:
        """The declared capability overrides for this endpoint, if any.

        Kept as a plain mapping in the settings so a configuration file can carry
        it, and turned into a typed record at the provider boundary.
        """

        if self.capability_overrides is not None:
            return dict(self.capability_overrides)
        preset = PROVIDER_PRESETS.get(self.name)
        if preset:
            declared = preset.get("capabilities")
            if isinstance(declared, dict):
                return dict(declared)
        return None

    @field_validator("base_url")
    @classmethod
    def _check_base_url(cls, value: str) -> str:
        cleaned = value.strip().rstrip("/")
        if not cleaned:
            raise ValueError("base_url must not be empty")
        if not cleaned.startswith(("http://", "https://")):
            raise ValueError("base_url must start with http:// or https://")
        return cleaned

    @field_validator("model", "name")
    @classmethod
    def _not_empty(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("value must not be empty")
        return stripped

    def requires_api_key(self) -> bool:
        """Return ``True`` when this endpoint needs a credential.

        Local OpenAI-compatible servers (Ollama, LM Studio, llama.cpp, vLLM ...)
        normally run unauthenticated, so the key stays optional for them.
        """

        host = self.base_url.lower()
        return not any(marker in host for marker in _LOCAL_HOST_MARKERS)


class AgentSettings(BaseModel):
    """Behavioural limits for the Agent Core.

    The v0.2 fields control how much autonomy the agent has once a workspace is
    open. Their defaults are the conservative ones: writes and commands are
    approved one at a time, and ``auto_approve_writes`` is false.
    """

    model_config = ConfigDict(validate_assignment=True)

    max_iterations: int = Field(default=8, ge=1, le=50)
    max_context_files: int = Field(default=8, ge=0, le=50)
    max_file_bytes: int = Field(default=256_000, gt=0)
    auto_approve_reads: bool = True
    system_prompt: str = ""
    history_turns: int = Field(default=12, ge=0, le=200)

    auto_approve_writes: bool = False
    """Approve ``write_file``/``edit_file`` without prompting.

    Deleting a file always prompts regardless of this setting, and command
    execution always prompts: both rules are registered per tool so this flag
    cannot widen them.
    """

    command_timeout_seconds: float = Field(default=120.0, gt=0, le=600)
    """Default time limit for ``run_command`` when the model does not ask for one."""

    max_command_output_chars: int = Field(default=20_000, gt=0, le=200_000)
    """Per-stream cap on captured command output."""

    persist_conversations: bool = True
    """Store conversations on disk so they survive a restart.

    Off means the transcript lives only in memory, which is the v0.2 behaviour.
    Storage holds messages and endpoints; credentials never enter a record.
    """

    resume_last_conversation: bool = True
    """Reopen the most recent conversation when the application starts.

    Reopening restores the transcript only. Approval grants and undo history are
    session-scoped and always start empty, whatever this is set to.
    """

    max_stored_conversations: int = Field(default=50, ge=1, le=500)
    """How many conversations to keep, oldest pruned first."""

    max_stored_messages: int = Field(default=400, ge=2, le=10_000)
    """Messages kept per conversation.

    Past this, the oldest messages go and the newest stay, with one exception: the
    opening request always survives, because it is what the conversation is named
    after and what tells a reopened transcript what it was for.
    """

    max_stored_tool_results: int = Field(default=400, ge=0, le=10_000)
    """Tool results kept per conversation, oldest pruned first. ``0`` keeps none."""

    max_stored_item_chars: int = Field(default=8_000, ge=200, le=100_000)
    """Length cap on any single stored message or tool value.

    Long tool output is the thing that makes a record enormous, so it is shortened
    in place with a note saying how much was left out. The model still sees the
    whole value in this session; only what is kept on disk is shortened.
    """

    git_enabled: bool = True


class LoggingSettings(BaseModel):
    """Local logging configuration."""

    model_config = ConfigDict(validate_assignment=True)

    level: LogLevel = LogLevel.INFO
    file_enabled: bool = True
    max_bytes: int = Field(default=2_000_000, gt=0)
    backup_count: int = Field(default=3, ge=0)


class Configuration(BaseModel):
    """The complete, persisted application configuration (secret-free)."""

    model_config = ConfigDict(validate_assignment=True)

    schema_version: int = 1
    provider: ProviderSettings = Field(default_factory=ProviderSettings)
    agent: AgentSettings = Field(default_factory=AgentSettings)
    logging: LoggingSettings = Field(default_factory=LoggingSettings)
    last_workspace: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Return a plain, secret-free mapping suitable for TOML.

        ``None`` values are omitted rather than written as empty strings, so a
        round-trip through disk restores the exact same configuration.
        """

        sections = {
            "provider": self.provider.model_dump(mode="json"),
            "agent": self.agent.model_dump(mode="json"),
            "logging": self.logging.model_dump(mode="json"),
        }
        for name, values in sections.items():
            sections[name] = {key: value for key, value in values.items() if value is not None}
        return {
            "schema_version": self.schema_version,
            **sections,
            "last_workspace": self.last_workspace,
        }

    def to_toml(self) -> str:
        """Serialise to TOML. Secrets are never included."""

        return toml_dumps(self.to_dict())

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Configuration:
        """Validate a mapping into a :class:`Configuration`."""

        try:
            return cls.model_validate(data)
        except ValidationError as exc:
            raise ConfigurationError(
                f"Invalid configuration: {exc}",
                user_message=(
                    "The saved configuration could not be read. "
                    "Reset the settings to defaults and re-enter your provider details."
                ),
            ) from exc


#: Ready-made endpoints for common OpenAI-compatible services.
#:
#: Each preset is one protocol implementation pointed at a different endpoint,
#: not a different provider class. The ``capabilities`` record what that service
#: is actually known to do, because "OpenAI-compatible" describes the shape of a
#: request, not the features behind it: a local Ollama endpoint accepts the same
#: JSON and honours far less of it.
PROVIDER_PRESETS: dict[str, dict[str, Any]] = {
    "OpenAI": {
        "base_url": "https://api.openai.com/v1",
        "model": "gpt-4o-mini",
        "capabilities": {"tools": True, "streaming": True, "structured_output": True},
    },
    "OpenRouter": {
        "base_url": "https://openrouter.ai/api/v1",
        "model": "openai/gpt-4o-mini",
        "capabilities": {"tools": True, "streaming": True, "structured_output": None},
    },
    "Groq": {
        "base_url": "https://api.groq.com/openai/v1",
        "model": "llama-3.3-70b-versatile",
        "capabilities": {"tools": True, "streaming": True, "structured_output": None},
    },
    "Local (Ollama)": {
        "base_url": "http://localhost:11434/v1",
        "model": "qwen2.5-coder",
        # Ollama accepts the OpenAI request shape, but tool-calling support
        # depends entirely on the model that is loaded, so it is declared
        # unknown rather than yes. The application then asks for plain text and
        # parses it, which works on every model instead of quietly failing.
        "capabilities": {"tools": None, "streaming": True, "structured_output": None},
    },
    "Local (LM Studio)": {
        "base_url": "http://localhost:1234/v1",
        "model": "local-model",
        "capabilities": {"tools": None, "streaming": True, "structured_output": None},
    },
    "Local (llama.cpp)": {
        "base_url": "http://localhost:8080/v1",
        "model": "local-model",
        "capabilities": {"tools": False, "streaming": True, "structured_output": False},
    },
    "Custom endpoint": {
        "base_url": "https://api.example.com/v1",
        "model": "my-model",
        "capabilities": {"tools": None, "streaming": None, "structured_output": None},
    },
}


def default_configuration() -> Configuration:
    """A configuration populated with v0.1 defaults."""

    return Configuration()


class ConfigManager:
    """Loads/saves :class:`Configuration` and resolves the API key.

    Credential resolution order:

    1. the environment variable named by :attr:`ProviderSettings.api_key_env`,
    2. the secret store (Windows Credential Manager or ``secrets.json``).
    """

    def __init__(
        self,
        paths: AppPaths | None = None,
        secret_store: SecretStore | None = None,
    ) -> None:
        self.paths = paths or default_paths()
        self.secret_store = secret_store or build_secret_store(self.paths.secrets_file)
        self._config: Configuration = default_configuration()

    @property
    def config(self) -> Configuration:
        return self._config

    @property
    def config_file(self) -> Path:
        return self.paths.config_file

    def load(self) -> Configuration:
        """Read the config file if present, otherwise return defaults."""

        path = self.config_file
        if not path.exists():
            self._config = default_configuration()
            return self._config
        try:
            raw = tomllib.loads(path.read_text(encoding="utf-8"))
        except (OSError, tomllib.TOMLDecodeError) as exc:
            raise ConfigurationError(
                f"Cannot read configuration file: {exc}",
                user_message=(
                    "The configuration file is unreadable or corrupted. "
                    "Rename it to config.toml.bak and restart to regenerate defaults."
                ),
            ) from exc
        self._config = Configuration.from_dict(raw)
        return self._config

    def save(self, config: Configuration | None = None) -> Path:
        """Persist ``config`` (or the current one) to disk."""

        if config is not None:
            self._config = config
        path = self.config_file
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self._config.to_toml(), encoding="utf-8")
        return path

    def reset(self) -> Configuration:
        """Restore defaults and persist them."""

        self._config = default_configuration()
        self.save()
        return self._config

    # -- credentials -----------------------------------------------------
    def api_key_secret_key(self) -> str:
        """Secret-store key for the active provider's credential."""

        return f"provider:{self._config.provider.name}:api_key"

    def api_key_value(self) -> str | None:
        """The credential as a plain string, for the provider's request time.

        Resolution order: the environment variable named by
        :attr:`ProviderSettings.api_key_env`, then the secret store. Returning
        ``None`` is how the provider learns that no credential is configured.
        """

        env_value = os.environ.get(self._config.provider.api_key_env)
        if env_value:
            return env_value
        secret = self.secret_store.get(self.api_key_secret_key())
        return secret.get_secret_value() if secret else None

    def resolve_api_key(self) -> SecretStr | None:
        """The credential as a :class:`~pydantic.SecretStr`, which masks itself."""

        value = self.api_key_value()
        return SecretStr(value) if value else None

    def set_api_key(self, value: str) -> None:
        """Persist an API key to the secret store."""

        stripped = value.strip()
        if not stripped:
            raise ConfigurationError(
                "API key must not be empty",
                user_message="Enter a non-empty API key.",
            )
        self.secret_store.set(self.api_key_secret_key(), SecretStr(stripped))

    def clear_api_key(self) -> bool:
        """Remove the stored API key. Returns ``True`` when one was removed."""

        return self.secret_store.delete(self.api_key_secret_key())

    def has_api_key(self) -> bool:
        secret = self.resolve_api_key()
        return bool(secret and secret.get_secret_value())

    def masked_api_key(self) -> str:
        """UI-safe description of the configured credential (never the value)."""

        return mask_secret(self.api_key_value())

    def describe(self) -> str:
        """One-line human summary for the status bar."""

        provider = self._config.provider
        return f"{APP_NAME} {__version__} | {provider.model} @ {provider.base_url}"
