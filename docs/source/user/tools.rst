.. _user-tools:


=====
Tools
=====


A tool is the only way the agent touches your files. Each one has a stable
name, a description, a JSON Schema the model reads, and a permission
requirement. v0.9.0 registers sixteen tools: eight read-only readers, four file
mutations, one command runner and three read-only Git tools.

Reading the workspace
=====================

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

The code-navigation tools
=========================

``code_outline``
----------------

Everything in one Python file: classes, functions, methods and properties with
the exact lines they occupy, their signature, their decorators and the first
line of the docstring. Use it before editing -- the line range is what
``edit_file``'s ``replace_lines`` addresses, so a change to one function no
longer needs the whole module in the context window.

.. code-block:: text

    input:  path
    output: path, lines, symbol_count, returned, truncated, outline, symbols[]

.. code-block:: text

    class Store  [12-20]  # A store.
        method def __init__(self, root)  [15-16]
        method def save(self, payload)  [18-20]  # Write a payload.
    function def add(left, right) -> int  [4-6]  # Add two numbers.

Each entry in ``symbols`` carries ``name``, ``qualified`` (``Store.save``),
``kind`` (``class`` / ``function`` / ``method`` / ``property`` /
``async function`` / ``async method``), ``line``, ``end_line``, ``signature``,
``decorators``, ``doc`` and ``parent``.

``find_references``
-------------------

Where a symbol is defined, where it is imported, and where it is used, across
the project. Imports are resolved to real files, so this answers the question
that matters before changing an interface: *which files have to change with it?*

.. code-block:: text

    input:  symbol, path (default "."), max_results (1-200, default 50),
            expand (default true)
    output: symbol, root, files_scanned, files_skipped, unparsable_files,
            unresolved_imports, definition_count, reference_count,
            dependent_files[], related[], truncated, expanded, ambiguous,
            resolution, definitions[], references[]

.. code-block:: json

    {
      "definitions": [
        {"path": "geometry.py", "qualified": "distance", "kind": "function",
         "line": 4, "end_line": 6}
      ],
      "dependent_files": ["plotter.py", "test_geometry.py"],
      "references": [
        {"path": "plotter.py", "line": 6, "kind": "import"},
        {"path": "plotter.py", "line": 10, "kind": "use"}
      ],
      "related": [
        {"importer": "plotter.py", "source_module": "geometry.py",
         "imported_name": "distance", "bound_name": "distance", "line": 6}
      ],
      "ambiguous": false,
      "resolution": "name and import matching, one import level, no type inference"
    }

What it resolves
~~~~~~~~~~~~~~~~

* ``from pkg.store import Store`` -> ``pkg/store.py``, and ``pkg/store/__init__.py``
  if the module is a package.
* Relative imports, anchored on the importing file's own package: ``from .x``
  inside ``pkg/app.py`` means ``pkg.x``; ``from ..x`` inside ``pkg/sub/app.py``
  means ``pkg.x``.
* Aliases: ``from pkg.store import Store as Repository`` makes
  ``pkg/reader.py`` a dependent file, which a plain name search would miss.

What it does not do
~~~~~~~~~~~~~~~~~~~

It does not do type resolution, and it says so in the ``resolution`` field rather
than letting the model infer more certainty than exists. Concretely:

* ``ambiguous`` is ``true`` when more than one file defines the name. The
  definitions are all returned; none is picked for you.
* An import the workspace cannot map to a file -- a third-party package, a
  relative import above the root -- is counted in ``unresolved_imports`` and not
  guessed at.
* ``related`` follows exactly one import level, and only within the dependent
  files. It does not walk transitively.
* ``from x import *`` binds no name it can report, so it yields nothing.
* Matching is by name, so ``json.load`` also matches a search for ``load``.
  Over-reporting is deliberate: a missed call site costs a broken refactor, while
  a spurious one costs a single look.

``project_symbols``
-------------------

Every definition in the project, in one call -- so locating code you have not
read yet does not cost one outline per file.

