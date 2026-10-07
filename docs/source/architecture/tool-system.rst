.. _architecture-tool-system:


===========
Tool System
===========


Three pieces: a contract, a registry that enforces policy, and the v0.1 tool
implementations.

The contract
============

.. code-block:: python

    class ToolSpec:
        name: str                 # stable identifier, used in prompts and logs
        description: str          # what the model reads
        input_schema: dict        # JSON Schema
        permission: Permission    # capability required
        summary_template: str     # one-line activity label

    class Tool(Protocol):
        spec: ToolSpec
        def run(self, arguments: dict) -> ToolResult: ...

    class ToolResult:
        tool: str
        ok: bool
        summary: str
        data: dict
        error: str | None
        duration_ms: float

The registry is the enforcement point
=====================================

.. code-block:: text

    ToolRegistry.execute(name, arguments)
      1. resolve the tool               -> ToolNotFoundError if unknown
      2. coerce arguments (dict or JSON string)
      3. validate against input_schema  -> required keys, primitive types
      4. emit tool_requested
      5. PermissionBroker.require       -> DENY short-circuits here
      6. emit permission_decision
      7. emit tool_started
      8. tool.run(arguments)
      9. emit tool_completed or tool_failed
     10. return ToolResult

Tools never check permissions themselves. That is the point: a new tool cannot
forget to ask, because asking is not something a tool does.

Failure semantics
-----------------

* **Expected failures** (missing file, escaping path, no matches) return
  ``ToolResult.failure``. The agent sees them and can adapt.
* **Unknown tool, bad arguments, permission denial** also return a failed
  result, so one bad tool call cannot end a turn.
* **Unexpected exceptions** are caught, logged with a traceback in debug mode,
  and reported as ``Internal error in tool '<name>'``. A tool bug never crashes
  the application, and never leaks a traceback into the transcript.

Only ``ToolNotFoundError`` escapes :meth:`ToolRegistry.execute`, and the Agent
Core catches it to produce a failed observation.

Validation
----------

A small validator checks required keys and primitive types instead of pulling in
a schema library:

* ``"string"``, ``"number"``, ``"integer"``, ``"boolean"``, ``"array"``,
  ``"object"``;
* a JSON boolean is rejected where an integer is expected (Python's ``bool`` is
  a subclass of ``int``);
* ``None`` values skip the type check;
* unknown properties are ignored, so a chatty model is not punished.

Observations
============

.. code-block:: python

    result.to_observation(max_chars=8000)

renders the result as the text fed back to the model:

.. code-block:: text

    TOOL RESULT (read_file): read README.md (3 lines)
    { "path": "README.md", ... }

    TOOL ERROR (read_file): 'nope.py' does not exist in this workspace.

Long observations are truncated with an explicit marker so the model knows it is
looking at a slice rather than the whole file.

The v0.1 tools
==============

See :doc:`../user/tools` for arguments and sample output. In summary:

=====================  ==================  =========
Tool                   Permission          Read-only
=====================  ==================  =========
``project_structure``  ``READ_WORKSPACE``  yes
``list_directory``     ``READ_WORKSPACE``  yes
``read_file``          ``READ_WORKSPACE``  yes
``search_content``     ``READ_WORKSPACE``  yes
=====================  ==================  =========

Adding a tool
=============

.. code-block:: python

    from quantsmind_agent.permissions import Permission
    from quantsmind_agent.tools import ToolResult, ToolSpec

    class GrepTool:
        spec = ToolSpec(
            name="grep",
            description="Regex search across the workspace.",
            input_schema={
                "type": "object",
                "properties": {
                    "regex": {"type": "string"},
                    "path": {"type": "string", "default": "."},
                },
                "required": ["regex"],
            },
            permission=Permission.READ_WORKSPACE,
            summary_template="grep {regex!r}",
        )

        def __init__(self, workspace):
            self._workspace = workspace

        def run(self, arguments):
            ...

    registry.register(GrepTool(workspace))

The schema is sent to the model on the next request. Because the registry
enforces permissions and emits events, the new tool automatically appears in the
activity panel and the audit trail.

Guardrails for new tools
------------------------

* Resolve every path through ``Workspace``; never build paths with ``os.path``.
* Bound the output. A tool that returns a 50 MB blob will be truncated, but it
  will still cost context.
* Return failures; raise only for genuinely exceptional conditions.
* Annotate the correct permission. Marking a write tool ``READ_WORKSPACE`` is
  the one mistake the system cannot catch for you -- review it.

Related
=======

* :doc:`permissions`, :doc:`workspace`
* :doc:`../user/tools`
* :doc:`../developer/extension-points`
