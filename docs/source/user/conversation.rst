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
markers (``→`` started, ``✓`` completed, ``✗`` failed). Permission decisions
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

Good questions to try
=====================

Questions that reward tool use:

* ``Explain the structure of this project.``
* ``Read the README and summarize the project.``
* ``Where is the default port configured?``
* ``Find every place that raises an exception, and explain why.``
* ``Which files would I need to change to rename the config module?``

Questions v0.1 cannot answer
----------------------------

* ``Run the tests.``
* ``Fix the bug.``
* ``Commit this.``
* ``Search the web for ...``

The agent will say it cannot run commands, modify files, use git or access the
network in this version. That is the v0.1 boundary, not a bug.
