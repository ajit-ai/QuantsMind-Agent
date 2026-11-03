"""Measure what the sandbox boundary actually enforces (M3B-S1.4).

A design is not a boundary. This module *runs a synthetic child* inside the
candidate boundary and asks it to do the things untrusted code would try
to do:

* read a canary file that lives **outside** the sandbox;
* read the user's credential store;
* connect to a controlled local listener;
* inherit a credential variable from the parent;
* spawn a child process.

The answers are the evidence. The runner uses them: a policy that demands
``untrusted_code`` isolation is refused when the measurement shows the
boundary is only a *lifecycle* one.

**The probe's own payload is synthetic.** It uses a canary file the probe
creates and deletes, a listener bound to the loopback interface on an
ephemeral port, and a canary environment variable. It never reads a real
credential, and it never contacts the public internet.
"""

from __future__ import annotations

import contextlib
import json
import os
import socket
import tempfile
import threading
from pathlib import Path
from typing import Any

from quantsmind_agent.sandbox.policy import IsolationLevel, SandboxPolicy
from quantsmind_agent.sandbox.windows import launch

__all__ = [
    "MANDATORY_CHECKS",
    "IsolationReport",
    "build_probe_policy",
    "measure_isolation",
]

#: The properties that must hold for model-written code to run. Every one of
#: them is measured **from inside the child**, by a payload the application
#: controls, not by the parent's configuration.
MANDATORY_CHECKS = (
    "read_outside_file",
    "read_credential_store",
    "connect_local_listener",
)

#: The payload the probe child runs. It is written into the sandbox's own
#: temporary directory and executed there.
PROBE_SOURCE = r"""
import json, os, socket, sys

report = {"checks": {}}

#: The names whose presence in the child would mean a credential leaked.
CREDENTIAL_ENV_NAMES = (
    "QM_API_KEY",
    "OPENAI_API_KEY",
    "OPENROUTER_API_KEY",
    "ANTHROPIC_API_KEY",
    "AZURE_OPENAI_API_KEY",
    "GOOGLE_API_KEY",
    "GEMINI_API_KEY",
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
    "AWS_SESSION_TOKEN",
    "AZURE_CLIENT_SECRET",
    "GITHUB_TOKEN",
    "GH_TOKEN",
    "NPM_TOKEN",
)


def _can(name, fn):
    try:
        report["checks"][name] = bool(fn())
    except Exception as exc:
        report["checks"][name] = False
        report.setdefault("errors", {})[name] = type(exc).__name__


_outside = os.environ["PROBE_OUTSIDE_FILE"]
_credential = os.environ["PROBE_CREDENTIAL_FILE"]
_listener = int(os.environ["PROBE_LISTENER_PORT"])



def read_outside():
    with open(_outside, "r", encoding="utf-8") as handle:
        return handle.read()


def read_credential_store():
    with open(_credential, "r", encoding="utf-8") as handle:
        return handle.read()


def connect_local():
    with socket.create_connection(("127.0.0.1", _listener), timeout=3) as sock:
        sock.sendall(b"probe")
        return sock.recv(16)


def spawn_child():
    import subprocess
    completed = subprocess.run(
        [sys.executable, "-c", "print('child')"],
        capture_output=True, text=True, timeout=30,
    )
    return completed.stdout.strip() == "child"


_can("read_outside_file", read_outside)
_can("read_credential_store", read_credential_store)
_can("connect_local_listener", connect_local)
_can("spawn_child_process", spawn_child)
report["credential_env_names_present"] = sorted(
    name for name in CREDENTIAL_ENV_NAMES if name in os.environ
)
report["userprofile"] = os.environ.get("USERPROFILE", "")
report["appdata"] = os.environ.get("APPDATA", "")
report["localappdata"] = os.environ.get("LOCALAPPDATA", "")
sys.stdout.write(json.dumps(report))
"""


