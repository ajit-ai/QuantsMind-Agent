.. _adr-0001:

========================================================
ADR-0001: Python and PySide6 for the desktop application
========================================================

Status
    Accepted (v0.1)

Context
-------

v0.1 needs a real desktop GUI on Windows 11, direct filesystem access, HTTP
calls to a language-model API, automated tests and a path to future packaging
and VS Code integration. The repository was empty, so there was no prior
architecture to preserve.

Options considered
------------------

.. list-table::
   :header-rows: 1
   :widths: 25 30 45

   * - Option
     - Strengths
     - Why not
   * - **Python + PySide6 (Qt)**
     - Native widgets, threading primitives (QThread, signals), mature
       offscreen mode for headless GUI tests, excellent HTTP and validation
       libraries, straightforward PyInstaller packaging
     - Requires shipping Qt runtime; no ecosystem precedent in this repo
       (accepted: v0.1 is greenfield)
   * - Python + Tkinter
     - Zero dependencies, ships with CPython
     - Dated look, no signal/slot threading model, weak testability,
       no styling system
   * - Electron + TypeScript
     - Excellent UI ecosystem
     - Two runtimes, heavy footprint, duplicates Python's strengths poorly
   * - C# / WinUI or WPF
     - Native Windows feel, MSIX packaging
     - No in-repo precedent; slower to reach a working agent loop; weaker
       Linux CI story
   * - Tauri + web frontend
     - Small binaries
     - Rust toolchain plus a web stack; excessive for v0.1

Decision
--------

Python 3.11+ with **PySide6-Essentials** for the GUI, **httpx** for HTTP,
**pydantic** for configuration validation, **platformdirs** for per-user paths
and **keyring** for the Windows Credential Manager.

Essentials rather than the full PySide6 package: v0.1 needs widgets only, and
skipping the Addons bundle removes roughly half the runtime size.

Consequences
------------

* One language for the core, the GUI and the tests; no IPC, no second runtime.
* The core imports no Qt, so unit and integration tests need no display server.
  GUI tests use Qt's ``offscreen`` platform plugin.
* Qt's threading model provides the non-blocking requirement: the agent loop runs
  on a ``QThread`` and reports through queued signals.
* Packaging is PyInstaller in one-directory mode (see
  :ref:`installation-packaging`).
* Qt licensing (LGPL v3) is compatible with the project's MIT licence; the
  dynamic-linking requirements of the LGPL are satisfied by PyInstaller's
  one-directory layout.

Alternatives revisited when
----------------------------

* If the GUI ever needs to be embedded in VS Code, the headless core is already
  a library and only the view layer is replaced.
* If binary size becomes critical, ``PySide6-Essentials`` plus Qt deployment
  tooling is the lever; no architectural change is needed.