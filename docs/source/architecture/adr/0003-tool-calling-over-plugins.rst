.. _adr-0003:

======================================================
ADR-0003: Native tool calling instead of an MCP client
======================================================

Status
    Accepted (v0.1)

Context
-------

The agent needs tools. Tools must be discoverable by the model, typed enough to
be called correctly, permission-checked, and observable in the interface.

Options considered
------------------

.. list-table::
   :header-rows: 1
   :widths: 30 35 35

   * - Option
     - Strengths
     - Why not for v0.1
   * - **Native function calling**
     - One request carries the tools; the model returns typed calls; no extra
       process or protocol
     - Only what the model API supports; no cross-process sharing
   * - MCP client in v0.1
     - Reusable tool servers; a real ecosystem
     - Subprocess management, a second protocol, JSON-RPC or stdio transport,
       lifecycle, and a much larger review surface -- before one vertical slice
       works
   * - ReAct text protocol
     - Works with any model, including ones without tool APIs
     - Fragile parsing, no schema guarantees, much higher token cost

Decision
--------

Use the provider's native function-calling: tool schemas are serialised as
OpenAI ``tools`` definitions, and the response's ``tool_calls`` become
:class:`~quantsmind_agent.schema.ToolCall` objects.

Tool arguments are validated against the declared JSON Schema before execution
(required keys and primitive types), and the tool result is returned to the model
as a ``tool`` message with the originating call id, which is what the protocol
requires to pair them.

Consequences
------------

* Four tools cost one prompt section; there is no protocol machinery to operate.
* The tool contract (:class:`~quantsmind_agent.tools.ToolSpec`) stays the single
  place to declare a capability, so permission, schema and observability are
  declared once.
* Requires a provider with tool-calling support. This is stated in
  :doc:`../../getting-started` rather than discovered at runtime.
* Streaming tool calls are accumulated per index and only executed once the
  arguments parse -- a half-received call is never run.

Future MCP
----------

The tool contract is already the boundary an MCP server would expose: name,
description, JSON Schema, result. v0.2 can add an MCP client that converts remote
tools into :class:`~quantsmind_agent.tools.ToolSpec` objects and registers them in
the same registry, inheriting permissions and events for free.

Rejected alternative, restated
------------------------------

Shipping MCP in v0.1 would have made the first end-to-end run depend on a
subprocess protocol. A local, typed registry delivers the same user-visible
behaviour with far less to go wrong.