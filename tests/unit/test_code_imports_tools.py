"""Tests for import-aware references and one-level expansion.

The question these answer is the one that matters before editing an interface:
which files import this symbol, and what else do they pull from the same module?
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from quantsmind_agent.code_tools import build_code_tools
from quantsmind_agent.permissions import (
    ApprovalOutcome,
    PermissionBroker,
    StaticPermissionProvider,
)
from quantsmind_agent.tools import ToolRegistry
from quantsmind_agent.workspace import Workspace

STORE = '''"""Storage."""


class Store:
    """A store."""

    def save(self, payload):
        """Write."""
        return len(payload)

    def load(self, key):
        """Read."""
        return None


def helper():
    """Not related to Store."""
    return 1
'''

READER = '''"""Reader."""

from pkg.store import Store, helper


def use(root):
    return Store(root).save({})
'''

ALIASED = '''"""Aliasing import."""

from pkg.store import Store as Repository


def build(root):
    return Repository(root)
'''

RELATIVE = '''"""Relative import."""

from .store import Store


def use(root):
    return Store(root)
'''

UNRELATED = '''"""Imports something else."""

import os


def other():
    return os.getcwd()
'''


@pytest.fixture
def project(tmp_path: Path) -> Path:
    root = tmp_path / "project"
    (root / "pkg").mkdir(parents=True)
    (root / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (root / "pkg" / "store.py").write_text(STORE, encoding="utf-8")
    (root / "pkg" / "reader.py").write_text(READER, encoding="utf-8")
    (root / "pkg" / "aliased.py").write_text(ALIASED, encoding="utf-8")
    (root / "pkg" / "relative_user.py").write_text(RELATIVE, encoding="utf-8")
    (root / "pkg" / "unrelated.py").write_text(UNRELATED, encoding="utf-8")
    (root / "other_store.py").write_text("class Store:\n    pass\n", encoding="utf-8")
    return root


@pytest.fixture
def tools(project: Path) -> ToolRegistry:
    registry = ToolRegistry(
        permissions=PermissionBroker(
            StaticPermissionProvider.for_v0_2(),
            approver=lambda request: ApprovalOutcome.ALLOW_ONCE,
        )
    )
    registry.register_all(build_code_tools(Workspace(project)))
    return registry


def _refs(tools: ToolRegistry, symbol: str, **extra: Any) -> dict[str, Any]:
    result = tools.execute("find_references", {"symbol": symbol, **extra})
    assert result.ok, result.error
    return result.data


class TestDefinitionsAndImports:
    def test_the_defining_file_is_reported(self, tools: ToolRegistry) -> None:
        data = _refs(tools, "Store")

        assert {(d["path"], d["qualified"]) for d in data["definitions"]} == {
            ("pkg/store.py", "Store"),
            ("other_store.py", "Store"),
        }

    def test_a_dependent_file_is_discovered_through_its_import(self, tools: ToolRegistry) -> None:
        data = _refs(tools, "Store")

        assert "pkg/reader.py" in data["dependent_files"]
        assert "pkg/aliased.py" in data["dependent_files"]
        assert "pkg/relative_user.py" in data["dependent_files"]
        assert "pkg/unrelated.py" not in data["dependent_files"]

    def test_an_import_site_is_marked_as_an_import_not_a_use(self, tools: ToolRegistry) -> None:
        data = _refs(tools, "Store")
        sites = {(r["path"], r["line"]): r["kind"] for r in data["references"]}

        assert sites[("pkg/reader.py", 3)] == "import"
        assert sites[("pkg/reader.py", 7)] == "use"

    def test_an_alias_still_counts_as_a_dependency(self, tools: ToolRegistry) -> None:
        data = _refs(tools, "Store")

        # ``Store as Repository`` binds Repository, so the reference search on the
        # name alone would miss it; the import resolution does not.
        assert "pkg/aliased.py" in data["dependent_files"]

    def test_a_relative_import_is_resolved(self, tools: ToolRegistry) -> None:
        data = _refs(tools, "Store")

        assert "pkg/relative_user.py" in data["dependent_files"]

    def test_two_definitions_are_reported_as_ambiguous(self, tools: ToolRegistry) -> None:
        data = _refs(tools, "Store")

        assert data["ambiguous"] is True

    def test_a_single_definition_is_not_ambiguous(self, tools: ToolRegistry) -> None:
        data = _refs(tools, "save")

        assert data["definition_count"] == 1
        assert data["ambiguous"] is False

    def test_the_resolution_basis_is_stated(self, tools: ToolRegistry) -> None:
        # The model must not read this as type resolution.
        data = _refs(tools, "Store")

        assert "no type inference" in data["resolution"]


class TestExpansion:
    def test_one_level_of_import_expansion_finds_the_sibling_import(
        self, tools: ToolRegistry
    ) -> None:
        # reader.py imports Store *and* helper from store.py, so changing the
        # module's surface reaches both.
        data = _refs(tools, "Store")

        related = {(r["importer"], r["imported_name"]) for r in data["related"]}
        assert ("pkg/reader.py", "Store") in related
        assert ("pkg/reader.py", "helper") in related

    def test_expansion_can_be_switched_off(self, tools: ToolRegistry) -> None:
        data = _refs(tools, "Store", expand=False)

        assert data["expanded"] is False
        assert data["related"] == []
        assert data["dependent_files"], "the dependency list is still reported"

    def test_expansion_does_not_leave_the_dependent_files(self, tools: ToolRegistry) -> None:
        # One level means one level: unrelated.py imports os and must not appear.
        data = _refs(tools, "Store")

        importers = {r["importer"] for r in data["related"]}
        assert importers <= set(data["dependent_files"])

    def test_expansion_names_the_source_module(self, tools: ToolRegistry) -> None:
        data = _refs(tools, "Store")

        entry = next(r for r in data["related"] if r["importer"] == "pkg/reader.py")
        assert entry["source_module"] == "pkg/store.py"
        assert entry["bound_name"] == "Store"


class TestHonestFailure:
    def test_unresolvable_imports_are_counted_not_guessed(
        self, tools: ToolRegistry, project: Path
    ) -> None:
        (project / "pkg" / "third_party_user.py").write_text(
            "from some_external_lib import Widget\n\n\ndef build():\n    return Widget()\n",
            encoding="utf-8",
        )

        data = _refs(tools, "Store")

        assert data["unresolved_imports"] >= 1

    def test_an_unparsable_file_is_listed_and_the_search_continues(
        self, tools: ToolRegistry, project: Path
    ) -> None:
        (project / "pkg" / "broken.py").write_text("def oops(:\n", encoding="utf-8")

        data = _refs(tools, "Store")

        assert "pkg/broken.py" in data["unparsable_files"]
        assert data["definition_count"] == 2, "the readable files were still searched"

    def test_scanned_and_skipped_counts_are_reported(self, tools: ToolRegistry) -> None:
        data = _refs(tools, "Store")

        assert data["files_scanned"] >= 5
        assert "files_skipped" in data
        assert "files_scanned" in data

    def test_the_result_is_still_bounded(self, tools: ToolRegistry, project: Path) -> None:
        for index in range(30):
            (project / f"pkg/user{index}.py").write_text(
                f"from pkg.store import Store\n\n\ndef use{index}():\n    return Store()\n",
                encoding="utf-8",
            )

        data = _refs(tools, "Store", max_results=5)

        assert len(data["references"]) <= 5
        assert data["truncated"] is True

    def test_an_empty_symbol_still_returns_an_answer(self, tools: ToolRegistry) -> None:
        data = _refs(tools, "nothing_anywhere")

        assert data["definitions"] == []
        assert data["dependent_files"] == []
