.. _architecture-permissions:


===========
Permissions
===========


The permission layer answers one question: *may this capability be used right
now?* It is deliberately tiny, and it is the only thing standing between a
misbehaving model and your disk.

Design
======

.. code-block:: text

    tool call
       |
       v
    PermissionRequest(permission, tool, summary, target, details)
       |
       v
    PermissionProvider.decide(request)  ->  ALLOW | DENY | ASK
       |
       +-- ASK ---> approver callback ---> ALLOW | DENY
       |                        (no approver -> DENY)
       v
    Decision + audit entry
       |
       +-- DENY -> ToolResult.failure, reported to the model
       +-- ALLOW -> tool.run()

Components
==========

``Permission``
    The taxonomy: ``READ_WORKSPACE``, ``WRITE_WORKSPACE``, ``EXECUTE_COMMAND``,
    ``GIT``, ``NETWORK``.

``PermissionRequest``
    An immutable record of a specific capability use, including the target path.

``PermissionProvider``
    A protocol with one method, ``decide(request) -> Decision``. Implement it to
    drive policy from anywhere: a config file, a UI, an enterprise server.

``StaticPermissionProvider``
    The shipped implementation. An ordered rule list, evaluated in first-match
    order, preferring rules that name the exact tool and target.

``PermissionBroker``
    The choke point. Escalates ``ASK``, degrades it to ``DENY`` without an
    approver, records the decision, and raises
    :class:`~quantsmind_agent.errors.PermissionDeniedError` from
    :meth:`~PermissionBroker.require`.

Rule matching
=============

.. code-block:: python

    PermissionRule(permission, decision, tool="*", target_pattern="*")

A rule applies when all of these hold:

* ``rule.permission is request.permission``;
* ``rule.tool`` is ``"*"`` or equals ``request.tool``;
* ``rule.target_pattern`` is ``"*"``, equals ``request.target``, or matches it
  with :func:`fnmatch.fnmatch`.

Rules are sorted so exact-tool, exact-target rules win over wildcards, which
makes later additions predictable instead of order-dependent.

.. code-block:: python

    provider.add_rule(PermissionRule(READ_WORKSPACE, DENY))
    provider.add_rule(PermissionRule(READ_WORKSPACE, ALLOW, tool="read_file"))

    provider.decide(request_for("read_file"))     # ALLOW
    provider.decide(request_for("search_content")) # DENY

Default policy
==============

:code:`StaticPermissionProvider.for_v0_1()` returns:

.. code-block:: text

    READ_WORKSPACE    ALLOW
    WRITE_WORKSPACE   DENY
    EXECUTE_COMMAND   DENY
    GIT               DENY
    NETWORK           DENY

Anything not matched is ``DENY``. The policy is deny-by-default, which is the
property that matters.

The application can tighten it: with ``agent.auto_approve_reads = false`` the
context adds a ``READ_WORKSPACE -> ASK`` rule, and since the v0.1 GUI installs
no approver, reads fail closed.

Why ASK degrades to DENY
========================

An ``ASK`` that silently succeeds is worse than no permission system: the
operator believes they are in control while the agent proceeds. So when no
approver is wired up, ``ASK`` becomes ``DENY``. The alternative -- blocking on a
prompt that never appears -- would hang the agent.

Wiring an approver (v0.2):

.. code-block:: python

    broker.set_approver(
        lambda request: QMessageBox.question(
            None, "Allow?",
            f"{request.tool} wants {request.permission.value} on {request.target}",
        ) == QMessageBox.Yes
    )

Because the approver runs on the GUI thread (or must marshal to it), v0.2 will
need an explicit thread hand-off for approvals raised from the worker thread.

Audit
=====

.. code-block:: python

    broker.history            # [(request, decision), ...]
    broker.on_decision        # callback for live display

Decisions are also published as ``permission_decision`` events, which is what
drives the activity panel line ``permission ALLOW: read_file``.

Testing without weakening the policy
====================================

.. code-block:: python

    provider = StaticPermissionProvider()
    for permission in Permission:
        provider.add_rule(PermissionRule(permission, Decision.ALLOW))

    registry = ToolRegistry(permissions=PermissionBroker(provider))

This is how the test suite exercises tools. Production defaults are never
loosened to make a test pass.

Related
=======

* :doc:`../user/permissions`
* :doc:`security-boundaries`
* :doc:`../security`
