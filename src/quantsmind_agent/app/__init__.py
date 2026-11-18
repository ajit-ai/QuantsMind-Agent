"""Headless application composition root and entry points.

``ApplicationContext`` is re-exported lazily so importing the package does not
pull in the provider stack; ``main`` is resolved from :mod:`quantsmind_agent.app.cli`
on demand, which keeps the ``quantsmind_agent.app:main`` entry point valid.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

__all__ = [
    "ApplicationContext",
    "ReadinessCode",
    "ReadinessReport",
    "main",
]

if TYPE_CHECKING:  # pragma: no cover - typing only
    # The runtime names are resolved lazily by ``__getattr__`` below. Declaring
    # them here lets type checkers see real definitions instead of a dynamic
    # placeholder, without importing the provider stack at runtime. The ``as``
    # form marks each name as an explicit re-export.
    from quantsmind_agent.app.cli import (
        _build_parser as _build_parser,
    )
    from quantsmind_agent.app.cli import (
        _paths_from_args as _paths_from_args,
    )
    from quantsmind_agent.app.cli import (
        main as main,
    )
    from quantsmind_agent.app.context import (
        ApplicationContext as ApplicationContext,
    )
    from quantsmind_agent.app.context import (
        ReadinessCode as ReadinessCode,
    )
    from quantsmind_agent.app.context import (
        ReadinessReport as ReadinessReport,
    )


def __getattr__(name: str) -> object:
    if name in ("ApplicationContext", "ReadinessCode", "ReadinessReport"):
        from quantsmind_agent.app import context

        return getattr(context, name)
    if name in ("_build_parser", "_paths_from_args", "main"):
        from quantsmind_agent.app import cli

        return getattr(cli, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
