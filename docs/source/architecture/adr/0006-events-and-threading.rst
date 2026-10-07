.. _adr-0006:

====================================================
ADR-0006: Structured events and the Qt worker bridge
====================================================

Status
    Accepted (v0.1)

Context
-------

The GUI must show what the agent is doing while it is doing it: which tool ran,
whether it succeeded, what the provider is doing, how long things take. The same
information is needed for tests and for diagnostics.

Options considered
------------------

* **Invisible widget updates.** The agent calls the widgets directly. Fast to
  write, untestable headlessly, and it forces agent code to know about Qt.
* **Callbacks passed into the loop.** Works, but the signature grows with every
  new consumer, and there is no history to inspect afterwards.
* **A structured event bus.** One publish path, decoupled producers and
  consumers, retained history, and testable assertions on the sequence.

Decision
--------

Every subsystem publishes :class:`~quantsmind_agent.events.AgentEvent` records to
an :class:`~quantsmind_agent.events.EventBus`. The bus is synchronous, ordered,
fault-isolating, and keeps a bounded history.

Threading is separate from events: the Agent Core runs on a ``QThread``, and
:class:`~quantsmind_agent.gui.worker.AgentWorkerBridge` marshals worker signals
into the GUI thread.

Three implementation rules came out of building it, each of which cost a
regression test:

1. **Worker signals connect to methods, never to another signal's ``emit``.**
   Cross-thread signal-to-signal connections silently drop events in PySide6,
   which initially hid the second and later tool-activity rows.

2. **Resolve the agent per turn.** ``ApplicationContext`` rebuilds the
   ``AgentCore`` when the workspace or configuration changes, so the bridge holds
   ``context.current_agent`` (a callable). A direct reference ran the GUI against
   an agent with no tools.

3. **Defer completion.** ``turn_finished`` is re-emitted with
   ``QTimer.singleShot(0, ...)`` so already-posted tool events are rendered
   before the final answer.

Consequences
------------

* The core imports no Qt, so headless tests exercise production code.
* Event sequences are assertable, which is how ordering regressions are caught
  (``tests/gui/test_main_window.py::TestWorkerBridge``).
* The GUI stays responsive: the model call and every tool run happen off the GUI
  thread.
* The synchronous bus means a handler runs in the emitting thread. Handlers must
  not touch widgets; the window receives Qt signals instead, which *are* queued.
  A v0.2 event relay could marshal explicitly, removing the constraint.
* Bounded history (500 session events, 500 bus events) keeps memory flat during
  long sessions.

Related
=======

* :doc:`../events`
* :doc:`../gui`
* :doc:`../agent-core`