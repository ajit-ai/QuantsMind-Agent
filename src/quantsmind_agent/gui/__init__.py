"""Qt GUI package.

Everything under :mod:`quantsmind_agent.gui` requires PySide6. The headless core
(:mod:`quantsmind_agent.runtime.agent.core`, :mod:`quantsmind_agent.providers.gateway`, ...) never imports
Qt, which keeps unit and integration tests fast and headless-safe.
"""

from __future__ import annotations

__all__ = ["ActivityView", "ConversationView", "MainWindow", "Sidebar"]


def __getattr__(name: str) -> object:
    """Import GUI classes lazily so ``import quantsmind_agent.gui`` is cheap."""

    mapping = {
        "ActivityView": ("quantsmind_agent.gui.conversation.activity", "ActivityView"),
        "ConversationView": ("quantsmind_agent.gui.conversation.transcript", "ConversationView"),
        "MainWindow": ("quantsmind_agent.gui.shell.window", "MainWindow"),
        "Sidebar": ("quantsmind_agent.gui.shell.sidebar", "Sidebar"),
    }
    if name not in mapping:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module_name, attribute = mapping[name]
    import importlib

    return getattr(importlib.import_module(module_name), attribute)
