.. _user-permissions:


===========
Permissions
===========


Every tool declares a permission. Nothing executes until
:class:`~quantsmind_agent.permissions.PermissionBroker` returns ``ALLOW``.
Tools never check permissions themselves, so a mis-annotated tool cannot bypass
policy.

The taxonomy
============

Five capabilities:

===================  ================================================
Permission           Meaning
===================  ================================================
``READ_WORKSPACE``   Read files inside the opened project.
``WRITE_WORKSPACE``  Create, edit or delete files in the project.
``EXECUTE_COMMAND``  Run a command on this computer.
``GIT``              Inspect repository state (read-only tools only).
``NETWORK``          Fetch arbitrary URLs. Denied in v0.2.
===================  ================================================

Since v0.2, ``WRITE_WORKSPACE``, ``EXECUTE_COMMAND`` and ``GIT`` are in active
use. The shipped policy is:

===================  ========  =================================
Permission           Decision  Notes
===================  ========  =================================
``READ_WORKSPACE``   ALLOW     Reads change nothing.
``WRITE_WORKSPACE``  ASK       Every write is approved.
``EXECUTE_COMMAND``  ASK       Always approved, never automatic.
``GIT``              ALLOW     Only read-only tools exist.
``NETWORK``          DENY      No network tool in v0.2.
===================  ========  =================================

Two operations keep asking no matter what, because both are irreversible or
host-affecting: ``delete_file`` and ``run_command``. Turning on
``auto_approve_writes`` in :doc:`../configuration` deliberately cannot widen
them.

Asking a human
==============

When the policy returns ``ASK``, the application shows a prompt and waits. The
prompt shows the operation, the tool, the target, the workspace, the arguments,
the reason the model gave, and the risk classification.

==================  =======================================================
Choice              Effect
==================  =======================================================
Allow / Allow Once  Runs this one call only.
Allow for Session   Allows later calls of the same tool, until the
                     conversation, project or settings change. Never offered
                     for ``delete_file``.
Deny                Does not run. The model is told and the turn
                     continues.
==================  =======================================================

Pressing **Stop**, closing the window or pressing Escape all **deny**. There is
no path from the prompt to "run it anyway" other than an explicit Allow, and a
denied tool never changes the filesystem.

A denial is not a dead end: the tool returns a failure, the agent sees it and
can try a different approach.

.. note::

   A command the safety policy forbids never reaches this prompt at all. The
   policy is a separate, non-overridable gate -- see
   :doc:`../architecture/command-execution`.

Decisions
=========

.. code-block:: text

    ALLOW   run it
    DENY    refuse, report the refusal to the model
    ASK     ask the user; with no approver wired up, ASK becomes DENY

That last rule is the important safety property: **nothing dangerous ever runs
unattended.** A GUI that has not wired an approver treats ``ASK`` as ``DENY``.

Default policy
==============

.. code-block:: text

    READ_WORKSPACE    -> ALLOW
    WRITE_WORKSPACE   -> ASK      (ALLOW only if auto_approve_writes)
    EXECUTE_COMMAND   -> ASK      (never automatic)
    GIT               -> ALLOW    (read-only tools only)
    NETWORK           -> DENY

Rules are evaluated in first-match order, preferring rules that name the exact
tool and an exact target over wildcard rules:

.. code-block:: python

    from quantsmind_agent.permissions import (
        Decision, Permission, PermissionRule, StaticPermissionProvider,
    )

    provider = StaticPermissionProvider()
    for rule in provider.default_rules():
        provider.add_rule(rule)

    # Reads under src/ are always allowed; anything else must be approved.
    provider.add_rule(
        PermissionRule(Permission.READ_WORKSPACE, Decision.ALLOW, target_pattern="src/*")
    )

Interactive approval
====================

The broker accepts an approver callback:

.. code-block:: python

    broker.set_approver(lambda request: ask_the_user(request.summary))

In the v0.1 GUI no approver is installed, so every ``ASK`` degrades to ``DENY``.
Turning off **Allow workspace reads without prompting** in Settings therefore
makes reads fail closed.

What the user sees
==================

* Each decision is published as a ``permission_decision`` agent event.
* The activity panel shows ``permission ALLOW: read_file`` (or ``DENY``).
* A denial is reported to the model as an observation, so it can explain itself
  rather than failing silently.

Audit trail
===========

.. code-block:: python

    for request, decision in broker.history:
        print(decision.value, request.permission.value, request.tool, request.target)

The broker records every decision for the current session, which is what makes
the activity panel trustworthy.

Related
=======

* :doc:`../architecture/permissions`
* :doc:`../security`
* :doc:`tools`
