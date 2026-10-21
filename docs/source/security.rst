.. _security:


========
Security
========


Summary
=======

QuantsMind-Agent runs on your machine, with your privileges, and sends prompts,
tool observations and the configured credential to the model endpoint you
choose. It has no sandbox process and no telemetry. What protects you is a small,
reviewed set of code boundaries: workspace containment, a permission broker,
human approval for mutations, and an application-level command policy.

The current release can read, write, edit, delete and undo files, run approved
commands, and read Git state. Those capabilities make the boundaries more
important than they were in the read-only releases; they do not turn the
application into an operating-system sandbox.

.. list-table::
   :header-rows: 1
   :widths: 34 66

   * - Area
     - Status
   * - Credential storage
     - Windows Credential Manager (file fallback), never in config or logs
   * - Credential exposure
     - masked in the UI, scrubbed from logs, excluded from events
   * - Filesystem access
     - read, write, edit, delete and undo, confined to the opened project
   * - Path traversal
     - rejected, including direct symlink and junction escapes
   * - Project discovery
     - symbolic links, junctions and other reparse points are skipped
   * - Command execution
     - implemented, approval-gated and policy-filtered; **not an OS sandbox**
   * - Git access
     - read-only ``git_status``, ``git_diff`` and ``git_log``
   * - Outbound network
     - only the configured model endpoint by default; approved commands may
       make their own connections
   * - Telemetry
     - **none**
   * - Updates
     - **none**; the application never phones home
   * - OS-level sandbox
     - **not implemented**
   * - Release signing
     - **not implemented**; published Windows artifacts are unsigned

Credential handling
===================

Storage
-------

1. **Windows Credential Manager** via ``keyring``, used when available.
2. **``secrets.json``** in the per-user config directory, written with
   owner-only permissions, when no credential service is reachable.

Both live outside the repository. ``.gitignore`` additionally blocks
``secrets.toml``, ``qm-secrets.toml``, ``.env`` and ``*.key``.

Resolution order
----------------

1. the environment variable named by ``provider.api_key_env`` (default
   ``QM_API_KEY``);
2. the secret store.

The GUI settings path is preferred: it goes through the credential store rather
than the process environment.

Non-exposure
------------

.. list-table::
   :header-rows: 1
   :widths: 26 74

   * - Surface
     - Guarantee
   * - ``config.toml``
     - never contains a key; the field is not part of the model
   * - Settings dialog
     - password field, cleared on save, masked status text
   * - Sidebar
     - shows ``sk-******op``
   * - Transcript
     - never receives a key
   * - Log file
     - redaction filter scrubs ``Authorization``/``api_key`` patterns
   * - Events
     - failure payloads carry the exception type name only
   * - Error messages
     - written for users; never built from request dumps
   * - Session snapshot
     - no credential by construction

Verification
------------

.. code-block:: powershell

    Select-String -Path "$env:LOCALAPPDATA\QuantsMind-Agent\QuantsMind-Agent\logs\quantsmind-agent.log" -Pattern "sk-|Bearer |api_key"

Tests asserting these guarantees:
``tests/unit/test_secrets_paths.py``,
``tests/unit/test_errors_logging.py``,
``tests/gui/test_main_window.py::TestErrorHandling::test_no_credentials_appear_in_the_transcript``.

Workspace isolation
===================

The workspace is the security boundary. Every tool path -- read, write, edit,
delete and undo -- is resolved through
:class:`~quantsmind_agent.workspace.Workspace`, which refuses absolute paths,
drive letters, UNC paths, ``..`` traversal, home references, null bytes, and
anything resolving outside the root after links are followed.

Project discovery is deliberately stricter than an explicit path request.
Symbolic links, directory junctions and other filesystem reparse points are
omitted from listings and walks rather than followed. A workspace containing an
outward link therefore still produces a bounded snapshot, and the excluded path
is recorded in ``unknowns`` instead of pretending the project was complete.

