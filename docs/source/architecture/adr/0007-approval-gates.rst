.. _adr-0007-approval-gates:

===================================================
ADR 0007: Separate the command policy from approval
===================================================

:Status: Accepted
:Date: 2026
:Supersedes: none

Context
=======

v0.2 adds ``run_command``. Two things must be true before a command runs: the
application must consider it permissible at all, and the user must agree to it
this time.

The obvious implementation routes both through the permission layer: a command
matches no rule, the dialog opens, and whatever the user clicks decides. That
makes the safety policy a *default*, and a dialog whose Allow button bypasses
the policy is not a control -- it is a confirmation.

An early implementation made exactly that mistake and it was caught in testing:
``format C:`` was classified ``BLOCKED`` by the policy but still prompted,
because the registry asked permission before the tool body could run and refuse.

Decision
========

Split the two gates and order them.

#. **The policy runs first**, inside the registry, via a ``guard`` hook on
   :class:`~quantsmind_agent.tools.ToolSpec`. A blocked command returns a
   failure and never reaches a dialog.
#. **Approval runs second**, through the existing permission layer.

The guard is deliberately powerless to widen anything: it may only raise, never
allow. The registry therefore remains the single enforcement point, and no tool
can bypass the permission layer by supplying extra arguments.

The permission taxonomy also grew an explicit outcome -- ``ALLOW_ONCE``,
``ALLOW_FOR_SESSION``, ``DENY`` -- because a boolean cannot express *allow for
the rest of this conversation*, and collapsing that into a boolean would either
lose the feature or leak the grant into future sessions.

Consequences
============

* A blocked command is genuinely unrunnable, which is what makes the dialog
  worth reading.
* Ordering is now load-bearing. The permission provider sorts candidates by
  "exact tool first, then non-wildcard target pattern" and that sort is stable,
  so a permissive wildcard appended after a restrictive one is *unreachable*.
  The application builds its rule list in an explicit order and documents the
  invariant. This also fixed a latent v0.1 bug where
  ``auto_approve_reads = false`` never took effect for exactly this reason.
* ``delete_file`` and ``run_command`` carry per-tool ``ASK`` rules so that
  ``auto_approve_writes`` cannot widen them.
* Session grants are cleared when the conversation, project or settings change.
  A new conversation is a new trust boundary.

Threading consequence
=====================

The prompt is produced on the agent's worker thread and must be *shown* on the
GUI thread. Two distinct handovers are involved, and the first version of v0.2
got the second one wrong.

**The prompt signal.** It is emitted on the worker thread and connected with the
default ``AutoConnection``. Qt resolves that correctly, so the dialog *is*
built on the GUI thread. It is now additionally decorated with ``@Slot(object)``
and connected with an explicit ``QueuedConnection``, which is belt and braces:
either alone would work, and a test pins each so neither can be dropped without
failing.

**Agent events, which were genuinely broken.** ``EventBus`` is a plain Python
callback list, not a Qt signal. A subscriber therefore runs on whichever thread
published the event -- and the window's subscriber renders activity rows and
status-bar text. During an approval the worker publishes ``permission_decision``
and ``permission_approval``, so the agent's worker thread was mutating
``QListWidget`` and ``QLabel`` objects directly. Qt does not support that, and
the observable result on Windows 11 was an approval window that opened blank and
reported "Not Responding". The window now only *enqueues* bus events and drains
them on a GUI-thread timer.

Every failure path fails closed. An off-thread request, a dialog that cannot be
constructed, and a second request while one is pending all deny and release the
worker, because an unanswered prompt would otherwise block the agent
indefinitely.

The lesson is worth stating separately: a green test suite did not catch this,
because the same code paths run correctly headless. Thread-affinity bugs need
either a test that asserts the thread the handler ran on, or real on-desktop
verification.
