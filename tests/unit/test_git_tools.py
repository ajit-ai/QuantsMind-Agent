"""Tests for the read-only Git tools, against a real temporary repository."""

from __future__ import annotations

import inspect
import subprocess
from pathlib import Path
from typing import Any
from unittest import mock

import pytest

from quantsmind_agent.git_tools import build_git_tools
from quantsmind_agent.permissions import (
    ApprovalOutcome,
    Decision,
    Permission,
    PermissionBroker,
    PermissionRule,
    StaticPermissionProvider,
)
from quantsmind_agent.tools import ToolRegistry
from quantsmind_agent.workspace import Workspace


def _git(root: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(root), *args],
        check=True,
        capture_output=True,
        text=True,
    )


def _registry(
    project: Path, *, outcome: ApprovalOutcome | None = ApprovalOutcome.ALLOW_ONCE
) -> ToolRegistry:
    broker = PermissionBroker(
        StaticPermissionProvider.for_v0_2(),
        approver=(lambda request: outcome) if outcome is not None else None,
    )
    registry = ToolRegistry(permissions=broker)
    registry.register_all(build_git_tools(Workspace(project)))
    return registry


@pytest.fixture(scope="module")
def git_available() -> None:
    try:
        subprocess.run(["git", "--version"], check=True, capture_output=True)
    except (OSError, subprocess.CalledProcessError):  # pragma: no cover - environment
        pytest.skip("git is not available on this machine")


@pytest.fixture
def repo(tmp_path_factory: pytest.TempPathFactory, git_available: None) -> Path:
    """A small repository with one commit, for the read-only tools to inspect."""

    root = tmp_path_factory.mktemp("repo")
    (root / "a.txt").write_text("one\ntwo\n")
    (root / "b.txt").write_text("beta\n")
    _git(root, "init", "-b", "main")
    _git(root, "config", "user.email", "test@example.invalid")
    _git(root, "config", "user.name", "QuantsMind Test")
    _git(root, "config", "commit.gpgsign", "false")
    _git(root, "add", ".")
    _git(root, "commit", "-m", "initial commit")
    return root


@pytest.fixture
def git_tools(repo: Path) -> ToolRegistry:
    return _registry(repo)


@pytest.fixture
def dirty_repo(repo: Path) -> Path:
    """The repository with one staged, one unstaged and one untracked change."""

    (repo / "a.txt").write_text("one\nTWO\n")
    (repo / "b.txt").write_text("beta changed\n")
    _git(repo, "add", "b.txt")
    (repo / "c.txt").write_text("untracked\n")
    return repo


class TestGitStatus:
    def test_clean_tree(self, git_tools: ToolRegistry) -> None:
        result = git_tools.execute("git_status", {})
        assert result.ok
        assert result.data["branch"] == "main"
        assert result.data["clean"] is True
        assert result.data["staged"] == []
        assert result.data["unstaged"] == []

    def test_reports_every_kind_of_change(self, dirty_repo: Path) -> None:
        result = _registry(dirty_repo).execute("git_status", {})
        assert result.ok
        assert result.data["branch"] == "main"
        assert result.data["clean"] is False
        assert [item["path"] for item in result.data["staged"]] == ["b.txt"]
        assert [item["path"] for item in result.data["unstaged"]] == ["a.txt"]
        assert result.data["untracked"] == ["c.txt"]

    def test_classifies_the_two_change_columns(self, dirty_repo: Path) -> None:
        result = _registry(dirty_repo).execute("git_status", {})
        staged = result.data["staged"][0]
        assert staged["staged"] is True
        assert staged["unstaged"] is False


class TestGitDiff:
    def test_working_tree_diff(self, dirty_repo: Path) -> None:
        result = _registry(dirty_repo).execute("git_diff", {})
        assert result.ok
        assert result.data["file_count"] == 1
        assert result.data["files"][0]["path"] == "a.txt"
        assert result.data["lines_added"] == 1
        assert result.data["lines_removed"] == 1
        assert "TWO" in result.data["patch"]

    def test_staged_diff(self, dirty_repo: Path) -> None:
        result = _registry(dirty_repo).execute("git_diff", {"staged": True})
        assert result.data["staged"] is True
        assert [item["path"] for item in result.data["files"]] == ["b.txt"]

    def test_patch_can_be_omitted(self, dirty_repo: Path) -> None:
        result = _registry(dirty_repo).execute("git_diff", {"include_patch": False})
        assert result.ok
        assert result.data["patch"] == ""

    def test_scoped_to_one_path(self, dirty_repo: Path) -> None:
        result = _registry(dirty_repo).execute("git_diff", {"path": "a.txt"})
        assert result.data["scope"] == "a.txt"
        assert [item["path"] for item in result.data["files"]] == ["a.txt"]

    def test_no_changes_reports_zero(self, git_tools: ToolRegistry) -> None:
        result = git_tools.execute("git_diff", {})
        assert result.ok
        assert result.data["file_count"] == 0


class TestGitLog:
    def test_lists_commits(self, git_tools: ToolRegistry) -> None:
        result = git_tools.execute("git_log", {"limit": 5})
        assert result.ok
        assert result.data["count"] >= 1
        first = result.data["commits"][0]
        assert first["subject"] == "initial commit"
        assert first["author"] == "QuantsMind Test"
        assert first["hash"]
        assert first["date"]

    def test_limit_is_honoured_and_clamped(self, git_tools: ToolRegistry) -> None:
        assert git_tools.execute("git_log", {"limit": 1}).data["count"] == 1
        assert git_tools.execute("git_log", {"limit": 99_999}).data["count"] >= 1
        assert git_tools.execute("git_log", {"limit": -5}).data["count"] >= 1

    def test_scoped_to_one_path(self, git_tools: ToolRegistry) -> None:
        result = git_tools.execute("git_log", {"limit": 5, "path": "a.txt"})
        assert result.data["scope"] == "a.txt"
        assert result.data["count"] == 1


