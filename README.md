# QuantsMind-Agent

**QuantsMind-Agent is an AI coding-agent platform inspired by modern
coding-agent workflows.**

QuantsMind-Agent is a cross-platform desktop application (Windows, Linux, macOS) that connects a real
language model to a local project folder. You open a repository, ask a coding
question, and the agent inspects your files through controlled tools before
answering — showing every tool call it makes.

> **Status: v0.9.0 (alpha).** Planning starts from a bounded, versioned account
> of the whole project, and that account is kept with the task as evidence.
> The published Windows MSI is qualified for a supervised limited beta only:
> real-model, human GUI, clean-machine, signing and release-commit CI evidence
> are still outstanding. See the
> [qualification record](docs/source/qualification/index.rst).

---

## Supported platforms

Support is claimed per platform, and only where it has actually been verified.
"Native-verified" means the application has been run on that operating system.

| Platform | Status | Evidence |
|---|---|---|
| Windows 11 x64 | **Supported** | Full test suite, packaged executable and v0.9.0 MSI built, installed and smoke-tested on Windows 11 |
| Windows 10 x64 | **Not verified** | The same code path may work, but no Windows 10 artifact or desktop run is recorded |
| Linux x64 (glibc) | **Supported, headless** | Full test suite in CI on Ubuntu; no on-desktop run yet |
| macOS (Apple Silicon, Intel) | **Planned** | Artifact builds are configured in CI; the GUI has not been run on macOS |
| BSD (FreeBSD, NetBSD, OpenBSD) | **Not supported** | Blocked: no PySide6 wheels exist for BSD. See docs/source/installation.rst |

Windows 11 is the supported desktop target. Linux is supported headless;
macOS is not claimed until its GUI has been run. BSD is blocked at the
dependency level, not by the application code.

## What v0.9.0 does

| Capability | v0.9.0 status |
|---|---|
| Desktop GUI (PySide6 / Qt 6) | Windows 11 verified; Linux headless CI; macOS planned |
| Open a local project folder | ✅ |
| OpenAI-compatible provider (OpenAI, OpenRouter, Groq, Ollama, LM Studio, llama.cpp, …) | ✅ |
| Streaming answers and a tool-calling agent loop | ✅ |
| Read tools: `project_structure`, `list_directory`, `read_file`, `search_content` | ✅ |
| Code navigation: `code_outline`, `find_references`, `project_symbols`, `project_context` | ✅ |
| Write tools: `write_file`, `edit_file` (deterministic, staleness-checked), `delete_file` | ✅ approval-gated |
| `undo_file` for the newest reversible change, session-scoped | ✅ approval-gated |
| `run_command` with timeout, cancellation and bounded output | ✅ approval-gated |
| Command safety policy (application deny list, not an OS sandbox) | ✅ |
| Read-only Git: `git_status`, `git_diff`, `git_log` | ✅ |
| Plan → approve → execute → verify task lifecycle | ✅ |
| Bounded workspace snapshot and project-aware planning (v0.9) | ✅ |
| Persistent conversations, task history, checkpoints and recovery | ✅ |
| Human approval prompts (Allow / Allow Once / Deny / Allow for Session) | ✅; delete, undo and command always ask |
| Permission layer (`ALLOW` / `DENY` / `ASK`, deny-by-default) | ✅ |
| Path containment, including symlink and junction escapes | ✅; discovery skips all filesystem links |
| Credentials in the OS keyring (Windows Credential Manager, macOS Keychain, Secret Service on Linux), never logged, with a file-store fallback | ✅ |
| Sphinx/RST documentation with a local HTML build | ✅ |
| Automated tests (unit + integration + security + GUI, offline) | ✅ |
| CI on Linux, Windows and macOS, plus native artifact builds | Configured; not all runners are currently available, and no green release-commit run is evidenced |
| Signed Windows MSI and executable | ❌ unsigned; signing is an open release blocker |
| OS-level sandbox for approved commands | ❌ not implemented |
| **Git mutations (commit/push/checkout/reset)** | ❌ deliberately blocked |
| **Web-fetch tool, MCP, VS Code extension, multi-agent** | ❌ not implemented |

QuantsMind-Agent is **not** a Cline/OpenCode clone and does not claim parity
with them. It implements the core loop of a coding agent: a request, controlled
tools, observations, and an answer grounded in files it actually read.

The rule the rest of the design follows:

> The LLM may propose actions, but the application remains responsible for
> authorization and execution.

Concretely: every write, every delete and every command is authorised by the
application, not by the model. Commands that would be obviously destructive on
the host are refused outright and are never shown in an approval dialog at all.
See [the v0.9 behaviour note](docs/source/architecture/v0.9-behaviour.rst), the
[changelog](CHANGELOG.md), and the
[security boundaries](docs/source/architecture/security-boundaries.rst).

## Quick start

```powershell
git clone https://github.com/ajit-ai/QuantsMind-Agent.git
cd QuantsMind-Agent

python -m venv .venv
.venv\Scripts\Activate.ps1

pip install -e ".[dev,docs]"

quantsmind-agent
```

On first launch:

1. **Open project…** — select a repository folder.
2. **Settings…** (`Ctrl+K`) — pick a provider preset, confirm the model, paste
   your API key.
3. Ask: `Explain the structure of this project.`

Try it against the bundled disposable fixture:

```powershell
python scripts\make_acceptance_fixture.py --force
quantsmind-agent --workspace .acceptance-fixture\widget-factory
```

