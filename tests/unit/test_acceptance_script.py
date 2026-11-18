"""Tests for the acceptance script's own assertions.

The acceptance script (``scripts/acceptance_v0_2.py``) is the artifact that
decides whether a release is acceptable, so its checks need to be trustworthy
in the same way the product is. These tests never contact a model: they drive
the same helper functions against a real tool registry and assert both directions:
that a correct product passes, and that a broken one would fail.

They exist because the previous versions of checks C, E, F, G and J could not
fail for the reason they claimed. Most asserted only that something had *not*
happened, which is equally true when nothing was attempted at all.
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import pytest

REPO = Path(__file__).resolve().parents[2]
for _path in (REPO / "src", REPO / "scripts"):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

if TYPE_CHECKING:
    from collections.abc import Iterator

import acceptance_v0_2 as acceptance  # noqa: E402

from quantsmind_agent.runtime.tools import ToolResult  # noqa: E402
from quantsmind_agent.security.permissions import ApprovalOutcome  # noqa: E402


@pytest.fixture
def workspace() -> Path:
    """A disposable acceptance workspace, matching the real script's fixture."""

    created: Path = acceptance.make_workspace()
    return created


@pytest.fixture
def ctx(workspace: Path) -> Iterator[Any]:
    """The tool registry the acceptance checks drive, and nothing heavier.

    Deliberately *not* an `ApplicationContext`. `quantsmind_agent.app`
    imports the GUI approval module, which imports QtWidgets, and the product
    checks only ever touch `ctx.tools`. Building the registry directly, the
    way `tests/unit/test_write_tools.py` already does, keeps Qt out of the
    unit phase entirely.

    That matters because CI's coverage step runs the whole suite in one
    process. Creating and shutting down a couple of dozen application contexts
    in the unit phase left the later GUI phase running in a process that had
    already imported and torn down Qt, which segfaulted reproducibly on
    CPython 3.12. The GUI-only job never saw it because it never ran these
    tests.
    """

    from quantsmind_agent.events import EventBus
    from quantsmind_agent.git_tools import build_git_tools
    from quantsmind_agent.runtime.tools import ToolRegistry
    from quantsmind_agent.runtime.tools.builtin.command import build_command_tools
    from quantsmind_agent.security.permissions import PermissionBroker, StaticPermissionProvider
    from quantsmind_agent.services.workspace.containment import Workspace
    from quantsmind_agent.write_tools import build_mutating_tools

    broker = PermissionBroker(StaticPermissionProvider.for_v0_2())
    registry = ToolRegistry(permissions=broker)
    ws = Workspace(workspace)
    registry.register_all(build_mutating_tools(ws))
    registry.register_all(build_command_tools(ws))
    registry.register_all(build_git_tools(ws))

    # ``events`` is a plain, Qt-free object, and ``current_agent`` is a
    # placeholder the event-subscription tests replace.
    yield SimpleNamespace(tools=registry, events=EventBus(), current_agent=lambda: None)


def last_result() -> tuple[str, str, str]:
    """The most recently recorded acceptance result as ``(item, status, detail)``."""

    assert acceptance.results, "no acceptance result was recorded"
    entry = acceptance.results[-1]
    return entry.item, entry.status, entry.detail


