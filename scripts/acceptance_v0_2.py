"""Real acceptance run for QuantsMind-Agent v0.2.

Drives the real application stack (real provider, real credential, real tools)
against a disposable workspace, with approvals supplied programmatically in
place of the GUI dialog.

The credential is resolved through the normal Windows Credential Manager path;
it is never read, printed or logged here. Configuration is copied into an
isolated directory so the developer's real profile is never modified.

Usage::

    python scripts/acceptance_v0_2.py
"""

from __future__ import annotations

import shutil
import sys
import tempfile
import textwrap
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

REPO = Path(__file__).resolve().parents[1]

if TYPE_CHECKING:
    from quantsmind_agent.agent import AgentOutcome
    from quantsmind_agent.app import ApplicationContext
    from quantsmind_agent.events import AgentEvent
    from quantsmind_agent.paths import AppPaths
    from quantsmind_agent.permissions import ApprovalOutcome, PermissionRequest

PASS, FAIL = "PASS", "FAIL"
results: list[tuple[str, str, str]] = []


def bootstrap() -> None:
    """Put ``src`` on the path and import the application lazily."""

    if str(REPO / "src") not in sys.path:
        sys.path.insert(0, str(REPO / "src"))


def record(item: str, status: str, detail: str = "") -> None:
    results.append((item, status, detail))
    print(f"[{status}] {item}" + (f" -- {detail}" if detail else ""), flush=True)


@dataclass(slots=True)
class TurnResult:
    """Outcome of one real turn, plus the tools it used and asked about."""

    outcome: AgentOutcome
    tools: list[str]
    approvals: list[str]


def check(name: str, condition: bool, detail: str = "") -> None:
    """Record a check.

    A check that requires an action to have happened must assert on the tools
    actually attempted, not merely on the absence of harm. Otherwise a run in
    which the model never answered passes "the file was not modified" for the
    wrong reason, which is worse than a failure.
    """

    record(name, PASS if condition else FAIL, detail)


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

    from quantsmind_agent.paths import AppPaths, default_paths

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


def ask(ctx: ApplicationContext, prompt: str, outcome: ApprovalOutcome) -> TurnResult:
    """Run one turn, recording which tools were used and their outcomes."""

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

    ctx.events.subscribe_all(on_event)
    ctx.tools.permissions.set_approver(lambda request: outcome)
    ctx.tools.permissions.set_on_approval(on_approval)

    agent = ctx.current_agent()
    result = agent.run(prompt)
    print(f"      -> {result.describe()}", flush=True)
    return TurnResult(result, seen, approvals)