class IsolationReport:
    """What the probe measured."""

    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload

    @property
    def checks(self) -> dict[str, bool]:
        return dict(self._payload.get("checks", {}))

    @property
    def credential_env_leaked(self) -> bool:
        """Whether a credential variable reached the child."""

        return bool(self._payload.get("credential_env_names_present"))

    @property
    def level(self) -> IsolationLevel:
        """The isolation level the measurement actually supports."""

        checks = self.checks
        if not checks:
            return IsolationLevel.LIFECYCLE
        confidentiality = (
            not checks.get("read_outside_file", True)
            and not checks.get("read_credential_store", True)
            and not checks.get("connect_local_listener", True)
            and not self.credential_env_leaked
        )
        if confidentiality:
            return IsolationLevel.UNTRUSTED_CODE
        return IsolationLevel.LIFECYCLE

    def to_dict(self) -> dict[str, Any]:
        return {
            "checks": self.checks,
            "credential_env_leaked": self.credential_env_leaked,
            "credential_env_names_present": self._payload.get("credential_env_names_present", []),
            "established_level": str(self.level),
            "errors": self._payload.get("errors", {}),
        }


def build_probe_policy(
    policy: SandboxPolicy,
    *,
    sandbox_home: Path | None = None,
) -> SandboxPolicy:
    """The policy for the *trusted* probe.

    The probe is a payload the application controls, written into a scratch
    directory of its own. It is what establishes the boundary, so it runs
    first and it runs inside the same boundary as the code it is judging.
    """

    base = Path(sandbox_home) if sandbox_home else Path(policy.workspace_root) / ".sandbox"
    base.mkdir(parents=True, exist_ok=True)
    script = base / "qm_isolation_probe.py"
    script.write_text(PROBE_SOURCE, encoding="utf-8")
    return SandboxPolicy(
        workspace_root=Path(policy.workspace_root),
        allowed_command=(policy.allowed_command[0], str(script)),
        timeout_seconds=60.0,
        max_output_bytes=100_000,
        max_child_processes=4,
        max_memory_bytes=512 * 1024 * 1024,
        required_isolation=IsolationLevel.LIFECYCLE,
        reason="trusted isolation probe",
    )


class ProbeHarness:
    """The parent side of the probe: the canaries, the listener, the policy.

    The harness owns everything the child needs to *try* to do the forbidden
    things, so a denial is meaningful: the canary is known to be readable by
    this process, and the listener is known to be reachable by this process.
    """

    def __init__(self, policy: SandboxPolicy) -> None:
        self._policy = policy
        self._tmp = tempfile.TemporaryDirectory(prefix="qm-probe-")
        self._base = Path(self._tmp.name)
        self._outside = self._base / "outside-canary.txt"
        self._outside.write_text("outside-canary-value", encoding="utf-8")
        self._credential = self._base / "credential-canary.txt"
        self._credential.write_text("credential-canary-value", encoding="utf-8")
        self._listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._listener.bind(("127.0.0.1", 0))
        self._listener.listen(1)
        self._port = self._listener.getsockname()[1]
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self) -> None:
        self._listener.settimeout(5.0)
        try:
            connection, _address = self._listener.accept()
            with connection:
                connection.settimeout(2.0)
                try:
                    connection.recv(16)
                    connection.sendall(b"ok")
                except OSError:
                    pass
        except OSError:
            pass
        finally:
            self._stop.set()

    def control_check(self) -> dict[str, bool]:
        """What the *authorised* control process can do.

        The acceptance criterion for a denial is that the canary is
        reachable from here. Without this, a denial proves nothing.
        """

        checks: dict[str, bool] = {}
        try:
            checks["control_reads_outside_file"] = bool(self._outside.read_text(encoding="utf-8"))
        except OSError:
            checks["control_reads_outside_file"] = False
        try:
            checks["control_reads_credential_canary"] = bool(
                self._credential.read_text(encoding="utf-8")
            )
        except OSError:
            checks["control_reads_credential_canary"] = False
        try:
            with socket.create_connection(("127.0.0.1", self._port), timeout=3) as sock:
                sock.sendall(b"probe")
                checks["control_reaches_listener"] = bool(sock.recv(16))
        except OSError:
            checks["control_reaches_listener"] = False
        return checks

    def policy_for(self, base: SandboxPolicy) -> SandboxPolicy:
        """The probe's policy, with the canary locations injected.

        The command is the *trusted probe script*, written into the harness's
        own scratch directory, not the caller's command.
        """

        script = self._base / "qm_isolation_probe.py"
        script.write_text(PROBE_SOURCE, encoding="utf-8")
        interpreter = base.allowed_command[0]
        return SandboxPolicy(
            workspace_root=Path(base.workspace_root),
            allowed_command=(interpreter, str(script)),
            timeout_seconds=base.timeout_seconds,
            max_output_bytes=base.max_output_bytes,
            max_child_processes=base.max_child_processes,
            max_memory_bytes=base.max_memory_bytes,
            required_isolation=IsolationLevel.LIFECYCLE,
            injected_environment={
                "PROBE_OUTSIDE_FILE": str(self._outside),
                "PROBE_CREDENTIAL_FILE": str(self._credential),
                "PROBE_LISTENER_PORT": str(self._port),
            },
            reason="trusted isolation probe",
        )

    def close(self) -> None:
        self._stop.set()
        with contextlib.suppress(OSError):
            self._listener.close()
        self._thread.join(timeout=2.0)
        self._tmp.cleanup()

    def __enter__(self) -> ProbeHarness:
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()


