"""Qt styling for QuantsMind-Agent.

A single stylesheet keeps the application visually consistent and easy to
rebrand. Colours are chosen for contrast on Windows 11 light and dark themes;
the palette is applied via dynamic properties rather than by forcing a theme.
"""

from __future__ import annotations

__all__ = ["COLORS", "ROLE_COLORS", "STYLESHEET", "apply_theme"]

COLORS = {
    "accent": "#3b6fd4",
    "accent_hover": "#2f5cb5",
    "panel": "#f5f6f8",
    "border": "#d7dae0",
    "text": "#1c1f24",
    "muted": "#6a7280",
    "success": "#1f7a45",
    "warning": "#b06a00",
    "error": "#b3261e",
    "tool": "#5a3ea8",
}

#: Accent colours per conversation role.
ROLE_COLORS = {
    "user": COLORS["accent"],
    "assistant": COLORS["text"],
    "system": COLORS["muted"],
    "tool": COLORS["tool"],
    "error": COLORS["error"],
}

STYLESHEET = f"""
QMainWindow, QDialog {{
    background: {COLORS["panel"]};
}}

QGroupBox {{
    background: white;
    border: 1px solid {COLORS["border"]};
    border-radius: 8px;
    margin-top: 14px;
    padding: 10px;
    font-weight: 600;
}}
QGroupBox::title {{
    subcontrol-origin: margin;
    left: 10px;
    padding: 0 4px;
    color: {COLORS["muted"]};
}}

QLabel#SectionLabel {{
    color: {COLORS["muted"]};
    font-size: 11px;
    text-transform: uppercase;
}}

QLabel#StatusText {{
    color: {COLORS["muted"]};
    padding: 0 6px;
}}

QLabel#ErrorText {{
    color: {COLORS["error"]};
    font-weight: 600;
}}

QLineEdit, QPlainTextEdit, QTextEdit, QSpinBox, QDoubleSpinBox, QComboBox {{
    background: white;
    border: 1px solid {COLORS["border"]};
    border-radius: 6px;
    padding: 5px 7px;
    selection-background-color: {COLORS["accent"]};
}}

QLineEdit:focus, QPlainTextEdit:focus, QTextEdit:focus,
QSpinBox:focus, QDoubleSpinBox:focus, QComboBox:focus {{
    border: 1px solid {COLORS["accent"]};
}}

QLineEdit:read-only {{
    background: #eef0f3;
    color: {COLORS["muted"]};
}}

QPushButton {{
    background: white;
    border: 1px solid {COLORS["border"]};
    border-radius: 6px;
    padding: 6px 14px;
    min-height: 18px;
}}
QPushButton:hover {{
    border-color: {COLORS["accent"]};
}}
QPushButton:pressed {{
    background: #eceff4;
}}
QPushButton:disabled {{
    color: #a8adb6;
    background: #f0f1f4;
}}
QPushButton#PrimaryButton {{
    background: {COLORS["accent"]};
    color: white;
    border-color: {COLORS["accent"]};
    font-weight: 600;
}}
QPushButton#PrimaryButton:hover {{
    background: {COLORS["accent_hover"]};
}}
QPushButton#PrimaryButton:disabled {{
    background: #a9bde3;
    border-color: #a9bde3;
}}

QPushButton#DangerButton {{
    background: {COLORS["error"]};
    color: white;
    border-color: {COLORS["error"]};
}}

QListWidget {{
    background: white;
    border: 1px solid {COLORS["border"]};
    border-radius: 8px;
    outline: none;
}}
QListWidget::item {{
    padding: 5px 7px;
    border-bottom: 1px solid #eef0f3;
}}
QListWidget::item:selected {{
    background: #e6edfb;
    color: {COLORS["text"]};
}}

QTextBrowser {{
    background: white;
    border: 1px solid {COLORS["border"]};
    border-radius: 8px;
    padding: 8px;
}}

QScrollBar:vertical {{
    background: transparent;
    width: 10px;
    margin: 2px;
}}
QScrollBar::handle:vertical {{
    background: #c2c7cf;
    border-radius: 5px;
    min-height: 30px;
}}
QScrollBar::add-line, QScrollBar::sub-line {{
    height: 0;
}}

QSplitter::handle {{
    background: transparent;
    width: 8px;
}}

QStatusBar {{
    background: white;
    border-top: 1px solid {COLORS["border"]};
}}

QToolTip {{
    background: #2b2f36;
    color: white;
    border: none;
    padding: 4px 6px;
}}
"""


def apply_theme(app: object) -> None:
    """Apply the application stylesheet to a ``QApplication``-like object."""

    set_style = getattr(app, "setStyleSheet", None)
    if callable(set_style):  # pragma: no branch - trivial
        set_style(STYLESHEET)
