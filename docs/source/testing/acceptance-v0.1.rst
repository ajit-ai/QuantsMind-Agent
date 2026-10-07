.. _acceptance-v0.1:


======================
v0.1 acceptance report
======================


This report records **actual** results. Verdicts are ``PASS``, ``FAIL``,
``NOT RUN`` or ``BLOCKED``.

.. important::

   Automated results and manual results are reported separately. A passing CI
   run says nothing about GUI rendering on a Windows 11 desktop or about the
   behaviour of a real model. Nothing below is inferred from a different
   category.

Release under test
==================

.. list-table::
   :header-rows: 1
   :widths: 30 70

   * - Field
     - Value
   * - Version
     - 0.1.0
   * - Starting commit
     - ``182c6ae300ee44f5ca776c03827468d1458af219`` (repository initial commit)
   * - Environment
     - Windows 11, CPython 3.14.7, PySide6-Essentials 6.11.2, ruff 0.16.2,
       mypy 2.3.0, pytest 9.1.1, Sphinx 7.4.7
   * - Test isolation
     - ``QT_QPA_PLATFORM=offscreen``; every test uses a temporary
       configuration directory and a file-backed secret store

Automated results
=================

Executed from the repository root with the commands in the right-hand column.
Every command was actually run; every ``PASS`` below reflects an observed
result, not an expectation.

.. list-table::
   :header-rows: 1
   :widths: 24 12 64

   * - Gate
     - Verdict
     - Command / result
   * - Lint
     - PASS
     - ``ruff check .`` -> *All checks passed!*
   * - Format
     - PASS
     - ``ruff format --check .`` -> *66 files already formatted*
   * - Static types
     - PASS
     - ``mypy`` -> *Success: no issues found in 61 source files* (strict mode)
   * - Unit tests
     - PASS
     - ``pytest tests/unit -q`` -> **307 passed, 1 skipped**
   * - Integration tests
     - PASS
     - ``pytest tests/integration -q`` -> **46 passed**
   * - GUI tests (offscreen Qt)
     - PASS
     - ``pytest tests/gui -q`` -> **44 passed**
   * - Full suite
     - PASS
     - ``pytest -q`` -> **397 passed, 1 skipped**
   * - The one skip
     - n/a
     - ``test_symlink_escape_is_rejected`` needs Developer Mode or elevation
       to create a symlink on Windows; it skips rather than passing vacuously
   * - RST normalisation
     - PASS
     - ``python scripts/normalize_rst.py --check`` -> *Would fix 0 issues*
   * - Documentation build
     - PASS
     - ``python scripts/build_docs.py --clean`` -> **zero warnings**
       (``warningiserror=True``); wrote ``docs/build/html/index.html``
   * - Package build
     - PASS
     - ``python -m build`` -> ``quantsmind_agent-0.1.0.tar.gz`` and
       ``quantsmind_agent-0.1.0-py3-none-any.whl``
   * - Wheel contents
     - PASS
     - 35 entries, package only (no tests, no docs); metadata declares the five
       runtime dependencies and the three extras
   * - Editable install + entry point
     - PASS
     - ``pip install -e .``; ``quantsmind-agent --help`` lists all five
       options; ``import quantsmind_agent`` reports ``0.1.0``
   * - CLI configuration report
     - PASS
     - ``quantsmind-agent --workspace .acceptance-fixture/widget-factory
       --check-config`` prints the resolved workspace and the four registered
       tools; exit ``1`` without a key, ``0`` for a keyless local endpoint
   * - Window construction and rendering
     - PASS
     - ``tests/gui/test_startup_smoke.py``: window shown at 1180x800,
       ``grab()`` returns a non-null pixmap, ``close()`` completes cleanly
   * - Acceptance fixture
     - PASS
     - ``scripts/make_acceptance_fixture.py`` generated the disposable project;
       ``project_structure`` and ``search_content`` returned correct results
       against it through the real tool registry
   * - Real LLM
     - NOT RUN
     - requires a provider credential; see below
   * - Real coding task
     - NOT RUN
     - requires a provider credential; see below

Counts are the exact output of the runs recorded above.

Bugs found and fixed by these tests
===================================

Worth recording, because "the tests pass" means less when the tests never
caught anything.

