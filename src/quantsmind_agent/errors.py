"""Domain-level error hierarchy for QuantsMind-Agent.

Every error raised by a subsystem carries:

* a stable machine code (``code``),
* a short operator-facing message (``message``), and
* a ``user_message`` which is safe to display in the GUI.

Stack traces are never part of ``str(error)``; they are logged in debug mode
only. See :mod:`quantsmind_agent.logging_setup`.
"""

from __future__ import annotations

__all__ = [
    "AgentError",
    "ConfigurationError",
    "ContextError",
    "InvalidPathError",
    "InvalidWorkspaceError",
    "LLMConfigurationError",
    "LLMError",
    "LLMProtocolError",
    "LLMTimeoutError",
    "MissingAPIKeyError",
    "ModelUnavailableError",
    "NetworkError",
    "PermissionDeniedError",
    "QuantsMindError",
    "ToolError",
    "ToolFailedError",
    "ToolNotFoundError",
    "WorkspaceError",
]


class QuantsMindError(Exception):
    """Base class for every error raised by QuantsMind-Agent."""

    code: str = "QM_ERROR"

    def __init__(
        self,
        message: str,
        *,
        user_message: str | None = None,
        details: dict[str, object] | None = None,
        cause: BaseException | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.user_message = user_message or message
        self.details: dict[str, object] = dict(details or {})
        if cause is not None:
            self.__cause__ = cause

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.message


# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------
class ConfigurationError(QuantsMindError):
    """Local configuration is missing or invalid."""

    code = "QM_CONFIG_INVALID"


class MissingAPIKeyError(ConfigurationError):
    """No API key was configured for the selected provider."""

    code = "QM_CONFIG_MISSING_API_KEY"

    def __init__(self, provider: str) -> None:
        super().__init__(
            f"No API key configured for provider {provider!r}",
            user_message=(
                f"No API key is configured for provider '{provider}'. "
                "Open Settings and enter a key, or set the matching environment variable."
            ),
            details={"provider": provider},
        )


class LLMConfigurationError(ConfigurationError):
    """The model/provider configuration cannot be used for a request."""

    code = "QM_CONFIG_LLM_INVALID"


# --------------------------------------------------------------------------
# Workspace
# --------------------------------------------------------------------------
class WorkspaceError(QuantsMindError):
    """Base class for workspace failures."""

    code = "QM_WORKSPACE_ERROR"


class InvalidWorkspaceError(WorkspaceError):
    """The selected workspace path is not a usable directory."""

    code = "QM_WORKSPACE_INVALID"


class InvalidPathError(WorkspaceError):
    """A path escapes the workspace root or is otherwise not allowed."""

    code = "QM_WORKSPACE_PATH_DENIED"


# --------------------------------------------------------------------------
# Model gateway
# --------------------------------------------------------------------------
class LLMError(QuantsMindError):
    """Base class for failures while talking to a language model."""

    code = "QM_LLM_ERROR"


class NetworkError(LLMError):
    """The provider could not be reached."""

    code = "QM_LLM_NETWORK"


class LLMTimeoutError(LLMError):
    """The provider did not answer within the configured timeout."""

    code = "QM_LLM_TIMEOUT"


class ModelUnavailableError(LLMError):
    """The provider rejected the request (auth, unknown model, quota...)."""

    code = "QM_LLM_MODEL_UNAVAILABLE"


class LLMProtocolError(LLMError):
    """The provider returned a response the agent could not interpret."""

    code = "QM_LLM_PROTOCOL"


# --------------------------------------------------------------------------
# Tools / permissions / context
# --------------------------------------------------------------------------
class ToolError(QuantsMindError):
    """Base class for tool failures."""

    code = "QM_TOOL_ERROR"


class ToolNotFoundError(ToolError):
    """The agent requested a tool that is not registered."""

    code = "QM_TOOL_NOT_FOUND"


class ToolFailedError(ToolError):
    """A tool ran but could not complete its objective."""

    code = "QM_TOOL_FAILED"


class PermissionDeniedError(ToolError):
    """A permission check denied an operation."""

    code = "QM_PERMISSION_DENIED"

    def __init__(
        self,
        permission: str,
        reason: str = "denied by policy",
        *,
        details: dict[str, object] | None = None,
    ) -> None:
        merged: dict[str, object] = {"permission": permission, **(details or {})}
        super().__init__(
            f"Permission {permission!r} denied: {reason}",
            user_message=f"The operation was not permitted ({permission}): {reason}.",
            details=merged,
        )


class ContextError(QuantsMindError):
    """The context engine could not assemble a usable prompt."""

    code = "QM_CONTEXT_ERROR"


# --------------------------------------------------------------------------
# Agent
# --------------------------------------------------------------------------
class AgentError(QuantsMindError):
    """Base class for Agent Core failures."""

    code = "QM_AGENT_ERROR"
