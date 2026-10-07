"""Read-only Git awareness tools.

``git_status``
    What is staged, what is unstaged, what is untracked, and which branch is
    checked out.
``git_diff``
    Line-level changes, optionally limited to the staged set or to a path.
``git_log``
    Recent commits, optionally limited to one path.

v0.2 exposes no Git mutation tool. That restriction is enforced two ways:

* **No mutating verb is reachable.** Every argv vector is assembled here from a
  fixed vocabulary of read-only sub-commands. The model chooses flags and
  optional paths, never the sub-command, so ``commit``, ``push``, ``checkout``,
  ``reset``, ``merge`` and ``rebase`` are not spelled anywhere in this module.
* **Argument injection is impossible.** The model never supplies a program name
  and never supplies a free-form argument string. A ``--`` separator is always
  inserted before a user path, so a path beginning with ``-`` is a path and not a
  flag, and cannot be read as an option.

Both properties survive even a fully adversarial model, which is why these tools
do not need the command policy's deny list: there is simply nothing here that
changes the repository.
"""

from __future__ import annotations

import contextlib
import subprocess
import sys
from typing import Any

from quantsmind_agent.command_tools import CappedReader, sanitized_environment
from quantsmind_agent.errors import GitError, InvalidPathError
from quantsmind_agent.logging_setup import get_logger
from quantsmind_agent.permissions import Permission
from quantsmind_agent.tools import ToolResult, ToolSpec
from quantsmind_agent.workspace import Workspace

__all__ = ["GitDiffTool", "GitLogTool", "GitStatusTool", "build_git_tools"]

logger = get_logger(__name__)

MAX_OUTPUT_CHARS = 60_000
DEFAULT_TIMEOUT = 30.0
MAX_LOG_ENTRIES = 200
MAX_DIFF_PATHS = 400


class _GitRunner:
    """Runs one fixed, read-only Git sub-command inside the workspace."""

    def __init__(self, workspace: Workspace, *, timeout: float = DEFAULT_TIMEOUT) -> None:
        self._workspace = workspace
        self._timeout = timeout

    def run(self, arguments: list[str], *, check_repo: bool = True) -> tuple[int, str, str]:
        """Return ``(exit_code, stdout, stderr)`` for a read-only invocation."""

        if check_repo:
            self._require_repository()

        argv = ["git", "-C", str(self._workspace.root), *arguments]
        creation_flags = (
            getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0
        )
        try:
            process = subprocess.Popen(
                argv,
                cwd=str(self._workspace.root),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                stdin=subprocess.DEVNULL,
                shell=False,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=sanitized_environment(),
                creationflags=creation_flags,
            )
        except FileNotFoundError as exc:
            raise GitError(
                "git is not installed or not on PATH",
                user_message=(
                    "Git is not available on this machine, so the repository cannot be "
                    "inspected. Install Git, or continue without Git awareness."
                ),
                details={"command": "git"},
            ) from exc
        except OSError as exc:
            raise GitError(
                f"git could not be started: {exc}",
                user_message="Git could not be started.",
                details={"command": "git"},
            ) from exc

        out = CappedReader(process.stdout, MAX_OUTPUT_CHARS)
        err = CappedReader(process.stderr, MAX_OUTPUT_CHARS)
        out.start()
        err.start()
        try:
            process.wait(timeout=self._timeout)
        except subprocess.TimeoutExpired:
            with contextlib.suppress(OSError):
                process.kill()
            raise GitError(
                f"'git {' '.join(arguments)}' did not finish in {self._timeout:g}s",
                user_message="Git took too long to answer and was stopped.",
                details={"command": " ".join(argv), "timeout_seconds": self._timeout},
            ) from None
        out.join(timeout=2)
        err.join(timeout=2)
        return int(process.returncode or 0), out.text, err.text

    def _require_repository(self) -> None:
        """Fail clearly when the workspace is not inside a Git repository."""

        code, stdout, _ = self.run(
            ["rev-parse", "--is-inside-work-tree"],
            check_repo=False,
        )
        if code != 0 or stdout.strip().lower() != "true":
            raise GitError(
                "the workspace is not inside a Git repository",
                user_message=(
                    "This folder is not a Git repository, so there is no repository "
                    "state to report."
                ),
                details={"root": str(self._workspace.root)},
            )

    def scope(self, raw: str | None) -> str | None:
        """Validate an optional workspace-relative path scope."""

        if not raw or raw == ".":
            return None
        try:
            return self._workspace.relative(raw)
        except InvalidPathError as exc:
            raise InvalidPathError(
                f"git path rejected: {raw!r}",
                user_message=(
                    f"'{raw}' is outside the open project, so it cannot be used to "
                    "limit this Git query."
                ),
                details={"path": str(raw), "reason": exc.details.get("reason")},
            ) from exc


