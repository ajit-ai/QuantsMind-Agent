.. _architecture-session:


=======
Session
=======


:class:`~quantsmind_agent.session.Session` holds the state of one application
run. It is in memory only.

.. _architecture-session-state:

State
=====

.. code-block:: text

    Session
      ├── id              conversation id (fresh per session)
      ├── workspace       the open Workspace, or None
      ├── messages        user / assistant / tool traffic
      ├── turns           one TurnRecord per user request
      ├── events          the last 500 agent events
      └── tool_results    every ToolResult produced this run

Turns
=====

.. code-block:: python

    record = session.begin_turn("Explain the structure of this project.")
    ...
    session.record_tool_result(result)
    session.end_turn(record)

    record.iterations      # model round-trips used
    record.tool_results    # results for this turn only
    record.final_text      # the answer
    record.duration_ms
    record.error           # user-safe message, or None
    record.cancelled
    record.succeeded

The GUI renders ``1 iteration(s), 3 tool call(s)`` after a turn from this record,
and the sidebar shows live message/turn/tool counts.

History
=======

:meth:`Session.history_for_context` returns only user and assistant messages,
dropping empty ones. Tool traffic is excluded from history but *is* sent within
the turn that produced it -- replaying old observations would spend context on
stale file contents.

Reset
=====

:meth:`Session.reset` clears the conversation, turns, events and tool results,
keeps the workspace by default, and issues a new conversation id. The application
context rebuilds the tool registry and agent at the same time, so a new session
cannot inherit a stale tool set.

Why nothing is persisted
========================

Persistence would mean a database or a file format, plus questions this version
cannot answer well: retention, deletion, encryption at rest, export, sync,
telemetry. The alternative -- in-memory sessions -- is easy to reason about and
makes the privacy statement verifiable:

> Conversation content stays in memory and in your log directory. The only data
> that leaves your machine is the model request you asked for.

v0.2 will add explicit, opt-in conversation history.

Diagnostics
===========

:meth:`Session.to_dict` produces a JSON-serialisable snapshot (messages, turns,
tool results, workspace path). It contains no credentials by construction, which
makes it safe to attach to a bug report after a manual review.

Related
=======

* :doc:`events`
* :doc:`../releases/v0.1`
* :doc:`adr/0005-no-persistence-in-v01`