class TestStaleEditCheck:
    """C must prove the tool refused, not merely that nothing changed."""

    def test_absent_text_is_really_absent_from_the_fixture(self, ctx: Any, workspace: Path) -> None:
        assert acceptance.ABSENT_TEXT.encode() not in (workspace / "calc.py").read_bytes()

    def test_passes_when_the_tool_correctly_refuses(self, ctx: Any, workspace: Path) -> None:
        acceptance.product_c_stale_edit(ctx, workspace)
        item, status, detail = last_result()
        assert item.startswith("C.")
        assert status == acceptance.PASS, detail

    def test_fails_if_the_tool_were_to_succeed(
        self, ctx: Any, workspace: Path, monkeypatch: Any
    ) -> None:
        """A tool that wrongly applied the edit must turn the check red.

        This is the direction the old check could not express: it asserted the
        absent text was still absent, which stays true whenever nothing runs.
        """

        monkeypatch.setattr(
            ctx.tools,
            "execute",
            lambda name, arguments: ToolResult.success("edit_file", "replaced", {}),
        )
        acceptance.product_c_stale_edit(ctx, workspace)
        item, status, _detail = last_result()
        assert item.startswith("C.")
        assert status == acceptance.FAIL

    def test_fails_if_the_file_were_modified(
        self, ctx: Any, workspace: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Correctly failing is not enough; the bytes must be untouched."""

        real_execute = ctx.tools.execute

        def corrupting_execute(name: str, arguments: Any) -> Any:
            result = real_execute(name, arguments)
            if name == "edit_file":
                (workspace / "calc.py").write_text("# clobbered\n", encoding="utf-8")
            return result

        monkeypatch.setattr(ctx.tools, "execute", corrupting_execute)
        acceptance.product_c_stale_edit(ctx, workspace)
        item, status, _detail = last_result()
        assert item.startswith("C.")
        assert status == acceptance.FAIL


class TestCommandCheck:
    """E must show the command really ran, with real output."""

    def test_passes_and_reports_output(self, ctx: Any, workspace: Path) -> None:
        acceptance.product_e_command(ctx)
        item, status, detail = last_result()
        assert item.startswith("E.")
        assert status == acceptance.PASS, detail
        assert "Python" in detail

    def test_fails_when_the_command_errors(
        self, ctx: Any, workspace: Path, monkeypatch: Any
    ) -> None:
        monkeypatch.setattr(
            ctx.tools,
            "execute",
            lambda name, arguments: ToolResult.failure(name, "boom", error="boom"),
        )
        acceptance.product_e_command(ctx)
        item, status, _detail = last_result()
        assert item.startswith("E.")
        assert status == acceptance.FAIL


class TestBlockedCommandCheck:
    """F must prove policy refusal, not model cooperation."""

    def test_format_is_blocked_and_never_approved(self, ctx: Any, workspace: Path) -> None:
        acceptance.product_f_blocked_command(ctx)
        item, status, detail = last_result()
        assert item.startswith("F.")
        assert status == acceptance.PASS, detail
        assert "approvals=[]" in detail

    def test_blocked_holds_even_with_an_always_allow_approver(
        self, ctx: Any, workspace: Path
    ) -> None:
        """The check installs ALLOW_FOR_SESSION, so policy has to win."""

        acceptance.product_f_blocked_command(ctx)
        _, status, detail = last_result()
        assert status == acceptance.PASS
        assert "never_asked=True" in detail

    def test_fails_if_the_command_were_allowed(
        self, ctx: Any, workspace: Path, monkeypatch: Any
    ) -> None:
        monkeypatch.setattr(
            ctx.tools,
            "execute",
            lambda name, arguments: ToolResult.success(name, "formatted", {}),
        )
        acceptance.product_f_blocked_command(ctx)
        item, status, _detail = last_result()
        assert item.startswith("F.")
        assert status == acceptance.FAIL


class TestTraversalCheck:
    """J must inspect the file the traversal would really have reached."""

    def test_sentinel_is_the_path_the_traversal_resolves_to(
        self, ctx: Any, workspace: Path
    ) -> None:
        sentinel = workspace.parent / "secret.txt"
        assert (workspace / ".." / "secret.txt").resolve() == sentinel.resolve()

    def test_refuses_and_leaves_the_outside_file_identical(self, ctx: Any, workspace: Path) -> None:
        sentinel = workspace.parent / "secret.txt"
        acceptance.product_j_traversal(ctx, sentinel)
        item, status, detail = last_result()
        assert item.startswith("J.")
        assert status == acceptance.PASS, detail
        assert sentinel.read_text(encoding="utf-8") == "must not be touched"
        sentinel.unlink(missing_ok=True)

    def test_fails_if_the_outside_file_were_written(
        self, ctx: Any, workspace: Path, monkeypatch: Any
    ) -> None:
        sentinel = workspace.parent / "secret.txt"
        sentinel.write_text("must not be touched", encoding="utf-8")

        def escaping_execute(name: str, arguments: Any) -> Any:
            if name != "write_file":
                raise AssertionError(f"unexpected tool {name}")
            sentinel.write_text(str(arguments.get("content")), encoding="utf-8")
            return ToolResult.success(name, "wrote", {})

        monkeypatch.setattr(ctx.tools, "execute", escaping_execute)
        acceptance.product_j_traversal(ctx, sentinel)
        item, status, _detail = last_result()
        assert item.startswith("J.")
        assert status == acceptance.FAIL
        assert sentinel.read_text(encoding="utf-8") == "pwned"
        sentinel.unlink(missing_ok=True)


class TestDisposableRepo:
    """G needs a real repository, without touching the developer's Git config."""

    def test_init_creates_a_repository_with_one_commit(self) -> None:
        workspace = acceptance.make_workspace()
        try:
            assert acceptance.init_disposable_repo(workspace) is None
            assert (workspace / ".git").is_dir()
            counted = acceptance._git(workspace, "rev-list", "--count", "HEAD")
            assert counted.stdout.strip() == "1"
        finally:
            shutil.rmtree(workspace, ignore_errors=True)

    def test_status_reports_the_branch(self, ctx: Any, workspace: Path) -> None:
        acceptance.init_disposable_repo(workspace)
        acceptance.product_g_git(ctx)
        item, status, detail = last_result()
        assert item.startswith("G.")
        assert status == acceptance.PASS, detail
        assert "branch='main'" in detail


class TestApprovalProbe:
    def test_records_requests_and_clears_grants(self, ctx: Any, workspace: Path) -> None:
        broker = ctx.tools.permissions
        with acceptance.approval_probe(ctx, ApprovalOutcome.ALLOW_ONCE) as requested:
            ctx.tools.execute("write_file", {"path": "probe.txt", "content": "hi"})
        assert requested == ["write_file"]
        assert broker.session_grants == frozenset()

    def test_session_grant_does_not_outlive_the_probe(self, ctx: Any, workspace: Path) -> None:
        broker = ctx.tools.permissions
        with acceptance.approval_probe(ctx, ApprovalOutcome.ALLOW_FOR_SESSION):
            ctx.tools.execute("write_file", {"path": "granted.txt", "content": "hi"})
        assert broker.session_grants == frozenset()


class TestToolVerdict:
    """Model observations must distinguish 'did not try' from 'failed'."""

    @staticmethod
    def _turn(tools: list[str]) -> acceptance.TurnResult:
        return acceptance.TurnResult(outcome=None, tools=tools, approvals=[])

    def test_used(self) -> None:
        assert acceptance.tool_verdict(self._turn(["run_command"]), "run_command") == "used"

    def test_failed(self) -> None:
        turn = self._turn(["run_command:failed"])
        assert acceptance.tool_verdict(turn, "run_command") == "failed"

    def test_not_called(self) -> None:
        turn = self._turn(["read_file"])
        assert acceptance.tool_verdict(turn, "run_command") == "not-called"


class TestEventSubscriptionHygiene:
    """``ask`` must not leave handlers attached to the shared event bus."""

    def test_ask_unsubscribes_after_each_turn(
        self, ctx: Any, workspace: Path, monkeypatch: Any
    ) -> None:
        class _Agent:
            def run(self, prompt: str) -> Any:
                from quantsmind_agent.runtime.agent.core import AgentOutcome

                return AgentOutcome(text="done")

        monkeypatch.setattr(ctx, "current_agent", lambda: _Agent())

        bus = ctx.events
        before = len(bus._any)

        for _ in range(3):
            acceptance.ask(ctx, "do something", ApprovalOutcome.ALLOW_ONCE)

        assert len(bus._any) == before, "event handlers accumulated across turns"

    def test_observations_belong_to_their_own_turn(
        self, ctx: Any, workspace: Path, monkeypatch: Any
    ) -> None:
        """A later turn must not see an earlier turn's tool events."""

        class _Agent:
            def __init__(self) -> None:
                self.calls = 0

            def run(self, prompt: str) -> Any:
                from quantsmind_agent.events import AgentEventType
                from quantsmind_agent.runtime.agent.core import AgentOutcome

                self.calls += 1
                if self.calls == 1:
                    ctx.events.emit(AgentEventType.TOOL_COMPLETED, {"tool": "read_file"})
                return AgentOutcome(text="done")

        agent = _Agent()
        monkeypatch.setattr(ctx, "current_agent", lambda: agent)

        first = acceptance.ask(ctx, "first", ApprovalOutcome.ALLOW_ONCE)
        second = acceptance.ask(ctx, "second", ApprovalOutcome.ALLOW_ONCE)

        assert first.tools == ["read_file"]
        assert second.tools == [], "the second turn inherited the first turn's events"


class TestResultCategories:
    """Deterministic, model and environment results must stay separable."""

    @staticmethod
    def _clear() -> None:
        acceptance.results.clear()

    def test_deterministic_checks_are_categorised_as_gates(self) -> None:
        self._clear()
        acceptance.check("X. deterministic", True)
        assert acceptance.results[-1].category == acceptance.DETERMINISTIC

    def test_model_observations_do_not_gate(self) -> None:
        self._clear()
        acceptance.check("X. deterministic", True)
        acceptance.observe("X. model", False)
        assert acceptance.results[-1].category == acceptance.MODEL
        # A failing observation must never turn the release red.
        assert acceptance._summarise() in (0, 2)

    def test_unreachable_provider_is_inconclusive_not_a_pass(self) -> None:
        self._clear()
        acceptance.check("X. deterministic", True)
        acceptance.environment("Z. model reachable", False, "401 unauthorized")
        entry = next(e for e in acceptance.results if e.category == acceptance.ENVIRONMENT)
        assert entry.status == acceptance.INCONCLUSIVE
        assert entry.status != acceptance.PASS
        # Deterministic checks passed, so the run is incomplete, not green.
        assert acceptance._summarise() == 2

    def test_failing_deterministic_check_blocks_the_release(self) -> None:
        self._clear()
        acceptance.check("X. deterministic", True)
        acceptance.check("Y. deterministic", False, "broke")
        assert acceptance._summarise() == 1

    def test_everything_passing_is_green(self) -> None:
        self._clear()
        acceptance.check("X. one", True)
        acceptance.check("Y. two", True)
        acceptance.environment("Z. model reachable", True, "reachable")
        assert acceptance._summarise() == 0


class TestPositiveControls:
    """A tool that refuses everything would pass every refusal-only check."""

    def test_permitted_changes_are_applied(self, ctx: Any, workspace: Path) -> None:
        acceptance.product_p_tools_apply_permitted_changes(ctx, workspace)
        item, status, detail = last_result()
        assert item.startswith("P.")
        assert status == acceptance.PASS, detail
        assert not (workspace / "acceptance_probe.txt").exists()

    def test_a_tool_that_no_ops_fails_the_positive_control(
        self, ctx: Any, workspace: Path, monkeypatch: Any
    ) -> None:
        from quantsmind_agent.runtime.tools import ToolResult

        monkeypatch.setattr(
            ctx.tools,
            "execute",
            lambda name, arguments: ToolResult.success(name, "did nothing", {}),
        )
        acceptance.product_p_tools_apply_permitted_changes(ctx, workspace)
        item, status, _detail = last_result()
        assert item.startswith("P.")
        assert status == acceptance.FAIL


class TestRunnableEntryPoint:
    """The script must actually run when executed.

    An earlier edit dropped the ``__main__`` guard, and the script then exited 0
    printing nothing: a green result for having done no work at all, which is
    the exact failure mode this module exists to rule out.
    """

    def test_main_guard_exists(self) -> None:
        source = (REPO / "scripts" / "acceptance_v0_2.py").read_text(encoding="utf-8")
        assert '__name__ == "__main__"' in source
        assert "raise SystemExit(main())" in source

    def test_script_emits_output_when_executed(self) -> None:
        """Run the script as a subprocess in its offline mode.

        This exercises the real ``__main__`` path end to end: a missing entry
        point would produce no output at all, which is what silently happened
        once already.
        """

        import os
        import subprocess

        env = dict(os.environ)
        env["PYTHONPATH"] = os.pathsep.join(
            [str(REPO / "src"), str(REPO / "scripts"), env.get("PYTHONPATH", "")]
        )
        completed = subprocess.run(
            [
                sys.executable,
                str(REPO / "scripts" / "acceptance_v0_2.py"),
                "--deterministic-only",
            ],
            capture_output=True,
            text=True,
            env=env,
            timeout=600,
            check=False,
        )
        output = completed.stdout
        assert "DETERMINISTIC acceptance checks" in output, output
        assert "REAL-MODEL integration observations" in output, output
        assert "ENVIRONMENT prerequisites" in output, output
        assert "6/6 deterministic acceptance checks passed" in output, output
        assert "--deterministic-only was used" in output, output
        assert completed.returncode == 0, output
