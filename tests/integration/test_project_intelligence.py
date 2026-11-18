"""The agent's project intelligence, driven through the real application.

These are the questions the whole feature exists for, asked of a real
:class:`ApplicationContext`: can the agent find out what kind of project it is in,
where the tests are, who calls the thing it is about to change, and does any of
that survive a file the agent just edited?

Nothing is stubbed below the tool boundary. The provider is scripted, the
workspace is a real directory, and writes go through the same approval, diff and
undo machinery as every other change.
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


def _context(app_paths: Any, project: Path | None = None) -> ApplicationContext:
    manager = ConfigManager(app_paths, secret_store=FileSecretStore(app_paths.secrets_file))
    context = ApplicationContext.create(paths=app_paths, config_manager=manager)
    if project is not None:
        context.open_workspace(project, trusted=True)
    context.tools.permissions.set_approver(lambda request: ApprovalOutcome.ALLOW_ONCE)
    return context


def _data(result: Any) -> dict[str, Any]:
    return dict(result.data)


def _pytest() -> dict[str, Any]:
    """Run pytest through the configured interpreter.

    The command policy permits the interpreter and refuses an arbitrary binary, so
    ``run_command`` gets ``python -m pytest`` rather than a bare ``pytest``. That is
    the real permission path, not a test shortcut.
    """

    return {
        "command": sys.executable,
        "arguments": ["-m", "pytest", "-q", "--no-header", "-p", "no:cacheprovider"],
    }


@pytest.fixture
def shop(tmp_path: Path) -> Path:
    """A small but realistic package: a layout, a test and something to change."""

    root = tmp_path / "shop"
    (root / "src" / "shop" / "cli").mkdir(parents=True)
    (root / "tests").mkdir()
    (root / "pyproject.toml").write_text(
        '[project]\nname = "shop"\n\n[project.scripts]\nshop = "shop.cli.app:main"\n',
        encoding="utf-8",
    )
    (root / "README.md").write_text("# Shop\n", encoding="utf-8")
    (root / "src" / "shop" / "__init__.py").write_text("", encoding="utf-8")
    (root / "src" / "shop" / "cart.py").write_text(
        'def total(items):\n    """Sum the prices."""\n\n    running = 0\n'
        "    for price in items:\n        running += price\n    return running\n",
        encoding="utf-8",
    )
    (root / "src" / "shop" / "cli" / "__init__.py").write_text("", encoding="utf-8")
    (root / "src" / "shop" / "cli" / "app.py").write_text(
        "from shop import cart\n\n\ndef main():\n    return cart.total([1, 2])\n",
        encoding="utf-8",
    )
    (root / "tests" / "test_cart.py").write_text(
        "from shop.cart import total\n\n\ndef test_total():\n    assert total([1, 2]) == 3\n",
        encoding="utf-8",
    )
    return root


class TestTheAgentCanUnderstandTheProject:
    def test_the_project_is_classified_and_oriented(self, app_paths: Any, shop: Path) -> None:
        summary = _context(app_paths, shop).context_engine.workspace_summary()

        assert "python-package" in summary
        assert "Source roots: src" in summary
        assert "Tests: tests" in summary

    def test_project_context_reports_the_layout(self, app_paths: Any, shop: Path) -> None:
        result = _context(app_paths, shop).tools.execute("project_context", {"task": "look around"})
        data = _data(result)

        assert result.ok
        assert data["project"]["kind"] == "python-package"
        assert data["project"]["layout"] == "src"
        assert data["project"]["test_roots"] == ["tests"]

    def test_project_context_names_the_entry_point(self, app_paths: Any, shop: Path) -> None:
        data = _data(
            _context(app_paths, shop).tools.execute("project_context", {"task": "how is it run"})
        )

        assert any(entry["command"] == "shop" for entry in data["project"]["entry_points"])

    def test_project_context_finds_the_test_for_a_file(self, app_paths: Any, shop: Path) -> None:
        data = _data(
            _context(app_paths, shop).tools.execute(
                "project_context", {"path": "src/shop/cart.py", "task": "change the total"}
            )
        )

        assert "tests/test_cart.py" in data["tests"]

    def test_project_context_finds_who_calls_a_file(self, app_paths: Any, shop: Path) -> None:
        data = _data(
            _context(app_paths, shop).tools.execute("project_context", {"path": "src/shop/cart.py"})
        )

        assert "src/shop/cli/app.py" in data["callers"]

    def test_the_tool_is_available_to_the_agent(self, app_paths: Any, shop: Path) -> None:
        assert "project_context" in _context(app_paths, shop).tools.names

    def test_the_output_is_bounded_and_says_when_it_is(self, app_paths: Any, shop: Path) -> None:
        data = _data(
            _context(app_paths, shop).tools.execute(
                "project_context", {"path": "src/shop/cart.py", "max_files": 1}
            )
        )

        assert len(data["files"]) <= 1
        assert isinstance(data["truncated"], bool)
        assert "lexical" in data["relevance"]

    def test_a_call_with_nothing_to_aim_at_is_refused(self, app_paths: Any, shop: Path) -> None:
        result = _context(app_paths, shop).tools.execute("project_context", {})

        assert result.ok is False
        assert "task, a path or a symbol" in str(result.error)

    def test_the_index_state_is_reported(self, app_paths: Any, shop: Path) -> None:
        data = _data(_context(app_paths, shop).tools.execute("project_context", {"task": "look"}))

        assert data["index"]["state"] == "indexed"
        assert data["index"]["files"] >= 1


class TestTheAgentCanNavigateReusingTheIndex:
    def test_project_symbols_comes_from_the_index(self, app_paths: Any, shop: Path) -> None:
        result = _context(app_paths, shop).tools.execute("project_symbols", {"query": "total"})
        data = _data(result)

        assert result.ok
        assert data["source"] == "project index"
        assert any(item["qualified"] == "total" for item in data["symbols"])

    def test_project_symbols_accepts_a_single_file(self, app_paths: Any, shop: Path) -> None:
        data = _data(
            _context(app_paths, shop).tools.execute("project_symbols", {"path": "src/shop/cart.py"})
        )

        assert [item["qualified"] for item in data["symbols"]] == ["total"]

    def test_the_index_is_shared_between_calls(self, app_paths: Any, shop: Path) -> None:
        context = _context(app_paths, shop)
        assert context.project_intel is not None

        context.tools.execute("project_symbols", {})
        first = context.project_intel.map.known_paths()
        context.tools.execute("project_symbols", {"query": "total"})

        # One walk, reused: the second call re-parses nothing.
        assert context.project_intel.map.known_paths() == first


class TestTheIndexFollowsTheAgentsOwnWrites:
    def test_a_new_symbol_appears_after_an_edit(self, app_paths: Any, shop: Path) -> None:
        context = _context(app_paths, shop)
        context.tools.execute("project_symbols", {})

        context.tools.execute(
            "edit_file",
            {
                "path": "src/shop/cart.py",
                "operation": "append",
                "new_text": "def average(items):\n    return total(items) / len(items)\n",
            },
        )

        data = _data(context.tools.execute("project_symbols", {"query": "average"}))

        assert any(item["qualified"] == "average" for item in data["symbols"])

    def test_an_edit_invalidates_the_workspace_snapshot(self, app_paths: Any, shop: Path) -> None:
        context = _context(app_paths, shop)
        assert context.workspace_snapshot() is not None
        assert context.workspace_intel is not None
        assert context.workspace_intel.cached_snapshot is not None

        context.tools.execute(
            "edit_file",
            {
                "path": "src/shop/cart.py",
                "operation": "append",
                "new_text": "def average(items):\n    return total(items) / len(items)\n",
            },
        )

        assert context.workspace_intel.cached_snapshot is None

    def test_a_created_file_is_visible_to_the_context_tool(
        self, app_paths: Any, shop: Path
    ) -> None:
        context = _context(app_paths, shop)
        context.tools.execute("project_context", {"task": "look"})

        context.tools.execute(
            "write_file",
            {
                "path": "src/shop/discount.py",
                "content": "def discounted(total, percent):\n    return total * (1 - percent)\n",
            },
        )

        data = _data(context.tools.execute("project_context", {"symbol": "discount"}))

        assert any(item["path"] == "src/shop/discount.py" for item in data["files"])

    def test_a_deleted_file_stops_being_reported_as_a_caller(
        self, app_paths: Any, shop: Path
    ) -> None:
        context = _context(app_paths, shop)
        context.tools.execute("project_context", {"path": "src/shop/cart.py"})

        context.tools.execute("delete_file", {"path": "src/shop/cli/app.py"})

        assert context.project_intel is not None
        assert context.project_intel.importers_of("src/shop/cart.py") == ()

    def test_an_undo_re_invalidates_what_it_restored(self, app_paths: Any, shop: Path) -> None:
        context = _context(app_paths, shop)
        context.tools.execute("project_symbols", {})
        context.tools.execute(
            "edit_file",
            {
                "path": "src/shop/cart.py",
                "operation": "append",
                "new_text": "def tax(x):\n    return x\n",
            },
        )
        assert context.project_intel is not None
        after_edit = _data(context.tools.execute("project_symbols", {"query": "tax"}))
        assert any(item["qualified"] == "tax" for item in after_edit["symbols"])

        context.tools.execute("undo_file", {"path": "src/shop/cart.py"})

        # The undo put the file back, so a symbol that is no longer on disk must
        # not still be in the map.
        assert _data(context.tools.execute("project_symbols", {"query": "tax"}))["symbols"] == []

    def test_the_write_still_reports_affected_files(self, app_paths: Any, shop: Path) -> None:
        context = _context(app_paths, shop)
        context.tools.execute("project_symbols", {})

        data = _data(
            context.tools.execute(
                "edit_file",
                {
                    "path": "src/shop/cart.py",
                    "operation": "replace",
                    "old_text": "return running\n",
                    "new_text": "return round(running, 2)\n",
                },
            )
        )

        # The intelligence layer must not cost the existing guarantees.
        assert data["undoable"] is True
        assert "diff" in data


class TestApplicationWorkspaceSnapshot:
    def test_the_snapshot_is_cached_between_calls(self, app_paths: Any, shop: Path) -> None:
        context = _context(app_paths, shop)

        first = context.workspace_snapshot()

        assert first is not None
        assert first.file_count > 0
        assert context.workspace_snapshot() is first

    def test_a_refresh_sees_files_created_since_the_last_snapshot(
        self, app_paths: Any, shop: Path
    ) -> None:
        context = _context(app_paths, shop)
        first = context.workspace_snapshot()
        assert first is not None

        (shop / "src" / "shop" / "extra.py").write_text("VALUE = 1\n", encoding="utf-8")
        refreshed = context.workspace_snapshot(refresh=True)

        assert refreshed is not None
        assert refreshed.file_count > first.file_count

    def test_restoring_the_last_workspace_builds_intelligence(
        self, app_paths: Any, shop: Path
    ) -> None:
        _context(app_paths, shop)

        restored = _context(app_paths)

        assert restored.workspace is not None
        assert restored.workspace_intel is not None
        assert restored.workspace_snapshot() is not None


class TestACodingWorkflowEndToEnd:
    """The whole loop, through the agent, on a project with a real defect.

    ``cart.total`` ignores discounts. The agent has to work out the project, find
    the file and its test, inspect the symbol, change it, notice the caller,
    validate, see the failure, correct it and finish. Every step goes through the
    ordinary tools -- nothing here reaches past the boundary.
    """

    def _provider(self) -> FakeModelProvider:
        return FakeModelProvider(
            [
                # 1. understand the project before touching it
                tool_response(
                    [("project_context", {"path": "src/shop/cart.py", "task": "apply a discount"})]
                ),
                # 2. see the symbol
                tool_response([("project_symbols", {"path": "src/shop/cart.py"})]),
                # 3. make the change
                tool_response(
                    [
                        (
                            "edit_file",
                            {
                                "path": "src/shop/cart.py",
                                "operation": "replace",
                                "old_text": "def total(items):",
                                "new_text": "def total(items, discount=0):",
                            },
                        )
                    ]
                ),
                # 4. find who is affected
                tool_response([("project_context", {"path": "src/shop/cart.py"})]),
                # 5. validate
                tool_response([("run_command", _pytest())]),
                # 6. the failing run comes back; correct it
                tool_response(
                    [
                        (
                            "edit_file",
                            {
                                "path": "src/shop/cart.py",
                                "operation": "replace",
                                "old_text": "return running\n",
                                "new_text": "return round(running * (1 - discount), 2)\n",
                            },
                        )
                    ]
                ),
                text_response("The total now applies the discount and the test passes."),
            ]
        )

    @pytest.fixture
    def broken_shop(self, tmp_path: Path) -> Path:
        root = tmp_path / "broken_shop"
        (root / "src" / "shop").mkdir(parents=True)
        (root / "tests").mkdir()
        (root / "pyproject.toml").write_text(
            "[project]\n"
            'name = "shop"\n'
            "\n[tool.pytest.ini_options]\n"
            'testpaths = ["tests"]\n'
            'pythonpath = ["src"]\n',
            encoding="utf-8",
        )
        (root / "src" / "shop" / "__init__.py").write_text("", encoding="utf-8")
        (root / "src" / "shop" / "cart.py").write_text(
            "def total(items):\n"
            "    running = 0\n"
            "    for price in items:\n"
            "        running += price\n"
            "    return running\n",
            encoding="utf-8",
        )
        (root / "src" / "shop" / "cli.py").write_text(
            "from shop import cart\n\n\ndef main():\n    return cart.total([10, 20])\n",
            encoding="utf-8",
        )
        (root / "tests" / "test_cart.py").write_text(
            "from shop.cart import total\n\n\ndef test_discount():\n"
            "    assert total([100], 0.1) == 90.0\n",
            encoding="utf-8",
        )
        return root

    def test_the_agent_works_from_the_project_down_to_a_verified_change(
        self, app_paths: Any, broken_shop: Path
    ) -> None:
        context = _context(app_paths, broken_shop)
        context.agent._gateway.set_provider(self._provider())

        outcome = context.agent.run("Apply a discount in cart.total")

        used = [
            str(call.name)
            for message in context.session.messages
            for call in getattr(message, "tool_calls", [])
        ]
        # The agent started from the project, not from a blind file read.
        assert used[0] == "project_context"
        assert "project_symbols" in used
        assert used.count("edit_file") == 2
        assert "project_context" in used[1:]
        assert "run_command" in used
        assert context.tools.execute("read_file", {"path": "src/shop/cart.py"}).ok

        # The change is real, on disk, and matches what the agent said.
        source = (broken_shop / "src" / "shop" / "cart.py").read_text(encoding="utf-8")
        assert "return round(running * (1 - discount), 2)" in source
        assert outcome.text.startswith("The total now applies the discount")

    def test_the_defect_is_genuinely_fixed(self, app_paths: Any, broken_shop: Path) -> None:
        context = _context(app_paths, broken_shop)
        context.agent._gateway.set_provider(self._provider())
        context.agent.run("Apply a discount in cart.total")

        # Opening the same project again rebuilds the tool stack, and the approver
        # with it: this exercises the real permission path rather than assuming.
        context.open_workspace(broken_shop, trusted=True)
        context.tools.permissions.set_approver(lambda request: ApprovalOutcome.ALLOW_ONCE)
        check = context.tools.execute("run_command", _pytest())

        assert check.ok, check.summary
        assert "1 passed" in str(check.data.get("stdout", "")) or "passed" in str(
            check.data.get("stdout", "")
        )

    def test_the_change_is_undoable_and_undo_restores_the_original(
        self, app_paths: Any, broken_shop: Path
    ) -> None:
        context = _context(app_paths, broken_shop)
        context.agent._gateway.set_provider(self._provider())

        context.agent.run("Apply a discount in cart.total")

        assert context.session.revisions.size == 2
        for path in ("src/shop/cart.py", "src/shop/cart.py"):
            context.tools.execute("undo_file", {"path": path})

        restored = (broken_shop / "src" / "shop" / "cart.py").read_text(encoding="utf-8")
        assert "return round(running * (1 - discount), 2)" not in restored
        assert "return running" in restored

    def test_the_turn_is_recorded_with_every_tool_it_used(
        self, app_paths: Any, broken_shop: Path
    ) -> None:
        context = _context(app_paths, broken_shop)
        context.agent._gateway.set_provider(self._provider())

        context.agent.run("Apply a discount in cart.total")

        turn = context.session.turns[-1]
        assert [result.tool for result in turn.tool_results][:2] == [
            "project_context",
            "project_symbols",
        ]

    def test_the_index_survives_the_turn_and_sees_the_final_state(
        self, app_paths: Any, broken_shop: Path
    ) -> None:
        context = _context(app_paths, broken_shop)
        context.agent._gateway.set_provider(self._provider())

        context.agent.run("Apply a discount in cart.total")

        assert context.project_intel is not None
        symbols = {symbol.qualified for symbol in context.project_intel.symbols("src/shop/cart.py")}
        assert symbols == {"total"}
