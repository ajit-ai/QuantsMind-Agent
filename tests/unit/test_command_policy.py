"""Unit tests for the command execution policy.

The policy is the security boundary that no user approval can override, so it is
tested exhaustively and separately from execution.
"""

from __future__ import annotations

import inspect

import pytest

from quantsmind_agent import command_policy
from quantsmind_agent.command_policy import CommandClass, classify_command, program_name


class TestProgramName:
    @pytest.mark.parametrize(
        ("argv0", "expected"),
        [
            ("ruff", "ruff"),
            ("./ruff", "ruff"),
            (r"C:\Python312\Scripts\ruff.exe", "ruff"),
            ("ruff.exe", "ruff"),
            ("ruff.cmd", "ruff"),
            ("/usr/bin/git", "git"),
            ('"quoted.exe"', "quoted"),
        ],
    )
    def test_normalises_to_a_bare_name(self, argv0: str, expected: str) -> None:
        assert program_name(argv0) == expected


class TestBlockedCommands:
    """Hard denies. None may be reached by any user approval."""

    @pytest.mark.parametrize(
        ("argv", "why"),
        [
            (["format", "C:"], "volume format"),
            (["format.com", "C:"], "volume format via .com"),
            ([r"C:\Windows\System32\format.exe", "C:"], "volume format by full path"),
            (["diskpart"], "disk management"),
            (["mkfs", "/dev/sda"], "filesystem creation"),
            (["fsck", "/dev/sda"], "raw filesystem repair"),
            (["dd", "if=/dev/zero", "of=/dev/sda"], "raw block copy"),
            (["cipher", "/w:C"], "data wipe"),
            (["shutdown", "/s", "/t", "0"], "host shutdown"),
            (["reboot"], "host restart"),
            (["systemctl", "stop", "sshd"], "service control"),
            (["sc", "stop", "sshd"], "windows service control"),
            (["reg", "add", "HKLM"], "registry write"),
            (["curl", "https://example.invalid/x"], "network transfer"),
            (["wget", "http://example.invalid/x"], "network transfer"),
            (["ssh", "host"], "remote shell"),
            (["scp", "a", "host:b"], "remote copy"),
            (["certutil", "-urlcache", "-f"], "downloader"),
            (["bitsadmin", "/transfer", "x"], "background download"),
            (["rundll32", "x.dll,Entry"], "dll loader"),
            (["cmdkey", "/list"], "credential access"),
            (["vaultcmd", "/listcreds"], "vault access"),
            (["net", "user"], "user administration"),
            (["runas", "/user:x"], "identity change"),
            (["docker", "run", "-v", "C:/:/mnt", "alpine"], "container host mount"),
            (["rm", "-rf", "/"], "filesystem root delete"),
            (["rm", "--no-preserve-root", "-rf", "/"], "root delete, explicit"),
            (["del", "C:\\*", "/s", "/f"], "drive-wide delete"),
            (["rmdir", "C:\\Windows", "/s", "/q"], "recursive drive delete"),
            (["npm", "publish"], "package publish"),
            (["twine", "upload", "dist/*"], "PyPI publish"),
            (["git", "push"], "git publish"),
            (["git", "commit", "-m", "x"], "git commit"),
            (["git", "reset", "--hard"], "git reset"),
            (["git", "checkout", "main"], "git checkout"),
            (["git", "switch", "main"], "git switch"),
            (["git", "rebase", "main"], "git rebase"),
            (["git", "merge", "x"], "git merge"),
            (["git", "clean", "-fd"], "git clean"),
            (["git", "branch", "-D", "main"], "git branch delete"),
            (["git", "branch", "-m", "x"], "git branch rename"),
            (["git", "stash"], "git stash"),
            (["git", "update-ref", "refs/heads/x"], "raw ref update"),
        ],
    )
    def test_blocked(self, argv: list[str], why: str) -> None:
        verdict = classify_command(argv, workspace_root=r"C:\project")
        assert verdict.command_class is CommandClass.BLOCKED, f"{argv} ({why}) not blocked"
        assert not verdict.permitted
        assert verdict.reason

    @pytest.mark.parametrize(
        ("argv", "why"),
        [
            (["Format-Volume", "-DriveLetter", "C"], "volume format cmdlet"),
            (["Clear-Disk", "-Number", "0"], "disk erase cmdlet"),
            (["Remove-Item", "-Recurse", "C:\\"], "drive root removal"),
            (["Set-ExecutionPolicy", "Bypass"], "execution policy change"),
            (["Set-MpPreference", "-DisableRealtimeMonitoring", "1"], "security policy"),
            (["Clear-EventLog", "-LogName", "Security"], "audit log clearing"),
            (["npm", "publish", "--access", "public"], "package publish"),
            (["poetry", "publish", "-b", "pypi"], "PyPI publish"),
            (["Invoke-Expression", "x"], "dynamic execution"),
            (["iex", "DownloadString x"], "dynamic execution"),
            (["Invoke-WebRequest", "-Uri", "x"], "in-shell download"),
            ([r"powershell", "-c", "FromBase64String('AAA')"], "encoded loader"),
        ],
    )
    def test_blocked_by_argument_shape(self, argv: list[str], why: str) -> None:
        verdict = classify_command(argv, workspace_root=r"C:\project")
        assert verdict.command_class is CommandClass.BLOCKED, f"{argv} ({why}) not blocked"

    def test_empty_command_is_blocked(self) -> None:
        assert classify_command([]).command_class is CommandClass.BLOCKED


