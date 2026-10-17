"""Unit tests for the v0.2 mutating filesystem tools.

Security-focused: every escape route the v0.1 read tools refuse must also be
refused on the way *in*, because a write is far harder to undo than a read.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from quantsmind_agent.edit_ops import EditOperation, apply_edit, detect_eol, normalize_newlines
from quantsmind_agent.errors import EditConflictError, StaleContentError
from quantsmind_agent.permissions import (
    ApprovalOutcome,
    Decision,
    Permission,
    PermissionBroker,
    PermissionRule,
    StaticPermissionProvider,
)
from quantsmind_agent.tools import ToolRegistry
from quantsmind_agent.workspace import Workspace
from quantsmind_agent.write_tools import build_mutating_tools


def _registry(
    workspace: Workspace,
    *,
    outcome: ApprovalOutcome | None = ApprovalOutcome.ALLOW_ONCE,
) -> ToolRegistry:
    broker = PermissionBroker(
        StaticPermissionProvider.for_v0_2(),
        approver=(lambda request: outcome) if outcome is not None else None,
    )
    registry = ToolRegistry(permissions=broker)
    registry.register_all(build_mutating_tools(workspace))
    return registry


@pytest.fixture
def tools(workspace: Workspace) -> ToolRegistry:
    return _registry(workspace)


@pytest.fixture
def denying(workspace: Workspace) -> ToolRegistry:
    return _registry(workspace, outcome=ApprovalOutcome.DENY)


class TestEditOperations:
    """Pure edit semantics, including the staleness guards."""

    def test_replace_first_occurrence(self) -> None:
        out = apply_edit("a\nb\na\n", "f.py", operation="replace", old_text="a", new_text="X")
        assert out.content == "X\nb\na\n"

    def test_replace_nth_occurrence(self) -> None:
        out = apply_edit(
            "a\na\na\n", "f.py", operation="replace", old_text="a", new_text="Z", occurrence=2
        )
        assert out.content == "a\nZ\na\n"

    def test_replace_all(self) -> None:
        out = apply_edit("x\nx\n", "f.py", operation="replace_all", old_text="x", new_text="y")
        assert out.content == "y\ny\n"
        assert out.occurrences == 2

    def test_multiline_old_text(self) -> None:
        out = apply_edit("a\nb\nc\n", "f.py", operation="replace", old_text="a\nb", new_text="X")
        assert out.content == "X\nc\n"

    def test_old_text_may_end_with_a_newline(self) -> None:
        # A model copies the line it saw in read_file, newline included. Matching
        # only worked without it, so the natural edit was refused on the last line
        # of a file with the message "the text you asked to replace is not in the
        # file any more" -- which was not true.
        out = apply_edit("a\nb\n", "f.py", operation="replace", old_text="b\n", new_text="Z\n")
        assert out.content == "a\nZ\n"

    def test_replacing_the_last_line_leaves_exactly_one_trailing_newline(self) -> None:
        out = apply_edit("a\nb\n", "f.py", operation="replace", old_text="b\n", new_text="Z")
        assert out.content == "a\nZ\n"

    def test_a_file_without_a_trailing_newline_still_matches(self) -> None:
        out = apply_edit("a\nb", "f.py", operation="replace", old_text="b", new_text="Z")
        assert out.content == "a\nZ"

    @pytest.mark.parametrize(
        ("operation", "kwargs", "expected"),
        [
            ("replace_lines", {"start_line": 2, "end_line": 3, "new_text": "Z"}, "a\nZ\n"),
            ("insert_before", {"old_text": "b", "new_text": "N"}, "a\nN\nb\nc\n"),
            ("insert_after", {"old_text": "b", "new_text": "N"}, "a\nb\nN\nc\n"),
            ("append", {"new_text": "tail"}, "a\nb\nc\ntail\n"),
            ("prepend", {"new_text": "head"}, "head\na\nb\nc\n"),
        ],
    )
    def test_operations(self, operation: str, kwargs: Any, expected: str) -> None:
        out = apply_edit("a\nb\nc\n", "f.py", operation=operation, **kwargs)
        assert out.content == expected

    def test_crlf_line_endings_are_preserved(self) -> None:
        out = apply_edit("a\r\nb\r\nc\r\n", "f.py", operation="replace", old_text="b", new_text="B")
        assert out.content == "a\r\nB\r\nc\r\n"
        assert "\n" not in out.content.replace("\r\n", "")
        assert out.eol == "\r\n"

    def test_detect_eol(self) -> None:
        assert detect_eol("a\r\nb\r\n") == "\r\n"
        assert detect_eol("a\nb\n") == "\n"
        assert detect_eol("no newlines") == "\n"

    def test_normalize_newlines(self) -> None:
        assert normalize_newlines("a\r\nb\rc\n") == "a\nb\nc\n"

    def test_file_without_trailing_newline_stays_that_way(self) -> None:
        out = apply_edit("a\nb", "f.py", operation="replace", old_text="b", new_text="c")
        assert out.content == "a\nc"


class TestEditStaleness:
    """A mismatch must fail rather than destroy work."""

    def test_absent_text_is_stale(self) -> None:
        with pytest.raises(StaleContentError):
            apply_edit("a\n", "f.py", operation="replace", old_text="zz", new_text="x")

    def test_occurrence_count_mismatch_is_stale(self) -> None:
        with pytest.raises(StaleContentError):
            apply_edit(
                "x\nx\n",
                "f.py",
                operation="replace",
                old_text="x",
                new_text="y",
                expected_occurrences=3,
            )

    def test_line_range_past_end_is_stale(self) -> None:
        with pytest.raises(StaleContentError):
            apply_edit(
                "a\nb\n", "f.py", operation="replace_lines", start_line=1, end_line=99, new_text="x"
            )

    def test_missing_line_fields_is_a_conflict(self) -> None:
        with pytest.raises(EditConflictError):
            apply_edit("a\n", "f.py", operation="replace_lines", new_text="x")

    def test_unknown_operation_is_a_conflict(self) -> None:
        with pytest.raises(EditConflictError):
            apply_edit("a\n", "f.py", operation="destroy_everything", new_text="x")

    def test_occurrence_out_of_range_is_a_conflict(self) -> None:
        with pytest.raises(EditConflictError):
            apply_edit(
                "x\nx\n", "f.py", operation="replace", old_text="x", new_text="y", occurrence=7
            )

    def test_empty_old_text_is_a_conflict(self) -> None:
        with pytest.raises(EditConflictError):
            apply_edit("a\n", "f.py", operation="replace", new_text="y")

    def test_all_edit_operations_are_declared(self) -> None:
        assert {member.value for member in EditOperation} == {
            "replace",
            "replace_all",
            "insert_before",
            "insert_after",
            "replace_lines",
            "append",
            "prepend",
        }


class TestWriteFile:
    def test_creates_a_new_file(self, tools: ToolRegistry, sample_project: Path) -> None:
        result = tools.execute("write_file", {"path": "src/created.py", "content": "x = 1\n"})
        assert result.ok
        assert result.data["created"] is True
        assert result.data["replaced"] is False
        assert (sample_project / "src" / "created.py").read_text() == "x = 1\n"

    def test_replaces_an_existing_file(self, tools: ToolRegistry, sample_project: Path) -> None:
        result = tools.execute("write_file", {"path": "README.md", "content": "new\n"})
        assert result.ok
        assert result.data["replaced"] is True
        assert (sample_project / "README.md").read_text() == "new\n"

    def test_parent_directory_must_be_authorised(
        self, tools: ToolRegistry, sample_project: Path
    ) -> None:
        denied = tools.execute("write_file", {"path": "fresh/dir/a.py", "content": "x"})
        assert not denied.ok
        assert not (sample_project / "fresh").exists()

        allowed = tools.execute(
            "write_file", {"path": "fresh/dir/a.py", "content": "x", "create_parents": True}
        )
        assert allowed.ok
        assert (sample_project / "fresh" / "dir" / "a.py").is_file()

    def test_refuses_to_overwrite_a_directory(self, tools: ToolRegistry) -> None:
        assert not tools.execute("write_file", {"path": "src", "content": "x"}).ok

    def test_rejects_a_missing_path(self, tools: ToolRegistry) -> None:
        assert not tools.execute("write_file", {"content": "x"}).ok

    def test_rejects_non_string_content(self, tools: ToolRegistry) -> None:
        assert not tools.execute("write_file", {"path": "a.txt", "content": 5}).ok

    def test_rejects_null_byte_in_content(self, tools: ToolRegistry) -> None:
        assert not tools.execute("write_file", {"path": "a.txt", "content": "a\x00b"}).ok

    def test_expected_sha256_blocks_a_concurrent_change(
        self, tools: ToolRegistry, sample_project: Path
    ) -> None:
        result = tools.execute(
            "write_file",
            {"path": "README.md", "content": "x", "expected_sha256": "0" * 64},
        )
        assert not result.ok
        assert "changed" in (result.error or "").lower()
        assert (sample_project / "README.md").read_text().startswith("# Sample Project")


class TestWriteFileContainment:
    @pytest.mark.parametrize(
        "bad_path",
        [
            "../escape.txt",
            "src/../../escape.txt",
            "..",
            "C:/Windows/System32/evil.txt",
            "C:\\Windows\\evil.txt",
            "//server/share/evil.txt",
            "\\\\server\\share\\evil.txt",
            "~/escape.txt",
            "with\x00null.txt",
        ],
    )
    def test_every_escape_route_is_refused(
        self, tools: ToolRegistry, sample_project: Path, escape_target: Path, bad_path: str
    ) -> None:
        result = tools.execute("write_file", {"path": bad_path, "content": "pwned"})
        assert not result.ok, f"{bad_path!r} was not refused"
        assert escape_target.read_text() != "pwned"
        assert not (sample_project.parent / "escape.txt").exists()

    def test_write_through_an_escaping_symlink_is_refused(
        self, tools: ToolRegistry, sample_project: Path, escape_target: Path
    ) -> None:
        link = sample_project / "escape_link"
        try:
            link.symlink_to(escape_target.parent)
        except (OSError, NotImplementedError):  # pragma: no cover - privilege dependent
            pytest.skip("symlinks are not available on this platform")
        result = tools.execute("write_file", {"path": "escape_link/owned.txt", "content": "pwned"})
        assert not result.ok
        assert not (escape_target.parent / "owned.txt").exists()


class TestEditFileTool:
    def test_successful_edit(self, tools: ToolRegistry, sample_project: Path) -> None:
        target = sample_project / "src" / "app" / "settings.py"
        result = tools.execute(
            "edit_file",
            {
                "path": "src/app/settings.py",
                "operation": "replace",
                "old_text": "8080",
                "new_text": "9090",
            },
        )
        assert result.ok
        assert "9090" in target.read_text()
        assert result.data["lines_before"] == result.data["lines_after"]

    def test_stale_content_is_refused_and_nothing_is_written(
        self, tools: ToolRegistry, sample_project: Path
    ) -> None:
        target = sample_project / "src" / "app" / "settings.py"
        before = target.read_text()
        result = tools.execute(
            "edit_file",
            {
                "path": "src/app/settings.py",
                "operation": "replace",
                "old_text": "9999",
                "new_text": "x",
            },
        )
        assert not result.ok
        assert "no longer matches" in (result.error or "")
        assert target.read_text() == before

    def test_expected_sha256_refuses_a_changed_file(
        self, tools: ToolRegistry, sample_project: Path
    ) -> None:
        target = sample_project / "src" / "app" / "settings.py"
        result = tools.execute(
            "edit_file",
            {
                "path": "src/app/settings.py",
                "operation": "replace",
                "old_text": "8080",
                "new_text": "1",
                "expected_sha256": "0" * 64,
            },
        )
        assert not result.ok
        assert "8080" in target.read_text()

    def test_missing_file_is_refused(self, tools: ToolRegistry) -> None:
        result = tools.execute(
            "edit_file", {"path": "does/not/exist.py", "operation": "append", "new_text": "x"}
        )
        assert not result.ok
        assert "does not exist" in (result.error or "")

    def test_append_creates_content(self, tools: ToolRegistry, sample_project: Path) -> None:
        result = tools.execute(
            "edit_file",
            {"path": "src/app/settings.py", "operation": "append", "new_text": "\nEXTRA = 1"},
        )
        assert result.ok
        assert "EXTRA = 1" in (sample_project / "src" / "app" / "settings.py").read_text()

    @pytest.mark.parametrize(
        "bad_path", ["../escape.py", "C:/Windows/evil.py", "//host/share/x.py", "a\x00b.py"]
    )
    def test_traversal_and_absolute_paths_are_refused(
        self, tools: ToolRegistry, bad_path: str
    ) -> None:
        result = tools.execute(
            "edit_file", {"path": bad_path, "operation": "append", "new_text": "pwned"}
        )
        assert not result.ok

    def test_edit_through_an_escaping_symlink_is_refused(
        self, tools: ToolRegistry, sample_project: Path, escape_target: Path
    ) -> None:
        link = sample_project / "escape_link"
        try:
            link.symlink_to(escape_target.parent)
        except (OSError, NotImplementedError):  # pragma: no cover - privilege dependent
            pytest.skip("symlinks are not available on this platform")
        result = tools.execute(
            "edit_file",
            {"path": "escape_link/owned.py", "operation": "append", "new_text": "pwned"},
        )
        assert not result.ok
        assert not (escape_target.parent / "owned.py").exists()

    def test_permission_denial_leaves_the_file_untouched(
        self, denying: ToolRegistry, sample_project: Path
    ) -> None:
        target = sample_project / "src" / "app" / "settings.py"
        before = target.read_text()
        result = denying.execute(
            "edit_file",
            {
                "path": "src/app/settings.py",
                "operation": "replace",
                "old_text": "8080",
                "new_text": "1",
            },
        )
        assert not result.ok
        assert "not permitted" in (result.error or "")
        assert target.read_text() == before

    def test_approval_denial_leaves_the_file_untouched(
        self, denying: ToolRegistry, sample_project: Path
    ) -> None:
        target = sample_project / "README.md"
        before = target.read_text()
        result = denying.execute("write_file", {"path": "README.md", "content": "pwned\n"})
        assert not result.ok
        assert target.read_text() == before


class TestDeleteFile:
    def test_deletes_a_file(self, tools: ToolRegistry, sample_project: Path) -> None:
        target = sample_project / "src" / "app" / "settings.py"
        result = tools.execute("delete_file", {"path": "src/app/settings.py"})
        assert result.ok
        assert not target.exists()
        assert result.data["path"] == "src/app/settings.py"

    def test_directories_are_never_deleted_recursively(
        self, tools: ToolRegistry, sample_project: Path
    ) -> None:
        folder = sample_project / "src"
        result = tools.execute("delete_file", {"path": "src"})
        assert not result.ok
        assert folder.is_dir()
        assert (folder / "app" / "settings.py").is_file()

    def test_missing_file_is_refused(self, tools: ToolRegistry) -> None:
        assert not tools.execute("delete_file", {"path": "no/such/file.py"}).ok

    @pytest.mark.parametrize(
        "bad_path", ["../outside.txt", "C:/Windows/evil.txt", "//host/share/x.txt", "a\x00b"]
    )
    def test_escape_routes_are_refused(self, tools: ToolRegistry, bad_path: str) -> None:
        assert not tools.execute("delete_file", {"path": bad_path}).ok

    def test_expected_sha256_refuses_a_changed_file(
        self, tools: ToolRegistry, sample_project: Path
    ) -> None:
        result = tools.execute("delete_file", {"path": "README.md", "expected_sha256": "0" * 64})
        assert not result.ok
        assert (sample_project / "README.md").exists()

    def test_user_denial_keeps_the_file(self, denying: ToolRegistry, sample_project: Path) -> None:
        result = denying.execute("delete_file", {"path": "README.md"})
        assert not result.ok
        assert (sample_project / "README.md").exists()


class TestPermissionEnforcement:
    """The model must not be able to widen its own permissions via arguments."""

    def test_delete_always_asks_even_when_writes_are_auto_approved(
        self, workspace: Workspace
    ) -> None:
        broker = PermissionBroker(
            _auto_approve_writes_policy(),
            approver=lambda request: ApprovalOutcome.DENY,
        )
        registry = ToolRegistry(permissions=broker)
        registry.register_all(build_mutating_tools(workspace))

        assert registry.execute("write_file", {"path": "a.txt", "content": "x"}).ok
        assert not registry.execute("delete_file", {"path": "a.txt"}).ok

    def test_a_hostile_path_cannot_change_the_permission(self, workspace: Workspace) -> None:
        registry = _registry(workspace, outcome=ApprovalOutcome.DENY)
        result = registry.execute(
            "write_file",
            {"path": "a.txt", "content": "x", "permission": "WRITE_WORKSPACE", "decision": "ALLOW"},
        )
        assert not result.ok

    def test_no_approver_means_denied(self, workspace: Workspace) -> None:
        registry = _registry(workspace, outcome=None)
        assert not registry.execute("write_file", {"path": "a.txt", "content": "x"}).ok
        assert not registry.execute(
            "edit_file", {"path": "a.txt", "operation": "append", "new_text": "x"}
        ).ok
        assert not registry.execute("delete_file", {"path": "a.txt"}).ok


def _auto_approve_writes_policy() -> StaticPermissionProvider:
    """The policy the application builds when auto_approve_writes is on.

    Rule order is significant: decide() sorts by "exact tool first, then a
    non-wildcard target pattern" and that sort is stable, so a permissive
    wildcard appended *after* a restrictive wildcard is unreachable. The
    application therefore emits permissive wildcards before restrictive ones,
    and this helper mirrors that ordering so the test exercises a policy that
    can actually take effect.
    """

    provider = StaticPermissionProvider()
    provider.add_rule(PermissionRule(Permission.WRITE_WORKSPACE, Decision.ASK, tool="delete_file"))
    provider.add_rule(PermissionRule(Permission.EXECUTE_COMMAND, Decision.ASK, tool="run_command"))
    provider.add_rule(
        PermissionRule(
            permission=Permission.WRITE_WORKSPACE,
            decision=Decision.ALLOW,
            tool="*",
            target_pattern="*",
        )
    )
    provider.add_rule(PermissionRule(Permission.EXECUTE_COMMAND, Decision.ASK))
    provider.add_rule(PermissionRule(Permission.NETWORK, Decision.DENY))
    return provider