.. list-table::
   :header-rows: 1
   :widths: 46 54

   * - Bug
     - Regression test
   * - Cross-thread signal-to-signal connections silently dropped events, so
       later tool activity never appeared
     - ``gui/test_main_window.py::TestWorkerBridge``
   * - The GUI bridge held a stale ``AgentCore`` after a workspace rebuild, so
       requests ran with no tools registered
     - ``gui/harness.py`` + ``gui/test_main_window.py``
   * - A final answer was lost when a provider could not stream
     - ``gui/test_main_window.py``, ``integration/test_streaming_and_app.py``
   * - **Stop** pressed immediately after **Send** was cleared by the new turn
     - ``integration/test_streaming_and_app.py::TestCancellation``
   * - Credential-shaped errors were re-wrapped as network errors, hiding 401
       from the user
     - ``unit/test_provider.py::TestComplete``
   * - ``Workspace.relative`` rejected the absolute paths the workspace itself
       produced
     - ``unit/test_workspace.py``
   * - A boolean satisfied an ``integer`` schema check (Python's ``bool`` is an
       ``int``)
     - ``unit/test_tools.py::TestArgumentValidation``
   * - ``ASK`` with no approver wired up could not reach ``DENY``
     - ``unit/test_permissions.py::TestBroker``
   * - ``--workspace`` was ignored by ``--check-config``
     - ``integration/test_cli.py::TestCheckConfig``
   * - A tool call's completion event could be rendered after the final answer
     - ``gui/test_main_window.py::TestWorkerBridge``

Windows 11 manual acceptance
============================

Procedure: :ref:`windows-manual-checklist` in :doc:`../windows`.

.. list-table::
   :header-rows: 1
   :widths: 24 16 60

   * - Step
     - Verdict
     - Note
   * - Launch, window renders
     - PARTIAL
     - the window is constructed, sized, shown and rendered to a pixmap by an
       automated test under offscreen Qt; no human has seen it on a display
   * - Workspace selection
     - PARTIAL
     - ``Workspace`` containment and the sidebar logic pass automated tests;
       the native folder dialog has not been opened by a person
   * - Configuration dialog
     - PARTIAL
     - widgets, validation and credential hiding pass automated tests; visual
       layout unverified
   * - Message submission
     - PASS (automated)
     - full round trip through the real window with a scripted provider
   * - Tool activity display
     - PASS (automated)
     - ordering and completeness asserted in the transcript and the panel
   * - Error handling in the UI
     - PASS (automated)
     - a provider failure is rendered without a traceback
   * - Clean exit
     - PARTIAL
     - ``window.close()`` and ``shutdown_logging()`` pass; no human has
       observed process exit
   * - Visual appearance, DPI scaling, native dialogs, keyboard traversal
     - NOT RUN
     - no automated coverage

.. important::

   **NOT RUN -- MANUAL WINDOWS 11 ACCEPTANCE REQUIRED** for everything marked
   PARTIAL or NOT RUN. Do not read them as passing.

GUI acceptance
==============

Automated GUI tests (offscreen Qt) **PASS**: 44 tests covering startup, window
construction and rendering, workspace selection, the Settings dialog, message
submission, streaming, tool-activity display and ordering, cancellation, error
handling, and the credential-hiding guarantee -- all against the real
``MainWindow`` and the real agent stack.

What they do **not** cover:

* visual layout and DPI scaling on a real display;
* native folder and file dialogs;
* window resizing, focus and keyboard traversal;
* rendering with the Windows system theme (this environment's offscreen Qt has
  no system fonts, so text renders as boxes in a screenshot);
* screenshot comparison of any kind.

Real LLM acceptance
===================

Procedure: :ref:`manual-llm`.

.. list-table::
   :header-rows: 1
   :widths: 40 16 44

   * - Test
     - Verdict
     - Note
   * - QM-V01-LLM-001 real LLM connectivity
     - NOT RUN
     - requires a provider credential
   * - QM-V01-LOCAL-001 local endpoint, no key
     - NOT RUN
     - requires a local Ollama / llama.cpp server
   * - QM-V01-CODE-001 real coding-agent task
     - NOT RUN
     - requires a provider credential

