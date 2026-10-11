.. _platform-acceptance-matrix:

.. list-table::
   :header-rows: 1
   :widths: 16 26 18 40

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

What has actually been verified, per platform and per kind of check. A cell
records only what was really observed. Queued, skipped, unavailable and
human-pending checks are recorded as such and are never counted as passes.

.. contents::
   :local:

The four kinds of evidence
==========================

They are not interchangeable, and the most common mistake in a release report is
treating a weaker one as a stronger one.

#. **Headless smoke** -- the packaged executable is launched with
   ``--check-config``. It proves the bundle imports Qt and finds a platform
   plugin. It never opens a window, so it cannot detect a blank one.
#. **Automated GUI (offscreen)** -- the GUI test suite with Qt's ``offscreen``
   platform. Real widgets, no display server.
#. **Automated GUI (real display)** -- the same suite against the platform's
   actual plugin (``windows``, ``xcb`` under Xvfb, ``cocoa``), plus launching
   the packaged executable and asserting a titled top-level window appears.
#. **Human desktop acceptance** -- rendering, and the approval dialog. Requires
   a person; see :doc:`native-gui-acceptance`. **None performed yet.**

Matrix
======

.. list-table::
   :header-rows: 1
   :widths: 15 11 11 11 11 11 30

   * - Platform
     - Source unit + integration
     - Headless smoke
     - GUI offscreen
     - GUI real display
     - Packaged executable
     - Notes
   * - Windows 10/11 x64
     - PASS
     - PASS
     - PASS
     - PASS
     - PASS (onedir)
     - Native desktop session used for the GUI run
   * - Linux x64, Ubuntu 22.04
     - PASS
     - PASS
     - PASS
     - PASS (Xvfb)
     - PASS (onedir)
     - Virtual display only; no real desktop session tested
   * - macOS 14 arm64
     - PASS
     - PASS
     - PASS
     - PASS
     - PASS (``.app``)
     - Unsigned; Gatekeeper refuses first launch
   * - macOS Intel x64
     - not run
     - not run
     - not run
     - not run
     - not run
     - GitHub-hosted Intel runners were unavailable for the whole phase
   * - BSD
     - not run
     - not run
     - not run
     - not run
     - not run
     - Blocked: no PySide6 wheel exists

**Nothing above is human desktop acceptance.** No row has had a person look at
the rendered window or exercise an approval dialog.

Operating systems and architectures tested
==========================================

.. list-table::
   :header-rows: 1
   :widths: 16 26 18 40

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

Prerequisites and known limitations
===================================

Linux system libraries
----------------------

The packaged Linux build links Qt's xcb platform plugin, which needs::

    libegl1 libgl1 libxkbcommon-x11-0 libdbus-1-3 libfontconfig1
    libxcb-cursor0 libxcb-icccm4 libxcb-image0 libxcb-keysyms1
    libxcb-randr0 libxcb-render-util0 libxcb-shape0 libxcb-xinerama0
    libxcb-xkb1 libxcb-glx0

This set was found the hard way: Phase 1 ran Linux with Qt's *offscreen*
platform, which links none of them, so the gap was invisible until a real X
server was used. Qt aborts the process when no platform plugin can be
initialised, and the explanatory message is swallowed by pytest, so the symptom
is a bare ``Fatal Python error: Aborted``. The Linux job now installs the full
set and asserts ``app.platformName() == "xcb"`` before running anything else.

Credential storage
------------------

The API key goes to the OS keyring: Credential Manager on Windows, the Keychain
on macOS, Secret Service on Linux. On a Linux host with no Secret Service
provider the application falls back to a file-backed store, which is only as
protected as the file's permissions.

Signing
-------

No artifact is code-signed or notarised. Windows security software may flag an
unsigned PyInstaller build; macOS Gatekeeper refuses first launch until the user
explicitly allows it in Finder. Do not disable Gatekeeper.

Console output encoding
-----------------------

A windowed PyInstaller build writes stdout in the machine's legacy ANSI code
page on Windows and ignores ``PYTHONIOENCODING`` and ``PYTHONUTF8``. Non-ASCII
paths printed by ``--check-config`` are therefore mangled in a captured pipe on
some machines. This affects diagnostic output only, not the application's own
path handling, which was verified separately.

Intermittent CPython 3.11 GUI abort
------------------------------------

The ubuntu-3.11 GUI job aborted with exit 134 (SIGABRT) in roughly one run in
two during Phases 1 and 2, and passed on re-run every time. It is *not* the same
as the xcb problem: it also occurs on the offscreen platform and on 3.12.

Status: **unresolved**. Evidence and next steps are recorded in
:doc:`../known-issues`. Not worked around by retries in the workflow: a failing
run is re-run manually and reported.

.. note::

   Documentation here is built by CI and uploaded as an artifact. It is **not
   published** -- ``docs.yml`` has no deploy step. Do not describe this page as
   publicly available until a deployment exists.
