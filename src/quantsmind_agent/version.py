"""Single source of truth for the QuantsMind-Agent version.

Keep this module dependency-free so that it can be imported from
``pyproject.toml`` tooling, packaging scripts and the application itself.
"""

from __future__ import annotations

from typing import Final

__version__: Final[str] = "0.2.0"
version_info: Final[tuple[int, int, int]] = (0, 2, 0)

#: Human readable product name used in window titles and documentation.
APP_NAME: Final[str] = "QuantsMind-Agent"
#: Reverse-DNS identifier used for user configuration directories.
APP_SLUG: Final[str] = "quantsmind-agent"

__all__ = ["APP_NAME", "APP_SLUG", "__version__", "version_info"]
