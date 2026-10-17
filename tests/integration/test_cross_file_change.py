"""A cross-file change, end to end, through the real agent loop.

The scenario is the one that breaks naive agents: a function's interface changes,
so every caller has to change with it. The agent has to find the interface, find
the callers, change all of them, and confirm with the test suite -- and when one
edit is denied, it must say so rather than report the change as complete.

Everything here drives the real application, registry, broker and command tool.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

from quantsmind_agent.app import ApplicationContext
from quantsmind_agent.config import ConfigManager
from quantsmind_agent.permissions import ApprovalOutcome, PermissionRequest
from quantsmind_agent.secrets import FileSecretStore
from tests.fakes import FakeModelProvider, text_response, tool_response

pytestmark = pytest.mark.integration

GEOMETRY = '''"""Geometry helpers."""


def distance(ax, ay, bx, by):
    """Distance between two points."""
    return ((ax - bx) ** 2 + (ay - by) ** 2) ** 0.5
'''

PLOTTER = '''"""Plotting."""


def path_length(points):
    """Length of a path through the points."""
    from geometry import distance

    total = 0.0
    for first, second in zip(points, points[1:]):
        total += distance(first[0], first[1], second[0], second[1])
    return total
'''

TEST_GEOMETRY = """from geometry import distance


def test_distance():
    assert distance(0, 0, 3, 4) == 5.0
"""

TEST_PLOTTER = """from plotter import path_length


def test_path_length():
    assert path_length([(0, 0), (3, 4)]) == 5.0
