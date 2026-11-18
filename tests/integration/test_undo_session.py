"""Session and application wiring for the undo list.

The journal has to outlive the tool objects that fill it: the application
rebuilds every tool whenever the workspace, the settings or the conversation
change. These tests pin that ownership, the reset behaviour, and the always-ASK
policy that makes undo a decision the user makes rather than one the agent makes.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from quantsmind_agent.app import ApplicationContext
from quantsmind_agent.app.settings import ConfigManager
from quantsmind_agent.security.permissions import (
    ApprovalOutcome,
    Decision,
    Permission,
    PermissionRequest,
)
from quantsmind_agent.security.secrets import FileSecretStore
from quantsmind_agent.services.conversations.session import Session
from quantsmind_agent.services.workspace.containment import Workspace

pytestmark = pytest.mark.integration

README = "README.md"


def _context(app_paths: Any, sample_project: Path) -> ApplicationContext:
    manager = ConfigManager(app_paths, secret_store=FileSecretStore(app_paths.secrets_file))
    context = ApplicationContext.create(paths=app_paths, config_manager=manager)
    context.open_workspace(sample_project, trusted=True)
    return context


def _approver(outcome: ApprovalOutcome, asked: list[str]) -> Any:
    def approve(request: PermissionRequest) -> ApprovalOutcome:
        asked.append(request.tool)
        return outcome

    return approve


class TestSessionOwnership:
    def test_a_session_starts_with_an_empty_journal(self) -> None:
        assert Session().revisions.size == 0

    def test_reset_clears_the_journal(self, workspace: Workspace) -> None:
        session = Session(workspace)
        session.revisions.record(_revision(README, previous_bytes=b"old\n", result_sha256="a" * 64))

        session.reset(keep_workspace=True)

        assert session.revisions.size == 0

    def test_reset_without_keeping_the_workspace_clears_it_too(self, workspace: Workspace) -> None:
        session = Session(workspace)
        session.revisions.record(_revision(README, previous_bytes=b"old\n", result_sha256="a" * 64))

        session.reset(keep_workspace=False)

        assert session.revisions.size == 0
        assert session.workspace is None

    def test_the_conversation_is_preserved_when_only_the_project_changes(
        self, tmp_path: Path
    ) -> None:
        first = _project(tmp_path / "first")
        second = _project(tmp_path / "second")
        session = Session(Workspace(first))
        session.add_message(_message("hello"))

        session.set_workspace(Workspace(second))

        assert session.workspace is not None
        assert session.workspace.root == second
        assert len(session.messages) == 1

    def test_switching_project_drops_revisions_from_the_old_one(self, tmp_path: Path) -> None:
        first = _project(tmp_path / "first")
        second = _project(tmp_path / "second")
        session = Session(Workspace(first))
        session.revisions.record(_revision(README, previous_bytes=b"old\n", result_sha256="a" * 64))

        session.set_workspace(Workspace(second))

        # The paths in the old journal were relative to a folder the user is no
        # longer looking at, so offering to restore them would be a surprise.
        assert session.revisions.size == 0

    def test_reopening_the_same_project_keeps_the_journal(self, tmp_path: Path) -> None:
        project = _project(tmp_path)
        session = Session(Workspace(project))
        session.revisions.record(_revision(README, previous_bytes=b"old\n", result_sha256="a" * 64))

        session.set_workspace(Workspace(project))

        assert session.revisions.size == 1


class TestApplicationWiring:
    def test_the_tool_is_registered_with_the_workspace(
        self, app_paths: Any, sample_project: Path
    ) -> None:
        context = _context(app_paths, sample_project)

        assert "undo_file" in context.tools.names

    def test_no_workspace_means_no_tools(self, app_paths: Any) -> None:
        manager = ConfigManager(app_paths, secret_store=FileSecretStore(app_paths.secrets_file))
        context = ApplicationContext.create(paths=app_paths, config_manager=manager)

        assert "undo_file" not in context.tools.names

    def test_the_journal_survives_a_tool_rebuild(
        self, app_paths: Any, sample_project: Path
    ) -> None:
        context = _context(app_paths, sample_project)
        asked: list[str] = []
        context.tools.permissions.set_approver(_approver(ApprovalOutcome.ALLOW_ONCE, asked))
        target = sample_project / README
        original = target.read_bytes()

        assert context.tools.execute("write_file", {"path": README, "content": "clobbered\n"}).ok
        assert context.session.revisions.size == 1

        # apply_configuration() rebuilds the gateway, the registry, the broker and
        # every tool; a write from before the rebuild must still be undoable.
        context.apply_configuration()

        assert context.session.revisions.size == 1
        context.tools.permissions.set_approver(_approver(ApprovalOutcome.ALLOW_ONCE, asked))
        assert context.tools.execute("undo_file", {"path": README}).ok
        assert target.read_bytes() == original

    def test_a_new_conversation_clears_the_undo_list(
        self, app_paths: Any, sample_project: Path
    ) -> None:
        context = _context(app_paths, sample_project)
        asked: list[str] = []
        context.tools.permissions.set_approver(_approver(ApprovalOutcome.ALLOW_ONCE, asked))
        context.tools.execute("write_file", {"path": README, "content": "clobbered\n"})
        assert context.session.revisions.size == 1

        context.reset_conversation()

        assert context.session.revisions.size == 0
        context.tools.permissions.set_approver(_approver(ApprovalOutcome.ALLOW_ONCE, asked))
        result = context.tools.execute("undo_file", {"path": README})
        assert not result.ok
        assert "nothing to undo" in (result.error or "")

    def test_writes_reach_the_application_journal(
        self, app_paths: Any, sample_project: Path
    ) -> None:
        context = _context(app_paths, sample_project)
        asked: list[str] = []
        context.tools.permissions.set_approver(_approver(ApprovalOutcome.ALLOW_ONCE, asked))

        assert context.tools.execute("write_file", {"path": README, "content": "changed\n"}).ok

        assert context.session.revisions.depth(README) == 1
        assert context.session.revisions.paths == [README]

    def test_switching_project_clears_the_application_journal(
        self, app_paths: Any, sample_project: Path, tmp_path: Path
    ) -> None:
        context = _context(app_paths, sample_project)
        asked: list[str] = []
        context.tools.permissions.set_approver(_approver(ApprovalOutcome.ALLOW_ONCE, asked))
        context.tools.execute("write_file", {"path": README, "content": "changed\n"})
        other = tmp_path / "other_project"
        other.mkdir()

        context.open_workspace(other, trusted=True)

        assert context.session.revisions.size == 0


class TestPolicyIsAlwaysAsk:
    def test_undo_is_asked_even_with_auto_approved_writes(
        self, app_paths: Any, sample_project: Path
    ) -> None:
        context = _context(app_paths, sample_project)
        context.config_manager.config.agent.auto_approve_writes = True
        context.apply_configuration()
        asked: list[str] = []

        def approve(request: PermissionRequest) -> ApprovalOutcome:
            asked.append(request.tool)
            return (
                ApprovalOutcome.ALLOW_ONCE if request.tool == "write_file" else ApprovalOutcome.DENY
            )

        context.tools.permissions.set_approver(approve)

        assert context.tools.execute("write_file", {"path": README, "content": "changed\n"}).ok
        assert not context.tools.execute("undo_file", {"path": README}).ok
        # The write itself never reached the user: auto-approve covers ordinary
        # writes only, and undo is not an ordinary write.
        assert asked == ["undo_file"]
        assert (sample_project / README).read_text(encoding="utf-8") == "changed\n"

    def test_a_session_grant_on_write_does_not_cover_undo(
        self, app_paths: Any, sample_project: Path
    ) -> None:
        context = _context(app_paths, sample_project)
        asked: list[str] = []

        def approve(request: PermissionRequest) -> ApprovalOutcome:
            asked.append(request.tool)
            return (
                ApprovalOutcome.ALLOW_FOR_SESSION
                if request.tool == "write_file"
                else ApprovalOutcome.DENY
            )

        context.tools.permissions.set_approver(approve)

        assert context.tools.execute("write_file", {"path": README, "content": "changed\n"}).ok
        assert not context.tools.execute("undo_file", {"path": README}).ok
        assert asked.count("undo_file") == 1

    def test_the_policy_decision_for_undo_is_ask(self, app_paths: Any) -> None:
        manager = ConfigManager(app_paths, secret_store=FileSecretStore(app_paths.secrets_file))
        context = ApplicationContext.create(paths=app_paths, config_manager=manager)
        context.config_manager.config.agent.auto_approve_writes = True
        provider = context._build_permission_provider()

        decision = provider.decide(
            PermissionRequest(
                permission=Permission.WRITE_WORKSPACE,
                tool="undo_file",
                summary="undo README.md",
                target=README,
            )
        )

        assert decision is Decision.ASK

    def test_a_denied_undo_leaves_the_file_as_the_agent_left_it(
        self, app_paths: Any, sample_project: Path
    ) -> None:
        context = _context(app_paths, sample_project)
        asked: list[str] = []
        context.tools.permissions.set_approver(_approver(ApprovalOutcome.ALLOW_ONCE, asked))
        context.tools.execute("write_file", {"path": README, "content": "changed\n"})
        context.tools.permissions.set_approver(_approver(ApprovalOutcome.DENY, asked))

        assert not context.tools.execute("undo_file", {"path": README}).ok
        assert (sample_project / README).read_text(encoding="utf-8") == "changed\n"
        assert context.session.revisions.depth(README) == 1


def _project(path: Path) -> Path:
    root = path / "project"
    root.mkdir(parents=True)
    (root / README).write_text("# Sample Project\n", encoding="utf-8")
    return root


def _revision(path: str, *, previous_bytes: bytes, result_sha256: str) -> Any:
    from quantsmind_agent.revisions import capture_revision

    return capture_revision(
        path=path,
        tool="write_file",
        previous_bytes=previous_bytes,
        result_sha256=result_sha256,
    )


def _message(text: str) -> Any:
    from quantsmind_agent.schema import Message, MessageRole

    return Message(role=MessageRole.USER, content=text)
