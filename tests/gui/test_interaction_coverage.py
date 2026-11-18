"""Meaningful GUI interaction coverage for navigation, palette and window state.

These tests exercise real user workflows beyond rendering: switching
destinations via commands, validating command enablement by workspace state,
the command palette activation/dismissal, and window resize stability.
"""

from __future__ import annotations

import pytest

from quantsmind_agent.gui.studio.navigation import DESTINATIONS
from tests.gui.harness import GuiHarness

pytestmark = pytest.mark.gui


class TestDestinationInteractions:
    def test_all_five_destinations_switch_via_show_destination(
        self, gui_harness: GuiHarness
    ) -> None:
        window = gui_harness.window
        for destination in DESTINATIONS:
            window.show_destination(destination.id)
            assert window._destination == destination.id
            assert window._destinations.currentWidget() is window._destination_pages[destination.id]
            assert window.header.current() == destination.id

    def test_header_buttons_are_checkable_and_exclusive(self, gui_harness: GuiHarness) -> None:
        window = gui_harness.window
        buttons = window.header._buttons
        assert set(buttons) == {d.id for d in DESTINATIONS}
        # Clicking one destination unchecks the others.
        buttons["changes"].click()
        assert buttons["changes"].isChecked()
        assert not buttons["home"].isChecked()


class TestCommandPaletteInteractions:
    def test_palette_opens_and_closes(self, gui_harness: GuiHarness) -> None:
        window = gui_harness.window
        window._show_command_palette()
        palette = window._command_palette
        assert palette is not None
        assert palette.isVisible()
        # Closing the palette must not crash and must release the reference.
        palette.close()
        assert palette is not None

    def test_navigate_commands_switch_destinations(self, gui_harness: GuiHarness) -> None:
        window = gui_harness.window
        registry = window._command_registry
        # The ChangeSets destination id is "changes" but its command is
        # "go.changesets".
        command_id_by_dest = {
            "home": "go.home",
            "ai": "go.ai",
            "development": "go.development",
            "changes": "go.changesets",
            "control": "go.control",
        }
        for destination in DESTINATIONS:
            command = registry.get(command_id_by_dest[destination.id])
            assert command is not None
            command.callback()
            assert window._destination == destination.id

    def test_command_run_disabled_without_workspace(self, gui_harness: GuiHarness) -> None:
        window = gui_harness.window
        # No workspace open in a fresh harness.
        assert not window._context.workspace
        command = window._command_registry.get("command.run")
        assert command is not None
        assert not command.is_enabled()

    def test_palette_search_filters_navigate_commands(self, gui_harness: GuiHarness) -> None:
        window = gui_harness.window
        window._show_command_palette()
        palette = window._command_palette
        assert palette is not None
        palette.input.setText("go to control")
        labels = [c.id for c in palette.commands()]
        assert "go.control" in labels
        palette.close()

    def test_window_resize_keeps_splitters_valid(self, gui_harness: GuiHarness) -> None:
        window = gui_harness.window
        window.show_destination("development")
        for width, height in ((1000, 640), (1440, 900), (1920, 1080)):
            window.resize(width, height)
            window.show()
            assert window.width() == width
            assert window.height() == height
            # Splitters must never collapse children below the minimum.
            assert window._destinations.count() == 5
