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

from quantsmind_agent.config import ConfigManager


def _select_qpa() -> str | None:
    """Choose and install a headless QPA platform plugin."""

    if sys.platform == "win32" or (
        not os.environ.get("DISPLAY") and sys.platform.startswith("linux")
    ):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    return os.environ.get("QT_QPA_PLATFORM")


_select_qpa()


@pytest.fixture(scope="session")
def qapp() -> Iterator[object]:
    """A single ``QApplication`` shared by every GUI test."""

    pytest.importorskip("PySide6.QtWidgets", reason="PySide6 is not installed")
    from PySide6.QtWidgets import QApplication

    from quantsmind_agent.gui.style import apply_theme

    app = QApplication.instance() or QApplication(["quantsmind-agent-tests"])
    apply_theme(app)
    yield app
    app.processEvents()


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
    harness.window.close()
