.. _security:


========
Security
========


Summary
=======

QuantsMind-Agent runs on your machine, with your privileges, and sends prompts
to the model endpoint you configure. It has no sandbox process and no telemetry.
What protects you is a small, reviewed set of code boundaries plus a
read-only tool set.

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
     - read-only, confined to the opened project
   * - Path traversal
     - rejected, including symlink and junction escapes
   * - Command execution
     - **not implemented**
   * - Outbound network
     - only the configured model endpoint
   * - Telemetry
     - **none**
   * - Updates
     - **none**; the application never phones home
   * - OS-level sandbox
     - **not implemented**

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

The workspace is the security boundary. Every tool path is resolved through
:class:`~quantsmind_agent.workspace.Workspace`, which refuses absolute paths,
drive letters, UNC paths, ``..`` traversal, home references, null bytes, and
anything resolving outside the root after links are followed.

.. code-block:: text

    workspace/
      src/            readable
      docs/           readable
      .git/           ignored
    ../secrets.txt    refused
    C:\Users\me\...   refused
    link -> C:\Users  refused (resolves outside)

Limitations, stated plainly:

* The agent runs with **your** privileges. Containment is enforced in code, not
  by the OS.
* Repository content is **data, not authority**. An instruction inside a README
  cannot become an action -- the resulting tool call is still validated -- but it
  can mislead the model about facts. There is no injection detector in v0.1.
* Binary files are refused; large files are capped.

Permissions
===========

Five capabilities are declared: ``READ_WORKSPACE``, ``WRITE_WORKSPACE``,
``EXECUTE_COMMAND``, ``GIT``, ``NETWORK``. Only ``READ_WORKSPACE`` is used.

.. code-block:: text

    unmatched rule      -> DENY
    ASK without approver -> DENY

That is the property to rely on: an unconfigured action is refused, and an
interactive request without an approver fails closed rather than proceeding.
Decisions are recorded in the broker history and published as events, so the
activity panel is an audit trail rather than decoration.

Command execution status
========================

**Not implemented.** There is no tool that starts a process, no shell
integration, and no path from model text to ``subprocess``. This is a feature of
v0.1's design, not an unfinished area -- see :doc:`releases/v0.1`.

When it arrives (v0.2), it will require ``EXECUTE_COMMAND``, an approval dialog
naming the exact command and working directory, containment of the working
directory to the workspace, output and time caps, and full capture in the
activity log.

Network status
==============

The only outbound traffic is the model request to the configured ``base_url``.
There is no URL-fetching tool, no web search, and no "follow the link" behaviour.
TLS uses ``httpx``'s ``certifi`` bundle rather than the Windows certificate
store, so a corporate proxy with a private CA requires environment-level
configuration -- a known limitation listed in :doc:`windows`.

Data flow
=========

.. code-block:: text

    your machine                       provider
    +-----------------------------+     +------------------+
    | project files ---------------|- - ->| your prompt      |
    | prompt + tool schemas        |- - ->| your API key     |
    |                             |     | (HTTPS)          |
    | response <-----------------|-|- - -|                  |
    | tool activity, transcript   |     +------------------+
    +-------------------------------------------+

What leaves the machine: the prompt, the tool schemas, and the API key in the
``Authorization`` header. Nothing else. There is no analytics, no crash upload
and no update check.

What stays: conversation text in memory (lost on exit), and operational logs in
your log directory.

.. warning::

   Logs contain file paths, error details and question text. They are not
   encrypted. Review a log before sharing it in a bug report.

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
   * - Deny by default
     - DONE
     - permission provider + tests
   * - No command execution
     - DONE
     - no such tool exists
   * - No telemetry
     - DONE
     - no network code path
   * - Credentials outside the repo
     - DONE
     - ``.gitignore`` + user config dir
   * - Real LLM validation
     - NOT RUN
     - manual, see :doc:`testing/acceptance-v0.1`
   * - OS-level sandbox
     - NOT DONE
     - documented limitation
