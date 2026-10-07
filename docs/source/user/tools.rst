.. _user-tools:


=====
Tools
=====


A tool is the only way the agent touches your files. Each one has a stable
name, a description, a JSON Schema the model reads, and a permission
requirement. v0.1 ships four read-only tools.

The v0.1 tool set
=================

``project_structure``
---------------------

A bounded tree of the project -- start here when you do not know the codebase.

.. code-block:: text

    input:  path (default "."), max_depth (1-6, default 3),
            max_entries (1-400, default 200)
    output: root, max_depth, entry_count, truncated, tree

.. code-block:: text

    .
    |-- docs/
    |   `-- architecture.md
    |-- widget_factory/
    |   |-- __init__.py
    |   `-- main.py
    |-- pyproject.toml
    `-- README.md

``list_directory``
------------------

The direct children of one folder, directories first, with sizes and modified
timestamps.

.. code-block:: text

    input:  path (default "."), max_entries (1-500, default 200)
    output: path, total, truncated, entries[]

``read_file``
-------------

Reads a UTF-8 text file and returns it with line numbers, so the model can cite
``path/to/file.py:42``.

.. code-block:: text

    input:  path, start_line (default 1), max_lines (1-2000, default 800)
    output: path, total_lines, start_line, returned_lines, truncated, content

Binary files are refused. Undecodable bytes are replaced rather than raising, so
a stray byte never aborts a turn.

``search_content``
------------------

Literal or regular-expression search across text files.

.. code-block:: text



    input:  pattern, path (default "."), file_pattern (glob, e.g. "*.py"),
            use_regex (default false), ignore_case (default true),
            max_results (1-200, default 50)
    output: pattern, root, files_scanned, match_count, truncated, matches[]

.. code-block:: json

    {
      "matches": [
        {"path": "src/app/settings.py", "line": 3, "text": "DEFAULT_PORT = 8080"}
      ]
    }

Every tool
==========

All four require ``READ_WORKSPACE`` and are read-only. There is no write, delete,
rename, command, git or network tool in v0.1 -- see
:doc:`../releases/v0.1`.

Limits
------

============  ==========================================
Limit         Value
============  ==========================================
read bytes    ``agent.max_file_bytes`` (default 256 000)
read lines    2000 per call
search hits   200 per call
walk entries  5000 per call
============  ==========================================

Limits exist so one call cannot flood the context window. Results carry
``truncated`` so the model knows it is looking at a slice.

What the model sees
===================

A successful call becomes an observation fed back on the next iteration:

.. code-block:: text

    TOOL RESULT (read_file): read README.md (3 lines)
    {
      "path": "README.md",
      "total_lines": 3,
      "content": "    1\t# Sample Project\n..."
    }

A failure becomes an error observation:

.. code-block:: text

    TOOL ERROR (read_file): 'nope.py' does not exist in this workspace.

The agent then decides whether to correct the path or to answer without it. A
failed tool never ends the turn by itself.

Unknown and malformed calls
===========================

If the model invents a tool or sends bad arguments, the registry converts that
into a failed observation the agent can recover from, rather than ending the
turn:

.. code-block:: text

    TOOL ERROR (delete_everything): The agent requested an unknown tool
    'delete_everything'.

Extending the tool set
======================

.. code-block:: python

    from quantsmind_agent.permissions import Permission
    from quantsmind_agent.tools import ToolResult, ToolSpec

    class FileStatTool:
        spec = ToolSpec(
            name="file_stat",
            description="Return size and modification time for one file.",
            input_schema={
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
            permission=Permission.READ_WORKSPACE,
            summary_template="stat {path}",
        )

        def __init__(self, workspace):
            self._workspace = workspace

        def run(self, arguments):
            path = self._workspace.resolve(arguments["path"])
            info = path.stat()
            return ToolResult.success(
                "file_stat", f"stat {path.name}",
                {"size": info.st_size},
            )

    registry.register(FileStatTool(workspace))

Register it before the agent runs; the schema is sent to the model on the next
request. Tools must never check permissions themselves -- the registry does
that, so a mis-annotated tool cannot bypass policy.

Related
=======

* :doc:`permissions`
* :doc:`../architecture/tool-system`
* :doc:`../developer/extension-points`
