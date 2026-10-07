.. _adr-0004:

================================================
ADR-0004: The workspace is the security boundary
================================================

Status
    Accepted (v0.1)

Context
-------

The agent reads real files on a developer's machine, driven by text a language
model produced. Any mistake -- in our validation, in a tool, or in the model's
output -- could otherwise read a credential or a private file.

The threat is not hypothetical: models are asked to read ``~/.aws/credentials``
often enough that assuming they will stay inside the project is not reasonable.

Options considered
------------------

* **No boundary; pass paths through.** Simplest code, unacceptable exposure.
* **Allow-list of directories.** Restricts the agent to fixed roots; cannot
  follow the user to an arbitrary project folder.
* **A sandbox process with restricted ACLs.** Strong, but heavy: process
  management, credential plumbing, and platform-specific setup -- a large amount
  of machinery before the first working agent loop.
* **A workspace object with path containment.** One choke point, unit-testable,
  no extra process.

Decision
--------

Introduce :class:`~quantsmind_agent.workspace.Workspace` as the only object
permitted to touch user files, and require every tool path argument to resolve
through it.

Containment is checked on the **resolved** path, not the textual one:

.. code-block:: text

    requested:  src/link/secret.txt
    resolved:   C:\Users\someone\secret.txt   -> outside root -> refused
    requested:  ../../etc/passwd               -> ".."            -> refused
    requested:  C:\Windows\win.ini             -> drive letter    -> refused
    requested:  /etc/passwd                    -> absolute        -> refused
    requested:  ~/notes.md                     -> home reference  -> refused

Both separators are accepted, and surrounding quotes are stripped, so model output
is not rejected merely for using ``\`` on Windows or quoting a path.

Consequences
------------

* Escape attempts are impossible by construction for read-only tools, and the
  property is covered by a dedicated test class rather than by review.
* A legitimate link that leaves the project is refused. The workaround is to
  open the parent directory. That is the right trade: a security boundary that
  can be relaxed is not a boundary.
* Content is still *data*: an injected instruction in a README cannot become an
  action, because the resulting tool call is still validated here and by the
  permission broker.
* No OS-level isolation. The agent runs with the user's own privileges, so the
  tool set is the second half of the defence. See
  :doc:`../security-boundaries`.
* Writes, when they arrive in v0.2, must go through this class too -- which is
  precisely why it is a class rather than a set of helper functions.

Rejected alternative, restated
------------------------------

A sandbox process would eventually be the strongest answer, but it is a
distribution and support burden. Containment in one reviewable class delivers the
practical guarantee now, and a sandbox can wrap it later without changing the
tools.