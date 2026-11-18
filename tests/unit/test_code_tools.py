"""Tests for the v0.3 code-navigation tools.

What matters is not that the parser works -- that is covered in
``test_code_index`` -- but that the tools behave safely inside a workspace:
bounded, read-only, honest about what they could not read, and useful enough to
aim an edit at.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from quantsmind_agent.code_tools import build_code_tools
from quantsmind_agent.runtime.tools import ToolRegistry
from quantsmind_agent.security.permissions import (
    ApprovalOutcome,
    PermissionBroker,
    StaticPermissionProvider,
)
from quantsmind_agent.services.workspace.containment import Workspace
from quantsmind_agent.write_tools import build_mutating_tools

SAMPLE = '''"""Sample module."""

import json


def load(path):
    """Load a document."""
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


class Store:
    """A store."""

    def __init__(self, root):
        self.root = root

    def save(self, payload):
        """Write a payload."""
        return load(self.root)
'''

READER = "from pkg.store import Store\n\n\ndef use(root):\n    return Store(root).save({})\n"


@pytest.fixture
def project(tmp_path: Path) -> Path:
    root = tmp_path / "project"
    (root / "pkg").mkdir(parents=True)
    (root / "pkg" / "store.py").write_text(SAMPLE, encoding="utf-8")
    (root / "pkg" / "reader.py").write_text(READER, encoding="utf-8")
    (root / "README.md").write_text("# Project\n", encoding="utf-8")
    return root


def _registry(project: Path) -> ToolRegistry:
    registry = ToolRegistry(
        permissions=PermissionBroker(
            StaticPermissionProvider.for_v0_2(),
            approver=lambda request: ApprovalOutcome.ALLOW_ONCE,
        )
    )
    registry.register_all(build_code_tools(Workspace(project)))
    return registry


@pytest.fixture
def tools(project: Path) -> ToolRegistry:
    return _registry(project)


class TestCodeOutline:
    def test_lists_the_definitions_with_line_ranges(
        self, tools: ToolRegistry, project: Path
    ) -> None:
        result = tools.execute("code_outline", {"path": "pkg/store.py"})

        assert result.ok
        names = {symbol["qualified"] for symbol in result.data["symbols"]}
        assert names == {"load", "Store", "Store.__init__", "Store.save"}
        assert result.data["symbol_count"] == 4

    def test_the_rendered_outline_is_readable(self, tools: ToolRegistry) -> None:
        result = tools.execute("code_outline", {"path": "pkg/store.py"})

        rendered = result.data["outline"]
        assert "class Store  [12-20]" in rendered
        assert "def load(path)" in rendered
        assert "# Load a document." in rendered

    def test_line_ranges_match_the_file(self, tools: ToolRegistry, project: Path) -> None:
        result = tools.execute("code_outline", {"path": "pkg/store.py"})
        lines = (project / "pkg" / "store.py").read_text(encoding="utf-8").splitlines()
        save = next(s for s in result.data["symbols"] if s["qualified"] == "Store.save")

        assert lines[save["line"] - 1].strip().startswith("def save")
        assert lines[save["end_line"] - 1].strip() == "return load(self.root)"

    def test_an_outline_drives_a_precise_edit(self, tools: ToolRegistry, project: Path) -> None:
        # The point of line ranges: the model can replace exactly one line of a
        # function without reading the module.
        registry = _registry(project)
        registry.register_all(build_mutating_tools(Workspace(project)))
        outline = registry.execute("code_outline", {"path": "pkg/store.py"})
        load = next(s for s in outline.data["symbols"] if s["qualified"] == "load")

        result = registry.execute(
            "edit_file",
            {
                "path": "pkg/store.py",
                "operation": "replace_lines",
                "start_line": load["line"],
                "end_line": load["line"],
                "new_text": "def load(path):",
            },
        )

        assert result.ok
        assert "def load(path):" in (project / "pkg" / "store.py").read_text(encoding="utf-8")

    def test_a_non_python_file_is_refused_with_a_pointer(self, tools: ToolRegistry) -> None:
        result = tools.execute("code_outline", {"path": "README.md"})

        assert not result.ok
        assert "not a .py file" in (result.error or "")
        assert "read_file" in (result.error or "")

    def test_a_syntax_error_is_reported_not_raised(
        self, tools: ToolRegistry, project: Path
    ) -> None:
        (project / "pkg" / "broken.py").write_text("def oops(:\n", encoding="utf-8")

        result = tools.execute("code_outline", {"path": "pkg/broken.py"})

        assert not result.ok
        assert "not valid Python" in (result.error or "")
        assert "line 1" in (result.error or "")

    def test_a_missing_file_is_refused(self, tools: ToolRegistry) -> None:
        assert not tools.execute("code_outline", {"path": "pkg/nope.py"}).ok

    def test_no_path_is_refused(self, tools: ToolRegistry) -> None:
        assert not tools.execute("code_outline", {}).ok

    @pytest.mark.parametrize(
        "bad_path", ["../escape.py", "C:/Windows/evil.py", "//host/share/x.py", "a\x00b.py"]
    )
    def test_escape_routes_are_refused(self, tools: ToolRegistry, bad_path: str) -> None:
        assert not tools.execute("code_outline", {"path": bad_path}).ok

    def test_an_empty_file_outlines_to_nothing(self, tools: ToolRegistry, project: Path) -> None:
        (project / "pkg" / "empty.py").write_text("", encoding="utf-8")

        result = tools.execute("code_outline", {"path": "pkg/empty.py"})

        assert result.ok
        assert result.data["symbol_count"] == 0
        assert result.data["outline"] == ""


class TestFindReferences:
    def test_finds_the_definition_and_its_call_sites(self, tools: ToolRegistry) -> None:
        result = tools.execute("find_references", {"symbol": "Store"})

        assert result.ok
        defined = {(d["path"], d["qualified"]) for d in result.data["definitions"]}
        assert defined == {("pkg/store.py", "Store")}
        uses = {(r["path"], r["line"]) for r in result.data["references"]}
        # The import line is where the name enters the other module, and the
        # constructor call inside ``use()`` is the real call site.
        assert ("pkg/reader.py", 1) in uses
        assert ("pkg/reader.py", 5) in uses

    def test_a_definition_line_is_not_reported_as_a_use(self, tools: ToolRegistry) -> None:
        result = tools.execute("find_references", {"symbol": "load"})

        lines = {(r["path"], r["line"]) for r in result.data["references"]}
        assert ("pkg/store.py", 6) not in lines, "the def line is a definition, not a use"
        assert ("pkg/store.py", 20) in lines, "the call inside save() is a use"
        # ``json.load`` shares the name, and name matching deliberately
        # over-reports rather than risk a missed call site.
        assert ("pkg/store.py", 9) in lines

    def test_a_method_is_found_by_name(self, tools: ToolRegistry) -> None:
        result = tools.execute("find_references", {"symbol": "save"})

        assert result.ok
        assert result.data["definition_count"] == 1
        assert result.data["definitions"][0]["kind"] == "method"

    def test_non_python_files_are_not_scanned(self, tools: ToolRegistry) -> None:
        result = tools.execute("find_references", {"symbol": "Project"})

        assert result.ok
        assert result.data["definition_count"] == 0
        assert result.data["files_scanned"] == 2

    def test_an_unknown_symbol_is_an_empty_answer_not_an_error(self, tools: ToolRegistry) -> None:
        result = tools.execute("find_references", {"symbol": "nothing_here"})

        assert result.ok
        assert result.data["definitions"] == []
        assert result.data["references"] == []

    def test_results_are_bounded_and_say_so(self, tools: ToolRegistry, project: Path) -> None:
        for index in range(10):
            (project / f"pkg/many{index}.py").write_text(
                f"from pkg.store import Store\nvalue{index} = Store\n", encoding="utf-8"
            )

        result = tools.execute("find_references", {"symbol": "Store", "max_results": 3})

        assert result.ok
        assert len(result.data["references"]) <= 3
        assert result.data["truncated"] is True

    def test_a_search_can_be_scoped_to_a_directory(self, tools: ToolRegistry) -> None:
        result = tools.execute("find_references", {"symbol": "Store", "path": "pkg"})

        assert result.ok
        assert {d["path"] for d in result.data["definitions"]} == {"pkg/store.py"}

    def test_a_scope_that_does_not_exist_is_refused(self, tools: ToolRegistry) -> None:
        assert not tools.execute("find_references", {"symbol": "Store", "path": "nope"}).ok

    def test_no_symbol_is_refused(self, tools: ToolRegistry) -> None:
        assert not tools.execute("find_references", {"symbol": "  "}).ok

    @pytest.mark.parametrize("bad_path", ["../escape.py", "C:/Windows/evil.py", "//host/share"])
    def test_escape_routes_are_refused(self, tools: ToolRegistry, bad_path: str) -> None:
        assert not tools.execute("find_references", {"symbol": "Store", "path": bad_path}).ok

    def test_an_unparsable_file_is_counted_not_fatal(
        self, tools: ToolRegistry, project: Path
    ) -> None:
        (project / "pkg" / "broken.py").write_text("def oops(:\n", encoding="utf-8")

        result = tools.execute("find_references", {"symbol": "Store"})

        assert result.ok
        assert "pkg/broken.py" in result.data["unparsable_files"]
        assert result.data["definition_count"] == 1


class TestProjectSymbols:
    def test_maps_every_definition_in_the_project(self, tools: ToolRegistry) -> None:
        result = tools.execute("project_symbols", {})

        assert result.ok
        assert result.data["files_scanned"] == 2
        found = {(s["path"], s["qualified"]) for s in result.data["symbols"]}
        assert found == {
            ("pkg/store.py", "load"),
            ("pkg/store.py", "Store"),
            ("pkg/store.py", "Store.__init__"),
            ("pkg/store.py", "Store.save"),
            ("pkg/reader.py", "use"),
        }

    def test_a_name_query_narrows_the_result(self, tools: ToolRegistry) -> None:
        result = tools.execute("project_symbols", {"query": "load"})

        assert result.ok
        assert [s["qualified"] for s in result.data["symbols"]] == ["load"]

    def test_a_query_matches_the_qualified_name(self, tools: ToolRegistry) -> None:
        result = tools.execute("project_symbols", {"query": "store"})

        found = {s["qualified"] for s in result.data["symbols"]}
        assert "Store.save" in found
        assert "load" not in found

    def test_a_kind_filter_selects_constructs(self, tools: ToolRegistry) -> None:
        result = tools.execute("project_symbols", {"kind": "class"})

        assert result.ok
        assert {s["qualified"] for s in result.data["symbols"]} == {"Store"}

    def test_a_scope_can_be_narrowed_to_a_directory(self, tools: ToolRegistry) -> None:
        result = tools.execute("project_symbols", {"path": "pkg", "kind": "method"})

        assert result.ok
        assert {s["qualified"] for s in result.data["symbols"]} == {
            "Store.__init__",
            "Store.save",
        }

    def test_the_summary_says_what_was_matched(self, tools: ToolRegistry) -> None:
        result = tools.execute("project_symbols", {"query": "load"})

        assert "matching 'load'" in result.summary
        assert "1 symbols" in result.summary

    def test_results_are_bounded_and_say_so(self, tools: ToolRegistry, project: Path) -> None:
        for index in range(5):
            (project / f"pkg/extra{index}.py").write_text(
                f"def helper{index}():\n    return {index}\n", encoding="utf-8"
            )

        result = tools.execute("project_symbols", {"max_results": 2})

        assert result.ok
        assert result.data["returned"] == 2
        assert result.data["truncated"] is True
        assert result.data["symbol_count"] > 2

    def test_non_python_files_are_not_indexed(self, tools: ToolRegistry) -> None:
        result = tools.execute("project_symbols", {"query": "project"})

        assert result.ok
        assert result.data["symbols"] == []

    def test_an_unparsable_file_is_counted_not_fatal(
        self, tools: ToolRegistry, project: Path
    ) -> None:
        (project / "pkg" / "broken.py").write_text("def oops(:\n", encoding="utf-8")

        result = tools.execute("project_symbols", {})

        assert result.ok
        assert "pkg/broken.py" in result.data["unparsable_files"]
        assert result.data["files_scanned"] == 3

    def test_a_missing_scope_is_refused(self, tools: ToolRegistry) -> None:
        assert not tools.execute("project_symbols", {"path": "nope"}).ok

    @pytest.mark.parametrize("bad_path", ["../escape.py", "C:/Windows/evil.py", "//host/share"])
    def test_escape_routes_are_refused(self, tools: ToolRegistry, bad_path: str) -> None:
        assert not tools.execute("project_symbols", {"path": bad_path}).ok

    def test_no_arguments_indexes_the_whole_project(self, tools: ToolRegistry) -> None:
        assert tools.execute("project_symbols", {}).ok


class TestReadOnly:
    def test_every_navigation_tool_requires_only_read_permission(self, tools: ToolRegistry) -> None:
        for name in ("code_outline", "find_references", "project_symbols"):
            spec = tools.get(name).spec
            assert spec.permission.value == "READ_WORKSPACE", name
            assert spec.read_only is True, name
