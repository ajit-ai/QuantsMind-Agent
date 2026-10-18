.. _persistence-and-recovery:


=======================
Persistence and recovery
========================

The application keeps two kinds of durable state: conversations, and tasks. Both
are JSON on disk under the user's application-data directory, never inside the
installed program, and neither is ever packaged.

What is stored
==============

**Conversations** (``conversations.py``, ``SCHEMA_VERSION = 3``) store the message
history, the model that answered, and turn summaries. A record written by a newer
build is skipped with a warning rather than guessed at -- a misread record is
worse than an absent one.

**Tasks** (``task_store.py`` on top of ``tasks.py``, ``SCHEMA_VERSION = 1``) store
the objective, the plan and its digest, the workspace, the budget, the approval
record, per-step statuses, every execution record, file changes with before/after
hashes, check results, repair attempts, recovery decisions and the counters that
bound the run.

The approval is stored *with* the plan, not separately, because the two are only
meaningful together: an approval names a plan digest. Persisting them apart would
make it possible to record that a task was authorised without recording what it
was authorised to do.

Writes
======

``TaskStore.save`` writes to a temporary file in the same directory and then
``os.replace``s it into position, so a crash mid-write leaves the previous record
intact rather than a truncated one. Credential-shaped strings are scrubbed on the
way *in*, so a secret quoted by a model in a task's text is never written to disk
at all.

Corrupt or unreadable records are quarantined rather than deleted, and
``verify_integrity`` lists them. A record that cannot be parsed is evidence; it
may be the only account of what an interrupted task did.

Schema versioning
=================

Each record declares its ``schema_version``. On read, an older record is migrated
forward through ``migrate``; a newer one is refused with ``PlanRejected`` rather
than reinterpreted. Plan digests carry their own ``DIGEST_VERSION`` inside the
hashed payload, so changing which fields are canonical invalidates every approval
already granted -- which is the correct outcome, and is why the version is hashed
in rather than assumed.

Recovery
========

.. mermaid::

    flowchart TD
        A[Application starts] --> B{Load stored tasks}
        B --> C{Task status in<br/>running / verifying / repairing?}
        C -- no --> D[History list, nothing to report]
        C -- yes --> E[analyse_task compares the record<br/>against the world]
        E --> F{Workspace present<br/>and the same?}
        F -- no --> G[Decision recorded:<br/>workspace missing or changed]
        F -- yes --> H{Files match<br/>their recorded hashes?}
        H -- no --> I[Decision recorded:<br/>a file was edited since]
        H -- yes --> J{Approval still<br/>matches the plan?}
        J -- no --> K[Marked as needing<br/>re-approval]
        J -- yes --> L[Parked as paused,<br/>outcome unknown]
        G --> M[Nothing resumes automatically]
        I --> M
        K --> M
        L --> M
        M --> N[User selects it and<br/>decides what to do]

The rules, stated plainly:

* An interrupted task is **not** marked failed. Failure is a claim about an
  outcome; an interruption means the outcome is unknown.
* An interrupted task is **not** resumed automatically, and never on startup.
* Completed work and known side effects are preserved: the step statuses, the
  execution records and the file-change hashes all survive, which is what makes
  "what had this already changed?" answerable.
* Re-approval is bound to the plan as stored at that moment. If the plan changed
  while the application was closed, the renewed approval cannot carry authority
  over a plan the user never saw.

What this does not do
=====================

There is no automatic replay of external side effects, no cross-device task
migration, and no database. The storage is JSON files in one directory because the
volume is one user's task history on one machine, and a database would add a
migration surface without removing a real constraint.