.. code-block:: text

    workspace/
      src/            readable
      docs/           readable
      .git/           ignored
    ../secrets.txt    refused
    C:\Users\me\...   refused
    link -> C:\Users  refused (resolves outside)
    junction -> ...   skipped during discovery; direct reads refused

.. warning::

   The published v0.9.0 Windows MSI predates the reparse-point discovery fix.
   In that artifact, an outward junction can still fail project discovery even
   though direct reads through it are refused. The internal candidate passed the
   check; any public release candidate must repeat it against its own hash. See
   :doc:`qualification/defect-triage`.

Limitations, stated plainly:

* The agent runs with **your** privileges. Containment is enforced in code, not
  by the OS.
* Repository content is **data, not authority**. An instruction inside a README
  cannot grant a permission or widen the workspace, but it can mislead the model
  about facts. There is no injection detector in v0.9.0.
* Binary files are refused; large files are capped.
* A snapshot is a bounded sample, not a complete inventory. ``truncated``,
  ``sampled`` and ``unknowns`` say what was omitted.

Command execution
=================

``run_command`` is implemented. It requires ``EXECUTE_COMMAND``, always asks the
user, and passes through the command policy first. The policy is an
application-level deny list: it blocks destructive host operations, common
network-transfer and loader tools, and Git mutations. A blocked command is never
offered for approval.

The runner does not use a shell. It starts the program with an argument vector,
confines the working directory to the workspace, enforces a timeout, caps
captured output, and removes the configured provider key from the child
environment.

That is not isolation. An approved program:

* runs with the user's privileges;
* can spawn child processes, and cancellation stops only the direct child on
  Windows;
* can read files the user can read and write files the user can write, including
  paths outside the workspace if the program itself addresses them;
* can make network connections unless the policy blocks the program by name;
* can do anything else the user's account is permitted to do.

Approving a command approves that program, not a verified effect. Real isolation
requires OS-level mechanisms -- containers, job objects, AppContainer or a
dedicated low-privilege account -- and none of those are implemented. See
:doc:`architecture/command-execution`.

Permissions
===========

Five capabilities are declared: ``READ_WORKSPACE``, ``WRITE_WORKSPACE``,
``EXECUTE_COMMAND``, ``GIT`` and ``NETWORK``. Four are in active use; there is no
built-in network tool, so ``NETWORK`` is denied.

.. list-table::
   :header-rows: 1
   :widths: 26 18 56

   * - Permission
     - Default
     - Notes
   * - ``READ_WORKSPACE``
     - ``ALLOW``
     - Reads change nothing.
   * - ``WRITE_WORKSPACE``
     - ``ASK``
     - Writes and edits ask unless ``auto_approve_writes`` is enabled;
       ``delete_file`` and ``undo_file`` always ask.
   * - ``EXECUTE_COMMAND``
     - ``ASK``
     - ``run_command`` always asks.
   * - ``GIT``
     - ``ALLOW``
     - Only the read-only Git tools are registered.
   * - ``NETWORK``
     - ``DENY``
     - No built-in network tool.

.. code-block:: text

    unmatched rule      -> DENY
    ASK without approver -> DENY

That is the property to rely on: an unconfigured action is refused, and an
interactive request without an approver fails closed rather than proceeding.
Decisions are recorded in the broker history and published as events, so the
activity panel is an audit trail rather than decoration.

Network and data flow
=====================

The only built-in outbound request is the model request to the configured
``base_url``. There is no URL-fetching tool and no web search. An approved
``run_command`` can start a program that uses the network; the command policy
blocks common transfer and loader tools, but it is not a network sandbox.

TLS uses ``httpx``'s ``certifi`` bundle rather than the Windows certificate
store, so a corporate proxy with a private CA requires environment-level
configuration -- a known limitation listed in :doc:`windows`.

