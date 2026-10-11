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

* Failing runs produce no test name and no Python traceback naming a test. The
  process dies mid-suite.
* Re-running the same commit passes every time. Observed on ``f611536``,
  ``30e6542``, ``f1ef166``, ``845c0f8`` and ``7f33e59`` -- the failures appear
  on commits where a re-run of the identical tree succeeded.
* The banner alternates between ``none_dealloc: deallocating None`` and
  ``Fatal Python error: Aborted``.
* The faulting frame moves with wherever a finalisation happens to be triggered:
  it has been seen inside ``Garbage-collecting`` in ``_drain_agent_events``,
  inside a plain ``drain`` call, and most recently in
  ``_pytest/warnings.py`` inside ``pytest_runtest_protocol``. Nothing in our
  code is consistently responsible.
* ``pyproject.toml`` sets
  ``filterwarnings = ["error::DeprecationWarning:quantsmind_agent.*"]``, so
  pytest installs and tears down a warnings filter around **every** test. The
  most recent failure landed inside that machinery, which makes the warnings
  plugin a plausible trigger rather than a bystander.
* It is **not** the same defect as the Linux xcb problem below. That one was
  reproducible on demand and is fixed. This one occurs on the *offscreen*
  platform too, and has also been seen on Python 3.12, so it is neither
  display-plugin-specific nor 3.11-specific.
* An earlier, separately identified defect -- ``MainWindow`` never releasing its
  ``EventBus`` subscription, which left abandoned windows with live timers for
  the cyclic collector to finalise -- produced the same exit code and was fixed
  in PR #1. That fix made the single-process full suite stable, but this
  residual remains.

The diagnostic exists now, and measuring it immediately paid off
----------------------------------------------------------------

``scripts/diagnose_gui_abort.py`` runs the suite N times and records what each
run did. The **first dispatch of it produced a false result, and that is worth
recording**: every run failed during collection with
``ImportError: libEGL.so.1`` because the workflow was missing the Qt system
libraries. The harness reported ``abort 0/8 (0%)``.

That number was meaningless -- it was 0% over *zero runs that executed a single
test*. So the harness was changed to make that class of failure impossible to
report as a clean result:

* exit code 2 is now classified as ``collection-error`` and never counts as a
  sample;
* a run whose outcome is not one of ``pass`` / ``test-failure`` / ``abort`` /
  ``segfault`` is not counted as executed either;
* if fewer runs executed than were requested, the rate prints as
  ``NOT MEASURED`` and the process exits **non-zero** with a message pointing at
  the missing libraries;
* each run now gets its own ``--basetemp``. pytest's default is one shared
  directory that it rewrites per run; on a machine where that directory was held
  open by another process, every run failed with
  ``PermissionError: ... pytest-of-<user>`` and looked like a genuine test
  failure.

The workflow now installs the Qt system libraries. Three consecutive local runs
of the harness report ``pass 3/3, abort rate 0/3 executed runs (0%)`` -- on
Windows/Python 3.14, where the abort has never reproduced. **This is not
evidence that the abort is fixed.** It is evidence that the instrument works.

What has been ruled out
-----------------------

* Not the lifecycle regression tests: they pass, and they are preserved.
* Not the offscreen platform plugin: the same abort occurs under it.
* Not introduced by the packaging work: the first occurrence predates it.

Next diagnostic step
--------------------

Two steps, cheapest first, neither of which hides the failure:

#. **Test whether the warnings plugin is the trigger.** Run the GUI job with the
   plugin disabled::

       python scripts/run_qt_tests.py tests/gui -q -p no:warnings

   If several consecutive runs are clean, the interaction is pytest's
   per-test warning filter combined with PySide6 finalisation, and the fix
   belongs in how the suite is configured -- not in a retry.
#. **Capture a native stack.** If it still aborts, install ``gdb`` and run::

       xvfb-run -a gdb --batch \\
         -ex run -ex bt -ex "thread apply all bt" \\
         --args python scripts/run_qt_tests.py tests/gui -q

Suspected area, stated as a hypothesis and not a conclusion: teardown ordering
between PySide's object ownership and CPython's finalisation, provoked by the
per-test warnings filter. That is the class of fault that produces
``none_dealloc``/``SIGABRT`` with no test named, and it matches every
observation so far. It has not been confirmed.

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

The one-file build **succeeded** and passes **14 of 14** smoke checks. The
original failure was the file-presence plugin check, which is meaningless for a
layout that extracts at runtime; that check is now layout-aware.

Its GUI step is a further finding. A one-file build is a self-extracting
bootloader that runs the real application as a **child process**, so the window
belongs to the child, not to the pid that was launched. The probe now walks
child processes and finally falls back to matching on the window title.

This does **not** promote one-file to the verified layout. Onedir remains the
release candidate: one-file is slower to start, writes to the system temp
directory on every launch, is flagged more readily by security software, and
still has to be signed.

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
deploy or publish step, so documentation is **not** publicly available and no
URL should be quoted for it.

Public deployment is additionally **not available in this repository's current
configuration**, which was verified rather than assumed:

* the repository is **private** (``"private": true``, ``"visibility":
  "private"``)
* GitHub Pages is **not enabled** (``has_pages: false``; the Pages API returns
  404)
* the available API token carries ``gist, read:org, repo, workflow`` -- no
  Pages or administration scope

GitHub Pages for a private repository requires GitHub Enterprise Cloud. On
github.com a private repository cannot host public Pages. Enabling publication
would require at least one of:

* making the repository public -- a significant, deliberate decision that is
  not taken here and is not implied by this work
* moving to GitHub Enterprise Cloud
* choosing a different host (Read the Docs, Netlify, Vercel, S3) and
  configuring its credentials

None of these has been done. Until one is, the documentation **build** is
verified and publication is **pending**.
