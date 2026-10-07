# QuantsMind-Agent

**QuantsMind-Agent is an AI coding-agent platform inspired by modern
coding-agent workflows.**

QuantsMind-Agent is a Windows 11 desktop application that connects a real
language model to a local project folder. You open a repository, ask a coding
question, and the agent inspects your files through controlled tools before
answering — showing every tool call it makes.

> **Status: v0.1.0 (alpha).** The core workflow is complete and tested offline.
> Real-model behaviour and on-desktop rendering are verified manually, not by
> CI. See [the acceptance report](docs/source/testing/acceptance-v0.1.rst).

---

## What v0.1 does

| Capability | v0.1 |
|---|---|
| Windows 11 desktop GUI (PySide6) | ✅ |
| Open a local project folder | ✅ |
| OpenAI-compatible provider (OpenAI, OpenRouter, Groq, Ollama, LM Studio, llama.cpp, …) | ✅ |
| Streaming answers | ✅ |
| Tool-calling agent loop | ✅ |
| Workspace tools: `project_structure`, `list_directory`, `read_file`, `search_content` | ✅ |
| Visible tool activity with per-call state | ✅ |
| Permission layer (`ALLOW` / `DENY` / `ASK`, deny-by-default) | ✅ |
| Path-traversal and symlink-escape protection | ✅ |
| Credentials in the Windows Credential Manager, never logged | ✅ |
| Conversation kept for the session | ✅ |
| Sphinx/RST documentation with a local HTML build | ✅ |
| Automated tests (unit + integration + GUI, offline) | ✅ |
| CI on Linux and Windows | ✅ |
| **File writes, commands, git, network tools** | ❌ v0.2 |
| **Persistent conversation history** | ❌ v0.2 |
| **MCP, VS Code extension, multi-agent** | ❌ planned |

QuantsMind-Agent is **not** a Cline/OpenCode clone and does not claim parity
with them. It implements the core loop of a coding agent: a request, controlled
tools, observations, and an answer grounded in files it actually read.

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
  → Tool Registry       permission check
  → workspace tool      contained, read-only
  ← observation
  → Model Gateway       next round
  ← final answer        streamed to the transcript
```

Every step emits a structured event, which is what drives the activity panel
and the diagnostic log.

## Security

* The agent **only reads** files inside the folder you opened. Absolute paths,
  `..` traversal and symlinks leaving the root are refused.
* The model cannot grant itself a capability: tools declare permissions, and
  the registry enforces them. Unmatched rules are denied.
* No command execution, no file writes, no network tools in v0.1.
* Your API key lives in the Windows Credential Manager, is masked everywhere it
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
