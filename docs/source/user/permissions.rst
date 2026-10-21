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
``NETWORK``          Fetch arbitrary URLs. No built-in tool; denied.
===================  ================================================

In v0.9.0, ``WRITE_WORKSPACE``, ``EXECUTE_COMMAND`` and ``GIT`` are in active
use. The shipped policy is:

===================  ========  ================================
Permission           Decision  Notes
===================  ========  ================================
``READ_WORKSPACE``   ALLOW     Reads change nothing.
``WRITE_WORKSPACE``  ASK       Writes and edits ask by default.
``EXECUTE_COMMAND``  ASK       Always asks, never automatic.
``GIT``              ALLOW     Only read-only tools exist.
``NETWORK``          DENY      No built-in network tool.
===================  ========  ================================

Three operations keep asking no matter what, because they are irreversible or
host-affecting: ``delete_file``, ``undo_file`` and ``run_command``. Turning on
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
                     for ``delete_file``, ``undo_file`` or ``run_command``.
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

Threading
=========

The agent runs on a worker thread; the prompt is a Qt dialog on the GUI thread.
Two separate handovers are involved, and both must be marshalled:

**The prompt itself.** It crosses from the worker thread via a Qt signal. The
receiving slot is a real ``@Slot(object)`` and the connection is an explicit
``Qt.ConnectionType.QueuedConnection``. Qt's default ``AutoConnection`` already
queues correctly here, so the decorator is belt and braces rather than the fix
for a defect -- but both are pinned by tests so neither can be lost silently.

**Agent events.** :class:`~quantsmind_agent.events.EventBus` is a plain Python
callback list with **no Qt involvement**: a subscriber runs on whichever thread
published the event, which is usually the agent's worker. The window therefore
only *enqueues* events and drains them on a GUI-thread timer, because the
handler writes to the activity panel and the status bar. Mutating those widgets
from the worker is undefined behaviour and shows up as an unstable or frozen
window -- and it fires precisely while an approval is pending, since
``permission_decision`` and ``permission_approval`` are published during the
decision.

Every failure path answers. If the dialog cannot be built, or a request arrives
off-thread, or one is already pending, the request is **denied** and the waiting
worker is released. A blank dialog, a timeout, a cancellation or a GUI
exception never authorises anything.

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

The packaged GUI installs an approver, so an ``ASK`` opens the approval dialog.
A headless run or a custom embedding that does not install one makes every
``ASK`` degrade to ``DENY``. Turning off **Allow workspace reads without
prompting** in Settings therefore makes reads fail closed when no approver is
available.

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
