.. _architecture-overview:


============
Architecture
============


Boundaries
==========

.. code-block:: text

    +-----------------------------------------------------------+
    |  GUI (PySide6)          quantsmind_agent.gui.*            |
    |  no provider logic, no tool bodies, no policy            |
    +---------------------------+-------------------------------+
                                | events, calls
    +---------------------------v-------------------------------+
    |  Agent Core               quantsmind_agent.agent           |
    |  loop: decide -> act -> observe -> decide                  |
    +----+-------------+----------------+-------------------------+
         |             |                |
    +----v-----+  +----v------+  +------v---------------------+
    | Model    |  | Tool      |  | Context Engine               |
    | Gateway  |  | Registry  |  | quantsmind_agent.context     |
    | .llm     |  | .tools    |  +------------------------------+
    +----+-----+  +----+------+                 |
         |             |                        |
    +----v-----  +-----v--------------+        |
    | Provider |  | Permissions       |        |
    | .providers| | .permissions      |        |
    +----+-----+  +-----+-------------+        |
         |              |                      |
    +----v--------------v----------------------v-----+
    |  Workspace .workspace   |  Configuration .config  |
    |  Session    .session    |  Secrets     .secrets   |
    +-------------------------------------------+
                |  Event Bus .events
                v
        structured events -> GUI, logs, audit
    +-----------------------------------------------------------+

Each arrow is an interface, not a call into internals:

* The GUI never imports a provider.
* The Agent Core never imports a provider or a tool body.
* Tools never check permissions; the registry does.
* Nothing but :class:`~quantsmind_agent.workspace.Workspace` touches user files.

Modules
=======

======================  ====================================================
Module                  Responsibility
======================  ====================================================
``app.py``              Composition root; CLI entry point
``agent.py``            Tool-calling loop, cancellation, streaming
``llm.py``              Provider-independent gateway and DTOs
``providers/``          Concrete providers (``openai_compat``)
``tools.py``            Tool contract, registry, argument validation
``workspace_tools.py``  The four v0.1 tools
``workspace.py``        Path containment, safe reads, metadata
``permissions.py``      Capability taxonomy and broker
``context.py``          Bounded prompt assembly
``session.py``          Conversation and turn state
``schema.py``           Message dataclasses and wire format
``config.py``           Validated settings, TOML I/O
``secrets.py``          Credential stores with masking
``paths.py``            Per-user directories
``events.py``           Agent events and the event bus
``errors.py``           Error taxonomy with user-safe messages
``logging_setup.py``    Logging with credential redaction
``services.py``         Application services the GUI calls (``TaskService``)
``tasks.py``            Plan and task models, lifecycle table, plan digest
``orchestrator.py``     Task state machine and evidence recording
``task_policy.py``      Scope, risk and per-step authorisation
``task_store.py``       Task persistence with atomic writes and quarantine
``recovery.py``         Interruption analysis and re-authorisation
``gui/``                PySide6 widgets and the worker bridge
======================  ====================================================

The application services
========================

Since v0.5 the window does not construct domain objects. It asks
:class:`~quantsmind_agent.services.TaskService` what is true and renders the
answer. The service owns the task store, builds orchestrators against the live
tool registry, records approval decisions, and reports interrupted work.

.. code-block:: text

    +-----------------------------------------------------------+
    |  GUI (PySide6)          quantsmind_agent.gui.*            |
    |  asks what is true; decides nothing about authorisation  |
    +---------------------------+-------------------------------+
                                |
    +---------------------------v-------------------------------+
    |  Application services      quantsmind_agent.services      |
    |  TaskService: propose, decide, adopt, renew, history      |
    +----+-------------+----------------+-------------------------+
         |             |                |
    +----v-----+  +----v------+  +------v---------------------+
    | Provider |  | Task      |  | Context Engine              |
    | Gateway  |  | lifecycle |  | quantsmind_agent.context    |
    | .llm     |  | .tasks    |  +-----------------------------+
    +----+-----+  | .orch...  |                |
         |        | .task_... |                |
    +----v--------v----------+                |
    |  Authorisation: permissions, task_policy |
    |  Persistence: task_store, recovery      |
    +-----------------------------------------+

This boundary matters for security rather than tidiness. Before it, ``_on_task_run``
in the window assigned ``task.status = APPROVED`` directly -- a widget deciding
lifecycle state. Now the service and the policy are the only writers, both are
testable without Qt, and ``tests/security`` asserts the invariants against them.

Where a decision lives
======================

* Lifecycle rules: :doc:`task-lifecycle` (``ALLOWED_TRANSITIONS`` in ``tasks.py``).
* Whether a step may run: ``task_policy.py``, consulted by the orchestrator at the
  moment of execution rather than at approval time.
* Whether a tool may run: ``permissions.py``, the single broker every tool goes
  through.
* Whether a plan digest still matches: ``tasks.plan_digest``, with
  ``DIGEST_VERSION`` inside the hashed payload.
* What happened: the execution records on the task, written on every transition.

The vertical slice
==================

A single user turn flows like this:

.. code-block:: text

    user text
      -> AgentCore.run
      -> ContextEngine.build        system prompt + trimmed history + question
      -> ChatRequest                 + tool schemas (OpenAI function format)
      -> ModelGateway.complete
      -> provider POST /chat/completions
      <- ChatResponse               text, or tool calls
      |
      +-- tool calls --> ToolRegistry.execute
                          -> PermissionBroker.require       ALLOW / DENY / ASK
                          -> tool.run(arguments)             workspace only
      <-- ToolMessage  observation text
      |
      +-- text --> AgentOutcome  -> streamed to the transcript
      -> agent_completed event -> activity panel, status bar

Every step emits events, so the interface can explain itself after the fact.

Extension points
================

v0.1 is deliberately small, but every seam a later version needs already exists:

======================  =============================================
Interface               v0.1 state / future use
======================  =============================================
``ModelProvider``       One implementation; add native providers here
``Tool``                Four read tools; add write/terminal/git here
``PermissionProvider``  Static rules; add interactive approval UI
``ContextProvider``     Summary-based; add retrieval and ranking
``Workspace``           Read-only; add writes behind permissions
``Agent``               One loop; add planning and sub-agents
``Session``             In-memory; add persistence
``EventBus``            Synchronous; add replay or remote sinks
======================  =============================================

See :doc:`../developer/extension-points`.

Design decisions
================

The significant decisions have ADRs:

* :doc:`adr/0001-python-pyside6` -- language and GUI toolkit.
* :doc:`adr/0002-openai-compatible-provider` -- one API contract, many vendors.
* :doc:`adr/0003-tool-calling-over-plugins` -- function calling instead of an
  MCP ecosystem in v0.1.
* :doc:`adr/0004-workspace-isolation` -- containment as the security boundary.
* :doc:`adr/0005-no-persistence-in-v01` -- in-memory sessions.
* :doc:`adr/0006-events-and-threading` -- event bus and the Qt worker bridge.

Related
=======

* :doc:`agent-core`, :doc:`model-gateway`, :doc:`tool-system`
* :doc:`security-boundaries`
