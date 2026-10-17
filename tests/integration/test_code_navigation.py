"""End-to-end: the agent finds code with the new tools and edits what it found.

These drive a real application, a real tool registry and a scripted provider. The
question they answer is whether the navigation tools are useful in the loop, not
whether they parse: the model outlines a file, aims ``edit_file`` at the line
range it was given, and the result is checked against the file on disk.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from quantsmind_agent.app import ApplicationContext
from quantsmind_agent.config import ConfigManager
from quantsmind_agent.permissions import ApprovalOutcome
from quantsmind_agent.secrets import FileSecretStore
from tests.fakes import FakeModelProvider, text_response, tool_response

pytestmark = pytest.mark.integration

CALCULATOR = '''"""Calculator."""


def add(left, right):
    """Add two numbers."""
    return left + right


def subtract(left, right):
    """Subtract."""
    return left - right
'''

REPORT = "The calculator exports two functions: add and subtract.\n"


def _context(app_paths: Any, project: Path, provider: FakeModelProvider) -> ApplicationContext:
    manager = ConfigManager(app_paths, secret_store=FileSecretStore(app_paths.secrets_file))
    context = ApplicationContext.create(paths=app_paths, config_manager=manager)
    context.open_workspace(project)
    context.agent._gateway.set_provider(provider)

    def approve(request: Any) -> ApprovalOutcome:
        return ApprovalOutcome.ALLOW_ONCE

    context.tools.permissions.set_approver(approve)
    return context


@pytest.fixture
def project(tmp_path: Path) -> Path:
    root = tmp_path / "calc_project"
    root.mkdir()
    (root / "calculator.py").write_text(CALCULATOR, encoding="utf-8")
    return root


class TestNavigationInALoop:
    def test_the_model_outlines_a_file_then_answers_from_it(
        self, app_paths: Any, project: Path
    ) -> None:
        provider = FakeModelProvider(
            [
                tool_response([("code_outline", {"path": "calculator.py"})]),
                text_response(REPORT),
            ]
        )
        context = _context(app_paths, project, provider)

        result = context.agent.run("What does calculator.py export?")

        assert result.error is None
        assert "add" in result.text
        outline_call = next(r for r in context.session.tool_results if r.tool == "code_outline")
        assert outline_call.ok
        qualified = {s["qualified"] for s in outline_call.data["symbols"]}
        assert qualified == {"add", "subtract"}

    def test_an_outline_makes_a_line_addressed_edit_possible(
        self, app_paths: Any, project: Path
    ) -> None:
        # The model reads the line range from the outline and replaces exactly it.
        # Nothing else in the file is touched, which is the property that makes
        # the tools worth having.
        provider = FakeModelProvider(
            [
                tool_response([("code_outline", {"path": "calculator.py"})]),
                tool_response(
                    [
                        (
                            "edit_file",
                            {
                                "path": "calculator.py",
                                "operation": "replace_lines",
                                "start_line": 5,
                                "end_line": 5,
                                "new_text": '    """Add two numbers, precisely."""',
                            },
                        )
                    ]
                ),
                text_response("Updated the docstring."),
            ]
        )
        context = _context(app_paths, project, provider)

        result = context.agent.run("Improve the add() docstring.")

        assert result.error is None
        source = (project / "calculator.py").read_text(encoding="utf-8")
        assert '"""Add two numbers, precisely."""' in source
        assert "def add(left, right):" in source
        assert "def subtract(left, right):" in source
        assert '"""Subtract."""' in source

    def test_references_show_the_blast_radius_before_a_change(
        self, app_paths: Any, project: Path
    ) -> None:
        (project / "app.py").write_text(
            "from calculator import add\n\n\ndef main():\n    return add(1, 2)\n",
            encoding="utf-8",
        )
        provider = FakeModelProvider(
            [
                tool_response([("find_references", {"symbol": "add"})]),
                text_response("Two places use add: the import and main()."),
            ]
        )
        context = _context(app_paths, project, provider)

        result = context.agent.run("Where is add() used?")

        assert result.error is None
        references = next(r for r in context.session.tool_results if r.tool == "find_references")
        assert references.ok
        assert references.data["definition_count"] == 1
        uses = {(r["path"], r["line"]) for r in references.data["references"]}
        assert ("app.py", 1) in uses
        assert ("app.py", 5) in uses

    def test_the_navigation_tools_are_read_only_in_a_real_session(
        self, app_paths: Any, project: Path
    ) -> None:
        before = (project / "calculator.py").read_bytes()
        provider = FakeModelProvider(
            [
                tool_response([("code_outline", {"path": "calculator.py"})]),
                text_response("Done."),
            ]
        )
        context = _context(app_paths, project, provider)

        context.agent.run("Outline calculator.py")

        assert (project / "calculator.py").read_bytes() == before
        assert context.session.revisions.size == 0, "reading records nothing to undo"

    def test_a_renamed_symbol_is_found_under_its_new_name(
        self, app_paths: Any, project: Path
    ) -> None:
        (project / "calculator.py").write_text(
            CALCULATOR.replace("def add(", "def plus(").replace("add two", "plus two"),
            encoding="utf-8",
        )
        provider = FakeModelProvider(
            [
                tool_response([("find_references", {"symbol": "add"})]),
                tool_response([("find_references", {"symbol": "plus"})]),
                text_response("It is called plus now."),
            ]
        )
        context = _context(app_paths, project, provider)

        context.agent.run("Find the adding function.")

        calls = [r for r in context.session.tool_results if r.tool == "find_references"]
        assert calls[0].data["definition_count"] == 0
        assert calls[1].data["definition_count"] == 1

    def test_a_non_python_file_is_a_tool_error_the_model_can_recover_from(
        self, app_paths: Any, project: Path
    ) -> None:
        (project / "notes.md").write_text("# Notes\n", encoding="utf-8")
        provider = FakeModelProvider(
            [
                tool_response([("code_outline", {"path": "notes.md"})]),
                text_response("That is a Markdown file, so I read it instead."),
            ]
        )
        context = _context(app_paths, project, provider)

        result = context.agent.run("Outline notes.md")

        assert result.error is None, "a refused tool must not end the turn"
        failure = next(r for r in context.session.tool_results if r.tool == "code_outline")
        assert not failure.ok
        assert "read_file" in (failure.error or "")
