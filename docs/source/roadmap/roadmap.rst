.. _roadmap:


=======
Roadmap
=======


Direction, not a commitment. Items move as the product and its users dictate.

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

Retrieval-based context
-----------------------

* Index files on open (incremental, watching the filesystem).
* Rank candidates against the question; inject the top matches with citations.
* Implemented behind ``ContextProvider``, so the Agent Core does not change.

Conversation persistence
------------------------

* Opt-in history, SQLite or JSON, with export and hard delete.
* Resume a conversation across restarts.
* Redaction of secrets found in stored transcripts.

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
