"""Qt entry point for the desktop application.

Kept separate from :mod:`quantsmind_agent.app.context` so the composition root
and the headless commands import no Qt modules. PySide6 is imported inside the
entry functions, which is what lets tests exercise the rest of the package on a
machine without a display.
"""

from __future__ import annotations

import json
import os
import sys

from quantsmind_agent.app.context import ApplicationContext
from quantsmind_agent.diagnostics.logging import get_logger

__all__ = ["_run_describe_window", "_run_gui"]

logger = get_logger(__name__)


def _run_describe_window(context: ApplicationContext) -> int:
    """Report the window's composition, then exit without running it.

    The acceptance harness watches the process from the outside: it can prove a
    window was created, but not what is in it. Building the real window here and
    printing its structure is how the shipped executable is shown to carry the
    task workflow -- otherwise only the source tree proves it.
    """

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

    from PySide6.QtWidgets import QApplication

    from quantsmind_agent.gui.shell.style import apply_theme
    from quantsmind_agent.gui.shell.window import MainWindow

    app = QApplication.instance() or QApplication([sys.argv[0]])
    apply_theme(app)
    window = MainWindow(context)
    description = window.describe_structure()
    window.close()
    print(json.dumps(description, indent=2, sort_keys=True))
    return 0


def _run_gui(context: ApplicationContext) -> int:
    """Start the Qt application. Imported lazily so headless tests skip Qt."""

    from PySide6.QtWidgets import QApplication

    from quantsmind_agent.gui.shell.style import apply_theme
    from quantsmind_agent.gui.shell.window import MainWindow

    app = QApplication.instance() or QApplication(sys.argv[:1])
    apply_theme(app)

    # The window owns its worker bridge; it resolves the agent per turn.
    window = MainWindow(context)
    ready, reason = context.readiness()
    logger.info("Startup readiness: %s (%s)", "ready" if ready else "not ready", reason)
    window.show()
    return app.exec()
