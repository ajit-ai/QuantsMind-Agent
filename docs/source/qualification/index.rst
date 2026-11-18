.. _qualification-index:


=====================
Release qualification
=====================

This section records evidence about whether a release is fit for use. It is not
product documentation, and a plan is not a result.

Current assessment
==================

.. list-table::
   :header-rows: 1
   :widths: 24 76

   * - Item
     - Value
   * - Target
     - Published QuantsMind-Agent v0.9.0, Windows 11 x64
   * - Assessment date
     - 2026-10-20 (source remediation recorded 2026-10-21)
   * - Artifact
     - ``QuantsMindAgent-0.9.0-x64.msi``, SHA-256
       ``74823186651280cf6c7403bfdf1e016b35fbc2a9a3146aa750e2650f04e6dde6``
   * - Verdict
     - **CONDITIONAL -- LIMITED BETA ONLY** for the published v0.9.0 MSI
   * - Internal candidate
     - Built from the remediated commit into a separately named, ignored
       candidate directory. Its identity and SHA-256 are recorded beside the
       artifact. It is not published. Artifact-level G23 and G24 passed; the
       published v0.9.0 MSI is unchanged and still affected.
   * - Latest remediation
     - 2026-10-21: QF-006 junction discovery and QF-007 documentation are fixed,
       committed and carried with the internal candidate; the CI
       runner/reminder fixes are committed but not dispatched. QF-008 and
       signing QF-001 remain open; external evidence is still required.
   * - Not claimed
     - General real-world use, v1.0.0 readiness, code signing, clean-machine
       acceptance, real-provider acceptance, or human GUI acceptance

The MSI installs, launches, survives reinstall and uninstalls cleanly. The
source suite and packaged automated checks pass. The evidence does not include a
real model provider, a human approval-dialog review, a clean machine, or an
external tester. The installed artifacts are unsigned.

Documents
=========

.. toctree::
   :maxdepth: 1

   v1.0.0-readiness
   defect-triage
   manual-beta-acceptance
   v1.0.0-evidence-pack

* :doc:`v1.0.0-readiness` -- full report, evidence inventory and gate table.
* :doc:`defect-triage` -- open defects and qualification gaps, severity and
  close-out criteria.
* :doc:`manual-beta-acceptance` -- executable beta plan. **Not executed.**
* :doc:`v1.0.0-evidence-pack` -- blank recording templates for the human and
  external gates (QF-002/003/004/005/009/010). **Not executed.**

Status vocabulary
=================

.. list-table::
   :header-rows: 1
   :widths: 20 80

   * - Status
     - Meaning
   * - ``PASS``
     - The named evidence was observed at the required strength.
   * - ``FAIL``
     - The check ran and did not meet the requirement.
   * - ``BLOCKED``
     - A required dependency, machine, credential or person was unavailable.
   * - ``NOT TESTED``
     - No attempt was made, without an external blocker.
   * - ``NOT APPLICABLE``
     - The check is outside the stated scope.

A queued CI job is not a pass. A local stub is not a real provider. An automated
window check is not human GUI acceptance.

Related
========

* :doc:`../testing/platform-acceptance-matrix`
* :doc:`../testing/native-gui-acceptance`
* :doc:`../testing/manual-acceptance`
* :doc:`../known-issues`
* :doc:`../packaging`
