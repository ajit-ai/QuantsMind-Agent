"""Tests for the affected-file report on every edit.

A cross-file change only stays correct if the agent notices the callers.
Reporting them in the edit result means it does not have to remember to ask, and
the report must be honest when it cannot be complete.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from quantsmind_agent.code_imports import importers_of
from quantsmind_agent.permissions import (
    ApprovalOutcome,
    PermissionBroker,
    StaticPermissionProvider,
)
from quantsmind_agent.revisions import RevisionJournal
from quantsmind_agent.tools import ToolRegistry
from quantsmind_agent.workspace import Workspace
from quantsmind_agent.write_tools import build_mutating_tools

GEOMETRY = "def distance(a, b):\n    return 0.0\n"
PLOTTER = "from geometry import distance\n\n\ndef go():\n    return distance((0, 0), (1, 1))\n"
ALIASED = "from geometry import distance as far\n\n\ndef go():\n    return far((0, 0), (1, 1))\n"
UNRELATED = "import os\n\n\ndef thing():\n    return os.getcwd()\n"


@pytest.fixture
def project(tmp_path: Path) -> Path:
    root = tmp_path / "affected"
    root.mkdir()
    (root / "geometry.py").write_text(GEOMETRY, encoding="utf-8")
    (root / "plotter.py").write_text(PLOTTER, encoding="utf-8")
    (root / "aliased.py").write_text(ALIASED, encoding="utf-8")
    (root / "unrelated.py").write_text(UNRELATED, encoding="utf-8")
    (root / "test_plotter.py").write_text(
        "from plotter import go\n\n\ndef test_go():\n    assert go() == 0.0\n", encoding="utf-8"
    )
    return root


@pytest.fixture
def tools(project: Path) -> ToolRegistry:
    registry = ToolRegistry(
        permissions=PermissionBroker(
            StaticPermissionProvider.for_v0_2(),
            approver=lambda request: ApprovalOutcome.ALLOW_ONCE,
        )
    )
    registry.register_all(build_mutating_tools(Workspace(project), journal=RevisionJournal()))
    return registry


class TestImportersOf:
    def test_finds_the_files_that_import_the_target(self, project: Path) -> None:
        importers, truncated = importers_of(Workspace(project), "geometry.py")

        assert {entry["path"] for entry in importers} == {"plotter.py", "aliased.py"}
        assert truncated is False

    def test_an_aliased_importer_counts(self, project: Path) -> None:
        importers, _ = importers_of(Workspace(project), "geometry.py")
        aliased = next(entry for entry in importers if entry["path"] == "aliased.py")

        assert aliased["bound_name"] == "far"
        assert aliased["imported_name"] == "distance"

    def test_the_file_itself_is_not_its_own_importer(self, project: Path) -> None:
        importers, _ = importers_of(Workspace(project), "geometry.py")

        assert "geometry.py" not in {entry["path"] for entry in importers}

    def test_a_leaf_module_has_no_importers(self, project: Path) -> None:
        importers, _ = importers_of(Workspace(project), "unrelated.py")

        assert importers == []

    def test_the_scan_is_bounded_and_says_so(self, project: Path) -> None:
        importers, truncated = importers_of(Workspace(project), "geometry.py", max_files=1)

        assert truncated is True
        assert len(importers) <= 2

    def test_an_unparsable_file_does_not_stop_the_scan(self, project: Path) -> None:
        (project / "broken.py").write_text("def oops(:\n", encoding="utf-8")

        importers, _ = importers_of(Workspace(project), "geometry.py")

        assert {entry["path"] for entry in importers} == {"plotter.py", "aliased.py"}


class TestEditResultsCarryAffectedFiles:
    def test_editing_a_module_reports_its_importers(self, tools: ToolRegistry) -> None:
        result = tools.execute(
            "edit_file",
            {
                "path": "geometry.py",
                "operation": "replace",
                "old_text": "return 0.0",
                "new_text": "return 1.0",
            },
        )

        assert result.ok
        assert {entry["path"] for entry in result.data["dependents"]} == {
            "plotter.py",
            "aliased.py",
        }
        assert result.data["dependents_truncated"] is False

    def test_editing_a_leaf_reports_no_importers(self, tools: ToolRegistry) -> None:
        result = tools.execute(
            "edit_file",
            {
                "path": "unrelated.py",
                "operation": "replace",
                "old_text": "os.getcwd()",
                "new_text": "os.getcwd()",
            },
        )

        assert result.data["dependents"] == []

    def test_writing_over_a_module_reports_its_importers(self, tools: ToolRegistry) -> None:
        result = tools.execute("write_file", {"path": "geometry.py", "content": GEOMETRY})

        assert {entry["path"] for entry in result.data["dependents"]} == {
            "plotter.py",
            "aliased.py",
        }

    def test_creating_a_file_skips_the_scan(self, tools: ToolRegistry) -> None:
        # Nothing could have been importing a file that did not exist.
        result = tools.execute("write_file", {"path": "brand_new.py", "content": "x = 1\n"})

        assert result.ok
        assert "dependents" not in result.data

    def test_the_existing_keys_are_untouched(self, tools: ToolRegistry) -> None:
        result = tools.execute(
            "edit_file",
            {"path": "geometry.py", "operation": "replace", "old_text": "0.0", "new_text": "1.0"},
        )

        assert result.data["path"] == "geometry.py"
        assert result.data["diff_kind"] == "modified"
        assert result.data["undoable"] is True
        assert result.data["sha256_before"] != result.data["sha256_after"]

    def test_the_report_does_not_change_what_the_edit_does(
        self, tools: ToolRegistry, project: Path
    ) -> None:
        # The scan is read-only: the file keeps exactly the edited bytes.
        result = tools.execute(
            "edit_file",
            {"path": "geometry.py", "operation": "replace", "old_text": "0.0", "new_text": "1.0"},
        )

        assert result.ok
        assert (project / "geometry.py").read_text(encoding="utf-8") == GEOMETRY.replace(
            "0.0", "1.0"
        )
