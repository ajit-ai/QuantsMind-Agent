.. _acceptance-v0-2:

==========================================
Acceptance verification for v0.2
==========================================

.. contents::
   :local:

Why acceptance checks are split
===============================

An acceptance script answers one question: *may this release ship?* That
question cannot be answered by a single kind of evidence, because a check can
fail for three completely different reasons, and only one of them is a defect.

A run that conflates them produces reports that are wrong in both directions.
The failure mode this document exists to prevent is a check that reports success
because nothing happened: a stale-edit check that asserts a search string is
still absent, when that string was never present to begin with, passes whether
the tool worked, silently did nothing, or was never called at all.

The three categories
====================

.. list-table::
   :header-rows: 1
   :widths: 18 34 48

   * - Category
     - What it asserts
     - When a failure means
   * - Deterministic
     - A registered tool, driven directly, upheld an invariant
     - A product defect. Blocks the release.
   * - Real-model
     - Which tools the model chose for a given prompt
     - A model or tool-selection limitation. Never blocks.
   * - Environment
     - Credentials, provider reachability, external prerequisites
     - Nothing about the product. Reported as inconclusive.

Deterministic checks
--------------------

These call ``ToolRegistry.execute`` directly, through the same registry and the
same permission broker the agent loop uses. They never depend on a model
choosing to invoke anything, which is what makes them repeatable and cheap: the
whole set runs offline in a few seconds.

The deterministic set covers the security-relevant invariants:

.. list-table::
   :header-rows: 1
   :widths: 10 46 44

   * - Check
     - Invariant
     - Why it is asserted this way
   * - C
     - An ``edit_file`` whose ``old_text`` is absent fails, and the target file
       is byte-for-byte unchanged
     - Asserting the absent text is *still* absent proves nothing, so the
       original bytes are captured first and compared after
   * - E
     - An approved, harmless command really executes and returns output
     - Uses the running interpreter's ``--version``, so there is output to
       check rather than merely a success flag
   * - F
     - A policy-blocked command is refused, is never executed, and never
       reaches an approval prompt
     - The approver is set to always-allow on purpose; if policy is sound, the
       strongest possible approval still cannot release the command
   * - G
     - ``git_status`` reports the disposable repository's real branch state
     - The workspace is initialised as a local repository first, so a missing
       ``git`` is a prerequisite problem rather than a tool defect
   * - J
     - A traversal write is refused and the outside file is unchanged
     - The sentinel is created at the path the traversal actually resolves to,
       so the assertion inspects the file the attempt would have reached
   * - P
     - ``write_file``, ``edit_file`` and ``delete_file`` really do apply an
       ordinary permitted change
     - The positive control for C, I and J

Positive controls
-----------------

C and J both assert that an operation was *refused*, and I asserts that a
deleted file is gone. A build in which every mutating tool refused everything
would therefore pass all of them. Check P exists to close that hole: it drives
the same three tools with ordinary permitted requests and asserts the resulting
file contents, so a tool that silently no-ops fails.

A suite made only of refusal checks cannot distinguish a secure tool from a
broken one. Adding P is what makes the refusal results trustworthy.

Real-model observations
-----------------------

These ask the model to perform a task and record which tools it chose. They are
reported, and they never gate the release.

The distinction is not cosmetic. If a model answers ``python --version`` from
memory without calling ``run_command``, that tells you something about the
model's tool selection. It tells you nothing about whether ``run_command``
works, and treating it as a failed command-execution test would be a false
negative against the product.

Each observation is labelled ``used``, ``failed`` or ``not-called`` so the three
situations are never confused.

Environment prerequisites
-------------------------

Credential and provider checks are recorded as ``INCONCLUSIVE``, never as a
pass. An unreachable provider is not an application success and not an
application failure, so it can be neither.

The deterministic checks run *before* the provider is contacted, precisely so
that a dead credential cannot suppress them. A run with a broken key still
produces a complete deterministic verdict.

Exit codes
==========

.. list-table::
   :header-rows: 1
   :widths: 12 88

   * - Code
     - Meaning
   * - ``0``
     - All deterministic checks passed and every environment prerequisite held
   * - ``1``
     - At least one deterministic check failed. A product defect.
   * - ``2``
     - Deterministic checks passed, but an environment prerequisite was
       inconclusive, so the run is **not** a complete release verification

Code ``2`` is deliberately distinct from ``0``. Treating an unverified run as a
pass is the specific mistake this split exists to prevent.

Running it
==========

.. code-block:: console

   python scripts/acceptance_v0_2.py

The script resolves its credential through the normal Windows Credential
Manager path, exactly as the application does. The key is never read, printed or
logged. Configuration is copied into a temporary profile so the developer's real
profile is untouched, and the disposable workspace is removed afterwards.

Cost
====

Only the real-model observations consume provider credits, and they run only
after the provider answers a one-word preflight probe. When debugging a
deterministic check, iterate on the unit tests in
``tests/unit/test_acceptance_script.py`` instead; they exercise the same helper
functions without contacting a provider at all.