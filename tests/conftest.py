"""Shared pytest fixtures.

Fixtures never touch the real user profile: every test gets an isolated
``QM_CONFIG_DIR`` equivalent through :class:`AppPaths` and an in-memory secret
store, so no test can read or write the developer's credentials.
"""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:  # pragma: no cover - import bootstrap
    sys.path.insert(0, str(SRC))

from pydantic import SecretStr  # noqa: E402

from quantsmind_agent.app.settings import PROVIDER_ENV_VARS, ConfigManager  # noqa: E402
from quantsmind_agent.diagnostics.logging import shutdown_logging  # noqa: E402
from quantsmind_agent.persistence.paths import AppPaths  # noqa: E402
from quantsmind_agent.security.secrets import SecretStore  # noqa: E402
from quantsmind_agent.services.workspace.containment import Workspace  # noqa: E402

#: Every variable the application may read as a credential. Ambient values are
#: removed for every test so a developer's real key can never influence a run.
_API_KEY_ENV_VARS = ("QM_API_KEY", *PROVIDER_ENV_VARS.values())


class InMemorySecretStore(SecretStore):
    """A secure, process-local store. Nothing is written to the user profile.

    It claims ``is_secure`` because the production policy refuses to persist
    credentials in a plaintext store; tests that need the degraded path pass
    their own ``FileSecretStore`` instead.
    """

    def __init__(self) -> None:
        self._values: dict[str, str] = {}

    @property
    def is_secure(self) -> bool:
        return True

    def get(self, key: str) -> SecretStr | None:
        value = self._values.get(key)
        return SecretStr(value) if value else None

    def set(self, key: str, value: SecretStr) -> None:
        self._values[key] = value.get_secret_value()

    def delete(self, key: str) -> bool:
        return self._values.pop(key, None) is not None

    def keys(self) -> list[str]:
        return sorted(self._values)


@pytest.fixture(autouse=True)
def _isolate_environment(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Remove ambient credentials so tests never depend on the developer env."""

    for name in _API_KEY_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    yield


@pytest.fixture(autouse=True)
def _quiet_logging() -> Iterator[None]:
    """Keep pytest output clean; each test configures logging if it needs to."""

    import logging

    previous = logging.getLogger("quantsmind_agent").level
    logging.getLogger("quantsmind_agent").setLevel(logging.CRITICAL)
    yield
    logging.getLogger("quantsmind_agent").setLevel(previous)
    shutdown_logging()


@pytest.fixture
def app_paths(tmp_path: Path) -> AppPaths:
    """Isolated per-test application directories."""

    return AppPaths(
        config_dir=tmp_path / "config",
        log_dir=tmp_path / "logs",
        cache_dir=tmp_path / "cache",
        data_dir=tmp_path / "data",
    ).ensure()


@pytest.fixture
def config_manager(app_paths: AppPaths) -> ConfigManager:
    """A :class:`ConfigManager` over temp directories and a secure in-memory store."""

    return ConfigManager(app_paths, secret_store=InMemorySecretStore())


@pytest.fixture
def sample_project(tmp_path: Path) -> Path:
    """A small, realistic Python project used by tools, agent and GUI tests."""

    root = tmp_path / "sample_project"
    (root / "src" / "app").mkdir(parents=True)
    (root / "docs").mkdir()
    (root / ".git").mkdir()
    (root / "node_modules" / "junk").mkdir(parents=True)

    (root / "README.md").write_text(
        "# Sample Project\n\nA tiny project used by the QuantsMind-Agent test suite.\n",
        encoding="utf-8",
    )
    (root / "pyproject.toml").write_text(
        '[project]\nname = "sample-project"\nversion = "0.1.0"\n', encoding="utf-8"
    )
    (root / "src" / "app" / "__init__.py").write_text(
        '"""Sample application package."""\n', encoding="utf-8"
    )
    (root / "src" / "app" / "main.py").write_text(
        '"""Entry point."""\n\n\ndef main() -> int:\n    """Start the sample app."""\n'
        '    print("sample app starting")\n    return 0\n\n\nif __name__ == "__main__":\n'
        "    raise SystemExit(main())\n",
        encoding="utf-8",
    )
    (root / "src" / "app" / "settings.py").write_text(
        '"""Configuration loader."""\n\nDEFAULT_PORT = 8080\n\n\ndef load_settings() -> dict:\n'
        '    """Return default settings."""\n    return {"port": DEFAULT_PORT}\n',
        encoding="utf-8",
    )
    (root / "docs" / "usage.md").write_text(
        "# Usage\n\nRun `python -m app.main` from the src directory.\n", encoding="utf-8"
    )
    (root / ".git" / "config").write_text("[core]\n", encoding="utf-8")
    (root / "node_modules" / "junk" / "index.js").write_text("module.exports = 1;\n", "utf-8")
    (root / "logo.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 32)
    return root


@pytest.fixture
def workspace(sample_project: Path) -> Workspace:
    """A :class:`Workspace` bound to :func:`sample_project`."""

    return Workspace(sample_project)


@pytest.fixture
def outward_junction_project(tmp_path: Path) -> Iterator[Path]:
    """A workspace whose outward directory junction points at an outside tree."""

    if os.name != "nt":
        pytest.skip("directory junctions are only available on Windows")

    root = tmp_path / "junction_project"
    (root / "src").mkdir(parents=True)
    (root / "src" / "safe.py").write_text("VALUE = 1\n", encoding="utf-8")
    outside = tmp_path / "outside_project"
    outside.mkdir()
    (outside / "secret.py").write_text("SECRET = 1\n", encoding="utf-8")
    link = root / "linked-outside"

    created = subprocess.run(
        ["cmd.exe", "/d", "/c", "mklink", "/J", str(link), str(outside)],
        capture_output=True,
        check=False,
        text=True,
    )
    if created.returncode != 0:
        pytest.skip(f"could not create a directory junction: {created.stderr or created.stdout}")

    try:
        yield root
    finally:
        if os.path.lexists(link):
            os.rmdir(link)


@pytest.fixture
def escape_target(tmp_path: Path, sample_project: Path) -> Path:
    """A file that exists but lives *outside* the workspace root."""

    outside = tmp_path / "outside_secret.txt"
    outside.write_text("this must never be readable by the agent\n", encoding="utf-8")
    return outside


def pytest_report_header(config: pytest.Config) -> list[str]:
    """Note in the report when the real credential env var is present."""

    present = [name for name in _API_KEY_ENV_VARS if os.environ.get(name)]
    if present:
        return ["environment: real API keys present (never used by the test suite)"]
    return ["environment: no ambient API keys"]
