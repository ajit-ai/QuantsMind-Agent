.. _installation:


============
Installation
============


Supported environments
======================

Support is claimed only where it has been verified. *Native-verified* means the
application has actually been run on that operating system, not merely that the
code appears portable or that a build succeeded.

=================  =========  =================================================
Platform           Status     Evidence
=================  =========  =================================================
Windows 10/11 x64  Supported  Full suite and a packaged executable, built and
                               smoke-tested on Windows 11
Linux x64 (glibc)  Supported  Full suite in CI on Ubuntu (headless); no
                               on-desktop run yet
macOS arm64/x64    Planned    Native artifact builds run in CI; the GUI has not
                               been run on macOS
BSD                Not        Blocked at the dependency level, see below
                   supported
=================  =========  =================================================

Python 3.11, 3.12 and 3.13 are the supported interpreters; CI tests 3.11 and
3.12, and the code also runs on 3.14.

.. _installation-bsd-blocker:

Why BSD is not supported
========================

QuantsMind-Agent's only hard GUI dependency is PySide6-Essentials, and Qt
publishes binary wheels for Windows, macOS and Linux only. There is no PySide6
wheel for FreeBSD, NetBSD or OpenBSD, so `pip install` on those systems would
require building Qt 6 from source.

The application code itself is not the obstacle: it contains only three
OS-conditional branches (a Windows-only console flag in `command_tools` and
`git_tools`, and `platformdirs` for directories), and subprocesses are
always invoked with an argv list and `shell=False`. The blocker is entirely the
availability of a Qt binary.

Supporting FreeBSD would mean either maintaining Qt 6 for FreeBSD or finding a
maintained community build, neither of which is in scope. NetBSD and OpenBSD have
no practical GitHub Actions runner either. Revisit only if a maintained PySide6
port appears.

From source (recommended)
=========================

.. code-block:: powershell

    git clone https://github.com/ajit-ai/QuantsMind-Agent.git
    cd QuantsMind-Agent

    python -m venv .venv
    .venv\Scripts\Activate.ps1

    pip install -e ".[dev,docs]"

Then:

.. code-block:: powershell

    quantsmind-agent

Equivalent invocations:

.. code-block:: powershell

    python -m quantsmind_agent          # module entry point
    python -m quantsmind_agent --help   # command-line options

Running a packaged executable
=============================

A standalone build is produced with PyInstaller, which does not cross-compile:
each artifact is built on a native runner for its own operating system.

.. code-block:: console

    python -m pip install -e ".[packaging]"
    python scripts/build_executable.py --clean
    python scripts/smoke_test_executable.py

The build writes `dist/BUILD-METADATA.json` recording the version, commit,
layout, and the OS/architecture/Python it was produced on.

Layouts
-------

`onedir` (the default, and the one that is smoke-tested) produces a folder
containing `QuantsMindAgent[.exe]` plus the Qt plugins and shared libraries it
needs. The executable must be shipped together with that folder.

`--onefile` produces a single self-extracting executable. It is easier to copy
but slower to start, and antivirus engines flag large self-extracting binaries
far more often. Expect an unsigned build to be quarantined on some machines; the
artifact is not code-signed or notarised yet.

What is *not* in the bundle
---------------------------

Configuration, credentials, logs and the workspace you open all stay outside the
executable. `platformdirs` resolves their location at runtime, and
`--config-dir` or `QM_CONFIG_DIR` override it. Nothing user-specific is baked
in, so one artifact is safe to share.

Verifying a build without a desktop
-----------------------------------

`--check-config` loads the configuration, creates the application directories,
registers the tools and reports readiness, then exits. It is the supported way to
confirm that a bundle starts:

.. code-block:: console

    QuantsMindAgent --check-config --workspace ~/projects/my-app

It exits `0` when ready and `1` when not. `scripts/smoke_test_executable.py`
runs a battery of these checks. It does **not** open a window, so it cannot
verify rendering, the approval dialog, or window close; those remain manual
desktop acceptance steps.

Linux system libraries
----------------------

A packaged Linux build links against Qt's XCB platform plugin and needs these
present on the target system:

.. code-block:: console

    sudo apt-get install -y libegl1 libxkbcommon-x11-0 libdbus-1-3 libfontconfig1

