"""End-to-end approval behaviour at the application level.

These drive a real :class:`~quantsmind_agent.app.ApplicationContext` -- real
agent loop, real permission broker, real tools -- with the approval supplied by
a scripted approver instead of the GUI dialog. They exist because completing a
turn while pumping Qt events from a test thread is not reliable; the GUI half of
the same path (delivery to the GUI thread, and every dialog choice) is covered
in ``tests/gui/test_approval_window.py``.

What matters here is that an approval is the only thing standing between a
model's tool call and the filesystem, in both directions: approved applies, and
denied, cancelled or unanswerable never does.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from quantsmind_agent.app import ApplicationContext
from quantsmind_agent.permissions import ApprovalOutcome
from tests.fakes import FakeModelProvider, text_response, tool_response

pytestmark = pytest.mark.integration

EDIT = {
    "path": "README.md",
    "operation": "replace",
    "old_text": "# Sample Project",
    "new_text": "# Accepted Project",
}


def _context(
    app_paths: Any, sample_project: Path, provider: FakeModelProvider
) -> ApplicationContext:
    from quantsmind_agent.config import ConfigManager

    manager = ConfigManager(app_paths, secret_store=_file_store(app_paths))
    manager.config.provider.base_url = "http://localhost:11434/v1"
    manager.config.provider.api_key_env = "QM_UNUSED_KEY"
    context = ApplicationContext.create(paths=app_paths, config_manager=manager)
    context.open_workspace(sample_project)
    context.agent._gateway.set_provider(provider)
    return context


def _file_store(app_paths: Any) -> Any:
    from quantsmind_agent.secrets import FileSecretStore

    return FileSecretStore(app_paths.secrets_file)


def _approver(outcome: ApprovalOutcome, asked: list[str]) -> Any:
    """An approver that records which tool was put to the user."""

    def approve(request: Any) -> ApprovalOutcome:
        asked.append(request.tool)
        return outcome

    return approve


def _run(context: ApplicationContext, outcome: ApprovalOutcome) -> tuple[Any, list[str]]:
    asked: list[str] = []
    context.tools.permissions.set_approver(_approver(outcome, asked))
    result = context.agent.run("Update the README heading.")
    return result, asked


def _provider() -> FakeModelProvider:
    return FakeModelProvider([tool_response([("edit_file", EDIT)]), text_response("Done.")])


class TestApprovedEdit:
    def test_applies_only_the_authorized_change(self, app_paths: Any, sample_project: Path) -> None:
        context = _context(app_paths, sample_project, _provider())
        target = sample_project / "README.md"

        result, asked = _run(context, ApprovalOutcome.ALLOW_ONCE)

        assert asked == ["edit_file"], "the edit was not put to the user"
        assert result.error is None
        content = target.read_text(encoding="utf-8")
        assert "# Accepted Project" in content
        # The rest of the file is untouched.
        assert "A tiny project used by the QuantsMind-Agent test suite." in content
        assert "# Sample Project" not in content

    def test_allow_for_session_still_applies_this_call(
        self, app_paths: Any, sample_project: Path
    ) -> None:
        context = _context(app_paths, sample_project, _provider())
        result, asked = _run(context, ApprovalOutcome.ALLOW_FOR_SESSION)

        assert asked == ["edit_file"]
        assert result.error is None
        assert "# Accepted Project" in (sample_project / "README.md").read_text(encoding="utf-8")
        assert context.tools.permissions.session_grants


class TestDeniedEdit:
    def test_denied_edit_never_touches_the_file(self, app_paths: Any, sample_project: Path) -> None:
        context = _context(app_paths, sample_project, _provider())
        target = sample_project / "README.md"
        before = target.read_text(encoding="utf-8")

        result, asked = _run(context, ApprovalOutcome.DENY)

        assert asked == ["edit_file"], "the edit was not put to the user"
        assert result.error is None, "a denial must not abort the whole turn"
        assert target.read_text(encoding="utf-8") == before

    def test_the_model_is_told_and_can_continue(self, app_paths: Any, sample_project: Path) -> None:
        context = _context(app_paths, sample_project, _provider())
        _run(context, ApprovalOutcome.DENY)

        failures = [r for r in context.session.tool_results if not r.ok]
        assert failures, "the denial was not reported to the model as a failure"
        assert "not permitted" in (failures[0].error or "").lower()

    def test_no_approver_denies_everything(self, app_paths: Any, sample_project: Path) -> None:
        """With nothing able to answer, nothing is authorised."""

        context = _context(app_paths, sample_project, _provider())
        context.tools.permissions.set_approver(None)
        target = sample_project / "README.md"
        before = target.read_text(encoding="utf-8")

        result = context.agent.run("Update the README heading.")

        assert result.error is None
        assert target.read_text(encoding="utf-8") == before

    def test_a_cancelled_prompt_denies(self, app_paths: Any, sample_project: Path) -> None:
        """Cancelling while a decision is outstanding yields DENY, never ALLOW.

        This is the Stop button and the window-close path. The worker must be
        released and the answer must be a refusal.
        """

        import threading

        from quantsmind_agent.gui.approval import ApprovalBridge, build_prompt
        from quantsmind_agent.permissions import Permission, PermissionRequest

        bridge = ApprovalBridge()
        prompt = build_prompt(
            PermissionRequest(
                permission=Permission.WRITE_WORKSPACE,
                tool="edit_file",
                summary="edit README.md",
                target="README.md",
            )
        )
        outcome: list[Any] = []
        done = threading.Event()

        def worker() -> None:
            outcome.append(bridge.request(prompt))
            done.set()

        thread = threading.Thread(target=worker, daemon=True)
        thread.start()
        # Let the request register, then cancel before anything can answer.
        threading.Event().wait(0.2)
        bridge.cancel_pending()

        assert done.wait(timeout=5), "cancellation left the worker blocked forever"
        assert outcome == [ApprovalOutcome.DENY]
        assert not bridge.has_pending

    def test_shutdown_denies_a_pending_request(self, app_paths: Any) -> None:
        import threading

        from quantsmind_agent.gui.approval import ApprovalBridge, build_prompt
        from quantsmind_agent.permissions import Permission, PermissionRequest

        bridge = ApprovalBridge()
        prompt = build_prompt(
            PermissionRequest(
                permission=Permission.WRITE_WORKSPACE,
                tool="edit_file",
                summary="edit README.md",
                target="README.md",
            )
        )
        outcome: list[Any] = []
        done = threading.Event()

        def worker() -> None:
            outcome.append(bridge.request(prompt))
            done.set()

        thread = threading.Thread(target=worker, daemon=True)
        thread.start()
        threading.Event().wait(0.2)
        bridge.cancel_pending()

        assert done.wait(timeout=5)
        assert outcome == [ApprovalOutcome.DENY]


class TestBlockedOperationsNeverReachApproval:
    """A policy refusal happens before the user is asked."""

    def test_blocked_command_is_not_put_to_the_user(
        self, app_paths: Any, sample_project: Path
    ) -> None:
        context = _context(
            app_paths,
            sample_project,
            FakeModelProvider(
                [
                    tool_response([("run_command", {"command": "format", "arguments": ["C:"]})]),
                    text_response("I cannot do that."),
                ]
            ),
        )
        asked: list[str] = []
        context.tools.permissions.set_approver(_approver(ApprovalOutcome.ALLOW_ONCE, asked))

        result = context.agent.run("Format the C drive.")

        assert result.error is None
        assert asked == [], "a blocked command was offered to the user"
        assert any(not r.ok for r in context.session.tool_results)