"""

SIGNATURE = "def distance(ax, ay, bx, by):"
NEW_SIGNATURE = "def distance(a, b):"
BODY = "    return ((ax - bx) ** 2 + (ay - by) ** 2) ** 0.5"
NEW_BODY = "    return ((a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2) ** 0.5"
CALL_SITE = "        total += distance(first[0], first[1], second[0], second[1])"
NEW_CALL_SITE = "        total += distance(first, second)"
TEST_CALL = "    assert distance(0, 0, 3, 4) == 5.0"
NEW_TEST_CALL = "    assert distance((0, 0), (3, 4)) == 5.0"


@pytest.fixture
def project(tmp_path: Path) -> Path:
    root = tmp_path / "cross_file"
    root.mkdir()
    (root / "geometry.py").write_text(GEOMETRY, encoding="utf-8")
    (root / "plotter.py").write_text(PLOTTER, encoding="utf-8")
    (root / "test_geometry.py").write_text(TEST_GEOMETRY, encoding="utf-8")
    (root / "test_plotter.py").write_text(TEST_PLOTTER, encoding="utf-8")
    return root


def _context(
    app_paths: Any,
    project: Path,
    provider: FakeModelProvider,
    *,
    approver: Any = None,
) -> ApplicationContext:
    manager = ConfigManager(app_paths, secret_store=FileSecretStore(app_paths.secrets_file))
    context = ApplicationContext.create(paths=app_paths, config_manager=manager)
    context.open_workspace(project)
    context.agent._gateway.set_provider(provider)
    context.tools.permissions.set_approver(approver or (lambda request: ApprovalOutcome.ALLOW_ONCE))
    return context


def _run_tests() -> dict[str, Any]:
    return {
        "command": sys.executable,
        "arguments": ["-m", "pytest", "-q", "--no-header", "-p", "no:cacheprovider"],
    }


def _edit(path: str, old: str, new: str) -> dict[str, Any]:
    return {
        "path": path,
        "operation": "replace",
        "old_text": old,
        "new_text": new,
    }


class TestScopingTheChange:
    def test_references_identifies_every_caller_including_the_tests(
        self, app_paths: Any, project: Path
    ) -> None:
        provider = FakeModelProvider(
            [
                tool_response([("find_references", {"symbol": "distance"})]),
                text_response("distance() lives in geometry.py and has two callers."),
            ]
        )
        context = _context(app_paths, project, provider)

        context.agent.run("Who calls distance()?")

        found = next(r for r in context.session.tool_results if r.tool == "find_references")
        assert found.ok
        assert {d["path"] for d in found.data["definitions"]} == {"geometry.py"}
        # The tests import it too, which is exactly the file set a naive agent
        # forgets when it changes an interface.
        assert set(found.data["dependent_files"]) == {"plotter.py", "test_geometry.py"}

        kinds = {(r["path"], r["line"]): r["kind"] for r in found.data["references"]}
        assert kinds[("plotter.py", 6)] == "import"
        assert kinds[("plotter.py", 10)] == "use"

    def test_an_aliased_import_is_still_found_as_a_dependency(
        self, app_paths: Any, project: Path
    ) -> None:
        # Name matching alone would miss this; resolving the import does not.
        (project / "alias_user.py").write_text(
            "from geometry import distance as far\n\n\ndef go():\n    return far((0, 0), (1, 1))\n",
            encoding="utf-8",
        )
        provider = FakeModelProvider(
            [
                tool_response([("find_references", {"symbol": "distance"})]),
                text_response("Three files depend on it."),
            ]
        )
        context = _context(app_paths, project, provider)

        context.agent.run("Where is distance used?")

        found = next(r for r in context.session.tool_results if r.tool == "find_references")
        assert "alias_user.py" in found.data["dependent_files"]

    def test_expansion_shows_what_the_dependent_file_also_pulls_in(
        self, app_paths: Any, project: Path
    ) -> None:
        provider = FakeModelProvider(
            [
                tool_response([("find_references", {"symbol": "path_length"})]),
                text_response("path_length is used by its test."),
            ]
        )
        context = _context(app_paths, project, provider)

        context.agent.run("Where is path_length used?")

        found = next(r for r in context.session.tool_results if r.tool == "find_references")
        assert found.data["dependent_files"] == ["test_plotter.py"]
        related = {(r["importer"], r["imported_name"]) for r in found.data["related"]}
        assert ("test_plotter.py", "path_length") in related


class TestTheCrossFileChange:
    def test_the_interface_and_all_its_callers_are_changed_and_validated(
        self, app_paths: Any, project: Path
    ) -> None:
        # distance() switches from four coordinates to two points: a real
        # interface break. plotter.py and test_geometry.py both have to change,
        # and the suite proves the result. This is the cross-file capability.
        provider = FakeModelProvider(
            [
                tool_response([("find_references", {"symbol": "distance"})]),
                tool_response([("code_outline", {"path": "geometry.py"})]),
                tool_response([("edit_file", _edit("geometry.py", SIGNATURE, NEW_SIGNATURE))]),
                tool_response([("edit_file", _edit("geometry.py", BODY, NEW_BODY))]),
                tool_response([("edit_file", _edit("plotter.py", CALL_SITE, NEW_CALL_SITE))]),
                tool_response([("edit_file", _edit("test_geometry.py", TEST_CALL, NEW_TEST_CALL))]),
                tool_response([("run_command", _run_tests())]),
                text_response("distance() now takes two points; every caller updated."),
            ]
        )
        context = _context(app_paths, project, provider)

        result = context.agent.run("Make distance() take two points instead of four numbers.")

        assert result.error is None
        assert NEW_SIGNATURE in (project / "geometry.py").read_text(encoding="utf-8")
        assert "total += distance(first, second)" in (project / "plotter.py").read_text(
            encoding="utf-8"
        )
        assert NEW_TEST_CALL in (project / "test_geometry.py").read_text(encoding="utf-8")

        # Four edits across three files, each with a diff the user can inspect.
        edits = [r for r in context.session.tool_results if r.tool == "edit_file"]
        assert len(edits) == 4
        assert all(edit.ok for edit in edits)
        assert all(edit.data["diff_kind"] == "modified" for edit in edits)
        assert all(edit.data["diff_truncated"] is False for edit in edits)
        assert {edit.data["path"] for edit in edits} == {
            "geometry.py",
            "plotter.py",
            "test_geometry.py",
        }

        # Each file is independently undoable; geometry.py took two edits, so its stack
        # is two deep and unwinds one step at a time.
        assert context.session.revisions.depth("geometry.py") == 2
        assert context.session.revisions.depth("plotter.py") == 1
        assert context.session.revisions.depth("test_geometry.py") == 1

        tests = next(r for r in context.session.tool_results if r.tool == "run_command")
        assert tests.data["exit_code"] == 0, "the suite validates the whole change"

    def test_a_failing_caller_surfaces_in_the_diagnostics_and_gets_fixed(
        self, app_paths: Any, project: Path
    ) -> None:
        # Change the interface without the callers, run the tests, read the
        # failure, fix the callers: the loop has to work across files.
        provider = FakeModelProvider(
            [
                tool_response([("edit_file", _edit("geometry.py", SIGNATURE, NEW_SIGNATURE))]),
                tool_response([("edit_file", _edit("geometry.py", BODY, NEW_BODY))]),
                tool_response([("run_command", _run_tests())]),
                tool_response([("edit_file", _edit("plotter.py", CALL_SITE, NEW_CALL_SITE))]),
                tool_response([("edit_file", _edit("test_geometry.py", TEST_CALL, NEW_TEST_CALL))]),
                tool_response([("run_command", _run_tests())]),
                text_response("The callers were passing four numbers; updated both."),
            ]
        )
        context = _context(app_paths, project, provider)

        result = context.agent.run("Make distance() take two points and keep the tests green.")

        assert result.error is None
        runs = [r for r in context.session.tool_results if r.tool == "run_command"]
        assert len(runs) == 2
        assert runs[0].data["exit_code"] != 0, "the first run must fail"
        assert "TypeError" in runs[0].data["stdout"] + runs[0].data["stderr"]
        assert runs[1].data["exit_code"] == 0, "the re-run after the fix must pass"

    def test_the_change_can_be_reverted_one_file_at_a_time(
        self, app_paths: Any, project: Path
    ) -> None:
        # A multi-file change stays reversible in pieces, with the same safety
        # rules as any single edit.
        provider = FakeModelProvider(
            [
                tool_response([("edit_file", _edit("geometry.py", SIGNATURE, NEW_SIGNATURE))]),
                tool_response([("edit_file", _edit("plotter.py", CALL_SITE, NEW_CALL_SITE))]),
                tool_response([("undo_file", {"path": "geometry.py"})]),
                text_response("Reverted the interface; the caller still passes two points."),
            ]
        )
        context = _context(app_paths, project, provider)
        original = (project / "geometry.py").read_text(encoding="utf-8")

        context.agent.run("Change the interface, update the caller, then undo the interface.")

        assert (project / "geometry.py").read_text(encoding="utf-8") == original
        assert "total += distance(first, second)" in (project / "plotter.py").read_text(
            encoding="utf-8"
        )
        assert context.session.revisions.depth("geometry.py") == 0
        assert context.session.revisions.depth("plotter.py") == 1, "the other edit stands"


class TestPartialFailuresStayVisible:
    def test_a_denied_caller_edit_leaves_the_change_incomplete(
        self, app_paths: Any, project: Path
    ) -> None:
        # The interface edits are approved, the caller edit is denied. The turn
        # must not present the cross-file change as finished, and the denied file
        # must be untouched.
        def approver(request: PermissionRequest) -> ApprovalOutcome:
            if request.target == "plotter.py":
                return ApprovalOutcome.DENY
            return ApprovalOutcome.ALLOW_ONCE

        provider = FakeModelProvider(
            [
                tool_response([("edit_file", _edit("geometry.py", SIGNATURE, NEW_SIGNATURE))]),
                tool_response([("edit_file", _edit("geometry.py", BODY, NEW_BODY))]),
                tool_response([("edit_file", _edit("plotter.py", CALL_SITE, NEW_CALL_SITE))]),
                tool_response([("run_command", _run_tests())]),
                text_response("I changed the interface but plotter.py was not updated."),
            ]
        )
        context = _context(app_paths, project, provider, approver=approver)
        plotter_before = (project / "plotter.py").read_text(encoding="utf-8")

        result = context.agent.run("Make distance() take two points and fix the callers.")

        assert result.error is None
        edits = [r for r in context.session.tool_results if r.tool == "edit_file"]
        assert [edit.ok for edit in edits] == [True, True, False]
        assert "not permitted" in (edits[2].error or "").lower()
        assert (project / "plotter.py").read_text(encoding="utf-8") == plotter_before
        assert context.session.revisions.depth("plotter.py") == 0, "a denied edit records nothing"

        # The suite then fails, and that failure is reported rather than hidden.
        tests = next(r for r in context.session.tool_results if r.tool == "run_command")
        assert tests.data["exit_code"] != 0

    def test_a_denied_interface_edit_leaves_the_caller_edit_undoable(
        self, app_paths: Any, project: Path
    ) -> None:
        def approver(request: PermissionRequest) -> ApprovalOutcome:
            if request.target == "geometry.py":
                return ApprovalOutcome.DENY
            return ApprovalOutcome.ALLOW_ONCE

        provider = FakeModelProvider(
            [
                tool_response([("edit_file", _edit("geometry.py", SIGNATURE, NEW_SIGNATURE))]),
                tool_response([("edit_file", _edit("plotter.py", CALL_SITE, NEW_CALL_SITE))]),
                text_response("The interface edit was declined; the caller was updated."),
            ]
        )
        context = _context(app_paths, project, provider, approver=approver)
        geometry_before = (project / "geometry.py").read_text(encoding="utf-8")

        context.agent.run("Make distance() take two points and fix the caller.")

        assert (project / "geometry.py").read_text(encoding="utf-8") == geometry_before
        edits = [r for r in context.session.tool_results if r.tool == "edit_file"]
        assert [edit.ok for edit in edits] == [False, True]
        assert context.session.revisions.depth("plotter.py") == 1


class TestAffectedFilesAreReportedByTheEdit:
    def test_editing_the_interface_surfaces_its_callers_to_the_agent(
        self, app_paths: Any, project: Path
    ) -> None:
        # The agent should not have to remember to ask who depends on the file it
        # just changed: the edit result tells it, so a cross-file change cannot be
        # half-finished because the model forgot one call site.
        provider = FakeModelProvider(
            [
                tool_response([("edit_file", _edit("geometry.py", SIGNATURE, NEW_SIGNATURE))]),
                tool_response([("edit_file", _edit("geometry.py", BODY, NEW_BODY))]),
                text_response("Changed the signature; plotter.py and test_geometry.py call it."),
            ]
        )
        context = _context(app_paths, project, provider)

        context.agent.run("Make distance() take two points.")

        first = next(r for r in context.session.tool_results if r.tool == "edit_file")
        assert first.ok
        assert {entry["path"] for entry in first.data["dependents"]} == {
            "plotter.py",
            "test_geometry.py",
        }
        assert first.data["dependents_truncated"] is False

    def test_the_caller_edit_reports_only_its_own_dependents(
        self, app_paths: Any, project: Path
    ) -> None:
        provider = FakeModelProvider(
            [
                tool_response([("edit_file", _edit("plotter.py", CALL_SITE, NEW_CALL_SITE))]),
                text_response("Updated the caller."),
            ]
        )
        context = _context(app_paths, project, provider)

        context.agent.run("Update the caller.")

        edit = next(r for r in context.session.tool_results if r.tool == "edit_file")
        # plotter.py is imported by its own test, so the report is specific rather
        # than "the whole project".
        assert {entry["path"] for entry in edit.data["dependents"]} <= {"test_plotter.py"}
