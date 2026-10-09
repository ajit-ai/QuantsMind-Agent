"""Smoke test: the window builds, renders and shuts down cleanly.

Covers the "application starts" requirement in an automated way. Rendering is
verified structurally (the widget tree can be rendered to a pixmap) rather than
visually: there is no screenshot assertion here, because offscreen Qt runs
without system fonts and a pixel comparison would be meaningless.
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("PySide6.QtWidgets", reason="PySide6 is required for GUI tests")

from quantsmind_agent.version import __version__, version_info
from tests.gui.harness import GuiHarness

pytestmark = pytest.mark.gui


class TestStartupSmoke:
    def test_window_renders_and_closes(self, gui_harness: GuiHarness, sample_project: Path) -> None:
        window = gui_harness.window
        window.resize(1180, 800)
        window.show()
        gui_harness.settle()

        assert window.isVisible() is True
        # Derived from the version module rather than hard-coded, so bumping the
        # release does not silently require editing this test.
        assert window.windowTitle() == f"QuantsMind-Agent {__version__}"
        assert __version__ == ".".join(str(part) for part in version_info)
        assert window.width() == 1180
        assert window.height() == 800

        # Rendering the widget tree must not raise or produce an empty pixmap.
        pixmap = window.grab()
        assert not pixmap.isNull()
        assert pixmap.width() == 1180

        gui_harness.open_project(sample_project)
        gui_harness.settle()
        assert window.grab().width() == 1180

        window.close()
        gui_harness.settle()
        assert window.isVisible() is False

    def test_close_event_shuts_the_context_down(
        self, gui_harness: GuiHarness, sample_project: Path
    ) -> None:
        gui_harness.open_project(sample_project)
        window = gui_harness.window
        window.show()
        gui_harness.settle()
        window.close()
        gui_harness.settle()
        # shutdown() must be safe when nothing is running.
        assert gui_harness.bridge.is_busy is False

    def test_startup_with_no_workspace_still_builds(self, gui_harness: GuiHarness) -> None:
        window = gui_harness.window
        window.show()
        gui_harness.settle()
        assert gui_harness.context.workspace is None
        assert window.send_button.isEnabled() is True
        window.close()
        gui_harness.settle()