class GitStatusTool:
    """Report branch, staged, unstaged and untracked state."""

    def __init__(self, workspace: Workspace, *, timeout: float = DEFAULT_TIMEOUT) -> None:
        self._git = _GitRunner(workspace, timeout=timeout)

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="git_status",
            description=(
                "Report the Git state of the project folder: the current branch, what "
                "is staged, what is modified but not staged, and what is untracked. "
                "Read-only; this cannot change the repository."
            ),
            input_schema={
                "type": "object",
                "properties": {},
                "required": [],
                "additionalProperties": False,
            },
            permission=Permission.GIT,
            summary_template="git status",
        )

    def run(self, arguments: dict[str, Any]) -> ToolResult:
        code, stdout, stderr = self._git.run(
            ["status", "--porcelain=v1", "--branch", "--untracked-files=normal"]
        )
        if code != 0:
            raise GitError(
                f"git status failed: {stderr.strip() or 'unknown error'}",
                user_message="Git could not report the repository status.",
                details={"exit_code": code},
            )

        branch = ""
        upstream = ""
        ahead = 0
        behind = 0
        staged: list[dict[str, Any]] = []
        unstaged: list[dict[str, Any]] = []
        untracked: list[str] = []

        for line in stdout.splitlines():
            if line.startswith("##"):
                meta = line[2:].strip()
                if "..." in meta:
                    branch, _, upstream = meta.partition("...")
                    upstream = upstream.strip()
                else:
                    branch = meta
                for token in meta.replace("...", " ").split():
                    if token.startswith("ahead"):
                        ahead = int(token.rstrip(")")[5:] or 0)
                    elif token.startswith("behind"):
                        behind = int(token.rstrip(")")[6:] or 0)
                continue
            if len(line) < 3:
                continue
            index, worktree, path = line[0], line[1], line[3:].strip()
            entry = {
                "status": f"{index}{worktree}",
                "path": path,
                "staged": index not in (" ", "?"),
                "unstaged": worktree not in (" ", ""),
            }
            if index == "?" and worktree == "?":
                untracked.append(path)
            else:
                if entry["staged"]:
                    staged.append(entry)
                if entry["unstaged"]:
                    unstaged.append(entry)

        clean = not staged and not unstaged and not untracked
        return ToolResult.success(
            self.spec.name,
            f"on branch {branch or '(unknown)'}: "
            f"{len(staged)} staged, {len(unstaged)} modified, {len(untracked)} untracked",
            {
                "branch": branch,
                "upstream": upstream or None,
                "ahead": ahead,
                "behind": behind,
                "clean": clean,
                "staged": staged,
                "unstaged": unstaged,
                "untracked": untracked,
            },
        )


