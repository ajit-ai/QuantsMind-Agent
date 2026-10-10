.. _known-issues:

=============
Known issues
=============

Unresolved problems, with the evidence behind them. Nothing here is worked
around silently.

.. contents::
   :local:

Intermittent CPython 3.11 GUI abort (exit 134)
===============================================

.. list-table::
   :header-rows: 1
   :widths: 22 78

   * - Status
     - **Unresolved.** Not suppressed, not retried automatically.
   * - Affects
     - ``ubuntu-latest, py3.11`` GUI job, intermittently. Roughly one run in
       two during Phases 1 and 2.
   * - Symptom
     - The GUI step dies with ``Aborted (core dumped)`` and exit code 134.
       Unit and integration steps in the same job pass.

Evidence
--------

* Failing runs produce no test name and no Python traceback. The process dies
  mid-suite.
* Re-running the same commit passes every time. Observed on ``f611536``,
  ``30e6542``, ``f1ef166`` and ``845c0f8`` -- the failures appear on commits
  where a re-run of the identical tree succeeded.
* The traceback frame has moved between ``Garbage-collecting`` inside
  ``_drain_agent_events`` and plain ``drain`` calls, so it is not one specific
  test.
* It is **not** the same defect as the Linux xcb problem below. That one was
  reproducible on demand and is fixed. This one occurs on the *offscreen*
  platform too, and it has also been seen on Python 3.12, so it is neither
  display-plugin-specific nor 3.11-specific.
* An earlier, separately identified defect -- ``MainWindow`` never releasing its
  ``EventBus`` subscription, which left abandoned windows with live timers for
  the cyclic collector to finalise -- produced the same exit code and was fixed
  in PR #1. That fix made the single-process full suite stable, but this
  residual remains.

What has been ruled out
-----------------------

* Not the lifecycle regression tests: they pass, and they are preserved.
* Not the offscreen platform plugin: the same abort occurs under it.
* Not introduced by the packaging work: the first occurrence predates it.

Next diagnostic step
--------------------

The abort needs a native stack, and this environment has no debugger. On a
Linux runner, install ``gdb`` and re-run the GUI job under it, so the
``SIGABRT`` is captured with the faulting native frames rather than only the
Python-level ``Fatal Python error`` banner. A concrete command to add to the job
temporarily::

    sudo apt-get install -y gdb
    xvfb-run -a gdb --batch \\
      -ex run -ex bt -ex "thread apply all bt" \\
      --args python scripts/run_qt_tests.py tests/gui -q

Suspected area, stated as a hypothesis and not a conclusion: teardown ordering
between PySide's object ownership and interpreter shutdown. That is the class of
fault that produces ``none_dealloc``/``SIGABRT`` with no Python traceback, and
it matches the only evidence available. It has not been confirmed.

Linux Qt xcb system libraries (fixed)
======================================

The packaged Linux build needs the Qt 6 xcb dependency set listed in
:doc:`testing/platform-acceptance-matrix`. It was missing because Phase 1 only ever ran
Linux with Qt's *offscreen* platform, which links none of them; Qt aborts when no
platform plugin loads, and pytest swallows the explanation. The Linux GUI job
now installs the full set and asserts ``app.platformName() == "xcb"`` before
running the suite.

Windows window-size assertion (fixed)
=====================================

``test_startup_smoke`` asserted the window was exactly the size it requested.
That only holds without a window manager, so it passed offscreen and failed on
a real desktop. Now window-manager independent, with a minimum size so a
collapsed window still fails.

One-file packaging is experimental
==================================

The verified release layout is **onedir**. One-file builds are produced by a
separate ``continue-on-error`` CI job purely to gather evidence.

Implications, measured and expected:

.. list-table::
   :header-rows: 1
   :widths: 26 74

   * - Property
     - Consequence
   * - Extraction
     - Everything, including the Qt platform plugins, is unpacked to a
       temporary directory at every start and removed afterwards. The plugins
       are therefore not files under ``dist/`` and cannot be verified there.
   * - Startup
     - Slower than onedir: the archive is unpacked before the first byte of
       application code runs. Onedir builds started in roughly 3s.
   * - Temporary files
     - Writes go to the system temp directory on every launch. Some security
       products flag that pattern.
   * - Antivirus
     - Large self-extracting executables are flagged far more often than a
       folder. Unsigned builds are the worst case.
   * - Size
     - Roughly the same installed footprint, but a single large file rather
       than a directory.

The one-file build **succeeded** and passed 13 of 14 smoke checks; the one
failure was the file-presence plugin check, which is meaningless for a layout
that extracts at runtime. That check is now layout-aware. This does **not**
promote one-file to the verified layout: onedir remains the release candidate
until one-file is signed and its startup cost is measured.

macOS Intel is unverified
=========================

GitHub-hosted Intel macOS runners were unavailable for the entire phase; the
job sat queued while the other three completed. No Intel result is claimed.
The arm64 result does not transfer, because the artifact is architecture-
specific. Options: drop the Intel target until capacity exists, or run Intel
builds on a self-hosted or Cirrus runner.

Windows desktop acceptance is outstanding
==========================================

No human has looked at the rendered window or exercised an approval dialog on
any platform. See :doc:`testing/native-gui-acceptance` for the checklist and the result
template. The ``Manual Windows 11 acceptance required`` CI job is a four-second
reminder and is not a test.

Documentation is not published
==============================

``docs.yml`` builds the HTML and uploads it as a CI artifact. There is no
deploy or publish step. Documentation is therefore not publicly available, and
nothing should claim otherwise until a deployment exists.