.. code-block:: text

    input:  path (default "."), query (name fragment),
            kind (class | function | method | property |
                  async function | async method),
            max_results (1-500, default 200)
    output: root, files_scanned, files_skipped, unparsable_files,
            symbol_count, returned, truncated, symbols[]

.. code-block:: json

    {
      "symbols": [
        {"path": "settings.py", "qualified": "load_settings",
         "kind": "function", "line": 5, "end_line": 7, "doc": "Read settings."}
      ]
    }

``query`` matches the *qualified* name case-insensitively, so ``settings``
finds ``load_settings``; ``symbol_count`` counts everything that matched even
when ``truncated`` cut the list short.

It answers from a shared, session-scoped index rather than re-walking the project
on every call -- the first call pays for the walk, the rest are free. A file the
agent writes, creates, deletes or undoes is re-read before anything is reported
about it, so the index is never the stale one. See :ref:`project-intelligence`.
The output says which path answered (``source: project index``) and what is
waiting to be re-read (``pending_refresh``).

``project_context``
-------------------

The project-level question: *what is this project, and what do I need to look at
for this task?* Ask it before outlining files one at a time.

.. code-block:: text

    input:  task (what you are about to do),
            path (a file you are about to change),
            symbol (a symbol to centre on),
            max_files (1-100, default 20),
            max_symbols (1-400, default 40)
    output: project (the layout and classification), files[], symbols[],
            tests[], callers[], notes[], scanned, truncated, relevance, index

It answers the questions that otherwise cost a walk each: what kind of project
this is, where the source and tests live, which files and symbols match the task,
which tests cover the file you named, and which files import it.

.. code-block:: json

    {
      "project": {"kind": "python-package", "confidence": 0.88,
                  "layout": "src", "source_roots": ["src"],
                  "test_roots": ["tests"], "packages": ["src/acme"]},
      "files":   [{"path": "src/acme/cart.py", "module": "src.acme.cart",
                   "symbols": 3, "imports": 1}],
      "tests":   ["tests/test_cart.py"],
      "callers": ["src/acme/cli/app.py"],
      "truncated": false,
      "relevance": "lexical: task words matched against file and symbol names, plus import edges"
    }

Two things worth knowing before you rely on it. **Matching is lexical**, not
semantic -- it compares the words of your task against file and symbol names, and
says so in ``relevance`` rather than pretending otherwise. And it is **bounded**:
``max_files`` and ``max_symbols`` are caps, and when one bites, ``truncated`` is
true and ``notes`` explains. A model told "12 related files" when there were 300
will work with 12 and call it thorough, so the flag is the honest part.

Tool calls overlap: name a ``path`` and you get its tests and importers; name
nothing meaningful and it will say so rather than dumping the project.

.. _project-intelligence:

How the project is understood
=============================

Behind ``project_context`` and ``project_symbols`` is a project-scoped layer that
answers project-level questions once per project and reuses the answer.

**Discovery** reads the layout in one bounded pass and classifies the project:
``pyproject.toml``/``setup.py``/``setup.cfg``, ``requirements*.txt``, ``src/``,
package directories, test directories, ``__main__.py``, console scripts declared
in ``pyproject.toml`` or ``setup.cfg``, documentation roots, build and CI files.
It does not assume one layout, and it does not guess: a project it cannot place is
reported as ``unknown`` with the reason in ``unknowns``, and the confidence comes
from how many independent signals agree.

**The map** holds the symbols and import edges per file, reusing the same
parsers the navigation tools use -- there is no second Python parser in this
product. Absolute imports resolve through the discovered package layout, so a
``src/`` project links ``import acme.cart`` to the file that exists.

**Invalidation is per file, and it is driven by the agent's own writes.** Editing
a module invalidates that module. Deleting one drops it *and every edge pointing
at it*, because reporting a caller that no longer exists is worse than reporting
none. Opening a different project creates a new map, so nothing can leak between
projects.

Nothing about this is written into a conversation. A project map is a claim about
files on disk, and files change while the application is closed, so caching it
across restarts would mean caching a claim that is probably false. The system
prompt is oriented from the profile on every turn instead, which costs one bounded
walk and cannot go stale within a session.

