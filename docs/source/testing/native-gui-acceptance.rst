.. _native-gui-acceptance:

=========================
Native GUI acceptance
=========================

Automated checks prove the application *starts* and that a window is created.
They cannot prove the window is *usable*, and they cannot reach an approval
dialog at all: one only appears when a live model asks for a tool, and CI has no
provider credential.

Everything below therefore needs a person at a real desktop. **No check on this
page has been performed.** Every result is recorded as ``PENDING``.

.. contents::
   :local:

Before you start
================

Build the artifact on the platform you are testing. PyInstaller does not
cross-compile, so a Windows artifact built on Linux will not run on Windows.

.. code-block:: console

    python -m pip install -e ".[packaging]"
    python scripts/build_executable.py --clean

The artifact is ``dist/QuantsMindAgent/`` on Windows and Linux, or
``dist/QuantsMindAgent.app`` on macOS. Keep the whole folder or bundle together.

You need a provider credential for the approval checks. Enter it through the
application's own Settings dialog -- **never** paste it into a terminal, a file,
or a bug report. To keep your own profile untouched, run with a separate
config directory:

Windows
    .. code-block:: console

        dist\QuantsMindAgent\QuantsMindAgent.exe --config-dir %TEMP%\qm-desktop-check

Linux
    .. code-block:: console

        ./dist/QuantsMindAgent/QuantsMindAgent --config-dir /tmp/qm-desktop-check

macOS
    .. code-block:: console

        ./dist/QuantsMindAgent.app/Contents/MacOS/QuantsMindAgent \
          --config-dir /tmp/qm-desktop-check

Record what you observe, including the OS version and architecture. Do not
invent screenshots, dates, or observations; if you did not do a step, mark it
``PENDING``.

Result template
===============

Copy this into your report and fill one column per platform. ``PASS`` requires
having looked at the screen; anything not performed stays ``PENDING``.

.. list-table::
   :header-rows: 1
   :widths: 4 30 12 12 12 26

   * - #
     - Check
     - Windows
     - Linux
     - macOS
     - Observation / defect
   * - 1
     - Main window appears and renders
     - PENDING
     - PENDING
     - PENDING
     - Window title, panels, any blank regions
   * - 2
     - Open a project folder
     - PENDING
     - PENDING
     - PENDING
     - Sidebar and readiness indicator
   * - 3
     - Settings open, save, persist
     - PENDING
     - PENDING
     - PENDING
     - Value survived a restart?
   * - 4
     - ``edit_file`` dialog renders
     - PENDING
     - PENDING
     - PENDING
     - Tool, target, args, risk label readable?
   * - 5
     - Allow applies the edit
     - PENDING
     - PENDING
     - PENDING
     - Exactly the shown change applied?
   * - 6
     - Deny prevents the edit
     - PENDING
     - PENDING
     - PENDING
     - File byte-identical afterwards?
   * - 7
     - Close while idle
     - PENDING
     - PENDING
     - PENDING
     - Clean exit, no error dialog
   * - 8
     - Close while an approval is pending
     - PENDING
     - PENDING
     - PENDING
     - Exit, hang, or stray dialog?
   * - 9
     - Reopen after closing
     - PENDING
     - PENDING
     - PENDING
     - Normal startup, settings retained
   * - 10
     - No blank/frozen dialogs, no unintended edits
     - PENDING
     - PENDING
     - PENDING
     - Overall verdict

Report the OS and architecture for each platform you complete:
Windows 10 or 11 and x86_64; Linux distribution, desktop environment, display
server, and architecture; macOS version and whether arm64 or Intel.

.. warning::

   Check 8 is the most likely to expose a lifecycle bug and the least likely to
   be covered automatically. If closing during a pending approval crashes,
   hangs, or leaves a dialog on screen, that is a defect. Report it with your
   steps and the log file from the ``--config-dir`` above.

How to reach each check
=======================

Prepare a workspace with a file you are happy to have edited:

.. code-block:: console

    mkdir qm-acceptance && cd qm-acceptance
    printf '# Demo\n\ndef add(a, b):\n    return a - b\n' > calc.py

Then, with the application open:

#. **Check 1** -- launch the artifact using the command above. Confirm the
   window is drawn, not blank, and drag it to confirm it responds.
#. **Check 2** -- open ``qm-acceptance`` as the workspace. The sidebar should
   show the project and the status should indicate readiness once a credential
   is stored.
#. **Check 3** -- open Settings, change the model, save, close and reopen
   Settings. The value must persist.
#. **Checks 4 to 6** -- ask the agent something that requires editing, e.g.
   *"In calc.py, fix add() so it returns a + b."* Answer the approval dialog
   with **Allow** and confirm only the shown change happened; repeat with
   **Deny** and confirm the file is byte-for-byte unchanged
   (``sha256sum calc.py`` before and after).
#. **Check 7** -- close the window while nothing is running.
#. **Check 8** -- ask for an edit, then close the window *while the approval
   dialog is still open*, without answering it.
#. **Check 9** -- launch again and confirm normal startup and retained settings.

Windows 11 notes
================

* Unsigned PyInstaller builds are flagged by Windows security software. If it
  quarantines the artifact, that is expected and is a signing gap, not an
  application defect. Record it.
* Screen scaling and small screens may constrain the window below the size the
  application requests. That is normal window-manager behaviour.

Linux notes
===========

Report the distribution, version, desktop environment and display server
(X11 or Wayland). Qt's ``xcb`` plugin needs these on Debian-family systems:

.. code-block:: console

    sudo apt-get install -y libegl1 libgl1 libxkbcommon-x11-0 libdbus-1-3 \
      libfontconfig1 libxcb-cursor0 libxcb-icccm4 libxcb-image0 \
      libxcb-keysyms1 libxcb-randr0 libxcb-render-util0 libxcb-shape0 \
      libxcb-xinerama0 libxcb-xkb1 libxcb-glx0

**A Wayland-only session is unverified.** Qt's ``xcb`` plugin targets X11; on a
Wayland-only session Qt may fall back to ``wayland``, which needs
``libqt6waylandclient`` and is not covered by anything here.

The automated Linux results come from a **virtual X server (Xvfb)**, not from
a physical desktop, and that is recorded as such. Do not relabel a physical
test as Xvfb or the reverse.

macOS notes
===========

* The bundle is unsigned and un-notarised. Gatekeeper refuses the first
  launch: use Finder's *Open* command, then *Open* again in the dialog that
  appears. Do **not** disable Gatekeeper.
* Report the version (e.g. macOS 14.6) and architecture (arm64 or Intel).
* Only arm64 has been built and tested. See
  :doc:`platform-acceptance-matrix` for exactly what is missing for Intel.

Credential storage
==================

The API key goes to the operating system keyring: Credential Manager on
Windows, the Keychain on macOS, Secret Service on Linux. A Linux host with no
Secret Service provider can still read a legacy ``secrets.json``, but it refuses
to save a new key rather than writing plaintext. If you are testing on such a
host, note that saving a key is expected to fail until a credential service is
available.