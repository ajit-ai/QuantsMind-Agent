.. _qualification-manual-beta:


=====================================
Manual beta acceptance plan: v0.9.0
=====================================

.. contents::
   :local:
   :depth: 2

**Execution status: NOT TESTED.** This is a plan, not a result. No external
tester has run it. Do not copy a row into a release report as ``PASS`` without
the recorded evidence.

The plan exists because automated checks cannot establish what a person sees,
whether an approval is understandable, or whether a real model chooses the right
tool. It is the bridge between the automated v0.9.0 evidence and a v1.0.0
decision.

Purpose
========

The beta answers four questions:

#. Does the published MSI install and start on a machine that is not the build
   host?
#. Does a real provider complete a read-only task and an approval-gated change?
#. Can a person understand and safely answer the approval dialog?
#. Does the application preserve the workspace, the user profile and the
   expected safety boundaries when things go wrong?

It does **not** certify v1.0.0. It supplies evidence for the open items in
:doc:`defect-triage`.

Beta cohort and scope
=====================

.. list-table::
   :header-rows: 1
   :widths: 24 76

   * - Item
     - Requirement
   * - Product
     - Published v0.9.0 Windows 11 x64 MSI and matching onedir build.
   * - Testers
     - At least three technical testers. At least one must not be the build
       operator.
   * - Machines
     - At least two Windows 11 x64 machines, including one clean VM or clean
       separate user profile with no Python, no source checkout and no prior
       install.
   * - Projects
     - Disposable projects or the generated acceptance fixture. No production
       repository, customer data, credentials, or work that cannot be restored.
   * - Provider
     - A dedicated, low-limit, revocable key entered through Settings.
   * - Duration
     - One supervised session per tester, plus a 20-turn session for at least one
       tester.
   * - Out of scope
     - Linux, macOS, Windows 10, ARM64, multi-user deployment, enterprise
       management, accessibility audit and model-quality benchmarking.

Do not expand the cohort on the strength of a single successful session. The
purpose is to find disagreements between the product and its documentation.

Artifact and trust
==================

Confirm the hash before installing:

.. code-block:: powershell

    Get-FileHash .\QuantsMindAgent-0.9.0-x64.msi -Algorithm SHA256

Expected:

.. code-block:: text

    74823186651280cf6c7403bfdf1e016b35fbc2a9a3146aa750e2650f04e6dde6

The build is unsigned. Record the SmartScreen or security-product result. A
warning is expected; a silent quarantine is a finding. Do not disable Windows
security controls to force the test.

Safety rules
============

#. **Use disposable data.** Back up the project or use a throwaway folder.
#. **Use a revocable key.** Never paste the key into a terminal, a report, a
   screenshot, or a chat message.
#. **Never test an outward junction on a repository you care about.** Use a
   disposable folder and a harmless outside file.
#. **Do not approve a command you do not understand.** If the dialog is unclear,
   record ``FAIL`` and stop that case.
#. **Do not test a blocked command by approving it.** A blocked command should
   never reach approval; if it does, stop and record a security finding.
#. **Close only after the current file operation has settled**, except in the
   deliberate pending-approval test.
#. **Stop on any stop condition** listed below, preserve the log, and notify the
   coordinator.

Preparing the fixture
=====================

On a machine with PowerShell, create a disposable project:

