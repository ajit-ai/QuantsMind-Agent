"""QuantsMind-Agent -- an AI coding-agent platform.

QuantsMind-Agent is an AI coding-agent platform inspired by modern
coding-agent workflows. This package contains the v0.1 vertical slice:

* a desktop GUI (PySide6),
* an Agent Core that can call controlled workspace tools,
* a provider-independent Model Gateway,
* a workspace abstraction with path-traversal protection,
* a permission layer, and
* structured agent events for observability.
"""

from __future__ import annotations

from quantsmind_agent.version import (
    __version__,
    version_info,
)

__all__ = ["__version__", "version_info"]
