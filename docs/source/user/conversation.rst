.. _user-conversation:


============
Conversation
============


The conversation is in memory for as long as the application stays open.
Nothing is persisted: closing the window discards it. That is deliberate -- v0.1
has no database, and it makes "no data leaves your machine except the model
request" easy to state and to verify.

Sending a request
=================

* Type in the composer and press **Send**, or ``Ctrl+Enter``.
* While a request runs the composer stays visible but **Send** is disabled and
  **Stop** appears.
* Pressing **Stop** requests cancellation.

What you see
============

The transcript distinguishes four things:

=========  ===============================================================
Element    Meaning
=========  ===============================================================
You        Your message, with a timestamp.
Assistant  The model's answer. Streamed text appears progressively.
Tool       One row per tool call: name, argument summary and a state mark.
Notice     Errors, cancellations and application messages.
=========  ===============================================================

The *Tool activity* panel mirrors tool calls as a compact audit log with state
markers (``â†’`` started, ``âœ“`` completed, ``âœ—`` failed). Permission decisions
appear there too.

Streaming
=========

The final answer is streamed when the provider supports SSE streaming. The
transcript appends text as it arrives, and the answer's metadata
(iterations, tool calls, tokens) is appended when it completes.

Two cases are handled explicitly:

* **Provider cannot stream** -- the agent retries the turn without streaming, so
  you get an answer rather than an error.
* **Stream produced nothing** -- the same non-streamed retry applies.

If the final answer differs from what was streamed (for example after a
fallback), the window renders the missing part rather than dropping it.

Cancellation
============

Cancellation is cooperative:

* it takes effect at the next agent iteration;
* during streaming it takes effect as soon as a chunk arrives;
* an in-flight HTTP request cannot be aborted;
* an answer that arrives after you pressed **Stop** is discarded, not shown.

Conversations beyond one turn
=============================

History is trimmed to ``agent.history_turns`` recent exchanges (default 12) and
dropped entirely when it exceeds the context budget, so long sessions stay
bounded. Tool traffic is *not* replayed as conversation history; the current
turn's tool results are sent with the turn that used them.

Starting a new conversation
===========================

**New conversation** (``Ctrl+N``) clears the transcript and the activity panel,
keeps the open project and your settings, and starts a fresh session id. To
switch projects as well, close the project first.

Asking for a change
===================

The agent can carry out a change end to end. It locates the code with the
navigation tools, makes the smallest edit that solves the problem, and runs the
check you named to confirm it. Every write, delete, undo and command asks you
first; a denial is final and the agent moves on rather than asking again.

Two behaviours are worth knowing, because both save you time:

* **Output is bounded from both ends.** A verbose build or test command is cut
  at the configured cap, but the *tail* is kept along with the head, because the
  failing assertion is at the end. When anything was dropped, the result says
  how much, so a cut is never mistaken for the whole output.
* **Repeats are refused.** If the agent runs the same command with the same
  arguments twice and nothing has changed in between, the second run is refused
  instead of repeating itself. Change something and it will run the same command
  again -- which is exactly what validating a fix looks like.

Questions that reward tool use
===============================

* ``Explain the structure of this project.``
* ``Where is the default port configured?``
* ``Which files would I need to change to rename the config module?``
* ``average() crashes on an empty list -- fix it and run the tests.``

What the agent still cannot do
==============================

* ``Search the web for ...``

There is no network access in any version. Git *mutations* -- commit, push,
checkout, reset, merge, rebase and the rest -- are blocked too, including
through the command tool; the read-only ``git_status``, ``git_diff`` and
``git_log`` are available.

Related
=======

* :doc:`tools`
* :doc:`permissions`
* :doc:`settings`

