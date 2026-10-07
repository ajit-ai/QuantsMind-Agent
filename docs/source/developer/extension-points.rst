.. _developer-extension-points:


================
Extension points
================


Every seam a later version needs exists in v0.1. This page states what each
interface looks like today and what it is *for* -- so extensions fit the design
instead of fighting it.

The interfaces
==============

.. list-table::
   :header-rows: 1
   :widths: 24 30 46

   * - Interface
     - v0.1 state
     - Planned use
   * - ``ModelProvider``
     - one implementation
     - native provider protocols, local inference, self-hosted gateways
   * - ``Tool``
     - four read tools
     - write, terminal, git, network, MCP-backed tools
   * - ``PermissionProvider``
     - static rules
     - interactive approval, team policy, audit sink
   * - ``ContextProvider``
     - summary-based
     - retrieval, ranking, symbol indexing, multi-repo context
   * - ``Workspace``
     - read-only
     - writes, diffs, multi-root workspaces
   * - ``Agent``
     - one loop
     - planning, sub-agents, verification passes
   * - ``Session``
     - in-memory
     - persistence, resume, export
   * - ``EventBus``
     - synchronous, bounded history
     - replay, timeline view, remote telemetry sink
   * - ``ToolResult``
     - structured data + error
     - diffs, structured patches, review workflow

ModelProvider
=============

.. code-block:: python

    from quantsmind_agent.llm import ChatRequest, ChatResponse, StreamChunk

    class AnthropicProvider:
        name = "anthropic"

        def __init__(self, api_key, timeout_seconds=120.0):
            self._api_key = api_key
            self.timeout_seconds = timeout_seconds

        def list_models(self):
            ...

        def complete(self, request: ChatRequest) -> ChatResponse:
            ...

        def stream(self, request: ChatRequest):
            ...

Register it:

.. code-block:: python

    from quantsmind_agent import providers

    providers._FACTORIES["anthropic"] = AnthropicProvider

Rules: translate to the shared DTOs; raise ``LLMError`` subclasses so the GUI
shows one consistent message per situation; never log the key.

PermissionProvider
==================

.. code-block:: python

    from quantsmind_agent.permissions import Decision, PermissionRequest

    class TeamPolicyProvider:
        def __init__(self, policy_document):
            self._policy = policy_document

        def decide(self, request: PermissionRequest) -> Decision:
            if self._policy.allows(request.permission.value, request.target):
                return Decision.ALLOW
            return Decision.ASK

Threading note: ``decide`` runs on the worker thread. A provider that needs UI
must marshal to the GUI thread -- v0.2 will ship an approval dialog that does
this.

ContextProvider
===============

.. code-block:: python

    class RetrievalContextProvider:
        def system_prompt(self) -> str: ...
        def workspace_summary(self) -> str: ...

Replace the summary with an index: embed files on open, rank them against the
question, inject the top matches plus a citation list. The budget logic in
:class:`~quantsmind_agent.context.ContextEngine` still applies, so a large
repository cannot blow the context window.

Tool
====

See :doc:`development` for a worked example. The contract that matters:

.. code-block:: text

    resolve every path through Workspace
    bound the output size
    return failures; raise only when exceptional
    declare the correct permission

A write tool, when it arrives, must:

1. resolve the target through ``Workspace`` (containment for free);
2. declare ``WRITE_WORKSPACE``;
3. return a structured diff in ``ToolResult.data`` so the GUI can show it;
4. require an interactive approver, because unattended writes are never
   acceptable.

EventBus
========

.. code-block:: python

    class AuditSink:
        def __init__(self, path):
            self._path = path

        def __call__(self, event):
            with self._path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps({
                    "type": event.type.value,
                    "timestamp": event.timestamp,
                    "session": event.session_id,
                    "payload": event.payload,
                }) + "\n")

    context.events.subscribe_all(AuditSink(path))

Remember: the bus is synchronous, so a slow sink slows the agent. Buffer in the
sink and flush asynchronously for production use.

Extending the GUI
=================

The window is a view. To add a panel:

.. code-block:: python

    class DiffPanel(QWidget):
        def __init__(self, parent=None):
            super().__init__(parent)
            self._text = QTextBrowser(self)

        def show_result(self, result: ToolResult) -> None:
            self._text.setHtml(result.to_observation())

    panel = DiffPanel()
    window.activity_panel_splitter.addWidget(panel)
    bridge.tool_activity.connect(lambda tool, summary, state: panel.show_result(...))

Widgets may only be touched on the GUI thread; receive data through bridge
signals, never by calling into the agent.

Design rules
============

* Extend through the interface, not by importing another subsystem's internals.
  If you need a Provider in the GUI, the composition root should hand it over --
  the GUI must not construct providers.
* Keep the headless core Qt-free. If a new core feature needs Qt, the feature
  belongs in the GUI layer.
* Add the test at the same level as the code: a new tool gets unit tests plus one
  integration test through the agent loop.
* Update the docs. A new capability is incomplete without a page describing it,
  its limits and its permissions.

Related
=======

* :doc:`development`
* :doc:`../architecture/overview`
* :doc:`../roadmap/roadmap`
