"""Run the Qt tests, then exit before PySide6 can crash during finalisation.

CPython 3.11 aborts with ``none_dealloc: deallocating None`` while finalising
PySide6 objects after the ``QApplication`` has been torn down. It shows up on
Ubuntu CI as::

    75 passed in 36.90s
    Fatal Python error: none_dealloc: deallocating None
    ##[error]Process completed with exit code 134.

Every test passes; the process dies in interpreter shutdown. This is an
upstream finalisation bug, not a defect in the tests or the application, and it
cannot be reproduced on every interpreter.

This runner executes pytest exactly as usual, disposes the remaining Qt state
while the application is still alive, and then exits with *pytest's own* exit
code. A genuine failure still fails the build; only the interpreter's unsafe
finalisation is skipped.

Usage::

    python scripts/run_qt_tests.py tests/gui -q
"""

from __future__ import annotations

import contextlib
import os
import sys
from typing import NoReturn


def _dispose_qt() -> None:
    """Destroy leftover Qt state while the ``QApplication`` still exists."""

    try:
        import shiboken6
        from PySide6.QtCore import QCoreApplication, QEvent, QEventLoop
        from PySide6.QtWidgets import QApplication
    except ImportError:  # pragma: no cover - PySide6 is a hard test dep
        return

    if QCoreApplication.instance() is None:  # pragma: no cover - no app ran
        return

    for widget in QApplication.topLevelWidgets():
        try:
            widget.close()
            # These top-level widgets are Python owned, so Qt must not free the
            # C++ object behind an unaware wrapper.
            shiboken6.delete(widget)
        except RuntimeError:  # pragma: no cover - already deleted
            continue

    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    for _ in range(5):
        QCoreApplication.processEvents(QEventLoop.ProcessEventsFlag.AllEvents, 20)

    application = QCoreApplication.instance()
    if application is not None:  # pragma: no branch - an app ran to get here
        application.processEvents()
        application.quit()


def main(argv: list[str]) -> NoReturn:
    import pytest

    exit_code = int(pytest.main(argv))

    _dispose_qt()

    for stream in (sys.stdout, sys.stderr):
        with contextlib.suppress(Exception):  # pragma: no cover - defensive
            stream.flush()

    # Propagate pytest's verdict, then leave before CPython finalises Qt objects
    # against a destroyed QApplication.
    os._exit(exit_code)


if __name__ == "__main__":
    main(sys.argv[1:] or ["tests/gui"])
