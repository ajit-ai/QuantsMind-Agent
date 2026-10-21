.. _qualification-defect-triage:


=============================
v1.0.0 defect and gap triage
=============================

.. contents::
   :local:
   :depth: 2

This triage covers every issue found while qualifying v0.9.0 for
:doc:`v1.0.0-readiness`. It deliberately includes qualification gaps as well as
product defects: a missing clean-machine result is not a code bug, but it is
still a reason v1.0.0 cannot be approved.

Severity is about the effect on a v1.0.0 decision, not about how difficult a fix
is.

.. list-table::
   :header-rows: 1
   :widths: 14 86

   * - Severity
     - Meaning
   * - Blocker
     - Prevents any public v1.0.0 distribution. Must be closed or the release
       must not ship.
   * - Critical
     - Essential product or evidence risk. Limited beta may proceed only under
       explicit controls; v1.0.0 cannot.
   * - Major
     - Significant defect or missing evidence with a workaround or bounded
       impact. Must be closed or formally accepted before v1.0.0.
   * - Minor
     - Limited impact, but it should be tracked and either fixed or bounded.
   * - Informational
     - Observation with no demonstrated user impact. Record it so it is not
       rediscovered as a surprise.

Summary
========

.. list-table::
   :header-rows: 1
   :widths: 7 27 12 38 16

   * - ID
     - Item
     - Severity
     - Effect on v1.0.0
     - Status
   * - QF-001
     - Artifact and installer are unsigned
     - Blocker
     - No trusted publisher identity; SmartScreen and security software can
       block the release.
     - Open
   * - QF-002
     - No real-provider or real local-endpoint qualification
     - Critical
     - The core model path has only scripted and stub evidence.
     - Open -- blocked
   * - QF-003
     - No human desktop or approval-dialog acceptance
     - Critical
     - Rendering, approval comprehension and close-during-approval are
       unverified.
     - Open -- blocked
   * - QF-004
     - No clean-machine independent install
     - Critical
     - Installer evidence is same-user and build-host only.
     - Open -- blocked
   * - QF-005
     - No external beta evidence
     - Critical
     - There are no field reports from independent testers.
     - Open -- blocked
   * - QF-006
     - Outward junction breaks workspace discovery
     - Major
     - Affected projects cannot refresh or plan from the project snapshot.
     - Fixed in source -- artifact retest pending
   * - QF-007
     - Product and security documentation are stale
     - Major
     - Testers were told the product is read-only and still at v0.1/v0.2.
     - Fixed in source -- strict docs build passed
   * - QF-008
     - Release-commit CI is not green
     - Major
     - Retired runner and stale reminders fixed in source; no run yet.
     - Open -- external
   * - QF-009
     - 0.9-to-1.0 upgrade and rollback untested
     - Major
     - Configuration, task and conversation migration is unproven.
     - Open -- no candidate
   * - QF-010
     - Long-session resource behaviour untested
     - Minor
     - Twenty-turn memory and responsiveness remain unknown.
     - Open -- not tested
   * - QF-011
     - Failed turns retain ``stopped_reason="final_answer"``
     - Informational
     - Internal semantic inconsistency; no user-visible impact observed.
     - Open
   * - QF-012
     - Test-only pytest deprecation warning
     - Informational
     - Future pytest compatibility, not a product failure.
     - Open

QF-001 -- Unsigned artifacts
============================

**Severity:** blocker for public v1.0.0.

Evidence
--------

* ``Get-AuthenticodeSignature`` reports ``NotSigned`` for both
  ``QuantsMindAgent-0.9.0-x64.msi`` and the installed
  ``QuantsMindAgent.exe``. There is no signer certificate or timestamp.
* ``packaging/v0.9.0/BUILD-METADATA.json`` records
  ``installer.signed: false``.
* No production signing certificate, HSM-backed key, or trusted-publishing
  credential exists in the repository or on the build host.
* ``scripts/build_msi.py`` can discover a certificate, but the release build does
  not sign.