.. code-block:: powershell

    $root = Join-Path $env:USERPROFILE "qm-beta"
    New-Item -ItemType Directory -Force -Path $root | Out-Null
    Set-Content -LiteralPath (Join-Path $root "README.md") `
        -Value "# Beta fixture`n`nA tiny project for approval testing." -Encoding UTF8
    Set-Content -LiteralPath (Join-Path $root "calc.py") `
        -Value "def add(a, b):`n    return a - b`n" -Encoding UTF8
    Set-Content -LiteralPath (Join-Path $root "calc_note.txt") `
        -Value "Expected: add(2, 3) == 5" -Encoding UTF8

If Python is available, also create ``test_calc.py``:

.. code-block:: powershell

    Set-Content -LiteralPath (Join-Path $root "test_calc.py") `
        -Value "from calc import add`n`ndef test_add():`n    assert add(2, 3) == 5`n" `
        -Encoding UTF8

Do not initialize a Git repository unless a test explicitly asks for one.

Test matrix
===========

A row is ``NOT TESTED`` until it is run; BETA-12 is ``BLOCKED`` until a v1.0.0
candidate exists. Use the exact statuses from :doc:`v1.0.0-readiness`. If a
known defect appears, record ``FAIL`` and cite the triage ID; do not relabel it
as ``PASS`` because it was expected.

.. list-table::
   :header-rows: 1
   :widths: 11 31 34 12 12

   * - ID
     - Test
     - Minimum evidence
     - Priority
     - Status
   * - BETA-01
     - Hash, install, launch, close
     - Hash, SmartScreen outcome, installed version, window title, exit
     - Required
     - NOT TESTED
   * - BETA-02
     - First run, Settings and credential persistence
     - No-key message, masked key, restart persistence, no key in log
     - Required
     - NOT TESTED
   * - BETA-03
     - Real-provider read-only task
     - Tool activity before answer; answer matches fixture; no credential
     - Required
     - NOT TESTED
   * - BETA-04
     - Approval-gated edit
     - Exact diff shown; Allow applies only that change
     - Required
     - NOT TESTED
   * - BETA-05
     - Deny and stale-edit safety
     - Deny leaves bytes unchanged; stale edit reports conflict
     - Required
     - NOT TESTED
   * - BETA-06
     - Command approval and policy refusal
     - Approved harmless command runs; ``curl --version`` never reaches approval
     - Required
     - NOT TESTED
   * - BETA-07
     - Close while approval is pending
     - Process exits, file unchanged, no stray dialog, no orphan process
     - Required
     - NOT TESTED
   * - BETA-08
     - Outward junction workspace
     - Scan fails without reading outside file; triage as QF-006
     - Known defect
     - NOT TESTED
   * - BETA-09
     - Project panel and bounded snapshot
     - Kind, roots, checks, sampling/truncation state visible or stated
     - Recommended
     - NOT TESTED
   * - BETA-10
     - Twenty-turn session
     - Memory/UI notes and no crash; tested limit recorded
     - Required for one tester
     - NOT TESTED
   * - BETA-11
     - Uninstall and profile preservation
     - Install, shortcuts and entry removed; profile unchanged
     - Required
     - NOT TESTED
   * - BETA-12
     - 0.9-to-1.0 upgrade and rollback
     - Settings, conversations and task evidence survive; rollback works
     - Blocked until v1 candidate
     - BLOCKED

Procedures
==========

BETA-01 -- Hash, install, launch, close
---------------------------------------

#. On the clean machine, record OS version, architecture and whether Python is
   installed.
#. Run ``Get-FileHash`` and compare with the expected SHA-256.
#. Double-click the MSI and complete the per-user install. Record the SmartScreen
   or security-product prompt verbatim.
#. Confirm the installed version in **Settings > Apps** or **Add/Remove
   Programs** is ``0.9.0``.
#. Launch from the Start Menu. Confirm the window title is
   ``QuantsMind-Agent 0.9.0`` and the window is drawn, not blank.
#. Close the window. Confirm the process exits and no ``QuantsMindAgent`` or
   orphaned Python process remains.

Record: hash match, prompt, install path, version, title, exit behaviour.

BETA-02 -- First run, Settings, credential
------------------------------------------

#. Before entering a key, start with an empty config directory if the command
   line is available::

       QuantsMindAgent.exe --config-dir %TEMP%\qm-beta-config

   If the OS credential store already contains a QuantsMind entry, use a clean
   VM or a null keyring backend for this check. ``--config-dir`` alone does not
   isolate Windows Credential Manager.
#. Confirm the readiness message names a missing API key and no traceback
   appears.
#. Open **Settings**, choose the provider, enter the dedicated key, and save.
#. Confirm the key is masked and cannot be read back from the Settings field.
#. Close and relaunch with the same config directory. Confirm the provider and
   masked status persist.
#. Search the log for credential-shaped strings, never for the key itself:

   .. code-block:: powershell

       Select-String -Path "$env:LOCALAPPDATA\QuantsMind-Agent\QuantsMind-Agent\logs\quantsmind-agent.log" `
           -Pattern 'sk-|Bearer |api_key'

   If a custom config directory was used, search the log under that directory
   instead. Expected: no matches that contain the key.

Record: no-key message, masked form, persistence, log result.

BETA-03 -- Real-provider read-only task
---------------------------------------

