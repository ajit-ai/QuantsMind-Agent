.. _roadmap:


=======
Roadmap
=======


Direction, not a commitment. Items move as the product and its users dictate.

.. note::

   This roadmap is historical. The v0.2 through v0.9 capabilities described
   below have shipped; see :doc:`../architecture/v0.9-behaviour` and
   ``CHANGELOG.md`` for what actually exists. Items still marked as future here
   are not a description of v0.9.0.

Principles
==========

#. **Vertical slices, not layers.** A capability ships when a user can use it
   end to end, not when a module is finished.
#. **Boundaries before features.** Containment, permissions and redaction are
   designed in from the start, because retrofitting them is how agents become
   unsafe.
#. **Offline-testable core.** The headless core must stay testable without
   credentials or a network, or the suite decays into manual testing.
#. **Say what is not done.** A documented limitation is more useful than a
   silent failure.

.. _roadmap-v0.2:

v0.2 -- make it act
===================

The release that turns a reader into a doer.

Write support
-------------

* ``write_file`` (create/overwrite) and ``edit_file`` (targeted replacement)
  behind ``WRITE_WORKSPACE``.
* Paths resolved through ``Workspace`` -- containment comes for free.
* Structured diffs in ``ToolResult.data`` so the interface can show exactly what
  changes.
* Mandatory interactive approval before any write.
* Reversible changes: a session-scoped undo list.

Terminal access
---------------

* ``run_command`` behind ``EXECUTE_COMMAND``.
* Approval dialog naming the exact command and working directory.
* Working directory constrained to the workspace.
* Output captured, truncated and shown in the activity panel.
* Timeout and output-size caps.

Git awareness
-------------

* ``git_status``, ``git_diff`` (read-only) first.
* ``git_commit`` only with explicit per-invocation approval.
* Output normalised through the same permission layer.

Why this order
--------------

Writes and commands are what a coding agent is *for*, and both are only safe
with an approval UI that does not exist yet. Building approval first is the
prerequisite, not the follow-up.

.. _roadmap-v0.3:

v0.3 -- handle real codebases
=============================

Code navigation
---------------

* ``code_outline``: every class, function, method and property in a Python file
  with its exact line range, signature, decorators and docstring summary. The
  line range feeds ``edit_file``'s ``replace_lines``, so a change to one
  function no longer requires the whole module in the context window.
* ``find_references``: definitions, import sites and call sites across the
  project, with imports resolved to real files -- so an interface change shows
  exactly which dependent files have to change with it, aliases included.
  One level of import expansion from those dependents, bounded.
* ``project_symbols``: the whole project's definitions in one call, filtered by
  name or kind, so locating code does not cost one outline per file.
* Both are read-only, deterministic, offline -- no embedding service, no
  background process -- so they work with every provider including a local one.
  Non-Python files and unparsable files are refused by name rather than guessed
  at.
* Implemented; see ``docs/source/user/tools.rst``.

Project intelligence
--------------------

* ``project_context``: what kind of project this is, where the source, tests and
  docs live, which files and symbols match the task, which tests cover a named
  file, and which files import it. Bounded on every axis, with an explicit
  truncation flag, because a model told "12 relevant files" when there were 300
  will call 12 thorough.
* One bounded discovery pass recognises the usual Python signals and reports its
  confidence and what it could not determine, rather than assuming one layout.
* A project-scoped symbol and import map replaces the repeated full walk, reusing
  the existing parsers. It is invalidated per file by the agent's own writes,
  created files, deletes and undos, and rebuilt wholesale when the project
  changes. Never persisted: it is a claim about files, and files change while the
  application is closed.
* Deliberately still out of scope: embeddings, an index service, and a background
  daemon. The value here is deterministic and free, and a cache that can be wrong
  is worse than a walk that is slow.
* Implemented; see ``docs/source/user/tools.rst``.

Retrieval-based context
-----------------------

* Index files on open (incremental, watching the filesystem).
* Rank candidates against the question; inject the top matches with citations.
* Partly superseded by ``project_context`` and the project map above, which cover
  the structure-aware case deterministically. What remains genuinely open is
  *ranking by meaning* rather than by name, which is the embedding question.
* Implemented behind ``ContextProvider``, so the Agent Core does not change.

Conversation persistence
------------------------

* Opt-in history, JSON, one file per conversation, with prune and delete.
* Resume a conversation across restarts, restoring the transcript and reopening
  its project when that folder is still there.
* Redaction of secrets found in stored transcripts.
* Approval grants and undo history are deliberately **not** persisted: both are
  scoped to the session that produced them, and restoring either would extend a
  user's trust past the moment they gave it.

Implemented; see ``docs/source/user/conversation.rst``.

Diff viewer
-----------

* Side-by-side diff of proposed changes with per-hunk accept/reject.
* Apply only the accepted hunks.

Larger projects
---------------

* Multi-root workspaces (a library plus its consumers).
* Symbol-level navigation rather than file-level reads.

.. _roadmap-v0.4:

v0.4 -- extend and integrate
============================

More providers
--------------

* Native Anthropic Messages support.
* Azure OpenAI (deployment URL plus ``api-version``).
* Local inference helpers: model discovery, prompt templates, pull guidance.

MCP
---

* MCP client mapping remote tools onto the existing registry, inheriting
  permissions and events.
* Server lifecycle management and per-server approval policy.

VS Code extension
-----------------

* The core is already a library; only a view layer is needed.
* Workspace selection from the editor, diff review in the editor, selected text
  as context.

Deeper observability
--------------------

* Persisted event log per session, exportable as JSON.
* Token and latency accounting per turn.
* Optional opt-in diagnostics, with credentials excluded by construction.

Under consideration
--------------------

* Multi-agent delegation for large refactors.
* Patch series and cherry-picking onto a branch.
* Repository-aware indexing with symbol graphs.
* Plugin API for third-party tools.

Deliberately out of scope
-------------------------

* Cloud SaaS, multi-tenancy, billing.
* Autonomous deployment.
* Browser automation.
* Telemetry that leaves the machine by default.

These would trade the local-first, privacy-respecting posture for capabilities
that are better solved by other tools.

Related
=======

* :doc:`../releases/v0.1` -- what v0.1 does and does not do
* :doc:`../architecture/adr/index` -- decisions that shape the roadmap
* :doc:`../developer/extension-points` -- where to build each of these
