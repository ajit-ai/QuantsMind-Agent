# Security Policy

QuantsMind-Agent is a **private** repository. Please report security issues
privately, not through a public issue or a public pull request.

## Reporting a vulnerability

1. **Contact the maintainer directly** (the repository owner) with a private
   message.
2. Include:
   - the version (`QuantsMind-Agent --version`, or the window title);
   - your platform and Python version;
   - what the issue is and what an attacker could achieve;
   - steps to reproduce, ideally on a machine you control.
3. If a log excerpt helps, produce it with `--debug` and **review it for
   sensitive content first**. Logs are not encrypted.
4. Please allow a reasonable time for a fix before public disclosure.

Please do not test against other people's repositories, attempt path escapes
outside your own machine, or exfiltrate data. Responsible disclosure keeps this
tool safe for everyone using it.

## Security model in v0.9.0

QuantsMind-Agent runs on your machine, with your privileges, and sends prompts,
tool observations and the configured credential to the model endpoint you
choose. It has **no sandbox process** and **no telemetry**.

| Area | Status |
|---|---|
| Credential storage | Windows Credential Manager; permissions-restricted file fallback |
| Credential exposure | Masked in the UI; redacted from logs; excluded from events |
| Filesystem access | Read, write, edit, delete and undo, confined to the opened project |
| Path traversal | Rejected, including direct symlink and junction escapes |
| Project discovery | Symbolic links, junctions and other reparse points are skipped |
| Command execution | Implemented, approval-gated and policy-filtered; **not an OS sandbox** |
| Git access | Read-only `git_status`, `git_diff` and `git_log` |
| Outbound network | Only the configured model endpoint by default; approved commands may connect |
| Telemetry | **None** |
| Updates | **None** — the application never phones home |
| OS-level sandbox | **Not implemented** |
| Release signing | **Not implemented**; published Windows artifacts are unsigned |

Full details, including data flow and residual risks:
[`docs/source/security.rst`](docs/source/security.rst).

### What protects you

1. **The model cannot name its own capability.** Capabilities exist only
   because code registered a tool that declares one and the registry enforces
   the permission before the tool body runs.
2. **Paths are contained.** Every tool path is resolved through a single
   `Workspace` object that refuses absolute paths, drive letters, UNC paths,
   `..` traversal, home references, null bytes, and anything resolving outside
   the project root after links are followed.
3. **Policy lives in one place.** Tools do not check permissions; the registry
   does.
4. **The default answer is no.** Unmatched permission rules are `DENY`, and
   `ASK` without an approver degrades to `DENY`.
5. **Mutations and commands ask.** Writes, edits, deletes, undo and
   `run_command` need an explicit decision. Deleting, undo and running a command
   always ask, regardless of auto-approval settings.
6. **Blocked commands cannot be approved.** The command policy is evaluated
   before an approval prompt is built.
7. **Credentials stay local.** The key is only ever placed in an
   `Authorization` header. A redaction filter scrubs every log record.

### Known risks

- **The agent runs with your privileges.** Containment is enforced in code, not
  by the operating system.
- **An approved command is powerful.** It can spawn children, use the network,
  and do anything your account can do. The command policy is not a sandbox.
- **Prompt injection can mislead answers and actions.** Repository content is
  data, not authority, but an injected instruction that becomes an approved
  write or command does happen. There is no injection detector in v0.9.0.
- **Logs are not encrypted** and contain file paths and question text.
- **The configured endpoint receives your prompt, tool observations and your
  API key** by design.
- **The published v0.9.0 MSI is unsigned** and predates the junction-discovery
  fix. See the qualification record.

## Verifying the guarantees yourself

Check that no credential reached the log:

```powershell
Select-String -Path "$env:LOCALAPPDATA\QuantsMind-Agent\QuantsMind-Agent\logs\quantsmind-agent.log" `
    -Pattern "sk-|Bearer |api_key"
```

Expected: no matches.

Check that the workspace boundary holds:

```python
from quantsmind_agent.workspace import Workspace
from quantsmind_agent.errors import InvalidPathError

space = Workspace(r"C:\code\app")
for candidate in (
    "../../etc/passwd",
    r"C:\Windows\win.ini",
    "/etc/passwd",
    "~/secrets",
    r"\\server\share",
):
    try:
        space.resolve(candidate)
        print("LEAK:", candidate)
    except InvalidPathError:
        print("blocked:", candidate)
```

The corresponding automated tests are
`tests/unit/test_workspace.py`, `tests/unit/test_workspace_intel.py`,
`tests/unit/test_secrets_paths.py`, `tests/unit/test_errors_logging.py` and
`tests/gui/test_main_window.py::TestErrorHandling`.

## Credential hygiene for contributors

- Never paste a real API key into an issue, a commit, a test or a log excerpt.
- The test suite removes `QM_API_KEY` and `OPENAI_API_KEY` for every test; a
  test that depends on a real credential is a bug.
- `.gitignore` blocks `secrets.toml`, `qm-secrets.toml`, `.env` and `*.key`. If
  you add a credential file, add it there too.
- If you ever commit a real key, rotate it — the redaction filter only helps
  for logs, not for history.