* ``packaging/WINDOWS-SIGNING.md`` and
  ``scripts/verify_windows_signature.ps1`` exist only on
  ``release-engineering/post-v0.8.0`` at ``aede81b``. They are unmerged, and a
  verifier without a certificate cannot create trust.

Impact
------

The published v0.9.0 alpha is intentionally unsigned, so this is not a v0.9.0
regression. It is still a v1.0.0 blocker: a user cannot establish publisher
identity from the file, Windows SmartScreen will warn, and security software may
quarantine the installer or executable.

A SHA-256 published over a trusted channel detects accidental substitution. It
does not authenticate the publisher and is not a substitute for a code signature.

Required action
---------------

* Obtain a real code-signing identity appropriate for public distribution.
* Sign ``QuantsMindAgent.exe`` before building the MSI; sign the MSI with a
  trusted timestamp.
* Verify both with ``signtool verify /pa`` in the release pipeline and fail
  closed if the signature is absent, invalid, expired, or from the wrong
  subject.
* Record the signer subject and timestamp in the release metadata.
* Do not use a self-signed certificate as release evidence.
* Do not re-sign or replace v0.9.0. Publish a new version, tag and artifact.
* Stop the release if no trusted signing path is available.
* Test on a clean machine that SmartScreen and the security product in use show
  the expected publisher rather than an unknown one.

Close-out
---------

A clean-machine run shows a valid signature and trusted publisher for the MSI and
installed executable, and the release record contains the verification result.
Maps to gate G25.

QF-002 -- No real-provider or real local-endpoint qualification
===============================================================

**Severity:** critical evidence gap.

Evidence
--------

* The v0.9.0 assessment made no request to a real cloud provider.
* No Ollama, llama.cpp, LM Studio, vLLM or other real local endpoint was used.
* Provider evidence consists of the offline suite's scripted provider and a
  temporary local HTTP stub. The stub exercised a normal reply, 401, 429, 500,
  timeout and malformed JSON.
* The stub proves request construction and error normalisation. It does not prove
  provider API compatibility, model tool selection, streaming cadence, quota
  behaviour, or provider-specific error bodies.

Impact
------

The application's core claim is that it connects a real model to a local project
through controlled tools. That path has not been exercised against a real
endpoint in this assessment. A qualified v1.0.0 cannot rest on scripted model
responses alone.

Required action
---------------

* Use a dedicated, low-limit, revocable test credential. Enter it through
  Settings, never in a command line or a report.
* Run a read-only task and a plan/edit/approval task on the disposable fixture.
* Confirm tool activity appears before the answer and that claims match the
  fixture files.
* Test one wrong-key request and one forced timeout or rate-limit path where the
  provider permits it.
* Record the provider, model, date, result and a scrubbed log excerpt.
* Optionally repeat with a local endpoint and no key to cover gate G17.

Close-out
---------

At least one real cloud provider completes a normal read-only turn and one
approval-gated turn without a key leak, with reproducible evidence. A local
endpoint is separately recorded as pass, fail, blocked or not tested. Maps to
gates G16 and G17.

QF-003 -- No human desktop or approval-dialog acceptance
=========================================================

**Severity:** critical evidence gap.

Evidence
--------

* The packaged GUI acceptance passed 8/8, but it only proves that a titled
  window exists, the Chat and Task views are present, and Run/Approve are
  disabled before approval.
* It does not prove what is drawn inside the window, whether text is clipped,
  whether controls are reachable by keyboard, or whether an approval dialog is
  understandable.
* No person answered Allow, Allow Once, Deny, or Allow for Session.
* Closing the window while an approval is pending has not been exercised by a
  human or a live model turn.

Impact
------

Approval is the product's main safety boundary. Automated tests can verify the
state machine, but not that a person can see what is being approved. A GUI that
is technically present but unreadable is not acceptable for general use.

Required action
---------------

* Execute the checks in :doc:`../testing/native-gui-acceptance` on a real
  Windows 11 desktop.