#. Open the fixture folder.
#. Refresh the project panel. Record the detected kind, roots and checks.
#. Ask::

       Read README.md and calc.py. Explain what add() currently does and what
       the note says it should do.

#. Confirm ``read_file`` or an equivalent workspace tool appears in the activity
   panel before the answer.
#. Confirm the answer says ``add`` returns ``a - b`` and the note expects
   ``a + b``.
#. Confirm no credential appears in the transcript, activity panel, or log.

This is a real-model test. A scripted answer or a claim without a tool call does
not pass.

BETA-04 -- Approval-gated edit
------------------------------

#. Ask::

       In calc.py, fix add() so it returns a + b. Show me the proposed change
       before applying it.

#. Confirm an approval dialog appears and names ``edit_file``, the target
   ``calc.py``, and the exact change.
#. Choose **Allow Once** or **Allow**. Confirm the file now contains
   ``return a + b`` and only that change was made.
#. Run a read-only check if Python is available::

       python -m pytest -q

   The new behavior should make the test pass. If Python is unavailable, record
   that and verify the file content directly.

BETA-05 -- Deny and stale-edit safety
-------------------------------------

#. Reset ``calc.py`` to ``return a - b`` outside the application.
#. Ask the agent to make the same fix, then choose **Deny**.
#. Hash the file before and after. It must be byte-identical.
#. Ask for an edit that no longer matches the file, or change the file after the
   approval is shown, and confirm the application reports a stale/conflict error
   without changing bytes.

Record the approval choice, hashes, and error text.

BETA-06 -- Command approval and policy refusal
----------------------------------------------

#. Ask::

       Run this exact command in the project: cmd /c echo beta-command-ok

#. Confirm the approval dialog names the exact command and working directory.
#. Approve it and confirm the output appears.
#. Ask::

       Run this exact command: curl --version

#. Confirm ``curl`` is refused by the safety policy and is **never shown as an
   approvable command**. If an approval dialog appears, choose **Deny**, stop,
   and record a security finding.

Do not substitute a destructive command for this test.

BETA-07 -- Close while approval is pending
------------------------------------------

#. Ask for another edit so an approval dialog is open.
#. Do **not** answer the dialog. Close the main window.
#. Confirm the process exits, the dialog disappears, and the file is unchanged.
#. Relaunch and confirm no unexpected "interrupted task" state causes a crash.

If the application hangs, leaves a dialog, or applies the edit, record ``FAIL``
and stop the session.

BETA-08 -- Outward junction workspace
-------------------------------------

This is a known defect in v0.9.0. The purpose is to record the real user-visible
failure, not to demand a pass.

The source tree now contains a junction-discovery fix, but this plan targets the
published v0.9.0 MSI, which does not include it. Keep the expected failure below
until a new candidate has been built, hashed and tested. See
:doc:`defect-triage`.

