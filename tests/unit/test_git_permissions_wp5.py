"""Stage/unstage/commit require explicit approval (WP5)."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from quantsmind_agent.git_tools import build_git_tools
from quantsmind_agent.runtime.tools import ToolRegistry
from quantsmind_agent.security.permissions import (
    ApprovalOutcome,
    Decision,
    Permission,
    PermissionBroker,
    PermissionRule,
    StaticPermissionProvider,
)
from quantsmind_agent.services.workspace.containment import Workspace


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True, text=True)


@pytest.fixture
def repo(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("repo")
    (root / "a.txt").write_text("one\n")
    _git(root, "init", "-b", "main")
    _git(root, "config", "user.email", "t@example.invalid")
    _git(root, "config", "user.name", "T")
    _git(root, "config", "commit.gpgsign", "false")
    _git(root, "add", ".")
    _git(root, "commit", "-m", "initial")
    (root / "a.txt").write_text("two\n")
    (root / "b.txt").write_text("new\n")
    return root


def test_mutation_tools_declare_execute_command(repo: Path) -> None:
    for tool in build_git_tools(Workspace(repo)):
        if tool.spec.name in ("git_stage", "git_unstage", "git_commit"):
            assert tool.spec.permission is Permission.EXECUTE_COMMAND
            assert tool.spec.read_only is False
        else:
            assert tool.spec.permission is Permission.GIT
            assert tool.spec.read_only is True


def _ask_registry(repo: Path, outcome: ApprovalOutcome | None) -> ToolRegistry:
    provider = StaticPermissionProvider()
    provider.add_rule(PermissionRule(Permission.GIT, Decision.ALLOW))
    provider.add_rule(PermissionRule(Permission.EXECUTE_COMMAND, Decision.ASK))
    broker = PermissionBroker(provider, approver=(lambda r: outcome) if outcome else None)
    reg = ToolRegistry(permissions=broker)
    reg.register_all(build_git_tools(Workspace(repo)))
    return reg


@pytest.mark.parametrize(
    "tool,args",
    [
        ("git_stage", {"paths": ["a.txt"]}),
        ("git_unstage", {"paths": ["a.txt"]}),
        ("git_commit", {"message": "x"}),
    ],
)
def test_stage_unstage_commit_denied_without_approval(repo: Path, tool: str, args: dict) -> None:
    result = _ask_registry(repo, ApprovalOutcome.DENY).execute(tool, args)
    assert not result.ok


def test_stage_unstage_commit_denied_without_approver(repo: Path) -> None:
    reg = _ask_registry(repo, None)
    assert not reg.execute("git_stage", {"paths": ["a.txt"]}).ok
    assert not reg.execute("git_unstage", {"paths": ["a.txt"]}).ok
    assert not reg.execute("git_commit", {"message": "x"}).ok


def test_stage_and_unstage_with_approval(repo: Path) -> None:
    reg = _ask_registry(repo, ApprovalOutcome.ALLOW_ONCE)
    r = reg.execute("git_stage", {"paths": ["a.txt"]})
    assert r.ok
    r2 = reg.execute("git_unstage", {"paths": ["a.txt"]})
    assert r2.ok
    # Unrelated file b.txt remains untracked/unstaged after staging a.txt only.
    status = reg.execute("git_status", {})
    assert status.ok
    staged_paths = [e["path"] for e in status.data["staged"]]
    assert "b.txt" not in staged_paths


def test_no_push_or_no_verify_in_source() -> None:
    import ast
    import inspect

    from quantsmind_agent import git_tools

    tree = ast.parse(inspect.getsource(git_tools))
    literals = {
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    }
    # Literals that could reach argv; docstrings legitimately describe absence.
    doc_lits = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = getattr(node, "body", [])
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                doc_lits.add(body[0].value.value)
    code_lits = literals - doc_lits
    assert "push" not in code_lits
    assert "--no-verify" not in code_lits