* Include close-while-approval-pending, high-DPI or scaled display, and keyboard
  navigation if those are in the intended v1.0.0 support scope.
* Capture screenshots and observations without any credential or sensitive path.
* Use the beta plan in :doc:`manual-beta-acceptance` for independent testers.

Close-out
---------

A named human reviewer records each required check as pass or fail on a real
desktop, including at least one Allow and one Deny, and the pending-approval
close path. Maps to gate G18.

QF-004 -- No clean-machine independent install
==============================================

**Severity:** critical evidence gap.

Evidence
--------

* The MSI lifecycle passed 23/23, including install, launch, reinstall, uninstall
  and a byte-identical real user profile.
* The test ran on the build host, under the same Windows user, with Python, the
  source tree, Qt build dependencies and the user's credential store already
  present.
* Windows Sandbox was not available, and no clean VM or second machine was
  supplied.

Impact
------

Same-host success does not prove that a machine without Python or the build
toolchain can install and start the bundle. It also does not reproduce the
genuine first-run SmartScreen experience for an unsigned download.

Required action
---------------

* Use a clean Windows 11 x64 VM or a separate machine with no Python, no source
  checkout, no prior QuantsMind-Agent install, and no test credential.
* Verify the MSI hash before installation.
* Install, launch, open a disposable workspace, close, uninstall, and verify no
  shortcuts, processes, install directory or installed-apps entry remain.
* Record the SmartScreen or security-product outcome for the unsigned build.

Close-out
---------

The complete lifecycle is reproduced on a clean machine by someone other than
the build operator. Maps to gate G19.

QF-005 -- No external beta evidence
===================================

**Severity:** critical evidence gap for v1.0.0.

Evidence
--------

* All evidence in this assessment was produced on one build workstation.
* No independent tester, field report, or beta questionnaire exists.
* The beta plan is written but unexecuted.

Impact
------

Internal and automated tests share assumptions with the implementation. External
testers are the first realistic check for confusing approval text, unexpected
security-software behaviour, project layouts, and recovery after mistakes.

Required action
---------------

* Run :doc:`manual-beta-acceptance` with at least three technical testers on at
  least two machines, using disposable projects and revocable keys.
* Triage every failure and record which build and hash produced it.
* Do not count a test as passed if the tester only repeated the automated checks.

Close-out
---------

A completed beta matrix with independent evidence, no unresolved blocker or
critical data-safety issue, and a written decision on every failure. Maps to
gate G20.

QF-006 -- Outward junction breaks workspace discovery
=====================================================

**Severity:** major product defect.

Evidence
--------

A workspace containing a directory junction to a directory outside the workspace
reproduced the failure in two paths:

* ``Workspace.walk(".")`` raised ``InvalidPathError`` for the outside target;
* ``discover_workspace()`` failed for the same workspace;
* a direct read through the junction was correctly refused as outside the
  workspace.

The GUI scan worker catches the exception and emits a failure signal, so the
likely user-visible result is a failed project scan rather than a process crash
or a data read. The exact error observed was::

    Path rejected: '...\escape\outside-secret.txt'
    (path is outside the workspace root)

The three Windows symlink tests are skipped because symlinks are unavailable on
the test host. That is a coverage gap for this class of path, not evidence that
the junction path is safe.

Impact
------

A project with an outward junction cannot produce a workspace snapshot. Planning
and the Task panel's project summary then lose the bounded project context. The
failure is not a data escape, but it is a functional failure for affected
projects, and junctions occur in ordinary Windows tooling and dependency
layouts.

Required action
---------------

* Decide the traversal policy for reparse points: skip them, record them as
  explicitly excluded unknowns, or reject the workspace with a clear message.
* Never follow an outward junction during discovery.
* Add a Windows regression test that creates a junction, scans the workspace,
  confirms the outer file is not read, and confirms the snapshot remains usable
  or explicitly incomplete.
* Keep the direct-read refusal unchanged.

Close-out
---------

A junction-containing workspace no longer raises an unhandled discovery error;
the result states what was skipped; direct reads outside the root remain refused.
Maps to gate G23.

