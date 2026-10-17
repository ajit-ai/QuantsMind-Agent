# Changelog

All notable changes to QuantsMind-Agent are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and the project uses [semantic versioning](https://semver.org/).

---

## [Unreleased]

### Added

**Project intelligence (v0.3)**

- The agent now understands the project before it changes it. A new
  `project_context` tool answers, in one bounded call: what kind of project this
  is, where the source and tests live, which files and symbols match the task,
  which tests cover the file you named, and which files import it. The system
  prompt is oriented from the same profile every turn, so the model knows the
  layout before it asks.
- Discovery recognises `pyproject.toml`, `setup.py`/`setup.cfg`,
  `requirements*.txt`, `src/` layouts, package directories, test and documentation
  directories, `__main__.py`, console scripts declared in `pyproject.toml` or
  `setup.cfg`, and the usual build and CI files -- in one bounded pass, without
  assuming any particular layout. It reports what it could not determine and how
  confident it is rather than guessing, so a project it cannot place reads as
  `unknown` rather than as a confident mistake.
- A project-scoped symbol and import map, so the navigation tools stop walking
  the whole workspace on every call. It reuses the existing parsers: there is no
  second Python parser in this product, and no embeddings, index service, daemon
  or network dependency.
- Invalidation is per file and driven by the agent's own writes. Editing a module
  invalidates that module; creating one adds it; deleting one drops it *and every
  edge that pointed at it*; undo re-invalidates what it restored. Opening another
  project builds a new map, so nothing leaks between projects. A file that has
  been invalidated is re-read rather than answered from the previous record.
- Project intelligence is not written into conversations. It is a claim about
  files on disk, and files change while the application is closed, so it is
  rebuilt per session rather than persisted as though it were still true.

### Fixed

- **An edit whose `old_text` ended with a newline could be refused on the last
  line of a file**, with "the text you asked to replace is not in the file any
  more" -- which was not true. The trailing newline was kept out of the text the
  needle was matched against, and a model naturally copies the line it read
  including the newline.
- **Absolute imports in a `src/` layout resolved to nothing.** `import acme.cart`
  names `src/acme/cart.py` and the two spellings share no prefix, so the importer
  scan reported *no callers* for every file in such a project -- a confident,
  specific, wrong answer, and the dangerous kind. Imports now resolve through the
  discovered package layout.
- **`from pkg import mod` resolved to the package's `__init__.py`** rather than to
  the submodule it names, so a caller edge pointed at the package marker instead of
  the file an agent needs to open. The submodule is preferred when it exists, and
  the package marker remains the answer when it does not.
- `project_symbols` returned nothing when `path` named a single file, because the
  file was treated as a directory prefix -- and reading that as "this file defines
  nothing" is worse than an error.
- `python -m pkg` was reported as `python -m pkg.__main__` or
  `python -m src.pkg.__main__`; neither command runs.

**Conversation resumption (v0.3)**

- A reopened conversation now shows what actually happened: user requests,
  assistant answers and the tool calls in between, in the order they occurred, each
  line carrying the time it happened rather than the time you reopened it. Tool
  results gained a timestamp, because tool traffic is deliberately kept out of the
  message list and a restored transcript had no way to say where a call sat among
  the messages around it.
- A restored transcript states what it is not showing. A turn that was interrupted,
  cancelled or failed says so instead of ending on the last thing that happened to
  succeed, and a transcript stored under a size limit admits that its older history
  is gone.
- Per-conversation storage limits (`agent.max_stored_messages`,
  `agent.max_stored_tool_results`, `agent.max_stored_item_chars`). The oldest
  messages are dropped, the newest kept, and the opening request always survives
  the trim because it is what the conversation is named after. Any over-long value
  is shortened in place with a note saying how much was left out. What a record
  lost is recorded in the record.
- Conversation export, from the sidebar or the command line, as Markdown for
  reading and sharing or as JSON for scripting. Markdown carries no tool payloads
  and no internal metadata.
- The console entry point honours `resume_last_conversation` like the window does,
  and gained `--conversation`, `--no-resume`, `--list-conversations` and
  `--export`. Resumption happens once, before the window is built, so a resumed
  transcript is never replayed into the view twice.
- `QM_CONFIG_DIR` now moves the whole profile, cache and data included, and
  `QM_DATA_DIR` / `QM_CACHE_DIR` move those two specifically. Previously only the
  settings file moved, so the same application kept two profiles depending on
  whether it was started by flag or by environment variable, and a redirected run
  still wrote conversation records into the real user profile.

### Fixed

- **A clean shutdown no longer marks its conversation as interrupted.**
  `shutdown()` cancelled and then asked whether a turn was in flight, so the answer
  was always yes: every conversation was recorded as unfinished when the
  application quit normally, and reopened as if it had been cut short.
- Message ids and timestamps survive a restore. Without them the next autosave
  rewrote the real history as "this just happened", so a conversation slowly lost
  its own age.
- A tool result restored from disk that cannot be rebuilt is dropped rather than
  shown as an empty line, and a turn whose recorded finish precedes its start
  reports no duration instead of a large negative one.
- Refreshing the conversation list no longer emits a selection, which could reopen
  the conversation already open and stack a second copy of its transcript.
- Switching conversations clears tool results and turn history, not just messages.

**Conversations (v0.3)**

- Conversations are stored on disk, one JSON file each under the application data
  directory, and the most recent one is reopened at startup: transcript restored,
  project reopened when that folder still exists. New settings
  `agent.persist_conversations`, `agent.resume_last_conversation` and
  `agent.max_stored_conversations` control this; the first defaults to on, which
  is a change from v0.2 where a conversation died with the process.
- Full conversation lifecycle through the application and the sidebar: list,
  reopen, rename and delete, with the stored project and provider recorded in the
  record. `New conversation` keeps the previous one rather than discarding it, and
  a name you set stays with the conversation across later turns.
- Renaming a listed conversation updates it in place instead of rebuilding the
  list, so a rename does not make you fight a moving dropdown.
- Writes are atomic (sibling temporary file plus `os.replace`), so a crash
  mid-write leaves the previous record intact. A corrupt record is quarantined as
  `.json.corrupt` instead of being deleted, one bad record does not hide the
  rest, and a record written by a newer version is skipped rather than misread.
  Records are versioned and older ones are migrated on read.
- Credential-shaped text is masked before anything is written, and API keys never
  enter a record at all.

### Fixed

- A turn's summary is now recorded before the end-of-turn event fires, so a
  conversation saved as a turn ended holds the same turn data the session does.
- Opening a conversation whose project had moved no longer leaves that missing
  path behind: the next conversation you open, even a perfectly healthy one, no
  longer claims your project is unavailable. Opening a project by hand also
  clears the stale notice.

### Changed

- Command output is now bounded from both ends. The cap keeps the head *and* the
  tail of stdout and stderr, with a visible marker where the middle was dropped.
  Previously the head alone survived, which silently discarded the traceback and
  failure summary of a failing test run -- the one output the agent needed in
  order to fix the failure it had just caused. `stdout_omitted_chars` /
  `stderr_omitted_chars` now report how much was dropped.
- The agent refuses to repeat an identical tool call within a turn when nothing
  has changed since it last ran, and tells the model why. Any successful
  `write_file`, `edit_file`, `delete_file` or `undo_file` reopens every call, so
  validating after an edit -- the normal shape of the work -- is untouched.
  `run_command` deliberately does not count as evidence: the application cannot
  know what a command changed, so it will not pretend it did.

### Fixed

- The system prompt no longer tells the model it cannot run commands or modify
  files. That was v0.1 wording, and it fought the agent on every request that
  involved making a change. It now describes the real tool set, the
  locate/edit/verify loop, and the approval and refusal rules.
- The user documentation no longer claims the agent cannot fix a bug or run
  tests.
- Restoring a conversation brings back the transcript only. Approval session
  grants, undo history and pending tool calls are never persisted or replayed,
  and an interrupted turn is recorded as unfinished rather than as complete.

### Added

**Code navigation (v0.3)**

- `code_outline` lists every class, function, method and property in a Python
  file with its exact line range, signature, decorators and docstring summary.
  Because `edit_file`'s `replace_lines` addresses line ranges, the model can now
  aim a precise edit at one function without reading the whole module.
- `find_references` reports where a symbol is defined, where it is imported and
  where it is used, including import lines and attribute access, so a rename or
  behaviour change can be scoped before it is made.
- `find_references` now resolves imports to real workspace files: absolute
  (`pkg.store`), relative (`from . import x` anchored on the importing file's
  own package) and aliased (`Store as Repository`) forms. It reports
  `dependent_files` -- the modules a change actually reaches, tests included --
  and one level of import expansion from them (`related`, switchable with
  `expand`). Results carry `unresolved_imports`, `ambiguous` and an explicit
  `resolution` string, because this is name and import matching with no type
  inference and the model must not read it as more.
- `write_file` and `edit_file` now report the files that import the file they
  just changed (`dependents`, with `dependents_truncated` when the scan was cut
  short). A cross-file change only stays correct if the agent notices the
  callers, and it should not have to remember to ask: the caller list arrives in
  the same observation as the edit. Creating a file skips the scan, because
  nothing could have been importing it.
- `project_symbols` maps every definition in the project in one call, filterable
  by name fragment or by kind, so locating code the agent has not read does not
  cost one outline per file. It is a map, not an index: nothing is cached or
  persisted, because a stale index is worse than none.
- Both tools are read-only, deterministic and offline: no index, no embedding
  service and no background process, so they work with every provider including
  a local one. Non-Python files, unparsable files and unreadable files are
  refused or counted rather than guessed at, and reference matching
  over-reports by design so a call site is never missed.

**Filesystem**

- `undo_file` puts one file back the way it was before the newest change the
  agent made to it. `write_file` and `edit_file` record a pre-image in a
  session-scoped undo list — a stack per file, holding the exact bytes and a
  SHA-256 digest — and one call reverses one step. Restoring absence when the
  change created the file is supported; `delete_file` remains deliberately
  irreversible and cannot be undone. An undo that would overwrite a file changed
  outside the agent is refused rather than applied, and `undo_file` always asks,
  like `delete_file`.
- Structured diffs: `write_file` and `edit_file` now report what they changed —
  a bounded unified diff, the number of lines added and removed, and whether the
  text was truncated — in `ToolResult.data`. `diff_kind` names the cases with
  nothing to compare (`created`, `unavailable`); `delete_file` reports no diff.

### Fixed

- `max_command_output_chars` now reaches the command tool. The setting was
  loaded, validated and documented but never passed to `run_command`, so the cap
  was always the built-in 20000 characters and changing it did nothing. Both
  stdout and stderr are capped by the configured value, and truncation is still
  reported through `stdout_truncated` / `stderr_truncated`.
- The v0.2 notes no longer overstate the approval guarantees: deleting a file and
  running a command can be approved once, what they cannot receive is a session
  grant; auto-approve reads is on by default rather than off; and the reason
  line and risk classification are shown for commands, not for every tool.

---

## [0.2.0] - 2026-07-10

A controlled coding agent: the model can now change the workspace and run
approved commands, and every such action is authorised by the application rather
than by the model.

Baseline: `62a61d9b11fbcfdca5228c8dc5c495b8fe1941d7` (v0.1, frozen).

### Added

**Filesystem tools**

- `write_file` creates or replaces a text file; parent folders are created only
  when the call explicitly authorises it.
- `edit_file` applies one deterministic edit (`replace`, `replace_all`,
  `insert_before`, `insert_after`, `replace_lines`, `append`, `prepend`) with an
  explicit precondition, so a stale request fails instead of overwriting work.
- `delete_file` removes a single file after an explicit, non-session approval.

**Command execution**

- `run_command` with working-directory containment, timeout, cancellation,
  bounded stdout/stderr, exit code and a credential-stripped environment.
- An application-level command policy classifying commands as safe, mutating,
  destructive, unknown or blocked, with a non-overridable deny list.

**Git awareness**

- `git_status`, `git_diff` and `git_log`, structurally read-only.

**Approvals and audit**

- Human approval prompts for every `ASK` decision, with Allow, Allow Once, Deny
  and Allow for Session. The prompt shows the operation, target, workspace,
  arguments, the reason the model gave and the risk class.
- A `permission_approval` event recording what the user chose, separate from the
  resulting decision.

**Configuration**

- `auto_approve_writes`, `command_timeout_seconds`, `max_command_output_chars`
  and `git_enabled`.

### Security

- A blocked command is refused *before* the approval request is built, so the
  prompt never offers Allow for something the application forbids.
- Model-provider credentials are stripped from the environment of every command
  the agent runs.
- Git mutating verbs are blocked, so "mutations are deferred" holds even when
  reached through the generic command tool.

### Fixed

- `auto_approve_reads = false` never took effect: two wildcard rules shared a
  sort key, so the stricter rule was unreachable. The permission policy is now
  assembled in an explicit, documented order.
- `edit_file` appending text that already ended in a newline produced a
  trailing blank line.
- `pip install -e .` was classified high risk, because `-e` means "execute this
  string" for `node` but "editable install" for `pip`.

### Known limitations

- Application-level command policy is **not** an OS sandbox.
- Cancellation terminates the direct child only.
- Directory-tree deletion is not supported.

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
