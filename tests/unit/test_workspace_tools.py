"""Unit tests for the v0.1 workspace tool set."""

from __future__ import annotations

import pytest

from quantsmind_agent.runtime.tools import ToolRegistry
from quantsmind_agent.security.permissions import (
    Permission,
    PermissionBroker,
    StaticPermissionProvider,
)
from quantsmind_agent.services.workspace.containment import Workspace
from quantsmind_agent.workspace_tools import build_default_tools


@pytest.fixture
def registry(workspace: Workspace) -> ToolRegistry:
    reg = ToolRegistry(permissions=PermissionBroker(StaticPermissionProvider.for_v0_1()))
    reg.register_all(build_default_tools(workspace))
    return reg


class TestToolSetShape:
    def test_tool_set_is_the_v0_1_set(self, registry: ToolRegistry) -> None:
        assert registry.names == [
            "list_directory",
            "project_structure",
            "read_file",
            "search_content",
        ]

    def test_every_tool_declares_a_read_permission(self, registry: ToolRegistry) -> None:
        for spec in registry.specs():
            assert spec.permission is Permission.READ_WORKSPACE
            assert spec.read_only

    def test_every_tool_has_a_json_schema(self, registry: ToolRegistry) -> None:
        for schema in registry.openai_schemas():
            function = schema["function"]
            assert function["description"].strip()
            assert function["parameters"]["type"] == "object"

    def test_every_tool_declares_workspace_relative_paths(self, registry: ToolRegistry) -> None:
        for schema in registry.openai_schemas():
            for prop in schema["function"]["parameters"].get("properties", {}).values():
                assert "absolute" not in prop.get("description", "").lower() or "relative" in (
                    prop.get("description", "").lower()
                )


class TestListDirectory:
    def test_root(self, registry: ToolRegistry) -> None:
        result = registry.execute("list_directory", {"path": "."})
        assert result.ok
        names = {entry["name"] for entry in result.data["entries"]}
        assert {"src", "docs", "README.md"} <= names
        assert "node_modules" not in names
        assert ".git" not in names

    def test_subfolder(self, registry: ToolRegistry) -> None:
        result = registry.execute("list_directory", {"path": "src/app"})
        assert result.ok
        assert "main.py" in {entry["name"] for entry in result.data["entries"]}

    def test_rejects_escape(self, registry: ToolRegistry) -> None:
        result = registry.execute("list_directory", {"path": "../.."})
        assert not result.ok
        assert "outside the current workspace" in (result.error or "")

    def test_requires_path_argument(self, registry: ToolRegistry) -> None:
        result = registry.execute("list_directory", {})
        assert not result.ok

    def test_missing_path_reports_a_message(self, registry: ToolRegistry) -> None:
        result = registry.execute("list_directory", {"path": "nope"})
        assert not result.ok
        assert "nope" in (result.error or "")

    def test_max_entries_truncates(self, registry: ToolRegistry) -> None:
        result = registry.execute("list_directory", {"path": ".", "max_entries": 1})
        assert result.ok
        assert result.data["truncated"] is True
        assert len(result.data["entries"]) == 1


class TestReadFile:
    def test_reads_with_line_numbers(self, registry: ToolRegistry) -> None:
        result = registry.execute("read_file", {"path": "README.md"})
        assert result.ok
        assert "Sample Project" in result.data["content"]
        assert result.data["content"].lstrip().startswith("1")

    def test_line_window(self, registry: ToolRegistry) -> None:
        full = registry.execute("read_file", {"path": "src/app/main.py"}).data
        window = registry.execute(
            "read_file", {"path": "src/app/main.py", "start_line": 1, "max_lines": 2}
        ).data
        assert window["returned_lines"] == 2
        assert window["total_lines"] == full["total_lines"]

    def test_missing_file(self, registry: ToolRegistry) -> None:
        result = registry.execute("read_file", {"path": "nope.py"})
        assert not result.ok
        assert "does not exist" in (result.error or "")

    def test_empty_path(self, registry: ToolRegistry) -> None:
        result = registry.execute("read_file", {"path": ""})
        assert not result.ok

    def test_directory_is_not_readable_as_text(self, registry: ToolRegistry) -> None:
        result = registry.execute("read_file", {"path": "src"})
        assert not result.ok

    def test_binary_is_refused(self, registry: ToolRegistry) -> None:
        result = registry.execute("read_file", {"path": "logo.png"})
        assert not result.ok
        assert "binary" in (result.error or "")

    def test_escape_is_refused(self, registry: ToolRegistry) -> None:
        result = registry.execute("read_file", {"path": "../outside_secret.txt"})
        assert not result.ok
        assert "outside" in (result.error or "")


