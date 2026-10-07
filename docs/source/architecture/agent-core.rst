.. _architecture-agent-core:


==========
Agent Core
==========


:mod:`quantsmind_agent.agent` contains the loop. It is provider-independent and
knows nothing about HTTP, Qt or the filesystem.

The loop
========

.. code-block:: text

    for iteration in 1..max_iterations:
        if cancelled:                      -> stop
        offer_tools = iteration < max_iterations
        messages = context.build(question, history, tools)
        request  = ChatRequest(messages, tools if offer_tools else [])
        response = gateway.complete(request)          # tool rounds
        response = gateway.stream(request)            # final round

        if response.tool_calls:
            run each call through ToolRegistry.execute
            append observations
            continue

        return final answer

Three properties are worth calling out.

The last iteration withholds tools
----------------------------------

With ``max_iterations = 8`` the eighth request carries no ``tools`` field. The
model therefore *cannot* ask for another tool call and must answer in prose.
Without this, a model that keeps exploring would burn the whole budget and
return nothing.

If the budget runs out anyway, the loop synthesises an actionable fallback::

    The agent reached its tool-call limit without producing an answer.
    Try a more specific request.

Tool rounds are never streamed
------------------------------

Streaming is used only for the round that produces the final answer. Tool-call
arguments arrive in fragments, and executing a half-received tool call is not
safe, so those rounds use blocking requests.

Recoverable mistakes are not fatal
----------------------------------

A tool that fails, a path outside the workspace, an unknown tool name, invalid
arguments: each becomes a ``TOOL ERROR`` observation and the loop continues.
The model gets a chance to correct itself. Only provider-level failures (auth,
network, timeout, protocol) end the turn with an error.

Cancellation
============

.. code-block:: python

    agent.cancel()          # takes effect at the next iteration boundary
    agent.reset_cancel()    # discard a pending cancellation

``cancel()`` while a loop is running sets the flag immediately; ``cancel()``
while idle marks the *next* turn. That is what makes a **Stop** click right
after **Send** take effect instead of being cleared by the new turn.

An HTTP request already in flight cannot be aborted. Instead, the late answer is
discarded: the user asked to stop, so it is not shown.

Outcome
=======

.. code-block:: python

    outcome = agent.run("Explain the structure of this project.")

    outcome.succeeded        # no error and not cancelled
    outcome.text             # the final answer
    outcome.iterations       # model round-trips used
    outcome.tool_results     # every ToolResult, in order
    outcome.cancelled
    outcome.stopped_reason   # final_answer | max_iterations | cancelled | ...
    outcome.error            # user-safe message, never a traceback
    outcome.tokens           # total tokens when the provider reports usage

``run`` never raises for an expected failure. Errors are returned on the outcome
and mirrored as an ``agent_failed`` event, which is what lets the GUI show a
message without a try/except at every layer.

Threading
=========

``AgentCore`` is synchronous and interruptible. The GUI runs it on a ``QThread``
(see :doc:`gui`) and posts results back through signals. Nothing in
:mod:`quantsmind_agent.agent` imports Qt.

API
===

.. code-block:: python

    from quantsmind_agent.agent import AgentCore, StreamEmitter

    agent = AgentCore(
        gateway, tools, context, session,
        max_iterations=8,
        use_streaming=True,
        request_options=lambda: {
            "model": "gpt-4o-mini",
            "temperature": 0.2,
            "max_output_tokens": 4096,
            "timeout_seconds": 120.0,
        },
    )

    emitter = StreamEmitter(
        on_text=lambda delta: print(delta),
        on_tool_start=lambda name, args: print(name, args),
        on_tool_end=lambda result: print(result.summary),
        on_iteration=lambda n: print("iteration", n),
    )
    outcome = agent.run("Where is the port configured?", emitter=emitter)

``request_options`` is a callable, so a settings change applies to the next
request without rebuilding the agent.

Related
=======

* :doc:`model-gateway`, :doc:`context-engine`, :doc:`tool-system`
* :doc:`events`
* :doc:`../releases/v0.1`