#. Create a harmless outside folder and file::

       $outside = Join-Path $env:TEMP "qm-beta-outside"
       New-Item -ItemType Directory -Force -Path $outside | Out-Null
       Set-Content -LiteralPath (Join-Path $outside "outside.txt") `
           -Value "not a secret" -Encoding UTF8

#. Inside the fixture, create a junction::

       cmd /c mklink /J "$env:USERPROFILE\qm-beta\linked-outside" "$outside"

#. Refresh the project panel and ask the agent to summarize the project.
#. Record the scan result. The expected current result is a failed discovery or
   an explicit error, not a usable snapshot.
#. Ask the agent to read ``linked-outside/outside.txt``. The direct read must be
   refused.
#. Remove the junction after the test::

       cmd /c rmdir "$env:USERPROFILE\qm-beta\linked-outside"

Record: error text, whether the UI stays responsive, whether the outside file
was read, and the triage reference to QF-006.

BETA-09 -- Project panel and bounded snapshot
---------------------------------------------

#. Open a larger disposable project, ideally more than 400 files.
#. Refresh the project panel.
#. Confirm the panel shows the project kind, manifests, roots and declared
   checks, or explicitly says it has none.
#. Confirm it states when the file sample is incomplete or truncated. Do not
   infer completeness from a file count.
#. Confirm the planning prompt does not display an absolute user path in the
   transcript or UI.

BETA-10 -- Twenty-turn session
------------------------------

#. In one application session, complete at least 20 turns, including one
   read-only task, one approved benign command, and several edits/denials.
#. Record working set before and after, UI responsiveness, log size, and any
   error dialogs.
#. If the application becomes unusable, record the turn number and stop.

BETA-11 -- Uninstall and profile preservation
---------------------------------------------

#. Record the config, log and data directory paths before uninstall.
#. Uninstall through **Settings > Apps** or the MSI.
#. Confirm the install directory, Start Menu shortcut, desktop shortcut and
   installed-apps entry are gone.
#. Confirm the per-user config/data directory is unchanged, and no process or
   stray window remains.
#. Record whether an uninstall preserved or removed conversations, and whether
   that matches the documentation.

BETA-12 -- 0.9-to-1.0 upgrade and rollback
------------------------------------------

Do not run this against v0.9.0 alone. It requires a v1.0.0 release candidate.

When a candidate exists:

#. Install v0.9.0 and create settings, a conversation and a task with a
   workspace snapshot.
#. Upgrade in place to v1.0.0.
#. Confirm settings, masked credential status, conversations and task evidence
   survive.
#. Uninstall v1.0.0, reinstall v0.9.0, and confirm a documented rollback path.
#. Record schema versions and any migration message.

Evidence record
===============

Use one row per test, per machine. Do not combine results from different
machines.

.. list-table::
   :header-rows: 1
   :widths: 24 76

   * - Field
     - Record
   * - Test ID
     - ``BETA-01`` ... ``BETA-12``
   * - Tester
     - Name or stable pseudonym
   * - Date and time
     - Local time with time zone
   * - Machine
     - Physical or VM; Windows 11 build; architecture; clean or existing profile
   * - Artifact
     - File name and SHA-256
   * - Provider
     - Provider and model **without** the key
   * - Status
     - ``PASS``, ``FAIL``, ``BLOCKED``, ``NOT TESTED``, ``NOT APPLICABLE``
   * - Observation
     - What was seen, including exact error text and the approval choice
   * - Evidence
     - Scrubbed log excerpt, screenshot, or transcript; no key or sensitive path
   * - Triage
     - QF ID for every failure, or "new" with a short description

Do not store full logs in a public issue without reviewing them. Logs contain
paths, prompts and error details.

Exit criteria
=============

The beta is complete when:

#. At least three testers have completed BETA-01 through BETA-07 on at least two
   machines.
#. At least one run used a clean VM or clean profile with no Python and no prior
   install.
#. A real provider completed BETA-03 and BETA-04 with a real tool call and a real
   approval.
#. BETA-05, BETA-06 and BETA-07 pass, or every failure has a triage item and a
   stop-work decision.
#. BETA-08 is recorded as the known QF-006 defect; it must not be reported as a
   pass.
#. BETA-10 has a measured result and an explicit supported session bound.
#. BETA-11 confirms no install residue and no profile corruption.
#. No new blocker or critical issue is open without a written decision.
#. The coordinator has reviewed the evidence and updated
   :doc:`v1.0.0-readiness`.

A beta can close with known major defects, but it cannot close with an
unexplained failure, a lost file, a credential exposure, an approval bypass, or
a test recorded as a pass without evidence.

Stop conditions
===============

Stop the session immediately and preserve the log if any of these occurs:

* a file outside the workspace is read or written;
* a blocked command is offered for approval or executes;
* a credential appears in the UI, transcript, log, or screenshot;
* an approval is applied after **Deny**;
* closing during approval loses data or hangs the process;
* uninstall removes or corrupts the user profile;
* the application crashes while a write is in progress;
* the tester cannot explain what an approval will do.

Report the stop condition to the coordinator before retrying. Do not "try once
more" to see whether it reproduces.

Sign-off
========

.. list-table::
   :header-rows: 1
   :widths: 26 20 54

   * - Role
     - Status
     - Evidence
   * - Beta coordinator
     - PENDING
     - Artifact hash, cohort, evidence completeness, triage review
   * - Tester 1
     - PENDING
     - Completed evidence rows
   * - Tester 2
     - PENDING
     - Completed evidence rows
   * - Tester 3
     - PENDING
     - Completed evidence rows
   * - Maintainer/reviewer
     - PENDING
     - Decision on every open QF item
   * - Release owner
     - PENDING
     - Limited-beta or v1.0.0 decision

Related
========

* :doc:`v1.0.0-readiness`
* :doc:`defect-triage`
* :doc:`../testing/native-gui-acceptance`
* :doc:`../testing/manual-acceptance`
* :doc:`../windows`