Post-qualification remediation (2026-10-20)
-------------------------------------------

Status: **fixed in source; published v0.9.0 remains affected; release retest
pending.**

Root cause: ``os.walk(..., followlinks=False)`` does not suppress Windows
directory junctions. The walk descended the junction, then the containment check
on the resolved outside file raised ``InvalidPathError`` and aborted discovery.

The fix applies one policy to listing and discovery:

* symbolic links, junctions and other filesystem reparse points are never
  followed or listed;
* ``Workspace.walk`` can return their workspace-relative paths as excluded
  entries;
* ``discover_workspace`` records those paths in ``unknowns``, so the snapshot is
  usable without claiming complete coverage;
* direct reads through an outward link are still refused.

Verification used a real Windows junction to an outside folder. ``list_dir`` and
``walk`` returned only the safe entries; the snapshot included the safe source
file and reported ``1 filesystem link(s) were not followed``; the outside file
was not read; ``read_text("linked-outside/secret.py")`` raised
``InvalidPathError``.

Regression coverage:

* ``tests/unit/test_workspace.py::TestJunctions`` -- listing, walking and
  direct-read refusal;
* ``tests/unit/test_workspace_intel.py::TestLinks`` -- snapshot exclusion and
  unknown reporting;
* fixture ``outward_junction_project`` in ``tests/conftest.py`` creates the
  junction with ``mklink /J`` and removes it after the test.

Verification results: focused workspace/intelligence tests ``86 passed, 1
skipped``; workspace integration and Task panel tests ``23 passed``; full suite
``1713 passed, 3 skipped, 1 warning``. The skip is the existing Windows symlink
test. The full suite used an explicit ``--basetemp`` because the host's default
pytest temporary root returned ``WinError 5``.

This does not change the G23 result for the published v0.9.0 MSI. No replacement
artifact was built. The next release candidate must repeat the junction test
against its exact hash.

QF-007 -- Stale product and security documentation
==================================================

**Severity:** major documentation defect.

Evidence
--------

At the assessment cut-off:

* ``docs/source/index.rst`` says "Version 0.2 is a controlled coding agent".
* ``docs/source/security.rst`` says filesystem access is read-only, command
  execution is not implemented, and only ``READ_WORKSPACE`` is used.
* ``docs/source/testing/manual-acceptance.rst`` still calls itself "the release
  gate for v0.1" and describes v0.1's no-write/no-command behaviour.
* The README also describes v0.2 capabilities while the application reports
  0.9.0.

These pages are not merely incomplete. They contradict the shipped behaviour:
v0.9.0 has approved writes, edits, deletes, commands, Git read tools and human
approval.

Impact
------

A tester who follows the current security page can form an unsafe expectation
about what the agent is allowed to do. A tester who follows the manual gate will
not exercise writes, commands or the approval dialog at all.

Required action
---------------

* Update the index version and short description.
* Rewrite the security summary and permission/command sections for the shipped
  capability set.
* Replace or supersede the v0.1 manual gate with the beta plan.
* Link this qualification section from the documentation index.
* Search for stale phrases such as "Version 0.2", "not implemented", and
  "release gate for v0.1" before the v1.0.0 docs build.

Close-out
---------

A user can read the index, security page and acceptance page and reach the same
understanding as the shipped application. Maps to gate G24.

Post-qualification remediation (2026-10-21)
-------------------------------------------

Status: **fixed in source; strict documentation build passed; not committed or
published.**

The current-facing pages were corrected: README, index, security summary,
security boundaries, getting-started, tools, permissions, conversation,
configuration, manual acceptance, Windows, installation, packaging, workspace
and command-execution notes. The manual acceptance page now points to
:doc:`manual-beta-acceptance` as the gate and is limited to a local smoke test.
The v0.1 Windows checklist was retained under a historical heading so the
historical release notes and acceptance report still resolve.

The changes also close two additional contradictions found during the search:
configuration/session state now describes the persisted session, and workspace
tool documentation no longer claims that writes are denied or that only four
tools exist.