class TestSearchContent:
    def test_literal_match(self, registry: ToolRegistry) -> None:
        result = registry.execute("search_content", {"pattern": "DEFAULT_PORT"})
        assert result.ok
        matches = result.data["matches"]
        assert matches
        assert matches[0]["path"] == "src/app/settings.py"
        assert "DEFAULT_PORT" in matches[0]["text"]

    def test_case_insensitive_by_default(self, registry: ToolRegistry) -> None:
        result = registry.execute("search_content", {"pattern": "default_port"})
        assert result.data["match_count"] >= 1

    def test_case_sensitive(self, registry: ToolRegistry) -> None:
        result = registry.execute(
            "search_content", {"pattern": "default_port", "ignore_case": False}
        )
        assert result.data["match_count"] == 0

    def test_regex(self, registry: ToolRegistry) -> None:
        result = registry.execute(
            "search_content", {"pattern": r"def \w+_settings", "use_regex": True}
        )
        assert result.data["match_count"] >= 1

    def test_invalid_regex_is_reported(self, registry: ToolRegistry) -> None:
        result = registry.execute("search_content", {"pattern": "(unclosed", "use_regex": True})
        assert not result.ok
        assert "not valid" in (result.error or "")

    def test_file_pattern_filter(self, registry: ToolRegistry) -> None:
        result = registry.execute("search_content", {"pattern": "Sample", "file_pattern": "*.md"})
        assert result.data["match_count"] >= 1
        assert all(match["path"].endswith(".md") for match in result.data["matches"])

    def test_no_match_is_a_success(self, registry: ToolRegistry) -> None:
        result = registry.execute("search_content", {"pattern": "zzz-definitely-not-present-zzz"})
        assert result.ok
        assert result.data["match_count"] == 0

    def test_empty_pattern(self, registry: ToolRegistry) -> None:
        assert not registry.execute("search_content", {"pattern": ""}).ok

    def test_binary_files_are_skipped(self, registry: ToolRegistry) -> None:
        result = registry.execute("search_content", {"pattern": "PNG"})
        assert result.ok
        assert all(not match["path"].endswith(".png") for match in result.data["matches"])

    def test_max_results(self, registry: ToolRegistry) -> None:
        result = registry.execute("search_content", {"pattern": "e", "max_results": 1})
        assert result.data["truncated"] is True
        assert result.data["match_count"] <= 1


class TestProjectStructure:
    def test_tree_contains_files(self, registry: ToolRegistry) -> None:
        result = registry.execute("project_structure", {})
        assert result.ok
        tree = result.data["tree"]
        assert "src/" in tree
        assert "main.py" in tree
        assert "README.md" in tree

    def test_ignored_directories_are_absent(self, registry: ToolRegistry) -> None:
        tree = registry.execute("project_structure", {}).data["tree"]
        assert "node_modules" not in tree
        assert ".git" not in tree

    def test_no_arguments_is_valid(self, registry: ToolRegistry) -> None:
        assert registry.execute("project_structure", {}).ok

    def test_max_depth_limits_output(self, registry: ToolRegistry) -> None:
        shallow = registry.execute("project_structure", {"max_depth": 1}).data["tree"]
        deep = registry.execute("project_structure", {"max_depth": 5}).data["tree"]
        assert len(shallow) < len(deep)

    def test_subdirectory(self, registry: ToolRegistry) -> None:
        result = registry.execute("project_structure", {"path": "src"})
        assert result.ok
        assert "app/" in result.data["tree"]

    def test_invalid_path(self, registry: ToolRegistry) -> None:
        result = registry.execute("project_structure", {"path": "missing-dir"})
        assert not result.ok

    def test_file_path_is_rejected(self, registry: ToolRegistry) -> None:
        result = registry.execute("project_structure", {"path": "README.md"})
        assert not result.ok
        assert "not a directory" in (result.error or "")