.. important::

   **NOT RUN -- MANUAL ACCEPTANCE REQUIRED.** No claim is made about real-model
   behaviour: tool-argument quality, streaming fidelity, refusal behaviour,
   prompt sensitivity or token accounting.

   What *is* verified offline is the provider protocol itself -- request
   construction, SSE parsing, tool-call fragment accumulation,
   auth/404/429/timeout/network error mapping and malformed-response handling
   -- against an ``httpx`` mock transport (``unit/test_provider.py``,
   ``unit/test_provider_stream.py``), plus the full agent loop against a scripted
   provider (``integration/``). That is a different claim from end-to-end
   behaviour with a real model, and the distinction is deliberate.

   The acceptance fixture was generated and its tools were exercised directly:
   ``project_structure`` returned the correct tree and ``search_content``
   located ``def main()`` in ``widget_factory/main.py``. The workspace half of
   QM-V01-CODE-001 is therefore proven; the model half is not.

Packaging acceptance
====================

.. list-table::
   :header-rows: 1
   :widths: 40 16 44

   * - Test
     - Verdict
     - Note
   * - ``python -m build`` (sdist + wheel)
     - PASS
     - artifacts build, install, and expose working entry points
   * - PyInstaller one-directory bundle
     - NOT RUN
     - ``scripts\\package.ps1`` is written and documented, but PyInstaller was
       not installed in this environment; the packaged ``.exe`` has not been
       built or launched

What v0.1 was not verified to do
================================

Stated plainly, so no reader infers more than was tested:

* it was **not** run against a real language model;
* it was **not** displayed on a physical Windows 11 desktop;
* it was **not** packaged with PyInstaller and launched;
* it was **not** tested with a provider that lacks tool-calling support;
* file writes, commands, git and network tools **do not exist** in v0.1.

Scope confirmation
==================

.. list-table::
   :header-rows: 1
   :widths: 40 20 40

   * - Capability
     - Verdict
     - Evidence
   * - Launch on Windows 11
     - PARTIAL
     - entry point, package build and offscreen window rendering pass; the
       window has not been observed on a display
   * - Open a local project
     - PASS
     - ``integration/test_streaming_and_app.py``,
       ``gui/test_main_window.py``, ``integration/test_cli.py``
   * - Configure provider and model
     - PASS
     - ``unit/test_config.py``, ``gui/test_main_window.py`` (Settings dialog)
   * - Store a key without exposing it
     - PASS
     - ``unit/test_secrets_paths.py``, ``unit/test_errors_logging.py``,
       ``integration/test_cli.py``
   * - Send a request to a model
     - PARTIAL
     - offline provider tests pass; a real request is ``NOT RUN``
   * - Streamed response
     - PARTIAL
     - SSE parsing and GUI rendering pass; a real provider is ``NOT RUN``
   * - Inspect project files with tools
     - PASS
     - ``unit/test_workspace_tools.py``, ``integration/test_agent_loop.py``
   * - Show tool activity in the GUI
     - PASS
     - ``gui/test_main_window.py``
   * - Show the final response
     - PASS
     - ``gui/test_main_window.py``
   * - Preserve the conversation for the session
     - PASS
     - ``unit/test_session_events.py``
   * - Reject paths outside the workspace
     - PASS
     - ``unit/test_workspace.py``

Next steps to close this report
================================

1. Run the Windows 11 checklist (:doc:`../windows`) on a real desktop and
   replace every ``PARTIAL`` with ``PASS`` or ``FAIL``.
2. Run QM-V01-LLM-001 and QM-V01-CODE-001 with a real credential.
3. Install the packaging extra and run ``scripts\\package.ps1``, then launch
   ``dist\\QuantsMind-Agent\\QuantsMind-Agent.exe --check-config``.
4. Enable Windows Developer Mode and re-run the suite so
   ``test_symlink_escape_is_rejected`` executes instead of skipping.
5. Record each result here with the command and the observed output.

Validation commands used for this report
========================================

.. code-block:: powershell

    ruff check .
    ruff format --check .
    mypy
    pytest tests/unit -q
    pytest tests/integration -q
    pytest tests/gui -q
    pytest -q
    python scripts/normalize_rst.py --check
    python scripts/build_docs.py --clean
    python -m build
    pip install -e .
    quantsmind-agent --help
    quantsmind-agent --workspace .acceptance-fixture/widget-factory --check-config
    python scripts/make_acceptance_fixture.py --force

Each was run from the repository root with
``QT_QPA_PLATFORM=offscreen`` set.
