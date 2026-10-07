"""Filesystem locations used by QuantsMind-Agent.

All locations follow platform conventions via :mod:`platformdirs`:

* Windows 11: ``%LOCALAPPDATA%\\QuantsMind-Agent\\QuantsMind-Agent``
* Linux: ``~/.local/share/quantsmind-agent``
* macOS: ``~/Library/Application Support/quantsmind-agent``

Directory layout::

    <user_config_dir>/
        config.toml          non-secret settings
        secrets.json         credentials (0600, never committed)
        logs/
            quantsmind-agent.log
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from platformdirs import PlatformDirs

from quantsmind_agent.version import APP_NAME, APP_SLUG

__all__ = ["AppPaths", "default_paths"]

_ENV_CONFIG_DIR = "QM_CONFIG_DIR"
_ENV_LOG_DIR = "QM_LOG_DIR"


def _resolve(base: Path) -> Path:
    """Expand environment variables and ``~`` inside ``base``."""

    return Path(os.path.expandvars(str(base))).expanduser()


@dataclass(frozen=True, slots=True)
class AppPaths:
    """Resolved per-user directories for configuration, secrets and logs."""

    config_dir: Path
    log_dir: Path
    cache_dir: Path
    data_dir: Path

    @property
    def config_file(self) -> Path:
        return self.config_dir / "config.toml"

    @property
    def secrets_file(self) -> Path:
        return self.config_dir / "secrets.json"

    @property
    def log_file(self) -> Path:
        return self.log_dir / "quantsmind-agent.log"

    def ensure(self) -> AppPaths:
        """Create the directories if they do not exist yet."""

        for directory in (self.config_dir, self.log_dir, self.cache_dir, self.data_dir):
            directory.mkdir(parents=True, exist_ok=True)
        return self

    def __str__(self) -> str:  # pragma: no cover - trivial
        return str(self.config_dir)

    def __fspath__(self) -> str:  # pragma: no cover - convenience
        return str(self.config_dir)


def default_paths() -> AppPaths:
    """Return the standard per-user paths, honouring QM_* environment overrides.

    On Windows 11 this yields
    ``%LOCALAPPDATA%\\QuantsMind-Agent\\{config.toml,secrets.json,Logs\\...}``.
    """

    dirs = PlatformDirs(appname=APP_NAME, appauthor=False, roaming=False)

    config_override = os.environ.get(_ENV_CONFIG_DIR)
    log_override = os.environ.get(_ENV_LOG_DIR)

    config_dir = _resolve(Path(config_override)) if config_override else Path(dirs.user_config_dir)
    log_dir = _resolve(Path(log_override)) if log_override else Path(dirs.user_log_dir)

    return AppPaths(
        config_dir=config_dir,
        log_dir=log_dir,
        cache_dir=Path(dirs.user_cache_dir),
        data_dir=Path(dirs.user_data_dir),
    )


def default_paths_for(app_slug: str = APP_SLUG) -> AppPaths:
    """Backwards-compatible alias kept for readability at call sites."""

    _ = app_slug
    return default_paths()
