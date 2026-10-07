.. _installation:


============
Installation
============


Supported environments
======================

==========  =========  ====================================
Platform    Status     Notes
==========  =========  ====================================
Windows 11  Supported  Primary target; validated manually
Linux       Dev/CI     Headless, offscreen Qt for GUI tests
macOS       Dev/CI     Headless, offscreen Qt for GUI tests
==========  =========  ====================================

Python 3.11, 3.12 and 3.13 are the supported interpreters. The code also runs on
3.14.

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