.. code-block:: text

    your machine                       provider
    +-----------------------------+     +--------------------------+
    | project files -------------|- - ->| prompt + file contents   |
    | prompt + tool schemas       |- - ->| tool schemas             |
    | tool observations           |- - ->| tool results             |
    |                             |     | API key in Authorization |
    | response <-----------------|-|- - -| (HTTPS)                  |
    | tool activity, transcript   |     +--------------------------+
    +-------------------------------------------+

What leaves the machine: the prompt, the tool schemas, tool observations
(including file contents the agent read), and the API key in the
``Authorization`` header. An approved command can send whatever that program
sends. There is no analytics, no crash upload and no update check.

What stays: live conversation state in memory, stored conversations and task
records if persistence is enabled, and operational logs in your log directory.

.. warning::

   Logs contain file paths, error details and question text. They are not
   encrypted. Review a log before sharing it in a bug report.

Prompt injection
================

Containment and approval reduce what an injected instruction can achieve, but
they do not make injection harmless. A malicious README can try to persuade the
model to write a file or run a command. The resulting call still needs the
correct permission and, for a mutation or command, a human approval. If a user
approves it, the action happens.

Mitigations in place:

* repository content is data, never authority;
* workspace containment limits which paths the built-in file tools can address;
* blocked commands never reach the approval dialog;
* the approval prompt names the tool, target, arguments and risk class.

Not yet in place: provenance labelling of file contents, an injection detector,
and an OS sandbox. Review approvals rather than assuming the model's summary is
complete.

Release trust
=============

The published v0.9.0 MSI and executable are unsigned. Windows SmartScreen will
warn on first run. No production code-signing certificate, hardware token, cloud
signing profile or trusted-publisher configuration exists in this repository.
Signing requirements and verification steps are recorded in the qualification
material; a self-signed certificate is not a production substitute.

Reporting a security issue
==========================

This is a **private** repository. Do not open a public issue for a security
problem.

Report privately:

1. Contact the maintainer directly (repository owner) with a private message.
2. Include the version, your platform, a description of the issue, and steps to
   reproduce.
3. If a log excerpt helps, produce it with ``--debug`` and **review it for
   sensitive content first**.
4. Please allow a reasonable window for a fix before public disclosure.

Please do not test against other people's repositories, attempt path escapes
outside your own machine, or exfiltrate data. Responsible disclosure keeps this
tool safe for everyone using it.

Security checklist before release
=================================

.. list-table::
   :header-rows: 1
   :widths: 44 14 42

   * - Check
     - Status
     - Enforced by
   * - No key in ``config.toml``
     - DONE
     - schema + test
   * - No key in logs
     - DONE
     - ``RedactingFilter`` + tests
   * - No key in the UI after saving
     - DONE
     - ``SettingsDialog`` + GUI test
   * - No key in events
     - DONE
     - gateway payload design
   * - Path traversal refused
     - DONE
     - ``Workspace.resolve`` + tests
   * - Symlink escape refused
     - DONE
     - resolved-path check + test
   * - Junction discovery handled
     - DONE IN SOURCE
     - reparse-point skip + junction regression tests
   * - Junction discovery in published v0.9.0
     - AFFECTED
     - published artifact predates the fix
   * - Deny by default
     - DONE
     - permission provider + tests
   * - Command execution approval-gated
     - DONE
     - registry + broker + policy
   * - Command policy blocks destructive commands
     - DONE
     - ``command_policy`` + tests
   * - No telemetry
     - DONE
     - no network code path
   * - Credentials outside the repo
     - DONE
     - ``.gitignore`` + user config dir
   * - Real LLM validation
     - NOT RUN
     - manual, see :doc:`qualification/manual-beta-acceptance`
   * - Human GUI and approval acceptance
     - NOT RUN
     - manual, see :doc:`qualification/manual-beta-acceptance`
   * - Signed release artifact
     - NOT DONE
     - no certificate; see :doc:`qualification/v1.0.0-readiness`
   * - OS-level sandbox
     - NOT DONE
     - documented limitation