class GitDiffTool:
    """Report line-level changes, working tree or staged."""

    def __init__(self, workspace: Workspace, *, timeout: float = DEFAULT_TIMEOUT) -> None:
        self._git = _GitRunner(workspace, timeout=timeout)

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="git_diff",
            description=(
                "Show what changed in the project: added and removed lines per file, "
                "plus the patch itself. Use staged=true for changes that are already "
                "added to the index. Read-only; this cannot change the repository."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "staged": {
                        "type": "boolean",
                        "description": "Diff the index against HEAD instead of the working tree.",
                        "default": False,
                    },
                    "path": {
                        "type": "string",
                        "description": "Workspace-relative file or folder to limit the diff to.",
                    },
                    "include_patch": {
                        "type": "boolean",
                        "description": "Include the unified diff text. Default true.",
                        "default": True,
                    },
                },
                "required": [],
                "additionalProperties": False,
            },
            permission=Permission.GIT,
            summary_template="git diff{staged}",
        )

    def run(self, arguments: dict[str, Any]) -> ToolResult:
        staged = bool(arguments.get("staged", False))
        scope = self._git.scope(
            arguments.get("path") if isinstance(arguments.get("path"), str) else None
        )
        include_patch = arguments.get("include_patch", True) is not False

        base: list[str] = ["diff"]
        if staged:
            base.append("--staged")

        code, numstat, stderr = self._git.run(
            [*base, "--numstat", "--", *([scope] if scope else [])]
        )
        if code != 0:
            raise GitError(
                f"git diff failed: {stderr.strip() or 'unknown error'}",
                user_message="Git could not report the changes.",
                details={"exit_code": code},
            )

        files: list[dict[str, Any]] = []
        for line in numstat.splitlines():
            parts = line.split("\t")
            if len(parts) < 3:
                continue
            added, removed, path = parts[0], parts[1], parts[2]
            files.append(
                {
                    "path": path,
                    "added": int(added) if added.isdigit() else None,
                    "removed": int(removed) if removed.isdigit() else None,
                    "binary": not (added.isdigit() and removed.isdigit()),
                }
            )
            if len(files) >= MAX_DIFF_PATHS:
                break

        patch = ""
        if include_patch:
            code, patch, stderr = self._git.run([*base, "--", *([scope] if scope else [])])
            if code != 0:
                raise GitError(
                    f"git diff failed: {stderr.strip() or 'unknown error'}",
                    user_message="Git could not produce the patch.",
                    details={"exit_code": code},
                )

        total_added = sum(item["added"] or 0 for item in files)
        total_removed = sum(item["removed"] or 0 for item in files)
        return ToolResult.success(
            self.spec.name,
            f"{len(files)} file(s) changed, +{total_added}/-{total_removed}"
            + (" (staged)" if staged else ""),
            {
                "staged": staged,
                "scope": scope,
                "file_count": len(files),
                "lines_added": total_added,
                "lines_removed": total_removed,
                "files": files,
                "patch": patch,
                "patch_included": include_patch,
            },
        )


class GitLogTool:
    """Report recent commits, optionally for one path."""

    def __init__(self, workspace: Workspace, *, timeout: float = DEFAULT_TIMEOUT) -> None:
        self._git = _GitRunner(workspace, timeout=timeout)

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="git_log",
            description=(
                "List recent commits: hash, author, date and subject. Limit to one path "
                "with 'path' to see the history of a single file. Read-only; this "
                "cannot change the repository."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "limit": {
                        "type": "integer",
                        "description": f"How many commits to return (1-{MAX_LOG_ENTRIES}).",
                        "default": 10,
                    },
                    "path": {
                        "type": "string",
                        "description": "Workspace-relative file or folder to limit history to.",
                    },
                },
                "required": [],
                "additionalProperties": False,
            },
            permission=Permission.GIT,
            summary_template="git log (last {limit})",
        )

    def run(self, arguments: dict[str, Any]) -> ToolResult:
        raw_limit = arguments.get("limit", 10)
        limit = (
            10
            if isinstance(raw_limit, bool) or not isinstance(raw_limit, (int, float))
            else max(1, min(int(raw_limit), MAX_LOG_ENTRIES))
        )
        scope = self._git.scope(
            arguments.get("path") if isinstance(arguments.get("path"), str) else None
        )

        code, stdout, stderr = self._git.run(
            [
                "log",
                f"-{limit}",
                "--date=iso-strict",
                "--pretty=format:%h%x1f%an%x1f%ad%x1f%s",
                "--",
                *([scope] if scope else []),
            ]
        )
        if code != 0:
            raise GitError(
                f"git log failed: {stderr.strip() or 'unknown error'}",
                user_message="Git could not report the commit history.",
                details={"exit_code": code},
            )

        commits: list[dict[str, str]] = []
        for line in stdout.splitlines():
            parts = line.split("\x1f")
            if len(parts) < 4:
                continue
            commits.append(
                {"hash": parts[0], "author": parts[1], "date": parts[2], "subject": parts[3]}
            )

        return ToolResult.success(
            self.spec.name,
            f"{len(commits)} commit(s)" + (f" touching {scope}" if scope else ""),
            {"count": len(commits), "scope": scope, "commits": commits},
        )


def build_git_tools(workspace: Workspace, *, timeout: float = DEFAULT_TIMEOUT) -> list[Any]:
    """Instantiate the read-only Git tools bound to ``workspace``."""

    return [
        GitStatusTool(workspace, timeout=timeout),
        GitDiffTool(workspace, timeout=timeout),
        GitLogTool(workspace, timeout=timeout),
    ]
