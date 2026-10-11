.. _platform-acceptance-matrix:

=================================
Platform acceptance matrix
=================================

What has actually been verified, per platform, using explicit labels. A cell is
never a single unqualified "supported". Queued, skipped, unavailable and
human-pending checks appear as such.

.. contents::
   :local:

Labels used
===========

============================  =============================================
Label                         Meaning
============================  =============================================
``Built``                     A packaged artifact was produced and its
                              ``BUILD-METADATA.json`` recorded
``Automated smoke passed``    The artifact starts headlessly and passes
                              ``scripts/smoke_test_executable.py``
``Automated GUI passed``      The GUI suite ran against the platform's real
                              Qt plugin, and the packaged artifact opened a
                              titled window
``Automated GUI under Xvfb``  As above, but on a virtual display, not a
                              physical session
``Human acceptance passed``   A person confirmed rendering and drove the
                              approval workflow. **Nobody has done this.**
``Unverified``                Not attempted, or blocked by infrastructure
``Blocked``                   Cannot be attempted without a dependency that
                              does not exist
============================  =============================================

The four strengths of evidence
==============================

They are not interchangeable. The most common way a release report goes wrong
is treating a weaker one as a stronger one.

#. **Headless smoke** -- ``--check-config``. Proves the bundle imports Qt and
   finds a platform plugin. Never opens a window, so it cannot detect a blank
   one.
#. **Automated GUI (real display)** -- the GUI suite against the platform's own
   Qt plugin (``windows``, ``xcb``, ``cocoa``) plus launching the packaged
   artifact and asserting a titled window appears.
#. **Virtual-display GUI** -- the same, under Xvfb. Real Qt rendering, but not
   a physical desktop session, and with no window manager.
#. **Human desktop acceptance** -- rendering and the approval dialog.
   Requires a person; see :doc:`native-gui-acceptance`. **Not performed.**

Matrix
======

.. list-table::
   :header-rows: 1
   :widths: 13 26 9 22 20 10

   * - Platform
     - Source unit + integration
     - Built
     - Automated smoke passed
     - Automated GUI
     - Human acceptance passed
   * - Windows 10/11 x64
     - PASS (CI + local)
     - onedir
     - 14/14
     - PASS -- real ``windows`` plugin
     - **No -- pending**
   * - Linux x64, Ubuntu 22.04
     - PASS (CI)
     - onedir
     - 14/14
     - PASS -- ``xcb`` under Xvfb
     - **No -- pending**
   * - macOS 14 arm64
     - PASS (CI)
     - ``.app``
     - 14/14
     - PASS -- ``cocoa``
     - **No -- pending**
   * - macOS Intel x64
     - Unverified
     - Unverified
     - Unverified
     - Unverified
     - **No -- pending**
   * - BSD (FreeBSD, NetBSD, OpenBSD)
     - Blocked
     - Blocked
     - Blocked
     - Blocked
     - **No -- pending**

**No row has human desktop acceptance.** Nothing in this table verifies what the
window looks like or that an approval dialog can be allowed or denied.

Operating systems and architectures actually exercised
=======================================================

.. list-table::
   :header-rows: 1
   :widths: 16 26 18 44

   * - Platform
     - OS version
     - Architecture
     - Packaging
   * - Windows
     - Windows 11
     - x86_64
     - PyInstaller onedir
   * - Linux
     - Ubuntu 22.04 LTS (jammy)
     - x86_64
     - PyInstaller onedir
   * - macOS
     - macOS 14
     - arm64
     - PyInstaller application bundle

Python: 3.11 and 3.12 are exercised in CI. Packaging builds used 3.12; local
Windows GUI verification used 3.14.

Distribution and signing readiness
==================================

.. list-table::
   :header-rows: 1
   :widths: 16 26 30 28

   * - Platform
     - Code signing
     - Notarisation / Gatekeeper
     - Status
   * - Windows
     - **None**. Unsigned PyInstaller build.
     - Not applicable
     - Security software may quarantine it. Not release-ready.
   * - macOS
     - **None**
     - **Not done.** Gatekeeper refuses first launch until the user explicitly
       allows it in Finder.
     - Not distributable to other machines as-is.
   * - Linux
     - Not applicable
     - Not applicable
     - Runnable from the folder as-is.

No artifact in this repository is signed or notarised. That is a release
blocker, independent of test results.

macOS Intel: exact missing evidence
===================================

A macOS Intel artifact was never produced. GitHub-hosted ``macos-13`` Intel
runners were unavailable for the entire Phase 2 and Phase 3 runs -- the job sat
queued while the other three completed. The missing evidence is an Intel
``BUILD-METADATA.json`` plus a smoke result and a GUI result from an Intel
runner. The arm64 result does not transfer, because the artifact is
architecture-specific and no universal binary has been built.

Viable options: drop the Intel target until capacity returns, run Intel builds
on a self-hosted or Cirrus runner, or produce a ``universal2`` binary on an
Apple Silicon runner -- which still needs Apple toolchain support and signing.

Linux scope
===========

The Linux results are from **Ubuntu 22.04 on GitHub-hosted runners**. That is
one distribution on one architecture. Other distributions -- Debian, Fedora,
Arch, the BSDs -- are **unverified**. The dependency set below is what Qt 6's
``xcb`` plugin needs on Debian-family systems; a distribution with different
library names or a Wayland-only session may need more.

.. note::

   The documentation here is built by CI and uploaded as an artifact. It is
   **not published** -- ``docs.yml`` has no deploy step. Do not describe this
   page as publicly available until a deployment exists.
