# Changelog

All notable changes to QuantsMind-Agent are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and the project uses [semantic versioning](https://semver.org/).

---

## [Unreleased]

Nothing yet.

---

## [0.1.0] — 2026-03-14

The first complete vertical slice: a runnable Windows 11 application that
connects a real language model to a local project and lets the agent inspect
files through controlled tools.

Starting commit: `182c6ae300ee44f5ca776c03827468d1458af219`.

### Added

**Application**

- PySide6 desktop GUI: workspace panel, model summary, conversation transcript,
  tool-activity panel, composer and status bar.
- Project folder selection (native dialog and `--workspace`).
- Settings dialog for provider, model, base URL, credential, timeout, token
  limit, temperature and agent limits, with presets for OpenAI, OpenRouter,
  Groq, Ollama, LM Studio and llama.cpp. Changes apply without a restart.
- `python -m quantsmind_agent` and the `quantsmind-agent` command, with
  `--workspace`, `--config-dir`, `--debug`, `--no-log-file` and
  `--check-config`.
- Application-wide light stylesheet; HTML conversation rendering with role
  colours and timestamps; keyboard shortcuts (`Ctrl+Enter`, `Ctrl+O`, `Ctrl+K`,
  `Ctrl+N`).

**Agent Core**

- Tool-calling loop with a bounded iteration budget; the final iteration
  withholds tools so the model must answer in prose.
- Recovery from tool failures, escaping paths, unknown tools and malformed
  arguments: each becomes an observation instead of ending the turn.
- Provider errors normalised into actionable messages (missing key, 401/403,
  404, 429, timeout, network, protocol).
- Cooperative cancellation honoured even when requested immediately after
  sending; a late answer is discarded rather than shown.

**Model Gateway**

- Provider-independent `ModelProvider` protocol and `ModelGateway`.
- `OpenAICompatProvider`: OpenAI `/chat/completions` with streaming and
  non-streaming, tool-call fragment accumulation, and best-effort
  `/models` discovery.
- Streaming for the final answer, with a non-streamed fallback when a provider
  cannot stream.

**Tools and permissions**

- Four read-only workspace tools: `project_structure`, `list_directory`,
  `read_file`, `search_content`.
- Tool contract with a JSON Schema, declared permission, and a structured
  result/error model.
- Permission layer with `ALLOW` / `DENY` / `ASK` and a deny-by-default policy;
  `ASK` without an approver degrades to `DENY`.
- Registry-enforced permissions and argument validation; a tool bug cannot
  crash the application or leak a traceback into the transcript.

**Workspace and security**

- `Workspace` with path containment: refuses absolute paths, drive letters, UNC
  paths, `..` traversal, home references, null bytes, and symlinks or junctions
  resolving outside the root.
- Ignore list for generated and VCS directories; binary-file refusal; bounded
  reads and searches.
- API keys stored in the Windows Credential Manager with a permissions-restricted
  file fallback; masked in the UI; scrubbed from logs; never written to the
  configuration file.

**Observability**

- Structured event bus covering the full agent lifecycle, with bounded history
  and fault-isolating subscribers.
- Rotating per-user log file with credential redaction and a developer mode.

**Configuration**

- Validated TOML settings in the per-user configuration directory, with
  `QM_CONFIG_DIR` / `QM_LOG_DIR` overrides.
- Environment-variable credential support (`QM_API_KEY` by default).

**Developer experience**

- 397 automated tests (307 unit, 46 integration, 44 GUI), all offline and
  credential-free; 1 symlink test skips without Windows Developer Mode.
- Strict `mypy` clean across 61 source files; `ruff` lint and format clean.
- Sphinx/reStructuredText documentation with a strict local HTML build (zero
  warnings), six architecture decision records, and a reproducible RST
  normalisation check.
- GitHub Actions CI on Linux and Windows; separate documentation workflow that
  uploads the HTML as a private artifact rather than publishing it.
- PyInstaller one-directory packaging script.
- Disposable acceptance fixture generator, verified end to end through the real
  tool registry.
- ``quantsmind-agent --check-config`` for a redacted, scriptable health check.

### Changed

Nothing — this is the first release.

### Known limitations

**Not implemented**

- No file writes, no command execution, no git, no network tools.
- No conversation persistence: history is lost on restart; no database is used.
- No multi-agent orchestration, no MCP, no VS Code extension, no telemetry.
- No installer framework; the packaged build has no Start-menu entry.

**Behavioural**

- Requires a provider with tool-calling support.
- Context is bounded: large repositories need narrower questions.
- Cancellation cannot abort an in-flight HTTP request; the late answer is
  discarded.
- No OS-level sandbox: the agent runs with the user's privileges. Containment
  and the read-only tool set are the defences.
- Prompt injection can mislead the model's *answers*; it cannot cause an escape.
- Binary files are unreadable and skipped by search.
- Symlinks that leave the project are refused, even when legitimate.
- TLS uses `certifi`, not the Windows certificate store, so a corporate proxy
  with a private CA needs environment-level configuration.

**Verification**

- Not verified against a real language model (requires a credential).
- Not verified on a physical Windows 11 desktop (GUI tests run under Qt's
  offscreen platform).
- PyInstaller bundle not built and launched on the release machine.

Full, honest results: `docs/source/testing/acceptance-v0.1.rst`.

### Fixed during development

Each of these was found by the test suite and now has a regression test.

- Cross-thread signal-to-signal connections silently dropped events in
  PySide6, hiding tool activity after the first call.
- The GUI held a stale `AgentCore` after a workspace rebuild, so requests ran
  with no tools registered.
- A final answer was discarded when a provider could not stream.
- A **Stop** pressed immediately after **Send** was cleared by the new turn.
- Credential-shaped provider errors were re-wrapped as network errors, hiding
  401 responses from the user.
- `Workspace.relative` rejected the absolute paths the workspace itself
  produced.
- A JSON boolean satisfied an `integer` schema check.
- An `ASK` decision could not reach `DENY` when no approver was wired up.
- `--workspace` was ignored when combined with `--check-config`.
- A tool call's completion event could render after the final answer.

[Unreleased]: https://github.com/ajit-ai/QuantsMind-Agent/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/ajit-ai/QuantsMind-Agent/releases/tag/v0.1.0
