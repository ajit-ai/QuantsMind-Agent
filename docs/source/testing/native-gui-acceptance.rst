.. _native-gui-acceptance:

=========================
Native GUI acceptance
=========================

Headless automation proves the application *starts*. It cannot prove the window
*looks right*, and it cannot reach an approval dialog at all, because an
approval dialog only appears when a live model asks for a tool -- and CI has no
provider credential.

Everything on this page therefore requires a human at a real desktop. The
automated evidence that does exist is recorded separately, in
:doc:`platform-acceptance-matrix`.

.. contents::
   :local:

What automation already covers
==============================

Do not repeat these by hand; they run in CI every push.

* the packaged executable starts and creates its config, log, cache and data
  directories
* Qt finds its platform plugin (offscreen, xcb, cocoa, windows)
* a titled top-level window is created and survives startup
* the workspace opens, tools register, unknown flags are rejected, a missing
  workspace is reported without crashing
* the GUI test suite runs against the real platform plugin on Windows, macOS
  and a virtual X server on Linux

What only a human can verify
============================

These are **PENDING**. None has been performed.

Preparing a build
-----------------

.. code-block:: console

    python -m pip install -e ".[packaging]"
    python scripts/build_executable.py --clean

The artifact is ``dist/QuantsMindAgent/`` (Windows, Linux) or
``dist/QuantsMindAgent.app`` (macOS). Keep the whole folder together.

You need a provider credential for the approval-dialog checks. Enter it through
the application's Settings dialog -- do not paste it into a terminal or a file.
To keep the test workspace clean, use a separate profile:

.. code-block:: console

    dist\QuantsMindAgent\QuantsMindAgent.exe --config-dir %TEMP%\qm-desktop-check

Result template
---------------

Record one line per check. ``PASS`` requires having looked at the screen.

.. list-table::
   :header-rows: 1
   :widths: 4 34 12 50

   * - #
     - Check
     - Result
     - What to record
   * - 1
     - Main window appears and renders
     - PENDING
     - Title, whether panels/sidebar/composer are drawn, any blank areas
   * - 2
     - Open a project folder
     - PENDING
     - Sidebar shows the project; status becomes "ready"
   * - 3
     - Settings open, save and persist
     - PENDING
     - Change the model, save, reopen Settings; value persisted?
   * - 4
     - ``edit_file`` approval dialog renders
     - PENDING
     - Tool, target, arguments and risk label all readable and not blank
   * - 5
     - Allow applies the edit
     - PENDING
     - File content changed as shown in the dialog, nothing else changed
   * - 6
     - Deny prevents the edit
     - PENDING
     - File byte-for-byte unchanged
   * - 7
     - Close while idle
     - PENDING
     - Exits cleanly, no error dialog
   * - 8
     - Close while an approval is pending
     - PENDING
     - Exits cleanly; no crash; no stray dialog left behind
   * - 9
     - Reopen after closing
     - PENDING
     - Window appears again; previous settings retained
   * - 10
     - No blank or frozen dialogs, no unintended edits
     - PENDING
     - Overall verdict and anything that looked wrong

.. warning::

   Check 8 is the one most likely to expose a lifecycle bug, and the one least
   likely to be covered automatically. If closing during a pending approval
   crashes, hangs, or leaves a dialog on screen, that is a defect -- please
   report it with the steps and the log file from the ``--config-dir`` above.

Platform notes for the tester
=============================

Windows
    Confirm the window is not blank. Windows Defender may quarantine an unsigned
    build; the folder is produced by PyInstaller and is not code-signed.

Linux
    The build needs Qt's xcb libraries present. On Debian/Ubuntu::

        sudo apt-get install -y libegl1 libgl1 libxkbcommon-x11-0 \
          libdbus-1-3 libfontconfig1 libxcb-cursor0 libxcb-icccm4 \
          libxcb-image0 libxcb-keysyms1 libxcb-randr0 libxcb-render-util0 \
          libxcb-shape0 libxcb-xinerama0 libxcb-xkb1 libxcb-glx0

    Verified on Ubuntu 22.04 x86_64 in CI, under a virtual X server. A real
    desktop session on another distribution has not been tested.

macOS
    The bundle is neither signed nor notarised, so Gatekeeper refuses to open it
    on first launch. Use Finder's *Open* command, then *Open* again in the dialog
    that appears. Do not disable Gatekeeper.

    Verified on macOS 14 arm64 in CI. Intel has not been tested: GitHub-hosted
    Intel runners were unavailable for the whole Phase 2 run.