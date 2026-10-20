# QuantsMind-Agent

**QuantsMind-Agent is an AI coding-agent platform inspired by modern
coding-agent workflows.**

QuantsMind-Agent is a cross-platform desktop application (Windows, Linux, macOS) that connects a real
language model to a local project folder. You open a repository, ask a coding
question, and the agent inspects your files through controlled tools before
answering — showing every tool call it makes.

> **Status: v0.9.0 (alpha).** Planning starts from a bounded, versioned account
> of the whole project, and that account is kept with the task as evidence.
> Real-model behaviour and on-desktop rendering are verified manually, not by CI.

---

## Supported platforms

Support is claimed per platform, and only where it has actually been verified.
"Native-verified" means the application has been run on that operating system.

| Platform | Status | Evidence |
|---|---|---|
| Windows 10/11 x64 | **Supported** | Full test suite, packaged executable built and smoke-tested on Windows 11 |
| Linux x64 (glibc) | **Supported** | Full test suite in CI on Ubuntu; headless only, no on-desktop run yet |
| macOS (Apple Silicon, Intel) | **Planned** | Artifact builds run in CI; the GUI has not been run on macOS |
| BSD (FreeBSD, NetBSD, OpenBSD) | **Not supported** | Blocked: no PySide6 wheels exist for BSD. See docs/source/installation.rst |

Windows and Linux are supported. macOS is building artifacts but has no
on-desktop verification, so it is not claimed as supported. BSD is blocked at the
dependency level, not by the application code.

## What v0.2 does

| Capability | v0.2 |
|---|---|
| Cross-platform desktop GUI (PySide6 / Qt 6): Windows, Linux, macOS | ✅ |
| Open a local project folder | ✅ |
| OpenAI-compatible provider (OpenAI, OpenRouter, Groq, Ollama, LM Studio, llama.cpp, …) | ✅ |
| Streaming answers | ✅ |
| Tool-calling agent loop | ✅ |
| Read tools: `project_structure`, `list_directory`, `read_file`, `search_content` | ✅ |
| Write tools: `write_file`, `edit_file` (deterministic, staleness-checked) | ✅ v0.2 |
| `delete_file`, file-only, always approved | ✅ v0.2 |
| `run_command` with timeout, cancellation and bounded output | ✅ v0.2 |
| Command safety policy (deny-by-default, not an OS sandbox) | ✅ v0.2 |
| Read-only Git: `git_status`, `git_diff`, `git_log` | ✅ v0.2 |
| Human approval prompts (Allow / Allow Once / Deny / Allow for Session) | ✅ v0.2 |
| Visible tool activity with per-call state | ✅ |
| Permission layer (`ALLOW` / `DENY` / `ASK`, deny-by-default) | ✅ |
| Path-traversal and symlink-escape protection | ✅ |
| Credentials in the OS keyring (Windows Credential Manager, macOS Keychain, Secret Service on Linux), never logged, with a file-store fallback | ✅ |
| Conversation kept for the session | ✅ |
| Sphinx/RST documentation with a local HTML build | ✅ |
| Automated tests (unit + integration + GUI, offline) | ✅ |
| CI on Linux and Windows, plus native artifact builds for Windows, Linux and macOS | ✅ |
| **Git mutations (commit/push/checkout/reset)** | ❌ deliberately blocked |
| **Persistent conversation history** | ❌ planned |
| **MCP, VS Code extension, multi-agent** | ❌ planned |

QuantsMind-Agent is **not** a Cline/OpenCode clone and does not claim parity
with them. It implements the core loop of a coding agent: a request, controlled
tools, observations, and an answer grounded in files it actually read.

The rule the rest of the design follows:

> The LLM may propose actions, but the application remains responsible for
> authorization and execution.

Concretely: every write, every delete and every command is authorised by the
application, not by the model. Commands that would be obviously destructive on
the host are refused outright and are never shown in an approval dialog at all.
See [the v0.2 release notes](docs/source/releases/v0.2.rst) and the
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

* Every path — read, write, edit or delete — is validated by the same
  containment check. Absolute paths, `..` traversal, null bytes and symlinks
  leaving the root are refused.
* The model cannot grant itself a capability: tools declare permissions, the
  registry enforces them, and unmatched rules are denied.
* Anything that changes the workspace prompts first. Deleting a file and running
  a command always prompt, every time.
* Destructive host commands are refused outright and are never offered in an
  approval dialog. Git mutations are refused too.
* **Application-level command policy is not an OS sandbox.** It is a deny list
  evaluated by this process. Read
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
* [v0.2 release notes](docs/source/releases/v0.2.rst)
* [v0.1 release notes](docs/source/releases/v0.1.rst)
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
[packaging decision](docs/source/installation.rst#installation-packaging).

## Project status

`main` is the development branch. See
[CHANGELOG.md](CHANGELOG.md) for what changed, and the
[acceptance report](docs/source/testing/acceptance-v0.1.rst) for what has and has
not been verified.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). Bug reports and security issues are
handled privately — see [SECURITY.md](SECURITY.md).

## Licence

MIT. See [LICENSE](LICENSE).