Affected files, reported by the edit
------------------------------------

After ``write_file`` or ``edit_file`` succeeds, the result carries the files that
import the one just changed, so the agent does not have to remember to ask:

.. code-block:: json

    {
      "path": "geometry.py",
      "undoable": true,
      "diff": "+def distance(a, b):...",
      "dependents": [
        {"path": "plotter.py", "line": 1,
         "imported_name": "distance", "bound_name": "distance"}
      ],
      "dependents_truncated": false
    }

This is what keeps a cross-file change from being half-finished: the caller list
arrives in the same observation as the edit, so the next step can cover them.
Creating a file skips the scan, because nothing could have been importing it.
The scan is bounded and ``dependents_truncated`` says when the answer may be
incomplete.

What the navigation tools refuse to do
---------------------------------------

They read Python, and they say so instead of guessing:

* a file that is not ``.py`` is refused with a pointer to ``read_file``;
* a file that does not parse reports the syntax error and its line, because a
  half-parsed outline is worse than none;
* files that could not be read are counted in ``files_skipped``, and files that
  could not be parsed are listed in ``unparsable_files``, so a partial answer is
  never mistaken for a complete one.

All four require ``READ_WORKSPACE``, are read-only, and record nothing in the
undo list.

The registered tool set
=======================

.. list-table::
   :header-rows: 1
   :widths: 28 20 52

   * - Tool
     - Permission
     - Effect
   * - ``project_structure``, ``list_directory``, ``read_file``,
       ``search_content``
     - ``READ_WORKSPACE``
     - Read files inside the workspace.
   * - ``code_outline``, ``find_references``, ``project_symbols``,
       ``project_context``
     - ``READ_WORKSPACE``
     - Read Python structure and project relationships.
   * - ``write_file``, ``edit_file``
     - ``WRITE_WORKSPACE``
     - Create or replace a file; apply a deterministic, staleness-checked edit.
       Approval is required unless ``auto_approve_writes`` is enabled.
   * - ``delete_file``
     - ``WRITE_WORKSPACE``
     - Delete one file. Always asks; irreversible; no directory-tree delete.
   * - ``undo_file``
     - ``WRITE_WORKSPACE``
     - Restore the newest reversible change to one file. Always asks; the undo
       journal is session-scoped.
   * - ``run_command``
     - ``EXECUTE_COMMAND``
     - Run one approved command in the workspace. Always asks and is filtered by
       the command policy. It is not an OS sandbox.
   * - ``git_status``, ``git_diff``, ``git_log``
     - ``GIT``
     - Read repository state. There is no Git mutation tool.

File changes
============

``write_file`` creates or replaces a text file, writes through a temporary
sibling, and reports a bounded diff plus whether the file was created or
replaced. ``edit_file`` applies one deterministic operation against an explicit
precondition, so a stale request fails instead of overwriting newer content.

Both record a pre-image for the session undo journal and report the files that
import the changed file. Neither can widen the approval they were given:
``delete_file`` and ``undo_file`` ask every time, even when ordinary writes are
auto-approved.

``delete_file`` removes one file, never a directory tree. It is the one
irreversible file operation and is deliberately not added to the undo journal.
``undo_file`` restores the newest reversible write or edit from the current
session; it does not restore deletes, and it starts empty after a restart.

Command execution
=================

``run_command`` starts one program with an argument vector, not a shell, after
the user approves it. The working directory must be inside the workspace; the
runner enforces a timeout, caps stdout and stderr, and removes the configured
provider key from the child environment.

The command policy is a separate, non-overridable gate. A blocked command never
reaches the approval dialog. The policy blocks destructive host operations,
common network-transfer and loader tools, and Git mutations. It is a deny list
evaluated by this process, **not an OS sandbox**: an approved program runs with
your privileges and may spawn children or use the network. See
:doc:`../architecture/command-execution`.

Git
===

``git_status``, ``git_diff`` and ``git_log`` are read-only and use ``GIT``.
``git_enabled = false`` removes them from the registry rather than denying them,
so the model cannot even name them. Git mutations are blocked in the command
policy as well.

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