Headless machines additionally need `QT_QPA_PLATFORM=offscreen`.

macOS
-----

The macOS build produces `QuantsMindAgent.app`. It is neither signed nor
notarised, so Gatekeeper will refuse to open it until a user allows it in
System Settings. Distributing it to other machines requires an Apple Developer
identity, signing and notarisation, none of which is configured here.

Credential storage
------------------

The API key is stored in the operating system keyring: Windows Credential
Manager on Windows, the Keychain on macOS, and the Secret Service (GNOME Keyring,
KWallet) on Linux. On a Linux machine with no Secret Service provider -- a bare
container, some minimal window managers -- `keyring` reports no usable backend
and the application falls back to a file-backed store in its data directory. That
fallback is plain file storage, so on such a machine the key is only as protected
as the file's permissions.

Command-line options
====================

.. list-table::
   :header-rows: 1
   :widths: 26 74

   * - Option
     - Effect
   * - ``--workspace PATH``
     - Open a project on startup instead of prompting
   * - ``--config-dir PATH``
     - Use ``PATH`` for config, secrets and logs (portable mode)
   * - ``--debug``
     - Verbose logging (developer mode)
   * - ``--no-log-file``
     - Do not write a log file
   * - ``--check-config``
     - Print a redacted configuration summary and exit

``--check-config`` is the fastest way to verify an installation:

.. code-block:: powershell

    > quantsmind-agent --check-config
    config file : C:\Users\<you>\AppData\Local\QuantsMind-Agent\QuantsMind-Agent\config.toml
    log file    : ...\logs\quantsmind-agent.log
    provider    : openai_compatible
    base url    : https://api.openai.com/v1
    model       : gpt-4o-mini
    api key     : sk-******op
    workspace   : (none)
    tools       : (none)
    ready       : no (Open a project folder first.)

The command exits with code ``0`` when ready and ``1`` when something is still
missing. It never prints a credential.

Runtime dependencies
====================

======================  ============================================
Package                 Why
======================  ============================================
``PySide6-Essentials``  Qt GUI (widgets only; no 3D/web engine)
``httpx``               HTTP client for the model gateway
``pydantic``            Configuration validation
``platformdirs``        Windows/Linux/macOS config and log locations
``keyring``             Windows Credential Manager access
======================  ============================================

Development extras add ``pytest``, ``ruff``, ``mypy``, ``build``; ``docs``
adds ``sphinx``; ``packaging`` adds ``pyinstaller``.

Where files are written
=======================

====================================  ================================
Path (Windows 11)                     Contents
====================================  ================================
``%LOCALAPPDATA%\QuantsMind-Agent\``  ``config.toml``
``%LOCALAPPDATA%\QuantsMind-Agent\``  ``secrets.json`` (file fallback)
Windows Credential Manager            the API key (primary store)
``%LOCALAPPDATA%\QuantsMind-Agent\``  ``logs\quantsmind-agent.log``
====================================  ================================

None of these live inside the repository, and all are covered by
``.gitignore``. Override them with ``--config-dir`` for a fully portable setup.

.. _installation-packaging:

Packaging a Windows executable
==============================

.. code-block:: powershell

    pip install -e ".[packaging]"
    scripts\package.ps1

This runs PyInstaller and produces ``dist\QuantsMind-Agent\QuantsMind-Agent.exe``
(one directory, so Qt's DLLs and plugins are collected correctly).

Packaging decision
------------------

A **one-directory** PyInstaller bundle is used rather than a one-file bundle or
an MSI installer:

* one-file extraction costs several seconds on every launch and makes antivirus
  scanning unreliable;
* one-directory starts instantly and is easy to replace or uninstall;
* an installer framework (Inno Setup, WiX, MSIX) adds build complexity that
  v0.1 does not justify, and is planned only if distribution requires it.

Trade-off: the user runs the ``.exe`` from ``dist\``; there is no Start-menu
entry. See ``scripts/package.ps1`` for the exact command.

Verifying the build
===================

.. code-block:: powershell

    pip install -e ".[dev,docs]"
    ruff check .
    ruff format --check .
    mypy
    pytest
    scripts\build-docs.ps1
    python -m build

Each command must exit with code ``0``. See :doc:`developer/testing` for details.
