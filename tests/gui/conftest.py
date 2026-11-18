"""Pytest configuration for GUI tests.

GUI tests instantiate real Qt widgets, so they need a QPA plugin that works
without a visible desktop. On Windows that is Qt's ``offscreen`` platform; on
Linux CI it is ``xvfb``. If no platform is available the GUI tests are skipped
rather than failing the suite.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

if str(Path(__file__).resolve().parents[2] / "src") not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from quantsmind_agent.app.settings import ConfigManager


def _select_qpa() -> str | None:
    """Choose and install a headless QPA platform plugin."""

    if sys.platform == "win32" or (
        not os.environ.get("DISPLAY")
        and not os.environ.get("WAYLAND_DISPLAY")
        # Linux has no usable default without an X or Wayland display. macOS
        # uses Cocoa, which needs nothing, so only force it off when we are
        # clearly running headless -- otherwise a developer on a real macOS
        # desktop would get offscreen rendering with no display to show it on.
        and (sys.platform.startswith("linux") or os.environ.get("CI"))
    ):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    return os.environ.get("QT_QPA_PLATFORM")


_select_qpa()


@pytest.fixture(scope="session")
def qapp() -> Iterator[object]:
    """A single ``QApplication`` shared by every GUI test."""

    pytest.importorskip("PySide6.QtWidgets", reason="PySide6 is not installed")
    from PySide6.QtWidgets import QApplication

    from quantsmind_agent.gui.shell.style import apply_theme

    app = QApplication.instance() or QApplication(["quantsmind-agent-tests"])
    apply_theme(app)
    yield app
    _dispose_top_level_widgets()
    app.processEvents()


def _dispose_top_level_widgets() -> None:
    """Destroy every surviving top-level widget while Qt is still alive.

    A ``QWidget`` that outlives its ``QApplication`` has its C++ object torn
    down after Qt is gone, which aborts the interpreter at shutdown with
    ``none_dealloc: deallocating None`` -- seen on CPython 3.11 in CI. Tests
    should close their own dialogs; this is the backstop that keeps one leaky
    test from taking down the whole suite, and it runs at session teardown,
    when the application is guaranteed to still exist.
    """

    from PySide6.QtCore import QCoreApplication, QEvent, QEventLoop
    from PySide6.QtWidgets import QApplication

    for widget in QApplication.topLevelWidgets():
        try:
            widget.close()
            widget.deleteLater()
        except RuntimeError:  # pragma: no cover - already deleted
            continue

    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    for _ in range(5):
        QCoreApplication.processEvents(QEventLoop.ProcessEventsFlag.AllEvents, 20)


@pytest.fixture
def gui_harness(qapp: object, config_manager: ConfigManager) -> Iterator[object]:
    """A real window wired to a scripted (offline) model provider."""

    pytest.importorskip("PySide6.QtWidgets", reason="PySide6 is not installed")
    from quantsmind_agent.app import ApplicationContext
    from tests.fakes import FakeModelProvider
    from tests.gui.harness import GuiHarness

    context = ApplicationContext.create(paths=config_manager.paths, config_manager=config_manager)
    provider = FakeModelProvider()
    harness = GuiHarness(context, provider)
    harness.install_provider()
    yield harness
    # Deny any dialog still on screen and drain the worker, so a failed test
    # reports instead of leaving a blocked QThread hanging the session.
    harness.shutdown()
