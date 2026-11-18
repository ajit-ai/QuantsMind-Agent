.. _windows:


==========
Windows 11
==========


Windows 11 is the primary target for QuantsMind-Agent. This page covers the
platform-specific behaviour, the manual validation procedure, and the known
limitations.

Windows-specific behaviour
==========================

Configuration and logs
----------------------

Paths come from ``platformdirs``:

.. code-block:: text

    %LOCALAPPDATA%\QuantsMind-Agent\QuantsMind-Agent\config.toml
    %LOCALAPPDATA%\QuantsMind-Agent\QuantsMind-Agent\secrets.json
    %LOCALAPPDATA%\QuantsMind-Agent\QuantsMind-Agent\logs\quantsmind-agent.log

``QM_CONFIG_DIR`` and ``QM_LOG_DIR`` override them, which is how the test suite
and the ``--config-dir`` flag stay out of the real user profile.

Credentials
-----------

With ``keyring`` installed the API key is stored in **Windows Credential
Manager**. A keyring that cannot be used is treated as unavailable rather than
as permission to write plaintext: the application still starts and can read a
legacy ``secrets.json``, but it refuses to save a new key there. The UI reports
the degraded state and suggests restoring Credential Manager access. The
application never fails to start because of this.

File paths
----------

The workspace accepts both separators, so ``src\app\main.py`` and
``src/app/main.py`` refer to the same file. Absolute paths, drive letters and
UNC paths are rejected: the agent may only address files relative to the opened
project root.

Junctions and symbolic links
----------------------------

A link inside the project that points outside it is treated as an escape: the
resolved path is checked against the root, and the request is refused. This
means a repository containing a link to ``C:\Users`` cannot be used to read that
directory through the agent.

Project discovery is deliberately stricter than an explicit path request.
Directory junctions, symbolic links and other reparse points are omitted from
listings and walks instead of being followed. A junction-containing workspace
therefore still produces a bounded snapshot, with the excluded link named as an
unknown; it no longer fails the whole scan. The published v0.9.0 MSI predates
this source fix; see :doc:`qualification/defect-triage`.

Long paths
----------

QuantsMind-Agent relies on the long-path behaviour of the Python runtime. Very
deep trees (beyond the classic 260-character limit) require the Windows
long-paths policy to be enabled; the default install is sufficient for normal
projects.

Manual validation procedure
===========================

The v0.9.0 Windows 11 acceptance gate is the executable beta plan:
:doc:`qualification/manual-beta-acceptance`. It covers install and hash,
first-run settings, a real-provider read-only task, approval-gated edits,
deny/stale-edit safety, command approval and policy refusal, closing during an
approval, the outward-junction known defect, the Project panel, a twenty-turn
session, and uninstall/profile preservation.

For a quick local check, use the smoke table in
:doc:`testing/manual-acceptance`. It is not a substitute for the beta evidence.

Do **not** record v0.9.0 results in :doc:`testing/acceptance-v0.1`; that report is
the historical v0.1 record. Record beta evidence in
:doc:`qualification/manual-beta-acceptance`, with the artifact hash and exact
observations.

The packaged MSI and the onedir executable have automated install, launch,
reinstall and uninstall checks. Those checks do not cover human rendering,
approval-dialog comprehension or a real model. Those remain manual beta gates.

.. _windows-manual-checklist:

Historical v0.1 Windows checklist
---------------------------------

This was the v0.1 read-only release gate. It is preserved for the historical
v0.1 release notes and acceptance report; it is **not** the current gate and
does not exercise writes, commands or approvals.

.. list-table::
   :header-rows: 1
   :widths: 8 68 24

   * - Step
     - Action
     - Result
   * - 1
     - Launch ``quantsmind-agent`` from a terminal
     - PASS / FAIL
   * - 2
     - Window renders; sidebar shows "No project open"
     - PASS / FAIL
   * - 3
     - **Open project…** and select a small repository
     - PASS / FAIL
   * - 4
     - Sidebar and status bar show the folder
     - PASS / FAIL
   * - 5
     - Settings opens; model and masked key are shown
     - PASS / FAIL
   * - 6
     - Ask "Explain the structure of this project"
     - PASS / FAIL
   * - 7
     - Tool activity lists at least one tool call
     - PASS / FAIL
   * - 8
     - The answer streams into the transcript
     - PASS / FAIL
   * - 9
     - Ask "Read the README and summarize it"
     - PASS / FAIL
   * - 10
     - ``read_file`` appears in the tool activity
     - PASS / FAIL
   * - 11
     - No credential appears in the UI or the log file
     - PASS / FAIL
   * - 12
     - Press **Stop** mid-request; the UI stays responsive
     - PASS / FAIL
   * - 13
     - Close the window; the process exits cleanly
     - PASS / FAIL

Known Windows limitations
=========================

* Windows 10 is not separately verified; the supported desktop target is
  Windows 11 x64.
* The portable onedir folder has no Start-menu entry or file association. The
  MSI creates an installed-apps entry and shortcuts.
* Long paths beyond 260 characters need the Windows long-paths policy.
* When Credential Manager is unavailable, a legacy ``secrets.json`` remains
  readable but new keys cannot be saved until a credential service is usable.
* Sockets and TLS use ``httpx``'s bundled ``certifi`` bundle, not the Windows
  certificate store, so a corporate proxy with a private CA needs
  ``REQUESTS_CA_BUNDLE``-style configuration at the environment level.
* The published v0.9.0 MSI is unsigned and predates the junction-discovery fix.
