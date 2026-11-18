"""Shortcut map dialog (WP4.1 of v1.3)."""

from __future__ import annotations

from PySide6.QtWidgets import QDialog, QListWidget, QVBoxLayout, QWidget

__all__ = ["SHORTCUTS", "ShortcutMapDialog"]


SHORTCUTS: tuple[tuple[str, str], ...] = (
    ("Ctrl+1..5", "Switch destination (Home / AI / Studio / ChangeSets / Control)"),
    ("Ctrl+O", "Open project"),
    ("Ctrl+K", "Settings"),
    ("Ctrl+J", "Toggle bottom panel (Problems / Terminal / Git)"),
    ("Ctrl+Shift+P", "Command palette"),
    ("Ctrl+Space", "LSP completion (editor)"),
    ("Ctrl+Click", "Go to definition (editor)"),
    ("Left / Right", "Next/previous file in ChangeSets review"),
    ("Enter", "Activate palette result"),
    ("Esc", "Close dialog"),
)


class ShortcutMapDialog(QDialog):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("ShortcutMapDialog")
        self.setWindowTitle("Keyboard shortcuts")
        layout = QVBoxLayout(self)
        self.list = QListWidget()
        for keys, desc in SHORTCUTS:
            self.list.addItem(f"{keys} — {desc}")
        layout.addWidget(self.list)
