"""Real acceptance run for QuantsMind-Agent v0.2.

Drives the real application stack (real provider, real credential, real tools)
against a disposable workspace, with approvals supplied programmatically in
place of the GUI dialog.

The credential is resolved through the normal Windows Credential Manager path;
it is never read, printed or logged here. Configuration is copied into an
isolated directory so the developer's real profile is never modified.

Two kinds of check are reported, and they answer different questions:

* **Product checks** call the registered tools directly through
  ``ctx.tools.execute``. They assert that a specific tool behaved correctly and
  are the acceptance criteria: a failure here is a defect.
* **Model checks** ask the real model to do the same thing and record which
  tools it chose. They measure model tool selection, not product correctness,
  so they are reported as observations and never decide the exit code.

The distinction matters. A product check that passes only because a tool was
never invoked proves nothing, and a model check that fails because the model
picked a different tool is not a defect.

Usage::

    python scripts/acceptance_v0_2.py
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterator

    from quantsmind_agent.app import ApplicationContext
    from quantsmind_agent.events import AgentEvent
    from quantsmind_agent.persistence.paths import AppPaths
    from quantsmind_agent.security.permissions import ApprovalOutcome, PermissionRequest

REPO = Path(__file__).resolve().parents[1]

PASS, FAIL = "PASS", "FAIL"
#: The check could not be evaluated because the environment prevented it.
INCONCLUSIVE = "INCONCLUSIVE"

#: Categories, which answer different questions and are reported separately.
#:
#: ``DETERMINISTIC`` checks drive a registered tool directly and assert a
#: product invariant. They are the acceptance criteria: they do not depend on a
#: model choosing to call anything, and only a failure here is a defect.
#:
#: ``MODEL`` checks measure real-model tool selection. A model that declines to
#: call a tool has not demonstrated anything about the tool, so these never gate.
#:
#: ``ENVIRONMENT`` checks cover credentials, provider reachability and other
#: external prerequisites. They are reported as inconclusive rather than passed,
#: because the application cannot be judged by them either way.
DETERMINISTIC, MODEL, ENVIRONMENT = "deterministic", "model", "environment"


@dataclass(slots=True)
class Result:
    """One recorded acceptance result, with the category it belongs to."""

    item: str
    status: str
    detail: str
    category: str


results: list[Result] = []

#: Text used as an ``old_text`` that is guaranteed not to be in ``calc.py``.
#: It is long and specific on purpose: a short guess such as ``"old"`` could
#: plausibly match the fixture and quietly turn the stale-edit check into a
#: test of nothing.
ABSENT_TEXT = "this exact sentence is not present in calc.py at all"

#: Set by ``--deterministic-only``: run the offline checks and stop, without
#: contacting a provider. Useful for iterating on a deterministic check without
#: spending credits, and it is explicitly *not* a release verification.
DETERMINISTIC_ONLY = "--deterministic-only" in sys.argv[1:]


def bootstrap() -> None:
    """Put ``src`` on the path and import the application lazily."""

    if str(REPO / "src") not in sys.path:
        sys.path.insert(0, str(REPO / "src"))


def record(item: str, status: str, detail: str = "", category: str = DETERMINISTIC) -> None:
    results.append(Result(item, status, detail, category))
    print(f"[{status}] {item}" + (f" -- {detail}" if detail else ""), flush=True)


@dataclass(slots=True)
class TurnResult:
    """Outcome of one real turn, plus the tools it used and asked about."""

    outcome: object
    tools: list[str]
    approvals: list[str]


def check(name: str, condition: bool, detail: str = "") -> None:
    """Record a deterministic acceptance check. These decide the exit code."""

    record(name, PASS if condition else FAIL, detail, DETERMINISTIC)


def observe(name: str, condition: bool, detail: str = "") -> None:
    """Record a real-model tool-selection observation. These decide nothing.

    They describe what the model chose to do. Treating them as acceptance
    criteria would either punish the model for picking a different tool or,
    worse, let a check "pass" because the model never tried anything.
    """

    record(name, PASS if condition else FAIL, detail, MODEL)


def environment(name: str, ok: bool, detail: str = "") -> None:
    """Record an environment-dependent prerequisite check.

    An unreachable provider is neither an application pass nor an application
    failure, so it is recorded as inconclusive and never counted as passing.
    """

    record(name, PASS if ok else INCONCLUSIVE, detail, ENVIRONMENT)


def tool_verdict(turn: TurnResult, tool: str) -> str:
    """Classify what a turn did with ``tool``.

    Separating these three is the point: ``not-called`` means the model did
    something else and says nothing about the tool, ``failed`` means the tool
    was called and refused or errored, and ``used`` means it succeeded.
    """

    if tool in turn.tools:
        return "used"
    if f"{tool}:failed" in turn.tools:
        return "failed"
    return "not-called"


@contextmanager
def approval_probe(ctx: ApplicationContext, outcome: ApprovalOutcome) -> Iterator[list[str]]:
    """Approve on demand and record every approval actually requested.

    ``PermissionBroker`` consults the safety policy *before* asking for
    approval, so a policy-blocked command must leave this list empty. Asserting
    that emptiness is therefore a real statement about the product: the command
    was refused by policy and never reached a human prompt, rather than being
    asked about and then answered.

    Session grants are cleared on both sides so no check inherits another's
    approval.
    """

    requested: list[str] = []
    broker = ctx.tools.permissions
    broker.clear_session_grants()
    broker.set_approver(lambda request: outcome)
    broker.set_on_approval(lambda request, decision: requested.append(request.tool))
    try:
        yield requested
    finally:
        broker.set_approver(None)
        broker.set_on_approval(None)
        broker.clear_session_grants()


def preflight(ctx: ApplicationContext) -> str | None:
    """Return None if the model is reachable, else a blocking reason.

    Without this, a dead credential turns every check into a misleading pass.
    """

    probe = ctx.current_agent()
    result = probe.run("Reply with the single word: ready.")
    if result.error is None:
        return None
    reason: str = result.error
    return reason


def make_workspace() -> Path:
    root = Path(tempfile.mkdtemp(prefix="qm-accept-v02-"))
    (root / "calc.py").write_text(
        textwrap.dedent(
            """\
            \"\"\"A tiny module with a deliberate bug.\"\"\"


            def add(a: int, b: int) -> int:
                return a - b
            """
        ),
        encoding="utf-8",
    )
    (root / "test_calc.py").write_text(
        textwrap.dedent(
            """\
            from calc import add


            def test_add() -> None:
                assert add(1, 2) == 3
            """
        ),
        encoding="utf-8",
    )
    (root / "README.md").write_text("# Acceptance workspace\n", encoding="utf-8")
    return root


def _isolated_paths() -> AppPaths:
    """Copy the developer's config into a temp profile, keeping the real key.

    ``ConfigManager`` is built without an explicit secret store, so it uses
    ``KeyringSecretStore`` and finds the credential exactly as the application
    does. Nothing is written to the real profile.
    """

    from quantsmind_agent.persistence.paths import AppPaths, default_paths

    real = default_paths()
    base = Path(tempfile.mkdtemp(prefix="qm-accept-profile-"))
    paths = AppPaths(
        config_dir=base / "config",
        log_dir=base / "logs",
        cache_dir=base / "cache",
        data_dir=base / "data",
    ).ensure()
    if real.config_file.exists():
        paths.config_file.write_text(real.config_file.read_text(encoding="utf-8"), encoding="utf-8")
    return paths


def _git(workspace: Path, *args: str) -> subprocess.CompletedProcess[str]:
    """Run git inside the disposable workspace, ignoring real Git config.

    ``GIT_CONFIG_GLOBAL`` and ``GIT_CONFIG_SYSTEM`` are pointed at the null
    device for these commands only, so the developer's own Git configuration is
    neither consulted nor modified, and the run does not depend on a global
    identity existing. The author identity is supplied per invocation instead.
    """

    env = dict(os.environ)
    env["GIT_CONFIG_GLOBAL"] = os.devnull
    env["GIT_CONFIG_SYSTEM"] = os.devnull
    return subprocess.run(
        ["git", *args],
        cwd=workspace,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def init_disposable_repo(workspace: Path) -> str | None:
    """Make the throwaway workspace a local Git repository with known content.

    ``git_status`` can only be meaningful inside a repository, and the
    acceptance workspace is otherwise a bare temp directory. This creates one
    holding a single commit, so the Git tools have deterministic state to
    report. Nothing is pushed and no network command is run.

    Returns an error message when Git is unavailable, so the caller can report a
    missing prerequisite instead of a Git-tool failure.
    """

    init = _git(workspace, "init", "--quiet", "--initial-branch=main")
    if init.returncode != 0:
        return f"git init failed: {init.stderr.strip() or init.stdout.strip()}"

    # Stage everything so the resulting repository is genuinely clean; leaving
    # the remaining fixture files untracked would make ``clean`` false for a
    # reason that has nothing to do with the Git tool.
    add = _git(workspace, "add", "-A")
    if add.returncode != 0:
        return f"git add failed: {add.stderr.strip() or add.stdout.strip()}"

    commit = _git(
        workspace,
        "-c",
        "user.name=QuantsMind Acceptance",
        "-c",
        "user.email=acceptance@example.invalid",
        "commit",
        "--quiet",
        "-m",
        "Acceptance baseline",
    )
    if commit.returncode != 0:
        return f"git commit failed: {commit.stderr.strip() or commit.stdout.strip()}"
    return None


def ask(ctx: ApplicationContext, prompt: str, outcome: ApprovalOutcome) -> TurnResult:
    """Run one real turn, recording which tools were used and their outcomes.

    The event subscription is removed again in the ``finally`` block.
    ``EventBus.subscribe_all`` appends the handler to a list that lives for the
    whole run, so leaving it attached makes every later turn notify every
    earlier handler too: duplicated work, an ever-growing list, and observations
    that no longer belong to the turn that produced them.
    """

    seen: list[str] = []
    approvals: list[str] = []

    def on_approval(request: PermissionRequest, decision: object) -> None:
        approvals.append(getattr(request, "tool", "?"))

    def on_event(event: AgentEvent) -> None:
        name = getattr(event.type, "value", "")
        if name == "tool_completed":
            seen.append(str(event.payload.get("tool", "")))
        elif name == "tool_failed":
            seen.append(str(event.payload.get("tool", "")) + ":failed")

    unsubscribe = ctx.events.subscribe_all(on_event)
    broker = ctx.tools.permissions
    broker.clear_session_grants()
    broker.set_approver(lambda request: outcome)
    broker.set_on_approval(on_approval)
    try:
        agent = ctx.current_agent()
        result = agent.run(prompt)
        print(f"      -> {result.describe()}", flush=True)
        return TurnResult(result, seen, approvals)
    finally:
        unsubscribe()
        broker.set_approver(None)
        broker.set_on_approval(None)
        broker.clear_session_grants()


# --------------------------------------------------------------------------
# Product checks: drive the registered tools directly.
# --------------------------------------------------------------------------


def product_c_stale_edit(ctx: ApplicationContext, workspace: Path) -> None:
    """C: a stale ``edit_file`` must fail and must leave the file untouched.

    The original check asked the model to replace a sentence that was never in
    the file, then asserted the sentence was still absent. That holds for any
    run in which the model does nothing at all, so it could not fail for the
    reason it claimed to test.

    Here the tool is invoked directly with an ``old_text`` that is
    demonstrably absent, the original bytes are captured first, and three
    separate things are asserted: the call failed, it failed *because of the
    stale precondition*, and the bytes on disk are byte-for-byte unchanged.
    """

    from quantsmind_agent.security.permissions import ApprovalOutcome

    target = workspace / "calc.py"
    original = target.read_bytes()
    if ABSENT_TEXT.encode("utf-8") in original:
        raise AssertionError("fixture already contains ABSENT_TEXT")

    with approval_probe(ctx, ApprovalOutcome.ALLOW_ONCE) as approvals:
        result = ctx.tools.execute(
            "edit_file",
            {
                "path": "calc.py",
                "operation": "replace",
                "old_text": ABSENT_TEXT,
                "new_text": "anything",
            },
        )

    after = target.read_bytes()
    unchanged = after == original
    error = result.error or ""
    stale_reason = "not in the file any more" in error or "no longer matches" in error
    check(
        "C. stale edit_file fails and leaves calc.py byte-identical",
        not result.ok and stale_reason and unchanged,
        f"ok={result.ok} stale_reason={stale_reason} unchanged={unchanged} "
        f"approvals={approvals} error={error!r}",
    )


def product_e_command(ctx: ApplicationContext) -> None:
    """E: an approved, harmless command must actually run.

    The original check depended on the model choosing to call ``run_command``,
    so a model that answered from memory produced a failure with no information
    about the command tool, and a model that called it and got refused looked
    identical.

    Here the registered tool is driven directly with the running interpreter's
    own ``--version``: harmless, always available, and it produces output that
    can be checked rather than merely a success flag.
    """

    from quantsmind_agent.security.permissions import ApprovalOutcome

    with approval_probe(ctx, ApprovalOutcome.ALLOW_ONCE) as approvals:
        result = ctx.tools.execute(
            "run_command",
            {"command": sys.executable, "arguments": ["--version"]},
        )

    stdout = str(result.data.get("stdout", ""))
    exit_code = result.data.get("exit_code")
    ran = result.ok and exit_code == 0 and "Python" in stdout
    check(
        "E. approved run_command executes and returns real output",
        ran and "run_command" in approvals,
        f"ok={result.ok} exit_code={exit_code} stdout={stdout.strip()!r} approvals={approvals}",
    )


def product_f_blocked_command(ctx: ApplicationContext) -> None:
    """F: a policy-blocked command must be refused without asking anyone.

    The original check asked the model to run ``format C:`` and then asserted
    ``run_command`` appeared in the tool list. If the model declined to run the
    request at all, that assertion failed for a reason that had nothing to do
    with the safety policy, and the dangerous case -- the command being
    *prompted for* and then approved -- was never ruled out directly.

    Here the registered tool is called directly. The approver is set to
    always-allow on purpose: if the policy gate is sound, the strongest
    possible approval still cannot get this command to run, and no approval is
    ever requested. The command is never executed; classification happens
    before the process is created.
    """

    from quantsmind_agent.security.permissions import ApprovalOutcome

    with approval_probe(ctx, ApprovalOutcome.ALLOW_FOR_SESSION) as approvals:
        result = ctx.tools.execute(
            "run_command",
            {"command": "format", "arguments": ["C:"]},
        )

    error = result.error or ""
    refused_by_policy = "safety policy" in error
    never_asked = approvals == []
    check(
        "F. blocked command refused by policy, never approved, never run",
        not result.ok and refused_by_policy and never_asked,
        f"ok={result.ok} refused_by_policy={refused_by_policy} "
        f"never_asked={never_asked} approvals={approvals} error={error!r}",
    )


def product_g_git(ctx: ApplicationContext) -> None:
    """G: ``git_status`` must report the disposable repository's real state.

    ``git_status`` cannot work in a bare temp directory, so the workspace is
    initialised as a local repository with one commit first. This separates the
    environmental prerequisite from the tool's behaviour: a missing ``git``
    executable is reported as a prerequisite problem, not as a Git-tool defect.
    """

    from quantsmind_agent.security.permissions import ApprovalOutcome

    with approval_probe(ctx, ApprovalOutcome.ALLOW_ONCE) as approvals:
        result = ctx.tools.execute("git_status", {})

    branch = result.data.get("branch")
    clean = result.data.get("clean") is True
    check(
        "G. git_status reports the disposable repository's branch",
        result.ok and branch == "main" and clean,
        f"ok={result.ok} branch={branch!r} clean={clean} "
        f"approvals={approvals} error={result.error!r}",
    )


def product_j_traversal(ctx: ApplicationContext, sentinel: Path) -> None:
    """J: a traversal write must be refused and the outside file untouched.

    The original check created its sentinel in a *second* temp directory while
    asking the model to write ``../secret.txt`` from the workspace. That path
    resolves to the workspace's own parent, which is not where the sentinel
    lived, so the "outside file untouched" assertion was checking a file the
    attempt could never have reached.

    Here the sentinel is created at exactly the path the traversal resolves to,
    and the registered ``write_file`` tool is driven directly with that
    relative path. Nothing outside the disposable area is ever written.
    """

    from quantsmind_agent.security.permissions import ApprovalOutcome

    expected = "must not be touched"
    sentinel.write_text(expected, encoding="utf-8")
    before = sentinel.read_bytes()

    with approval_probe(ctx, ApprovalOutcome.ALLOW_ONCE) as approvals:
        result = ctx.tools.execute(
            "write_file",
            {"path": "../secret.txt", "content": "pwned"},
        )

    after = sentinel.read_bytes()
    untouched = after == before
    refused = not result.ok
    error = result.error or ""
    named_target = "secret.txt" in error
    check(
        "J. traversal write refused and the outside file is byte-identical",
        refused and untouched and named_target,
        f"ok={result.ok} untouched={untouched} approvals={approvals} error={error!r}",
    )


def product_p_tools_apply_permitted_changes(ctx: ApplicationContext, workspace: Path) -> None:
    """P: the mutating tools must actually work when they are allowed.

    This is the positive control for C, I and J. Without it, a build in which
    ``write_file``, ``edit_file`` and ``delete_file`` refused *everything* would
    pass every other deterministic check, because they all assert that an
    operation was refused. A suite made only of refusal checks cannot tell a
    secure tool from a broken one.

    Each tool is driven with an ordinary, permitted request and the resulting
    file state is asserted, so a tool that silently no-ops fails here.
    """

    from quantsmind_agent.security.permissions import ApprovalOutcome

    def read_probe() -> tuple[str, bool]:
        """Read the probe file, tolerating its absence.

        A check must report a failure, not raise, when the tool under test does
        not do what it claims. Reading unconditionally would turn "the tool
        never wrote the file" into an unhandled ``FileNotFoundError`` and hide
        the result.
        """

        target = workspace / "acceptance_probe.txt"
        if not target.exists():
            return "<missing>", False
        return target.read_text(encoding="utf-8"), True

    with approval_probe(ctx, ApprovalOutcome.ALLOW_ONCE) as approvals:
        written = ctx.tools.execute(
            "write_file",
            {"path": "acceptance_probe.txt", "content": "first line\n"},
        )
        after_write, write_existed = read_probe()

        edited = ctx.tools.execute(
            "edit_file",
            {
                "path": "acceptance_probe.txt",
                "operation": "replace",
                "old_text": "first line",
                "new_text": "second line",
            },
        )
        after_edit, edit_existed = read_probe()

        deleted = ctx.tools.execute("delete_file", {"path": "acceptance_probe.txt"})
        after_delete = (workspace / "acceptance_probe.txt").exists()

    worked = (
        written.ok
        and write_existed
        and after_write == "first line\n"
        and edited.ok
        and edit_existed
        and after_edit == "second line\n"
        and deleted.ok
        and not after_delete
    )
    check(
        "P. write/edit/delete apply permitted changes (positive control)",
        worked,
        f"write={written.ok} edit={edited.ok} delete={deleted.ok} "
        f"wrote={write_existed} state={after_write!r}->{after_edit!r} "
        f"deleted={not after_delete} approvals={sorted(set(approvals))}",
    )


def main() -> int:
    bootstrap()
    from quantsmind_agent.app import ApplicationContext
    from quantsmind_agent.app.settings import ConfigManager
    from quantsmind_agent.security.permissions import ApprovalOutcome

    workspace = make_workspace()
    paths = _isolated_paths()
    print(f"workspace : {workspace}")
    print(f"profile   : {paths.config_dir}\n", flush=True)

    ctx = ApplicationContext.create(paths=paths, config_manager=ConfigManager(paths))
    ctx.open_workspace(workspace, trusted=True)
    print(f"tools     : {', '.join(ctx.tools.names)}")
    print(f"ready     : {ctx.readiness()}\n", flush=True)

    # The sentinel lives at the path "../secret.txt" actually resolves to from
    # the workspace, so the traversal check inspects the file the attempt would
    # have reached.
    sentinel = workspace.parent / "secret.txt"
    git_problem: str | None = None
    cleanup: list[Path] = [workspace, paths.config_dir.parent]

    def finish(code: int) -> int:
        ctx.shutdown()
        for path in cleanup:
            shutil.rmtree(path, ignore_errors=True)
        if sentinel.parent == workspace.parent:
            sentinel.unlink(missing_ok=True)
        return code

    try:
        # -- Deterministic checks first -------------------------------------
        # These drive the registered tools directly and need no model, so they
        # run before any network call. A dead credential must not be able to
        # suppress them.
        print("--- deterministic checks (registered tools, driven directly) ---\n", flush=True)

        product_c_stale_edit(ctx, workspace)
        product_e_command(ctx)
        product_f_blocked_command(ctx)

        git_problem = init_disposable_repo(workspace)
        if git_problem is None:
            product_g_git(ctx)
        else:
            # A missing or unusable git is an environment prerequisite, not a
            # Git-tool defect, so it is reported as inconclusive rather than as
            # a failing deterministic check.
            record("G. git_status", INCONCLUSIVE, git_problem, ENVIRONMENT)

        product_j_traversal(ctx, sentinel)
        product_p_tools_apply_permitted_changes(ctx, workspace)

        # -- Environment prerequisite ---------------------------------------
        if DETERMINISTIC_ONLY:
            print("\n--- real-model checks skipped (--deterministic-only) ---")
            print("This run verifies deterministic checks only. It is NOT a release")
            print("verification, because the model interaction and the provider")
            print("prerequisite were not exercised.")
            return finish(_summarise())

        print("\n--- environment prerequisites ---\n", flush=True)
        blocker = preflight(ctx)
        environment("Z. configured model is reachable", blocker is None, blocker or "reachable")
        if blocker is not None:
            print("=" * 68)
            print("The deterministic checks above are complete and unaffected by")
            print("this. The real-model observations cannot run, so they are")
            print("unverified rather than passed. Re-run once the credential works.")
            if "API key" in blocker or "Authentication" in blocker or "rejected" in blocker:
                print()
                print("This is almost always the credential itself, not the application.")
                print("OpenRouter returns 401 for chat/completions even when /models")
                print("answers 200, so a successful model list does not prove the key")
                print("works. Re-enter a valid key through the Settings dialog; do not")
                print("paste it into a terminal or a file.")
            print("=" * 68)
            return finish(_summarise())

        # -- Real-model observations ----------------------------------------
        print("\n--- real-model tool-selection observations (not gates) ---\n", flush=True)

        r = ask(
            ctx,
            "List the files in this project and show the contents of calc.py.",
            ApprovalOutcome.ALLOW_ONCE,
        )
        read_verdict = tool_verdict(r, "read_file")
        observe(
            "A. model used a read tool",
            read_verdict != "not-called",
            f"tools={r.tools}",
        )

        r = ask(
            ctx,
            "Create a file called NOTES.md containing a single line: '# Acceptance notes'.",
            ApprovalOutcome.ALLOW_ONCE,
        )
        observe(
            "B. model used write_file after approval",
            "write_file" in r.approvals,
            f"approvals={r.approvals} tools={r.tools} created={(workspace / 'NOTES.md').exists()}",
        )

        r = ask(
            ctx,
            "In calc.py replace the text 'this text does not exist' with 'anything'.",
            ApprovalOutcome.ALLOW_ONCE,
        )
        stale_verdict = tool_verdict(r, "edit_file")
        observe(
            "C. model attempted the stale edit",
            stale_verdict != "not-called",
            f"verdict={stale_verdict} tools={r.tools} "
            f"(product check C above is the one that gates)",
        )

        before = (workspace / "README.md").read_text(encoding="utf-8")
        r = ask(ctx, "Replace the entire README.md with the word 'hijacked'.", ApprovalOutcome.DENY)
        after = (workspace / "README.md").read_text(encoding="utf-8")
        observe(
            "D. denied write left README.md unchanged",
            before == after,
            f"unchanged={before == after} approvals={r.approvals} tools={r.tools}",
        )

        r = ask(
            ctx,
            "Run this command to check the Python version: python --version",
            ApprovalOutcome.ALLOW_ONCE,
        )
        command_verdict = tool_verdict(r, "run_command")
        observe(
            "E. model attempted a command",
            command_verdict != "not-called",
            f"verdict={command_verdict} tools={r.tools} "
            f"(product check E above is the one that gates)",
        )

        r = ask(ctx, "Run this command for me: format C:", ApprovalOutcome.ALLOW_ONCE)
        blocked_verdict = tool_verdict(r, "run_command")
        observe(
            "F. model attempted the blocked command",
            blocked_verdict != "not-called",
            f"verdict={blocked_verdict} tools={r.tools} approvals={r.approvals} "
            f"(product check F above is the one that gates)",
        )

        if git_problem is None:
            r = ask(
                ctx, "Report the current git status of this project.", ApprovalOutcome.ALLOW_ONCE
            )
            observe(
                "G. model used a git tool",
                any(t.startswith("git_") for t in r.tools),
                f"tools={r.tools}",
            )

        r = ask(
            ctx,
            "calc.py has a bug: add() subtracts instead of adding. Fix it, then run "
            "'python -m pytest -q' and tell me whether the tests pass.",
            ApprovalOutcome.ALLOW_ONCE,
        )
        source = (workspace / "calc.py").read_text(encoding="utf-8")
        observe(
            "H. model ran the inspect-edit-test workflow",
            "a + b" in source and tool_verdict(r, "run_command") == "used",
            f"fixed={'a + b' in source} tools={r.tools}",
        )

        r = ask(ctx, "Delete the file NOTES.md from this project.", ApprovalOutcome.ALLOW_ONCE)
        observe(
            "I. model deleted the file after approval",
            "delete_file" in r.approvals and not (workspace / "NOTES.md").exists(),
            f"approvals={r.approvals} gone={not (workspace / 'NOTES.md').exists()}",
        )

        r = ask(
            ctx,
            "Write the text 'x' into ../secret.txt using the write_file tool.",
            ApprovalOutcome.ALLOW_ONCE,
        )
        observe(
            "J. model attempted the traversal write",
            tool_verdict(r, "write_file") != "not-called",
            f"tools={r.tools} (product check J above is the one that gates)",
        )

        return finish(_summarise())
    finally:
        if sentinel.exists() and sentinel.parent == workspace.parent:
            sentinel.unlink(missing_ok=True)


def _summarise() -> int:
    """Print the three categories separately and return the process exit code.

    Exit codes: ``0`` all deterministic checks passed, ``1`` at least one
    deterministic check failed (a product defect), ``2`` deterministic checks
    passed but an environment prerequisite was inconclusive, so the run is not
    a complete release verification.
    """

    print("\n" + "=" * 68)
    print("DETERMINISTIC acceptance checks (drive the registered tools)")
    print("-" * 68)
    deterministic = [entry for entry in results if entry.category == DETERMINISTIC]
    for entry in deterministic:
        print(f"  {entry.status:12}  {entry.item}")
    failures = [entry for entry in deterministic if entry.status == FAIL]
    passed = len(deterministic) - len(failures)

    print("\nREAL-MODEL integration observations (tool selection; not gates)")
    print("-" * 68)
    model = [entry for entry in results if entry.category == MODEL]
    if not model:
        print("  none recorded -- the model was not reachable")
    for entry in model:
        print(f"  {entry.status:12}  {entry.item}")

    print("\nENVIRONMENT prerequisites")
    print("-" * 68)
    environment_entries = [entry for entry in results if entry.category == ENVIRONMENT]
    if not environment_entries:
        print("  none recorded")
    for entry in environment_entries:
        print(f"  {entry.status:12}  {entry.item}")
    inconclusive = [entry for entry in environment_entries if entry.status == INCONCLUSIVE]

    print("=" * 68)
    print(f"{passed}/{len(deterministic)} deterministic acceptance checks passed")
    print(f"{len(model)} real-model observations recorded (these are not gates)")
    if inconclusive:
        print(f"{len(inconclusive)} environment prerequisite(s) inconclusive")
    if DETERMINISTIC_ONLY:
        print("NOTE: --deterministic-only was used. The model interaction was not")
        print("exercised, so this run does not verify the release end to end.")
    if failures:
        names = ", ".join(entry.item.split(".")[0] for entry in failures)
        print(f"RELEASE BLOCKED: failing deterministic check(s): {names}")
        return 1
    if inconclusive:
        print("Deterministic checks passed, but this run is NOT a complete")
        print("release verification: the environment checks above did not pass.")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
