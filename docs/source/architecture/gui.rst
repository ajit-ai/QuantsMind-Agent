.. _architecture-gui:


===
GUI
===


The GUI is a thin view over the application. It contains no provider logic, no
tool bodies and no policy; it renders events and forwards intent.

Layout
======

.. code-block:: text

    +----------------------------------------------------------+
    | QuantsMind-Agent                                         |
    +-------------------+--------------------------------------+
    | Workspace         | Conversation                         |
    |  [Open project…]  |   You        14:02:11                |
    |  path display     |   ...                                  |
    |  [Close project]  |                                      |
    |                   |   Assistant  14:02:13                |
    | Model             |   ...                                  |
    |  model            |                                      |
    |  provider · url   |   Tool  14:02:12                     |
    |  key: sk-******op |   ✗ read_file — outside the workspace |
    |  [Settings…]      |                                      |
    |                   +--------------------------------------+
    | Session           | Tool activity                        |
    |  counters         |  ✓ 14:02:11 read_file — README.md    |
    |  [New conversation]|  ✓ 14:02:12 project_structure        |
    +----------------------------------------------------------+
    | [composer ................................] [Stop] [Send]|
    +----------------------------------------------------------+
    | status: model @ url | workspace | last activity          |
    +----------------------------------------------------------+

Files
=====

============================  ==================================================
``gui/main_window.py``        Layout, signal wiring, turn rendering
``gui/sidebar.py``            Workspace, model and session panels
``gui/conversation.py``       Transcript rendering (HTML)
``gui/activity.py``           Tool activity log
``gui/settings_dialog.py``    Provider/agent configuration
``gui/worker.py``             ``QThread`` bridge for the agent loop
``gui/style.py``              Stylesheet and role colours
============================  ==================================================

``quantsmind_agent.gui`` exports its widgets lazily, so ``import
quantsmind_agent.gui`` does not pull in Qt. The headless core never imports Qt at
all, which keeps unit and integration tests fast and display-server independent.

Threading
=========

Qt widgets may only be touched on the GUI thread, so the agent loop runs on a
``QThread``:

.. code-block:: text

    GUI thread                        worker thread
    -----------                       --------------
    Send clicked
      bridge.start(text)
      reset_cancel()  ---------------> AgentWorker.run(text)
    busy_changed(True)                   AgentCore.run(...)
      (window disables Send)               emitter callbacks
                                         text_delta.emit("Hel")
                                         tool_activity.emit(...)
                                         finished.emit(outcome)
    text_delta  -------------------->  append to transcript
    tool_activity --------------->    activity row + notice
    turn_finished --------------->    end block, metadata, counters
    busy_changed(False)
      (window re-enables Send)

Three rules keep this correct:

1. **Worker signals connect to methods, never to another signal's ``emit``.**
   Cross-thread signal-to-signal connections silently drop events in PySide6.
   ``AgentWorkerBridge`` therefore connects to ``_on_text_delta``,
   ``_on_tool_activity``, ``_on_turn_started``, ``_on_finished`` and
   ``_on_failed``, which re-emit the bridge's own signals.
   ``tests/gui/test_main_window.py::TestWorkerBridge`` is the regression test.

2. **The agent is resolved per turn.** Opening a project or saving settings
   rebuilds the ``AgentCore``; the bridge holds
   ``context.current_agent`` (a callable), not a direct reference. A stale
   reference would run the GUI against an agent with no tools.

3. **Completion is deferred.** ``turn_finished`` is re-emitted through
   ``QTimer.singleShot(0, ...)`` so tool-activity events the worker already
   posted are rendered *before* the final answer.

The window never freezes during a model call: the loop is not on the GUI
thread.

Rendering
=========

The transcript is a ``QTextBrowser`` filled with HTML:

* **You** / **Assistant** / **Tool** / **Notice** blocks with timestamps;
* user content is HTML-escaped, so model output cannot inject markup;
* streaming appends to an open assistant block;
* answers that were *not* streamed (a provider fallback) are still rendered --
  :meth:`MainWindow._show_missing_text` appends whatever the stream did not
  deliver.

The activity panel is a ``QListWidget`` with state glyphs and colours:
``→`` started (blue), ``✓`` completed (green), ``✗`` failed (red). Rows are
capped at 300 so a long session cannot grow without bound.

Configuration UI
================

``SettingsDialog`` reads and writes :class:`~quantsmind_agent.config.Configuration`
and stores credentials through ``ConfigManager``. The API-key field echoes and is
always cleared on save, so a stored key is never rendered again. Saving
rebuilds the live stack -- no restart required.

Accessibility and usability
===========================

* Labels carry ``objectName`` values the stylesheet targets for consistent
  contrast.
* Every control has a tooltip explaining consequence, not mechanics.
* The composer is a multi-line ``QTextEdit`` (no single-line truncation) with
  ``Ctrl+Enter`` to send.
* Errors use the ``ErrorText`` role: red, bold, never a traceback.

Known limitations
=================

* No dark-theme-specific palette; the stylesheet is tuned for light themes and
  inherits system colours elsewhere.
* Transcript search, copy-as-markdown and export are not implemented.
* The activity panel is not filterable.

Related
=======

* :doc:`events`
* :doc:`../user/conversation`
* :doc:`../windows`
