"""Tests for the undo tool and the writes that feed the journal.

The properties under test are the ones a user is entitled to assume: undo puts a
file back byte for byte, undoing a creation removes the file, one undo reverses
one step, and undo never silently overwrites a change it did not make. Every
mutation still goes through the registry, so approval is exercised the same way
the application does it.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import pytest

from quantsmind_agent.errors import InvalidPathError
from quantsmind_agent.permissions import (
    ApprovalOutcome,
    Decision,
    Permission,
    PermissionBroker,
    PermissionRule,
    StaticPermissionProvider,
)
from quantsmind_agent.revisions import RevisionJournal, capture_revision
from quantsmind_agent.tools import ToolRegistry
from quantsmind_agent.workspace import Workspace
from quantsmind_agent.write_tools import build_mutating_tools, build_undo_tool

README = "README.md"
SETTINGS = "src/app/settings.py"


def _registry(
    workspace: Workspace,
    journal: RevisionJournal,
    *,
    outcome: ApprovalOutcome | None = ApprovalOutcome.ALLOW_ONCE,
) -> ToolRegistry:
    broker = PermissionBroker(
        StaticPermissionProvider.for_v0_2(),
        approver=(lambda request: outcome) if outcome is not None else None,
    )
    registry = ToolRegistry(permissions=broker)
    registry.register_all(build_mutating_tools(workspace, journal=journal))
    registry.register(build_undo_tool(workspace, journal))
    return registry


@pytest.fixture
def journal() -> RevisionJournal:
    return RevisionJournal()


@pytest.fixture
def tools(workspace: Workspace, journal: RevisionJournal) -> ToolRegistry:
    return _registry(workspace, journal)


def _registry_without_journal(workspace: Workspace) -> ToolRegistry:
    """A registry whose tools have no undo list, as the acceptance harness uses."""

    broker = PermissionBroker(
        StaticPermissionProvider.for_v0_2(),
        approver=lambda request: ApprovalOutcome.ALLOW_ONCE,
    )
    registry = ToolRegistry(permissions=broker)
    registry.register_all(build_mutating_tools(workspace))
    return registry


class TestUndoRestoresBytes:
    def test_undo_after_write_file_restores_the_original_bytes(
        self, tools: ToolRegistry, sample_project: Path, journal: RevisionJournal
    ) -> None:
        target = sample_project / README
        original = target.read_bytes()

        assert tools.execute("write_file", {"path": README, "content": "clobbered\n"}).ok
        assert target.read_text(encoding="utf-8") == "clobbered\n"

        result = tools.execute("undo_file", {"path": README})

        assert result.ok
        assert target.read_bytes() == original
        assert journal.depth(README) == 0

    def test_undo_after_edit_file_restores_the_original_bytes(
        self, tools: ToolRegistry, sample_project: Path, journal: RevisionJournal
    ) -> None:
        target = sample_project / SETTINGS
        original = target.read_bytes()

        assert tools.execute(
            "edit_file",
            {"path": SETTINGS, "operation": "replace", "old_text": "8080", "new_text": "9090"},
        ).ok
        assert "9090" in target.read_text(encoding="utf-8")

        assert tools.execute("undo_file", {"path": SETTINGS}).ok

        assert target.read_bytes() == original
        assert journal.depth(SETTINGS) == 0

    def test_crlf_line_endings_come_back_as_crlf(
        self, tools: ToolRegistry, sample_project: Path
    ) -> None:
        target = sample_project / "windows.txt"
        target.write_bytes(b"first\r\nsecond\r\n")

        tools.execute("write_file", {"path": "windows.txt", "content": "replaced\n"})

        assert tools.execute("undo_file", {"path": "windows.txt"}).ok

        assert target.read_bytes() == b"first\r\nsecond\r\n"

    def test_content_that_is_not_valid_utf8_comes_back_unchanged(
        self, tools: ToolRegistry, sample_project: Path
    ) -> None:
        # A lossy text round-trip would turn these bytes into U+FFFD, which is
        # precisely the failure "restore the exact bytes" exists to prevent.
        target = sample_project / "latin1.txt"
        original = b"caf\xe9\r\nna\xefve\n"
        target.write_bytes(original)

        tools.execute("write_file", {"path": "latin1.txt", "content": "plain\n"})

        assert tools.execute("undo_file", {"path": "latin1.txt"}).ok

        assert target.read_bytes() == original

    def test_the_restored_file_matches_the_recorded_digest(
        self, tools: ToolRegistry, sample_project: Path
    ) -> None:
        target = sample_project / README
        before = target.read_bytes()

        tools.execute("write_file", {"path": README, "content": "clobbered\n"})
        result = tools.execute("undo_file", {"path": README})

        assert result.data["restored_sha256"] == hashlib.sha256(before).hexdigest()
        assert result.data["action"] == "restored"
        assert result.data["remaining_undo_steps"] == 0


class TestUndoOfACreation:
    def test_undo_removes_a_file_the_agent_created(
        self, tools: ToolRegistry, sample_project: Path, journal: RevisionJournal
    ) -> None:
        created = sample_project / "created.txt"

        assert tools.execute("write_file", {"path": "created.txt", "content": "new\n"}).ok
        assert created.exists()
        assert journal.depth("created.txt") == 1

        result = tools.execute("undo_file", {"path": "created.txt"})

        assert result.ok
        assert not created.exists(), "undoing a creation must restore absence"
        assert result.data["action"] == "removed"
        assert result.data["was_creation"] is True
        assert journal.size == 0

    def test_emptying_a_created_file_is_not_the_same_as_removing_it(
        self, tools: ToolRegistry, sample_project: Path
    ) -> None:
        target = sample_project / "empty.txt"

        tools.execute("write_file", {"path": "empty.txt", "content": ""})
        tools.execute("undo_file", {"path": "empty.txt"})

        assert not target.exists()


class TestStacksAreIndependent:
    def test_successive_edits_undo_in_reverse_order(
        self, tools: ToolRegistry, sample_project: Path, journal: RevisionJournal
    ) -> None:
        target = sample_project / SETTINGS
        original = target.read_bytes()

        for old, new in (("8080", "8081"), ("8081", "8082")):
            assert tools.execute(
                "edit_file",
                {"path": SETTINGS, "operation": "replace", "old_text": old, "new_text": new},
            ).ok
        assert journal.depth(SETTINGS) == 2

        first_undo = tools.execute("undo_file", {"path": SETTINGS})
        assert first_undo.ok
        assert "8081" in target.read_text(encoding="utf-8")
        assert "8082" not in target.read_text(encoding="utf-8")
        assert first_undo.data["remaining_undo_steps"] == 1

        assert tools.execute("undo_file", {"path": SETTINGS}).ok

        assert target.read_bytes() == original
        assert journal.depth(SETTINGS) == 0

    def test_one_call_undoes_exactly_one_step(
        self, tools: ToolRegistry, sample_project: Path
    ) -> None:
        target = sample_project / README

        tools.execute("write_file", {"path": README, "content": "one\n"})
        tools.execute("write_file", {"path": README, "content": "two\n"})

        tools.execute("undo_file", {"path": README})

        assert target.read_text(encoding="utf-8") == "one\n"

    def test_separate_paths_undo_independently(
        self, tools: ToolRegistry, sample_project: Path, journal: RevisionJournal
    ) -> None:
        readme = sample_project / README
        settings = sample_project / SETTINGS
        readme_original = readme.read_bytes()
        settings_original = settings.read_bytes()

        tools.execute("write_file", {"path": README, "content": "changed readme\n"})
        tools.execute(
            "edit_file",
            {"path": SETTINGS, "operation": "replace", "old_text": "8080", "new_text": "1"},
        )
        assert journal.size == 2

        tools.execute("undo_file", {"path": README})

        assert readme.read_bytes() == readme_original
        assert "DEFAULT_PORT = 1" in settings.read_text(encoding="utf-8")
        assert journal.depth(SETTINGS) == 1

        tools.execute("undo_file", {"path": SETTINGS})

        assert settings.read_bytes() == settings_original
        assert journal.size == 0

    def test_a_different_path_has_nothing_to_undo(
        self, tools: ToolRegistry, sample_project: Path
    ) -> None:
        tools.execute("write_file", {"path": README, "content": "changed\n"})

        result = tools.execute("undo_file", {"path": SETTINGS})

        assert not result.ok
        assert "nothing to undo" in (result.error or "")
        assert (sample_project / README).read_text(encoding="utf-8") == "changed\n"


class TestFailedOperationsLeaveNoTrace:
    def test_a_refused_write_creates_no_revision(
        self, tools: ToolRegistry, journal: RevisionJournal
    ) -> None:
        result = tools.execute(
            "write_file", {"path": "missing_dir/a.txt", "content": "x", "create_parents": False}
        )

        assert not result.ok
        assert journal.size == 0

    def test_a_stale_write_creates_no_revision(
        self, tools: ToolRegistry, journal: RevisionJournal
    ) -> None:
        result = tools.execute(
            "write_file", {"path": README, "content": "x", "expected_sha256": "0" * 64}
        )

        assert not result.ok
        assert journal.size == 0

    def test_a_failed_edit_creates_no_revision(
        self, tools: ToolRegistry, sample_project: Path, journal: RevisionJournal
    ) -> None:
        target = sample_project / SETTINGS
        before = target.read_bytes()

        result = tools.execute(
            "edit_file",
            {"path": SETTINGS, "operation": "replace", "old_text": "not there", "new_text": "x"},
        )

        assert not result.ok
        assert journal.size == 0
        assert target.read_bytes() == before

    def test_a_failed_undo_keeps_the_revision_for_a_retry(
        self,
        workspace: Workspace,
        tools: ToolRegistry,
        sample_project: Path,
        journal: RevisionJournal,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        target = sample_project / README
        original = target.read_bytes()
        tools.execute("write_file", {"path": README, "content": "clobbered\n"})

        def refuse(*args: object, **kwargs: object) -> None:
            raise InvalidPathError(
                "no space left", user_message="'README.md' could not be written."
            )

        monkeypatch.setattr(workspace, "write_bytes", refuse)

        failed = tools.execute("undo_file", {"path": README})

        assert not failed.ok
        assert journal.depth(README) == 1, "a failed undo must stay retryable"
        assert target.read_text(encoding="utf-8") == "clobbered\n"

        monkeypatch.undo()

        retried = tools.execute("undo_file", {"path": README})

        assert retried.ok
        assert target.read_bytes() == original
        assert journal.depth(README) == 0

    def test_a_write_without_a_journal_is_honestly_reported_as_not_undoable(
        self, workspace: Workspace, sample_project: Path
    ) -> None:
        registry = _registry_without_journal(workspace)

        result = registry.execute("write_file", {"path": README, "content": "changed\n"})

        assert result.ok
        assert result.data["undoable"] is False


class TestExternalChangesAreNeverOverwritten:
    def test_a_file_edited_outside_the_agent_blocks_the_undo(
        self, tools: ToolRegistry, sample_project: Path, journal: RevisionJournal
    ) -> None:
        target = sample_project / README
        tools.execute("write_file", {"path": README, "content": "agent version\n"})
        target.write_text("someone else's work\n", encoding="utf-8")

        result = tools.execute("undo_file", {"path": README})

        assert not result.ok
        assert "changed" in (result.error or "")
        assert target.read_text(encoding="utf-8") == "someone else's work\n"
        assert journal.depth(README) == 1, "a refused undo must keep the revision"

    def test_a_file_deleted_outside_the_agent_blocks_the_undo(
        self, tools: ToolRegistry, sample_project: Path, journal: RevisionJournal
    ) -> None:
        target = sample_project / README
        tools.execute("write_file", {"path": README, "content": "agent version\n"})
        target.unlink()

        result = tools.execute("undo_file", {"path": README})

        assert not result.ok
        assert "removed" in (result.error or "")
        assert not target.exists()
        assert journal.depth(README) == 1

    def test_undoing_twice_walks_further_back_through_one_file(
        self, tools: ToolRegistry, sample_project: Path
    ) -> None:
        # Two writes on one path are one stack, not two unrelated entries: after
        # the first undo the file is exactly what the first write left, which is
        # the state the older revision expects.
        target = sample_project / README
        original = target.read_bytes()
        tools.execute("write_file", {"path": README, "content": "one\n"})
        tools.execute("write_file", {"path": README, "content": "two\n"})

        assert tools.execute("undo_file", {"path": README}).ok
        assert tools.execute("undo_file", {"path": README}).ok

        assert target.read_bytes() == original


class TestDeleteStaysIrreversible:
    def test_delete_file_records_nothing(
        self, tools: ToolRegistry, journal: RevisionJournal
    ) -> None:
        tools.execute("write_file", {"path": README, "content": "changed\n"})
        depth_before = journal.depth(README)

        assert tools.execute("delete_file", {"path": README}).ok

        assert journal.depth(README) == depth_before

    def test_a_deleted_file_cannot_be_brought_back_by_undo(
        self, tools: ToolRegistry, sample_project: Path
    ) -> None:
        target = sample_project / README
        tools.execute("write_file", {"path": README, "content": "changed\n"})

        assert tools.execute("delete_file", {"path": README}).ok
        result = tools.execute("undo_file", {"path": README})

        assert not result.ok
        assert not target.exists(), "delete_file is the one operation undo must not reverse"


class TestContainmentIsStillEnforced:
    @pytest.mark.parametrize(
        "bad_path",
        [
            "../escape.txt",
            "src/../../escape.txt",
            "C:/Windows/System32/evil.txt",
            "//server/share/evil.txt",
            "~/escape.txt",
            "with\x00null.txt",
        ],
    )
    def test_escape_routes_are_refused(
        self,
        tools: ToolRegistry,
        sample_project: Path,
        escape_target: Path,
        bad_path: str,
    ) -> None:
        assert not tools.execute("undo_file", {"path": bad_path}).ok
        assert (
            escape_target.read_text(encoding="utf-8")
            == "this must never be readable by the agent\n"
        )

    def test_a_forged_journal_entry_cannot_escape_the_workspace(
        self, tools: ToolRegistry, escape_target: Path, journal: RevisionJournal
    ) -> None:
        # Even if a revision were somehow recorded for a path outside the root,
        # the restore still goes through the workspace containment check.
        journal.record(
            capture_revision(
                path="../outside_secret.txt",
                tool="write_file",
                previous_bytes=b"clobbered by the agent\n",
                result_sha256="0" * 64,
            )
        )

        result = tools.execute("undo_file", {"path": "../outside_secret.txt"})

        assert not result.ok
        assert "outside" in (result.error or "").lower()
        assert (
            escape_target.read_text(encoding="utf-8")
            == "this must never be readable by the agent\n"
        )

    def test_a_missing_path_is_refused(self, tools: ToolRegistry) -> None:
        assert not tools.execute("undo_file", {}).ok


class TestApprovalIsAlwaysRequired:
    def test_undo_asks_even_when_writes_are_auto_approved(self, workspace: Workspace) -> None:
        journal = RevisionJournal()
        broker = PermissionBroker(
            _auto_approve_writes_policy(),
            approver=lambda request: ApprovalOutcome.DENY,
        )
        registry = ToolRegistry(permissions=broker)
        registry.register_all(build_mutating_tools(workspace, journal=journal))
        registry.register(build_undo_tool(workspace, journal))

        assert registry.execute("write_file", {"path": "a.txt", "content": "x\n"}).ok
        # The user denied it, so the file stays exactly as the agent left it.
        assert not registry.execute("undo_file", {"path": "a.txt"}).ok
        assert (workspace.root / "a.txt").read_text(encoding="utf-8") == "x\n"

    def test_undo_asks_even_when_a_session_grant_exists_for_another_tool(
        self, workspace: Workspace, journal: RevisionJournal
    ) -> None:
        asked: list[str] = []

        def approve(request: Any) -> ApprovalOutcome:
            asked.append(request.tool)
            if request.tool == "undo_file":
                return ApprovalOutcome.DENY
            return ApprovalOutcome.ALLOW_FOR_SESSION

        # The shipped policy asks for writes, so the grant below is a real one.
        broker = PermissionBroker(
            StaticPermissionProvider.for_v0_2(),
            approver=approve,
        )
        registry = ToolRegistry(permissions=broker)
        registry.register_all(build_mutating_tools(workspace, journal=journal))
        registry.register(build_undo_tool(workspace, journal))

        assert registry.execute("write_file", {"path": "a.txt", "content": "x\n"}).ok
        assert (Permission.WRITE_WORKSPACE, "write_file") in broker.session_grants

        assert not registry.execute("undo_file", {"path": "a.txt"}).ok
        assert "undo_file" in asked, "undo_file must reach the user even with a grant on write_file"

    def test_no_approver_denies_undo(self, workspace: Workspace, journal: RevisionJournal) -> None:
        broker = PermissionBroker(
            StaticPermissionProvider.for_v0_2(),
            approver=lambda request: ApprovalOutcome.ALLOW_ONCE,
        )
        registry = ToolRegistry(permissions=broker)
        registry.register_all(build_mutating_tools(workspace, journal=journal))
        registry.register(build_undo_tool(workspace, journal))
        registry.execute("write_file", {"path": "a.txt", "content": "x\n"})

        broker.set_approver(None)

        assert not registry.execute("undo_file", {"path": "a.txt"}).ok
        assert (workspace.root / "a.txt").exists()

    def test_a_hostile_argument_cannot_change_the_permission(
        self, workspace: Workspace, journal: RevisionJournal
    ) -> None:
        registry = _registry(workspace, journal, outcome=ApprovalOutcome.ALLOW_ONCE)
        registry.execute("write_file", {"path": "a.txt", "content": "x\n"})
        asked: list[str] = []
        registry.permissions.set_approver(_record_and_deny(asked))

        result = registry.execute(
            "undo_file",
            {"path": "a.txt", "permission": "WRITE_WORKSPACE", "decision": "ALLOW"},
        )

        assert not result.ok
        assert "not permitted" in (result.error or "")
        assert asked == ["undo_file"], "the injected keys must not skip the approval"
        assert (workspace.root / "a.txt").read_text(encoding="utf-8") == "x\n"


def _record_and_deny(asked: list[str]) -> Any:
    def approve(request: Any) -> ApprovalOutcome:
        asked.append(request.tool)
        return ApprovalOutcome.DENY

    return approve


def _auto_approve_writes_policy() -> StaticPermissionProvider:
    """The policy the application builds when auto_approve_writes is on.

    Mirrors :meth:`ApplicationContext._build_permission_provider`: the per-tool
    ASK rules are added first so the permissive wildcard below cannot reach them.
    """

    provider = StaticPermissionProvider()
    provider.add_rule(PermissionRule(Permission.WRITE_WORKSPACE, Decision.ASK, tool="delete_file"))
    provider.add_rule(PermissionRule(Permission.WRITE_WORKSPACE, Decision.ASK, tool="undo_file"))
    provider.add_rule(PermissionRule(Permission.EXECUTE_COMMAND, Decision.ASK, tool="run_command"))
    provider.add_rule(
        PermissionRule(Permission.READ_WORKSPACE, Decision.ALLOW, tool="*", target_pattern="*")
    )
    provider.add_rule(
        PermissionRule(Permission.WRITE_WORKSPACE, Decision.ALLOW, tool="*", target_pattern="*")
    )
    provider.add_rule(PermissionRule(Permission.EXECUTE_COMMAND, Decision.ASK))
    provider.add_rule(PermissionRule(Permission.NETWORK, Decision.DENY))
    return provider
