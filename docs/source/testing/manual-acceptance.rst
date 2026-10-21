.. _testing-manual-acceptance:
.. _manual-llm:


=================
Manual acceptance
=================


Everything in the automated suite is offline. These procedures use a real model,
a real API key and a real Windows 11 desktop. The **release gate for v0.9.0** is
the executable beta plan in :doc:`../qualification/manual-beta-acceptance`; this
page is a short local smoke test and a map to that plan.

.. important::

   Record the outcome honestly. ``NOT RUN``, ``NOT TESTED`` and ``BLOCKED`` are
   legitimate outcomes. Replacing any of them with ``PASS`` without evidence
   makes the whole report worthless.

   A pass in the local smoke test does not replace the beta evidence. The
   published v0.9.0 MSI has a known junction-discovery defect (QF-006) and no
   human approval-dialog or real-provider result.

Preparing the fixture
=====================

.. code-block:: powershell

    python scripts\make_acceptance_fixture.py --force

This creates a disposable project at
``.acceptance-fixture\widget-factory`` containing a small Python package
(``config.py``, ``factory.py``, ``main.py``), a README and a design note. It is
generated, not committed, and safe to delete.

Local v0.9.0 smoke test
=======================

Use a disposable project. Do not use production data, a customer repository or a
credential you cannot revoke.

.. list-table::
   :header-rows: 1
   :widths: 8 44 24 24

   * - Step
     - Check
     - Expected
     - Beta reference
   * - 1
     - Start the application and open the fixture
     - Window renders; project path is shown
     - BETA-01, BETA-02
   * - 2
     - Refresh the Project panel
     - Kind, roots and declared checks appear, or the panel says it has none
     - BETA-09
   * - 3
     - Configure a real provider and ask a read-only question
     - Streamed answer cites files actually read; tool activity appears first
     - BETA-03
   * - 4
     - Ask for a small edit and approve it
     - Dialog names the tool, file and change; only that change is applied
     - BETA-04
   * - 5
     - Repeat the edit and deny it
     - File bytes are unchanged
     - BETA-05
   * - 6
     - Ask for a stale edit
     - Conflict is reported; file is unchanged
     - BETA-05
   * - 7
     - Approve ``cmd /c echo beta-command-ok``
     - Exact command and working directory are shown; output appears
     - BETA-06
   * - 8
     - Ask to run ``curl --version``
     - Refused by policy; no approval dialog
     - BETA-06
   * - 9
     - Open an approval dialog, then close the window without answering
     - Process exits; file unchanged; no stray dialog
     - BETA-07
   * - 10
     - Restart and inspect the conversation and task history
     - Transcript returns; approvals and undo history do not
     - BETA-10, BETA-11
   * - 11
     - Search the UI and log for credential-shaped strings
     - No key or ``Bearer`` value appears
     - BETA-02, BETA-03

Verdict:

.. list-table::
   :header-rows: 1
   :widths: 30 70

   * - Verdict
     - Condition
   * - ``PASS``
     - Every applicable step behaved as specified, with evidence recorded.
   * - ``FAIL``
     - Any step failed. Record the exact text and stop if a stop condition in the
       beta plan applies.
   * - ``BLOCKED``
     - No API key, no network, no disposable project, or no usable desktop.
   * - ``NOT TESTED``
     - The step was not attempted and no external dependency prevented it.

Outward junction check
======================

This is a known defect in the published v0.9.0 artifact. The source tree contains
the fix, but the MSI does not. The test exists to record the real result, not to
demand a pass from the wrong artifact.

.. code-block:: powershell

    $outside = Join-Path $env:TEMP "qm-acceptance-outside"
    New-Item -ItemType Directory -Force -Path $outside | Out-Null
    Set-Content -LiteralPath (Join-Path $outside "outside.txt") `
        -Value "not a secret" -Encoding UTF8
    cmd /c mklink /J ".acceptance-fixture\widget-factory\linked-outside" "$outside"

Refresh the Project panel and ask the agent to summarize the project. For the
published v0.9.0 MSI, record a failed discovery or explicit error. Ask the agent
to read ``linked-outside/outside.txt``; the direct read must be refused. Remove
the junction afterwards:

.. code-block:: powershell

    cmd /c rmdir ".acceptance-fixture\widget-factory\linked-outside"

See :doc:`../qualification/defect-triage` and BETA-08 for the evidence fields.

Recording results
=================

Record beta results in :doc:`../qualification/manual-beta-acceptance`, not in the
historical v0.1 acceptance report. A local smoke result may be noted here only
with a date, artifact hash, provider/model and the exact observation.

The historical :doc:`acceptance-v0.1` report remains the record of what v0.1
verified. It is not a current release gate and must not be used to claim v0.9.0
acceptance.

Related
=======

* :doc:`../qualification/manual-beta-acceptance`
* :doc:`../qualification/v1.0.0-readiness`
* :doc:`../qualification/defect-triage`
* :doc:`../windows`
* :doc:`test-strategy`
