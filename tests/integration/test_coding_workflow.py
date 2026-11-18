"""The change loop, end to end: locate, edit, validate, correct.

Each test drives the real orchestration path -- a real application, a real tool
registry, a real broker -- with a scripted model. What they assert is the shape
of the work the agent is now able to do, and the two properties that keep that
work honest: a repeat cannot spin, and a refusal is never laundered into a
result.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

from quantsmind_agent.app import ApplicationContext
from quantsmind_agent.app.settings import ConfigManager
from quantsmind_agent.security.permissions import ApprovalOutcome
from quantsmind_agent.security.secrets import FileSecretStore
from tests.fakes import FakeModelProvider, text_response, tool_response

pytestmark = pytest.mark.integration

MODULE = '''"""A tiny module with a bug."""


def total(values):
    """Sum the values."""
    return sum(values)


def average(values):
    """Average the values."""
    return total(values) / len(values)
'''

TESTS = """from mod import average, total


def test_total():
    assert total([1, 2, 3]) == 6


def test_average():
    assert average([1, 2, 3]) == 2
"""


def _context(app_paths: Any, project: Path, provider: FakeModelProvider) -> ApplicationContext:
    manager = ConfigManager(app_paths, secret_store=FileSecretStore(app_paths.secrets_file))
    context = ApplicationContext.create(paths=app_paths, config_manager=manager)
    context.open_workspace(project, trusted=True)
    context.agent._gateway.set_provider(provider)
    context.tools.permissions.set_approver(lambda request: ApprovalOutcome.ALLOW_ONCE)
    return context


@pytest.fixture
def project(tmp_path: Path) -> Path:
    root = tmp_path / "bug_project"
    root.mkdir()
    (root / "mod.py").write_text(MODULE, encoding="utf-8")
    (root / "test_mod.py").write_text(TESTS, encoding="utf-8")
    return root


def _run_tests() -> dict[str, Any]:
    return {
        "command": sys.executable,
        "arguments": ["-m", "pytest", "-q", "--no-header", "-p", "no:cacheprovider"],
    }


def _fix_average_call() -> dict[str, Any]:
    return {
        "path": "mod.py",
        "operation": "replace",
        "old_text": "return total(values) / len(values)",
        "new_text": "return total(values) / len(values) if values else 0",
    }


class TestLocateEditValidate:
    def test_the_agent_locates_the_symbol_edits_it_and_verifies(
        self, app_paths: Any, project: Path
    ) -> None:
        # The whole loop in one turn: find the definition by name, read its
        # structure, replace the one line that is wrong, run the tests again.
        provider = FakeModelProvider(
            [
                tool_response([("project_symbols", {"query": "average"})]),
                tool_response([("code_outline", {"path": "mod.py"})]),
                tool_response([("edit_file", _fix_average_call())]),
                tool_response([("run_command", _run_tests())]),
                text_response("Fixed average() for an empty list; the suite passes."),
            ]
        )
        context = _context(app_paths, project, provider)

        result = context.agent.run("average() crashes on an empty list -- fix it")

        assert result.error is None
        assert "if values else 0" in (project / "mod.py").read_text(encoding="utf-8")

        used = [r.tool for r in context.session.tool_results]
        assert used == ["project_symbols", "code_outline", "edit_file", "run_command"]

        edit = next(r for r in context.session.tool_results if r.tool == "edit_file")
        assert edit.data["diff_kind"] == "modified"
        assert "+    return total(values) / len(values) if values else 0" in edit.data["diff"]

        tests = next(r for r in context.session.tool_results if r.tool == "run_command")
        assert tests.ok
        assert tests.data["exit_code"] == 0

    def test_the_fix_can_be_revalidated_in_a_later_turn(
        self, app_paths: Any, project: Path
    ) -> None:
        # The repeat guard is scoped to one turn: a new request is new evidence.
        first = FakeModelProvider(
            [
                tool_response([("edit_file", _fix_average_call())]),
                text_response("Guarded."),
            ]
        )
        context = _context(app_paths, project, first)
        context.agent.run("Guard average() against an empty list.")

        second = FakeModelProvider(
            [
                tool_response([("run_command", _run_tests())]),
                text_response("The suite passes."),
            ]
        )
        context.agent._gateway.set_provider(second)

        result = context.agent.run("Run the tests to confirm.")

        assert result.error is None
        run = next(r for r in context.session.tool_results if r.tool == "run_command")
        assert run.ok
        assert run.data["exit_code"] == 0


class TestNoSpinning:
    def test_an_identical_command_is_refused_until_something_changes(
        self, app_paths: Any, project: Path
    ) -> None:
        # Two identical runs with nothing in between cannot differ, so the
        # second is refused instead of spending an iteration and an approval.
        provider = FakeModelProvider(
            [
                tool_response([("run_command", _run_tests())]),
                tool_response([("run_command", _run_tests())]),
                text_response("The same command already ran; nothing changed since."),
            ]
        )
        context = _context(app_paths, project, provider)

        context.agent.run("Run the tests.")

        commands = [r for r in context.session.tool_results if r.tool == "run_command"]
        assert len(commands) == 2
        assert commands[0].ok
        assert not commands[1].ok
        assert "already called" in (commands[1].error or "")

    def test_an_edit_reopens_the_same_command(self, app_paths: Any, project: Path) -> None:
        # The guard must not break the loop it exists to protect: running the
        # tests again *after* a change is exactly right.
        provider = FakeModelProvider(
            [
                tool_response([("run_command", _run_tests())]),
                tool_response(
                    [
                        (
                            "edit_file",
                            {
                                "path": "mod.py",
                                "operation": "replace",
                                "old_text": "return sum(values)",
                                "new_text": "return sum(values) if values else 0",
                            },
                        )
                    ]
                ),
                tool_response([("run_command", _run_tests())]),
                text_response("Re-ran after the edit."),
            ]
        )
        context = _context(app_paths, project, provider)

        context.agent.run("Fix total() and re-run the tests.")

        commands = [r for r in context.session.tool_results if r.tool == "run_command"]
        assert len(commands) == 2
        assert commands[1].ok, "a re-run after a change must be allowed"
        assert "if values else 0" in (project / "mod.py").read_text(encoding="utf-8")

    def test_an_identical_read_is_refused(self, app_paths: Any, project: Path) -> None:
        provider = FakeModelProvider(
            [
                tool_response([("read_file", {"path": "mod.py"})]),
                tool_response([("read_file", {"path": "mod.py"})]),
                text_response("Already read it."),
            ]
        )
        context = _context(app_paths, project, provider)

        context.agent.run("What is in mod.py?")

        reads = [r for r in context.session.tool_results if r.tool == "read_file"]
        assert reads[0].ok
        assert not reads[1].ok

    def test_argument_order_does_not_make_a_call_new(self, app_paths: Any, project: Path) -> None:
        provider = FakeModelProvider(
            [
                tool_response([("search_content", {"pattern": "total", "ignore_case": True})]),
                tool_response([("search_content", {"ignore_case": True, "pattern": "total"})]),
                text_response("Same search."),
            ]
        )
        context = _context(app_paths, project, provider)

        context.agent.run("Find total.")

        searches = [r for r in context.session.tool_results if r.tool == "search_content"]
        assert searches[0].ok
        assert not searches[1].ok, "the same arguments in another order are the same call"

    def test_the_refusal_does_not_end_the_turn(self, app_paths: Any, project: Path) -> None:
        provider = FakeModelProvider(
            [
                tool_response([("code_outline", {"path": "mod.py"})]),
                tool_response([("code_outline", {"path": "mod.py"})]),
                text_response("Done."),
            ]
        )
        context = _context(app_paths, project, provider)

        result = context.agent.run("Outline mod.py.")

        assert result.error is None
        assert result.stopped_reason == "final_answer"


class TestFailuresAreActionable:
    def test_a_failing_test_run_reaches_the_model_with_its_diagnostics(
        self, app_paths: Any, project: Path
    ) -> None:
        # The point of keeping the tail: the failure is at the end of the output.
        (project / "test_mod.py").write_text(
            TESTS + "\n\ndef test_missing():\n    assert average([]) == 0\n",
            encoding="utf-8",
        )
        provider = FakeModelProvider(
            [
                tool_response([("run_command", _run_tests())]),
                text_response("average([]) raises ZeroDivisionError; it needs a guard."),
            ]
        )
        context = _context(app_paths, project, provider)

        context.agent.run("Run the tests.")

        run = next(r for r in context.session.tool_results if r.tool == "run_command")
        assert run.ok, "a failing suite is a successful command, not a tool failure"
        assert run.data["exit_code"] != 0
        assert "ZeroDivisionError" in run.data["stderr"] + run.data["stdout"]

    def test_a_blocked_command_never_reaches_the_user(self, app_paths: Any, project: Path) -> None:
        provider = FakeModelProvider(
            [
                tool_response([("run_command", {"command": "push", "arguments": ["origin"]})]),
                text_response("Pushing is blocked."),
            ]
        )
        context = _context(app_paths, project, provider)

        result = context.agent.run("Push my work.")

        assert result.error is None
        run = next(r for r in context.session.tool_results if r.tool == "run_command")
        assert not run.ok
        assert "push" in (run.error or "").lower()

    def test_a_denied_command_changes_nothing(self, app_paths: Any, project: Path) -> None:
        marker = project / "marker.txt"
        script = f"open({str(marker)!r}, 'w').write('created')"
        provider = FakeModelProvider(
            [
                tool_response(
                    [("run_command", {"command": sys.executable, "arguments": ["-c", script]})]
                ),
                text_response("Understood, I will not run it again."),
            ]
        )
        context = _context(app_paths, project, provider)
        context.tools.permissions.set_approver(lambda request: ApprovalOutcome.DENY)

        result = context.agent.run("Create marker.txt with a command.")

        assert result.error is None
        assert not marker.exists(), "a denied command must not run"
        run = next(r for r in context.session.tool_results if r.tool == "run_command")
        assert not run.ok
        assert "not permitted" in (run.error or "").lower()

    def test_a_denied_edit_leaves_the_file_alone_and_the_loop_can_retry(
        self, app_paths: Any, project: Path
    ) -> None:
        # Denial is a failed observation, not a dead end: the model is told and
        # may propose something else on the next iteration.
        target = project / "mod.py"
        before = target.read_bytes()
        provider = FakeModelProvider(
            [
                tool_response([("edit_file", _fix_average_call())]),
                tool_response([("code_outline", {"path": "mod.py"})]),
                text_response("The edit was declined, so I left the file alone."),
            ]
        )
        context = _context(app_paths, project, provider)
        context.tools.permissions.set_approver(lambda request: ApprovalOutcome.DENY)

        result = context.agent.run("Guard average() against an empty list.")

        assert result.error is None
        assert target.read_bytes() == before
        assert context.session.revisions.size == 0, "a denied edit records no undo step"
        assert [r.tool for r in context.session.tool_results] == ["edit_file", "code_outline"]
