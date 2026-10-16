"""The mutating tools must report what they changed, not just that they changed.

These run through the registry, so they also confirm the diff sits alongside the
existing metadata rather than replacing it, and that it survives the trip to the
model as a bounded observation.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from quantsmind_agent.permissions import ApprovalOutcome, PermissionBroker, StaticPermissionProvider
from quantsmind_agent.revisions import RevisionJournal
from quantsmind_agent.tools import ToolRegistry
from quantsmind_agent.workspace import Workspace
from quantsmind_agent.write_tools import build_mutating_tools, build_undo_tool

README = "README.md"
SETTINGS = "src/app/settings.py"


@pytest.fixture
def tools(workspace: Workspace) -> ToolRegistry:
    broker = PermissionBroker(
        StaticPermissionProvider.for_v0_2(),
        approver=lambda request: ApprovalOutcome.ALLOW_ONCE,
    )
    registry = ToolRegistry(permissions=broker)
    registry.register_all(build_mutating_tools(workspace, journal=RevisionJournal()))
    registry.register(build_undo_tool(workspace, RevisionJournal()))
    return registry


class TestWriteFileReportsADiff:
    def test_replacing_a_file_shows_the_old_and_new_lines(self, tools: ToolRegistry) -> None:
        result = tools.execute("write_file", {"path": README, "content": "# Replaced\n"})

        assert result.ok
        assert result.data["diff_kind"] == "modified"
        assert "-# Sample Project" in result.data["diff"]
        assert "+# Replaced" in result.data["diff"]
        assert result.data["diff_added"] >= 1
        assert result.data["diff_removed"] >= 1
        assert result.data["diff_truncated"] is False

    def test_the_existing_metadata_is_still_there(self, tools: ToolRegistry) -> None:
        result = tools.execute("write_file", {"path": README, "content": "# Replaced\n"})

        # The diff is additive: the digests and flags other consumers rely on
        # must not disappear or change shape.
        assert result.data["sha256"]
        assert result.data["replaced"] is True
        assert result.data["created"] is False
        assert result.data["undoable"] is True
        assert result.data["bytes"] == len("# Replaced\n")

    def test_creating_a_file_says_created_instead_of_showing_an_empty_diff(
        self, tools: ToolRegistry
    ) -> None:
        result = tools.execute("write_file", {"path": "fresh.txt", "content": "hello\n"})

        assert result.ok
        assert result.data["diff_kind"] == "created"
        assert result.data["diff"] == ""

    def test_an_unreadable_pre_image_is_reported_as_unavailable(self, sample_project: Path) -> None:
        # A file larger than the tool's own byte limit cannot be captured as a
        # pre-image, so there is nothing honest to show.
        target = sample_project / "big.txt"
        target.write_text("x" * 5000, encoding="utf-8")
        small = _small_limit_registry(sample_project)

        result = small.execute("write_file", {"path": "big.txt", "content": "small\n"})

        assert result.ok
        assert result.data["diff_kind"] == "unavailable"
        assert result.data["diff"] == ""
        assert result.data["undoable"] is False


class TestEditFileReportsADiff:
    def test_the_diff_shows_only_the_edited_line(self, tools: ToolRegistry) -> None:
        result = tools.execute(
            "edit_file",
            {"path": SETTINGS, "operation": "replace", "old_text": "8080", "new_text": "9090"},
        )

        assert result.ok
        assert result.data["diff_kind"] == "modified"
        assert "-DEFAULT_PORT = 8080" in result.data["diff"]
        assert "+DEFAULT_PORT = 9090" in result.data["diff"]
        assert result.data["diff_added"] == 1
        assert result.data["diff_removed"] == 1

    def test_an_append_is_shown_as_added_lines(self, tools: ToolRegistry) -> None:
        result = tools.execute(
            "edit_file", {"path": SETTINGS, "operation": "append", "new_text": "EXTRA = 1\n"}
        )

        assert result.ok
        assert result.data["diff_added"] >= 1
        assert result.data["diff_removed"] == 0
        assert "+EXTRA = 1" in result.data["diff"]

    def test_the_pre_existing_keys_are_unchanged(self, tools: ToolRegistry) -> None:
        result = tools.execute(
            "edit_file",
            {"path": SETTINGS, "operation": "replace", "old_text": "8080", "new_text": "1"},
        )

        assert result.data["operation"] == "replace"
        assert result.data["sha256_before"] != result.data["sha256_after"]
        assert result.data["bytes_before"] > result.data["bytes_after"]


class TestTheModelSeesSomethingBounded:
    def test_the_observation_includes_the_diff(self, tools: ToolRegistry) -> None:
        result = tools.execute("write_file", {"path": README, "content": "# Replaced\n"})

        observation = result.to_observation()

        assert "diff" in observation
        assert "+# Replaced" in observation

    def test_a_huge_rewrite_does_not_blow_up_the_observation(
        self, tools: ToolRegistry, sample_project: Path
    ) -> None:
        target = sample_project / README
        target.write_text("".join(f"line {index}\n" for index in range(5000)), encoding="utf-8")

        result = tools.execute(
            "write_file",
            {"path": README, "content": "".join(f"new {index}\n" for index in range(5000))},
        )

        assert result.ok
        assert result.data["diff_truncated"] is True
        assert result.data["diff_added"] == 5000
        assert len(result.to_observation()) <= 9000


class TestDeleteHasNoDiff:
    def test_delete_reports_no_diff_keys(self, tools: ToolRegistry) -> None:
        # There is no "after" content to compare against, so inventing a diff
        # would mean inventing content.
        result = tools.execute("delete_file", {"path": README})

        assert result.ok
        assert "diff" not in result.data
        assert result.data["sha256"]


def _small_limit_registry(project: Path) -> ToolRegistry:
    """A registry whose tools refuse pre-images above a tiny limit."""

    from quantsmind_agent.workspace import Workspace as WorkspaceType

    workspace = WorkspaceType(project)
    registry = ToolRegistry(
        permissions=PermissionBroker(
            StaticPermissionProvider.for_v0_2(),
            approver=lambda request: ApprovalOutcome.ALLOW_ONCE,
        )
    )
    registry.register_all(build_mutating_tools(workspace, max_file_bytes=64))
    return registry
