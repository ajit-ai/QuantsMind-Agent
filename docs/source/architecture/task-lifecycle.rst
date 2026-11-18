.. _task-lifecycle:


==============
Task lifecycle
==============


A task is a proposal, an authorisation, a sequence of operations, and a record of
what actually happened. The lifecycle is the contract between those four things:
it decides what may happen next, and every move through it is checked against a
written table rather than implied by whichever method ran.

The table is the authority
==========================

``ALLOWED_TRANSITIONS`` in :mod:`quantsmind_agent.tasks` maps each status to the
statuses it may legally become. :meth:`~quantsmind_agent.tasks.Task.move_to` is
the only way product code changes a task's status, and an illegal move raises
``IllegalTransition`` rather than being logged and ignored -- an illegal move
means some code path believes it may do something the lifecycle forbids, and
swallowing that would hide the defect the table exists to expose.

.. mermaid::

    stateDiagram-v2
        [*] --> draft
        draft --> awaiting_approval: a validated plan is offered
        awaiting_approval --> approved: the user authorises it
        awaiting_approval --> cancelled: the user rejects it
        approved --> running: a step is authorised and starts
        approved --> paused
        approved --> verifying
        approved --> repairing
        running --> verifying
        running --> repairing
        running --> paused
        running --> completed: every step succeeded
        running --> failed
        running --> blocked: policy refused a step
        paused --> approved: resumed, approval still valid
        paused --> awaiting_approval: resumed, the plan moved
        verifying --> completed
        verifying --> failed
        verifying --> repairing
        repairing --> verifying
        repairing --> failed
        failed --> repairing: a repair is admitted
        failed --> blocked: the repair budget ran out
        blocked --> awaiting_approval: the plan was re-proposed
        completed --> verifying: acceptance criteria checked afterwards
        draft --> failed: the record could not be saved
        awaiting_approval --> failed: the record could not be saved
        approved --> cancelled
        running --> cancelled
        paused --> cancelled
        blocked --> cancelled
        cancelled --> [*]
        completed --> [*]
        failed --> [*]

``CANCELLED`` is reachable from everywhere non-terminal because the user's right
to stop work does not depend on how far it had got. ``CANCELLED`` itself is
terminal: nothing moves out of it. ``COMPLETED`` has exactly one exit,
``VERIFYING``, because evidence about whether a finished task met its acceptance
criteria can arrive after the work did.

Invariants
==========

These are enforced below the presentation layer and covered by
``tests/security/test_authorization_invariants.py``:

* A proposed task cannot execute. ``Orchestrator.advance`` performs no step while
  the task is unapproved, and :meth:`~quantsmind_agent.tasks.Task.approved_plan`
  returns nothing unless an *approving* decision matches the current plan digest.
* A rejected task cannot execute, and does not even report an approved plan.
* Approval refers to one plan digest, one workspace and one scope. A plan edit
  changes the digest, so the previous approval stops applying without anybody
  having to remember to revoke it.
* Only the transitions above are possible. Every product path goes through
  ``move_to``.
* Cancellation is never reported as completion. ``cancel()`` returns early on a
  terminal task, so a finished task cannot be relabelled as one the user stopped.
* A failed step is not reported as successful, and is not silently retried:
  eligibility skips ``FAILED`` and ``BLOCKED`` steps, so retrying is the repair
  controller's decision, made with the failure evidence in hand.
* A task cannot become ``COMPLETED`` because a worker thread exited. Completion
  is decided by ``_settle`` from step and check records.
* Application shutdown does not abandon work silently: closing the window
  releases the worker, cancels the task and joins the thread, and the cancelled
  record is persisted.

Pause and cancellation
======================

``pause()`` takes effect between actions. The operation already running is not
interrupted mid-flight -- nothing can promise to stop an external command safely,
so a pause is a promise about *what happens next*, not about what is happening
now. ``resume()`` re-checks the approval first: if the plan moved while paused,
the task returns to ``AWAITING_APPROVAL`` rather than running on a stale approval.

``cancel()`` sets an irreversible flag, moves the task to ``CANCELLED`` and stops
the worker at its next boundary. Operations already in progress may still complete
or partially apply; the record keeps every execution entry that was written, so
the history shows what was actually attempted rather than what was intended.

Interruption and recovery
=========================

A task recorded as ``RUNNING``, ``VERIFYING`` or ``REPAIRING`` when the application
closed was interrupted, and its outcome is **unknown**: the last thing written may
have been a tool call that had already changed a file. Such tasks are reported, not
marked failed and not resumed.

``TaskService.interrupted_tasks`` finds them at startup and
:meth:`~quantsmind_agent.recovery.analyse_task` records why each needs attention:
a missing workspace, a different workspace now open, a file changed since the
record was written, or an approval that no longer matches. Adoption brings a task
under management without running it; re-approval walks it
``RUNNING -> PAUSED -> APPROVED`` and binds the renewed record to the plan *as
stored now*, so it cannot carry authority for a plan that changed while the
application was closed.

Persistence is a precondition for running
==========================================

``Orchestrator.persist`` stops the task if its record cannot be written. This is
deliberate: the approval and the evidence of what was done both live in that
record, so continuing would mean running work whose permission and effects could
not be recovered afterwards. A task that cannot record itself does not sit at
"awaiting approval" pretending a future is available.
