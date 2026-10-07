.. _developer-logging:


=======
Logging
=======


Logging is structured, redacted and per-user. Stack traces appear only in debug
mode.

Locations
=========

.. code-block:: text

    Windows   %LOCALAPPDATA%\QuantsMind-Agent\QuantsMind-Agent\logs\quantsmind-agent.log
    Linux     ~/.local/state/quantsmind-agent/logs/quantsmind-agent.log
    macOS     ~/Library/Logs/quantsmind-agent/quantsmind-agent.log

Override with ``QM_LOG_DIR`` or ``--config-dir``. ``--no-log-file`` disables file
logging entirely.

Format
======

.. code-block:: text

    2026-03-14 14:02:11 INFO     quantsmind_agent.agent [12345] agent turn failed
    2026-03-14 14:02:11 WARNING  quantsmind_agent.agent [12345] permission DENY for read_file on ../..

The process id is included so interleaved runs are distinguishable.

Levels
======

.. list-table::
   :header-rows: 1
   :widths: 18 82

   * - Level
     - Used for
   * - ``DEBUG``
     - Tool arguments, request shapes, full tracebacks. Enable with
       ``--debug`` or the Developer mode checkbox in Settings.
   * - ``INFO``
     - Startup, provider and model in use, turn outcomes, permission decisions.
   * - ``WARNING``
     - Recoverable failures: denied permissions, tool errors, protocol fallbacks.
   * - ``ERROR``
     - Startup failure, unexpected agent errors (always with a traceback).
   * - ``CRITICAL``
     - Reserved for a future crash handler.

Redaction
=========

A filter is attached to every handler, so redaction cannot be bypassed by
logging in a new place:

.. code-block:: text

    Authorization: Bearer sk-abc...      ->  Authorization: ***redacted***
    api_key="sk-abc..."                 ->  api_key="***redacted***"

Sensitive *headers* (``authorization``, ``x-api-key``, ``cookie``, ...) are
replaced wholesale, and sensitive *keys* (``api_key``, ``password``, ``token``,
...) are masked when a mapping is passed through
:func:`~quantsmind_agent.logging_setup.safe_mapping`.

Verify it yourself:

.. code-block:: powershell

    Select-String -Path "$env:LOCALAPPDATA\QuantsMind-Agent\QuantsMind-Agent\logs\quantsmind-agent.log" `
        -Pattern "sk-|Bearer |api_key"

Rotation
========

2 MB per file, three backups (``log_file``, ``log_file.1``, ...). A long-running
install therefore keeps at most ~8 MB of logs.

Programmatic use
================

.. code-block:: python

    from quantsmind_agent.logging_setup import get_logger, setup_logging

    setup_logging(level="DEBUG", debug=True, log_file=Path("qm.log"))
    logger = get_logger("my.module")
    logger.info("starting with model %s", "gpt-4o-mini")

``get_logger`` namespaces loggers under ``quantsmind_agent`` so a single call in
``setup_logging`` configures the whole application. ``shutdown_logging()``
flushes and detaches every handler -- called on exit and by the test suite.

.. _developer-logging-debug:

Developer mode
==============

``--debug`` (or the Settings checkbox) raises the level to ``DEBUG`` and enables
tracebacks on errors. It does **not** change error *messages*: the GUI still
shows actionable text, never a stack trace. This is why a bug report can safely
include a log file.

Recommended log excerpt for a bug report
========================================

.. code-block:: powershell

    Get-Content "$env:LOCALAPPDATA\QuantsMind-Agent\QuantsMind-Agent\logs\quantsmind-agent.log" -Tail 200

Review it for anything sensitive first. The redaction filter covers credentials;
it cannot know that your project contains confidential business logic.

Related
=======

* :doc:`troubleshooting`
* :doc:`../security`
