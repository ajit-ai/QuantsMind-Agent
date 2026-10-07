# Contributing to QuantsMind-Agent

Thanks for helping. This is a **private** repository; if you do not already have
access, request it from the maintainer rather than forking.

## Getting set up

```powershell
git clone https://github.com/ajit-ai/QuantsMind-Agent.git
cd QuantsMind-Agent

python -m venv .venv
.venv\Scripts\Activate.ps1

pip install -e ".[dev,docs]"
pre-commit install      # optional
```

Windows 11 is the primary target. Linux and macOS work for development: the
headless core has no Qt dependency, and GUI tests use Qt's offscreen platform.

## The gates your change must pass

Every one of these runs in CI. Locally:

```powershell
ruff check .              # lint
ruff format --check .     # formatting
mypy                      # strict types
pytest                    # unit + integration + GUI
python scripts/normalize_rst.py --check
python scripts/build_docs.py --clean
python -m build           # sdist + wheel
```

All must exit `0`. Documentation warnings are errors.

## Ground rules

**Tests travel with the fix.** Every bug fix ships with the test that would have
caught it. Several v0.1 defects were found exactly this way; see the "Fixed
during development" section of the CHANGELOG for the pattern.

**No network in tests.** Use `tests/fakes.py`:

- `FakeModelProvider` for agent-level tests;
- `mock_transport` with `canned_body` / `sse_body` for provider-level tests.

A test that needs a real API key does not belong in the suite.

**No ambient credentials.** `tests/conftest.py` removes `QM_API_KEY` and
`OPENAI_API_KEY` for every test. If a test passes only because a key is set in
your shell, it is wrong.

**Name tests after behaviour.**

```python
def test_escaping_path_is_refused_not_raised(self, workspace): ...
```

**Assert user-visible outcomes** — returned data, rendered text, event
sequences — not private attributes.

## Architectural boundaries

These are the rules that keep the codebase reviewable. A change that breaks one
needs an ADR.

- The **GUI** imports no provider and holds no tool bodies.
- The **Agent Core** imports no provider, no tool body and no Qt.
- **Tools** never check permissions; the registry does, before execution.
- Nothing but **`Workspace`** touches user files.
- The **headless core** never imports Qt.
- **Events** carry no credentials.

See `docs/source/architecture/overview.rst` and
`docs/source/developer/extension-points.rst`.

## Adding a capability

1. Open an issue describing the user-visible outcome, not the implementation.
2. If it crosses a boundary, write an ADR under
   `docs/source/architecture/adr/` using the existing template, and add it to
   that directory's toctree.
3. Implement behind the existing interface — prefer extending `Tool`,
   `ModelProvider`, `PermissionProvider`, `ContextProvider`, `Workspace`,
   `Session` or `EventBus` over bypassing one.
4. Add unit tests plus one integration test through the agent loop.
5. For a new tool: a success case, an escaping-path case, and a case asserting
   the declared permission is enforced.
6. Update the documentation. A capability without a page describing it, its
   limits and its permissions is incomplete.

## Commit messages

Conventional commits:

```text
feat: add a write_file tool behind WRITE_WORKSPACE
fix: resolve the agent after a workspace rebuild
docs: document the cancellation limitation
test: cover symlink escapes in project_structure
chore: pin ruff to 0.16
```

One logical change per commit. Do not reformat unrelated code.

## Pull requests

- Branch from `main`, rebase before requesting review.
- Describe what changed, why, and how it was verified — paste the commands and
  their results, not a summary.
- State explicitly what you did **not** verify. "Not run: real LLM" is more
  useful than silence.
- Update `CHANGELOG.md` under *Unreleased* for user-visible changes.
- Update `docs/source/testing/acceptance-v0.1.rst` if you ran a manual
  procedure.

## Code review

Reviewers look for:

- **Containment.** Does any new path reach the filesystem without `Workspace`?
- **Permissions.** Does a new tool declare the correct capability? A write tool
  annotated `READ_WORKSPACE` is the mistake the system cannot catch for you.
- **Boundaries.** Does the GUI now import a provider, or the core import Qt?
- **Output size.** Does the tool bound what it returns? One large result can
  flood the context window.
- **Error paths.** Expected failures should be returned, not raised, so the
  agent can recover.
- **Leakage.** Can a credential reach a log, an event or the transcript?
- **Tests.** Would this test fail without the change?

## Security issues

Do not open a public issue. See [SECURITY.md](SECURITY.md).

## Licence

Contributions are accepted under the [MIT licence](LICENSE).
