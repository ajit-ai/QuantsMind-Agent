"""Unit tests for the error taxonomy, redaction and logging setup."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from quantsmind_agent.diagnostics.logging import (
    REDACTED,
    get_logger,
    redact,
    safe_headers,
    safe_mapping,
    setup_logging,
    shutdown_logging,
)
from quantsmind_agent.errors import (
    InvalidPathError,
    InvalidWorkspaceError,
    MissingAPIKeyError,
    PermissionDeniedError,
    QuantsMindError,
    ToolNotFoundError,
)

SECRET = "sk-fake-example-key-not-a-real-credential"


class TestErrors:
    def test_error_carries_code_and_user_message(self) -> None:
        error = QuantsMindError("internal detail", user_message="Friendly text.")
        assert error.code == "QM_ERROR"
        assert str(error) == "internal detail"
        assert error.user_message == "Friendly text."

    def test_user_message_defaults_to_the_message(self) -> None:
        assert QuantsMindError("same").user_message == "same"

    def test_details_and_cause_are_preserved(self) -> None:
        cause = ValueError("root")
        error = QuantsMindError("wrap", details={"a": 1}, cause=cause)
        assert error.details == {"a": 1}
        assert error.__cause__ is cause

    def test_missing_api_key_names_the_provider(self) -> None:
        error = MissingAPIKeyError("openai_compatible")
        assert error.code == "QM_CONFIG_MISSING_API_KEY"
        assert "openai_compatible" in error.user_message
        assert "Settings" in error.user_message

    def test_path_error_is_actionable(self) -> None:
        error = InvalidPathError("x", user_message="outside", details={"path": "x"})
        assert error.code == "QM_WORKSPACE_PATH_DENIED"
        assert "outside" in error.user_message

    def test_workspace_error_codes(self) -> None:
        assert InvalidWorkspaceError("x").code == "QM_WORKSPACE_INVALID"

    def test_tool_not_found_lists_options(self) -> None:
        error = ToolNotFoundError("x", user_message="unknown", details={"available": ["a"]})
        assert error.details["available"] == ["a"]

    def test_permission_denied_message(self) -> None:
        error = PermissionDeniedError("GIT", "not allowed in v0.1")
        assert error.code == "QM_PERMISSION_DENIED"
        assert "GIT" in error.user_message
        assert "not allowed in v0.1" in error.user_message

    def test_every_error_has_a_stable_code(self) -> None:
        errors = [
            QuantsMindError("x"),
            MissingAPIKeyError("p"),
            InvalidPathError("x"),
            InvalidWorkspaceError("x"),
            PermissionDeniedError("GIT"),
            ToolNotFoundError("x"),
        ]
        assert len({error.code for error in errors}) == len(errors)


class TestRedaction:
    @pytest.mark.parametrize(
        "text",
        [
            f"Authorization: Bearer {SECRET}",
            f"api_key={SECRET}",
            f'api_key="{SECRET}"',
        ],
    )
    def test_credentials_are_scrubbed(self, text: str) -> None:
        cleaned = redact(text)
        assert SECRET not in cleaned
        assert REDACTED in cleaned

    def test_ordinary_text_is_untouched(self) -> None:
        assert redact("read src/app/main.py") == "read src/app/main.py"

    @pytest.mark.parametrize(
        "header",
        ["authorization", "x-api-key", "cookie", "proxy-authorization"],
    )
    def test_sensitive_headers_are_masked(self, header: str) -> None:
        headers = safe_headers({header: "very-secret-value", "X-Trace": "abc"})
        assert headers[header] == REDACTED
        assert headers["X-Trace"] == "abc"

    def test_safe_mapping_masks_by_key_name(self) -> None:
        mapping = safe_mapping({"api_key": "secret", "path": "src/app.py"})
        assert mapping["api_key"] == REDACTED
        assert mapping["path"] == "src/app.py"

    def test_get_logger_namespaces(self) -> None:
        assert get_logger(__name__).name.startswith("quantsmind_agent")
        assert get_logger("custom").name == "quantsmind_agent.custom"


class TestLoggingSetup:
    def test_writes_a_file_and_redacts_secrets(self, tmp_path: Path) -> None:
        log_file = tmp_path / "qm.log"
        try:
            setup_logging(level="DEBUG", log_file=log_file, console=False)
            get_logger("test").warning("calling %s", "provider")
            get_logger("test").warning(f"Authorization: Bearer {SECRET}")
            shutdown_logging()
            written = log_file.read_text(encoding="utf-8")
        finally:
            shutdown_logging()
        assert "calling provider" in written
        assert SECRET not in written
        assert REDACTED in written

    def test_debug_mode_lowers_the_level(self, tmp_path: Path) -> None:
        try:
            setup_logging(level="INFO", debug=True, log_file=tmp_path / "a.log", console=False)
            assert logging.getLogger("quantsmind_agent").level == logging.DEBUG
        finally:
            shutdown_logging()

    def test_handlers_are_not_duplicated(self, tmp_path: Path) -> None:
        try:
            setup_logging(log_file=tmp_path / "a.log", console=False)
            first = len(logging.getLogger("quantsmind_agent").handlers)
            setup_logging(log_file=tmp_path / "a.log", console=False)
            assert len(logging.getLogger("quantsmind_agent").handlers) == first
        finally:
            shutdown_logging()

    def test_can_run_without_a_file(self, tmp_path: Path) -> None:
        try:
            setup_logging(file_enabled=False, console=False)
            assert logging.getLogger("quantsmind_agent").handlers == []
        finally:
            shutdown_logging()

    def test_unwritable_destination_does_not_crash(self, tmp_path: Path) -> None:
        try:
            setup_logging(
                log_file=tmp_path / "qm.log" / "nested" / "impossible.log",
                console=False,
            )
            get_logger("test").info("still logging")
        finally:
            shutdown_logging()
