.. _architecture-events:


======
Events
======


Structured events are how the application explains itself. They are the single
mechanism behind the activity panel, the status bar and the diagnostic log
entries -- and the reason a future version can add a timeline view without
touching the agent.

Event types
===========

.. code-block:: text

    user_message        the request entered the loop
    agent_started       the loop began (carries the workspace path)
    model_request       about to call the provider
    model_response      the provider answered, or failed
    tool_requested      the model asked for a tool
    permission_decision ALLOW / DENY for that request
    tool_started        the tool body is running
    tool_completed      success, with duration
    tool_failed         expected failure or internal error
    agent_completed     final answer produced
    agent_failed        the turn ended with an error
    agent_cancelled     the user stopped the turn
    workspace_opened    a project was opened or reopened
    config_changed      settings were applied

Event object
============

.. code-block:: python

    @dataclass(frozen=True)
    class AgentEvent:
        type: AgentEventType
        payload: dict[str, Any]
        event_id: str        # evt-<8 hex>-<counter>
        timestamp: float     # time.time()
        session_id: str

Payloads are per-type contracts, documented in ``events.py``. The bus does not
enforce them, but the test suite asserts the important ones.

.. _architecture-events-bus:

The bus
=======

.. code-block:: python

    bus = EventBus()

    unsubscribe = bus.subscribe(AgentEventType.TOOL_COMPLETED, handler)
    unsubscribe_all = bus.subscribe_all(everything_handler)

    bus.emit(AgentEventType.AGENT_STARTED, {"workspace": r"C:\code\app"},
             session_id=session.id)

Properties that matter:

* **Synchronous.** Handlers run in emission order, in the emitting thread.
* **Fault isolating.** A handler that raises is swallowed and reported through
  ``on_handler_error``. One broken widget cannot break the agent loop.
* **Bounded history.** The last 500 events are retained for diagnostics.
* **Unsubscribe-able.** Every subscription returns its own callable.

Threading caveat
================

Because the bus is synchronous, a handler runs in the *emitting* thread. The
application context therefore subscribes to the bus from the GUI thread while
the agent runs on a worker thread. That is safe only because those handlers do
not touch widgets -- the window subscribes separately and receives Qt signals,
which *are* queued. See :doc:`gui`.

A future event relay that marshals to the GUI thread would remove the
constraint; it is not needed for v0.1.

Where events are consumed
=========================

.. code-block:: text

    Agent Core      emits the whole lifecycle
    Tool Registry   emits tool_requested/started/completed/failed,
                    permission_decision
    Model Gateway   emits model_request, model_response
    GUI window      agent_started -> "Working…"
                    model_request -> status bar
                    permission_decision -> activity panel
    ApplicationContext  records every event into the session (bounded)
    Tests           assert the sequence, so regressions are visible

Payload hygiene
===============

Event payloads are observable output and may be written to logs. They therefore
never contain credentials:

* ``model_response`` on failure carries the exception *type name* only;
* ``tool_requested`` arguments pass through the redaction helper;
* ``model_request``/``model_response`` carry provider name, model name, counts
  and latency.

.. code-block:: text

    {"provider": "openai_compatible", "model": "gpt-4o-mini",
     "content_chars": 812, "tool_calls": ["read_file"],
     "finish_reason": "stop", "latency_ms": 1420.5, "total_tokens": 431}

Asserting an event sequence
===========================

.. code-block:: python

    seen = []
    harness.events.subscribe_all(lambda event: seen.append(event.type))

    harness.run("Read the README and summarize it.")

    assert seen == [
        AgentEventType.USER_MESSAGE,
        AgentEventType.AGENT_STARTED,
        AgentEventType.MODEL_REQUEST,
        AgentEventType.MODEL_RESPONSE,
        AgentEventType.TOOL_REQUESTED,
        AgentEventType.PERMISSION_DECISION,
        AgentEventType.TOOL_STARTED,
        AgentEventType.TOOL_COMPLETED,
        AgentEventType.MODEL_REQUEST,
        AgentEventType.MODEL_RESPONSE,
        AgentEventType.AGENT_COMPLETED,
    ]

``tests/unit/test_tools.py::TestToolRegistry::test_events_are_emitted_in_order``
pins the tool-event subsequence, and the integration tests pin the turn.

Related
=======

* :doc:`agent-core`, :doc:`tool-system`, :doc:`gui`
* :doc:`../developer/testing`
