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
Manager**. If no credential service is reachable (some service accounts, locked
sessions), the store degrades to ``secrets.json`` with owner-only permissions.
The application never fails to start because of this.

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

Long paths
----------

QuantsMind-Agent relies on the long-path behaviour of the Python runtime. Very
deep trees (beyond the classic 260-character limit) require the Windows
long-paths policy to be enabled; the default install is sufficient for normal
projects.

Manual validation procedure
===========================

This procedure is the *Windows 11 acceptance* gate. It is manual by design: it
requires a real desktop, a real API key and a real model. Run it once per
release candidate; record the outcome in
:doc:`testing/acceptance-v0.1`.

.. _windows-manual-checklist:

Checklist
---------

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

Detailed steps
--------------

1. **Launch** and confirm the window appears within a few seconds::

       quantsmind-agent

2. **Confirm the empty state.** The transcript reads *Open a project folder
   first.*

3. **Open a project.** Click **Open project…**, select any small repository,
   for example::

       python scripts\make_acceptance_fixture.py
       quantsmind-agent --workspace .acceptance-fixture\widget-factory

   The sidebar shows the resolved absolute path.

4. **Confirm the status bar** shows the model and the workspace.

5. **Open Settings** (``Ctrl+K``). Confirm the model name, the base URL, and
   that the API key field is empty with masked status text such as
   ``sk-******op``.

6. **Ask a question** that requires orientation::

       Explain the structure of this project.

7. **Confirm tool activity.** The *Tool activity* panel lists
   ``project_structure`` (or ``list_directory``) with a state marker.

8. **Confirm streaming.** Text appears progressively in the transcript rather
   than all at once.

9. **Ask a tool-driven question**::

       Read the README and summarize the project.

10. **Confirm ``read_file``** appears in the activity panel before the answer.

11. **Confirm no credential leaks.** Search the transcript, the Settings dialog
    and ``%LOCALAPPDATA%\QuantsMind-Agent\QuantsMind-Agent\logs\quantsmind-agent.log``
    for the first and last three characters of your key. Neither may appear.

12. **Cancel a request.** Press **Stop** while a request is running. The window
    stays responsive and the transcript reports the cancellation. Note the
    documented limitation: an HTTP request already in flight is not aborted, but
    its answer is discarded.

13. **Exit.** Close the window. The process terminates with code ``0`` and no
    orphaned ``python`` process remains.

Known Windows limitations
=========================

* The packaged executable has no Start-menu entry or file association in v0.1.
* Long paths beyond 260 characters need the Windows long-paths policy.
* The credential store falls back to ``secrets.json`` when Credential Manager is
  unavailable.
* Sockets and TLS use ``httpx``'s bundled ``certifi`` bundle, not the Windows
  certificate store, so a corporate proxy with a private CA needs
  ``REQUESTS_CA_BUNDLE``-style configuration at the environment level.