Verification: ``python scripts/build_docs.py --clean`` exited 0 with warnings
treated as errors; ``normalize_rst.py --check`` reported zero issues. The
corrected HTML is local and was not deployed. The original G24 FAIL for the
published artifact and the historical gate table are unchanged.

QF-008 -- Release-commit CI is not green
========================================

**Severity:** major evidence gap.

Evidence
--------

* The v0.9.0 package workflow run ``39428022100`` remained queued at the evidence
  cut-off.
* A ``diagnose-gui-abort`` run on ``main`` completed with failure.
* The known CPython 3.11 GUI abort is documented in :doc:`../known-issues`; it is
  not suppressed or automatically retried.

Impact
------

There is no clean, independent CI result for the release commit. Local success
does not replace a clean runner, and a queued job is not a pass.

Required action
---------------

* Requeue or rerun the package workflow for the exact release commit and wait
  for completion.
* Investigate the GUI diagnostic failure rather than hiding it with retries.
* Require a green result on the commit that will become v1.0.0, not on an earlier
  or later tree.

Close-out
---------

A green CI run on the release commit, with the run URL recorded in the v1.0.0
evidence. Maps to gate G21.

Post-qualification audit (2026-10-21)
-------------------------------------

The workflow audit found one guaranteed failure and stale acceptance guidance:

* ``.github/workflows/package.yml`` used ``macos-13``, a retired runner. It now
  uses ``macos-15-intel``, matching the unmerged fix at ``555bd93``.
* ``.github/workflows/ci.yml`` still told testers to record v0.2 results in the
  v0.1 report. The reminders now point at
  :doc:`manual-beta-acceptance`.
* The unmerged branch also adds packaging-record validation. It was not merged
  into this working tree.

No workflow was dispatched after these edits. The G21 result remains open; a
source fix is not a green run.

QF-009 -- 0.9-to-1.0 upgrade and rollback untested
===================================================

**Severity:** major evidence gap.

Evidence
--------

* The MSI reinstall path passed, but it installed the same 0.9.0 build over
  itself.
* No v1.0.0 candidate exists, so no cross-version upgrade was attempted.
* Configuration, conversation, task and snapshot schema compatibility across the
  release boundary is therefore unproven.

Impact
------

A v1.0.0 installer that loses settings, conversations or task evidence is a data
loss defect. A rollback that leaves an unusable profile is also a support
burden.

Required action
---------------

* Build a v1.0.0 release candidate.
* Install v0.9.0, create a workspace, conversation and task evidence, then
  upgrade in place.
* Verify settings, credentials (without exposing them), conversation history,
  task snapshots and schema versions survive.
* Uninstall v1.0.0, reinstall v0.9.0, and verify a documented rollback path.

Close-out
---------

The upgrade and rollback matrix passes on a clean machine, including schema
migrations. Maps to gate G22.

QF-010 -- Long-session resource behaviour untested
===================================================

**Severity:** minor, but it must be bounded before a general release.

Evidence
--------

* The full suite and packaged checks are short-lived.
* No 20-turn session, long-running command, or large-log scenario was measured
  during this assessment.

Impact
------

Memory growth, UI responsiveness and log rotation under sustained use are
unknown. This is not evidence of a defect, but it is also not evidence of
stability.

Required action
---------------

* Run at least 20 turns in one session on a disposable project, including a
  long-running command and several approvals.
* Record working set, UI responsiveness, log size and process count.
* If 20 turns cannot be supported, state the tested limit in the beta plan and
  user documentation.

Close-out
---------

A measured result with an explicit supported session bound. Maps to gate G26.

QF-011 -- Failed turns retain ``stopped_reason="final_answer"``
================================================================

**Severity:** informational.

Evidence
--------

A stub 401, 429, 500, timeout and malformed-response turn each set
``AgentOutcome.error`` to the correct user message. ``stopped_reason`` remained
the dataclass default ``"final_answer"`` for every failure.