### Verify the installation without opening a window

```powershell
quantsmind-agent --check-config
```

It prints a **redacted** summary and exits `0` when ready, `1` when a project or
credential is still missing.

## Screens

The window is a sidebar (workspace, model, session), the conversation
transcript, a tool-activity panel, and a composer. Tool calls appear in the
activity panel *before* the answer, so you can always see what the agent did.

```
+----------------------------------------------------------+
| QuantsMind-Agent                                         |
+-------------------+--------------------------------------+
| Workspace         | Conversation                         |
|  [Open project…]  |   You        14:02:11                |
|  C:\code\app      |   What does main() do?               |
|                   |                                      |
| Model             |   Assistant  14:02:13                |
|  gpt-4o-mini      |   main() loads settings, then …      |
|  key: sk-******op |                                      |
|                   |   Tool  14:02:12                     |
| Session           |   ✓ read_file — src/app/main.py      |
|  4 messages       |                                      |
+-------------------+--------------------------------------+
| [ask about this project….............................] [Send]|
+----------------------------------------------------------+
```

## How it works

```
user request
  → Context Engine      bounded prompt + workspace summary
  → Model Gateway       provider-independent request
  → real LLM
  ← tool calls, or an answer
  → Tool Registry       permission check + command policy
  → workspace tool      contained; reads, writes, edits, deletes
  → run_command         only after explicit approval
  → git tool            read-only, structurally
  ← observation
  → Model Gateway       next round
  ← final answer        streamed to the transcript
```

Every step emits a structured event, which is what drives the activity panel
and the diagnostic log.

## Security

* Every path — read, write, edit, delete or undo — is validated by the same
  containment check. Absolute paths, `..` traversal, null bytes and symlinks
  leaving the root are refused.
* Project discovery omits symbolic links, junctions and other reparse points
  instead of following them. The published v0.9.0 MSI predates that fix, so an
  outward junction can still fail discovery in that artifact.
* The model cannot grant itself a capability: tools declare permissions, the
  registry enforces them, and unmatched rules are denied.
* Anything that changes the workspace prompts first. Deleting a file, undoing a
  change and running a command always prompt, every time.
* Destructive host commands are refused outright and are never offered in an
  approval dialog. Git mutations are refused too.
* **Application-level command policy is not an OS sandbox.** It is a deny list
  evaluated by this process; an approved program still runs with your
  privileges. Read
  [security boundaries](docs/source/architecture/security-boundaries.rst) before
  pointing the agent at anything you care about.
* Your API key lives in your operating system keyring, is masked everywhere it
  is displayed, and is scrubbed from logs.
* No telemetry, no update checks.

Read [SECURITY.md](SECURITY.md) and [docs/source/security.rst](docs/source/security.rst).

## Documentation

The documentation is reStructuredText, built with Sphinx. This repository is
private, so it is **never** published to GitHub Pages; build it locally:

```powershell
scripts\build-docs.ps1          # strict: any warning fails the build
start docs\build\html\index.html
```

Other platforms:

```bash
python scripts/build_docs.py
```

CI builds the same documentation and fails on any warning. The HTML is uploaded
as a private workflow artifact.

Start with:

* [Getting started](docs/source/getting-started.rst)
* [Installation](docs/source/installation.rst)
* [Configuration](docs/source/configuration.rst)
* [Windows 11](docs/source/windows.rst)
* [Architecture](docs/source/architecture/overview.rst)
* [v0.9.0 behaviour](docs/source/architecture/v0.9-behaviour.rst)
* [Qualification record](docs/source/qualification/index.rst)
* [v0.2 release notes](docs/source/releases/v0.2.rst) (historical)
* [v0.1 release notes](docs/source/releases/v0.1.rst) (historical)
* [Roadmap](docs/source/roadmap/roadmap.rst)

## Development

```powershell
pip install -e ".[dev,docs]"

ruff check .              # lint
ruff format --check .     # formatting
mypy                      # strict types
pytest                    # unit + integration + GUI (offline, no API key)
scripts\build-docs.ps1    # documentation
python -m build           # sdist + wheel
```

GUI tests use Qt's offscreen platform, so no desktop is required.

```powershell
pytest tests/unit -q
pytest tests/integration -q
pytest tests/gui -q
pytest -m "not gui" -q
```

The whole suite is **offline**: no network, no credentials, no access to your
real user profile. See [docs/source/developer/testing.rst](docs/source/developer/testing.rst).

### Packaging

```powershell
pip install -e ".[packaging]"
scripts\package.ps1
```

Produces a PyInstaller one-directory bundle in `dist\QuantsMind-Agent\`. Ship the
whole directory; the rationale is in the
[packaging decision](docs/source/installation.rst#installation-packaging). The
published v0.9.0 Windows release also ships a per-user MSI. That MSI is
**unsigned**, so Windows SmartScreen may warn on first install; the artifact
hash and its limits are recorded in the
[qualification record](docs/source/qualification/index.rst).

## Project status

`main` is the development branch. See
[CHANGELOG.md](CHANGELOG.md) for what changed. The
[qualification record](docs/source/qualification/index.rst) separates automated
source evidence, published-artifact evidence and evidence that is still missing.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). Bug reports and security issues are
handled privately — see [SECURITY.md](SECURITY.md).

## Licence

MIT. See [LICENSE](LICENSE).