class TestNonRepository:
    """A folder that is not a repository must fail clearly, not crash."""

    @pytest.fixture
    def plain(self, tmp_path: Path) -> Path:
        folder = tmp_path / "plain"
        folder.mkdir()
        (folder / "x.txt").write_text("hi")
        return folder

    @pytest.mark.parametrize("tool", ["git_status", "git_diff", "git_log"])
    def test_reports_not_a_repository(self, plain: Path, tool: str) -> None:
        result = _registry(plain).execute(tool, {})
        assert not result.ok
        assert "not a Git repository" in (result.error or "")


class TestContainmentAndPermissions:
    @pytest.mark.parametrize("bad_path", ["../../etc", "..", "C:/Windows"])
    def test_path_scope_cannot_escape(self, git_tools: ToolRegistry, bad_path: str) -> None:
        assert not git_tools.execute("git_diff", {"path": bad_path}).ok
        assert not git_tools.execute("git_log", {"path": bad_path}).ok

    def test_git_permissions_are_enforced(self, repo: Path) -> None:
        """The declared GIT permission must actually gate the tools.

        The shipped policy allows GIT because these tools cannot mutate, so
        enforcement is proven by substituting a policy that asks or denies.
        """

        for decision in (Decision.DENY, Decision.ASK):
            provider = StaticPermissionProvider()
            provider.add_rule(PermissionRule(Permission.GIT, decision))
            broker = PermissionBroker(provider, approver=lambda request: ApprovalOutcome.DENY)
            registry = ToolRegistry(permissions=broker)
            registry.register_all(build_git_tools(Workspace(repo)))
            result = registry.execute("git_status", {})
            assert not result.ok, f"GIT={decision} was not enforced"
            assert "not permitted" in (result.error or "")

    def test_no_approver_means_denied_when_the_policy_asks(self, repo: Path) -> None:
        provider = StaticPermissionProvider()
        provider.add_rule(PermissionRule(Permission.GIT, Decision.ASK))
        registry = ToolRegistry(permissions=PermissionBroker(provider, approver=None))
        registry.register_all(build_git_tools(Workspace(repo)))
        assert not registry.execute("git_status", {}).ok

    def test_tools_declare_the_git_permission_and_are_read_only(self, repo: Path) -> None:
        for tool in build_git_tools(Workspace(repo)):
            assert tool.spec.permission is Permission.GIT
            assert tool.spec.read_only is True


class TestNoMutationCapability:
    """Mutation is structurally impossible, not merely disallowed.

    These tests assert on the source rather than on runtime behaviour: the Git
    tools build their argv from a fixed read-only vocabulary, so a mutation verb
    is not merely rejected, it is unspellable.
    """

    #: Verbs that would change repository state.
    MUTATION_VERBS = (
        "commit",
        "push",
        "checkout",
        "switch",
        "reset",
        "merge",
        "rebase",
        "restore",
        "clean",
        "stash",
        "cherry-pick",
        "fetch",
    )

    def test_no_mutation_verb_is_spellable_in_the_module(self) -> None:
        """No non-docstring literal in git_tools.py names a mutation verb.

        Two kinds of literal are excluded because they cannot become argv:

        * docstrings, which legitimately *describe* why those verbs are absent;
        * mapping keys, e.g. the ``"clean"`` key of the status result -- a
          mutation verb as a data field name says nothing about what the tool
          executes.

        What remains is every literal that could reach a command line.
        """

        import ast

        from quantsmind_agent import git_tools

        tree = ast.parse(inspect.getsource(git_tools))

        excluded: set[int] = set()

        # Docstrings: the first statement of a module, class or function.
        for node in ast.walk(tree):
            if not isinstance(
                node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
            ):
                continue
            body = getattr(node, "body", [])
            if (
                body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)
            ):
                excluded.add(id(body[0].value))

        # Mapping keys, which are field names rather than arguments.
        for node in ast.walk(tree):
            if isinstance(node, ast.Dict):
                excluded.update(id(key) for key in node.keys if key is not None)

        literals = {
            node.value
            for node in ast.walk(tree)
            if isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and id(node) not in excluded
        }
        for verb in self.MUTATION_VERBS:
            assert verb not in literals, f"git_tools.py can spell git {verb}"

    def test_only_read_only_subcommands_are_ever_issued(self, repo: Path) -> None:
        """Whatever the tools run, the sub-command is always read-only."""

        from quantsmind_agent.git_tools import _GitRunner

        issued: list[str] = []
        original = _GitRunner.run

        def spy(self: _GitRunner, arguments: list[str], *, check_repo: bool = True) -> Any:
            if arguments:
                issued.append(arguments[0])
            return original(self, arguments, check_repo=check_repo)

        broker = PermissionBroker(
            StaticPermissionProvider.for_v0_2(),
            approver=lambda request: ApprovalOutcome.ALLOW_ONCE,
        )
        registry = ToolRegistry(permissions=broker)
        registry.register_all(build_git_tools(Workspace(repo)))
        with mock.patch.object(_GitRunner, "run", spy):
            for name in ("git_status", "git_diff", "git_log"):
                registry.execute(name, {})
                registry.execute(name, {"path": "a.txt", "staged": True, "limit": 3})

        assert issued, "the spy observed no git invocation"
        assert set(issued) <= {"rev-parse", "status", "diff", "log"}
        assert set(issued) & set(self.MUTATION_VERBS) == set()
