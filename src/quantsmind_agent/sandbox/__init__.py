"""The sandbox package: an OS-enforced execution boundary (M3B-S1.4).

The sandbox exists because M3B-2P found that a command allowlist is not
an execution boundary. A fixed command still executes the code the model
wrote, and that code runs with the desktop user's Windows privileges unless
something prevents it.

The design is fail-closed:

* a :class:`~quantsmind_agent.sandbox.policy.SandboxPolicy` states the
  isolation the caller needs, before the approval is granted;
* the runner establishes what the host can enforce, and
  :mod:`quantsmind_agent.sandbox.probe` *measures* what was actually
  enforced;
* if the measurement is below the policy's requirement, the command is not
  run. There is no fallback to an ordinary subprocess.
"""

from __future__ import annotations

from quantsmind_agent.sandbox.policy import (
    ISOLATION_LEVELS,
    IsolationLevel,
    SandboxError,
    SandboxPolicy,
    SandboxResult,
)

__all__ = [
    "ISOLATION_LEVELS",
    "IsolationLevel",
    "SandboxError",
    "SandboxPolicy",
    "SandboxResult",
]