``AgentOutcome.describe()`` prioritises ``error``, and no current caller was
found that uses ``stopped_reason`` to classify a failed turn. No user-visible
impact was observed.

Impact
------

A future caller could misclassify an error as a completed answer if it reads
``stopped_reason`` without checking ``error``. The current UI does not.

Required action
---------------

* Either set a distinct reason on the error path or document that ``error`` is
  authoritative.
* Add a contract test if the field is used outside the agent.

Close-out
---------

The semantic is explicit and tested. No production fix is required before beta.

QF-012 -- Test-only pytest deprecation warning
===============================================

**Severity:** informational.

Evidence
--------

The full suite reports one ``PytestRemovedIn10Warning`` for a class-scoped
fixture method in
``tests/unit/test_platform_packaging.py::TestDebianControl``.

Impact
------

No current product impact. The test may stop working on a future pytest major
version.

Required action
---------------

Convert the fixture to a form supported by the pytest release currently pinned
for development, then rerun the suite.

Close-out
---------

The suite is warning-clean or the remaining warning has a documented,
time-bounded exception.

Not treated as defects
======================

The following were checked and are not product defects:

* **``--config-dir`` does not isolate the OS credential store.** The isolated
  first-run check used a null keyring backend so an existing Windows Credential
  Manager entry could not make the no-key test pass falsely. On a clean VM this
  is not an issue.
* **Same-version reinstall is not an upgrade test.** It proves repair and
  duplicate avoidance, not cross-version migration.
* **A queued CI run is not a pass.** It is recorded as ``BLOCKED`` for the
  release signal until it completes.
* **Unsigned v0.9.0 is a published alpha, not a regression.** It is still a
  v1.0.0 blocker.
* **A local stub success is not real-provider evidence.** It is recorded as
  level C evidence only.

Gate mapping
============

.. list-table::
   :header-rows: 1
   :widths: 12 58 30

   * - Item
     - Gate
     - Effect
   * - QF-001
     - G25 -- signing and trusted publisher
     - Blocks public v1.0.0
   * - QF-002
     - G16, G17 -- real provider and local endpoint
     - Blocks v1.0.0; bounded beta only
   * - QF-003
     - G18 -- human desktop and approval
     - Blocks v1.0.0; bounded beta only
   * - QF-004
     - G19 -- clean-machine install
     - Blocks v1.0.0; bounded beta only
   * - QF-005
     - G20 -- external beta
     - Blocks v1.0.0; bounded beta only
   * - QF-006
     - G23 -- junction workspace
     - Must be fixed or explicitly excluded from support
   * - QF-007
     - G24 -- documentation accuracy
     - Must be fixed before v1.0.0
   * - QF-008
     - G21 -- CI green
     - Must be green before v1.0.0
   * - QF-009
     - G22 -- upgrade and rollback
     - Must pass before v1.0.0
   * - QF-010
     - G26 -- long session
     - Bound the claim before v1.0.0
   * - QF-011
     - none
     - Record and clarify; no beta blocker
   * - QF-012
     - none
     - Test maintenance; no beta blocker

Release decision rule
=====================

A limited beta is allowed only while all blockers and critical gaps are handled
as controls, not described as passes. A v1.0.0 release is allowed only when:

#. QF-001 is closed;
#. QF-002, QF-003, QF-004 and QF-005 have recorded evidence;
#. QF-006, QF-007, QF-008 and QF-009 are closed or explicitly accepted in
   writing with a bounded support statement;
#. QF-010 has a measured bound;
#. the updated :doc:`v1.0.0-readiness` gate table contains no ``FAIL`` or
   ``BLOCKED`` row for an in-scope Windows 11 x64 claim;
#. the full suite, packaged smoke, GUI acceptance and installer lifecycle are
   rerun against the exact release candidate and hash.

Related
========

* :doc:`v1.0.0-readiness`
* :doc:`manual-beta-acceptance`
* :doc:`../known-issues`
* :doc:`../testing/native-gui-acceptance`
* :doc:`../testing/platform-acceptance-matrix`
