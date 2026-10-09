"""GUI tests for the v0.2 approval prompt and dialog.

The dialog data and the widget are covered here without Qt threading; the
end-to-end window behaviour is in ``test_approval_window.py``.
"""

from __future__ import annotations

from typing import Any

import pytest

from quantsmind_agent.gui.approval import (
    ApprovalBridge,
    ApprovalDialog,
    build_prompt,
    format_prompt,
)
from quantsmind_agent.permissions import ApprovalOutcome, Permission, PermissionRequest

pytestmark = pytest.mark.gui


def _request(
    permission: Permission,
    tool: str,
    *,
    summary: str = "do a thing",
    target: str = "some/file.py",
    **details: Any,
) -> PermissionRequest:
    return PermissionRequest(
        permission=permission, tool=tool, summary=summary, target=target, details=dict(details)
    )


class TestApprovalPrompt:
    """The data the dialog shows, independent of Qt."""

    def test_describes_a_write(self) -> None:
        prompt = build_prompt(
            _request(Permission.WRITE_WORKSPACE, "write_file", summary="write a.txt"),
            workspace=r"C:\proj",
        )
        text = format_prompt(prompt)
        assert "write_file" in text
        assert "some/file.py" in text
        assert r"C:\proj" in text
        assert prompt.operation

    def test_describes_a_command_with_its_risk(self) -> None:
        prompt = build_prompt(
            _request(
                Permission.EXECUTE_COMMAND,
                "run_command",
                summary="run format",
                target="format C:",
                command="format",
                arguments=["C:"],
            )
        )
        assert prompt.is_command
        assert prompt.risk == "blocked"
        assert prompt.risk_label == "Blocked by safety policy"

    def test_command_risk_agrees_with_the_policy(self) -> None:
        safe = build_prompt(
            _request(Permission.EXECUTE_COMMAND, "run_command", command="ruff", arguments=["check"])
        )
        assert safe.risk == "safe"
        risky = build_prompt(
            _request(
                Permission.EXECUTE_COMMAND, "run_command", command="bash", arguments=["-c", "ls"]
            )
        )
        assert risky.risk == "destructive"
        assert risky.blocking_warning

    def test_delete_carries_a_warning(self) -> None:
        prompt = build_prompt(
            _request(Permission.WRITE_WORKSPACE, "delete_file", summary="delete a.txt")
        )
        assert prompt.is_delete
        assert "cannot be undone" in prompt.blocking_warning

    def test_reason_is_surfaced(self) -> None:
        prompt = build_prompt(
            _request(
                Permission.EXECUTE_COMMAND, "run_command", command="pytest", reason="run the suite"
            )
        )
        assert prompt.reason == "run the suite"
        assert "run the suite" in format_prompt(prompt)

    def test_credential_shaped_values_are_withheld(self) -> None:
        prompt = build_prompt(
            _request(
                Permission.EXECUTE_COMMAND,
                "run_command",
                command="echo",
                note="sk-abcdefghijklmnopqrstuvwxyz0123",
            )
        )
        rendered = format_prompt(prompt)
        assert "sk-abcdefghijklmnop" not in rendered
        assert "(withheld)" in rendered

    def test_long_content_is_truncated_for_display(self) -> None:
        prompt = build_prompt(
            _request(Permission.WRITE_WORKSPACE, "write_file", content="y" * 5000)
        )
        rendered = "\n".join(prompt.argument_lines())
        assert "..." in rendered
        assert len(rendered) < 1000

    def test_argument_list_joins_sensibly(self) -> None:
        prompt = build_prompt(
            _request(Permission.EXECUTE_COMMAND, "run_command", arguments=["-m", "pytest"])
        )
        rendered = "\n".join(prompt.argument_lines())
        assert "arguments: -m pytest" in rendered


class TestApprovalDialog:
    def test_write_dialog_offers_allow_and_session(self, qapp: object) -> None:
        dialog = ApprovalDialog(
            build_prompt(_request(Permission.WRITE_WORKSPACE, "write_file", summary="write a.txt"))
        )
        labels = dialog.button_labels()
        assert "Allow" in labels
        assert "Allow for Session" in labels
        assert "Deny" in labels
        dialog.close()

    def test_command_dialog_says_allow_once(self, qapp: object) -> None:
        dialog = ApprovalDialog(
            build_prompt(_request(Permission.EXECUTE_COMMAND, "run_command", command="pytest"))
        )
        labels = dialog.button_labels()
        assert "Allow Once" in labels
        assert "Deny" in labels
        assert "Allow for Session" not in labels
        dialog.close()

    def test_delete_never_offers_a_session_grant(self, qapp: object) -> None:
        dialog = ApprovalDialog(
            build_prompt(
                _request(Permission.WRITE_WORKSPACE, "delete_file", summary="delete a.txt")
            )
        )
        labels = dialog.button_labels()
        assert "Allow" in labels
        assert "Allow for Session" not in labels
        dialog.close()

    @pytest.mark.parametrize(
        ("method", "expected"),
        [("_on_allow", ApprovalOutcome.ALLOW_ONCE), ("_on_deny", ApprovalOutcome.DENY)],
    )
    def test_buttons_set_the_outcome(
        self, qapp: object, method: str, expected: ApprovalOutcome
    ) -> None:
        dialog = ApprovalDialog(build_prompt(_request(Permission.WRITE_WORKSPACE, "write_file")))
        getattr(dialog, method)()
        assert dialog.outcome is expected
        dialog.close()

    def test_session_button_sets_the_session_outcome(self, qapp: object) -> None:
        dialog = ApprovalDialog(build_prompt(_request(Permission.WRITE_WORKSPACE, "write_file")))
        dialog._on_allow_session()
        assert dialog.outcome is ApprovalOutcome.ALLOW_FOR_SESSION
        dialog.close()

    def test_escape_denies(self, qapp: object) -> None:
        dialog = ApprovalDialog(build_prompt(_request(Permission.WRITE_WORKSPACE, "write_file")))
        dialog.reject()
        assert dialog.outcome is ApprovalOutcome.DENY

    def test_denial_is_the_default_outcome(self, qapp: object) -> None:
        dialog = ApprovalDialog(build_prompt(_request(Permission.WRITE_WORKSPACE, "write_file")))
        assert dialog.outcome is ApprovalOutcome.DENY
        dialog.close()

    def test_shows_the_requested_details(self, qapp: object) -> None:
        dialog = ApprovalDialog(
            build_prompt(
                _request(
                    Permission.EXECUTE_COMMAND,
                    "run_command",
                    summary="run pytest",
                    target="python -m pytest -q",
                    command="python",
                    arguments=["-m", "pytest", "-q"],
                    reason="verify the fix",
                ),
                workspace=r"C:\proj",
            )
        )
        text = dialog.full_text()
        assert "run_command" in text
        assert "python" in text
        assert "verify the fix" in text
        assert r"C:\proj" in text
        dialog.close()


class TestApprovalBridge:
    def test_bridge_starts_idle(self, qapp: object) -> None:
        assert not ApprovalBridge().has_pending

    def test_cancel_clears_a_pending_request(self, qapp: object) -> None:
        bridge = ApprovalBridge()
        bridge.cancel_pending()
        assert not bridge.has_pending