class TestRiskClassification:
    """Commands that are asked rather than refused, with the right label."""

    @pytest.mark.parametrize(
        "argv",
        [
            ["git", "status"],
            ["git", "diff"],
            ["git", "log", "--oneline"],
            ["git", "branch"],
            ["git", "branch", "-a"],
            ["git", "show", "HEAD"],
        ],
    )
    def test_read_only_git_is_safe(self, argv: list[str]) -> None:
        verdict = classify_command(argv, workspace_root=r"C:\project")
        assert verdict.command_class is CommandClass.SAFE, argv
        assert verdict.permitted

    @pytest.mark.parametrize(
        "argv", [["bash", "-c", "ls"], ["powershell", "-Command", "ls"], ["sh", "-c", "ls"]]
    )
    def test_interpreters_are_destructive_but_askable(self, argv: list[str]) -> None:
        verdict = classify_command(argv)
        assert verdict.command_class is CommandClass.DESTRUCTIVE, argv
        assert verdict.permitted

    @pytest.mark.parametrize(
        "argv",
        [["python", "-c", "import os"], ["node", "-e", "x"], ["python", "--command", "x"]],
    )
    def test_inline_code_is_destructive(self, argv: list[str]) -> None:
        verdict = classify_command(argv)
        assert verdict.command_class is CommandClass.DESTRUCTIVE, argv

    def test_inline_code_flags_are_scoped_to_interpreters(self) -> None:
        # ``-e`` means "execute this string" for node but "editable install" for
        # pip. Treating the letters alike would mislabel ordinary dev commands.
        assert (
            classify_command(["pip", "install", "-e", "."]).command_class is CommandClass.MUTATING
        )
        assert classify_command(["node", "-e", "x"]).command_class is CommandClass.DESTRUCTIVE

    def test_recursive_delete_outside_the_workspace_is_destructive(self) -> None:
        verdict = classify_command(
            ["rm", "-rf", "C:\\Users\\me\\elsewhere"], workspace_root=r"C:\p"
        )
        assert verdict.command_class is CommandClass.DESTRUCTIVE
        assert verdict.permitted

    def test_absolute_path_outside_the_workspace_is_destructive(self) -> None:
        verdict = classify_command(["python", "C:\\Windows\\evil.py"], workspace_root=r"C:\p")
        assert verdict.command_class is CommandClass.DESTRUCTIVE

    def test_absolute_path_inside_the_workspace_is_not_escalated(self) -> None:
        verdict = classify_command(["python", r"C:\p\tests.py"], workspace_root=r"C:\p")
        assert verdict.command_class is not CommandClass.DESTRUCTIVE

    def test_unknown_program_is_asked_not_blocked(self) -> None:
        verdict = classify_command(["someunknowntool", "--x"])
        assert verdict.command_class is CommandClass.UNKNOWN
        assert verdict.permitted

    @pytest.mark.parametrize(
        ("argv", "expected"),
        [
            (["python", "-m", "pytest", "-q"], CommandClass.SAFE),
            (["ruff", "check", "."], CommandClass.SAFE),
            (["mypy", "src"], CommandClass.SAFE),
            (["pip", "install", "-e", "."], CommandClass.MUTATING),
            (["rm", "-rf", "build"], CommandClass.MUTATING),
        ],
    )
    def test_classification(self, argv: list[str], expected: CommandClass) -> None:
        assert classify_command(argv, workspace_root=r"C:\p").command_class is expected

    def test_every_class_has_a_risk_label(self) -> None:
        for argv in (["git", "status"], ["rm", "-rf", "x"], ["sh", "-c", "x"], ["whatever"]):
            verdict = classify_command(argv, workspace_root=r"C:\p")
            assert verdict.risk_label

    def test_policy_is_documented_as_not_a_sandbox(self) -> None:
        text = inspect.getdoc(command_policy) or ""
        assert "not** an OS sandbox" in text
        assert "shell=False" in text
