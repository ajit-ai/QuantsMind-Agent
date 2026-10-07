.. _developer-troubleshooting:


===============
Troubleshooting
===============


Start here: the application never shows a stack trace, so the log is the place
to look.

First moves
===========

.. code-block:: powershell

    # 1. Is the installation wired up correctly?
    quantsmind-agent --check-config

    # 2. What did the application record?
    Get-Content "$env:LOCALAPPDATA\QuantsMind-Agent\QuantsMind-Agent\logs\quantsmind-agent.log" -Tail 100

    # 3. Reproduce with verbose logging
    quantsmind-agent --debug

.. _troubleshooting-table:

Symptom to cause
================

.. list-table::
   :header-rows: 1
   :widths: 34 32 34

   * - Symptom
     - Likely cause
     - Fix
   * - "No API key is configured"
     - Credential missing, or the endpoint requires one
     - Settings -> API key, or export ``QM_API_KEY``
   * - "The provider rejected the API key"
     - Wrong key for that endpoint, or a trailing space
     - Confirm key and base URL belong together
   * - "could not find that model or endpoint"
     - Wrong model name, or a missing ``/v1``
     - Use a preset, then change only the model
   * - "took too long to respond"
     - Timeout too low, or a slow model
     - Raise Request timeout in Settings
   * - "could not be reached"
     - No network, proxy, wrong host, or the local server is not running
     - Start the local server; check the base URL
   * - "is outside the current workspace"
     - The model asked for a path outside the project
     - Expected. Open the parent folder if the reference is legitimate
   * - Agent answers without using tools
     - The endpoint does not support tool calling
     - Use a tool-capable model; this is a provider requirement
   * - Agent says it cannot run commands
     - v0.1 has no such tool
     - Expected; see :doc:`../releases/v0.1`
   * - Blank window, no content
     - Qt platform plugin problem
     - Run with ``--debug``; try ``$env:QT_QPA_PLATFORM="offscreen"``
   * - "Open a project folder first"
     - No workspace yet
     - **Open project…** or start with ``--workspace``
   * - GUI hangs on send
     - Should not happen; the loop runs on a worker thread
     - Capture the log, then file an issue with the excerpt

Installation and start-up
=========================

**``quantsmind-agent`` is not recognised**
    The virtual environment is not active, or the package was not installed
    (editable installs provide the command). Re-activate and
    ``pip install -e .``.

**``ModuleNotFoundError: PySide6``**
    The GUI extra is missing. Install the project, not only the dev tools::

        pip install -e .

**Application does not start and no log appears**
    The configuration directory may be unwritable. Run with
    ``--config-dir .qm-local --debug`` to relocate both config and logs.

**A stale configuration file blocks startup**
    Startup falls back to defaults and logs a warning. To start clean::

        Rename-Item "$env:LOCALAPPDATA\QuantsMind-Agent\QuantsMind-Agent\config.toml" "config.toml.bak"

**Corrupted credential file**
    Delete ``secrets.json`` in the configuration directory and re-enter the key
    in Settings.

Project and workspace
=====================

**A file is "missing" though it exists**
    It is probably in an ignored directory (``.git``, ``node_modules``,
    ``__pycache__``, ...) or is binary. Check
    :doc:`../user/workspace` for the ignore list.

**A link inside the project cannot be read**
    Expected: links that resolve outside the project root are refused. Open the
    parent directory instead.

**Windows says the path is too long**
    Enable the Windows long-paths policy, or open a closer folder.

**Search finds nothing**
    Binary files are skipped. Narrow with ``file_pattern`` (``*.py``) or use a
    regex with ``use_regex``.

Model behaviour
===============

**The agent loops and never answers**
    Raise *Max agent iterations* in Settings, or ask a narrower question. The
    default (8) is deliberately bounded.

**The answer is wrong about the project**
    The agent only knows what tools returned. Ask it to cite
    ``path/to/file.py:42`` and verify the file was read in the activity panel.

**A tool failed repeatedly**
    Read the error text; the agent should correct its arguments rather than
    repeat a failing call. If it does not, the question may be too vague.

**Answers ignore the operator instructions**
    Extra instructions are appended to the system prompt, not to your message;
    keep them general and behavioural.

Documentation and packaging
===========================

**Documentation build fails**
    Warnings are errors by design. Fix the RST rather than suppressing::

        scripts\build-docs.ps1

**``scripts\package.ps1`` fails**
    Install the packaging extra (``pip install -e ".[packaging]"``). PyInstaller
    needs a writable ``build\`` and ``dist\``.

**The packaged app cannot start**
    Ship the whole ``dist\QuantsMind-Agent`` directory, not the ``.exe`` alone;
    Qt's DLLs and plugins must sit next to it.

Testing
=======

**GUI tests fail on Windows**
    Ensure the offscreen platform is selected; ``tests/gui/conftest.py`` sets it.
    If a plugin is missing, reinstall ``PySide6-Essentials``.

**``pytest`` cannot import ``quantsmind_agent``**
    ``pythonpath = ["src"]`` in ``pyproject.toml`` handles this. Otherwise run
    ``pip install -e .``.

**A test fails only in CI**
    Compare with ``ruff check .``, ``mypy`` and the full ``pytest`` run; CI runs
    all of them.

**A test passes with a real API key present**
    It should not. ``tests/conftest.py`` removes ``QM_API_KEY`` and
    ``OPENAI_API_KEY`` for every test; a failure here means the test depends on
    the network.

Still stuck?
============

Capture:

.. code-block:: text

    1. quantsmind-agent --version output (or the window title)
    2. the output of quantsmind-agent --check-config
    3. the last ~100 log lines from --debug
    4. the exact question that misbehaved
    5. whether the tool activity panel shows the expected calls

Review the log for anything sensitive before sharing it. The redaction filter
covers credentials, not confidential source code.

Related
=======

* :doc:`logging`
* :doc:`../user/settings`
* :doc:`../windows`
