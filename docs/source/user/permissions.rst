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

v0.1 defines five capabilities:

===================  ============================================
Permission           Meaning
===================  ============================================
``READ_WORKSPACE``   Read files inside the opened project.
``WRITE_WORKSPACE``  Create or modify files in the project.
``EXECUTE_COMMAND``  Run a shell or process.
``GIT``              Run git commands or write to the repository.
``NETWORK``          Fetch arbitrary URLs.
===================  ============================================

Only ``READ_WORKSPACE`` is actively used in v0.1. The others exist so v0.2 can
add tools without changing call sites -- see
:doc:`../architecture/permissions`.

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
    WRITE_WORKSPACE   -> DENY
    EXECUTE_COMMAND   -> DENY
    GIT               -> DENY
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
