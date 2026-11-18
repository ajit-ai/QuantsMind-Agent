"""Domain-level error hierarchy for QuantsMind-Agent.

Every error raised by a subsystem carries:

* a stable machine code (``code``),
* a short operator-facing message (``message``), and
* a ``user_message`` which is safe to display in the GUI.

Stack traces are never part of ``str(error)``; they are logged in debug mode
only. See :mod:`quantsmind_agent.diagnostics.logging`.
"""

from __future__ import annotations

__all__ = [
    "AgentError",
    "CommandDeniedError",
    "CommandExecutionError",
    "CommandTimeoutError",
    "ConfigurationError",
    "ContextError",
    "EditConflictError",
    "GitError",
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
    "StaleContentError",
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


# --------------------------------------------------------------------------
# Mutating files and running commands (v0.2)
# --------------------------------------------------------------------------
class StaleContentError(ToolError):
    """An edit did not apply because the file no longer matches what was expected.

    This is the guard that stops the agent from silently overwriting work that
    changed underneath it. The message tells the model to re-read the file and
    retry rather than guessing.
    """

    code = "QM_TOOL_STALE_CONTENT"

    def __init__(
        self,
        path: str,
        reason: str,
        *,
        expected_count: int | None = None,
        details: dict[str, object] | None = None,
    ) -> None:
        merged: dict[str, object] = {
            "path": path,
            "reason": reason,
            "expected_count": expected_count,
            **(details or {}),
        }
        super().__init__(
            f"Stale edit for {path!r}: {reason}",
            user_message=(
                f"'{path}' no longer matches the content you expected ({reason}). "
                "Read the file again and redo the edit; nothing was changed."
            ),
            details=merged,
        )


class EditConflictError(ToolError):
    """An edit operation was requested with invalid or ambiguous parameters."""

    code = "QM_TOOL_EDIT_CONFLICT"

    def __init__(self, path: str, reason: str, *, details: dict[str, object] | None = None) -> None:
        super().__init__(
            f"Edit of {path!r} is not well-formed: {reason}",
            user_message=f"The edit to '{path}' could not be applied: {reason}.",
            details={"path": path, "reason": reason, **(details or {})},
        )


class CommandDeniedError(ToolError):
    """A command was refused by the application-level execution policy.

    Distinct from :class:`PermissionDeniedError`: the user never saw this
    command, because the policy forbids it outright. No approval dialog is shown.
    """

    code = "QM_COMMAND_DENIED"

    def __init__(
        self,
        command: str,
        reason: str,
        *,
        rule: str = "",
        details: dict[str, object] | None = None,
    ) -> None:
        merged: dict[str, object] = {
            "command": command,
            "reason": reason,
            "rule": rule,
            **(details or {}),
        }
        super().__init__(
            f"Command denied by policy: {command!r} ({reason})",
            user_message=(
                f"That command is blocked by QuantsMind-Agent's safety policy ({reason}) "
                "and was not run."
            ),
            details=merged,
        )


class CommandExecutionError(ToolError):
    """A permitted command could not be started or was killed."""

    code = "QM_COMMAND_EXECUTION_FAILED"

    def __init__(
        self, command: str, reason: str, *, details: dict[str, object] | None = None
    ) -> None:
        super().__init__(
            f"Command failed to execute: {command!r} ({reason})",
            user_message=f"The command could not be run: {reason}",
            details={"command": command, "reason": reason, **(details or {})},
        )


class CommandTimeoutError(CommandExecutionError):
    """A permitted command exceeded its time budget and was terminated."""

    code = "QM_COMMAND_TIMEOUT"

    def __init__(
        self, command: str, seconds: float, *, details: dict[str, object] | None = None
    ) -> None:
        super().__init__(
            command,
            f"it exceeded the {seconds:g}s time limit and was stopped",
            details={"timeout_seconds": seconds, **(details or {})},
        )
        self.code = "QM_COMMAND_TIMEOUT"
        self.user_message = (
            f"The command ran for longer than the {seconds:g}s limit and was stopped. "
            "Increase the limit in Settings or run a smaller step."
        )


class GitError(ToolError):
    """A read-only Git query failed (not a repository, git missing, bad revision)."""

    code = "QM_GIT_ERROR"


class ContextError(QuantsMindError):
    """The context engine could not assemble a usable prompt."""

    code = "QM_CONTEXT_ERROR"


# --------------------------------------------------------------------------
# Agent
# --------------------------------------------------------------------------
class AgentError(QuantsMindError):
    """Base class for Agent Core failures."""

    code = "QM_AGENT_ERROR"
