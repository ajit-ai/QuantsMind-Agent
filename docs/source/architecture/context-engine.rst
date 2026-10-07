.. _architecture-context-engine:


==============
Context Engine
==============


The Context Engine turns session state into a bounded prompt. Its job is to
include what the model needs, and *not* include a repository.

.. code-block:: python

    class ContextProvider(Protocol):
        def system_prompt(self) -> str: ...
        def workspace_summary(self) -> str: ...

Anatomy of a prompt
===================

.. code-block:: text

    [system]
      QuantsMind-Agent operating rules
        - work only inside the open project
        - use the tools, never claim to have read a file you did not read
        - paths are workspace-relative
        - correct a failed tool instead of repeating it
        - you cannot run commands, write files, use git or browse
      Operator instructions            (optional, from Settings)
      Current workspace               (root, project name, top-level entries)

    [history]
      the last N user/assistant exchanges, oldest first

    [user]
      the current question

    [assistant] / [tool]               (only for the turn in progress)

Tool results of the current turn are appended as ``tool`` messages, carrying the
tool-call id, so the provider can match observations to calls.

The system prompt
=================

The default prompt encodes the v0.1 contract explicitly, including the negative
capabilities. Stating what the agent *cannot* do produces better refusals than
letting the model discover it:

.. code-block:: text

    You cannot run commands, modify files, use git or access the network in this
    version. Say so plainly if asked to.

Workspace summary
=================

.. code-block:: text

    Root: C:\code\my-project
    Project folder: my-project
    Top-level entries (12 total):
      src/
      tests/
      docs/
      pyproject.toml
      README.md
      ...
    Use project_structure / read_file / search_content for details.

That is orientation, not content. The agent is expected to pull specifics with
tools, which keeps the prompt small and makes every claim traceable to a file the
user can see being read.

The summary is cached per workspace and invalidated when the workspace changes.

Budgeting
=========

Two limits keep prompts bounded:

``history_turns`` (default 12)
    How many recent user/assistant exchanges are kept. History is walked
    backwards, so the most recent context survives.

``max_context_chars`` (default 24 000)
    If the accumulated history exceeds the budget, it is dropped entirely
    rather than truncated mid-message.

Both are reported through ``ContextBundle.estimated_chars`` (characters divided
by four, a rough token estimate).

``max_context_files`` is reserved for the v0.2 retrieval index.

Assembly
========

.. code-block:: python

    bundle = engine.build(
        "Where is the port configured?",
        history=prior_turns,
        tools=registry.openai_schemas(),
    )
    bundle.system_prompt      # the full system text
    bundle.messages           # system + history + question
    bundle.tools              # tool schemas for this turn
    bundle.workspace_summary  # orientation block
    bundle.estimated_chars    # rough token estimate

Tool messages of the turn in progress are appended by the Agent Core, not by the
engine -- the engine owns the *baseline* context, the loop owns the *turn*.

Future retrieval
================

Because the engine is behind :class:`ContextProvider`, v0.2 can replace the
summary with an index: embed files, rank them against the question, and inject
only the top matches. No Agent Core change is required.

Related
=======

* :doc:`agent-core`, :doc:`../user/tools`
* :doc:`../developer/extension-points`