def measure_isolation(
    policy: SandboxPolicy,
    *,
    sandbox_home: Path | None = None,
) -> IsolationReport:
    """Run the probe inside the boundary and report what was enforced."""

    with tempfile.TemporaryDirectory(prefix="qm-probe-") as tmp:
        base = Path(tmp)
        outside = base / "outside-canary.txt"
        outside.write_text("outside-canary-value", encoding="utf-8")
        credential = base / "credential-canary.txt"
        credential.write_text("credential-canary-value", encoding="utf-8")

        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        port = listener.getsockname()[1]
        stop = threading.Event()

        def serve() -> None:
            listener.settimeout(5.0)
            try:
                connection, _address = listener.accept()
                with connection:
                    connection.settimeout(2.0)
                    try:
                        connection.recv(16)
                        connection.sendall(b"ok")
                    except OSError:
                        pass
            except OSError:
                pass
            finally:
                stop.set()

        thread = threading.Thread(target=serve, daemon=True)
        thread.start()
        try:
            probe_script = base / "probe_child.py"
            probe_script.write_text(PROBE_SOURCE, encoding="utf-8")
            injected = {
                "PROBE_OUTSIDE_FILE": str(outside),
                "PROBE_CREDENTIAL_FILE": str(credential),
                "PROBE_LISTENER_PORT": str(port),
            }
            probe_policy = SandboxPolicy(
                workspace_root=policy.workspace_root,
                allowed_command=(
                    policy.allowed_command[0],
                    str(probe_script),
                ),
                timeout_seconds=60.0,
                max_output_bytes=100_000,
                max_child_processes=4,
                max_memory_bytes=512 * 1024 * 1024,
                required_isolation=IsolationLevel.LIFECYCLE,
                injected_environment=injected,
                reason="isolation probe",
            )
            result = launch(probe_policy, sandbox_home=sandbox_home)
            try:
                payload = json.loads(result.stdout or "{}")
            except ValueError:
                payload = {"checks": {}, "parse_error": result.stdout[:500]}
            return IsolationReport(payload)
        finally:
            stop.set()
            listener.close()
            thread.join(timeout=2.0)
            os.environ.pop("PROBE_ENV_CANARY", None)
