.. _testing-strategy:


=============
Test strategy
=============


Goals
=====

#. Prove the vertical slice works end to end, offline and repeatably.
#. Pin the boundaries that matter: path containment, permissions, credential
   handling, agent recovery.
#. Be honest about what automation cannot verify.

Layers
======

.. list-table::
   :header-rows: 1
   :widths: 16 30 54

   * - Layer
     - Question it answers
     - Example
   * - Unit
     - Does this component behave as specified?
     - A path with ``..`` is refused
   * - Integration
     - Do the subsystems compose correctly?
     - Agent -> gateway -> tool -> observation -> answer
   * - GUI
     - Does the window present and react correctly?
     - Tool activity appears before the answer
   * - Manual acceptance
     - Does the real product work on real Windows with a real model?
     - QM-V01-LLM-001
   * - CI
     - Is the repository still healthy on a clean machine?
     - lint, types, tests, docs, build

Unit
====

One module per subsystem, named after the class under test. The emphasis is on
*refusal* behaviour, because that is where a coding agent can do damage:

.. code-block:: text

    test_workspace.py         traversal, absolute paths, symlink escape, ignores
    test_permissions.py       ALLOW/DENY/ASK, approver, deny-by-default
    test_tools.py             schema validation, permission gate, error model
    test_secrets_paths.py     masking, per-user isolation, corrupt files
    test_errors_logging.py    credential redaction, user-safe messages

Integration
===========

The full chain is exercised with a scripted provider and a real workspace on
disk:

.. code-block:: text

    user request
      -> ContextEngine (bounded prompt, workspace summary)
      -> ModelGateway (events, error normalisation)
      -> ToolRegistry (permission gate)
      -> workspace tool (real filesystem read)
      -> observation
      -> ModelGateway
      -> streamed answer
      -> GUI transcript and activity panel

:mod:`tests.harness.AgentHarness` wires the production stack with only the
provider replaced, so these tests are not a parallel implementation.

The recovery paths matter most, because they are what a user hits first:

.. code-block:: text

    a tool fails            -> the agent sees TOOL ERROR and continues
    a path escapes          -> refused, agent explains
    an unknown tool is used  -> failed observation, turn continues
    a provider 500s          -> actionable error, no traceback in the UI
    the user presses Stop    -> turn cancelled, late answer discarded

GUI
===

Widgets run under Qt's ``offscreen`` platform, so the suite needs no display
server. ``tests/gui/harness.py`` drives the real ``MainWindow`` over the real
``ApplicationContext`` and a scripted provider.

Three GUI tests exist because of bugs found while building v0.1:

* every tool event reaches the window (cross-thread signal-to-signal drops);
* tool events are rendered *before* the answer (completion ordering);
* text deltas are all delivered (streaming completeness).

Manual acceptance
=================

The only layer that talks to a real model, on a real desktop, with real
credentials. See :doc:`manual-acceptance`. Its results are recorded in
:doc:`acceptance-v0.1`, using ``PASS`` / ``FAIL`` / ``NOT RUN`` / ``BLOCKED`` --
never inferred from the automated suite.

Rules
=====

#. No network in tests. ``mock_transport`` and ``FakeModelProvider`` are the only
   ways to produce a model response.
#. No ambient credentials. The fixture removes them for every test.
#. No real user profile. Paths point at ``tmp_path``.
#. Assert user-visible outcomes. Returned data, rendered text, event sequences --
   not private attributes.
#. A bug fix ships with its regression test.
#. Never report ``NOT RUN`` as ``PASS``.

Coverage
========

.. code-block:: powershell

    pytest --cov=quantsmind_agent --cov-report=term-missing --cov-report=html

Coverage is a navigation aid, not a target. ``permissions.py`` is the clearest
example of the right shape: a short module where every branch is a security
decision, so it is nearly fully covered by construction.

Related
=======

* :doc:`../developer/testing`
* :doc:`manual-acceptance`
* :doc:`acceptance-v0.1`