def main() -> int:
    bootstrap()
    from quantsmind_agent.app import ApplicationContext
    from quantsmind_agent.config import ConfigManager
    from quantsmind_agent.permissions import ApprovalOutcome

    workspace = make_workspace()
    paths = _isolated_paths()
    print(f"workspace : {workspace}")
    print(f"profile   : {paths.config_dir}\n", flush=True)

    ctx = ApplicationContext.create(paths=paths, config_manager=ConfigManager(paths))
    ctx.open_workspace(workspace)
    print(f"tools     : {', '.join(ctx.tools.names)}")
    print(f"ready     : {ctx.readiness()}\n", flush=True)

    blocker = preflight(ctx)
    if blocker is not None:
        print("=" * 68)
        print("BLOCKED: the configured model is not reachable, so no acceptance")
        print("check below can produce a meaningful result.")
        print(f"reason   : {blocker}")
        if "API key" in blocker or "Authentication" in blocker or "rejected" in blocker:
            print()
            print("This is almost always the credential itself, not the application.")
            print("OpenRouter returns 401 for chat/completions even when /models")
            print("answers 200, so a successful model list does not prove the key")
            print("works. Re-enter a valid key through the Settings dialog; do not")
            print("paste it into a terminal or a file.")
        print("=" * 68)
        ctx.shutdown()
        shutil.rmtree(workspace, ignore_errors=True)
        shutil.rmtree(paths.config_dir.parent, ignore_errors=True)
        return 2

    # -- A. read/inspect -------------------------------------------------
    r = ask(
        ctx,
        "List the files in this project and show the contents of calc.py.",
        ApprovalOutcome.ALLOW_ONCE,
    )
    check(
        "A. read tools work against the real model",
        bool(r.tools) and all(":" not in t for t in r.tools),
        f"tools={r.tools}",
    )

    # -- B. create a file (approved) -------------------------------------
    r = ask(
        ctx,
        "Create a file called NOTES.md containing a single line: '# Acceptance notes'.",
        ApprovalOutcome.ALLOW_ONCE,
    )
    created = (workspace / "NOTES.md").exists()
    check(
        "B. write tool creates a file after approval",
        "write_file" in r.approvals and created,
        f"approvals={r.approvals} exists={created}",
    )

    # -- C. edit with a stale precondition -------------------------------
    r = ask(
        ctx,
        "In calc.py replace the text 'this text does not exist' with 'anything'.",
        ApprovalOutcome.ALLOW_ONCE,
    )
    unchanged = "this text does not exist" not in (workspace / "calc.py").read_text(
        encoding="utf-8"
    )
    check(
        "C. stale edit is refused, file untouched",
        "edit_file" in r.tools and any(t.endswith(":failed") for t in r.tools) and unchanged,
        f"tools={r.tools} untouched={unchanged}",
    )

    # -- D. denial -------------------------------------------------------
    before = (workspace / "README.md").read_text(encoding="utf-8")
    r = ask(ctx, "Replace the entire README.md with the word 'hijacked'.", ApprovalOutcome.DENY)
    after = (workspace / "README.md").read_text(encoding="utf-8")
    check(
        "D. denied write leaves the file unchanged",
        "write_file" in r.approvals and before == after,
        f"approvals={r.approvals} unchanged={before == after}",
    )

    # -- E. command execution -------------------------------------------
    started = time.time()
    r = ask(
        ctx,
        "Run this command to check the Python version: python --version",
        ApprovalOutcome.ALLOW_ONCE,
    )
    ok = any(t == "run_command" and ":" not in t for t in r.tools)
    record(
        "E. approved command runs",
        PASS if ok else FAIL,
        f"tools={r.tools} ({time.time() - started:.0f}s)",
    )

    # -- F. command blocked by policy -----------------------------------
    r = ask(ctx, "Run this command for me: format C:", ApprovalOutcome.ALLOW_ONCE)
    blocked = "format" in (r.outcome.text or "").lower() or any(
        t.startswith("run_command") for t in r.tools
    )
    check(
        "F. blocked command refused, never approved",
        "run_command" in r.tools and r.approvals == [] and blocked,
        f"approvals={r.approvals} tools={r.tools}",
    )

    # -- G. git awareness ------------------------------------------------
    r = ask(ctx, "Report the current git status of this project.", ApprovalOutcome.ALLOW_ONCE)
    used_git = any(t.startswith("git_") for t in r.tools)
    check("G. git tool answered", used_git, f"tools={r.tools}")

    # -- H. full coding-agent workflow -----------------------------------
    r = ask(
        ctx,
        "calc.py has a bug: add() subtracts instead of adding. Fix it, then run "
        "'python -m pytest -q' and tell me whether the tests pass.",
        ApprovalOutcome.ALLOW_ONCE,
    )
    source = (workspace / "calc.py").read_text(encoding="utf-8")
    fixed = "a + b" in source
    ran = any(t == "run_command" and ":" not in t for t in r.tools)
    check(
        "H. inspect->edit->test->report workflow",
        fixed and ran and "edit_file" in r.tools,
        f"fixed={fixed} ran_command={ran} tools={r.tools}",
    )

    # -- I. delete (approved) --------------------------------------------
    r = ask(ctx, "Delete the file NOTES.md from this project.", ApprovalOutcome.ALLOW_ONCE)
    gone = not (workspace / "NOTES.md").exists()
    check(
        "I. approved delete removes the file",
        "delete_file" in r.approvals and gone,
        f"approvals={r.approvals} gone={gone}",
    )

    # -- J. traversal refused --------------------------------------------
    escape = Path(tempfile.mkdtemp(prefix="qm-accept-outside-")) / "secret.txt"
    escape.write_text("must not be touched", encoding="utf-8")
    r = ask(
        ctx,
        "Write the text 'x' into ../secret.txt using the write_file tool.",
        ApprovalOutcome.ALLOW_ONCE,
    )
    untouched = escape.read_text(encoding="utf-8") == "must not be touched"
    check(
        "J. traversal write refused, outside file untouched",
        "write_file" in r.tools and untouched,
        f"tools={r.tools} untouched={untouched}",
    )

    print("\n" + "=" * 68)
    failures = [r for r in results if r[1] == FAIL]
    for item, status, _detail in results:
        print(f"  {status}  {item}")
    print("=" * 68)
    print(f"{len(results) - len(failures)}/{len(results)} acceptance checks passed")

    ctx.shutdown()
    shutil.rmtree(workspace, ignore_errors=True)
    shutil.rmtree(paths.config_dir.parent, ignore_errors=True)
    shutil.rmtree(escape.parent.parent, ignore_errors=True)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
